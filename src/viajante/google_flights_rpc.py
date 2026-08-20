"""Owned Google Flights shopping RPC: request encode and compact parse."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from urllib.parse import quote, urlencode

from viajante.models import (
    FlightCabin,
    FlightQuery,
    MultiCity,
    RawJourneyLeg,
    RawLayover,
    RawSegment,
    RoundTrip,
    Trip,
)
from viajante.parsers import normalize_clock

SHOPPING_RESULTS_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetShoppingResults"
)
CALENDAR_GRID_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetCalendarGrid"
)
EXPLORE_DESTINATIONS_URL = (
    "https://www.google.com/_/FlightsFrontendUi/data/"
    "travel.frontend.flights.FlightsFrontendService/GetExploreDestinations"
)

_SEAT: Mapping[FlightCabin, int] = {
    "economy": 1,
    "premium-economy": 2,
    "business": 3,
    "first": 4,
}
_TRIP_ONE_WAY = 2
_TRIP_ROUND_TRIP = 1
_TRIP_MULTI_CITY = 3
_SEGMENT_OUTBOUND = 3
_SEGMENT_RETURN = 1
_ANTI_XSSI = ")]}'"
# viajante max_stops -> shopping segment[3]. TFS field 5 stays the viajante integer.
_SHOPPING_STOPS: Mapping[int, int] = {0: 1, 1: 2, 2: 3}
# Compact fare blocks put a [checked, carry_on] pair after the booking token.
# Counts above this are not bag counts (prices, durations).
_MAX_BAG_COUNT = 9


class CompactParseMiss(ValueError):
    """Compact shopping body was not a readable itinerary payload."""


class EmptyShoppingResults(Exception):
    """Shopping RPC returned itinerary slots with no priced offers."""


class ShoppingRejected(Exception):
    """Shopping RPC rejected the query (unknown airport or invalid request)."""


@dataclass(frozen=True)
class RawFlightCard:
    airline: Optional[str]
    departure: Optional[str]
    arrival: Optional[str]
    duration: Optional[str]
    stops: Optional[str]
    price: Optional[str]
    layover_city: Optional[str] = None
    layover_hours: Optional[float] = None
    flight_numbers: Optional[tuple[str, ...]] = None
    airline_codes: Optional[tuple[str, ...]] = None
    booking_token: Optional[str] = None
    legs: tuple[RawJourneyLeg, ...] = ()
    checked_bags: Optional[int] = None
    carry_on: Optional[int] = None


@dataclass(frozen=True)
class CompactCalendarDay:
    departure_date: date
    price_eur: Optional[float]


@dataclass(frozen=True)
class CompactExplorePlace:
    iata: str
    city: str
    country: Optional[str]


def shopping_stop_code(max_stops: int) -> int:
    try:
        return _SHOPPING_STOPS[max_stops]
    except KeyError:
        raise ValueError("max_stops must be 0, 1, or 2") from None


def _shopping_trip_kind(trip: Trip) -> int:
    if isinstance(trip, RoundTrip):
        return _TRIP_ROUND_TRIP
    if isinstance(trip, MultiCity):
        return _TRIP_MULTI_CITY
    return _TRIP_ONE_WAY


def _segment_classifier(trip: Trip, index: int) -> int:
    if isinstance(trip, RoundTrip) and index == 1:
        return _SEGMENT_RETURN
    return _SEGMENT_OUTBOUND


def _shopping_segment(
    *,
    origin: str,
    destination: Optional[str],
    departure_date: date,
    max_stops: int,
    classifier: int = _SEGMENT_OUTBOUND,
    selected_flight: Any = None,
) -> list[Any]:
    dest_field: Any = [[[destination, 0]]] if destination else []
    return [
        [[[origin, 0]]],
        dest_field,
        None,
        shopping_stop_code(max_stops),
        None,
        None,
        departure_date.isoformat(),
        None,
        selected_flight,
        None,
        None,
        None,
        None,
        None,
        classifier,
    ]


def _bags_constraint(*, bags: Optional[int], carry_on: Optional[int]) -> Any:
    if bags is None and carry_on is None:
        return None
    return [0 if bags is None else bags, 0 if carry_on is None else carry_on]


def _constraints_from_segments(
    segments: list[list[Any]],
    *,
    adults: int,
    cabin: FlightCabin,
    trip_kind: int = _TRIP_ONE_WAY,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
) -> list[Any]:
    return [
        None,
        None,
        trip_kind,
        None,
        [],
        _SEAT[cabin],
        [adults, 0, 0, 0],
        None,
        None,
        None,
        _bags_constraint(bags=bags, carry_on=carry_on),  # [checked, carry_on]; 7 is price-cap
        None,
        None,
        segments,
        None,
        None,
        None,
        1,
    ]


def build_search_constraints(
    trip: Trip,
    *,
    selected_flight: Any = None,
) -> list[Any]:
    return _constraints_from_segments(
        [
            _shopping_segment(
                origin=leg.origin,
                destination=leg.destination,
                departure_date=leg.departure_date,
                max_stops=leg.max_stops,
                classifier=_segment_classifier(trip, index),
                selected_flight=selected_flight if index == 0 else None,
            )
            for index, leg in enumerate(trip.legs)
        ],
        adults=trip.adults,
        cabin=trip.cabin,
        trip_kind=_shopping_trip_kind(trip),
        bags=trip.bags,
        carry_on=trip.carry_on,
    )


def build_shopping_inner(trip: Trip, token: Optional[str] = None) -> list[Any]:
    return [
        [None, None, None, token],
        build_search_constraints(trip),
        0,
        1,
        0,
        1,
    ]


def _rpc_params(html_lang: str, currency: str) -> dict[str, str]:
    return {
        "hl": html_lang,
        "curr": currency,
        "soc-app": "162",
        "soc-platform": "1",
        "soc-device": "1",
        "rt": "c",
    }


def _rpc_body(inner: list[Any]) -> str:
    envelope = json.dumps(
        [None, json.dumps(inner, separators=(",", ":"))],
        separators=(",", ":"),
    )
    return f"f.req={quote(envelope, safe='')}"


def build_shopping_request(
    trip: Trip,
    *,
    html_lang: str = "en",
    currency: str = "EUR",
) -> tuple[str, str]:
    url = f"{SHOPPING_RESULTS_URL}?{urlencode(_rpc_params(html_lang, currency))}"
    return url, _rpc_body(build_shopping_inner(trip))


def build_calendar_inner(
    query: FlightQuery,
    start: date,
    end: date,
) -> list[Any]:
    constraints = build_search_constraints(query)
    return [None, constraints, [start.isoformat(), end.isoformat()]]


def build_calendar_request(
    query: FlightQuery,
    start: date,
    end: date,
    *,
    html_lang: str = "en",
    currency: str = "EUR",
) -> tuple[str, str]:
    url = f"{CALENDAR_GRID_URL}?{urlencode(_rpc_params(html_lang, currency))}"
    return url, _rpc_body(build_calendar_inner(query, start, end))


def build_explore_inner(
    origin: str,
    departure_date: date,
    *,
    adults: int = 1,
    cabin: FlightCabin = "economy",
) -> list[Any]:
    constraints = _constraints_from_segments(
        [
            _shopping_segment(
                origin=origin,
                destination=None,
                departure_date=departure_date,
                max_stops=1,
            )
        ],
        adults=adults,
        cabin=cabin,
    )
    return [None, None, None, constraints]


def build_explore_request(
    origin: str,
    departure_date: date,
    *,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    html_lang: str = "en",
    currency: str = "EUR",
) -> tuple[str, str]:
    url = f"{EXPLORE_DESTINATIONS_URL}?{urlencode(_rpc_params(html_lang, currency))}"
    return url, _rpc_body(build_explore_inner(origin, departure_date, adults=adults, cabin=cabin))


SHOPPING_POST_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Same-Domain": "1",
    "Origin": "https://www.google.com",
    "Referer": "https://www.google.com/travel/flights",
}


def parse_shopping_body(text: str) -> tuple[RawFlightCard, ...]:
    if _is_shopping_rejected(text):
        raise ShoppingRejected(
            "Google Flights rejected this route or date (unknown airport or invalid query)."
        )
    payload = _first_wrb_data(text)
    if payload is None:
        raise CompactParseMiss("no wrb.fr shopping payload")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CompactParseMiss("wrb.fr data is not JSON") from exc
    if not isinstance(data, list):
        raise CompactParseMiss("wrb.fr data is not a list")
    items = _collect_itineraries(data)
    if not items:
        if _has_itinerary_slots(data):
            raise EmptyShoppingResults()
        raise CompactParseMiss("no itinerary groups in shopping payload")
    cards = tuple(card for item in items if (card := _itinerary_to_card(item)) is not None)
    if not cards:
        raise CompactParseMiss("itineraries had no priced offers")
    return cards


def _first_wrb_data(text: str) -> Optional[str]:
    body = text.lstrip()
    if body.startswith(_ANTI_XSSI):
        body = body[len(_ANTI_XSSI) :].lstrip()
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx] in " \t\r\n":
            idx += 1
        if idx >= len(body):
            break
        if body[idx].isdigit():
            newline = body.find("\n", idx)
            if newline < 0:
                break
            length_text = body[idx:newline].strip()
            if length_text.isdigit():
                size = int(length_text)
                chunk = body[newline + 1 : newline + 1 + size]
                try:
                    obj, _ = decoder.raw_decode(chunk)
                except json.JSONDecodeError:
                    idx = newline + 1
                    continue
                found = _wrb_data_string(obj)
                if found is not None:
                    return found
                idx = newline + 1 + size
                continue
        try:
            obj, _ = decoder.raw_decode(body, idx)
        except json.JSONDecodeError:
            break
        return _wrb_data_string(obj)
    return None


def _wrb_data_string(obj: object) -> Optional[str]:
    if (
        isinstance(obj, list)
        and obj
        and isinstance(obj[0], list)
        and obj[0]
        and obj[0][0] == "wrb.fr"
    ):
        obj = obj[0]
    if isinstance(obj, list) and len(obj) >= 3 and obj[0] == "wrb.fr" and isinstance(obj[2], str):
        return obj[2]
    return None


def _has_itinerary_slots(data: list[Any]) -> bool:
    return len(data) > 3 and isinstance(data[2], list)


def _collect_itineraries(data: list[Any]) -> list[Any]:
    found: list[Any] = []
    seen: set[int] = set()
    for index in (2, 3):
        if index >= len(data):
            continue
        for item in _iter_group(data[index]):
            marker = id(item)
            if marker in seen:
                continue
            seen.add(marker)
            found.append(item)
    return found


def _iter_group(group: object) -> list[Any]:
    if not isinstance(group, list) or not group:
        return []
    first = group[0]
    if isinstance(first, list) and first:
        nested = [item for item in first if _looks_like_itinerary(item)]
        if nested:
            return nested
    return [item for item in group if _looks_like_itinerary(item)]


def _looks_like_flight(flight: object) -> bool:
    if not isinstance(flight, list) or len(flight) < 10:
        return False
    airlines = flight[1]
    if not isinstance(airlines, list) or not airlines or not isinstance(airlines[0], str):
        return False
    if _flight_departure(flight) is None:
        return False
    return isinstance(flight[9], int)


def _collect_flights(obj: object, *, depth: int = 0, seen: Optional[set[int]] = None) -> list[Any]:
    if seen is None:
        seen = set()
    if _looks_like_flight(obj):
        marker = id(obj)
        if marker in seen:
            return []
        seen.add(marker)
        return [obj]
    if depth >= 3 or not isinstance(obj, list):
        return []
    found: list[Any] = []
    for item in obj:
        found.extend(_collect_flights(item, depth=depth + 1, seen=seen))
    return found


def _journey_flights(head: object) -> Optional[list[Any]]:
    flights = _collect_flights(head)
    if len(flights) >= 2:
        return flights
    return None


def _itinerary_journeys(item: list[Any]) -> list[Any]:
    head = item[0]
    journeys = _journey_flights(head)
    if journeys:
        return journeys
    collected = _collect_flights(item)
    if len(collected) >= 2:
        return collected
    if _looks_like_flight(head):
        return [head]
    return collected


def _looks_like_itinerary(item: object) -> bool:
    if not isinstance(item, list) or len(item) < 2:
        return False
    return bool(_itinerary_journeys(item))


def _itinerary_to_card(item: list[Any]) -> Optional[RawFlightCard]:
    journeys = _itinerary_journeys(item)
    if not journeys:
        return None
    flight = journeys[0]
    airlines = [name for name in flight[1] if isinstance(name, str)]
    price = _price_text(item[1] if len(item) > 1 else None)
    if price is None:
        return None
    layover_city, layover_hours = _layover_from_flight(flight)
    journey_legs = tuple(_flight_to_journey_leg(row) for row in journeys)
    checked_bags, carry_on = _bags_from_fare(item[1] if len(item) > 1 else None)
    return RawFlightCard(
        airline=", ".join(airlines) or None,
        departure=_flight_departure(flight),
        arrival=_flight_arrival(flight),
        duration=_format_duration(flight[9] if len(flight) > 9 else None),
        stops=_format_stops(flight[2]),
        price=price,
        layover_city=layover_city,
        layover_hours=layover_hours,
        flight_numbers=_flight_numbers(flight),
        airline_codes=_airline_codes(flight),
        booking_token=_booking_token(item[1] if len(item) > 1 else None),
        legs=journey_legs,
        checked_bags=checked_bags,
        carry_on=carry_on,
    )


def _flight_to_journey_leg(flight: list[Any]) -> RawJourneyLeg:
    layovers = _layovers_from_flight(flight)
    return RawJourneyLeg(
        departure=_flight_departure(flight),
        arrival=_flight_arrival(flight),
        duration=_format_duration(flight[9] if len(flight) > 9 else None),
        stops=_format_stops(flight[2]),
        segments=_segments_from_flight(flight),
        layovers=layovers,
    )


def _clock_from_slots(flight: list[Any], *indexes: int) -> Optional[str]:
    for index in indexes:
        if index >= len(flight):
            continue
        value = flight[index]
        if _is_year_ymd(value):
            continue
        clock = _format_clock(value)
        if clock is not None:
            return clock
    return None


def _flight_departure(flight: list[Any]) -> Optional[str]:
    return _clock_from_slots(flight, 5) or _clock_from_leg(
        flight[2] if len(flight) > 2 else None, 0, 8
    )


def _flight_arrival(flight: list[Any]) -> Optional[str]:
    return _clock_from_slots(flight, 8) or _clock_from_leg(
        flight[2] if len(flight) > 2 else None, -1, 10
    )


def _booking_token(block: object) -> Optional[str]:
    if not isinstance(block, list) or len(block) < 2:
        return None
    token = block[1]
    if isinstance(token, str) and token.strip():
        return token
    return None


def _bag_count(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _MAX_BAG_COUNT:
        return None
    return value


def _bag_pair(value: object) -> Optional[tuple[Optional[int], Optional[int]]]:
    if not isinstance(value, list) or not value:
        return None
    if len(value) == 1 and isinstance(value[0], list):
        return _bag_pair(value[0])
    checked = _bag_count(value[0])
    if len(value) == 1:
        if checked is None:
            return None
        return checked, None
    carry = _bag_count(value[1])
    if checked is None and carry is None:
        return None
    return checked, carry


def _bags_from_fare(block: object) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(block, list):
        return None, None
    # Fare is [[null, price], booking_token, [checked, carry_on], ...].
    # The pair is omitted when the compact body has no bag counts.
    for index in (2, 1):
        if index >= len(block):
            continue
        slot = block[index]
        if index == 1 and isinstance(slot, str):
            continue
        parsed = _bag_pair(slot)
        if parsed is not None:
            return parsed
    return None, None


def _price_text(block: object) -> Optional[str]:
    if not isinstance(block, list) or not block:
        return None
    first = block[0]
    if not isinstance(first, list) or len(first) < 2:
        return None
    amount = first[1]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if float(amount).is_integer():
        return f"€{int(amount)}"
    return f"€{amount}"


def _clock_from_leg(legs: object, index: int, field: int) -> Optional[str]:
    if not isinstance(legs, list) or not legs:
        return None
    try:
        leg = legs[index]
    except IndexError:
        return None
    if not isinstance(leg, list) or field >= len(leg):
        return None
    return _format_clock(leg[field])


def _format_clock(value: object) -> Optional[str]:
    if isinstance(value, str):
        return normalize_clock(value)
    parsed = _clock_hm(value)
    if parsed is None:
        return None
    hour, minute = parsed
    return f"{hour:02d}:{minute:02d}"


def _as_clock_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _layovers_from_flight(flight: list[Any]) -> tuple[RawLayover, ...]:
    block = flight[13] if len(flight) > 13 else None
    rows: list[RawLayover] = []
    if isinstance(block, list) and block:
        for entry in block:
            if not isinstance(entry, list) or not entry:
                continue
            minutes = _as_clock_int(entry[0])
            if minutes is None or minutes < 0:
                continue
            city: Optional[str] = None
            if len(entry) > 5 and isinstance(entry[5], str) and entry[5]:
                city = entry[5]
            elif len(entry) > 1 and isinstance(entry[1], str) and entry[1]:
                city = entry[1]
            rows.append(RawLayover(city=city, hours=minutes / 60.0))
    if rows:
        return tuple(rows)
    city, hours = _layover_from_legs(flight[2] if len(flight) > 2 else None)
    if city is None and hours is None:
        return ()
    return (RawLayover(city=city, hours=hours),)


def _layover_from_flight(flight: list[Any]) -> tuple[Optional[str], Optional[float]]:
    layovers = _layovers_from_flight(flight)
    if not layovers:
        return None, None
    if len(layovers) == 1:
        return layovers[0].city, layovers[0].hours
    longest = max(layovers, key=lambda row: row.hours or 0.0)
    return longest.city, longest.hours


def _is_year_ymd(value: object) -> bool:
    parsed = _ymd(value)
    return parsed is not None and parsed[0] >= 100


def _clock_hm(value: object) -> Optional[tuple[int, int]]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hour = _as_clock_int(value)
        if hour is None:
            return None
        return _normalize_hm(hour, 0)
    if not isinstance(value, list) or not value:
        return None
    if _is_year_ymd(value):
        if len(value) >= 5:
            hour = _as_clock_int(value[3])
            minute = _as_clock_int(value[4]) if len(value) > 4 else 0
            if hour is None:
                hour = 0
            if minute is None:
                minute = 0
            return _normalize_hm(hour, minute)
        return None
    if len(value) == 1 and isinstance(value[0], list):
        return _clock_hm(value[0])
    if value[0] is None and len(value) > 1 and isinstance(value[1], list):
        return _clock_hm(value[1])
    hour = _as_clock_int(value[0])
    minute = _as_clock_int(value[1]) if len(value) > 1 else 0
    # proto3 JSON omits hour 0, so 00:05 arrives as [null, 5].
    if hour is None:
        if len(value) < 2 or minute is None:
            return None
        hour = 0
    if minute is None:
        minute = 0
    suffix = value[2] if len(value) > 2 and isinstance(value[2], str) else None
    if suffix:
        clock = normalize_clock(f"{hour}:{minute:02d} {suffix}")
        if clock is None:
            return None
        parsed_hour, parsed_minute = clock.split(":", 1)
        return int(parsed_hour), int(parsed_minute)
    return _normalize_hm(hour, minute)


def _normalize_hm(hour: int, minute: int) -> Optional[tuple[int, int]]:
    if not (0 <= minute <= 59):
        return None
    if 0 <= hour <= 23:
        return hour, minute
    # Next-day arrivals sometimes use 24:05 rather than 00:05.
    if 24 <= hour <= 47:
        return hour % 24, minute
    return None


def _ymd(value: object) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, list) or len(value) < 3:
        return None
    year, month, day = value[0], value[1], value[2]
    if not isinstance(year, int) or not isinstance(month, int) or not isinstance(day, int):
        return None
    return year, month, day


def _layover_from_legs(legs: object) -> tuple[Optional[str], Optional[float]]:
    if not isinstance(legs, list) or len(legs) < 2:
        return None, None
    best_city: Optional[str] = None
    best_hours: Optional[float] = None
    for index in range(len(legs) - 1):
        inbound, outbound = legs[index], legs[index + 1]
        if not isinstance(inbound, list) or not isinstance(outbound, list):
            continue
        arrival = _clock_hm(inbound[10] if len(inbound) > 10 else None)
        departure = _clock_hm(outbound[8] if len(outbound) > 8 else None)
        arrival_date = _ymd(inbound[21] if len(inbound) > 21 else None)
        departure_date = _ymd(outbound[20] if len(outbound) > 20 else None)
        if arrival is None or departure is None or arrival_date is None or departure_date is None:
            continue
        start = datetime(*arrival_date, arrival[0], arrival[1])
        end = datetime(*departure_date, departure[0], departure[1])
        minutes = (end - start).total_seconds() / 60.0
        if minutes < 0:
            continue
        hours = minutes / 60.0
        city = outbound[3] if len(outbound) > 3 and isinstance(outbound[3], str) else None
        if best_hours is None or hours > best_hours:
            best_hours = hours
            best_city = city
    return best_city, best_hours


def _is_shopping_rejected(text: str) -> bool:
    return "travel.frontend.flights.ErrorResponse" in text


def _format_duration(value: object) -> Optional[str]:
    minutes = _as_clock_int(value)
    if minutes is None or minutes < 0:
        return None
    return _format_duration_minutes(minutes)


def _format_duration_minutes(minutes: int) -> str:
    hours, mins = divmod(max(0, minutes), 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    if hours:
        return f"{hours} hr"
    return f"{mins} min"


def _segments_from_flight(flight: list[Any]) -> tuple[RawSegment, ...]:
    legs = flight[2] if len(flight) > 2 else None
    if not isinstance(legs, list):
        return ()
    segments: list[RawSegment] = []
    for leg in legs:
        if not isinstance(leg, list):
            continue
        ident = _leg_ident(leg)
        airline = None
        if ident is not None and len(leg) > 22 and isinstance(leg[22], list) and len(leg[22]) > 3:
            name = leg[22][3]
            airline = name if isinstance(name, str) and name else ident[0]
        elif ident is not None:
            airline = ident[0]
        segments.append(
            RawSegment(
                origin=leg[3] if len(leg) > 3 and isinstance(leg[3], str) else None,
                destination=leg[6] if len(leg) > 6 and isinstance(leg[6], str) else None,
                departure=_format_clock(leg[8] if len(leg) > 8 else None),
                arrival=_format_clock(leg[10] if len(leg) > 10 else None),
                airline=airline,
                flight_number=None if ident is None else f"{ident[0]}{ident[1]}",
            )
        )
    return tuple(segments)


def parse_calendar_body(text: str) -> tuple[CompactCalendarDay, ...]:
    if _is_shopping_rejected(text):
        raise ShoppingRejected(
            "Google Flights rejected this route or date (unknown airport or invalid query)."
        )
    payload = _first_wrb_data(text)
    if payload is None:
        raise CompactParseMiss("no wrb.fr calendar payload")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CompactParseMiss("wrb.fr calendar data is not JSON") from exc
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        raise CompactParseMiss("calendar payload has no date rows")
    rows: list[CompactCalendarDay] = []
    for item in data[1]:
        parsed = _calendar_row(item)
        if parsed is not None:
            rows.append(parsed)
    if not rows:
        raise CompactParseMiss("calendar rows had no readable dates")
    return tuple(rows)


def parse_explore_body(text: str) -> tuple[CompactExplorePlace, ...]:
    if _is_shopping_rejected(text):
        raise ShoppingRejected(
            "Google Flights rejected this origin or date (unknown airport or invalid query)."
        )
    payload = _first_wrb_data(text)
    if payload is None:
        raise CompactParseMiss("no wrb.fr explore payload")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CompactParseMiss("wrb.fr explore data is not JSON") from exc
    if not isinstance(data, list) or len(data) < 4 or not isinstance(data[3], list):
        raise CompactParseMiss("explore payload has no destination group")
    group = data[3][0] if data[3] else None
    if not isinstance(group, list):
        raise CompactParseMiss("explore destination group is missing")
    places: list[CompactExplorePlace] = []
    seen: set[str] = set()
    for item in group:
        place = _explore_place(item)
        if place is None or place.iata in seen:
            continue
        seen.add(place.iata)
        places.append(place)
    if not places:
        raise CompactParseMiss("explore group had no destination codes")
    return tuple(places)


def _calendar_row(item: object) -> Optional[CompactCalendarDay]:
    if not isinstance(item, list) or not item or not isinstance(item[0], str):
        return None
    try:
        day = date.fromisoformat(item[0])
    except ValueError:
        return None
    price: Optional[float] = None
    if len(item) > 2 and isinstance(item[2], list) and item[2]:
        block = item[2][0]
        if isinstance(block, list) and len(block) > 1:
            amount = block[1]
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount > 0:
                price = float(amount)
    return CompactCalendarDay(departure_date=day, price_eur=price)


def _explore_place(item: object) -> Optional[CompactExplorePlace]:
    if not isinstance(item, list) or len(item) < 16:
        return None
    city = item[2] if isinstance(item[2], str) and item[2] else None
    country = item[4] if isinstance(item[4], str) and item[4] else None
    iata = item[15] if isinstance(item[15], str) else None
    if city is None or iata is None or len(iata) != 3 or not iata.isalpha():
        return None
    return CompactExplorePlace(iata=iata.upper(), city=city, country=country)


def _flight_numbers(flight: list[Any]) -> Optional[tuple[str, ...]]:
    legs = flight[2] if len(flight) > 2 else None
    if not isinstance(legs, list):
        return None
    numbers: list[str] = []
    for leg in legs:
        ident = _leg_ident(leg)
        if ident is None:
            continue
        code, number = ident
        numbers.append(f"{code}{number}")
    return tuple(numbers) or None


def _airline_codes(flight: list[Any]) -> Optional[tuple[str, ...]]:
    codes: list[str] = []
    head = flight[0] if flight else None
    if isinstance(head, str) and 2 <= len(head) <= 3 and head.isalpha():
        codes.append(head.upper())
    legs = flight[2] if len(flight) > 2 else None
    if isinstance(legs, list):
        for leg in legs:
            ident = _leg_ident(leg)
            if ident is not None:
                codes.append(ident[0])
    unique: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        unique.append(code)
    return tuple(unique) or None


def _leg_ident(leg: object) -> Optional[tuple[str, str]]:
    if not isinstance(leg, list) or len(leg) <= 22:
        return None
    ident = leg[22]
    if not isinstance(ident, list) or len(ident) < 2:
        return None
    code, number = ident[0], ident[1]
    if not isinstance(code, str) or not isinstance(number, str):
        return None
    if not code.isalpha() or not number:
        return None
    return code.upper(), number


def _format_stops(legs: object) -> Optional[str]:
    if not isinstance(legs, list) or not legs or not all(isinstance(leg, list) for leg in legs):
        return None
    count = len(legs) - 1
    if count <= 0:
        return "Nonstop"
    if count == 1:
        return "1 stop"
    return f"{count} stops"
