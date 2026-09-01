"""Typical fare from owned daily prices for the same trip shape.

One-way: median of cheapest-per-day calendar prices on that origin-destination.
Packaged round-trip: median of that same-stay calendar (same origin/destination
and the same number of nights). Missing or thin owned data means no comparison.
Never a guessed market average, never a third-party fare history, never a
hardcoded city fare. Multi-city has no honest same-stay grid, so it stays omitted.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from statistics import median
from typing import Optional, Sequence

from viajante.models import (
    ExploreDestination,
    FlightOffer,
    vs_typical,
    vs_typical_pct,
)

# Same cap as dates.MAX_DATE_WINDOW_DAYS. Tests pin the two together.
TYPICAL_WINDOW_DAYS = 31
MIN_DAILY_PRICES = 3


def typical_from_daily_prices(
    prices: Sequence[Optional[float]],
) -> Optional[float]:
    """Median of positive owned daily prices, or None if the list is too thin."""
    owned = [float(price) for price in prices if price is not None and price > 0]
    if len(owned) < MIN_DAILY_PRICES:
        return None
    return float(median(owned))


def with_typical(
    offer: FlightOffer,
    typical: Optional[float],
    *,
    cheapest_date: Optional[date] = None,
    cheapest: Optional[float] = None,
) -> FlightOffer:
    label = vs_typical(offer.price, typical)
    pct = vs_typical_pct(offer.price, typical)
    if typical is None or label is None or pct is None:
        return offer
    if cheapest_date is None or cheapest is None or cheapest <= 0:
        return replace(
            offer,
            typical=typical,
            vs_typical=label,
            vs_typical_pct=pct,
        )
    return replace(
        offer,
        typical=typical,
        vs_typical=label,
        vs_typical_pct=pct,
        cheapest_date=cheapest_date,
        cheapest=cheapest,
    )


def with_typical_dest(
    dest: ExploreDestination,
    typical: Optional[float],
) -> ExploreDestination:
    """Stamp a shopped dest from the same-route calendar median flights uses."""
    if dest.price is None:
        return dest
    label = vs_typical(dest.price, typical)
    pct = vs_typical_pct(dest.price, typical)
    if typical is None or label is None or pct is None:
        return dest
    return replace(
        dest,
        typical=typical,
        vs_typical=label,
        vs_typical_pct=pct,
    )
