"""Cheap destinations from an origin via the owned Explore RPC, then priced."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from viajante.airports import is_known_iata
from viajante.flights import _normalize_offer, classify_failure
from viajante.google_flights import GoogleFlightsHttpSource, RawFlightCard
from viajante.google_flights_rpc import CompactExplorePlace
from viajante.models import (
    ExploreDestination,
    ExploreReport,
    FlightCabin,
    FlightQuery,
    SearchError,
)
from viajante.storage import write_json_atomic

DEFAULT_EXPLORE_TOP = 12
MAX_EXPLORE_TOP = 30


class ExploreSource(Protocol):
    def fetch_explore(
        self,
        origin: str,
        departure_date: date,
        *,
        adults: int = 1,
        cabin: FlightCabin = "economy",
    ) -> Sequence[CompactExplorePlace]: ...

    def fetch(self, query: FlightQuery) -> Sequence[RawFlightCard]: ...

    def close(self) -> None: ...


def validate_explore_window(start: date, days: int, *, today: Optional[date] = None) -> None:
    if days < 1:
        raise ValueError("--days must be at least 1")
    check = today or date.today()
    if start < check:
        raise ValueError(f"start date is in the past: {start.isoformat()}")


def search_explore(
    origin: str,
    start: date,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    price_cap_eur: Optional[int] = None,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[ExploreSource] = None,
) -> ExploreReport:
    validate_explore_window(start, days)
    if top <= 0:
        raise ValueError("top must be positive")
    if top > MAX_EXPLORE_TOP:
        raise ValueError(f"top is at most {MAX_EXPLORE_TOP}")
    if price_cap_eur is not None and price_cap_eur <= 0:
        raise ValueError("price_cap_eur must be positive")
    origin = origin.strip().upper()
    if not is_known_iata(origin):
        raise ValueError(f"unknown origin IATA code: {origin!r}")
    report_progress = progress or (lambda _: None)
    report_progress(f"explore: from {origin} on {start.isoformat()} ({days}-day trip window)")
    started = time.perf_counter()
    client = source or GoogleFlightsHttpSource()
    error: Optional[SearchError] = None
    destinations: tuple[ExploreDestination, ...] = ()
    try:
        try:
            places = tuple(client.fetch_explore(origin, start, adults=adults, cabin=cabin))
        except Exception as exc:
            error = classify_failure(exc)
            places = ()
        priced: list[ExploreDestination] = []
        for index, place in enumerate(places[:top]):
            report_progress(f"[{index + 1}/{min(top, len(places))}] pricing {place.iata}")
            price = _cheapest_price(
                client,
                origin=origin,
                destination=place.iata,
                departure_date=start,
                max_stops=max_stops,
                adults=adults,
                cabin=cabin,
                price_cap_eur=price_cap_eur,
            )
            if price_cap_eur is not None and price is None:
                continue
            priced.append(
                ExploreDestination(
                    iata=place.iata,
                    city=place.city,
                    country=place.country,
                    price_eur=price,
                )
            )
        priced.sort(key=lambda row: (row.price_eur is None, row.price_eur or 0.0, row.iata))
        destinations = tuple(priced)
    finally:
        client.close()
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return ExploreReport(
        searched_at=datetime.now(timezone.utc),
        origin=origin,
        start_date=start,
        days=days,
        destinations=destinations,
        fetch_backend="explore",
        fetch_ms=fetch_ms,
        error=error,
    )


def write_explore_report_atomic(report: ExploreReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


def _cheapest_price(
    source: ExploreSource,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
    price_cap_eur: Optional[int] = None,
) -> Optional[float]:
    query = FlightQuery(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        price_cap_eur=price_cap_eur,
    )
    try:
        cards = source.fetch(query)
    except Exception:
        return None
    prices = [
        offer.price_eur
        for raw in cards
        if (offer := _normalize_offer(raw, max_stops, buffer_eur=0, price_cap_eur=price_cap_eur))
        is not None
    ]
    return min(prices) if prices else None
