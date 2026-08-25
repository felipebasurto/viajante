"""Cheapest-per-day calendar via the owned Google Flights date-grid RPC."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Tuple, TypeVar

from viajante.flights import (
    DEFAULT_BAGGAGE_BUFFER_EUR,
    DEFAULT_TOP,
    FlightSort,
    _normalize_offer,
    _rank_offers,
    classify_failure,
    compare_nonstop_vs_one_stop,
    drop_excluded_airport_trips,
    expand_nearby_trips,
    keep_included_dest_trips,
    normalize_trip_kind,
    parse_overnight_lists,
    parse_via_airports,
    validate_layover_hours,
)
from viajante.google_flights import GoogleFlightsHttpSource, RawFlightCard, google_flights_url
from viajante.google_flights_rpc import CompactCalendarDay, CompactParseMiss
from viajante.models import (
    DateCalendarReport,
    DateCalendarSummary,
    DatePriceRow,
    DateTripKind,
    FlexFetchBackend,
    FlexSearchReport,
    FlightCabin,
    FlightOffer,
    FlightQuery,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    StopsCompare,
    Trip,
    normalize_country,
    normalize_currency,
)
from viajante.storage import write_json_atomic
from viajante.typical import typical_eur_from_daily_prices, vs_typical, with_typical

MAX_DATE_WINDOW_DAYS = 31
MAX_FLEX_DAYS = 15

_T = TypeVar("_T")

EMPTY_DAY_MARK = "·"
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_SPARK_BLOCKS = "▁▂▃▄▅▆▇█"
_CELL_WIDTH = 5


def _english_day_label(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def _week_span_label(start: date, end: date) -> str:
    if start == end:
        return _english_day_label(start)
    if start.month == end.month and start.year == end.year:
        return f"{start.day}-{end.day} {_MONTHS[start.month - 1]}"
    return f"{_english_day_label(start)}-{_english_day_label(end)}"


def format_week_calendar(days: Sequence[DatePriceRow]) -> tuple[str, ...]:
    """English week lines of owned daily prices. Empty or missing days are ·."""
    if not days:
        return ()
    by_day = {row.departure_date: row for row in days}
    start = min(by_day)
    end = max(by_day)
    first_monday = date.fromordinal(start.toordinal() - start.weekday())
    header = "  " + " ".join(f"{name:>{_CELL_WIDTH}}" for name in _WEEKDAYS)
    lines = [header]
    cursor = first_monday
    while cursor <= end:
        cells: list[str] = []
        in_window = False
        for offset in range(7):
            day = date.fromordinal(cursor.toordinal() + offset)
            if day < start or day > end:
                cells.append(" " * _CELL_WIDTH)
                continue
            in_window = True
            row = by_day.get(day)
            price = None if row is None else row.price_eur
            if price is None or price <= 0:
                cells.append(f"{EMPTY_DAY_MARK:>{_CELL_WIDTH}}")
            else:
                cells.append(f"{price:>{_CELL_WIDTH}.0f}")
        if in_window:
            week_end = date.fromordinal(cursor.toordinal() + 6)
            label = _week_span_label(max(cursor, start), min(week_end, end))
            lines.append("  " + " ".join(cells) + f"  {label}")
        cursor = date.fromordinal(cursor.toordinal() + 7)
    return tuple(lines)


def format_sparkline(days: Sequence[DatePriceRow]) -> str:
    """One glyph per owned row. Empty days are ·; never a guessed height."""
    owned = [row.price_eur for row in days if row.price_eur is not None and row.price_eur > 0]
    if not owned:
        return ""
    lo = min(owned)
    hi = max(owned)
    span = hi - lo
    n_levels = len(_SPARK_BLOCKS)
    chars: list[str] = []
    for row in days:
        price = row.price_eur
        if price is None or price <= 0:
            chars.append(EMPTY_DAY_MARK)
            continue
        if span == 0:
            chars.append(_SPARK_BLOCKS[0])
            continue
        index = int(round((price - lo) / span * (n_levels - 1)))
        chars.append(_SPARK_BLOCKS[max(0, min(n_levels - 1, index))])
    return "".join(chars)


def format_summary_line(summary: DateCalendarSummary) -> str:
    return (
        f"  min {summary.min_eur:.0f} €  "
        f"median {summary.median_eur:.0f} €  "
        f"max {summary.max_eur:.0f} €  "
        f"cheapest {summary.cheapest_date.isoformat()}  "
        f"({summary.n_priced} priced)"
    )


class CalendarSource(Protocol):
    def fetch_calendar(
        self, trip: Trip, start: date, end: date
    ) -> Sequence[CompactCalendarDay]: ...

    def fetch(self, trip: Trip) -> Sequence[RawFlightCard]: ...

    def close(self) -> None: ...


def parse_route_pair(spec: str) -> tuple[str, str]:
    try:
        origin, destination = spec.split("-", 1)
    except ValueError as exc:
        raise ValueError(f"invalid route: {spec!r}. Expected ORIGIN-DEST") from exc
    origin, destination = origin.strip(), destination.strip()
    if not origin or not destination or "-" in destination:
        raise ValueError(f"invalid route: {spec!r}. Expected ORIGIN-DEST")
    return origin, destination


def date_window_days(start: date, end: date) -> int:
    return (end - start).days + 1


def shift_day(day: date, nights: int) -> date:
    return date.fromordinal(day.toordinal() + nights)


def validate_date_window(start: date, end: date, *, today: Optional[date] = None) -> None:
    if end < start:
        raise ValueError("end date must be on or after the start date")
    span = date_window_days(start, end)
    if span > MAX_DATE_WINDOW_DAYS:
        raise ValueError(f"date window is at most {MAX_DATE_WINDOW_DAYS} days (got {span})")
    check = today or date.today()
    if start < check:
        raise ValueError(f"start date is in the past: {start.isoformat()}")


def resolve_date_trip(
    trip: str = "one-way",
    nights: Optional[int] = None,
) -> tuple[DateTripKind, Optional[int]]:
    """Normalize dates trip kind. --nights implies rt; rt without nights is invalid."""
    kind = normalize_trip_kind(trip)
    if kind == "multi":
        raise ValueError("date search does not support multi-city")
    if nights is not None:
        if nights < 1:
            raise ValueError("nights must be at least 1")
        if kind == "one-way":
            kind = "rt"
    if kind == "rt" and nights is None:
        raise ValueError("round-trip date search needs nights")
    return kind, nights


def _occupancy_from_trip(trip: FlightQuery | RoundTrip) -> dict[str, int]:
    return {
        "adults": trip.adults,
        "children": trip.children,
        "infants_in_seat": trip.infants_in_seat,
        "infants_on_lap": trip.infants_on_lap,
    }


def _day_trip(
    seed: FlightQuery | RoundTrip,
    day: date,
    nights: Optional[int],
) -> FlightQuery | RoundTrip:
    return calendar_trip(
        seed.origin,
        seed.destination,
        day,
        max_stops=seed.legs[0].max_stops,
        cabin=seed.cabin,
        nights=nights,
        bags=seed.bags,
        carry_on=seed.carry_on,
        price_cap_eur=seed.price_cap_eur,
        airlines=seed.airlines,
        exclude_airlines=seed.exclude_airlines,
        alliances=seed.alliances,
        exclude_alliances=seed.exclude_alliances,
        **_occupancy_from_trip(seed),
    )


def _stamp_trip_google_flights_url(
    trip: Trip,
    *,
    currency: str,
    country: Optional[str],
    booking_token: Optional[str] = None,
) -> Optional[str]:
    return google_flights_url(trip, currency=currency, country=country, booking_token=booking_token)


def _stamp_date_row_url(
    row: DatePriceRow,
    seed: FlightQuery | RoundTrip,
    nights: Optional[int],
    *,
    currency: str,
    country: Optional[str],
) -> DatePriceRow:
    url = _stamp_trip_google_flights_url(
        _day_trip(seed, row.departure_date, nights),
        currency=currency,
        country=country,
    )
    if not url:
        return row
    return replace(row, google_flights_url=url)


def _stamp_offer_urls(
    trip: Trip,
    offers: Tuple[FlightOffer, ...],
    *,
    currency: str,
    country: Optional[str],
) -> Tuple[FlightOffer, ...]:
    return tuple(
        replace(
            offer,
            google_flights_url=_stamp_trip_google_flights_url(
                trip,
                currency=currency,
                country=country,
                booking_token=offer.booking_token,
            ),
        )
        for offer in offers
    )


def calendar_trip(
    origin: str,
    destination: str,
    start: date,
    *,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    nights: Optional[int] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
) -> FlightQuery | RoundTrip:
    airline_codes = tuple(airlines) if airlines is not None else None
    exclude_codes = tuple(exclude_airlines) if exclude_airlines is not None else None
    alliance_names = tuple(alliances) if alliances is not None else None
    exclude_alliance_names = tuple(exclude_alliances) if exclude_alliances is not None else None
    occupancy = {
        "adults": adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
    }
    if nights is None:
        return FlightQuery(
            origin=origin,
            destination=destination,
            departure_date=start,
            max_stops=max_stops,
            cabin=cabin,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap_eur,
            airlines=airline_codes,
            exclude_airlines=exclude_codes,
            alliances=alliance_names,
            exclude_alliances=exclude_alliance_names,
            **occupancy,
        )
    return RoundTrip(
        origin=origin,
        destination=destination,
        departure_date=start,
        return_date=shift_day(start, nights),
        max_stops=max_stops,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airline_codes,
        exclude_airlines=exclude_codes,
        alliances=alliance_names,
        exclude_alliances=exclude_alliance_names,
        **occupancy,
    )


def _parse_via_pair(
    via: Optional[Sequence[str]],
    exclude_via: Optional[Sequence[str]],
) -> tuple[Optional[tuple[str, ...]], Optional[tuple[str, ...]]]:
    parsed_via = parse_via_airports(",".join(via), role="via") if via else None
    parsed_exclude = (
        parse_via_airports(",".join(exclude_via), role="exclude-via") if exclude_via else None
    )
    if parsed_via and parsed_exclude and set(parsed_via) & set(parsed_exclude):
        raise ValueError("via and exclude-via must not share a code")
    return parsed_via, parsed_exclude


def _parse_exclude_airports(
    exclude_airports: Optional[Sequence[str]],
) -> Optional[tuple[str, ...]]:
    return (
        parse_via_airports(",".join(exclude_airports), role="exclude-airports")
        if exclude_airports
        else None
    )


def _parse_include_airports(
    include_airports: Optional[Sequence[str]],
) -> Optional[tuple[str, ...]]:
    return (
        parse_via_airports(",".join(include_airports), role="include-airports")
        if include_airports
        else None
    )


def _empty_dates_report(
    origin: str,
    destination: str,
    start: date,
    end: date,
    *,
    kind: DateTripKind,
    stay: Optional[int],
    currency: str,
) -> DateCalendarReport:
    return DateCalendarReport(
        searched_at=datetime.now(timezone.utc),
        origin=origin,
        destination=destination,
        start_date=start,
        end_date=end,
        days=(),
        trip=kind,
        nights=stay,
        fetch_backend="calendar",
        fetch_ms=0,
        currency=currency,
    )


def _empty_flex_report(
    origin: str,
    destination: str,
    around: date,
    flex_days: int,
    start: date,
    end: date,
    *,
    kind: DateTripKind,
    stay: Optional[int],
    currency: str,
) -> FlexSearchReport:
    return FlexSearchReport(
        searched_at=datetime.now(timezone.utc),
        origin=origin,
        destination=destination,
        around=around,
        flex_days=flex_days,
        start_date=start,
        end_date=end,
        days=(),
        trip=kind,
        nights=stay,
        fetch_backend="calendar",
        fetch_ms=0,
        currency=currency,
    )


def _offers_from_cards(
    cards: Sequence[RawFlightCard],
    query: FlightQuery | RoundTrip,
    *,
    buffer_eur: int = 0,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> list[FlightOffer]:
    """Apply the same owned shop post-filters search_flights uses."""
    return [
        offer
        for raw in cards
        if (
            offer := _normalize_offer(
                raw,
                query.legs[0].max_stops,
                buffer_eur=buffer_eur,
                airlines=query.airlines,
                exclude_airlines=query.exclude_airlines,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_layover_hours=max_layover_hours,
                min_layover_hours=min_layover_hours,
                max_duration_hours=max_duration_hours,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                bags=query.bags,
                carry_on=query.carry_on,
                price_cap_eur=query.price_cap_eur,
            )
        )
        is not None
    ]


def _one_or_many(reports: list[_T]) -> _T | tuple[_T, ...]:
    if len(reports) == 1:
        return reports[0]
    return tuple(reports)


def _date_calendar_for_seed(
    client: CalendarSource,
    seed: FlightQuery | RoundTrip,
    start: date,
    end: date,
    *,
    kind: DateTripKind,
    stay: Optional[int],
    parsed_via: Optional[tuple[str, ...]],
    parsed_exclude_via: Optional[tuple[str, ...]],
    parsed_no_overnight: Optional[tuple[str, ...]],
    parsed_require_overnight: Optional[tuple[str, ...]],
    depart_window: Optional[Tuple[int, int]],
    arrive_before: Optional[int],
    depart_after: Optional[int],
    max_layover_hours: Optional[float],
    min_layover_hours: Optional[float],
    max_duration_hours: Optional[float],
    currency: str,
    country: Optional[str],
    report_progress: Callable[[str], None],
) -> DateCalendarReport:
    stay_label = ""
    if kind == "rt" and stay is not None:
        night_word = "night" if stay == 1 else "nights"
        stay_label = f", rt {stay} {night_word}"
    nearby = f" ({seed.nearby_label})" if seed.nearby_label else ""
    report_progress(
        f"dates: {seed.origin} -> {seed.destination} "
        f"{start.isoformat()} .. {end.isoformat()} "
        f"(max {MAX_DATE_WINDOW_DAYS} days{stay_label}){nearby}"
    )
    started = time.perf_counter()
    backend = "calendar"
    try:
        compact = client.fetch_calendar(seed, start, end)
        days = _rows_from_calendar(start, end, compact, nights=stay)
    except CompactParseMiss:
        report_progress("calendar miss; pricing each day with shopping sweep")
        days = _sweep_per_day(
            client,
            seed,
            start,
            end,
            stay,
            report_progress,
            via=parsed_via,
            exclude_via=parsed_exclude_via,
            no_overnight=parsed_no_overnight,
            require_overnight=parsed_require_overnight,
            depart_window=depart_window,
            arrive_before=arrive_before,
            depart_after=depart_after,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
        )
        backend = "sweep"
    except Exception as exc:
        error = classify_failure(exc)
        days = _error_rows(start, end, error, nights=stay)
    days = tuple(
        _stamp_date_row_url(row, seed, stay, currency=currency, country=country) for row in days
    )
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return DateCalendarReport(
        searched_at=datetime.now(timezone.utc),
        origin=seed.origin,
        destination=seed.destination,
        start_date=start,
        end_date=end,
        days=days,
        trip=kind,
        nights=stay,
        fetch_backend=backend,
        fetch_ms=fetch_ms,
        google_flights_url=_stamp_trip_google_flights_url(seed, currency=currency, country=country),
        nearby_label=seed.nearby_label,
        currency=currency,
    )


def search_dates(
    origin: str,
    destination: str,
    start: date,
    end: date,
    *,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    trip: str = "one-way",
    nights: Optional[int] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
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
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    nearby: bool = False,
    currency: str = "EUR",
    country: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[CalendarSource] = None,
) -> DateCalendarReport | tuple[DateCalendarReport, ...]:
    validate_date_window(start, end)
    currency = normalize_currency(currency)
    country = normalize_country(country)
    validate_layover_hours(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    kind, stay = resolve_date_trip(trip, nights)
    parsed_via, parsed_exclude_via = _parse_via_pair(via, exclude_via)
    parsed_no_overnight, parsed_require_overnight = parse_overnight_lists(
        no_overnight, require_overnight
    )
    parsed_exclude_airports = _parse_exclude_airports(exclude_airports)
    parsed_include_airports = _parse_include_airports(include_airports)
    seed = calendar_trip(
        origin,
        destination,
        start,
        max_stops=max_stops,
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        nights=stay,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
    )
    trips = expand_nearby_trips((seed,), nearby=nearby)
    trips = keep_included_dest_trips(trips, parsed_include_airports)
    trips = drop_excluded_airport_trips(trips, parsed_exclude_airports)
    if not trips:
        return _empty_dates_report(
            origin,
            destination,
            start,
            end,
            kind=kind,
            stay=stay,
            currency=currency,
        )
    report_progress = progress or (lambda _: None)
    client = source or GoogleFlightsHttpSource(currency=currency, country=country)
    reports: list[DateCalendarReport] = []
    try:
        for item in trips:
            if not isinstance(item, (FlightQuery, RoundTrip)):
                continue
            reports.append(
                _date_calendar_for_seed(
                    client,
                    item,
                    start,
                    end,
                    kind=kind,
                    stay=stay,
                    parsed_via=parsed_via,
                    parsed_exclude_via=parsed_exclude_via,
                    parsed_no_overnight=parsed_no_overnight,
                    parsed_require_overnight=parsed_require_overnight,
                    depart_window=depart_window,
                    arrive_before=arrive_before,
                    depart_after=depart_after,
                    max_layover_hours=max_layover_hours,
                    min_layover_hours=min_layover_hours,
                    max_duration_hours=max_duration_hours,
                    currency=currency,
                    country=country,
                    report_progress=report_progress,
                )
            )
    finally:
        client.close()
    return _one_or_many(reports)


def write_dates_report_atomic(report: DateCalendarReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


def write_dates_reports_atomic(reports: Sequence[DateCalendarReport], destination: Path) -> None:
    owned = tuple(reports)
    if len(owned) == 1:
        write_dates_report_atomic(owned[0], destination)
        return
    write_json_atomic({"queries": [row.to_dict() for row in owned]}, destination)


def flex_window(
    around: date,
    flex_days: int,
    *,
    today: Optional[date] = None,
) -> tuple[date, date]:
    """Inclusive [around-flex, around+flex], clamped to today. Cap is 31 days."""
    if flex_days < 1:
        raise ValueError("flex must be at least 1 day")
    if flex_days > MAX_FLEX_DAYS:
        raise ValueError(
            f"flex is at most {MAX_FLEX_DAYS} days (window cap {MAX_DATE_WINDOW_DAYS})"
        )
    check = today or date.today()
    if around < check:
        raise ValueError(f"around date is in the past: {around.isoformat()}")
    start = date.fromordinal(around.toordinal() - flex_days)
    end = date.fromordinal(around.toordinal() + flex_days)
    if start < check:
        start = check
    validate_date_window(start, end, today=check)
    return start, end


def cheapest_priced_day(
    rows: Sequence[DatePriceRow],
    around: date,
) -> Optional[DatePriceRow]:
    """Cheapest owned calendar day. Ties: closer to around, then earlier date."""
    priced = [
        row
        for row in rows
        if row.status == "ok" and row.price_eur is not None and row.price_eur > 0
    ]
    if not priced:
        return None

    def sort_key(row: DatePriceRow) -> tuple[float, int, date]:
        price = row.price_eur if row.price_eur is not None else 0.0
        delta = abs((row.departure_date - around).days)
        return (price, delta, row.departure_date)

    return min(priced, key=sort_key)


def _flex_report_for_seed(
    client: CalendarSource,
    seed: FlightQuery | RoundTrip,
    around: date,
    flex_days: int,
    start: date,
    end: date,
    *,
    kind: DateTripKind,
    stay: Optional[int],
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
    bags: Optional[int],
    carry_on: Optional[int],
    price_cap_eur: Optional[int],
    airlines: Optional[Sequence[str]],
    exclude_airlines: Optional[Sequence[str]],
    alliances: Optional[Sequence[str]],
    exclude_alliances: Optional[Sequence[str]],
    parsed_via: Optional[tuple[str, ...]],
    parsed_exclude_via: Optional[tuple[str, ...]],
    parsed_no_overnight: Optional[tuple[str, ...]],
    parsed_require_overnight: Optional[tuple[str, ...]],
    depart_window: Optional[Tuple[int, int]],
    arrive_before: Optional[int],
    depart_after: Optional[int],
    max_layover_hours: Optional[float],
    min_layover_hours: Optional[float],
    max_duration_hours: Optional[float],
    top: int,
    buffer_eur: int,
    sort: FlightSort,
    currency: str,
    country: Optional[str],
    report_progress: Callable[[str], None],
) -> FlexSearchReport:
    stay_label = ""
    if kind == "rt" and stay is not None:
        night_word = "night" if stay == 1 else "nights"
        stay_label = f", rt {stay} {night_word}"
    nearby = f" ({seed.nearby_label})" if seed.nearby_label else ""
    report_progress(
        f"flex: {seed.origin} -> {seed.destination} around {around.isoformat()} "
        f"±{flex_days} {start.isoformat()} .. {end.isoformat()}{stay_label}{nearby}"
    )
    started = time.perf_counter()
    backend: FlexFetchBackend = "calendar"
    days: tuple[DatePriceRow, ...] = ()
    offers: tuple[FlightOffer, ...] = ()
    compare: Optional[StopsCompare] = None
    chosen: Optional[date] = None
    returning: Optional[date] = None
    error: Optional[SearchError] = None
    typical: Optional[float] = None
    shop: FlightQuery | RoundTrip | None = None
    try:
        compact = client.fetch_calendar(seed, start, end)
        days = _rows_from_calendar(start, end, compact, nights=stay)
    except CompactParseMiss:
        report_progress("calendar miss; no fare")
        days = ()
    except Exception as exc:
        error = classify_failure(exc)
        days = _error_rows(start, end, error, nights=stay)
    else:
        typical = typical_eur_from_daily_prices([row.price_eur for row in days])
        winner = cheapest_priced_day(days, around)
        if winner is None:
            report_progress("no priced day in flex window; no fare")
        else:
            chosen = winner.departure_date
            returning = winner.return_date
            shop = _day_trip(seed, chosen, stay)
            report_progress(f"chosen {chosen.isoformat()}; pricing that day")
            backend = "calendar_then_sweep"
            try:
                cards = client.fetch(shop)
            except Exception as exc:
                error = classify_failure(exc)
                cards = ()
            eligible = _offers_from_cards(
                cards,
                shop,
                buffer_eur=buffer_eur,
                via=parsed_via,
                exclude_via=parsed_exclude_via,
                no_overnight=parsed_no_overnight,
                require_overnight=parsed_require_overnight,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_layover_hours=max_layover_hours,
                min_layover_hours=min_layover_hours,
                max_duration_hours=max_duration_hours,
            )
            ranked = _rank_offers(eligible, top=top, sort=sort)
            if typical is not None:
                offers = tuple(with_typical(offer, typical) for offer in ranked)
            else:
                offers = ranked
            compare = compare_nonstop_vs_one_stop(eligible)
    fare = min((offer.price_eur for offer in offers), default=None)
    label = vs_typical(fare, typical) if fare is not None else None
    url_trip = shop or _day_trip(seed, chosen or around, stay)
    if offers:
        offers = _stamp_offer_urls(url_trip, offers, currency=currency, country=country)
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return FlexSearchReport(
        searched_at=datetime.now(timezone.utc),
        origin=seed.origin,
        destination=seed.destination,
        around=around,
        flex_days=flex_days,
        start_date=start,
        end_date=end,
        days=days,
        chosen_date=chosen,
        return_date=returning,
        offers=offers,
        stops_compare=compare,
        typical_eur=typical,
        vs_typical=label,
        trip=kind,
        nights=stay,
        fetch_backend=backend,
        fetch_ms=fetch_ms,
        google_flights_url=_stamp_trip_google_flights_url(
            url_trip, currency=currency, country=country
        ),
        error=error,
        nearby_label=seed.nearby_label,
        currency=currency,
    )


def search_flex(
    origin: str,
    destination: str,
    around: date,
    flex_days: int,
    *,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    trip: str = "one-way",
    nights: Optional[int] = None,
    top: int = DEFAULT_TOP,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    sort: FlightSort = "ranked",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
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
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    nearby: bool = False,
    currency: str = "EUR",
    country: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[CalendarSource] = None,
) -> FlexSearchReport | tuple[FlexSearchReport, ...]:
    """Calendar window, then at most one shopping POST on the cheapest legal day.

    A compact calendar miss or a window with no priced day is empty: no
    per-day shopping sweep, no invented fare.
    """
    currency = normalize_currency(currency)
    country = normalize_country(country)
    if top <= 0:
        raise ValueError("top must be a positive integer")
    if buffer_eur < 0:
        raise ValueError("baggage buffer must not be negative")
    if sort not in ("ranked", "fare", "price", "duration", "departure", "arrival"):
        raise ValueError(
            "sort must be 'ranked', 'fare', 'price', 'duration', 'departure', or 'arrival'"
        )
    validate_layover_hours(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    start, end = flex_window(around, flex_days)
    kind, stay = resolve_date_trip(trip, nights)
    parsed_via, parsed_exclude_via = _parse_via_pair(via, exclude_via)
    parsed_no_overnight, parsed_require_overnight = parse_overnight_lists(
        no_overnight, require_overnight
    )
    parsed_exclude_airports = _parse_exclude_airports(exclude_airports)
    parsed_include_airports = _parse_include_airports(include_airports)
    seed = calendar_trip(
        origin,
        destination,
        start,
        max_stops=max_stops,
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        nights=stay,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
    )
    trips = expand_nearby_trips((seed,), nearby=nearby)
    trips = keep_included_dest_trips(trips, parsed_include_airports)
    trips = drop_excluded_airport_trips(trips, parsed_exclude_airports)
    if not trips:
        return _empty_flex_report(
            origin,
            destination,
            around,
            flex_days,
            start,
            end,
            kind=kind,
            stay=stay,
            currency=currency,
        )
    report_progress = progress or (lambda _: None)
    client = source or GoogleFlightsHttpSource(currency=currency, country=country)
    reports: list[FlexSearchReport] = []
    try:
        for item in trips:
            if not isinstance(item, (FlightQuery, RoundTrip)):
                continue
            reports.append(
                _flex_report_for_seed(
                    client,
                    item,
                    around,
                    flex_days,
                    start,
                    end,
                    kind=kind,
                    stay=stay,
                    max_stops=max_stops,
                    adults=adults,
                    cabin=cabin,
                    bags=bags,
                    carry_on=carry_on,
                    price_cap_eur=price_cap_eur,
                    airlines=airlines,
                    exclude_airlines=exclude_airlines,
                    alliances=alliances,
                    exclude_alliances=exclude_alliances,
                    parsed_via=parsed_via,
                    parsed_exclude_via=parsed_exclude_via,
                    parsed_no_overnight=parsed_no_overnight,
                    parsed_require_overnight=parsed_require_overnight,
                    depart_window=depart_window,
                    arrive_before=arrive_before,
                    depart_after=depart_after,
                    max_layover_hours=max_layover_hours,
                    min_layover_hours=min_layover_hours,
                    max_duration_hours=max_duration_hours,
                    top=top,
                    buffer_eur=buffer_eur,
                    sort=sort,
                    currency=currency,
                    country=country,
                    report_progress=report_progress,
                )
            )
    finally:
        client.close()
    return _one_or_many(reports)


def write_flex_report_atomic(report: FlexSearchReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


def write_flex_reports_atomic(reports: Sequence[FlexSearchReport], destination: Path) -> None:
    owned = tuple(reports)
    if len(owned) == 1:
        write_flex_report_atomic(owned[0], destination)
        return
    write_json_atomic({"queries": [row.to_dict() for row in owned]}, destination)


def _return_for(day: date, nights: Optional[int], found: Optional[date] = None) -> Optional[date]:
    if found is not None:
        return found
    if nights is None:
        return None
    return shift_day(day, nights)


def _rows_from_calendar(
    start: date,
    end: date,
    compact: Sequence[CompactCalendarDay],
    *,
    nights: Optional[int] = None,
) -> tuple[DatePriceRow, ...]:
    by_day = {row.departure_date: row for row in compact}
    rows: list[DatePriceRow] = []
    cursor = start
    while cursor <= end:
        found = by_day.get(cursor)
        returning = _return_for(cursor, nights, None if found is None else found.return_date)
        if found is None or found.price_eur is None:
            rows.append(DatePriceRow(departure_date=cursor, return_date=returning, status="empty"))
        else:
            rows.append(
                DatePriceRow(
                    departure_date=cursor,
                    price_eur=found.price_eur,
                    return_date=returning,
                    status="ok",
                )
            )
        cursor = shift_day(cursor, 1)
    return tuple(rows)


def _row_from_day_cards(
    cursor: date,
    query: FlightQuery | RoundTrip,
    cards: Sequence[RawFlightCard],
    returning: Optional[date],
    *,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> DatePriceRow:
    offers = _offers_from_cards(
        cards,
        query,
        via=via,
        exclude_via=exclude_via,
        no_overnight=no_overnight,
        require_overnight=require_overnight,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    if not offers:
        return DatePriceRow(departure_date=cursor, return_date=returning, status="empty")
    best = min(offers, key=lambda offer: offer.price_eur)
    return DatePriceRow(
        departure_date=cursor,
        price_eur=best.price_eur,
        airline=best.airline,
        stops_count=best.stops_count,
        return_date=returning,
        status="ok",
        stops_compare=compare_nonstop_vs_one_stop(offers),
    )


def _row_from_day_error(
    cursor: date,
    exc: BaseException,
    returning: Optional[date],
) -> DatePriceRow:
    error = classify_failure(exc)
    if error.code in {SearchErrorCode.NO_RESULTS, SearchErrorCode.REJECTED}:
        return DatePriceRow(departure_date=cursor, return_date=returning, status="empty")
    return DatePriceRow(
        departure_date=cursor,
        return_date=returning,
        status="error",
        error=error,
    )


def _sweep_per_day(
    source: CalendarSource,
    seed: FlightQuery | RoundTrip,
    start: date,
    end: date,
    nights: Optional[int],
    progress: Callable[[str], None],
    *,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> tuple[DatePriceRow, ...]:
    day_queries: list[tuple[date, FlightQuery | RoundTrip]] = []
    cursor = start
    span = date_window_days(start, end)
    index = 0
    while cursor <= end:
        index += 1
        progress(f"[{index}/{span}] {seed.origin} -> {seed.destination} {cursor.isoformat()}")
        day_queries.append((cursor, _day_trip(seed, cursor, nights)))
        cursor = shift_day(cursor, 1)

    fetch_many = getattr(source, "fetch_many", None)
    if callable(fetch_many):
        results = fetch_many([day_query for _cursor, day_query in day_queries])
        rows = []
        for (cursor, day_query), result in zip(day_queries, results, strict=True):
            returning = _return_for(cursor, nights)
            if isinstance(result, BaseException):
                rows.append(_row_from_day_error(cursor, result, returning))
            else:
                rows.append(
                    _row_from_day_cards(
                        cursor,
                        day_query,
                        result,
                        returning,
                        via=via,
                        exclude_via=exclude_via,
                        no_overnight=no_overnight,
                        require_overnight=require_overnight,
                        depart_window=depart_window,
                        arrive_before=arrive_before,
                        depart_after=depart_after,
                        max_layover_hours=max_layover_hours,
                        min_layover_hours=min_layover_hours,
                        max_duration_hours=max_duration_hours,
                    )
                )
        return tuple(rows)

    rows: list[DatePriceRow] = []
    for cursor, day_query in day_queries:
        returning = _return_for(cursor, nights)
        try:
            cards = source.fetch(day_query)
        except Exception as exc:
            rows.append(_row_from_day_error(cursor, exc, returning))
            continue
        rows.append(
            _row_from_day_cards(
                cursor,
                day_query,
                cards,
                returning,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_layover_hours=max_layover_hours,
                min_layover_hours=min_layover_hours,
                max_duration_hours=max_duration_hours,
            )
        )
    return tuple(rows)


def _error_rows(
    start: date,
    end: date,
    error: SearchError,
    *,
    nights: Optional[int] = None,
) -> tuple[DatePriceRow, ...]:
    rows: list[DatePriceRow] = []
    cursor = start
    while cursor <= end:
        rows.append(
            DatePriceRow(
                departure_date=cursor,
                return_date=_return_for(cursor, nights),
                status="error",
                error=error,
            )
        )
        cursor = shift_day(cursor, 1)
    return tuple(rows)
