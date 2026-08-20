"""Cheapest-per-day calendar via the owned Google Flights date-grid RPC."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from viajante.flights import _normalize_offer, classify_failure, normalize_trip_kind
from viajante.google_flights import GoogleFlightsHttpSource, RawFlightCard
from viajante.google_flights_rpc import CompactCalendarDay, CompactParseMiss
from viajante.models import (
    DateCalendarReport,
    DateCalendarSummary,
    DatePriceRow,
    DateTripKind,
    FlightCabin,
    FlightQuery,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    Trip,
)
from viajante.storage import write_json_atomic

MAX_DATE_WINDOW_DAYS = 31

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


def calendar_trip(
    origin: str,
    destination: str,
    start: date,
    *,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    nights: Optional[int] = None,
) -> FlightQuery | RoundTrip:
    if nights is None:
        return FlightQuery(
            origin=origin,
            destination=destination,
            departure_date=start,
            max_stops=max_stops,
            adults=adults,
            cabin=cabin,
        )
    return RoundTrip(
        origin=origin,
        destination=destination,
        departure_date=start,
        return_date=shift_day(start, nights),
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
    )


def search_dates(
    origin: str,
    destination: str,
    start: date,
    end: date,
    *,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    trip: str = "one-way",
    nights: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[CalendarSource] = None,
) -> DateCalendarReport:
    validate_date_window(start, end)
    kind, stay = resolve_date_trip(trip, nights)
    seed = calendar_trip(
        origin,
        destination,
        start,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        nights=stay,
    )
    report_progress = progress or (lambda _: None)
    stay_label = ""
    if kind == "rt" and stay is not None:
        night_word = "night" if stay == 1 else "nights"
        stay_label = f", rt {stay} {night_word}"
    report_progress(
        f"dates: {seed.origin} -> {seed.destination} "
        f"{start.isoformat()} .. {end.isoformat()} "
        f"(max {MAX_DATE_WINDOW_DAYS} days{stay_label})"
    )
    started = time.perf_counter()
    client = source or GoogleFlightsHttpSource()
    backend = "calendar"
    try:
        try:
            compact = client.fetch_calendar(seed, start, end)
            days = _rows_from_calendar(start, end, compact, nights=stay)
        except CompactParseMiss:
            report_progress("calendar miss; pricing each day with shopping sweep")
            days = _sweep_per_day(client, seed, start, end, stay, report_progress)
            backend = "sweep"
        except Exception as exc:
            error = classify_failure(exc)
            days = _error_rows(start, end, error, nights=stay)
    finally:
        client.close()
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
    )


def write_dates_report_atomic(report: DateCalendarReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


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
) -> DatePriceRow:
    offers = [
        offer
        for raw in cards
        if (offer := _normalize_offer(raw, query.legs[0].max_stops, buffer_eur=0)) is not None
    ]
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
) -> tuple[DatePriceRow, ...]:
    day_queries: list[tuple[date, FlightQuery | RoundTrip]] = []
    cursor = start
    span = date_window_days(start, end)
    index = 0
    while cursor <= end:
        index += 1
        progress(f"[{index}/{span}] {seed.origin} -> {seed.destination} {cursor.isoformat()}")
        day_queries.append(
            (
                cursor,
                calendar_trip(
                    seed.origin,
                    seed.destination,
                    cursor,
                    max_stops=seed.legs[0].max_stops,
                    adults=seed.adults,
                    cabin=seed.cabin,
                    nights=nights,
                ),
            )
        )
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
                rows.append(_row_from_day_cards(cursor, day_query, result, returning))
        return tuple(rows)

    rows: list[DatePriceRow] = []
    for cursor, day_query in day_queries:
        returning = _return_for(cursor, nights)
        try:
            cards = source.fetch(day_query)
        except Exception as exc:
            rows.append(_row_from_day_error(cursor, exc, returning))
            continue
        rows.append(_row_from_day_cards(cursor, day_query, cards, returning))
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
