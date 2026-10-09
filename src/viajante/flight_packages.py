"""Packaged round-trip and multi-city offers: the package filters over every journey."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from viajante.flight_filters import OfferFilters, _passes_overnight_filters, _passes_via_filters
from viajante.flight_offers import (
    FlightSort,
    _normalize_offer,
    _offer_sort_key,
    _rank_offers,
)
from viajante.google_flights import RawFlightCard
from viajante.models import FlightOffer, Trip


def package_shop_filters(filters: OfferFilters, packaged: bool) -> OfferFilters:
    """What the one-way normalizer judges on each journey; the package pass judges the rest."""
    if not packaged:
        return filters
    return replace(filters, via=None, exclude_via=None, no_overnight=None, require_overnight=None)


def _packaged_eligible(
    offers: Sequence[FlightOffer],
    trip: Trip,
    filters: OfferFilters,
    *,
    top: int,
    sort: FlightSort,
) -> list[FlightOffer]:
    """The one package pass after normalization, in sort order, stopping once ``top`` rank.

    Offers are judged a chunk of ``top`` at a time, so a fare-ordered list whose first rows
    fail the package rules still reaches ``top`` passing offers. The result is therefore the
    passing prefix of the sorted offers, not every passing offer.
    """
    candidates = sorted(offers, key=lambda offer: _offer_sort_key(offer, sort))
    eligible: list[FlightOffer] = []
    for start in range(0, len(candidates), top):
        eligible.extend(
            offer
            for offer in candidates[start : start + top]
            if _passes_packaged_filters(offer, trip, filters)
        )
        if len(_rank_offers(eligible, top=top, sort=sort)) >= top:
            break
    return eligible


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
        if _normalize_offer(card, query.max_stops, **per_journey.normalizer_kwargs()) is None:
            return False
        for layover in leg.layovers:
            if layover.hours is None:
                continue
            if filters.max_layover_hours is not None and layover.hours > filters.max_layover_hours:
                return False
            if filters.min_layover_hours is not None and layover.hours < filters.min_layover_hours:
                return False
    return True
