"""Owned trip total: flight fare plus hotel stay when dates overlap.

Does not fetch by itself except `search_trip`, which runs the existing
flight then hotel loops sequentially. Never invents a fare or a stay.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple

from viajante.flights import (
    DEFAULT_BAGGAGE_BUFFER_EUR,
    DEFAULT_TOP,
    FlightSort,
    search_flights,
)
from viajante.hotels import HotelSourceName, search_hotels
from viajante.models import (
    FETCH_LANGUAGE,
    HotelQuery,
    HotelQuerySuccess,
    HotelSearchReport,
    MultiCity,
    QuerySuccess,
    RoundTrip,
    SearchReport,
    Trip,
    TripSearchReport,
    TripTotal,
)
from viajante.storage import write_json_atomic


def trip_date_span(query: Trip) -> tuple[date, date]:
    dates = [leg.departure_date for leg in query.legs]
    return min(dates), max(dates)


def dates_overlap(
    flight_start: date,
    flight_end: date,
    check_in: date,
    check_out: date,
) -> bool:
    return flight_start <= check_out and check_in <= flight_end


def stay_window_from_trips(trips: Sequence[Trip]) -> Optional[tuple[date, date]]:
    """Infer hotel check-in/out from owned flight dates. None if only one day."""
    dates = [leg.departure_date for trip in trips for leg in trip.legs]
    if not dates:
        return None
    start, end = min(dates), max(dates)
    if end <= start:
        return None
    return start, end


def _route_key(query: Trip) -> tuple[str, str, str]:
    if isinstance(query, MultiCity):
        return ("multi", query.legs[0].origin, query.legs[-1].destination)
    if isinstance(query, RoundTrip):
        return ("rt", query.origin, query.destination)
    return ("ow", query.origin, query.destination)


def _cheapest_fare(result: QuerySuccess) -> Optional[float]:
    if not result.offers:
        return None
    return min(offer.price_eur for offer in result.offers)


def _owned_flight_fare(
    flights: SearchReport,
    hotel_queries: Sequence[HotelQuery],
) -> Optional[float]:
    overlapping: list[QuerySuccess] = []
    for result in flights.queries:
        start, end = trip_date_span(result.query)
        if not any(
            dates_overlap(start, end, hotel.check_in, hotel.check_out) for hotel in hotel_queries
        ):
            continue
        if not isinstance(result, QuerySuccess):
            return None
        fare = _cheapest_fare(result)
        if fare is None:
            return None
        overlapping.append(result)
    if not overlapping:
        return None
    by_route: dict[tuple[str, str, str], float] = {}
    for result in overlapping:
        fare = _cheapest_fare(result)
        assert fare is not None
        key = _route_key(result.query)
        current = by_route.get(key)
        if current is None or fare < current:
            by_route[key] = fare
    return sum(by_route.values())


def _owned_hotel_stay(
    hotels: HotelSearchReport,
    flight_queries: Sequence[Trip],
) -> Optional[tuple[float, int]]:
    best: Optional[tuple[float, int]] = None
    for result in hotels.queries:
        if not any(
            dates_overlap(*trip_date_span(flight), result.query.check_in, result.query.check_out)
            for flight in flight_queries
        ):
            continue
        if not isinstance(result, HotelQuerySuccess) or not result.offers:
            continue
        stay = min(offer.total_price_eur for offer in result.offers)
        nights = result.query.nights
        if best is None or stay < best[0]:
            best = (stay, nights)
    return best


def owned_trip_total(
    flights: SearchReport,
    hotels: HotelSearchReport,
) -> Optional[TripTotal]:
    """Sum owned fare + owned stay. None if either side missed or dates miss."""
    if flights.currency.casefold() != hotels.currency.casefold():
        return None
    hotel_queries = tuple(result.query for result in hotels.queries)
    flight_queries = tuple(result.query for result in flights.queries)
    fare = _owned_flight_fare(flights, hotel_queries)
    stay = _owned_hotel_stay(hotels, flight_queries)
    if fare is None or stay is None:
        return None
    stay_eur, nights = stay
    return TripTotal(
        flight_fare_eur=fare,
        hotel_stay_eur=stay_eur,
        total_eur=fare + stay_eur,
        nights=nights,
    )


def _overlay_trip_shop_filters(
    trips: Sequence[Trip],
    *,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
) -> tuple[Trip, ...]:
    overlay: dict[str, object] = {}
    if bags is not None:
        overlay["bags"] = bags
    if carry_on is not None:
        overlay["carry_on"] = carry_on
    if price_cap_eur is not None:
        overlay["price_cap_eur"] = price_cap_eur
    if not overlay:
        return tuple(trips)
    return tuple(replace(trip, **overlay) for trip in trips)


def search_trip(
    trips: Sequence[Trip],
    hotel_query: HotelQuery,
    *,
    top: int = DEFAULT_TOP,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    progress: Optional[Callable[[str], None]] = None,
    sort: FlightSort = "ranked",
    fetch: str = "auto",
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    currency: str = "EUR",
    country: Optional[str] = None,
    hotel_source: HotelSourceName = "google",
) -> TripSearchReport:
    """Run flights then hotels sequentially. Omit trip_total when either misses."""
    flights = search_flights(
        _overlay_trip_shop_filters(
            trips,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap_eur,
        ),
        top=top,
        buffer_eur=buffer_eur,
        progress=progress,
        sort=sort,
        fetch=fetch,  # type: ignore[arg-type]
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
        depart_window=depart_window,
        via=via,
        exclude_via=exclude_via,
        currency=currency,
        country=country,
    )
    hotels = search_hotels(
        (hotel_query,),
        top=top,
        progress=progress,
        source=hotel_source,
    )
    fetch_ms: Optional[int] = None
    if flights.fetch_ms is not None or hotels.fetch_ms is not None:
        fetch_ms = (flights.fetch_ms or 0) + (hotels.fetch_ms or 0)
    searched_at = max(flights.searched_at, hotels.searched_at)
    return TripSearchReport(
        searched_at=searched_at,
        flights=flights,
        hotels=hotels,
        trip_total=owned_trip_total(flights, hotels),
        locale=FETCH_LANGUAGE,
        currency=flights.currency,
        fetch_ms=fetch_ms,
    )


def write_trip_report_atomic(report: TripSearchReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


def format_trip_total(total: TripTotal) -> str:
    nights_label = "night" if total.nights == 1 else "nights"
    return (
        f"Trip total: {total.flight_fare_eur:.0f} € fare + "
        f"{total.hotel_stay_eur:.0f} € stay (total stay, {total.nights} {nights_label}) = "
        f"{total.total_eur:.0f} €"
    )
