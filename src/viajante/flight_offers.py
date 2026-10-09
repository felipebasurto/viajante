"""Offer normalization, shop filtering, ranking, stop comparison, and recommendation."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Literal, Optional, Sequence, Tuple, get_args

from viajante.carriers import _normalize_airline, _passes_airline_filters
from viajante.flight_filters import (
    OfferFilters,
    _meets_requirements,
    _passes_overnight_filters,
    _passes_via_filters,
)
from viajante.flight_routes import _trip_max_stops
from viajante.google_flights import RawFlightCard
from viajante.models import FlightOffer, RawJourneyLeg, StopsCompare, StopsCompareSide, Trip
from viajante.parsers import clock_minutes as _clock_minutes
from viajante.parsers import normalize_clock, parse_duration_hours, parse_price, parse_stops_count
from viajante.recommend import Recommendation, Requirements, hide_slow_connections, recommend_offers

UNKNOWN_DURATION_SORTS_LAST = float("inf")


FlightSort = Literal["ranked", "fare", "price", "duration", "departure", "arrival"]


FLIGHT_SORTS: tuple[str, ...] = get_args(FlightSort)


LOW_COST_NAMES = [
    "AirAsia",
    "Batik Air",
    "Cebu Pacific",
    "easyJet",
    "Eurowings",
    "IndiGo",
    "Jetstar",
    "Jin Air",
    "Norwegian",
    "Peach",
    "Pegasus",
    "Ryanair",
    "Scoot",
    "Transavia",
    "T'way",
    "Vietjet",
    "Volotea",
    "Vueling",
    "Wizz Air",
    "ZIPAIR",
]


def validate_sort(sort: str) -> None:
    if sort not in FLIGHT_SORTS:
        raise ValueError(
            "sort must be 'ranked', 'fare', 'price', 'duration', 'departure', or 'arrival'"
        )


_LOW_COST_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(_normalize_airline(name)) for name in LOW_COST_NAMES) + r")\b"
)


def is_low_cost(airline_text: str) -> bool:
    return _LOW_COST_PATTERN.search(_normalize_airline(airline_text)) is not None


def _bag_evidence(
    raw: RawFlightCard,
    airline_text: str,
    *,
    baggage_buffer: int,
    requested: bool,
) -> tuple[int, bool]:
    knows = raw.checked_bags is not None or raw.carry_on is not None
    if knows:
        return 0, False
    needs_verify = is_low_cost(airline_text)
    if requested:
        return 0, needs_verify
    return (baggage_buffer if needs_verify else 0), needs_verify


def _package_totals(legs: Sequence[RawJourneyLeg]) -> tuple[Optional[int], Optional[float]]:
    stops = [parse_stops_count(leg.stops) for leg in legs]
    hours = [parse_duration_hours(leg.duration) for leg in legs]
    stops_count = max(stops) if None not in stops else None
    total = sum(hours) if None not in hours else None
    return stops_count, total


def _package_stops_text(count: Optional[int]) -> Optional[str]:
    if count is None:
        return None
    if count == 0:
        return "Nonstop"
    return f"{count} stop" if count == 1 else f"{count} stops"


def _package_duration_text(hours: Optional[float]) -> Optional[str]:
    if hours is None:
        return None
    minutes = round(hours * 60)
    return f"{minutes // 60} hr {minutes % 60} min"


def _normalize_offer(
    raw: RawFlightCard,
    max_stops: int,
    *,
    baggage_buffer: int = 0,
    max_layover_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_duration_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    enforce_requirements: bool = True,
) -> Optional[FlightOffer]:
    """Parse one card. ``enforce_requirements=False`` keeps offers that miss the stop cap,
    clock bounds, duration cap, or bag ask so the recommendation can report a relaxation."""
    price_text = raw.price or ""
    price = parse_price(price_text)
    if price is None or price <= 0:
        return None
    if not _passes_airline_filters(raw, airlines=airlines, exclude_airlines=exclude_airlines):
        return None
    if price_cap is not None and price > price_cap:
        return None
    if not _passes_via_filters(raw, via=via, exclude_via=exclude_via):
        return None
    if not _passes_overnight_filters(
        raw, no_overnight=no_overnight, require_overnight=require_overnight
    ):
        return None
    stops_count = parse_stops_count(raw.stops)
    layover_hours = raw.layover_hours
    duration_hours = parse_duration_hours(raw.duration)
    if (
        max_layover_hours is not None
        and stops_count
        and stops_count > 0
        and layover_hours is not None
        and layover_hours > max_layover_hours
    ):
        return None
    # The summary layover is the longest connection, so a minimum must read every known one.
    known_layovers = [
        layover.hours for leg in raw.legs for layover in leg.layovers if layover.hours is not None
    ]
    shortest_layover = min(known_layovers) if known_layovers else layover_hours
    if (
        min_layover_hours is not None
        and stops_count
        and stops_count > 0
        and shortest_layover is not None
        and shortest_layover < min_layover_hours
    ):
        return None
    packaged = len(raw.legs) > 1
    if packaged:
        # Stops and duration describe the whole package: the worst leg's stops, the summed
        # leg time. An unknown leg makes the total unknown; the outbound alone is not the trip.
        stops_count, duration_hours = _package_totals(raw.legs)
    airline = raw.airline or ""
    requested = bags is not None or carry_on is not None
    buffer, needs_verify = _bag_evidence(
        raw, airline, baggage_buffer=baggage_buffer, requested=requested
    )
    legs = tuple(
        RawJourneyLeg(
            departure=normalize_clock(leg.departure) or leg.departure,
            arrival=normalize_clock(leg.arrival) or leg.arrival,
            duration=leg.duration,
            stops=leg.stops,
            segments=leg.segments,
            layovers=leg.layovers,
        )
        for leg in raw.legs
    )
    offer = FlightOffer(
        airline=raw.airline,
        departure=normalize_clock(raw.departure) or raw.departure,
        arrival=normalize_clock(raw.arrival) or raw.arrival,
        price_text=price_text,
        price=price,
        duration=_package_duration_text(duration_hours) if packaged else raw.duration,
        duration_hours=duration_hours,
        stops=_package_stops_text(stops_count) if packaged else raw.stops,
        stops_count=stops_count,
        layover_city=None if packaged else raw.layover_city,
        layover_hours=None if packaged else layover_hours,
        flight_numbers=raw.flight_numbers,
        booking_token=raw.booking_token,
        baggage_buffer=buffer,
        needs_bag_verify=needs_verify,
        legs=legs,
        checked_bags=raw.checked_bags,
        carry_on=raw.carry_on,
    )
    if enforce_requirements and not _meets_requirements(
        raw,
        offer,
        max_stops,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        max_duration_hours=max_duration_hours,
        bags=bags,
        carry_on=carry_on,
    ):
        return None
    return offer


def _shop_offers(
    cards: Sequence[RawFlightCard],
    trip: Trip,
    filters: OfferFilters,
    *,
    baggage_buffer: int = 0,
) -> tuple[list[FlightOffer], list[FlightOffer]]:
    """One parse per card: (pool, eligible). Eligible also meets the requirements;
    the pool keeps the rest so a recommendation can report a relaxation."""
    max_stops = _trip_max_stops(trip)
    # With alliances on the request the provider applied a unioned include;
    # a card may qualify through an alliance member we cannot verify locally.
    airlines = trip.airlines if not trip.alliances else None
    shared: dict[str, Any] = {
        **vars(filters),
        "baggage_buffer": baggage_buffer,
        "airlines": airlines,
        "exclude_airlines": trip.exclude_airlines,
        "bags": trip.bags,
        "carry_on": trip.carry_on,
        "price_cap": trip.price_cap,
    }
    pool: list[FlightOffer] = []
    eligible: list[FlightOffer] = []
    for raw in cards:
        offer = _normalize_offer(raw, max_stops, enforce_requirements=False, **shared)
        if offer is None:
            continue
        pool.append(offer)
        if _meets_requirements(
            raw,
            offer,
            max_stops,
            depart_window=filters.depart_window,
            arrive_before=filters.arrive_before,
            depart_after=filters.depart_after,
            max_duration_hours=filters.max_duration_hours,
            bags=trip.bags,
            carry_on=trip.carry_on,
        ):
            eligible.append(offer)
    return pool, eligible


def offers_from_cards(
    cards: Sequence[RawFlightCard],
    trip: Trip,
    filters: OfferFilters,
    *,
    baggage_buffer: int = 0,
) -> list[FlightOffer]:
    """Owned offers that pass the trip's shop fields and the named post-filters."""
    return _shop_offers(cards, trip, filters, baggage_buffer=baggage_buffer)[1]


