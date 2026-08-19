"""Flight route parsing, ranking, and the Google Flights search loop."""

from __future__ import annotations

import random
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Protocol, Sequence, Tuple

from viajante.google_flights import (
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    GoogleFlightsSource,
    NoFlightsFound,
    RawFlightCard,
)
from viajante.models import (
    FetchBackend,
    FlightCabin,
    FlightLeg,
    FlightOffer,
    FlightQuery,
    MultiCity,
    QueryFailure,
    QueryResult,
    QuerySuccess,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
    Trip,
)
from viajante.orchestration import (
    MAX_ATTEMPTS,
    NON_RETRIABLE_CODES,
    inter_query_delay_seconds,
    retry_backoff_seconds,
    sweep_inter_query_delay_seconds,
)
from viajante.orchestration import (
    classify_failure as classify_provider_failure,
)
from viajante.parsers import (
    normalize_clock,
    parse_duration_hours,
    parse_price_eur,
    parse_stops_count,
)
from viajante.storage import default_state_dir, write_json_atomic

DEFAULT_BAGGAGE_BUFFER_EUR = 70
DEFAULT_TOP = 8

UNKNOWN_DURATION_SORTS_LAST = float("inf")

LOW_COST_NAMES = [
    "AirAsia",
    "Batik Air",
    "Cebu Pacific",
    "easyJet",
    "Eurowings",
    "IndiGo",
    "Jetstar",
    "Jin Air",
    "Norwegian",
    "Peach",
    "Pegasus",
    "Ryanair",
    "Scoot",
    "Transavia",
    "T'way",
    "Vietjet",
    "Volotea",
    "Vueling",
    "Wizz Air",
    "ZIPAIR",
]

NO_RESULTS_MESSAGE = "Google Flights returned no flights for this route and date."
REJECTED_MESSAGE = "Google Flights rejected this route or date (unknown airport or invalid query)."

FlightSort = Literal["ranked", "fare", "duration"]
FetchMode = Literal["auto", "sweep", "detail"]
TripKind = Literal["one-way", "rt", "multi"]
FlightPlan = Tuple[FlightQuery, ...] | RoundTrip | MultiCity
SWEEP_BATCH_THRESHOLD = 3
ROUTE_GRAMMAR = "ORIGIN-DESTINATION:DATE[,DATE...] or ORIGIN-DESTINATION:OUT:BACK"
RT_GRAMMAR = "ORIGIN-DESTINATION:OUT:BACK"
MULTI_GRAMMAR = "ORIGIN-DESTINATION:DATE"
_RT_DATES = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")
_ONE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TRIP_ALIASES = {
    "one-way": "one-way",
    "oneway": "one-way",
    "one_way": "one-way",
    "rt": "rt",
    "round-trip": "rt",
    "round_trip": "rt",
    "roundtrip": "rt",
    "multi": "multi",
    "multi-city": "multi",
    "multi_city": "multi",
    "multicity": "multi",
}
# Hide connections slower than this multiple of the fastest nonstop/shortest
# elapsed time. Short-haul overnight hops drop out; long-haul 1-stops stay.
RANKED_SLOW_CONNECTION_FACTOR = 3.0


def normalize_trip_kind(trip: str) -> TripKind:
    key = trip.strip().casefold().replace(" ", "-")
    try:
        return _TRIP_ALIASES[key]  # type: ignore[return-value]
    except KeyError:
        raise ValueError("trip must be 'one-way', 'rt', or 'multi'") from None


def resolve_fetch_mode(fetch: FetchMode, query_count: int) -> Literal["sweep", "detail"]:
    if fetch == "auto":
        return "sweep" if query_count >= SWEEP_BATCH_THRESHOLD else "detail"
    if fetch in ("sweep", "detail"):
        return fetch
    raise ValueError("fetch must be 'auto', 'sweep', or 'detail'")


def _needs_detail_fallback(result: QueryResult) -> bool:
    if isinstance(result, QuerySuccess):
        return result.raw_count == 0
    if not isinstance(result, QueryFailure):
        return False
    if result.error.code == SearchErrorCode.NO_RESULTS:
        return True
    if result.error.code == SearchErrorCode.BLOCKED:
        return True
    if result.error.code == SearchErrorCode.FETCH_FAILED:
        return True
    return False


def sweep_needs_fallback(report: SearchReport) -> bool:
    return any(_needs_detail_fallback(result) for result in report.queries)


