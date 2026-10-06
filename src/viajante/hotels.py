"""Hotel eligibility, ranking, and the Booking.com / Google Hotels search loop."""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable, Literal, Optional, Protocol, Sequence, Tuple

from viajante.booking import (
    BookingHotelsSource,
    BookingResultsTimeout,
)
from viajante.booking import (
    build_applied_filters as build_booking_filters,
)
from viajante.browser import playwright_available
from viajante.control import checkpoint, controlled, interruptible_sleep
from viajante.flights import DEFAULT_TOP
from viajante.google_flights import SweepTransportError
from viajante.google_hotels import (
    GoogleHotelsSource,
)
from viajante.google_hotels import (
    build_applied_filters as build_google_filters,
)
from viajante.google_hotels_rpc import (
    NON_PROPERTY_TITLES,
    EmptyHotelResults,
    HotelsBlocked,
    HotelsParseMiss,
    HotelsRejected,
)
from viajante.history import recorded_hotels
from viajante.models import (
    FETCH_LANGUAGE,
    AppliedHotelFilters,
    CancellationEvidence,
    HotelOffer,
    HotelPage,
    HotelProvider,
    HotelQuery,
    HotelQueryFailure,
    HotelQueryResult,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    RawHotelCard,
    SearchError,
    SearchErrorCode,
)
from viajante.orchestration import (
    MAX_ATTEMPTS,
    NON_RETRIABLE_CODES,
    classify_failure,
    inter_query_delay_seconds,
    retry_backoff_seconds,
    sweep_inter_query_delay_seconds,
)
from viajante.parsers import (
    has_lodging_evidence_conflict,
    parse_cancellation_evidence,
    parse_lodging_kind,
    parse_price,
    parse_property_type_evidence,
    parse_rating,
    parse_unit_hints,
)
from viajante.quote import HOTEL_CURRENCY_REQUIRED, resolve_quote_currency
from viajante.ratelimit import SKIPLAGGED_RATE_LIMIT_FILE, cooldown_until
from viajante.skiplagged import SkiplaggedRateLimited
from viajante.skiplagged_hotels import (
    SKIPLAGGED_HOTEL_CURRENCY,
    SkiplaggedHotelsSource,
    SkiplaggedNoHotels,
    SkiplaggedParseMiss,
    validate_search_party,
)
from viajante.skiplagged_hotels import (
    build_applied_filters as build_skiplagged_filters,
)
from viajante.storage import default_state_dir


class _HotelSource(Protocol):
    def fetch(
        self,
        query: HotelQuery,
        applied: AppliedHotelFilters,
        limit: int,
    ) -> HotelPage: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


def _normalize_card(card: RawHotelCard) -> Optional[HotelOffer]:
    title = card.title.strip()
    if not title or not card.total_price.strip():
        return None
    if title.casefold() in NON_PROPERTY_TITLES:
        return None
    total_price = parse_price(card.total_price)
    if total_price is None or total_price <= 0:
        return None
    evidence = card.details if card.unit_details is None else card.unit_details
    hints = parse_unit_hints(evidence)
    conflict = has_lodging_evidence_conflict(evidence, title=card.title)
    return HotelOffer(
        title=card.title,
        address=card.address,
        total_price_text=card.total_price,
        total_price=total_price,
        rating=card.rating,
        rating_score=parse_rating(card.rating),
        details=card.details,
        cancellation_evidence=parse_cancellation_evidence(evidence),
        property_type_evidence=PropertyTypeEvidence.UNKNOWN
        if conflict
        else parse_property_type_evidence(evidence),
        lodging_kind=LodgingKind.UNKNOWN
        if conflict
        else parse_lodging_kind(evidence, title=card.title if card.unit_details is None else None),
        lodging_evidence_conflict=conflict,
        bedrooms=hints["bedrooms"],
        bathrooms=hints["bathrooms"],
        beds=hints["beds"],
        link=card.link,
        link_context=(card.link_context if card.link_context != "none" else "property")
        if card.link
        else "none",
        latitude=card.latitude,
        longitude=card.longitude,
        review_count=card.review_count,
        sleeps=hints["sleeps"],
        place_types=card.place_types,
        class_label=card.class_label,
        priced_adults=card.priced_adults,
        provider_id=card.provider_id,
    )


def _distance_km(near: Tuple[float, float], latitude: float, longitude: float) -> float:
    """Straight-line (great-circle) kilometres, to 0.01 km."""
    return round(_raw_distance_km(near, latitude, longitude), 2)


