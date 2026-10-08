"""Owned Google Flights compact-data decoder: shopping, explore catalog, and wrb.fr parsing."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional, TypeVar
from urllib.parse import parse_qs

from viajante.airports import airport_geo
from viajante.models import RawJourneyLeg, RawLayover, RawSegment
from viajante.parsers import normalize_clock

_ANTI_XSSI = ")]}'"
_T = TypeVar("_T")
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
    price: Optional[float]
    return_date: Optional[date] = None


@dataclass(frozen=True)
class CompactExplorePlace:
    iata: str
    city: str
    country: Optional[str]


def _wrb_json(text: str, *, kind: str) -> Any:
    if _is_shopping_rejected(text):
        raise ShoppingRejected(
            "Google Flights rejected this query; the provider did not identify the cause."
        )
    payload = first_wrb_data(text)
    if payload is None:
        raise CompactParseMiss(f"no wrb.fr {kind} payload")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CompactParseMiss(f"wrb.fr {kind} data is not JSON") from exc


def parse_shopping_body(text: str, *, currency: str) -> tuple[RawFlightCard, ...]:
    data = _wrb_json(text, kind="shopping")
    if not isinstance(data, list):
        raise CompactParseMiss("wrb.fr data is not a list")
    return parse_shopping_data(data, currency=currency)


def parse_shopping_data(data: object, *, currency: str) -> tuple[RawFlightCard, ...]:
    """Parse Google Flights' decoded shopping-data array from any owned transport."""
    if not isinstance(data, list):
        raise CompactParseMiss("shopping data is not a list")
    items = _collect_itineraries(data)
    if not items:
        if _has_itinerary_slots(data):
            raise EmptyShoppingResults()
        raise CompactParseMiss("no itinerary groups in shopping payload")
    cards = tuple(
        card for item in items if (card := _itinerary_to_card(item, currency=currency)) is not None
    )
    if not cards:
        raise CompactParseMiss("itineraries had no priced offers")
    return cards


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


def _wrb_chunk_objects(text: str):
    """Yield each decoded JSON chunk of an ``)]}'``-framed wrb response body."""
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
                yield obj
                idx = newline + 1 + size
                continue
        try:
            obj, _ = decoder.raw_decode(body, idx)
        except json.JSONDecodeError:
            break
        yield obj
        return


def first_wrb_data(
    text: str, extract: Callable[[object], Optional[_T]] = _wrb_data_string
) -> Optional[_T]:
    """First wrb.fr envelope ``extract`` accepts, across length-prefixed or bare JSON chunks."""
    for obj in _wrb_chunk_objects(text):
        found = extract(obj)
        if found is not None:
            return found
    return None


def wrb_data_strings(text: str) -> tuple[str, ...]:
    """Every string payload on every ``wrb.fr`` row in a response body, in order."""
    found: list[str] = []
    for obj in _wrb_chunk_objects(text):
        rows = (
            obj
            if (
                isinstance(obj, list)
                and obj
                and isinstance(obj[0], list)
                and obj[0]
                and obj[0][0] == "wrb.fr"
            )
            else [obj]
        )
        for row in rows:
            data = _wrb_data_string(row)
            if data is not None:
                found.append(data)
    return tuple(found)


def _wrb_error_status(obj: object) -> Optional[int]:
    if isinstance(obj, list) and obj and isinstance(obj[0], list):
        statuses = [_wrb_error_status(row) for row in obj]
        return 13 if 13 in statuses else next((s for s in statuses if s is not None), None)
    if not (isinstance(obj, list) and len(obj) >= 6 and obj[0] == "wrb.fr" and obj[2] is None):
        return None
    status = obj[5]
    if isinstance(status, list) and status and isinstance(status[0], int):
        return status[0]
    return None


def rpc_error_status(text: str) -> Optional[int]:
    """Status code of a data-less wrb.fr error envelope (e.g. 13), else None."""
    if not text.lstrip().startswith(_ANTI_XSSI) or _is_shopping_rejected(text):
        return None
    return raw_rpc_error_status(text)


def raw_rpc_error_status(text: str) -> Optional[int]:
    """Return any data-less RPC status, including status 13 in ErrorResponse bodies.

    ``rpc_error_status`` keeps its historical shopping-rejection precedence; callers
    that need transport diagnostics can inspect this raw value separately.
    """
    if not text.lstrip().startswith(_ANTI_XSSI):
        return None
    statuses = [_wrb_error_status(obj) for obj in _wrb_chunk_objects(text)]
    return 13 if 13 in statuses else next((s for s in statuses if s is not None), None)


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


