"""Packaged round-trip and multi-city offers: next-leg completion and package filters."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Sequence, Tuple

from viajante.control import SearchDeadline
from viajante.flight_filters import OfferFilters, _passes_overnight_filters, _passes_via_filters
from viajante.flight_offers import _normalize_offer
from viajante.google_flights import RawFlightCard
from viajante.models import FlightOffer, RawJourneyLeg, Trip
from viajante.parsers import parse_price


def _selected_slices(leg: RawJourneyLeg) -> Optional[list[list[object]]]:
    """Owned slices for a next-leg shopping call. Incomplete slices stay unset."""
    if not leg.segments:
        return None
    rows: list[list[object]] = []
    for segment in leg.segments:
        code = segment.carrier
        number = segment.flight_number
        on = segment.departure_date
        if not segment.origin or not segment.destination or on is None or not code or not number:
            return None
        if not number.upper().startswith(code.upper()):
            return None
        bare = number[len(code) :]
        if not bare or not bare[0].isdigit():
            return None
        rows.append([segment.origin, on.isoformat(), segment.destination, None, code, bare])
    return rows


def _priced_return_leg(
    cards: Sequence[RawFlightCard],
    price: float,
    origin: str,
) -> Optional[RawJourneyLeg]:
    """The next leg whose package price equals the offer, only when that match is unique."""
    found: list[RawJourneyLeg] = []
    for card in cards:
        amount = parse_price(card.price)
        if amount is None or amount != price:
            continue
        if not card.legs:
            continue
        leg = card.legs[0]
        start = leg.segments[0].origin if leg.segments else None
        if start != origin:
            continue
        found.append(leg)
    if len(found) != 1:
        return None
    return found[0]


def _attach_missing_legs(
    trip: Trip,
    offers: Tuple[FlightOffer, ...],
    source: object,
) -> Tuple[FlightOffer, ...]:
    """Fill the next packaged leg from a follow-up shop. A miss stays unknown.

    ponytail: one follow-up fills only the next leg. A 3+ city trip still
    marks later legs unknown; chain selections if that search shows up.
    """
    fetch = getattr(source, "fetch_selected", None)
    if not callable(fetch) or len(trip.legs) < 2 or not offers:
        return offers
    pending: list[tuple[int, list[list[object]]]] = []
    for index, offer in enumerate(offers):
        if len(offer.legs) != 1:
            continue
        selected = _selected_slices(offer.legs[0])
        if selected is None:
            continue
        pending.append((index, selected))
    if not pending:
        return offers
    try:
        results = fetch(trip, [selected for _, selected in pending])
    except SearchDeadline:
        raise
    except Exception:
        return offers
    if not isinstance(results, Sequence) or len(results) != len(pending):
        return offers
    updated = list(offers)
    for (index, _), result in zip(pending, results, strict=True):
        if isinstance(result, SearchDeadline):
            raise result
        if isinstance(result, BaseException):
            continue
        offer = updated[index]
        origin = trip.legs[len(offer.legs)].origin
        leg = _priced_return_leg(result, offer.price, origin)
        if leg is None:
            continue
        updated[index] = replace(offer, legs=offer.legs + (leg,), completeness=None)
    return tuple(updated)


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
