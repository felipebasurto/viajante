"""Cheap destinations from an origin.

Catalog RPC, then price the first --top dests on the start date.
"""

from __future__ import annotations

import calendar
import threading
import time
from datetime import date, datetime, timezone
from typing import Callable, Optional, Protocol, Sequence, Tuple

from viajante.airports import dest_blocked_by_exclude_regions, is_known_iata, parse_exclude_regions
from viajante.control import checkpoint, controlled
from viajante.dates import _fetch_or_exception, calendar_trip, one_or_many
from viajante.flight_filters import OfferFilters, owned_clock, parse_code_list, parse_offer_filters
from viajante.flight_offers import (
    FlightSort,
    _cheapest_by_fare,
    _cheapest_by_ranked,
    compare_nonstop_vs_one_stop,
    offers_from_cards,
    validate_sort,
)
from viajante.flight_routes import expand_nearby_origins
from viajante.flights import classify_failure
from viajante.google_flights import RawFlightCard, google_flights_url
from viajante.google_flights_public import PublicGoogleFlightsHttpSource as GoogleFlightsHttpSource
from viajante.google_flights_rpc import CompactExplorePlace
from viajante.models import (
    ExploreDestination,
    ExploreReport,
    FlightCabin,
    FlightOffer,
    FlightQuery,
    QueryFailure,
    SearchCoverage,
    SearchError,
    StopsCompare,
    Trip,
    explore_stop,
    normalize_country,
)
from viajante.parsers import clock_minutes as _clock_minutes
from viajante.quote import resolve_baggage_buffer, resolve_quote_currency

DEFAULT_EXPLORE_TOP = 12
MAX_EXPLORE_TOP = 30


class ExploreSource(Protocol):
    def fetch_explore(
        self,
        origin: str,
        departure_date: date,
        *,
        adults: int = 1,
        cabin: FlightCabin = "economy",
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
    ) -> Sequence[CompactExplorePlace]: ...

    def fetch(self, query: FlightQuery) -> Sequence[RawFlightCard]: ...

    def close(self) -> None: ...


def validate_explore_window(start: date, days: int, *, today: Optional[date] = None) -> None:
    if days < 1:
        raise ValueError("--days must be at least 1")
    check = today or date.today()
    if start < check:
        raise ValueError(f"start date is in the past: {start.isoformat()}")


def month_window(
    value: str, *, flag: str = "month", today: Optional[date] = None
) -> tuple[date, int]:
    """First day of YYYY-MM and that month's length in days.

    The current month starts today: its earlier days are in the past and cannot be searched.
    """
    try:
        year_text, month_text = value.split("-", 1)
        start = date(int(year_text), int(month_text), 1)
    except ValueError as exc:
        raise ValueError(f"{flag} must look like YYYY-MM") from exc
    length = calendar.monthrange(start.year, start.month)[1]
    check = today or date.today()
    if (start.year, start.month) == (check.year, check.month):
        return check, length - check.day + 1
    return start, length


def _rank_explore_destinations(
    dests: Sequence[ExploreDestination],
    sort: FlightSort,
) -> tuple[ExploreDestination, ...]:
    """Re-rank shopped dests by an owned field. Missing key sorts last; never invent."""

    def sort_key(row: ExploreDestination) -> tuple:
        if sort == "duration":
            missing = row.duration_hours is None
            hours = row.duration_hours if row.duration_hours is not None else 0.0
            return (missing, hours, row.price is None, row.price or 0.0, row.iata)
        if sort == "departure":
            minutes = _clock_minutes(row.departure)
            missing = minutes is None
            return (missing, minutes or 0, row.price is None, row.price or 0.0, row.iata)
        if sort == "arrival":
            minutes = _clock_minutes(row.arrival)
            missing = minutes is None
            return (missing, minutes or 0, row.price is None, row.price or 0.0, row.iata)
        fare = row.price if row.price is not None else 0.0
        if sort == "ranked":
            buffer = row.baggage_buffer or 0
            return (row.price is None, fare + buffer, row.iata)
        return (row.price is None, fare, row.iata)

    return tuple(sorted(dests, key=sort_key))