def _itinerary_to_card(item: list[Any], *, currency: str) -> Optional[RawFlightCard]:
    journeys = _itinerary_journeys(item)
    if not journeys:
        return None
    flight = journeys[0]
    airlines = [name for name in flight[1] if isinstance(name, str)]
    price = _price_text(item[1] if len(item) > 1 else None, currency=currency)
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


def _price_text(block: object, *, currency: str) -> Optional[str]:
    if not isinstance(block, list) or not block:
        return None
    first = block[0]
    if not isinstance(first, list) or len(first) < 2:
        return None
    amount = first[1]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if float(amount).is_integer():
        amount_text = str(int(amount))
    else:
        amount_text = str(amount)
    code = currency.strip().upper()
    if code == "EUR":
        return f"€{amount_text}"
    return f"{amount_text} {code}"


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


def _leg_date(value: object) -> Optional[date]:
    parsed = _ymd(value)
    if parsed is None or parsed[0] < 100:
        return None
    try:
        return date(*parsed)
    except ValueError:
        return None


def _airport_timezone(value: object) -> Optional[str]:
    geo = airport_geo(value) if isinstance(value, str) else None
    return geo[0] if geo else None


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
                departure_date=_leg_date(leg[20] if len(leg) > 20 else None),
                carrier=None if ident is None else ident[0],
                arrival_date=_leg_date(leg[21] if len(leg) > 21 else None),
                departure_timezone=_airport_timezone(leg[3] if len(leg) > 3 else None),
                arrival_timezone=_airport_timezone(leg[6] if len(leg) > 6 else None),
            )
        )
    return tuple(segments)


@dataclass(frozen=True)
class ExploreCatalogPage:
    """Owned evidence parsed from the page-issued GetExploreDestinations body.

    ``places`` are priced rows joined to city cards; ``origin_echo`` is the
    provider-stamped origin code; ``currencies`` are the stamps inside priced
    offer tokens. ``unreadable_priced`` counts priced rows whose token could
    not prove the requested origin.
    """

    places: tuple[CompactExplorePlace, ...]
    origin_echo: Optional[str]
    currencies: frozenset[str]
    cards: int
    priced: int
    unreadable_priced: int


@dataclass(frozen=True)
class ExploreRequestEcho:
    """What the page's own GetExploreDestinations request says it sent."""

    origins: frozenset[str]
    dates: frozenset[str]
    cabin: object
    occupancy: object


_EXPLORE_TOKEN_PAIR = re.compile(rb"([A-Z]{3})-([A-Z]{3}):([0-9]{4}-[0-9]{2}-[0-9]{2})")
_EXPLORE_TOKEN_CURRENCY = re.compile(rb"\x1a\x03([A-Z]{3})")


