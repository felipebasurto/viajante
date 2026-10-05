"""Bounded Pareto comparison of owned offers within one query and currency."""

from typing import Callable, Optional, Sequence

from viajante.models import FlightOffer, Trip
from viajante.parsers import parse_duration_hours, parse_stops_count


def dimensions(offer: FlightOffer, trip: Trip) -> Optional[tuple[float, float, int]]:
    if len(trip.legs) > 1:
        if len(offer.legs) != len(trip.legs):
            return None
        durations = [parse_duration_hours(leg.duration) for leg in offer.legs]
        stops = [parse_stops_count(leg.stops) for leg in offer.legs]
        if any(value is None for value in durations + stops):
            return None
        return offer.price, sum(durations), sum(stops)  # type: ignore[arg-type,return-value]
    if offer.duration_hours is None or offer.stops_count is None:
        return None
    return offer.price, offer.duration_hours, offer.stops_count


def _same_baggage(left: FlightOffer, right: FlightOffer) -> bool:
    return (
        left.checked_bags is not None
        and left.carry_on is not None
        and (left.checked_bags, left.carry_on) == (right.checked_bags, right.carry_on)
    )


def pareto_select(
    offers: Sequence[FlightOffer],
    trip: Trip,
    *,
    top: int,
    order: Callable[[Sequence[FlightOffer]], Sequence[FlightOffer]],
) -> tuple[tuple[FlightOffer, ...], dict[str, object]]:
    measured = [(index, dimensions(offer, trip)) for index, offer in enumerate(offers)]
    complete = [(index, dims) for index, dims in measured if dims is not None]
    incomplete = [offers[index] for index, dims in measured if dims is None]
    frontier = []
    for index, dims in complete:
        if not any(
            other != index
            and _same_baggage(offers[other], offers[index])
            and all(a <= b for a, b in zip(other_dims, dims, strict=True))
            and any(a < b for a, b in zip(other_dims, dims, strict=True))
            for other, other_dims in complete
        ):
            frontier.append(index)
    chosen = []
    for axis in range(3):
        if frontier:
            best = min(frontier, key=lambda index: (measured[index][1][axis], index))
            if best not in chosen:
                chosen.append(best)
    candidates = [offers[index] for index in chosen]
    for offer in order([offers[index] for index in frontier]):
        if not any(offer is existing for existing in candidates):
            candidates.append(offer)
    candidates.extend(order(incomplete))
    return tuple(candidates[:top]), {
        "mode": "pareto",
        "dimensions": ["price", "duration_hours", "stops_count"],
        "candidates_compared": len(complete),
        "frontier_count": len(frontier),
        "incomplete_count": len(incomplete),
        "truncated": len(candidates) > top,
        "scope": "returned candidates in this query and currency; equivalent known baggage only",
    }