def classify_failure(exc: BaseException) -> SearchError:
    if isinstance(exc, NoFlightsFound):
        return SearchError(
            code=SearchErrorCode.NO_RESULTS,
            message=NO_RESULTS_MESSAGE,
        )
    if isinstance(exc, GoogleFlightsRejected):
        return SearchError(
            code=SearchErrorCode.REJECTED,
            message=REJECTED_MESSAGE,
        )
    if isinstance(exc, GoogleFlightsBlocked):
        return SearchError(
            code=SearchErrorCode.BLOCKED,
            message=str(exc) or "Google Flights blocked the request.",
        )
    if isinstance(exc, GoogleFlightsMarkupError):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message=str(exc) or "Google Flights markup could not be parsed.",
        )
    return classify_provider_failure(exc, provider="Google Flights")


class _SourceConfig(Protocol):
    html_lang: str
    currency: str


class _FlightSource(Protocol):
    config: _SourceConfig

    def fetch(self, trip: Trip) -> Sequence[RawFlightCard]: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


def _trip_max_stops(trip: Trip) -> int:
    if isinstance(trip, (FlightQuery, RoundTrip)):
        return trip.max_stops
    return max(leg.max_stops for leg in trip.legs)


def _progress_label(trip: Trip) -> str:
    if isinstance(trip, FlightQuery):
        return f"{trip.origin} -> {trip.destination} {trip.departure_date.isoformat()}"
    return " / ".join(
        f"{leg.origin} -> {leg.destination} {leg.departure_date.isoformat()}" for leg in trip.legs
    )


def parse_route_specs(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int = 1,
    cabin: FlightCabin = "economy",
) -> Tuple[FlightQuery, ...]:
    if max_stops not in (0, 1, 2):
        raise ValueError("max_stops must be 0, 1, or 2")
    queries: list[FlightQuery] = []
    for spec in specs:
        try:
            pair, dates_part = spec.split(":", 1)
            origin, destination = pair.split("-", 1)
        except ValueError as exc:
            raise ValueError(f"invalid route: {spec!r}. Expected {ROUTE_GRAMMAR}") from exc
        if not origin or not destination:
            raise ValueError(f"invalid route: {spec!r}. Expected {ROUTE_GRAMMAR}")
        stripped = dates_part.strip()
        if not stripped:
            raise ValueError(f"invalid route: {spec!r}. Expected {ROUTE_GRAMMAR}")
        if "," in stripped and ":" in stripped:
            raise ValueError(
                f"invalid route: {spec!r}. Do not mix comma-separated dates with OUT:BACK"
            )
        rt_match = _RT_DATES.fullmatch(stripped)
        if rt_match:
            outbound = date.fromisoformat(rt_match.group(1))
            inbound = date.fromisoformat(rt_match.group(2))
            if inbound <= outbound:
                raise ValueError(f"return date must be after outbound in route {spec!r}")
            queries.append(
                FlightQuery(
                    origin=origin,
                    destination=destination,
                    departure_date=outbound,
                    max_stops=max_stops,
                    adults=adults,
                    cabin=cabin,
                )
            )
            queries.append(
                FlightQuery(
                    origin=destination,
                    destination=origin,
                    departure_date=inbound,
                    max_stops=max_stops,
                    adults=adults,
                    cabin=cabin,
                )
            )
            continue
        for date_text in stripped.split(","):
            date_text = date_text.strip()
            if not date_text:
                continue
            try:
                departure_date = date.fromisoformat(date_text)
            except ValueError as exc:
                raise ValueError(f"invalid date {date_text!r} in route {spec!r}") from exc
            queries.append(
                FlightQuery(
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    max_stops=max_stops,
                    adults=adults,
                    cabin=cabin,
                )
            )
        if not any(part.strip() for part in stripped.split(",")):
            raise ValueError(f"invalid route: {spec!r}. Expected {ROUTE_GRAMMAR}")
    if not queries:
        raise ValueError("at least one route is required")
    return tuple(queries)


def plan_unit_count(plan: FlightPlan) -> int:
    if isinstance(plan, (RoundTrip, MultiCity)):
        return 1
    return len(plan)