def explore_request_constraints(post_data: Optional[str]) -> Optional[list[Any]]:
    """Decode a page-issued GetExploreDestinations ``f.req`` into the constraints list."""
    if not post_data:
        return None
    try:
        inner = json.loads(json.loads(parse_qs(post_data)["f.req"][0])[1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    constraints = inner[3] if isinstance(inner, list) and len(inner) > 3 else None
    return constraints if isinstance(constraints, list) else None


def explore_request_echo(constraints: Optional[list[Any]]) -> Optional[ExploreRequestEcho]:
    """Extract origin codes, journey dates, cabin, and occupancy the request carried."""
    if not isinstance(constraints, list) or len(constraints) <= 13:
        return None
    journeys = constraints[13]
    if not isinstance(journeys, list):
        return None
    origins: set[str] = set()
    dates: set[str] = set()
    for journey in journeys:
        if not isinstance(journey, list):
            continue
        try:
            entity = journey[0][0][0]
        except (IndexError, TypeError):
            entity = None
        if isinstance(entity, list) and entity and isinstance(entity[0], str):
            origins.add(entity[0])
        if len(journey) > 6 and isinstance(journey[6], str):
            dates.add(journey[6])
    return ExploreRequestEcho(
        origins=frozenset(origins),
        dates=frozenset(dates),
        cabin=constraints[5] if len(constraints) > 5 else None,
        occupancy=constraints[6],
    )


def _explore_token_fields(token: object) -> Optional[tuple[str, str, Optional[str]]]:
    """Decode an offer token's ``ORIGIN-DEST`` pair and quote-currency stamp."""
    if not isinstance(token, str) or not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        return None
    pair = _EXPLORE_TOKEN_PAIR.search(raw)
    if pair is None:
        return None
    currency = _EXPLORE_TOKEN_CURRENCY.search(raw)
    return (
        pair.group(1).decode("ascii"),
        pair.group(2).decode("ascii"),
        currency.group(1).decode("ascii") if currency else None,
    )


def _explore_priced_entries(payload: object) -> list[Any]:
    if (
        isinstance(payload, list)
        and len(payload) > 4
        and isinstance(payload[4], list)
        and payload[4]
        and isinstance(payload[4][0], list)
    ):
        return payload[4][0]
    return []


def parse_explore_catalog(text: str, *, origins: frozenset[str]) -> ExploreCatalogPage:
    """Decode the page-issued Explore catalog (every ``wrb.fr`` chunk, in order).

    The first payload carries city cards keyed by place id and the origin echo;
    later payloads carry priced flight rows whose offer tokens stamp the
    ``ORIGIN-DEST`` pair and quote currency. A row joins only when its token
    proves it was priced from an accepted origin code.
    """
    payloads: list[Any] = []
    for raw in wrb_data_strings(text):
        try:
            payloads.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    if not payloads:
        raise CompactParseMiss("explore catalog has no wrb.fr payload")
    main = payloads[0]
    if (
        not isinstance(main, list)
        or len(main) <= 6
        or not isinstance(main[3], list)
        or not main[3]
        or not isinstance(main[3][0], list)
    ):
        raise CompactParseMiss("explore catalog is missing destination cards")
    cards: dict[str, tuple[str, Optional[str]]] = {}
    for row in main[3][0]:
        if (
            isinstance(row, list)
            and len(row) > 4
            and isinstance(row[0], str)
            and isinstance(row[2], str)
            and row[2]
        ):
            cards[row[0]] = (row[2], row[4] if isinstance(row[4], str) else None)
    origin_echo: Optional[str] = None
    echo = main[6]
    if isinstance(echo, list) and echo and isinstance(echo[0], list) and len(echo[0]) > 3:
        code = echo[0][3]
        if isinstance(code, str) and len(code) == 3 and code.isalpha():
            origin_echo = code.upper()

    places: list[CompactExplorePlace] = []
    seen: set[str] = set()
    currencies: set[str] = set()
    priced = 0
    unreadable = 0
    for payload in payloads[1:]:
        for entry in _explore_priced_entries(payload):
            if not isinstance(entry, list) or len(entry) <= 6:
                continue
            priced += 1
            flight = entry[6]
            iata = flight[5] if isinstance(flight, list) and len(flight) > 5 else None
            if not (isinstance(iata, str) and len(iata) == 3 and iata.isalpha()):
                unreadable += 1
                continue
            iata = iata.upper()
            price = entry[1]
            token = price[1] if isinstance(price, list) and len(price) > 1 else None
            fields = _explore_token_fields(token)
            if fields is None:
                unreadable += 1
                continue
            token_origin, token_dest, currency_tag = fields
            if currency_tag:
                currencies.add(currency_tag)
            if token_origin not in origins or token_dest != iata:
                unreadable += 1
                continue
            card = cards.get(entry[0])
            if card is None or iata in seen:
                continue
            seen.add(iata)
            places.append(CompactExplorePlace(iata=iata, city=card[0], country=card[1]))
    return ExploreCatalogPage(
        places=tuple(places),
        origin_echo=origin_echo,
        currencies=frozenset(currencies),
        cards=len(cards),
        priced=priced,
        unreadable_priced=unreadable,
    )


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
            # leg[15] rows are codeshare marketing idents [code, number, ?, name];
            # the provider's airline filter qualifies a card on any of them.
            codeshares = leg[15] if isinstance(leg, list) and len(leg) > 15 else None
            if isinstance(codeshares, list):
                for entry in codeshares:
                    if isinstance(entry, list) and entry:
                        code = _carrier_code(entry[0])
                        if code is not None:
                            codes.append(code)
    unique: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        unique.append(code)
    return tuple(unique) or None


def _carrier_code(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    if not 2 <= len(code) <= 3 or not code.isalnum() or not any(char.isalpha() for char in code):
        return None
    return code


def _flight_number_text(value: object) -> Optional[str]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    if not text or not text[0].isdigit():
        return None
    if not text.isdigit() and not (text[:-1].isdigit() and text[-1].isalpha()):
        return None
    return text


def _leg_ident(leg: object) -> Optional[tuple[str, str]]:
    if not isinstance(leg, list) or len(leg) <= 22:
        return None
    ident = leg[22]
    if not isinstance(ident, list) or len(ident) < 2:
        return None
    code = _carrier_code(ident[0])
    number = _flight_number_text(ident[1])
    if code is None or number is None:
        return None
    return code, number


def _format_stops(legs: object) -> Optional[str]:
    if not isinstance(legs, list) or not legs or not all(isinstance(leg, list) for leg in legs):
        return None
    count = len(legs) - 1
    if count <= 0:
        return "Nonstop"
    if count == 1:
        return "1 stop"
    return f"{count} stops"
