"""Typical fare from owned daily prices for the same trip shape.

One-way: median of cheapest-per-day calendar prices on that origin-destination.
Packaged round-trip: median of that same-stay calendar (same origin/destination
and the same number of nights). Missing or thin owned data means no comparison
— never a guessed market average, never a third-party fare history, never a
hardcoded city fare. Multi-city has no honest same-stay grid, so it stays omitted.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from statistics import median
from typing import Optional, Sequence

from viajante.models import (
    NEAR_TYPICAL_RATIO,
    ExploreDestination,
    FlightOffer,
    vs_typical,
    vs_typical_pct,
)

# Same cap as dates.MAX_DATE_WINDOW_DAYS. Tests pin the two together.
TYPICAL_WINDOW_DAYS = 31
MIN_DAILY_PRICES = 3
NEAR_RATIO = NEAR_TYPICAL_RATIO


def typical_eur_from_daily_prices(
    prices: Sequence[Optional[float]],
) -> Optional[float]:
    """Median of positive owned daily prices, or None if the list is too thin."""
    owned = [float(price) for price in prices if price is not None and price > 0]
    if len(owned) < MIN_DAILY_PRICES:
        return None
    return float(median(owned))


def with_typical(
    offer: FlightOffer,
    typical_eur: Optional[float],
    *,
    cheapest_date: Optional[date] = None,
    cheapest_eur: Optional[float] = None,
) -> FlightOffer:
    label = vs_typical(offer.price_eur, typical_eur)
    pct = vs_typical_pct(offer.price_eur, typical_eur)
    if typical_eur is None or label is None or pct is None:
        return offer
    if cheapest_date is None or cheapest_eur is None or cheapest_eur <= 0:
        return replace(
            offer,
            typical_eur=typical_eur,
            vs_typical=label,
            vs_typical_pct=pct,
        )
    return replace(
        offer,
        typical_eur=typical_eur,
        vs_typical=label,
        vs_typical_pct=pct,
        cheapest_date=cheapest_date,
        cheapest_eur=cheapest_eur,
    )


def with_typical_dest(
    dest: ExploreDestination,
    typical_eur: Optional[float],
) -> ExploreDestination:
    """Stamp a shopped dest from the same-route calendar median flights uses."""
    if dest.price_eur is None:
        return dest
    label = vs_typical(dest.price_eur, typical_eur)
    pct = vs_typical_pct(dest.price_eur, typical_eur)
    if typical_eur is None or label is None or pct is None:
        return dest
    return replace(
        dest,
        typical_eur=typical_eur,
        vs_typical=label,
        vs_typical_pct=pct,
    )