def _raw_distance_km(near: Tuple[float, float], latitude: float, longitude: float) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (near[0], near[1], latitude, longitude))
    half = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(min(1.0, max(0.0, half))))


def _with_distance(offer: HotelOffer, near: Optional[Tuple[float, float]]) -> HotelOffer:
    if near is None or offer.latitude is None or offer.longitude is None:
        return offer
    return replace(offer, distance_km=_distance_km(near, offer.latitude, offer.longitude))


def validate_near(near: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    if near is None:
        return None
    lat, lng = near
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lng))
    if not (ok and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError("near must be a latitude (-90 to 90) and longitude (-180 to 180)")
    return (float(lat), float(lng))


def validate_max_distance(
    value: Optional[float], near: Optional[Tuple[float, float]]
) -> Optional[float]:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("max_distance_km must be a finite positive number")
    if near is None:
        raise ValueError("max_distance_km requires a named near point")
    return float(value)


def _within_radius(
    offer: HotelOffer, near: Optional[Tuple[float, float]], radius: Optional[float]
) -> bool:
    if radius is None:
        return True
    if near is None or offer.latitude is None or offer.longitude is None:
        return False
    return _raw_distance_km(near, offer.latitude, offer.longitude) <= radius


def _is_eligible(offer: HotelOffer, query: HotelQuery) -> bool:
    if offer.priced_adults is not None and offer.priced_adults != query.adults:
        return False
    if (
        (query.entire_home or offer.lodging_kind is LodgingKind.ENTIRE_HOME)
        and query.rooms == 1
        and offer.sleeps is not None
        and offer.sleeps < query.adults
    ):
        return False
    if query.min_rating is not None:
        if offer.rating_score is None or offer.rating_score < query.min_rating:
            return False
    if (
        query.free_cancellation
        and offer.cancellation_evidence is CancellationEvidence.NON_REFUNDABLE
    ):
        return False
    if query.entire_home and offer.property_type_evidence is PropertyTypeEvidence.NOT_ENTIRE_HOME:
        return False
    return True


def _normalized_text(value: Optional[str]) -> str:
    return " ".join((value or "").split()).casefold()


def _sorted_deduplicated_offers(
    offers: Sequence[HotelOffer],
) -> Tuple[HotelOffer, ...]:
    rows = sorted(
        offers,
        key=lambda offer: (
            offer.total_price,
            offer.rating_score is None,
            -(offer.rating_score or 0.0),
            _normalized_text(offer.title),
        ),
    )
    seen: set[tuple[str, str, float]] = set()
    deduplicated: list[HotelOffer] = []
    for offer in rows:
        identity = (
            _normalized_text(offer.title),
            _normalized_text(offer.address),
            offer.total_price,
        )
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(offer)
    return tuple(deduplicated)


def _rank_offers(
    offers: Sequence[HotelOffer],
    top: int,
) -> Tuple[HotelOffer, ...]:
    if top <= 0:
        raise ValueError("top must be positive")
    return _sorted_deduplicated_offers(offers)[:top]


def _classify_hotel_failure(exc: BaseException) -> SearchError:
    if isinstance(exc, SkiplaggedRateLimited):
        return SearchError(
            code=SearchErrorCode.BLOCKED,
            message=str(exc),
            rate_limited=True,
            retry_until=cooldown_until(str(exc), SKIPLAGGED_RATE_LIMIT_FILE),
        )
    if isinstance(exc, SkiplaggedNoHotels):
        return SearchError(code=SearchErrorCode.NO_RESULTS, message=str(exc))
    if isinstance(exc, SkiplaggedParseMiss):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message="Skiplagged hotel parse missed.",
        )
    if isinstance(exc, EmptyHotelResults):
        return SearchError(
            code=SearchErrorCode.NO_RESULTS,
            message="Google Hotels returned no stays for this search.",
        )
    if isinstance(exc, HotelsRejected):
        return SearchError(
            code=SearchErrorCode.REJECTED,
            message="Google Hotels rejected this search.",
        )
    if isinstance(exc, HotelsBlocked):
        return SearchError(
            code=SearchErrorCode.BLOCKED,
            message=str(exc) if exc.rate_limited else "Google Hotels blocked the sweep.",
            rate_limited=exc.rate_limited,
            retry_until=cooldown_until(str(exc)) if exc.rate_limited else None,
        )
    if isinstance(exc, HotelsParseMiss):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message="Google Hotels compact parse missed.",
        )
    return classify_failure(exc)


