"""Packaged round-trip and multi-city offers: the package filters over every journey."""

from __future__ import annotations

from dataclasses import replace

from viajante.flight_filters import OfferFilters, _passes_overnight_filters, _passes_via_filters
from viajante.flight_offers import _normalize_offer
from viajante.google_flights import RawFlightCard
from viajante.models import FlightOffer, Trip


def _passes_packaged_filters(offer: FlightOffer, trip: Trip, filters: OfferFilters) -> bool:
    if filters.named and len(offer.legs) != len(trip.legs):
        return False
    raw = RawFlightCard(
        airline=offer.airline,
        departure=offer.departure,
        arrival=offer.arrival,
        duration=offer.duration,
        stops=offer.stops,
        price=offer.price_text,
        layover_city=offer.layover_city,
        layover_hours=offer.layover_hours,
        legs=offer.legs,
    )
    if not _passes_via_filters(raw, via=filters.via, exclude_via=filters.exclude_via):
        return False
    if not _passes_overnight_filters(
        raw, no_overnight=filters.no_overnight, require_overnight=filters.require_overnight
    ):
        return False
    per_journey = replace(filters, via=None, exclude_via=None, require_overnight=None)
    for leg, query in zip(offer.legs, trip.legs, strict=False):
        card = replace(
            raw,
            departure=leg.departure,
            arrival=leg.arrival,
            duration=leg.duration,
            stops=leg.stops,
            layover_city=None,
            layover_hours=None,
            legs=(leg,),
        )
        if _normalize_offer(card, query.max_stops, **vars(per_journey)) is None:
            return False
        for layover in leg.layovers:
            if layover.hours is None:
                continue
            if filters.max_layover_hours is not None and layover.hours > filters.max_layover_hours:
                return False
            if filters.min_layover_hours is not None and layover.hours < filters.min_layover_hours:
                return False
    return True
