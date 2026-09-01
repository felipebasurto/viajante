"""Hotel eligibility, ranking, and the Booking.com search loop."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Protocol, Sequence, Tuple

from viajante.booking import (
    BookingHotelsSource,
    BookingResultsTimeout,
)
from viajante.booking import (
    build_applied_filters as build_booking_filters,
)
from viajante.flights import DEFAULT_TOP
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
    parse_cancellation_evidence,
    parse_lodging_kind,
    parse_price_eur,
    parse_property_type_evidence,
    parse_rating,
    parse_unit_hints,
)
from viajante.storage import default_state_dir, write_json_atomic


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
    total_price_eur = parse_price_eur(card.total_price)
    if total_price_eur is None or total_price_eur <= 0:
        return None
    hints = parse_unit_hints(card.details)
    return HotelOffer(
        title=card.title,
        address=card.address,
        total_price=card.total_price,
        total_price_eur=total_price_eur,
        rating=card.rating,
        rating_score=parse_rating(card.rating),
        details=card.details,
        cancellation_evidence=parse_cancellation_evidence(card.details),
        property_type_evidence=parse_property_type_evidence(card.details),
        lodging_kind=parse_lodging_kind(card.details, title=card.title),
        bedrooms=hints["bedrooms"],
        bathrooms=hints["bathrooms"],
        beds=hints["beds"],
        link=card.link,
    )


def _is_eligible(offer: HotelOffer, query: HotelQuery) -> bool:
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
            offer.total_price_eur,
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
            offer.total_price_eur,
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


def _classify_hotel_failure(exc: BaseException, *, provider: HotelProvider) -> SearchError:
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
            message="Google Hotels blocked the sweep.",
        )
    if isinstance(exc, HotelsParseMiss):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message="Google Hotels compact parse missed.",
        )
    label = "Google Hotels" if provider == "google-hotels" else "Booking.com"
    return classify_failure(exc, provider=label)


def _run_search(
    queries: Sequence[HotelQuery],
    *,
    top: int,
    source: _HotelSource,
    sleep: Callable[[float], None],
    random_gen: random.Random,
    now: Callable[[], datetime],
    html_lang: str = FETCH_LANGUAGE,
    currency: str = "EUR",
    progress: Optional[Callable[[str], None]] = None,
    provider: HotelProvider = "booking.com",
    applied_filters: Optional[Callable[..., AppliedHotelFilters]] = None,
    delay_seconds: Optional[Callable[[random.Random], float]] = None,
    fetch_backend: Optional[Literal["booking", "google"]] = None,
    fetch_ms: Optional[int] = None,
) -> HotelSearchReport:
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
                page = source.fetch(query, applied, fetch_limit)
                normalized = tuple(
                    offer for raw in page.cards if (offer := _normalize_card(raw)) is not None
                )
                eligible = tuple(offer for offer in normalized if _is_eligible(offer, query))
                rank_limit = max(top, len(eligible))
                ranked = _rank_offers(
                    eligible,
                    top=rank_limit,
                )
                outcome = HotelQuerySuccess(
                    query=query,
                    applied=applied,
                    raw_count=len(page.cards),
                    eligible_count=len(ranked),
                    offers=ranked[:top],
                )
                break
            except Exception as exc:
                failure = _classify_hotel_failure(exc, provider=provider)
                source.reset()
                if failure.code in NON_RETRIABLE_CODES or isinstance(exc, BookingResultsTimeout):
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    sleep(retry_backoff_seconds(attempt, random_gen))
        if outcome is None:
            default_message = (
                "Google Hotels search failed."
                if provider == "google-hotels"
                else "Booking.com hotel search failed."
            )
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
    )


HotelSourceName = Literal["booking", "google"]


def search_hotels(
    queries: Sequence[HotelQuery],
    *,
    top: int = DEFAULT_TOP,
    progress: Optional[Callable[[str], None]] = None,
    source: HotelSourceName = "booking",
) -> HotelSearchReport:
    if not queries:
        raise ValueError("at least one query is required")
    if top <= 0:
        raise ValueError("top must be positive")
    if source == "google":
        hotel_source: _HotelSource = GoogleHotelsSource()
        provider: HotelProvider = "google-hotels"
        applied_filters = build_google_filters
        delay_seconds = sweep_inter_query_delay_seconds
        fetch_backend: Literal["booking", "google"] = "google"
    elif source == "booking":
        hotel_source = BookingHotelsSource(default_state_dir())
        provider = "booking.com"
        applied_filters = build_booking_filters
        delay_seconds = inter_query_delay_seconds
        fetch_backend = "booking"
    else:
        raise ValueError("source must be booking or google")
    started = time.perf_counter()
    try:
        report = _run_search(
            queries,
            top=top,
            source=hotel_source,
            sleep=time.sleep,
            random_gen=random.Random(),
            now=lambda: datetime.now(timezone.utc),
            html_lang=hotel_source.config.html_lang,  # type: ignore[attr-defined]
            currency=hotel_source.config.currency,  # type: ignore[attr-defined]
            progress=progress,
            provider=provider,
            applied_filters=applied_filters,
            delay_seconds=delay_seconds,
            fetch_backend=fetch_backend,
        )
    finally:
        hotel_source.close()
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return HotelSearchReport(
        searched_at=report.searched_at,
        queries=report.queries,
        locale=report.locale,
        currency=report.currency,
        provider=report.provider,
        fetch_backend=fetch_backend,
        fetch_ms=fetch_ms,
    )


def write_hotel_report_atomic(
    report: HotelSearchReport,
    destination: Path,
) -> None:
    write_json_atomic(report.to_dict(), destination)
