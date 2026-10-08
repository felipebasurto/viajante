"""Typical fare from the owned daily prices a caller explicitly asked for.

Dates and flex take the median of the priced days inside the caller's own window
(one-way, or packaged round-trip with the same nights). Fewer than three priced
days means no median. Never a guessed market average, never a third-party fare
history, never a hardcoded city fare.
"""

from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Optional, Sequence

from viajante.models import (
    FlightOffer,
    vs_typical,
    vs_typical_pct,
)

MIN_DAILY_PRICES = 3


def typical_from_daily_prices(
    prices: Sequence[Optional[float]],
) -> Optional[float]:
    """Median of positive owned daily prices, or None if the list is too thin."""
    owned = [float(price) for price in prices if price is not None and price > 0]
    if len(owned) < MIN_DAILY_PRICES:
        return None
    return float(median(owned))


def _typical_fields(price: float, typical: Optional[float]) -> Optional[dict[str, object]]:
    label = vs_typical(price, typical)
    pct = vs_typical_pct(price, typical)
    if typical is None or label is None or pct is None:
        return None
    return {"typical": typical, "vs_typical": label, "vs_typical_pct": pct}


def with_typical(offer: FlightOffer, typical: Optional[float]) -> FlightOffer:
    fields = _typical_fields(offer.price, typical)
    if fields is None:
        return offer
    return replace(offer, **fields)
