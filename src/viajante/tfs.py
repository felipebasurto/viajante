"""Encode a Google Flights tfs query from a Trip."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from typing import Optional

from viajante.carriers import tfs_carrier_codes
from viajante.models import (
    FlightCabin,
    FlightLeg,
    FlightQuery,
    MultiCity,
    RawJourneyLeg,
    RawSegment,
    RoundTrip,
    Trip,
)

_VARINT = 0
_LEN = 2

_INFO_DATA = 3
_INFO_PASSENGERS = 8
_INFO_SEAT = 9
_INFO_BAGGAGE = 13
_INFO_TRIP = 19

# BaggageFilter fields, matching the provider's own encoder byte-for-byte:
# field 2 is the carry-on count. Field 3 produced no filter echo in probes, so
# only the observed zero is ever written for it.
_BAGGAGE_CARRY_ON = 2
_BAGGAGE_CHECKED = 3

_FLIGHT_DATE = 2
_FLIGHT_MAX_STOPS = 5
_FLIGHT_INCLUDE_CARRIERS = 6
_FLIGHT_EXCLUDE_CARRIERS = 7
_FLIGHT_FROM = 13
_FLIGHT_TO = 14

_AIRPORT_CODE = 2

CABIN_SEAT: Mapping[FlightCabin, int] = {
    "economy": 1,
    "premium-economy": 2,
    "business": 3,
    "first": 4,
}
TRIP_ONE_WAY = 2
TRIP_ROUND_TRIP = 1
TRIP_MULTI_CITY = 3
_PASSENGER_ADULT = 1
_PASSENGER_CHILD = 2
_PASSENGER_INFANT_IN_SEAT = 3
_PASSENGER_INFANT_ON_LAP = 4


def encode_tfs(trip: Trip) -> str:
    """Encode a Google Flights `tfs` query parameter from owned trip fields."""
    include, exclude = tfs_carrier_codes(trip)
    return _encode_legs(
        trip.legs,
        adults=trip.adults,
        children=trip.children,
        infants_in_seat=trip.infants_in_seat,
        infants_on_lap=trip.infants_on_lap,
        cabin=trip.cabin,
        carry_on=trip.carry_on,
        trip_kind=trip_kind_code(trip),
        include_carriers=include,
        exclude_carriers=exclude,
    )


def encode_tfs_selected_outbound(trip: RoundTrip, outbound: RawJourneyLeg) -> str:
    """Encode round-trip TFS with owned physical segments selected outbound."""
    if not isinstance(trip, RoundTrip):
        raise ValueError("selected outbound TFS requires a round-trip query")
    segments = outbound.segments
    if not segments:
        raise ValueError("selected outbound has no physical segments")
    selected: list[bytes] = []
    previous_destination: str | None = None
    previous_date = None
    for index, segment in enumerate(segments):
        if not segment.origin or not segment.destination or segment.departure_date is None:
            raise ValueError("selected outbound segment lacks route or departure date")
        if index == 0 and segment.origin != trip.legs[0].origin:
            raise ValueError("selected outbound does not start at the requested origin")
        if index == 0 and segment.departure_date != trip.legs[0].departure_date:
            raise ValueError("selected outbound first segment date differs from the query")
        if previous_destination is not None and segment.origin != previous_destination:
            raise ValueError("selected outbound physical segments are not contiguous")
        if previous_date is not None and segment.departure_date < previous_date:
            raise ValueError("selected outbound segment dates go backwards")
        previous_destination = segment.destination
        previous_date = segment.departure_date
        selected.append(_len_delim(4, _selected_flight_data(segment)))
    if previous_destination != trip.legs[0].destination:
        raise ValueError("selected outbound does not reach the requested destination")

    include, exclude = tfs_carrier_codes(trip)
    outbound_data = _flight_data(trip.legs[0], include=include, exclude=exclude) + b"".join(
        selected
    )
    return _encode_route_messages(
        [
            outbound_data,
            _flight_data(trip.legs[1], include=include, exclude=exclude),
        ],
        adults=trip.adults,
        children=trip.children,
        infants_in_seat=trip.infants_in_seat,
        infants_on_lap=trip.infants_on_lap,
        cabin=trip.cabin,
        carry_on=trip.carry_on,
        trip_kind=trip_kind_code(trip),
    )


def trip_kind_code(trip: Trip) -> int:
    if isinstance(trip, RoundTrip):
        return TRIP_ROUND_TRIP
    if isinstance(trip, MultiCity):
        return TRIP_MULTI_CITY
    if isinstance(trip, FlightQuery):
        return TRIP_ONE_WAY
    raise ValueError(f"cannot encode tfs for {type(trip).__name__}")


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        bits = n & 0x7F
        n >>= 7
        if n:
            out.append(bits | 0x80)
        else:
            out.append(bits)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _len_delim(field: int, payload: bytes) -> bytes:
    return _key(field, _LEN) + _varint(len(payload)) + payload


def _string(field: int, value: str) -> bytes:
    return _len_delim(field, value.encode("ascii"))


def _varint_field(field: int, value: int) -> bytes:
    return _key(field, _VARINT) + _varint(value)


def _packed_enums(field: int, values: Sequence[int]) -> bytes:
    payload = b"".join(_varint(value) for value in values)
    return _len_delim(field, payload)


def _airport(code: str) -> bytes:
    return _string(_AIRPORT_CODE, code)


def _flight_data(
    leg: FlightLeg,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> bytes:
    return (
        _string(_FLIGHT_DATE, leg.departure_date.isoformat())
        + _varint_field(_FLIGHT_MAX_STOPS, leg.max_stops)
        + b"".join(_string(_FLIGHT_INCLUDE_CARRIERS, code) for code in include)
        + b"".join(_string(_FLIGHT_EXCLUDE_CARRIERS, code) for code in exclude)
        + _len_delim(_FLIGHT_FROM, _airport(leg.origin))
        + _len_delim(_FLIGHT_TO, _airport(leg.destination))
    )


def _selected_flight_data(segment: RawSegment) -> bytes:
    carrier = segment.carrier
    identity = segment.flight_number
    if not carrier or not identity or not identity.startswith(carrier):
        raise ValueError("selected outbound segment lacks a carrier and flight number")
    number = identity[len(carrier) :]
    if not number:
        raise ValueError("selected outbound segment lacks a bare flight number")
    return (
        _string(1, segment.origin or "")
        + _string(2, segment.departure_date.isoformat())  # validated by caller
        + _string(3, segment.destination or "")
        + _string(5, carrier)
        + _string(6, number)
    )


def _encode_legs(
    legs: Sequence[FlightLeg],
    *,
    adults: int,
    cabin: FlightCabin,
    trip_kind: int,
    carry_on: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    include_carriers: Sequence[str] = (),
    exclude_carriers: Sequence[str] = (),
) -> str:
    return _encode_route_messages(
        [_flight_data(leg, include=include_carriers, exclude=exclude_carriers) for leg in legs],
        adults=adults,
        cabin=cabin,
        trip_kind=trip_kind,
        carry_on=carry_on,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
    )


def _encode_route_messages(
    route_messages: Sequence[bytes],
    *,
    adults: int,
    cabin: FlightCabin,
    trip_kind: int,
    carry_on: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> str:
    passengers = (
        (_PASSENGER_ADULT,) * adults
        + (_PASSENGER_CHILD,) * children
        + (_PASSENGER_INFANT_IN_SEAT,) * infants_in_seat
        + (_PASSENGER_INFANT_ON_LAP,) * infants_on_lap
    )
    flights = b"".join(_len_delim(_INFO_DATA, route) for route in route_messages)
    payload = (
        flights
        + _packed_enums(_INFO_PASSENGERS, passengers)
        + _varint_field(_INFO_SEAT, CABIN_SEAT[cabin])
        + _baggage_filter(carry_on)
        + _varint_field(_INFO_TRIP, trip_kind)
    )
    return base64.b64encode(payload).decode("ascii")


def _baggage_filter(carry_on: Optional[int]) -> bytes:
    if not carry_on:
        return b""
    inner = _varint_field(_BAGGAGE_CARRY_ON, carry_on) + _varint_field(_BAGGAGE_CHECKED, 0)
    return _len_delim(_INFO_BAGGAGE, inner)
