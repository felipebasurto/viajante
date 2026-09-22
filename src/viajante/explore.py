"""Cheap destinations from an origin.

Catalog RPC, then price the first --top dests on the start date.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, datetime, timezone
from typing import Callable, Optional, Protocol, Sequence, Tuple

from viajante.airports import dest_blocked_by_exclude_regions, is_known_iata, parse_exclude_regions
from viajante.dates import calendar_trip, one_or_many
from viajante.flights import (
    FlightSort,
    OfferFilters,
    _calendar_summary_from_source,
    _cheapest_by_fare,
    _cheapest_by_ranked,
    _clock_minutes,
    classify_failure,
    compare_nonstop_vs_one_stop,
    expand_nearby_origins,
    offers_from_cards,
    owned_clock,
    parse_code_list,
    parse_offer_filters,
    validate_sort,
)
from viajante.google_flights import GoogleFlightsHttpSource, RawFlightCard, google_flights_url
from viajante.google_flights_rpc import CompactExplorePlace
from viajante.models import (
    ExploreDestination,
    ExploreReport,
    FlightCabin,
    FlightOffer,
    FlightQuery,
    SearchError,
    StopsCompare,
    Trip,
    normalize_country,
)
from viajante.quote import resolve_baggage_buffer, resolve_quote_currency
from viajante.typical import with_typical_dest

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


def month_window(value: str, *, flag: str = "month") -> tuple[date, int]:
    """First day of YYYY-MM and that month's length in days."""
    try:
        year_text, month_text = value.split("-", 1)
        start = date(int(year_text), int(month_text), 1)
    except ValueError as exc:
        raise ValueError(f"{flag} must look like YYYY-MM") from exc
    return start, calendar.monthrange(start.year, start.month)[1]


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
        priced: list[ExploreDestination] = []
        typical_cache: dict = {}
        for index, place in enumerate(places[:top]):
            report_progress(f"[{index + 1}/{min(top, len(places))}] pricing {place.iata}")
            shop = calendar_trip(
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
            cheapest, compare = _cheapest_shop(
                client, shop, filters, baggage_buffer=baggage_buffer, sort=sort
            )
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
            if cheapest is not None:
                summary = _calendar_summary_from_source(client, shop, typical_cache)
                if summary is not None:
                    dest = with_typical_dest(dest, summary.median_price)
            priced.append(dest)
        return ExploreReport(
            searched_at=datetime.now(timezone.utc),
            origin=code,
            start_date=start,
            days=days,
            destinations=_rank_explore_destinations(priced, sort),
            fetch_backend="explore",
            fetch_ms=max(0, int((time.perf_counter() - started) * 1000)),
            error=error,
            nearby_label=nearby_label,
            currency=currency,
        )

    try:
        return one_or_many([explore_origin(code, label) for code, label in origins])
    finally:
        client.close()


def _cheapest_shop(
    source: ExploreSource,
    query: Trip,
    filters: OfferFilters,
    *,
    baggage_buffer: int,
    sort: FlightSort,
) -> tuple[Optional[FlightOffer], Optional[StopsCompare]]:
    try:
        cards = source.fetch(query)
    except Exception:
        return None, None
    eligible = offers_from_cards(cards, query, filters, baggage_buffer=baggage_buffer)
    cheapest = _cheapest_by_ranked(eligible) if sort == "ranked" else _cheapest_by_fare(eligible)
    return cheapest, compare_nonstop_vs_one_stop(eligible)