def _effective_cost(offer: FlightOffer) -> float:
    return offer.price + offer.baggage_buffer


def _duration_key(offer: FlightOffer) -> float:
    return offer.duration_hours if offer.duration_hours is not None else UNKNOWN_DURATION_SORTS_LAST


def _cheapest_by_fare(offers: Sequence[FlightOffer]) -> Optional[FlightOffer]:
    return min(offers, key=lambda offer: (offer.price, _duration_key(offer)), default=None)


def _cheapest_by_ranked(offers: Sequence[FlightOffer]) -> Optional[FlightOffer]:
    """Cheapest by owned fare+buffer. Missing buffer stamp is fare alone; never invent."""
    return min(
        offers, key=lambda offer: (_effective_cost(offer), _duration_key(offer)), default=None
    )


def compare_nonstop_vs_one_stop(offers: Sequence[FlightOffer]) -> Optional[StopsCompare]:
    """Cheapest cabin fare per stop bucket from one parsed set. No extra fetch."""
    nonstop = _cheapest_by_fare([offer for offer in offers if offer.stops_count == 0])
    one_stop = _cheapest_by_fare([offer for offer in offers if offer.stops_count == 1])
    if nonstop is None and one_stop is None:
        return None
    return StopsCompare(
        nonstop=StopsCompareSide.from_offer(nonstop) if nonstop is not None else None,
        one_stop=StopsCompareSide.from_offer(one_stop) if one_stop is not None else None,
    )


