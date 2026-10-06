"""MCP tool handlers. Import the public API only; do not import the MCP SDK."""

from __future__ import annotations

import functools
import threading
import time
from copy import deepcopy
from datetime import date
from typing import Mapping, Optional, Sequence

from viajante.airports import lookup_airports, parse_exclude_regions
from viajante.carriers import parse_airline_codes, parse_alliances
from viajante.dates import (
    flex_window,
    parse_route_pair,
    resolve_date_trip,
    search_dates,
    search_flex,
    validate_date_window,
)
from viajante.details import get_hotel_details
from viajante.envelope import stamp_local, stamp_recheck, stamp_search
from viajante.evidence import (
    failure_codes,
    find_offer,
    record,
    selected_reference,
    selection_records,
)
from viajante.evidence import verify_answer as verify_answer_evidence
from viajante.explore import (
    DEFAULT_EXPLORE_TOP,
    month_window,
    search_explore,
    validate_explore_window,
)
from viajante.flights import (
    DEFAULT_TOP,
    FlightSort,
    as_trips,
    expand_nearby_trips,
    parse_depart_window,
    parse_flight_plan,
    parse_named_clock,
    parse_overnight_airports,
    parse_via_airports,
    search_flights,
)
from viajante.hotels import (
    HotelSourceName,
    resolve_hotel_currency,
    search_hotels,
    validate_max_distance,
    validate_near,
)
from viajante.models import FlightCabin, HotelQuery
from viajante.points import (
    award_offer_from_mapping,
    compare_award,
    parse_balances,
    transfer_paths,
)
from viajante.quote import (
    first_origin_iata,
    resolve_quote_and_buffer,
)
from viajante.recheck import recheck_offer
from viajante.skiplagged import search_hidden_city
from viajante.skiplagged_hotels import search_hotel_rooms
from viajante.stays import plan_stay_blocks, split_stay_costs
from viajante.storage import reports_payload
from viajante.trip import search_trip, stay_window_from_trips
from viajante.validate import validate_itinerary

_SEARCH_LOCK = threading.Lock()
CACHE_SECONDS = 300.0
_CACHE: dict[tuple[str, str], tuple[float, dict, dict]] = {}
_SELECTION_CONTEXT = threading.local()


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


def _owned(payload: dict, report=None) -> dict:
    selections = selection_records(payload, report)
    record(payload, selections=selections)
    _SELECTION_CONTEXT.records = selections
    return payload


def _searched(payload: dict, report=None) -> dict:
    return _owned(stamp_search(payload), report)


