"""Cheap destinations from an origin via the owned Explore RPC, then priced."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

from viajante.airports import is_known_iata
from viajante.flights import _normalize_offer, classify_failure, parse_via_airports
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


def _parse_via_pair(
    via: Optional[Sequence[str]],
    exclude_via: Optional[Sequence[str]],
) -> tuple[Optional[tuple[str, ...]], Optional[tuple[str, ...]]]:
    parsed_via = parse_via_airports(",".join(via), role="via") if via else None
    parsed_exclude = (
        parse_via_airports(",".join(exclude_via), role="exclude-via") if exclude_via else None
    )
    if parsed_via and parsed_exclude and set(parsed_via) & set(parsed_exclude):
        raise ValueError("via and exclude-via must not share a code")
    return parsed_via, parsed_exclude


def _named_shop_filters(
    *,
    bags: Optional[int],
    carry_on: Optional[int],
    price_cap_eur: Optional[int],
    airlines: Optional[Sequence[str]],
    exclude_airlines: Optional[Sequence[str]],
    via: Optional[Sequence[str]],
    exclude_via: Optional[Sequence[str]],
) -> bool:
    return (
        bags is not None
        or carry_on is not None
        or price_cap_eur is not None
        or bool(airlines)
        or bool(exclude_airlines)
        or bool(via)
        or bool(exclude_via)
    )


def search_explore(
    origin: str,
    start: date,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
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
    parsed_via, parsed_exclude_via = _parse_via_pair(via, exclude_via)
    origin = origin.strip().upper()
    if not is_known_iata(origin):
        raise ValueError(f"unknown origin IATA code: {origin!r}")
    report_progress = progress or (lambda _: None)
    report_progress(f"explore: from {origin} on {start.isoformat()} ({days}-day trip window)")
    started = time.perf_counter()
    client = source or GoogleFlightsHttpSource()
    error: Optional[SearchError] = None
    destinations: tuple[ExploreDestination, ...] = ()
    drop_unpriced = _named_shop_filters(
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        via=parsed_via,
        exclude_via=parsed_exclude_via,
    )
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
                bags=bags,
                carry_on=carry_on,
                price_cap_eur=price_cap_eur,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                via=parsed_via,
                exclude_via=parsed_exclude_via,
            )
            if drop_unpriced and price is None:
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
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
) -> Optional[float]:
    airline_codes = tuple(airlines) if airlines is not None else None
    exclude_codes = tuple(exclude_airlines) if exclude_airlines is not None else None
    query = FlightQuery(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airline_codes,
        exclude_airlines=exclude_codes,
    )
    try:
        cards = source.fetch(query)
    except Exception:
        return None
    prices = [
        offer.price_eur
        for raw in cards
        if (
            offer := _normalize_offer(
                raw,
                max_stops,
                buffer_eur=0,
                airlines=query.airlines,
                exclude_airlines=query.exclude_airlines,
                via=via,
                exclude_via=exclude_via,
                bags=query.bags,
                carry_on=query.carry_on,
                price_cap_eur=query.price_cap_eur,
            )
        )
        is not None
    ]
    return min(prices) if prices else None