def _run_search(
    queries: Sequence[HotelQuery],
    *,
    top: int,
    source: _HotelSource,
    sleep: Callable[[float], None],
    random_gen: random.Random,
    now: Callable[[], datetime],
    html_lang: str = FETCH_LANGUAGE,
    currency: str,
    progress: Optional[Callable[[str], None]] = None,
    provider: HotelProvider = "booking.com",
    applied_filters: Optional[Callable[..., AppliedHotelFilters]] = None,
    delay_seconds: Optional[Callable[[random.Random], float]] = None,
    fetch_backend: Optional[Literal["booking", "google", "skiplagged"]] = None,
    fetch_ms: Optional[int] = None,
    near: Optional[Tuple[float, float]] = None,
    max_distance_km: Optional[float] = None,
) -> HotelSearchReport:
    near = validate_near(near)
    max_distance_km = validate_max_distance(max_distance_km, near)
    if not queries:
        raise ValueError("at least one query is required")
    if top <= 0:
        raise ValueError("top must be positive")

    build_filters = applied_filters or build_booking_filters
    delay = delay_seconds or inter_query_delay_seconds
    report_progress = progress or (lambda _: None)
    results: list[HotelQueryResult] = []
    fetch_limit = max(top * 3, 24)
    for index, query in enumerate(queries):
        report_progress(
            f"[{index + 1}/{len(queries)}] {query.location} "
            f"{query.check_in.isoformat()} -> {query.check_out.isoformat()}"
        )
        applied = build_filters(query, html_lang=html_lang, currency=currency)
        outcome: Optional[HotelQueryResult] = None
        failure: Optional[SearchError] = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                checkpoint()
                page = source.fetch(query, applied, fetch_limit)
                normalized = tuple(
                    _with_distance(offer, near)
                    for raw in page.cards
                    if (offer := _normalize_card(raw)) is not None
                )
                eligible = tuple(
                    offer
                    for offer in normalized
                    if _is_eligible(offer, query) and _within_radius(offer, near, max_distance_km)
                )
                rank_limit = max(top, len(eligible))
                ranked = _rank_offers(
                    eligible,
                    top=rank_limit,
                )
                outcome = HotelQuerySuccess(
                    query=query,
                    applied=(replace(applied, url=page.search_url) if page.search_url else applied),
                    raw_count=len(page.cards),
                    eligible_count=len(ranked),
                    offers=ranked[:top],
                    resolved_place=page.resolved_place,
                    place_bounds=page.place_bounds,
                )
                break
            except Exception as exc:
                failure = _classify_hotel_failure(exc)
                source.reset()
                if failure.code in NON_RETRIABLE_CODES or isinstance(
                    exc, (BookingResultsTimeout, SweepTransportError)
                ):
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    sleep(retry_backoff_seconds(attempt, random_gen))
        if outcome is None:
            default_message = {
                "google-hotels": "Google Hotels search failed.",
                "skiplagged": "Skiplagged hotel search failed.",
            }.get(provider, "Booking.com hotel search failed.")
            outcome = HotelQueryFailure(
                query=query,
                applied=applied,
                error=failure
                or SearchError(
                    code=SearchErrorCode.FETCH_FAILED,
                    message=default_message,
                ),
            )
            report_progress(f"  {outcome.error.code.value}: {outcome.error.message}")
        results.append(outcome)
        if index + 1 < len(queries):
            sleep(delay(random_gen))
    return HotelSearchReport(
        searched_at=now(),
        queries=tuple(results),
        locale=html_lang,
        currency=currency,
        provider=provider,
        fetch_backend=fetch_backend,
        fetch_ms=fetch_ms,
        near=near,
        max_distance_km=max_distance_km,
    )


HotelSourceName = Literal["booking", "google", "skiplagged"]


def resolve_hotel_currency(source: str, currency: Optional[str]) -> str:
    """Named ISO 4217, except Skiplagged where an unnamed currency is its USD."""
    if source == "skiplagged" and (currency is None or not str(currency).strip()):
        return SKIPLAGGED_HOTEL_CURRENCY
    return resolve_quote_currency(currency, None, missing=HOTEL_CURRENCY_REQUIRED)


