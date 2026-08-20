"""MCP tool handlers. Import the public API only; do not import the MCP SDK."""

from __future__ import annotations

import calendar
import threading
from datetime import date
from typing import Mapping, Optional, Sequence

from viajante.airports import lookup_airports
from viajante.dates import parse_route_pair, search_dates, validate_date_window
from viajante.explore import DEFAULT_EXPLORE_TOP, search_explore, validate_explore_window
from viajante.flights import (
    DEFAULT_BAGGAGE_BUFFER_EUR,
    FlightSort,
    parse_airline_codes,
    parse_depart_window,
    parse_flight_plan,
    search_flights,
)
from viajante.hotels import HotelSourceName, search_hotels
from viajante.models import FlightCabin, HotelQuery, MultiCity, RoundTrip, Trip

_SEARCH_LOCK = threading.Lock()


def _as_trips(plan: object) -> tuple[Trip, ...]:
    if isinstance(plan, (RoundTrip, MultiCity)):
        return (plan,)
    return tuple(plan)  # type: ignore[arg-type]


def _reject_past(dates: Sequence[date], *, label: str = "departure") -> None:
    today = date.today()
    for value in dates:
        if value < today:
            raise ValueError(f"{label} date is in the past: {value.isoformat()}")


def _with_search_lock(fn):
    if not _SEARCH_LOCK.acquire(blocking=False):
        raise ValueError("a viajante search is already running in this process")
    try:
        return fn()
    finally:
        _SEARCH_LOCK.release()


def _month_start(value: str) -> date:
    try:
        year_text, month_text = value.split("-", 1)
        year, month = int(year_text), int(month_text)
        return date(year, month, 1)
    except ValueError as exc:
        raise ValueError("month must look like YYYY-MM") from exc


def lookup_airports_tool(query: str, *, limit: int = 20) -> list[Mapping[str, str]]:
    return [row.to_dict() for row in lookup_airports(query, limit=limit)]


def search_flights_tool(
    routes: Sequence[str],
    *,
    trip: str = "one-way",
    max_stops: int = 1,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    top: int = 8,
    fetch: str = "auto",
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    depart_window: Optional[str] = None,
    max_duration: Optional[float] = None,
    min_layover: Optional[float] = None,
    max_layover: Optional[float] = None,
    baggage_buffer: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    sort: FlightSort = "ranked",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
) -> Mapping[str, object]:
    plan = parse_flight_plan(
        routes,
        trip=trip,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
    )
    trips = _as_trips(plan)
    _reject_past([leg.departure_date for item in trips for leg in item.legs])
    report = _with_search_lock(
        lambda: search_flights(
            trips,
            top=top,
            fetch=fetch,  # type: ignore[arg-type]
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            depart_window=parse_depart_window(depart_window),
            max_duration_hours=max_duration,
            min_layover_hours=min_layover,
            max_layover_hours=max_layover,
            buffer_eur=baggage_buffer,
            sort=sort,
        )
    )
    return dict(report.to_dict())


def search_dates_tool(
    route: str,
    start: str,
    end: str,
    *,
    max_stops: int = 1,
    adults: int = 1,
    cabin: FlightCabin = "economy",
) -> Mapping[str, object]:
    origin, destination = parse_route_pair(route)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    validate_date_window(start_date, end_date)
    _reject_past((start_date,))
    report = _with_search_lock(
        lambda: search_dates(
            origin,
            destination,
            start_date,
            end_date,
            max_stops=max_stops,
            adults=adults,
            cabin=cabin,
        )
    )
    return dict(report.to_dict())


def search_explore_tool(
    origin: str,
    start: Optional[str] = None,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    month: Optional[str] = None,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
) -> Mapping[str, object]:
    if month and start:
        raise ValueError("use either month or start, not both")
    if month:
        start_date = _month_start(month)
        days = calendar.monthrange(start_date.year, start_date.month)[1]
    else:
        if not start:
            raise ValueError("start or month is required")
        start_date = date.fromisoformat(start)
    validate_explore_window(start_date, days)
    _reject_past((start_date,))
    report = _with_search_lock(
        lambda: search_explore(
            origin,
            start_date,
            days=days,
            top=top,
            adults=adults,
            cabin=cabin,
            max_stops=max_stops,
        )
    )
    return dict(report.to_dict())


def search_hotels_tool(
    location: str,
    check_in: str,
    check_out: str,
    *,
    adults: int = 2,
    rooms: int = 1,
    top: int = 8,
    min_rating: Optional[float] = None,
    entire_home: bool = False,
    free_cancellation: bool = True,
    source: HotelSourceName = "google",
) -> Mapping[str, object]:
    if source == "google" and min_rating is not None and min_rating > 5:
        raise ValueError("min_rating must be at most 5 with source google")
    check_in_date = date.fromisoformat(check_in)
    check_out_date = date.fromisoformat(check_out)
    _reject_past((check_in_date,), label="check-in")
    query = HotelQuery(
        location,
        check_in_date,
        check_out_date,
        adults=adults,
        rooms=rooms,
        min_rating=min_rating,
        entire_home=entire_home,
        free_cancellation=free_cancellation,
    )
    report = _with_search_lock(lambda: search_hotels((query,), top=top, source=source))
    return dict(report.to_dict())