def _cached(fn):
    """Replay an identical successful search for CACHE_SECONDS instead of asking Google again."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        key = (fn.__name__, repr((args, sorted(kwargs.items()))))
        now = time.monotonic()
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            result = {**deepcopy(hit[1]), "cached": True}
            record(result, selections=hit[2])
            return result
        _SELECTION_CONTEXT.records = {}
        result = fn(*args, **kwargs)
        selections = getattr(_SELECTION_CONTEXT, "records", {})
        _SELECTION_CONTEXT.records = {}
        if not failure_codes(result):
            for stale in [k for k, (at, _, _) in _CACHE.items() if now - at >= CACHE_SECONDS]:
                del _CACHE[stale]
            if len(_CACHE) >= 20:
                del _CACHE[next(iter(_CACHE))]
            _CACHE[key] = (now, deepcopy(result), selections)
        return result

    return wrapper


def lookup_airports_tool(query: str, *, limit: int = 20) -> list[Mapping[str, str]]:
    return [row.to_dict() for row in lookup_airports(query, limit=limit)]


@_cached
def search_flights_tool(
    routes: Sequence[str],
    *,
    trip: str = "one-way",
    max_stops: int = 1,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    top: int = DEFAULT_TOP,
    fetch: str = "auto",
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    alliance: Optional[str] = None,
    exclude_alliance: Optional[str] = None,
    depart_window: Optional[str] = None,
    arrive_before: Optional[str] = None,
    depart_after: Optional[str] = None,
    max_duration: Optional[float] = None,
    min_layover: Optional[float] = None,
    max_layover: Optional[float] = None,
    via: Optional[str] = None,
    exclude_via: Optional[str] = None,
    no_overnight: Optional[str] = None,
    require_overnight: Optional[str] = None,
    exclude_airports: Optional[str] = None,
    include_airports: Optional[str] = None,
    baggage_buffer: Optional[int] = None,
    sort: FlightSort = "ranked",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    nearby: bool = False,
    proxy: Optional[str] = None,
) -> Mapping[str, object]:
    plan = parse_flight_plan(
        routes,
        trip=trip,
        max_stops=max_stops,
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap=price_cap,
    )
    trips = expand_nearby_trips(as_trips(plan), nearby=nearby)
    _reject_past([leg.departure_date for item in trips for leg in item.legs])
    currency, baggage_buffer = resolve_quote_and_buffer(
        currency, first_origin_iata(trips[0]), baggage_buffer
    )
    report = _with_search_lock(
        lambda: search_flights(
            trips,
            top=top,
            fetch=fetch,  # type: ignore[arg-type]
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            alliances=parse_alliances(alliance),
            exclude_alliances=parse_alliances(exclude_alliance),
            depart_window=parse_depart_window(depart_window),
            arrive_before=parse_named_clock(arrive_before, role="arrive-before"),
            depart_after=parse_named_clock(depart_after, role="depart-after"),
            max_duration_hours=max_duration,
            min_layover_hours=min_layover,
            max_layover_hours=max_layover,
            via=parse_via_airports(via),
            exclude_via=parse_via_airports(exclude_via, role="exclude-via"),
            no_overnight=parse_overnight_airports(no_overnight, role="no-overnight"),
            require_overnight=parse_overnight_airports(require_overnight, role="require-overnight"),
            exclude_airports=parse_via_airports(exclude_airports, role="exclude-airports"),
            include_airports=parse_via_airports(include_airports, role="include-airports"),
            baggage_buffer=baggage_buffer,
            sort=sort,
            currency=currency,
            country=country,
            proxy=proxy,
        )
    )
    return _searched(reports_payload(report), report)


@_cached
def search_dates_tool(
    route: str,
    start: str,
    end: str,
    *,
    max_stops: int = 1,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    trip: str = "one-way",
    nights: Optional[int] = None,
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    alliance: Optional[str] = None,
    exclude_alliance: Optional[str] = None,
    via: Optional[str] = None,
    exclude_via: Optional[str] = None,
    no_overnight: Optional[str] = None,
    require_overnight: Optional[str] = None,
    exclude_airports: Optional[str] = None,
    include_airports: Optional[str] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    nearby: bool = False,
    depart_window: Optional[str] = None,
    arrive_before: Optional[str] = None,
    depart_after: Optional[str] = None,
    max_duration: Optional[float] = None,
    min_layover: Optional[float] = None,
    max_layover: Optional[float] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    baggage_buffer: Optional[int] = None,
    sort: Optional[FlightSort] = None,
    proxy: Optional[str] = None,
) -> Mapping[str, object]:
    origin, destination = parse_route_pair(route)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    validate_date_window(start_date, end_date)
    kind, stay = resolve_date_trip(trip, nights)
    currency, baggage_buffer = resolve_quote_and_buffer(currency, origin, baggage_buffer)
    report = _with_search_lock(
        lambda: search_dates(
            origin,
            destination,
            start_date,
            end_date,
            max_stops=max_stops,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            trip=kind,
            nights=stay,
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            alliances=parse_alliances(alliance),
            exclude_alliances=parse_alliances(exclude_alliance),
            via=parse_via_airports(via),
            exclude_via=parse_via_airports(exclude_via, role="exclude-via"),
            no_overnight=parse_overnight_airports(no_overnight, role="no-overnight"),
            require_overnight=parse_overnight_airports(require_overnight, role="require-overnight"),
            exclude_airports=parse_via_airports(exclude_airports, role="exclude-airports"),
            include_airports=parse_via_airports(include_airports, role="include-airports"),
            bags=bags,
            carry_on=carry_on,
            price_cap=price_cap,
            nearby=nearby,
            depart_window=parse_depart_window(depart_window),
            arrive_before=parse_named_clock(arrive_before, role="arrive-before"),
            depart_after=parse_named_clock(depart_after, role="depart-after"),
            max_duration_hours=max_duration,
            min_layover_hours=min_layover,
            max_layover_hours=max_layover,
            currency=currency,
            country=country,
            baggage_buffer=baggage_buffer,
            sort=sort,
            proxy=proxy,
        )
    )
    return _searched(reports_payload(report), report)


@_cached
def search_flex_tool(
    route: str,
    around: str,
    flex: int,
    *,
    max_stops: int = 1,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    trip: str = "one-way",
    nights: Optional[int] = None,
    top: int = DEFAULT_TOP,
    baggage_buffer: Optional[int] = None,
    sort: FlightSort = "ranked",
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    alliance: Optional[str] = None,
    exclude_alliance: Optional[str] = None,
    via: Optional[str] = None,
    exclude_via: Optional[str] = None,
    no_overnight: Optional[str] = None,
    require_overnight: Optional[str] = None,
    exclude_airports: Optional[str] = None,
    include_airports: Optional[str] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    nearby: bool = False,
    depart_window: Optional[str] = None,
    arrive_before: Optional[str] = None,
    depart_after: Optional[str] = None,
    max_duration: Optional[float] = None,
    min_layover: Optional[float] = None,
    max_layover: Optional[float] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> Mapping[str, object]:
    origin, destination = parse_route_pair(route)
    around_date = date.fromisoformat(around)
    flex_window(around_date, flex)
    kind, stay = resolve_date_trip(trip, nights)
    currency, baggage_buffer = resolve_quote_and_buffer(currency, origin, baggage_buffer)
    report = _with_search_lock(
        lambda: search_flex(
            origin,
            destination,
            around_date,
            flex,
            max_stops=max_stops,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            trip=kind,
            nights=stay,
            top=top,
            baggage_buffer=baggage_buffer,
            sort=sort,
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            alliances=parse_alliances(alliance),
            exclude_alliances=parse_alliances(exclude_alliance),
            via=parse_via_airports(via),
            exclude_via=parse_via_airports(exclude_via, role="exclude-via"),
            no_overnight=parse_overnight_airports(no_overnight, role="no-overnight"),
            require_overnight=parse_overnight_airports(require_overnight, role="require-overnight"),
            exclude_airports=parse_via_airports(exclude_airports, role="exclude-airports"),
            include_airports=parse_via_airports(include_airports, role="include-airports"),
            bags=bags,
            carry_on=carry_on,
            price_cap=price_cap,
            nearby=nearby,
            depart_window=parse_depart_window(depart_window),
            arrive_before=parse_named_clock(arrive_before, role="arrive-before"),
            depart_after=parse_named_clock(depart_after, role="depart-after"),
            max_duration_hours=max_duration,
            min_layover_hours=min_layover,
            max_layover_hours=max_layover,
            currency=currency,
            country=country,
            proxy=proxy,
        )
    )
    return _searched(reports_payload(report), report)


@_cached
def search_explore_tool(
    origin: str,
    start: Optional[str] = None,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    month: Optional[str] = None,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    alliance: Optional[str] = None,
    exclude_alliance: Optional[str] = None,
    via: Optional[str] = None,
    exclude_via: Optional[str] = None,
    no_overnight: Optional[str] = None,
    require_overnight: Optional[str] = None,
    exclude_airports: Optional[str] = None,
    include_airports: Optional[str] = None,
    exclude_regions: Optional[str] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    nearby: bool = False,
    depart_window: Optional[str] = None,
    arrive_before: Optional[str] = None,
    depart_after: Optional[str] = None,
    max_duration: Optional[float] = None,
    min_layover: Optional[float] = None,
    max_layover: Optional[float] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    sort: FlightSort = "price",
    baggage_buffer: Optional[int] = None,
    proxy: Optional[str] = None,
) -> Mapping[str, object]:
    if month and start:
        raise ValueError("use either month or start, not both")
    if month:
        start_date, days = month_window(month)
    else:
        if not start:
            raise ValueError("start or month is required")
        start_date = date.fromisoformat(start)
    validate_explore_window(start_date, days)
    currency, baggage_buffer = resolve_quote_and_buffer(currency, origin, baggage_buffer)
    report = _with_search_lock(
        lambda: search_explore(
            origin,
            start_date,
            days=days,
            top=top,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            max_stops=max_stops,
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            alliances=parse_alliances(alliance),
            exclude_alliances=parse_alliances(exclude_alliance),
            via=parse_via_airports(via),
            exclude_via=parse_via_airports(exclude_via, role="exclude-via"),
            no_overnight=parse_overnight_airports(no_overnight, role="no-overnight"),
            require_overnight=parse_overnight_airports(require_overnight, role="require-overnight"),
            exclude_airports=parse_via_airports(exclude_airports, role="exclude-airports"),
            include_airports=parse_via_airports(include_airports, role="include-airports"),
            exclude_regions=parse_exclude_regions(exclude_regions),
            bags=bags,
            carry_on=carry_on,
            price_cap=price_cap,
            nearby=nearby,
            depart_window=parse_depart_window(depart_window),
            arrive_before=parse_named_clock(arrive_before, role="arrive-before"),
            depart_after=parse_named_clock(depart_after, role="depart-after"),
            max_duration_hours=max_duration,
            min_layover_hours=min_layover,
            max_layover_hours=max_layover,
            currency=currency,
            country=country,
            sort=sort,
            baggage_buffer=baggage_buffer,
            proxy=proxy,
        )
    )
    return _searched(reports_payload(report), report)


MAX_HOTEL_STAYS = 8


def _hotel_queries(
    location: Optional[str],
    check_in: Optional[str],
    check_out: Optional[str],
    stays: Optional[Sequence[Mapping[str, object]]],
    *,
    adults: int,
    rooms: int,
    min_rating: Optional[float],
    entire_home: bool,
    free_cancellation: bool,
) -> tuple[HotelQuery, ...]:
    if stays is None:
        specs: Sequence[Mapping[str, object]] = (
            {"location": location, "check_in": check_in, "check_out": check_out},
        )
    else:
        if location is not None or check_in is not None or check_out is not None:
            raise ValueError("pass either location/check_in/check_out or stays, not both")
        if not stays:
            raise ValueError("stays must not be empty")
        if len(stays) > MAX_HOTEL_STAYS:
            raise ValueError(f"stays is limited to {MAX_HOTEL_STAYS} per call")
        specs = stays
    queries = []
    for index, spec in enumerate(specs):
        unknown = set(spec) - {"location", "check_in", "check_out", "adults", "rooms"}
        if unknown:
            raise ValueError(f"stay {index + 1}: unknown keys {sorted(unknown)}")
        place, start, end = spec.get("location"), spec.get("check_in"), spec.get("check_out")
        if not (isinstance(place, str) and isinstance(start, str) and isinstance(end, str)):
            raise ValueError(f"stay {index + 1}: location, check_in and check_out are required")
        check_in_date = date.fromisoformat(start)
        _reject_past((check_in_date,), label="check-in")
        party = {}
        for name, default in (("adults", adults), ("rooms", rooms)):
            value = spec.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"stay {index + 1}: {name} must be an integer")
            party[name] = value
        queries.append(
            HotelQuery(
                place,
                check_in_date,
                date.fromisoformat(end),
                adults=party["adults"],
                rooms=party["rooms"],
                min_rating=min_rating,
                entire_home=entire_home,
                free_cancellation=free_cancellation,
            )
        )
    return tuple(queries)


def _near_point(near: Optional[Mapping[str, float]]) -> Optional[tuple[float, float]]:
    if near is None:
        return None
    if not isinstance(near, Mapping) or set(near) != {"lat", "lng"}:
        raise ValueError("near must be {lat, lng}")
    return (near["lat"], near["lng"])


@_cached
def search_hotels_tool(
    location: Optional[str] = None,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    *,
    adults: int = 2,
    rooms: int = 1,
    top: int = DEFAULT_TOP,
    min_rating: Optional[float] = None,
    entire_home: bool = False,
    free_cancellation: bool = True,
    source: HotelSourceName = "google",
    currency: Optional[str] = None,
    stays: Optional[Sequence[Mapping[str, object]]] = None,
    near: Optional[Mapping[str, float]] = None,
    max_distance_km: Optional[float] = None,
) -> Mapping[str, object]:
    if source == "google" and min_rating is not None and min_rating > 5:
        raise ValueError("min_rating must be at most 5 with source google")
    currency = resolve_hotel_currency(source, currency)
    queries = _hotel_queries(
        location,
        check_in,
        check_out,
        stays,
        adults=adults,
        rooms=rooms,
        min_rating=min_rating,
        entire_home=entire_home,
        free_cancellation=free_cancellation,
    )
    near_point = validate_near(_near_point(near))
    max_distance_km = validate_max_distance(max_distance_km, near_point)
    report = _with_search_lock(
        lambda: search_hotels(
            queries,
            top=top,
            source=source,
            currency=currency,
            near=near_point,
            max_distance_km=max_distance_km,
        )
    )
    return _searched(reports_payload(report), report)


@_cached
def search_hotel_rooms_tool(
    hotel_id: Optional[int],
    check_in: str,
    check_out: str,
    *,
    hotel_name: Optional[str] = None,
    city: Optional[str] = None,
    adults: int = 2,
    rooms: int = 1,
) -> Mapping[str, object]:
    check_in_date = date.fromisoformat(check_in)
    check_out_date = date.fromisoformat(check_out)
    _reject_past((check_in_date,), label="check-in")
    report = _with_search_lock(
        lambda: search_hotel_rooms(
            hotel_id,
            check_in_date,
            check_out_date,
            hotel_name=hotel_name,
            city=city,
            adults=adults,
            rooms=rooms,
        )
    )
    return _searched(dict(report.to_dict()))


@_cached
def search_trip_tool(
    routes: Sequence[str],
    location: str,
    *,
    check_in: Optional[str] = None,
    check_out: Optional[str] = None,
    trip: str = "one-way",
    max_stops: int = 1,
    adults: int = 1,
    rooms: int = 1,
    cabin: FlightCabin = "economy",
    top: int = DEFAULT_TOP,
    fetch: str = "auto",
    baggage_buffer: Optional[int] = None,
    sort: FlightSort = "ranked",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    airlines: Optional[str] = None,
    exclude_airlines: Optional[str] = None,
    alliance: Optional[str] = None,
    exclude_alliance: Optional[str] = None,
    via: Optional[str] = None,
    exclude_via: Optional[str] = None,
    no_overnight: Optional[str] = None,
    require_overnight: Optional[str] = None,
    exclude_airports: Optional[str] = None,
    include_airports: Optional[str] = None,
    arrive_before: Optional[str] = None,
    depart_after: Optional[str] = None,
    price_cap: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    min_rating: Optional[float] = None,
    entire_home: bool = False,
    free_cancellation: bool = True,
    source: HotelSourceName = "google",
    nearby: bool = False,
) -> Mapping[str, object]:
    """Flights then hotel, one lock. trip_total omitted if either side missed."""
    if source == "google" and min_rating is not None and min_rating > 5:
        raise ValueError("min_rating must be at most 5 with source google")
    plan = parse_flight_plan(
        routes,
        trip=trip,
        max_stops=max_stops,
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap=price_cap,
    )
    trips = expand_nearby_trips(as_trips(plan), nearby=nearby)
    _reject_past([leg.departure_date for item in trips for leg in item.legs])
    currency, baggage_buffer = resolve_quote_and_buffer(
        currency, first_origin_iata(trips[0]), baggage_buffer
    )
    window = stay_window_from_trips(trips)
    if check_in:
        check_in_date = date.fromisoformat(check_in)
    elif window is not None:
        check_in_date = window[0]
    else:
        raise ValueError("hotel stay needs check_in (or a two-date flight route)")
    if check_out:
        check_out_date = date.fromisoformat(check_out)
    elif window is not None:
        check_out_date = window[1]
    else:
        raise ValueError("hotel stay needs check_out (or a two-date flight route)")
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
    report = _with_search_lock(
        lambda: search_trip(
            trips,
            query,
            top=top,
            baggage_buffer=baggage_buffer,
            sort=sort,
            fetch=fetch,
            airlines=parse_airline_codes(airlines),
            exclude_airlines=parse_airline_codes(exclude_airlines),
            alliances=parse_alliances(alliance),
            exclude_alliances=parse_alliances(exclude_alliance),
            via=parse_via_airports(via),
            exclude_via=parse_via_airports(exclude_via, role="exclude-via"),
            no_overnight=parse_overnight_airports(no_overnight, role="no-overnight"),
            require_overnight=parse_overnight_airports(require_overnight, role="require-overnight"),
            exclude_airports=parse_via_airports(exclude_airports, role="exclude-airports"),
            include_airports=parse_via_airports(include_airports, role="include-airports"),
            arrive_before=parse_named_clock(arrive_before, role="arrive-before"),
            depart_after=parse_named_clock(depart_after, role="depart-after"),
            bags=bags,
            carry_on=carry_on,
            price_cap=price_cap,
            currency=currency,
            country=country,
            hotel_source=source,
        )
    )
    return _searched(reports_payload(report), report)


@_cached
def search_hidden_city_tool(
    route: str,
    departure: str,
    *,
    return_date: Optional[str] = None,
    adults: int = 1,
    top: int = DEFAULT_TOP,
    currency: Optional[str] = None,
) -> Mapping[str, object]:
    """Skiplagged MCP search. Opt-in. Does not mix Google Flights results.

    Named currency is a keep. Skiplagged cards are USD; omit or pass USD.
    """
    origin, destination = parse_route_pair(route)
    departure_date = date.fromisoformat(departure)
    back = date.fromisoformat(return_date) if return_date else None
    _reject_past((departure_date,))
    report = _with_search_lock(
        lambda: search_hidden_city(
            origin,
            destination,
            departure_date,
            return_date=back,
            adults=adults,
            top=top,
            currency=currency,
        )
    )
    return _searched(reports_payload(report), report)


def compare_awards_tool(
    offer: Mapping[str, object],
    *,
    cash_price: Optional[float] = None,
    currency: Optional[str] = None,
    balances: Optional[Sequence[Mapping[str, object]]] = None,
) -> Mapping[str, object]:
    """Local award vs cash math. Does not search live award inventory."""
    award = award_offer_from_mapping(offer)
    parsed_balances = parse_balances(balances or ())
    report = compare_award(
        award,
        cash_price=cash_price,
        currency=currency,
        balances=parsed_balances,
    )
    return stamp_local(dict(report.to_dict()))


def lookup_transfers_tool(
    program: str,
    points: int,
    *,
    balances: Optional[Sequence[Mapping[str, object]]] = None,
) -> Mapping[str, object]:
    parsed = parse_balances(balances or ())
    paths = transfer_paths(program, points, parsed)
    return stamp_local(
        {
            "program": program.strip().casefold(),
            "points": points,
            "transfer_paths": [path.to_dict() for path in paths],
        }
    )


def plan_stay_blocks_tool(roster: Mapping[str, Sequence[str]]) -> Mapping[str, object]:
    """Local: consecutive nights with the same people, as check-in/check-out blocks."""
    return stamp_local(dict(plan_stay_blocks(roster).to_dict()))


def split_stay_costs_tool(
    stays: Sequence[Mapping[str, object]],
    roster: Mapping[str, Sequence[str]],
    currency: str,
    *,
    fee_per_person_night: Optional[float] = None,
) -> Mapping[str, object]:
    """Local: split each stay's total among the people who sleep there, by nights."""
    report = split_stay_costs(
        stays, roster, currency=currency, fee_per_person_night=fee_per_person_night
    )
    payload = dict(report.to_dict())
    return _owned(stamp_local(payload, partial=bool(payload.get("unallocated_nights"))))