@controlled
def search_explore(
    origin: str,
    start: date,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    exclude_airports: Optional[Sequence[str]] = None,
    include_airports: Optional[Sequence[str]] = None,
    exclude_regions: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    nearby: bool = False,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    sort: FlightSort = "price",
    baggage_buffer: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[ExploreSource] = None,
    cancel: Optional[threading.Event] = None,
    deadline_seconds: Optional[float] = None,
) -> ExploreReport | tuple[ExploreReport, ...]:
    country = normalize_country(country)
    validate_explore_window(start, days)
    if top <= 0:
        raise ValueError("top must be positive")
    if top > MAX_EXPLORE_TOP:
        raise ValueError(f"top is at most {MAX_EXPLORE_TOP}")
    validate_sort(sort)
    if price_cap is not None and price_cap <= 0:
        raise ValueError("price_cap must be positive")
    filters = parse_offer_filters(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        via=via,
        exclude_via=exclude_via,
        no_overnight=no_overnight,
        require_overnight=require_overnight,
    )
    parsed_exclude_airports = parse_code_list(exclude_airports, role="exclude-airports")
    parsed_include_airports = parse_code_list(include_airports, role="include-airports")
    parsed_exclude_regions = (
        parse_exclude_regions(",".join(exclude_regions)) if exclude_regions else None
    )
    origin = origin.strip().upper()
    if not is_known_iata(origin):
        raise ValueError(f"unknown origin IATA code: {origin!r}")
    currency = resolve_quote_currency(currency, origin)
    baggage_buffer = resolve_baggage_buffer(baggage_buffer, currency)
    report_progress = progress or (lambda _: None)
    drop_unpriced = (
        bags is not None
        or carry_on is not None
        or price_cap is not None
        or bool(airlines or exclude_airlines or alliances or exclude_alliances)
        or filters.named
    )
    allowed = frozenset(parsed_include_airports or ())
    blocked = frozenset(parsed_exclude_airports or ())
    origins = expand_nearby_origins(origin, nearby=nearby, exclude_airports=parsed_exclude_airports)
    if not origins:
        return ExploreReport(
            searched_at=datetime.now(timezone.utc),
            origin=origin,
            start_date=start,
            days=days,
            destinations=(),
            fetch_backend="explore",
            fetch_ms=0,
            currency=currency,
            empty_reason="filtered_out",
        )
    client = source or GoogleFlightsHttpSource(currency=currency, country=country, proxy=proxy)

    def explore_origin(code: str, nearby_label: Optional[str]) -> ExploreReport:
        suffix = f" ({nearby_label})" if nearby_label else ""
        report_progress(
            f"explore: from {code} on {start.isoformat()} "
            f"(top {top} catalog dests that day){suffix}"
        )
        started = time.perf_counter()
        error: Optional[SearchError] = None
        try:
            checkpoint()
            places = tuple(
                client.fetch_explore(
                    code,
                    start,
                    adults=adults,
                    cabin=cabin,
                    children=children,
                    infants_in_seat=infants_in_seat,
                    infants_on_lap=infants_on_lap,
                )
            )
        except Exception as exc:
            error = classify_failure(exc)
            places = ()
        catalog_count = len(places)
        places = tuple(
            place
            for place in places
            if (not allowed or place.iata in allowed)
            and place.iata not in blocked
            and not (
                parsed_exclude_regions
                and dest_blocked_by_exclude_regions(place.iata, parsed_exclude_regions)
            )
        )
        chosen = places[:top]
        shops = [
            calendar_trip(
                code,
                place.iata,
                start,
                max_stops=max_stops,
                adults=adults,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                cabin=cabin,
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliances=alliances,
                exclude_alliances=exclude_alliances,
            )
            for place in chosen
        ]
        batch = None
        fetch_batch = getattr(client, "fetch_many", None)
        if callable(fetch_batch) and len(shops) > 1:
            report_progress(f"pricing {len(shops)} dests on one multiplexed round-trip")
            try:
                checkpoint()
                batch = fetch_batch(shops)
            except Exception as exc:
                # Every destination carries the batch failure; a serial resend would send each
                # request a second time with no record of the first.
                batch = [exc] * len(shops)
        priced: list[ExploreDestination] = []
        pricing_errors: list[QueryFailure] = []
        succeeded = empty = 0
        shop_cards = 0
        for index, (place, shop) in enumerate(zip(chosen, shops, strict=True)):
            if batch is None:
                report_progress(f"[{index + 1}/{len(chosen)}] pricing {place.iata}")
                cards = _fetch_or_exception(client, shop)
            else:
                cards = batch[index]
            cheapest, compare = _cheapest_shop(
                cards, shop, filters, baggage_buffer=baggage_buffer, sort=sort
            )
            if not isinstance(cards, BaseException):
                shop_cards += len(cards)
            if isinstance(cards, BaseException):
                pricing_errors.append(QueryFailure(query=shop, error=classify_failure(cards)))
            elif cheapest is None:
                empty += 1
            else:
                succeeded += 1
            if drop_unpriced and cheapest is None:
                continue
            dest = ExploreDestination(
                iata=place.iata,
                city=place.city,
                country=place.country,
                price=cheapest.price if cheapest is not None else None,
                duration_hours=cheapest.duration_hours if cheapest is not None else None,
                departure=owned_clock(cheapest.departure) if cheapest is not None else None,
                arrival=owned_clock(cheapest.arrival) if cheapest is not None else None,
                stops_compare=compare,
                google_flights_url=google_flights_url(shop, currency=currency, country=country),
                baggage_buffer=cheapest.baggage_buffer if cheapest is not None else None,
            )
            priced.append(dest)
        stop_reason, stop_note = explore_stop(error, pricing_errors)
        empty_reason = None
        if not priced and error is None and not pricing_errors:
            if catalog_count == 0 or (places and not shop_cards):
                empty_reason = "provider_empty"
            else:
                empty_reason = "filtered_out"
        return ExploreReport(
            searched_at=datetime.now(timezone.utc),
            origin=code,
            start_date=start,
            days=days,
            destinations=_rank_explore_destinations(priced, sort),
            fetch_backend="explore",
            fetch_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error=error,
            pricing_errors=tuple(pricing_errors),
            coverage=SearchCoverage(
                scope={
                    "kind": "explore_shortlist",
                    "origin": code,
                    "from": start.isoformat(),
                    "days": days,
                },
                attempted=len(chosen) + int(error is not None),
                succeeded=succeeded,
                empty=empty,
                failed=len(pricing_errors) + int(error is not None),
                complete=False,
                strategy="heuristic",
                stopping_reason=stop_reason,
                unsearched=stop_note,
            ),
            nearby_label=nearby_label,
            currency=currency,
            empty_reason=empty_reason,
        )

    try:
        return one_or_many([explore_origin(code, label) for code, label in origins])
    finally:
        client.close()


def _cheapest_shop(
    cards: Sequence[RawFlightCard] | BaseException,
    query: Trip,
    filters: OfferFilters,
    *,
    baggage_buffer: int,
    sort: FlightSort,
) -> tuple[Optional[FlightOffer], Optional[StopsCompare]]:
    if isinstance(cards, BaseException):
        return None, None
    eligible = offers_from_cards(cards, query, filters, baggage_buffer=baggage_buffer)
    cheapest = _cheapest_by_ranked(eligible) if sort == "ranked" else _cheapest_by_fare(eligible)
    return cheapest, compare_nonstop_vs_one_stop(eligible)
