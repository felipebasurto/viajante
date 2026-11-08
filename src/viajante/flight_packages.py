"""Packaged round-trip and multi-city offers: the package filters over every journey."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from viajante.flight_filters import (
    OfferFilters,
    _passes_overnight_filters,
    _passes_packaged_leg,
    _passes_via_filters,
)
from viajante.flight_offers import (
    FlightSort,
    _offer_sort_key,
    _rank_offers,
)
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
    if not _passes_via_filters(offer, via=filters.via, exclude_via=filters.exclude_via):
        return False
    if not _passes_overnight_filters(
        offer, no_overnight=filters.no_overnight, require_overnight=filters.require_overnight
    ):
        return False
    for leg, query in zip(offer.legs, trip.legs, strict=False):
        if not _passes_packaged_leg(leg, query.max_stops, filters):
            return False
    return True