def _offer_sort_key(offer: FlightOffer, sort: FlightSort) -> tuple[float, float]:
    if sort == "duration":
        return (_duration_key(offer), offer.price)
    if sort in ("departure", "arrival"):
        minutes = _clock_minutes(getattr(offer, sort))
        primary = float(minutes) if minutes is not None else UNKNOWN_DURATION_SORTS_LAST
        return (primary, offer.price)
    primary = offer.price if sort in ("fare", "price") else _effective_cost(offer)
    return (primary, _duration_key(offer))


def _rank_offers(
    offers: Sequence[FlightOffer],
    *,
    top: int,
    sort: FlightSort = "ranked",
) -> Tuple[FlightOffer, ...]:
    rows = offers if sort != "ranked" else hide_slow_connections(offers)
    rows = sorted(rows, key=lambda offer: _offer_sort_key(offer, sort))
    seen: set[tuple] = set()
    deduped: list[FlightOffer] = []
    for offer in rows:
        key = (
            offer.airline,
            offer.departure,
            offer.arrival,
            offer.price,
            offer.stops_count,
            offer.duration_hours,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(offer)
        if len(deduped) >= top:
            break
    return tuple(deduped)


def _recommend(
    trip: Trip,
    pool: Sequence[FlightOffer],
    filters: OfferFilters,
    *,
    currency: str,
    packaged: bool,
) -> Optional[Recommendation]:
    """Recommendation over the pool before the requirement filters, so a relaxation can show.

    Round-trip and multi-city offers only exist after the filters ran on every journey
    (the next journey is shopped lazily), so a package cannot be relaxed.
    """
    requirements = Requirements(
        max_stops=_trip_max_stops(trip),
        depart_window=filters.depart_window,
        depart_after=filters.depart_after,
        arrive_before=filters.arrive_before,
        max_duration=filters.max_duration_hours,
        carry_on=trip.carry_on,
        bags=trip.bags,
    )
    recommendation = recommend_offers(pool, requirements, currency=currency, relax=not packaged)
    if recommendation is None or not packaged:
        return recommendation
    note = (
        "Round-trip and multi-city: requirements were applied to every journey before ranking "
        "and cannot be relaxed; the per-offer requirement check covers the first journey only."
    )
    return replace(recommendation, notes=(*recommendation.notes, note))