def parse_flight_plan(
    specs: Sequence[str],
    *,
    trip: str = "one-way",
    max_stops: int,
    adults: int = 1,
    cabin: FlightCabin = "economy",
) -> FlightPlan:
    kind = normalize_trip_kind(trip)
    if kind == "one-way":
        return parse_route_specs(specs, max_stops=max_stops, adults=adults, cabin=cabin)
    if kind == "rt":
        return _parse_round_trip_plan(specs, max_stops=max_stops, adults=adults, cabin=cabin)
    return _parse_multi_city_plan(specs, max_stops=max_stops, adults=adults, cabin=cabin)


def _split_route(spec: str, *, grammar: str) -> tuple[str, str, str]:
    try:
        pair, dates_part = spec.split(":", 1)
        origin, destination = pair.split("-", 1)
    except ValueError as exc:
        raise ValueError(f"invalid route: {spec!r}. Expected {grammar}") from exc
    if not origin or not destination or not dates_part.strip():
        raise ValueError(f"invalid route: {spec!r}. Expected {grammar}")
    return origin, destination, dates_part.strip()


def _parse_round_trip_plan(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
) -> RoundTrip:
    if len(specs) != 1:
        raise ValueError(f"--trip rt expects exactly one {RT_GRAMMAR}")
    spec = specs[0]
    if "," in spec:
        raise ValueError("--trip rt does not accept comma-separated dates")
    origin, destination, dates_part = _split_route(spec, grammar=RT_GRAMMAR)
    rt_match = _RT_DATES.fullmatch(dates_part)
    if rt_match is None:
        raise ValueError(f"--trip rt expects {RT_GRAMMAR}")
    outbound = date.fromisoformat(rt_match.group(1))
    inbound = date.fromisoformat(rt_match.group(2))
    return RoundTrip(
        origin,
        destination,
        outbound,
        inbound,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
    )


def _parse_multi_city_plan(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
) -> MultiCity:
    if not 2 <= len(specs) <= 6:
        raise ValueError(f"--trip multi expects 2 to 6 {MULTI_GRAMMAR} routes")
    legs: list[FlightLeg] = []
    for spec in specs:
        if "," in spec:
            raise ValueError("--trip multi does not accept comma-separated dates")
        origin, destination, dates_part = _split_route(spec, grammar=MULTI_GRAMMAR)
        if _RT_DATES.fullmatch(dates_part):
            raise ValueError("--trip multi does not accept OUT:BACK")
        if _ONE_DATE.fullmatch(dates_part) is None:
            raise ValueError(f"invalid route: {spec!r}. Expected {MULTI_GRAMMAR}")
        legs.append(
            FlightLeg(
                origin,
                destination,
                date.fromisoformat(dates_part),
                max_stops=max_stops,
            )
        )
    return MultiCity(tuple(legs), adults=adults, cabin=cabin)


def _normalize_airline(airline_text: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (airline_text or "").casefold())


_LOW_COST_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(_normalize_airline(name)) + r"\b") for name in LOW_COST_NAMES
)


def is_low_cost(airline_text: str) -> bool:
    text = _normalize_airline(airline_text)
    return any(pattern.search(text) for pattern in _LOW_COST_PATTERNS)


AIRLINE_CODE_ALIASES = {
    "AF": ("air france",),
    "BA": ("british airways",),
    "FR": ("ryanair",),
    "I2": ("iberia express",),
    "IB": ("iberia",),
    "KL": ("klm",),
    "LH": ("lufthansa",),
    "RK": ("ryanair",),
    "TO": ("transavia",),
    "TP": ("tap", "tap air portugal"),
    "U2": ("easyjet", "easy jet"),
    "UX": ("air europa",),
    "VY": ("vueling",),
    "W6": ("wizz", "wizz air"),
}


def parse_airline_codes(text: Optional[str]) -> Optional[Tuple[str, ...]]:
    if text is None:
        return None
    codes = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not codes:
        raise ValueError("airline list must not be empty")
    for code in codes:
        if not (2 <= len(code) <= 3 and code.isalnum()):
            raise ValueError(f"invalid airline code: {code!r}")
    return codes


def parse_depart_window(text: Optional[str]) -> Optional[Tuple[int, int]]:
    if text is None:
        return None
    try:
        start_text, end_text = text.split("-", 1)
        start, end = int(start_text), int(end_text)
    except ValueError as exc:
        raise ValueError("depart window must look like 6-20") from exc
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ValueError("depart window hours must be between 0 and 23")
    if start > end:
        raise ValueError("depart window start must be at or before the end hour")
    return start, end