def validate_itinerary_tool(
    legs: Sequence[Mapping[str, object]],
    constraints: Mapping[str, object],
    *,
    currency: Optional[str] = None,
) -> Mapping[str, object]:
    payload = dict(validate_itinerary(legs, constraints, currency=currency).to_dict())
    return stamp_local(payload, partial=payload.get("feasible") is None)


def _without_previous(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [{key: value for key, value in row.items() if key != "previous"} for row in rows]


def recheck_offer_tool(
    offer: Mapping[str, object],
    *,
    query: Optional[Mapping[str, object]] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    fetch: Optional[str] = None,
    proxy: Optional[str] = None,
    allow_loose_match: bool = False,
    allow_substitute: bool = False,
) -> Mapping[str, object]:
    """One fresh search, never the replay cache, under the one-search process lock."""
    result = _with_search_lock(
        lambda: recheck_offer(
            offer,
            query=query,
            currency=currency,
            country=country,
            fetch=fetch,
            proxy=proxy,
            allow_loose_match=allow_loose_match,
            allow_substitute=allow_substitute,
            ledger_offer=find_offer,
        )
    )
    stamp_recheck(result)
    # Caller-typed values are not provider evidence: leave them out of the ledger.
    owned = dict(result)
    if result["previous"]["source"] != "search_evidence":  # type: ignore[index]
        del owned["previous"]
        if "differences" in owned:
            owned["differences"] = _without_previous(owned["differences"])  # type: ignore[arg-type]
        if "closest_candidate" in owned:
            closest = dict(owned["closest_candidate"])  # type: ignore[call-overload]
            closest["differences"] = _without_previous(closest["differences"])
            owned["closest_candidate"] = closest
    # Record only a check that ran to a provider answer (a match, a substitution, or a
    # completed not_found). An unsent or failed check, or incomplete_identity, proves nothing.
    if result["check_completed"]:
        record(owned)
    return result


def verify_answer_tool(answer: str) -> Mapping[str, object]:
    payload = dict(verify_answer_evidence(answer))
    if payload["ok"]:
        return stamp_local(payload)
    # status mirrors the verdict so it never reads "ok" beside "ok": false.
    nothing = not payload["searches"]
    return stamp_local(
        payload,
        status="failed",
        completeness="blocked" if nothing else "complete",
        error_code="no_search_recorded" if nothing else "unowned_claims",
    )


def get_hotel_details_tool(selection_id: str, *, room_rates: bool = False) -> Mapping[str, object]:
    if not isinstance(room_rates, bool):
        raise ValueError("room_rates must be a boolean")
    report, query_index, offer_index = selected_reference(selection_id, "hotel")

    def action():
        return get_hotel_details(report, query_index, offer_index, room_rates=room_rates)

    # A read does not enter the evidence ledger or the selection store.
    # room_rates false stays off the search lock; the server runs it on the lookup worker.
    detail = _with_search_lock(action) if room_rates else action()
    detail["original_quote"]["offer"]["selection_id"] = selection_id
    return detail
