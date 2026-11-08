"""Requests the public Google Flights page cannot verify, and the leg match both transports share.

These sit below ``google_flights_public`` and ``google_flights_detail`` so the browser detail
source never imports the public transport. The refusal is raised before any network call.
"""

from __future__ import annotations

from viajante.google_flights import GoogleFlightsRejected, RawFlightCard
from viajante.models import MultiCity, Trip


class GoogleFlightsUnsupported(GoogleFlightsRejected):
    """A requested capability cannot be represented by this transport."""


def validate_capabilities(trip: Trip, *, allow_multi_city: bool = False) -> None:
    unsupported = [
        key
        for key in (
            "bags",
            "exclude_alliances",
        )
        if getattr(trip, key, None) is not None
    ]
    # A positive carry-on rides the tfs BaggageFilter and is verified by the
    # page's Bags chip echo. Checked bags produce no provider echo on this
    # transport, and a named 0 is the unselected state (no echo either);
    # both stay refused like an unverifiable filter.
    if trip.carry_on == 0:
        unsupported.append("carry_on")
    if isinstance(trip, MultiCity) and not allow_multi_city:
        unsupported.append("multi_city")
    if unsupported:
        reasons = []
        if "bags" in unsupported or "carry_on" in unsupported:
            reasons.append("baggage-inclusive fares remain unknown")
        if "exclude_alliances" in unsupported:
            reasons.append(
                "alliance exclusion is not verifiable: the public page "
                "registers the filter but does not remove every "
                "alliance-marketed itinerary"
            )
        if "multi_city" in unsupported:
            reasons.append(
                "the public page returns no multi-city results; request an explicit "
                "browser detail search (--fetch detail) for multi-city packages"
            )
        message = (
            "Not sent. Google Flights public-page transport cannot verify "
            "these requested capabilities: "
        ) + ", ".join(unsupported)
        message += ". Remove them only for an explicitly separate scenario"
        if reasons:
            message += "; " + "; ".join(reasons)
        raise GoogleFlightsUnsupported(message + ".")


def matches_leg(card: RawFlightCard, query) -> bool:
    if len(card.legs) != 1 or not card.legs[0].segments:
        return False
    segments = card.legs[0].segments
    return (
        segments[0].origin == query.origin
        and segments[-1].destination == query.destination
        and segments[0].departure_date == query.departure_date
        and len(segments) - 1 <= query.max_stops
    )