def _airline_filter_hit(raw: RawFlightCard, token: str) -> bool:
    needle = token.strip().upper()
    codes = {code.upper() for code in (raw.airline_codes or ())}
    if needle in codes:
        return True
    name = _normalize_airline(raw.airline)
    if needle.casefold() in name:
        return True
    return any(alias in name for alias in AIRLINE_CODE_ALIASES.get(needle, ()))


def _passes_airline_filters(
    raw: RawFlightCard,
    *,
    airlines: Optional[Sequence[str]],
    exclude_airlines: Optional[Sequence[str]],
) -> bool:
    if airlines and not any(_airline_filter_hit(raw, token) for token in airlines):
        return False
    if exclude_airlines and any(_airline_filter_hit(raw, token) for token in exclude_airlines):
        return False
    return True


def _departure_hour(text: Optional[str]) -> Optional[int]:
    clock = normalize_clock(text)
    if not clock:
        return None
    try:
        return int(clock.split(":", 1)[0])
    except ValueError:
        return None


def _passes_depart_window(raw: RawFlightCard, window: Optional[Tuple[int, int]]) -> bool:
    if window is None:
        return True
    hour = _departure_hour(raw.departure)
    if hour is None:
        return False
    start, end = window
    return start <= hour <= end


def baggage_buffer_eur(
    airline_text: str,
    *,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
) -> int:
    return buffer_eur if is_low_cost(airline_text) else 0


def _eligible_stops(stops: Optional[str], max_stops: int) -> bool:
    count = parse_stops_count(stops)
    if count is not None:
        return count <= max_stops
    return max_stops >= 1


def _normalize_offer(
    raw: RawFlightCard,
    max_stops: int,
    *,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    max_layover_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    max_duration_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
) -> Optional[FlightOffer]:
    price_text = raw.price or ""
    price_eur = parse_price_eur(price_text)
    if price_eur is None or price_eur <= 0:
        return None
    if not _eligible_stops(raw.stops, max_stops):
        return None
    if not _passes_airline_filters(raw, airlines=airlines, exclude_airlines=exclude_airlines):
        return None
    if not _passes_depart_window(raw, depart_window):
        return None
    stops_count = parse_stops_count(raw.stops)
    layover_hours = raw.layover_hours
    duration_hours = parse_duration_hours(raw.duration)
    if (
        max_duration_hours is not None
        and duration_hours is not None
        and duration_hours > max_duration_hours
    ):
        return None
    if (
        max_layover_hours is not None
        and stops_count
        and stops_count > 0
        and layover_hours is not None
        and layover_hours > max_layover_hours
    ):
        return None
    if (
        min_layover_hours is not None
        and stops_count
        and stops_count > 0
        and layover_hours is not None
        and layover_hours < min_layover_hours
    ):
        return None
    airline = raw.airline or ""
    return FlightOffer(
        airline=raw.airline,
        departure=normalize_clock(raw.departure) or raw.departure,
        arrival=normalize_clock(raw.arrival) or raw.arrival,
        price=price_text,
        price_eur=price_eur,
        duration=raw.duration,
        duration_hours=duration_hours,
        stops=raw.stops,
        stops_count=stops_count,
        layover_city=raw.layover_city,
        layover_hours=layover_hours,
        flight_numbers=raw.flight_numbers,
        booking_token=raw.booking_token,
        baggage_buffer_eur=baggage_buffer_eur(airline, buffer_eur=buffer_eur),
        needs_bag_verify=is_low_cost(airline),
        legs=raw.legs,
    )


def _effective_cost(offer: FlightOffer) -> float:
    return offer.price_eur + offer.baggage_buffer_eur


def _fastest_duration(offers: Sequence[FlightOffer]) -> Optional[float]:
    hours = [offer.duration_hours for offer in offers if offer.duration_hours is not None]
    return min(hours) if hours else None


def _hide_slow_connections(
    offers: Sequence[FlightOffer],
) -> Tuple[FlightOffer, ...]:
    """Drop connections many times slower than the fastest nonstop/shortest offer."""
    nonstops = [offer for offer in offers if offer.stops_count == 0]
    baseline = _fastest_duration(nonstops) or _fastest_duration(offers)
    if baseline is None or baseline <= 0:
        return tuple(offers)
    limit = baseline * RANKED_SLOW_CONNECTION_FACTOR
    kept: list[FlightOffer] = []
    for offer in offers:
        duration = offer.duration_hours
        connecting = offer.stops_count is not None and offer.stops_count > 0
        if connecting and duration is not None and duration > limit:
            continue
        kept.append(offer)
    return tuple(kept) if kept else tuple(offers)


