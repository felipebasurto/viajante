"""Self-transfer: two owned one-way legs through each named via, on separate tickets.

The join measures the connection in UTC and labels it against caller-named
bounds. It never claims protection, never invents a minimum connection, and
never converts currency.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Callable, Optional, Sequence

from viajante.flights import DEFAULT_TOP, parse_code_list, search_flights
from viajante.models import (
    FETCH_LANGUAGE,
    FlightCabin,
    FlightOffer,
    FlightQuery,
    QuerySuccess,
    SelfTransferPairing,
    SelfTransferReport,
    SelfTransferStatus,
)
from viajante.temporal import segment_instant

MAX_VIAS = 5


def validate_connection_bounds(min_hours: Optional[float], max_hours: Optional[float]) -> None:
    for role, value in (("min_connection_hours", min_hours), ("max_connection_hours", max_hours)):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{role} must be a finite non-negative number of hours")
    if min_hours is not None and max_hours is not None and min_hours > max_hours:
        raise ValueError("min_connection_hours must not exceed max_connection_hours")


def connection_status(
    minutes: Optional[int],
    *,
    min_hours: Optional[float] = None,
    max_hours: Optional[float] = None,
) -> SelfTransferStatus:
    if minutes is None:
        return "unknown"
    # Departing at or before the arrival is never a connection, bound or not.
    if minutes <= 0 or (min_hours is not None and minutes < min_hours * 60):
        return "too_short"
    if max_hours is not None and minutes > max_hours * 60:
        return "too_long"
    return "ok"


def _connection(first: FlightOffer, second: FlightOffer) -> tuple[Optional[int], Optional[bool]]:
    arriving = first.legs[-1].segments[-1] if first.legs[-1].segments else None
    leaving = second.legs[0].segments[0] if second.legs[0].segments else None
    if arriving is None or leaving is None:
        return None, None
    same_airport = None
    if arriving.destination and leaving.origin:
        same_airport = arriving.destination == leaving.origin
    landed = segment_instant(arriving.to_dict(), "arrival")
    departs = segment_instant(leaving.to_dict(), "departure")
    if landed is None or departs is None:
        return None, same_airport
    return int((departs - landed).total_seconds() // 60), same_airport


def _shared_currency(first: FlightOffer, second: FlightOffer) -> Optional[str]:
    if first.evidence is None or second.evidence is None:
        return None
    if first.evidence.currency != second.evidence.currency:
        return None
    return first.evidence.currency


def _order(pairing: SelfTransferPairing) -> tuple:
    out_of_bounds = pairing.status in ("too_short", "too_long")
    price = pairing.total_price
    minutes = pairing.connection_minutes
    return (out_of_bounds, price is None, price or 0.0, minutes is None, minutes or 0)


def join_self_transfer(
    via: str,
    first_offers: Sequence[FlightOffer],
    second_offers: Sequence[FlightOffer],
    *,
    min_connection_hours: Optional[float] = None,
    max_connection_hours: Optional[float] = None,
) -> tuple[SelfTransferPairing, ...]:
    """Every first x second pairing, out-of-bounds last, then by total price and margin."""
    validate_connection_bounds(min_connection_hours, max_connection_hours)
    pairings = []
    for first in first_offers:
        for second in second_offers:
            minutes, same_airport = _connection(first, second)
            currency = _shared_currency(first, second)
            pairings.append(
                SelfTransferPairing(
                    via=via,
                    first=first,
                    second=second,
                    connection_minutes=minutes,
                    same_airport=same_airport,
                    status=connection_status(
                        minutes, min_hours=min_connection_hours, max_hours=max_connection_hours
                    ),
                    total_price=first.price + second.price if currency else None,
                    currency=currency,
                )
            )
    return tuple(sorted(pairings, key=_order))


def search_self_transfer(
    origin: str,
    vias: Sequence[str],
    destination: str,
    departure_date: date,
    *,
    second_date: Optional[date] = None,
    min_connection_hours: Optional[float] = None,
    max_connection_hours: Optional[float] = None,
    top: int = DEFAULT_TOP,
    max_stops: int = 1,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> SelfTransferReport:
    """Shop origin-via and via-destination for every named via in one sweep, then join."""
    validate_connection_bounds(min_connection_hours, max_connection_hours)
    if isinstance(vias, str):
        raise TypeError("vias must be a sequence of IATA codes, not one string")
    named = parse_code_list(vias, role="via")
    if not named:
        raise ValueError("name at least one via airport")
    if len(named) > MAX_VIAS:
        raise ValueError(f"at most {MAX_VIAS} via airports per self-transfer search")
    second_date = second_date or departure_date
    if second_date < departure_date:
        raise ValueError("second_date must be on or after the departure date")

    def leg(start: str, end: str, day: date) -> FlightQuery:
        return FlightQuery(
            start,
            end,
            day,
            max_stops=max_stops,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            bags=bags,
            carry_on=carry_on,
        )

    queries: list[FlightQuery] = []
    for via in named:
        first_query = leg(origin, via, departure_date)
        second_query = leg(via, destination, second_date)
        if via in (first_query.origin, second_query.destination):
            raise ValueError("via must differ from origin and destination")
        queries += (first_query, second_query)
    # Only the sweep RPC owns segment clocks and arrival dates; detail cards cannot
    # measure a connection.
    flights = search_flights(
        tuple(queries),
        top=top,
        progress=progress,
        fetch="sweep",
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
        currency=currency,
        country=country,
        proxy=proxy,
    )
    pairings: list[SelfTransferPairing] = []
    results = flights.queries
    for via, first_leg, second_leg in zip(named, results[::2], results[1::2], strict=True):
        if isinstance(first_leg, QuerySuccess) and isinstance(second_leg, QuerySuccess):
            pairings += join_self_transfer(
                via,
                first_leg.offers,
                second_leg.offers,
                min_connection_hours=min_connection_hours,
                max_connection_hours=max_connection_hours,
            )
    pairings.sort(key=_order)
    return SelfTransferReport(
        searched_at=flights.searched_at,
        origin=queries[0].origin,
        vias=named,
        destination=queries[1].destination,
        flights=flights,
        pairings=tuple(pairings[:top]),
        eligible_pairings=len(pairings),
        currency=flights.currency,
        min_connection_hours=min_connection_hours,
        max_connection_hours=max_connection_hours,
        locale=FETCH_LANGUAGE,
    )
