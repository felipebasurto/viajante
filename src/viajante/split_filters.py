"""Named filters for split itineraries, judged on evidence each itinerary owns.

A hub split is one journey through a hub: the clock filters read its first departure and
last arrival, ``max_duration`` reads each ticket, and the connection filters (layover
bounds, ``via``, ``exclude_via``, the overnight lists) read the hub connection. Airport
filters read every airport the itinerary names: origin, hub, and destination.

A mixed one-way pair (a round trip sold as two tickets) has no connection, so each ticket
is its own journey: the clock and duration filters apply to each ticket, and naming a
connection filter drops every mixed pair, because nothing can prove it.

Unknown clocks cannot prove a named bound, and an unknown duration does not exclude, the
same rules as the one-way search. Nothing here converts money or invents a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Tuple

from viajante.flight_filters import (
    OfferFilters,
    _clock_minutes,
    _passes_clock_bound,
)

if TYPE_CHECKING:
    from viajante.split import SplitItinerary


@dataclass(frozen=True)
class SplitFilters:
    offer: OfferFilters = field(default_factory=OfferFilters)
    exclude_airports: Tuple[str, ...] = ()
    include_airports: Tuple[str, ...] = ()

    @property
    def named(self) -> bool:
        return self.offer.named or bool(self.exclude_airports or self.include_airports)

    @property
    def connection_named(self) -> bool:
        offer = self.offer
        return any(
            value is not None
            for value in (
                offer.min_layover_hours,
                offer.max_layover_hours,
                offer.via,
                offer.exclude_via,
                offer.no_overnight,
                offer.require_overnight,
            )
        )


def _journey_clocks(itinerary: SplitItinerary) -> tuple[tuple[str | None, str | None], ...]:
    """(first departure, last arrival) per journey: one for a hub split, two for mixed."""
    if itinerary.kind == "hub":
        first, last = itinerary.parts[0].offer.legs[0], itinerary.parts[-1].offer.legs[-1]
        return ((first.departure, last.arrival),)
    return tuple(
        (part.offer.legs[0].departure, part.offer.legs[-1].arrival) for part in itinerary.parts
    )


def _clocks_pass(departure: str | None, arrival: str | None, offer: OfferFilters) -> bool:
    if offer.depart_window is not None:
        minutes = _clock_minutes(departure)
        if minutes is None:
            return False
        start, end = offer.depart_window
        if not start <= minutes <= end:
            return False
    return _passes_clock_bound(departure, offer.depart_after, before=False) and _passes_clock_bound(
        arrival, offer.arrive_before, before=True
    )


def _hub_connection_passes(itinerary: SplitItinerary, offer: OfferFilters) -> bool:
    hub = itinerary.hub
    if offer.min_layover_hours is not None or offer.max_layover_hours is not None:
        if itinerary.connection_minutes is None:
            return False
        hours = itinerary.connection_minutes / 60
        if offer.min_layover_hours is not None and hours < offer.min_layover_hours:
            return False
        if offer.max_layover_hours is not None and hours > offer.max_layover_hours:
            return False
    if offer.via is not None and hub not in offer.via:
        return False
    if offer.exclude_via is not None and hub in offer.exclude_via:
        return False
    if offer.no_overnight is not None and hub in offer.no_overnight and itinerary.overnight_at_hub:
        return False
    if (
        offer.require_overnight is not None
        and hub in offer.require_overnight
        and not itinerary.overnight_at_hub
    ):
        return False
    return True


def passes(itinerary: SplitItinerary, filters: SplitFilters) -> bool:
    """True when the itinerary satisfies every named filter from its own owned evidence."""
    offer = filters.offer
    if itinerary.kind != "hub" and filters.connection_named:
        return False
    if not all(_clocks_pass(dep, arr, offer) for dep, arr in _journey_clocks(itinerary)):
        return False
    if offer.max_duration_hours is not None:
        for part in itinerary.parts:
            duration = part.offer.duration_hours
            if duration is not None and duration > offer.max_duration_hours:
                return False
    airports = {
        code for part in itinerary.parts for code in (part.query.origin, part.query.destination)
    }
    if itinerary.hub:
        airports.add(itinerary.hub)
    if filters.exclude_airports and airports & set(filters.exclude_airports):
        return False
    if filters.include_airports:
        # Include: the trip destination (last ticket of a hub split, outbound of a pair).
        destination = (
            itinerary.parts[-1].query.destination
            if itinerary.kind == "hub"
            else itinerary.parts[0].query.destination
        )
        if destination not in filters.include_airports:
            return False
    if itinerary.kind == "hub":
        return _hub_connection_passes(itinerary, offer)
    return True