def _rank_offers(
    offers: Sequence[FlightOffer],
    *,
    top: int,
    sort: FlightSort = "ranked",
) -> Tuple[FlightOffer, ...]:
    rows = offers if sort != "ranked" else _hide_slow_connections(offers)

    def sort_key(offer: FlightOffer) -> tuple[float, float]:
        duration = offer.duration_hours
        if duration is None:
            duration = UNKNOWN_DURATION_SORTS_LAST
        if sort == "duration":
            return (duration, offer.price_eur)
        primary = offer.price_eur if sort == "fare" else _effective_cost(offer)
        return (primary, duration)

    rows = sorted(rows, key=sort_key)
    seen: set[tuple] = set()
    deduped: list[FlightOffer] = []
    for offer in rows:
        key = (
            offer.airline,
            normalize_clock(offer.departure) or offer.departure,
            normalize_clock(offer.arrival) or offer.arrival,
            offer.price_eur,
            offer.stops_count,
            offer.duration_hours,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(offer)
        if len(deduped) >= top:
            break
    return tuple(deduped)


def _run_search(
    trips: Sequence[Trip],
    *,
    top: int,
    source: _FlightSource,
    sleep: Callable[[float], None],
    random_gen: random.Random,
    now: Callable[[], datetime],
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    progress: Optional[Callable[[str], None]] = None,
    locale: str = "en",
    currency: str = "EUR",
    sort: FlightSort = "ranked",
    inter_query_delay: Callable[[random.Random], float] = inter_query_delay_seconds,
    fetch_backend: Optional[FetchBackend] = None,
    fetch_ms: Optional[int] = None,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
) -> SearchReport:
    report_progress = progress or (lambda _: None)
    results: list[QueryResult] = []
    for index, trip in enumerate(trips):
        report_progress(f"[{index + 1}/{len(trips)}] {_progress_label(trip)}")
        outcome: Optional[QueryResult] = None
        failure: Optional[SearchError] = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                cards = source.fetch(trip)
                eligible = [
                    offer
                    for raw in cards
                    if (
                        offer := _normalize_offer(
                            raw,
                            _trip_max_stops(trip),
                            buffer_eur=buffer_eur,
                            max_layover_hours=max_layover_hours,
                            min_layover_hours=min_layover_hours,
                            max_duration_hours=max_duration_hours,
                            airlines=airlines,
                            exclude_airlines=exclude_airlines,
                            depart_window=depart_window,
                        )
                    )
                    is not None
                ]
                outcome = QuerySuccess(
                    query=trip,
                    raw_count=len(cards),
                    eligible_count=len(eligible),
                    offers=_rank_offers(eligible, top=top, sort=sort),
                )
                break
            except Exception as exc:
                failure = classify_failure(exc)
                source.reset()
                if failure.code in NON_RETRIABLE_CODES:
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    sleep(retry_backoff_seconds(attempt, random_gen))
        if outcome is None:
            outcome = QueryFailure(
                query=trip,
                error=failure
                or SearchError(
                    code=SearchErrorCode.FETCH_FAILED,
                    message="Google Flights search failed.",
                ),
            )
            report_progress(f"  {outcome.error.code.value}: {outcome.error.message}")
        results.append(outcome)
        if index + 1 < len(trips):
            sleep(inter_query_delay(random_gen))
    return SearchReport(
        searched_at=now(),
        queries=tuple(results),
        locale=locale,
        currency=currency,
        fetch_backend=fetch_backend,
        fetch_ms=fetch_ms,
    )


def _attach_fetch_meta(
    report: SearchReport,
    *,
    fetch_backend: FetchBackend,
    fetch_ms: int,
) -> SearchReport:
    return SearchReport(
        searched_at=report.searched_at,
        queries=report.queries,
        locale=report.locale,
        currency=report.currency,
        fetch_backend=fetch_backend,
        fetch_ms=fetch_ms,
    )


def _search_with_source(
    trips: Sequence[Trip],
    *,
    source: _FlightSource,
    top: int,
    buffer_eur: int,
    progress: Optional[Callable[[str], None]],
    sort: FlightSort,
    inter_query_delay: Callable[[random.Random], float],
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
) -> SearchReport:
    try:
        return _run_search(
            trips,
            top=top,
            source=source,
            sleep=time.sleep,
            random_gen=random.Random(),
            now=lambda: datetime.now(timezone.utc),
            buffer_eur=buffer_eur,
            progress=progress,
            locale=source.config.html_lang,
            currency=source.config.currency,
            sort=sort,
            inter_query_delay=inter_query_delay,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
            airlines=airlines,
            exclude_airlines=exclude_airlines,
            depart_window=depart_window,
        )
    finally:
        source.close()


def search_flights(
    queries: Sequence[Trip],
    *,
    top: int = DEFAULT_TOP,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    progress: Optional[Callable[[str], None]] = None,
    sort: FlightSort = "ranked",
    fetch: FetchMode = "auto",
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
) -> SearchReport:
    if not queries:
        raise ValueError("at least one query is required")
    if top <= 0:
        raise ValueError("top must be positive")
    if buffer_eur < 0:
        raise ValueError("buffer_eur must not be negative")
    if max_layover_hours is not None and max_layover_hours < 0:
        raise ValueError("max_layover_hours must not be negative")
    if min_layover_hours is not None and min_layover_hours < 0:
        raise ValueError("min_layover_hours must not be negative")
    if max_duration_hours is not None and max_duration_hours < 0:
        raise ValueError("max_duration_hours must not be negative")
    if (
        min_layover_hours is not None
        and max_layover_hours is not None
        and min_layover_hours > max_layover_hours
    ):
        raise ValueError("min layover must be at or below max layover")
    if sort not in ("ranked", "fare", "duration"):
        raise ValueError("sort must be 'ranked', 'fare', or 'duration'")
    if fetch not in ("auto", "sweep", "detail"):
        raise ValueError("fetch must be 'auto', 'sweep', or 'detail'")
    trips = tuple(queries)
    planned = resolve_fetch_mode(fetch, len(trips))
    if any(isinstance(trip, MultiCity) for trip in trips) and planned == "detail":
        if fetch == "auto":
            planned = "sweep"
        else:
            raise ValueError("--trip multi does not support --fetch detail yet")
    report_progress = progress or (lambda _: None)
    started = time.perf_counter()
    if planned == "sweep":
        noun = "query" if len(trips) == 1 else "queries"
        report_progress(f"fetch: sweep ({len(trips)} {noun})")
        report = _search_with_source(
            trips,
            source=GoogleFlightsHttpSource(),
            top=top,
            buffer_eur=buffer_eur,
            progress=progress,
            sort=sort,
            inter_query_delay=sweep_inter_query_delay_seconds,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
            airlines=airlines,
            exclude_airlines=exclude_airlines,
            depart_window=depart_window,
        )
        retry_indexes = [
            index for index, result in enumerate(report.queries) if _needs_detail_fallback(result)
        ]
        if retry_indexes:
            report_progress("sweep empty/markup/block; falling back to detail")
            retry_trips = tuple(trips[index] for index in retry_indexes)
            detail_report = _search_with_source(
                retry_trips,
                source=GoogleFlightsSource(default_state_dir()),
                top=top,
                buffer_eur=buffer_eur,
                progress=progress,
                sort=sort,
                inter_query_delay=inter_query_delay_seconds,
                max_layover_hours=max_layover_hours,
                min_layover_hours=min_layover_hours,
                max_duration_hours=max_duration_hours,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                depart_window=depart_window,
            )
            merged = list(report.queries)
            for index, detail_result in zip(retry_indexes, detail_report.queries, strict=True):
                merged[index] = detail_result
            report = SearchReport(
                searched_at=report.searched_at,
                queries=tuple(merged),
                locale=report.locale,
                currency=report.currency,
            )
            backend: FetchBackend = "sweep_then_detail"
        else:
            backend = "sweep"
    else:
        noun = "query" if len(trips) == 1 else "queries"
        report_progress(f"fetch: detail ({len(trips)} {noun})")
        report = _search_with_source(
            trips,
            source=GoogleFlightsSource(default_state_dir()),
            top=top,
            buffer_eur=buffer_eur,
            progress=progress,
            sort=sort,
            inter_query_delay=inter_query_delay_seconds,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
            airlines=airlines,
            exclude_airlines=exclude_airlines,
            depart_window=depart_window,
        )
        backend = "detail"
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return _attach_fetch_meta(report, fetch_backend=backend, fetch_ms=fetch_ms)


def write_report_atomic(report: SearchReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)
