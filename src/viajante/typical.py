"""Typical fare for one origin-destination from owned daily prices.

The number is a median over cheapest-per-day calendar prices for that
same route. Missing or thin owned data means no comparison — never a
guessed market average, never a third-party fare history, never a
hardcoded city fare.
"""

from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Optional, Sequence

from viajante.models import FlightOffer, VsTypical

# Same cap as dates.MAX_DATE_WINDOW_DAYS. Tests pin the two together.
TYPICAL_WINDOW_DAYS = 31
MIN_DAILY_PRICES = 3
NEAR_RATIO = 0.10


def typical_eur_from_daily_prices(
    prices: Sequence[Optional[float]],
) -> Optional[float]:
    """Median of positive owned daily prices, or None if the list is too thin."""
    owned = [float(price) for price in prices if price is not None and price > 0]
    if len(owned) < MIN_DAILY_PRICES:
        return None
    return float(median(owned))


def vs_typical(price_eur: float, typical_eur: Optional[float]) -> Optional[VsTypical]:
    """Coarse label against an owned typical. None when there is no typical."""
    if typical_eur is None or typical_eur <= 0:
        return None
    if price_eur < typical_eur * (1.0 - NEAR_RATIO):
        return "below"
    if price_eur > typical_eur * (1.0 + NEAR_RATIO):
        return "above"
    return "near"


def with_typical(offer: FlightOffer, typical_eur: Optional[float]) -> FlightOffer:
    label = vs_typical(offer.price_eur, typical_eur)
    if typical_eur is None or label is None:
        return offer
    return replace(offer, typical_eur=typical_eur, vs_typical=label)
