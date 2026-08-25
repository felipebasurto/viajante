"""Cheap destinations from an origin via the owned Explore RPC, then priced."""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence, Tuple

from viajante.airports import is_known_iata
from viajante.flights import (
    _normalize_offer,
    classify_failure,
    expand_nearby_origins,
    parse_via_airports,
    validate_layover_hours,
)
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
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
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
    alliances: Optional[Sequence[str]],
    exclude_alliances: Optional[Sequence[str]],
    via: Optional[Sequence[str]],
    exclude_via: Optional[Sequence[str]],
    depart_window: Optional[Tuple[int, int]],
    max_layover_hours: Optional[float],
    min_layover_hours: Optional[float],
    max_duration_hours: Optional[float],
) -> bool:
    return (
        bags is not None
        or carry_on is not None
        or price_cap_eur is not None
        or bool(airlines)
        or bool(exclude_airlines)
        or bool(alliances)
        or bool(exclude_alliances)
        or bool(via)
        or bool(exclude_via)
        or depart_window is not None
        or max_layover_hours is not None
        or min_layover_hours is not None
        or max_duration_hours is not None
    )


def _one_or_many(reports: list[ExploreReport]) -> ExploreReport | tuple[ExploreReport, ...]:
    if len(reports) == 1:
        return reports[0]
    return tuple(reports)


def _explore_for_origin(
    client: ExploreSource,
    origin: str,
    start: date,
    *,
    days: int,
    top: int,
    adults: int,
    children: int,
    infants_in_seat: int,
    infants_on_lap: int,
    cabin: FlightCabin,
    max_stops: int,
    bags: Optional[int],
    carry_on: Optional[int],
    price_cap_eur: Optional[int],
    airlines: Optional[Sequence[str]],
    exclude_airlines: Optional[Sequence[str]],
    alliances: Optional[Sequence[str]],
    exclude_alliances: Optional[Sequence[str]],
    parsed_via: Optional[tuple[str, ...]],
    parsed_exclude_via: Optional[tuple[str, ...]],
    depart_window: Optional[Tuple[int, int]],
    max_layover_hours: Optional[float],
    min_layover_hours: Optional[float],
    max_duration_hours: Optional[float],
    drop_unpriced: bool,
    nearby_label: Optional[str],
    report_progress: Callable[[str], None],
) -> ExploreReport:
    nearby = f" ({nearby_label})" if nearby_label else ""
    report_progress(
        f"explore: from {origin} on {start.isoformat()} ({days}-day trip window){nearby}"
    )
    started = time.perf_counter()
    error: Optional[SearchError] = None
    destinations: tuple[ExploreDestination, ...] = ()
    try:
        places = tuple(
            client.fetch_explore(
                origin,
                start,
                adults=adults,
                cabin=cabin,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
            )
        )
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
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap_eur,
            airlines=airlines,
            exclude_airlines=exclude_airlines,
            alliances=alliances,
            exclude_alliances=exclude_alliances,
            via=parsed_via,
            exclude_via=parsed_exclude_via,
            depart_window=depart_window,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
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
        nearby_label=nearby_label,
    )


def search_explore(
    origin: str,
    start: date,
    *,
    days: int = 7,
    top: int = DEFAULT_EXPLORE_TOP,
    adults: int = 1,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin = "economy",
    max_stops: int = 1,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    nearby: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    source: Optional[ExploreSource] = None,
) -> ExploreReport | tuple[ExploreReport, ...]:
    validate_explore_window(start, days)
    if top <= 0:
        raise ValueError("top must be positive")
    if top > MAX_EXPLORE_TOP:
        raise ValueError(f"top is at most {MAX_EXPLORE_TOP}")
    if price_cap_eur is not None and price_cap_eur <= 0:
        raise ValueError("price_cap_eur must be positive")
    validate_layover_hours(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    parsed_via, parsed_exclude_via = _parse_via_pair(via, exclude_via)
    origin = origin.strip().upper()
    if not is_known_iata(origin):
        raise ValueError(f"unknown origin IATA code: {origin!r}")
    report_progress = progress or (lambda _: None)
    drop_unpriced = _named_shop_filters(
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
        via=parsed_via,
        exclude_via=parsed_exclude_via,
        depart_window=depart_window,
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    origins = expand_nearby_origins(origin, nearby=nearby)
    client = source or GoogleFlightsHttpSource()
    reports: list[ExploreReport] = []
    try:
        for code, label in origins:
            reports.append(
                _explore_for_origin(
                    client,
                    code,
                    start,
                    days=days,
                    top=top,
                    adults=adults,
                    children=children,
                    infants_in_seat=infants_in_seat,
                    infants_on_lap=infants_on_lap,
                    cabin=cabin,
                    max_stops=max_stops,
                    bags=bags,
                    carry_on=carry_on,
                    price_cap_eur=price_cap_eur,
                    airlines=airlines,
                    exclude_airlines=exclude_airlines,
                    alliances=alliances,
                    exclude_alliances=exclude_alliances,
                    parsed_via=parsed_via,
                    parsed_exclude_via=parsed_exclude_via,
                    depart_window=depart_window,
                    max_layover_hours=max_layover_hours,
                    min_layover_hours=min_layover_hours,
                    max_duration_hours=max_duration_hours,
                    drop_unpriced=drop_unpriced,
                    nearby_label=label,
                    report_progress=report_progress,
                )
            )
    finally:
        client.close()
    return _one_or_many(reports)


def write_explore_report_atomic(report: ExploreReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)


def write_explore_reports_atomic(reports: Sequence[ExploreReport], destination: Path) -> None:
    owned = tuple(reports)
    if len(owned) == 1:
        write_explore_report_atomic(owned[0], destination)
        return
    write_json_atomic({"queries": [row.to_dict() for row in owned]}, destination)


def _cheapest_price(
    source: ExploreSource,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    max_stops: int,
    adults: int,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
    cabin: FlightCabin,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> Optional[float]:
    airline_codes = tuple(airlines) if airlines is not None else None
    exclude_codes = tuple(exclude_airlines) if exclude_airlines is not None else None
    alliance_names = tuple(alliances) if alliances is not None else None
    exclude_alliance_names = tuple(exclude_alliances) if exclude_alliances is not None else None
    query = FlightQuery(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        max_stops=max_stops,
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        airlines=airline_codes,
        exclude_airlines=exclude_codes,
        alliances=alliance_names,
        exclude_alliances=exclude_alliance_names,
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
                depart_window=depart_window,
                max_layover_hours=max_layover_hours,
                min_layover_hours=min_layover_hours,
                max_duration_hours=max_duration_hours,
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