def _skiplagged_currency_mismatch(
    queries: Sequence[HotelQuery], currency: str
) -> HotelSearchReport:
    error = SearchError(
        code=SearchErrorCode.CURRENCY_MISMATCH,
        message=(
            f"Skiplagged hotel quotes are {SKIPLAGGED_HOTEL_CURRENCY}; {currency} was named. "
            "Viajante does not convert. Omit currency or pass USD."
        ),
    )
    return HotelSearchReport(
        searched_at=datetime.now(timezone.utc),
        queries=tuple(
            HotelQueryFailure(
                query=query,
                applied=build_skiplagged_filters(
                    query, html_lang=FETCH_LANGUAGE, currency=SKIPLAGGED_HOTEL_CURRENCY
                ),
                error=error,
            )
            for query in queries
        ),
        locale=FETCH_LANGUAGE,
        currency=SKIPLAGGED_HOTEL_CURRENCY,
        provider="skiplagged",
        fetch_backend="skiplagged",
        fetch_ms=0,
    )


def validate_hotel_search_args(queries: Sequence[HotelQuery], *, top: int, source: str) -> None:
    """Every check on search arguments alone, before any lock, fetch or state write.

    Shared by `search_hotels`, the MCP tool and saved watches so they reject the same input.
    """
    if not queries:
        raise ValueError("at least one query is required")
    if top <= 0:
        raise ValueError("top must be positive")
    if source not in ("booking", "google", "skiplagged"):
        raise ValueError("source must be booking, google, or skiplagged")
    if source == "google" and any(
        query.min_rating is not None and query.min_rating > 5 for query in queries
    ):
        raise ValueError("min_rating must be at most 5 with source google")
    if source == "skiplagged":
        for query in queries:
            validate_search_party(query.adults, query.rooms)
            if query.entire_home:
                raise ValueError("entire_home is not supported with source skiplagged")


@controlled
@recorded_hotels
def search_hotels(
    queries: Sequence[HotelQuery],
    *,
    top: int = DEFAULT_TOP,
    progress: Optional[Callable[[str], None]] = None,
    source: HotelSourceName = "booking",
    currency: Optional[str] = None,
    near: Optional[Tuple[float, float]] = None,
    max_distance_km: Optional[float] = None,
    cancel: Optional[threading.Event] = None,
    deadline_seconds: Optional[float] = None,
) -> HotelSearchReport:
    near = validate_near(near)
    max_distance_km = validate_max_distance(max_distance_km, near)
    validate_hotel_search_args(queries, top=top, source=source)
    currency = resolve_hotel_currency(source, currency)
    if source == "skiplagged" and currency != SKIPLAGGED_HOTEL_CURRENCY:
        return replace(
            _skiplagged_currency_mismatch(queries, currency),
            near=near,
            max_distance_km=max_distance_km,
        )
    if source == "google":
        hotel_source: _HotelSource = GoogleHotelsSource(currency=currency)
        provider: HotelProvider = "google-hotels"
        applied_filters = build_google_filters
        delay_seconds = sweep_inter_query_delay_seconds
        fetch_backend: Literal["booking", "google", "skiplagged"] = "google"
    elif source == "skiplagged":
        hotel_source = SkiplaggedHotelsSource()
        provider = "skiplagged"
        applied_filters = build_skiplagged_filters
        delay_seconds = sweep_inter_query_delay_seconds
        fetch_backend = "skiplagged"
    else:
        if not playwright_available():
            failure = classify_failure(ModuleNotFoundError("No module named 'playwright'"))
            now = datetime.now(timezone.utc)
            return HotelSearchReport(
                searched_at=now,
                queries=tuple(
                    HotelQueryFailure(
                        query=query,
                        applied=build_booking_filters(
                            query, html_lang=FETCH_LANGUAGE, currency=currency
                        ),
                        error=failure,
                    )
                    for query in queries
                ),
                locale=FETCH_LANGUAGE,
                currency=currency,
                provider="booking.com",
                fetch_backend="booking",
                fetch_ms=0,
                near=near,
                max_distance_km=max_distance_km,
            )
        hotel_source = BookingHotelsSource(default_state_dir(), currency=currency)
        provider = "booking.com"
        applied_filters = build_booking_filters
        delay_seconds = inter_query_delay_seconds
        fetch_backend = "booking"
    started = time.perf_counter()
    try:
        report = _run_search(
            queries,
            top=top,
            source=hotel_source,
            sleep=interruptible_sleep,
            random_gen=random.Random(),
            now=lambda: datetime.now(timezone.utc),
            html_lang=hotel_source.config.html_lang,  # type: ignore[attr-defined]
            currency=hotel_source.config.currency,  # type: ignore[attr-defined]
            progress=progress,
            provider=provider,
            applied_filters=applied_filters,
            delay_seconds=delay_seconds,
            fetch_backend=fetch_backend,
            near=near,
            max_distance_km=max_distance_km,
        )
    finally:
        hotel_source.close()
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return replace(report, fetch_ms=fetch_ms)
