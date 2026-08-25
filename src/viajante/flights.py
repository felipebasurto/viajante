"""Flight route parsing, ranking, and the Google Flights search loop."""

from __future__ import annotations

import random
import re
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol, Sequence, Tuple

from viajante.airports import get_airport, is_known_iata, same_city_iata
from viajante.carriers import AIRLINE_CODE_ALIASES
from viajante.google_flights import (
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    GoogleFlightsSource,
    NoFlightsFound,
    RawFlightCard,
    google_flights_url,
)
from viajante.models import (
    DateCalendarSummary,
    FetchBackend,
    FlightCabin,
    FlightLeg,
    FlightOffer,
    FlightQuery,
    MultiCity,
    QueryFailure,
    QueryResult,
    QuerySuccess,
    RawJourneyLeg,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
    StopsCompare,
    StopsCompareSide,
    Trip,
    normalize_country,
    normalize_currency,
    owned_calendar_summary,
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
from viajante.typical import TYPICAL_WINDOW_DAYS, with_typical

DEFAULT_BAGGAGE_BUFFER_EUR = 70
DEFAULT_TOP = 8

UNKNOWN_DURATION_SORTS_LAST = float("inf")
FLIGHT_SORTS: tuple[str, ...] = (
    "ranked",
    "fare",
    "price",
    "duration",
    "departure",
    "arrival",
)
_MINUTES_IN_DAY = 24 * 60

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

FlightSort = Literal["ranked", "fare", "price", "duration", "departure", "arrival"]
_CLOCK_TOKEN = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)
FetchMode = Literal["auto", "sweep", "detail"]
TripKind = Literal["one-way", "rt", "multi"]
FlightPlan = Tuple[FlightQuery, ...] | RoundTrip | MultiCity
TypicalTrip = FlightQuery | RoundTrip
TypicalCacheKey = tuple[str, str, date, Optional[int], int, int, int, int, int, str]
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
        base = f"{trip.origin} -> {trip.destination} {trip.departure_date.isoformat()}"
        return f"{base} ({trip.nearby_label})" if trip.nearby_label else base
    if isinstance(trip, RoundTrip):
        base = (
            f"{trip.origin} -> {trip.destination} {trip.departure_date.isoformat()}"
            f" / {trip.return_date.isoformat()}"
        )
        return f"{base} ({trip.nearby_label})" if trip.nearby_label else base
    return " / ".join(
        f"{leg.origin} -> {leg.destination} {leg.departure_date.isoformat()}" for leg in trip.legs
    )


def _nearby_city(code: str) -> Optional[str]:
    airport = get_airport(code)
    if airport is None or not airport.city.strip():
        return None
    return airport.city


def _nearby_pair_label(
    origin: str,
    dest: str,
    origin_codes: Tuple[str, ...],
    dest_codes: Tuple[str, ...],
) -> Optional[str]:
    parts: list[str] = []
    if len(origin_codes) > 1:
        city = _nearby_city(origin)
        parts.append(f"nearby {city} {origin}" if city else f"nearby {origin}")
    if len(dest_codes) > 1:
        city = _nearby_city(dest)
        parts.append(f"nearby {city} {dest}" if city else f"nearby {dest}")
    return "; ".join(parts) if parts else None


def nearby_notes(trips: Sequence[Trip]) -> Tuple[str, ...]:
    """Stderr legend for `--nearby` expands. Named open-jaw is not expanded."""
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    for trip in trips:
        if isinstance(trip, MultiCity) or not getattr(trip, "nearby_label", None):
            continue
        for code in (trip.origin, trip.destination):
            airport = get_airport(code)
            if airport is None or not airport.city.strip():
                continue
            key = (airport.city, airport.country)
            codes = same_city_iata(code)
            if len(codes) < 2 or key in seen:
                continue
            seen.add(key)
            notes.append(f"nearby {airport.city}: {', '.join(sorted(codes))}")
    return tuple(notes)


def _expand_flight_query(query: FlightQuery) -> Tuple[FlightQuery, ...]:
    origin_codes = same_city_iata(query.origin) or (query.origin,)
    dest_codes = same_city_iata(query.destination) or (query.destination,)
    expanded: list[FlightQuery] = []
    for origin in origin_codes:
        for destination in dest_codes:
            if origin == destination:
                continue
            label = _nearby_pair_label(origin, destination, origin_codes, dest_codes)
            expanded.append(
                replace(query, origin=origin, destination=destination, nearby_label=label)
            )
    return tuple(expanded) if expanded else (query,)


def _expand_round_trip(trip: RoundTrip) -> Tuple[RoundTrip, ...]:
    origin_codes = same_city_iata(trip.origin) or (trip.origin,)
    dest_codes = same_city_iata(trip.destination) or (trip.destination,)
    expanded: list[RoundTrip] = []
    for origin in origin_codes:
        for destination in dest_codes:
            if origin == destination:
                continue
            label = _nearby_pair_label(origin, destination, origin_codes, dest_codes)
            expanded.append(
                replace(trip, origin=origin, destination=destination, nearby_label=label)
            )
    return tuple(expanded) if expanded else (trip,)


def expand_nearby_trips(trips: Sequence[Trip], *, nearby: bool = False) -> Tuple[Trip, ...]:
    """Fan out one-way and mirrored RT queries to owned same-city IATA.

    Default off. Packaged open-jaw / multi-city keeps every named airport
    (LGW stays LGW). Does not invent codes or mix a mirrored RT into an
    open jaw.
    """
    if not nearby:
        return tuple(trips)
    expanded: list[Trip] = []
    for trip in trips:
        if isinstance(trip, FlightQuery):
            expanded.extend(_expand_flight_query(trip))
        elif isinstance(trip, RoundTrip):
            expanded.extend(_expand_round_trip(trip))
        else:
            expanded.append(trip)
    return tuple(expanded) if expanded else tuple(trips)


def expand_nearby_origins(
    origin: str, *, nearby: bool = False
) -> Tuple[tuple[str, Optional[str]], ...]:
    """Fan an explore origin out to owned same-city IATA.

    Default off. Returns ``(code, nearby_label)`` pairs. A city with no
    second major stays the named seed, unlabeled. Does not invent codes.
    """
    seed = origin.strip().upper()
    if not nearby:
        return ((seed, None),)
    codes = same_city_iata(seed)
    if len(codes) < 2:
        return ((codes[0] if codes else seed, None),)
    city = _nearby_city(seed)
    labeled: list[tuple[str, Optional[str]]] = []
    for code in codes:
        label = f"nearby {city} {code}" if city else f"nearby {code}"
        labeled.append((code, label))
    return tuple(labeled)


def nearby_origin_notes(origin: str) -> Tuple[str, ...]:
    """Stderr legend for explore ``--nearby``. No invented codes."""
    codes = same_city_iata(origin)
    if len(codes) < 2:
        return ()
    airport = get_airport(origin)
    if airport is None or not airport.city.strip():
        return ()
    return (f"nearby {airport.city}: {', '.join(sorted(codes))}",)


def parse_route_specs(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> Tuple[FlightQuery, ...]:
    if max_stops not in (0, 1, 2):
        raise ValueError("max_stops must be 0, 1, or 2")
    occupancy = {
        "adults": adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
        "cabin": cabin,
        "bags": bags,
        "carry_on": carry_on,
        "price_cap_eur": price_cap_eur,
    }
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
                    **occupancy,
                )
            )
            queries.append(
                FlightQuery(
                    origin=destination,
                    destination=origin,
                    departure_date=inbound,
                    max_stops=max_stops,
                    **occupancy,
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
                    **occupancy,
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
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> FlightPlan:
    kind = normalize_trip_kind(trip)
    occupancy = {
        "adults": adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
        "cabin": cabin,
        "bags": bags,
        "carry_on": carry_on,
        "price_cap_eur": price_cap_eur,
    }
    if kind == "one-way":
        return parse_route_specs(
            specs,
            max_stops=max_stops,
            **occupancy,
        )
    if kind == "rt":
        return _parse_round_trip_plan(
            specs,
            max_stops=max_stops,
            **occupancy,
        )
    return _parse_multi_city_plan(
        specs,
        max_stops=max_stops,
        **occupancy,
    )


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
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> RoundTrip | MultiCity:
    if len(specs) == 2:
        return _parse_open_jaw_rt_package(
            specs,
            max_stops=max_stops,
            adults=adults,
            cabin=cabin,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap_eur,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
        )
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
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
    )


def _parse_open_jaw_rt_package(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> MultiCity:
    """Two DATE legs under --trip rt are one open-jaw package, not a mirrored RT.

    ``ORIGIN-DEST:OUT:BACK`` always returns from DEST. YVR-LHR out + LGW-YVR
    back cannot use that grammar without dropping LGW, so we POST a two-leg
    multi-city package instead. No invented fare.
    """
    return _parse_multi_city_plan(
        specs,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
    )


def _parse_multi_city_plan(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int,
    cabin: FlightCabin,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
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
    return MultiCity(
        tuple(legs),
        adults=adults,
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
        cabin=cabin,
        bags=bags,
        carry_on=carry_on,
        price_cap_eur=price_cap_eur,
    )


_AIRLINE_STRIP = re.compile(r"[^a-z0-9 ]+")


def _normalize_airline(airline_text: Optional[str]) -> str:
    return _AIRLINE_STRIP.sub("", (airline_text or "").casefold())


_LOW_COST_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(_normalize_airline(name)) for name in LOW_COST_NAMES) + r")\b"
)


def is_low_cost(airline_text: str) -> bool:
    return _LOW_COST_PATTERN.search(_normalize_airline(airline_text)) is not None


def _clock_token_minutes(text: str) -> Optional[int]:
    match = _CLOCK_TOKEN.fullmatch(text.strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").casefold()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _looks_like_clock_token(text: str) -> bool:
    stripped = text.strip()
    return ":" in stripped or bool(re.search(r"(?i)[ap]m$", stripped))


def parse_depart_window(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse a local departure window as inclusive minutes from midnight.

    Hour form `6-20` keeps the whole start and end hours (06:00–20:59).
    Clock form `06:00-20:00` is exact on both ends.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00")
    try:
        start_text, end_text = raw.split("-", 1)
    except ValueError as exc:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00") from exc
    start_text = start_text.strip()
    end_text = end_text.strip()
    if _looks_like_clock_token(start_text) or _looks_like_clock_token(end_text):
        start = _clock_token_minutes(start_text)
        end = _clock_token_minutes(end_text)
        if start is None or end is None:
            raise ValueError("depart window must look like 6-20 or 06:00-20:00")
        if start > end:
            raise ValueError("depart window start must be at or before the end")
        return start, end
    try:
        start_hour, end_hour = int(start_text), int(end_text)
    except ValueError as exc:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00") from exc
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ValueError("depart window hours must be between 0 and 23")
    if start_hour > end_hour:
        raise ValueError("depart window start must be at or before the end hour")
    return start_hour * 60, end_hour * 60 + 59


def parse_named_clock(text: Optional[str], *, role: str = "clock") -> Optional[int]:
    """Parse a named HH:MM clock as minutes from midnight. Unnamed stays unset."""
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        raise ValueError(f"{role} must look like HH:MM")
    minutes = _clock_token_minutes(raw)
    if minutes is None:
        minutes = _clock_minutes(raw)
    if minutes is None:
        raise ValueError(f"{role} must look like HH:MM")
    return minutes


def validate_layover_hours(
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> None:
    """Reject negative layover/duration caps and a min above the max."""
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


def parse_via_airports(text: Optional[str], *, role: str = "via") -> Optional[Tuple[str, ...]]:
    """Parse comma-separated IATA codes for a via / exclude-via post-filter."""
    if text is None:
        return None
    codes = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not codes:
        raise ValueError(f"{role} list must not be empty")
    parsed: list[str] = []
    for code in codes:
        if len(code) != 3 or not code.isalpha() or not is_known_iata(code):
            raise ValueError(f"unknown {role} IATA code: {code!r}")
        if code not in parsed:
            parsed.append(code)
    return tuple(parsed)


def _via_aliases(code: str) -> Tuple[str, ...]:
    aliases = [code.casefold()]
    airport = get_airport(code)
    if airport is not None and airport.city:
        city = airport.city.strip().casefold()
        if city and city not in aliases:
            aliases.append(city)
    return tuple(aliases)


def _token_matches_via(token: str, code: str) -> bool:
    folded = token.strip().casefold()
    if not folded:
        return False
    return folded in _via_aliases(code)


def _connection_tokens(raw: RawFlightCard) -> Tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: Optional[str]) -> None:
        if not value:
            return
        text = value.strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        tokens.append(text)

    add(raw.layover_city)
    for leg in raw.legs:
        for layover in leg.layovers:
            add(layover.city)
        airports: list[str] = []
        for segment in leg.segments:
            if segment.origin:
                airports.append(segment.origin)
            if segment.destination:
                airports.append(segment.destination)
        if len(airports) >= 3:
            for code in airports[1:-1]:
                add(code)
    return tuple(tokens)


def _passes_via_filters(
    raw: RawFlightCard,
    *,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
) -> bool:
    if not via and not exclude_via:
        return True
    tokens = _connection_tokens(raw)
    if exclude_via and tokens:
        for code in exclude_via:
            if any(_token_matches_via(token, code) for token in tokens):
                return False
    if via:
        if not tokens:
            return False
        if not any(any(_token_matches_via(token, code) for token in tokens) for code in via):
            return False
    return True


def _overlay_carrier_filters(
    trips: Sequence[Trip],
    *,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
) -> Tuple[Trip, ...]:
    overlay: dict[str, Tuple[str, ...]] = {}
    if airlines:
        overlay["airlines"] = tuple(airlines)
    if exclude_airlines:
        overlay["exclude_airlines"] = tuple(exclude_airlines)
    if alliances:
        overlay["alliances"] = tuple(alliances)
    if exclude_alliances:
        overlay["exclude_alliances"] = tuple(exclude_alliances)
    if not overlay:
        return tuple(trips)
    return tuple(replace(trip, **overlay) for trip in trips)


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


def _clock_minutes(text: Optional[str]) -> Optional[int]:
    clock = normalize_clock(text)
    if not clock:
        return None
    try:
        hour_text, minute_text = clock.split(":", 1)
        minutes = int(hour_text) * 60 + int(minute_text)
    except ValueError:
        return None
    if not (0 <= minutes < _MINUTES_IN_DAY):
        return None
    return minutes


def _passes_depart_window(raw: RawFlightCard, window: Optional[Tuple[int, int]]) -> bool:
    if window is None:
        return True
    minutes = _clock_minutes(raw.departure)
    if minutes is None:
        return False
    start, end = window
    return start <= minutes <= end


def _passes_arrive_before(clock_text: Optional[str], bound: Optional[int]) -> bool:
    if bound is None:
        return True
    minutes = _clock_minutes(clock_text)
    if minutes is None:
        return False
    return minutes <= bound


def _passes_depart_after(clock_text: Optional[str], bound: Optional[int]) -> bool:
    if bound is None:
        return True
    minutes = _clock_minutes(clock_text)
    if minutes is None:
        return False
    return minutes >= bound


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


def _passes_bag_request(
    raw: RawFlightCard,
    *,
    bags: Optional[int],
    carry_on: Optional[int],
) -> bool:
    if bags is not None and raw.checked_bags is not None and raw.checked_bags < bags:
        return False
    if carry_on is not None and raw.carry_on is not None and raw.carry_on < carry_on:
        return False
    return True


def _bag_evidence(
    raw: RawFlightCard,
    airline_text: str,
    *,
    buffer_eur: int,
    requested: bool,
) -> tuple[int, bool]:
    knows = raw.checked_bags is not None or raw.carry_on is not None
    if knows:
        return 0, False
    needs_verify = is_low_cost(airline_text)
    if requested:
        return 0, needs_verify
    return (buffer_eur if needs_verify else 0), needs_verify


def _normalize_offer(
    raw: RawFlightCard,
    max_stops: int,
    *,
    buffer_eur: int = DEFAULT_BAGGAGE_BUFFER_EUR,
    max_layover_hours: Optional[float] = None,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    max_duration_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap_eur: Optional[int] = None,
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
    if not _passes_bag_request(raw, bags=bags, carry_on=carry_on):
        return None
    if price_cap_eur is not None and price_eur > price_cap_eur:
        return None
    if not _passes_via_filters(raw, via=via, exclude_via=exclude_via):
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
    requested = bags is not None or carry_on is not None
    buffer, needs_verify = _bag_evidence(raw, airline, buffer_eur=buffer_eur, requested=requested)
    legs = tuple(
        RawJourneyLeg(
            departure=normalize_clock(leg.departure) or leg.departure,
            arrival=normalize_clock(leg.arrival) or leg.arrival,
            duration=leg.duration,
            stops=leg.stops,
            segments=leg.segments,
            layovers=leg.layovers,
        )
        for leg in raw.legs
    )
    offer = FlightOffer(
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
        baggage_buffer_eur=buffer,
        needs_bag_verify=needs_verify,
        legs=legs,
        checked_bags=raw.checked_bags,
        carry_on=raw.carry_on,
    )
    if not _passes_arrive_before(offer.arrival, arrive_before):
        return None
    if not _passes_depart_after(offer.departure, depart_after):
        return None
    return offer


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


def _cheapest_by_fare(offers: Sequence[FlightOffer]) -> Optional[FlightOffer]:
    if not offers:
        return None

    def sort_key(offer: FlightOffer) -> tuple[float, float]:
        duration = offer.duration_hours
        if duration is None:
            duration = UNKNOWN_DURATION_SORTS_LAST
        return (offer.price_eur, duration)

    return min(offers, key=sort_key)


def compare_nonstop_vs_one_stop(offers: Sequence[FlightOffer]) -> Optional[StopsCompare]:
    """Cheapest cabin fare per stop bucket from one parsed set. No extra fetch."""
    nonstop = _cheapest_by_fare([offer for offer in offers if offer.stops_count == 0])
    one_stop = _cheapest_by_fare([offer for offer in offers if offer.stops_count == 1])
    if nonstop is None and one_stop is None:
        return None
    return StopsCompare(
        nonstop=StopsCompareSide.from_offer(nonstop) if nonstop is not None else None,
        one_stop=StopsCompareSide.from_offer(one_stop) if one_stop is not None else None,
    )


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
        if sort == "departure":
            minutes = _clock_minutes(offer.departure)
            primary = float(minutes) if minutes is not None else UNKNOWN_DURATION_SORTS_LAST
            return (primary, offer.price_eur)
        if sort == "arrival":
            minutes = _clock_minutes(offer.arrival)
            primary = float(minutes) if minutes is not None else UNKNOWN_DURATION_SORTS_LAST
            return (primary, offer.price_eur)
        primary = offer.price_eur if sort in ("fare", "price") else _effective_cost(offer)
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


def _typical_window(start: date) -> tuple[date, date]:
    end = date.fromordinal(start.toordinal() + TYPICAL_WINDOW_DAYS - 1)
    return start, end


def _typical_nights(trip: Trip) -> Optional[int]:
    if isinstance(trip, RoundTrip):
        return (trip.return_date - trip.departure_date).days
    return None


def _typical_trip(trip: Trip) -> Optional[TypicalTrip]:
    if isinstance(trip, (FlightQuery, RoundTrip)):
        return trip
    return None


def _typical_cache_key(trip: TypicalTrip) -> TypicalCacheKey:
    start, _end = _typical_window(trip.departure_date)
    return (
        trip.origin,
        trip.destination,
        start,
        _typical_nights(trip),
        trip.max_stops,
        trip.adults,
        trip.children,
        trip.infants_in_seat,
        trip.infants_on_lap,
        trip.cabin,
    )


def _summary_from_calendar_days(
    days: Optional[Sequence[Any]],
    start: date,
    end: date,
) -> Optional[DateCalendarSummary]:
    if not days:
        return None
    in_window = [
        (row.departure_date, row.price_eur) for row in days if start <= row.departure_date <= end
    ]
    return owned_calendar_summary(in_window)


def _calendar_summary_from_source(
    source: _FlightSource,
    trip: TypicalTrip,
    cache: dict[TypicalCacheKey, Optional[DateCalendarSummary]],
) -> Optional[DateCalendarSummary]:
    fetch_calendar = getattr(source, "fetch_calendar", None)
    if not callable(fetch_calendar):
        return None
    key = _typical_cache_key(trip)
    if key in cache:
        return cache[key]
    start, end = _typical_window(trip.departure_date)
    try:
        days = fetch_calendar(trip, start, end)
    except Exception:
        cache[key] = None
        return None
    summary = _summary_from_calendar_days(days, start, end)
    cache[key] = summary
    return summary


def _stamp_google_flights_urls(
    result: QueryResult,
    *,
    html_lang: str,
    currency: str,
    country: Optional[str],
) -> QueryResult:
    query_url = google_flights_url(
        result.query, html_lang=html_lang, currency=currency, country=country
    )
    if isinstance(result, QuerySuccess):
        offers = tuple(
            replace(
                offer,
                google_flights_url=google_flights_url(
                    result.query,
                    html_lang=html_lang,
                    currency=currency,
                    country=country,
                    booking_token=offer.booking_token,
                ),
            )
            for offer in result.offers
        )
        return replace(result, offers=offers, google_flights_url=query_url)
    return replace(result, google_flights_url=query_url)


def _stamp_typical(
    trip: Trip,
    offers: Tuple[FlightOffer, ...],
    source: _FlightSource,
    cache: dict[TypicalCacheKey, Optional[DateCalendarSummary]],
) -> Tuple[FlightOffer, ...]:
    seed = _typical_trip(trip)
    if not offers or seed is None:
        return offers
    summary = _calendar_summary_from_source(source, seed, cache)
    if summary is None:
        return offers
    return tuple(
        with_typical(
            offer,
            summary.median_eur,
            cheapest_date=summary.cheapest_date,
            cheapest_eur=summary.min_eur,
        )
        for offer in offers
    )


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
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    retry_backoff: Callable[[int, random.Random], float] = retry_backoff_seconds,
) -> SearchReport:
    report_progress = progress or (lambda _: None)
    results: list[QueryResult] = []
    typical_cache: dict[TypicalCacheKey, Optional[DateCalendarSummary]] = {}

    def _success_from_cards(trip: Trip, cards: Sequence[RawFlightCard]) -> QuerySuccess:
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
                    airlines=airlines if airlines is not None else trip.airlines,
                    exclude_airlines=(
                        exclude_airlines if exclude_airlines is not None else trip.exclude_airlines
                    ),
                    depart_window=depart_window,
                    arrive_before=arrive_before,
                    depart_after=depart_after,
                    via=via,
                    exclude_via=exclude_via,
                    bags=trip.bags,
                    carry_on=trip.carry_on,
                    price_cap_eur=trip.price_cap_eur,
                )
            )
            is not None
        ]
        ranked = _rank_offers(eligible, top=top, sort=sort)
        return QuerySuccess(
            query=trip,
            raw_count=len(cards),
            eligible_count=len(eligible),
            offers=_stamp_typical(trip, ranked, source, typical_cache),
            stops_compare=compare_nonstop_vs_one_stop(eligible),
        )

    def _maybe_reset(failure: SearchError) -> None:
        # Empty/rejected/markup are owned outcomes. Drop TLS only when a
        # retry might succeed, or when the session may be poisoned (blocked).
        if failure.code not in NON_RETRIABLE_CODES or failure.code == SearchErrorCode.BLOCKED:
            source.reset()

    def _search_one(trip: Trip, *, start_attempt: int = 0) -> QueryResult:
        outcome: Optional[QueryResult] = None
        failure: Optional[SearchError] = None
        for attempt in range(start_attempt, MAX_ATTEMPTS):
            try:
                fetch_pair = getattr(source, "fetch_with_calendar", None)
                seed = _typical_trip(trip)
                if callable(fetch_pair) and seed is not None:
                    start, end = _typical_window(seed.departure_date)
                    cards, days = fetch_pair(seed, start, end)
                    typical_cache[_typical_cache_key(seed)] = _summary_from_calendar_days(
                        days, start, end
                    )
                else:
                    cards = source.fetch(trip)
                outcome = _success_from_cards(trip, cards)
                break
            except Exception as exc:
                failure = classify_failure(exc)
                _maybe_reset(failure)
                if failure.code in NON_RETRIABLE_CODES:
                    break
                if attempt + 1 < MAX_ATTEMPTS:
                    delay = retry_backoff(attempt, random_gen)
                    if delay > 0:
                        sleep(delay)
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
        return _stamp_google_flights_urls(
            outcome,
            html_lang=getattr(source.config, "html_lang", "en"),
            currency=currency,
            country=getattr(source.config, "country", None),
        )

    fetch_batch = getattr(source, "fetch_many_with_calendar", None)
    if (
        callable(fetch_batch)
        and len(trips) > 1
        and all(_typical_trip(trip) is not None for trip in trips)
    ):
        for index, trip in enumerate(trips):
            report_progress(f"[{index + 1}/{len(trips)}] {_progress_label(trip)}")
        jobs = []
        for trip in trips:
            seed = _typical_trip(trip)
            if seed is None:
                continue
            start, end = _typical_window(seed.departure_date)
            jobs.append((seed, start, end))
        try:
            batch_rows = fetch_batch(jobs)
        except Exception:
            batch_rows = None
        if batch_rows is not None:
            for trip, (cards_or_exc, days) in zip(trips, batch_rows, strict=True):
                seed = _typical_trip(trip)
                if not isinstance(cards_or_exc, BaseException):
                    if seed is not None:
                        start, end = _typical_window(seed.departure_date)
                        typical_cache[_typical_cache_key(seed)] = _summary_from_calendar_days(
                            days, start, end
                        )
                    results.append(
                        _stamp_google_flights_urls(
                            _success_from_cards(trip, cards_or_exc),
                            html_lang=getattr(source.config, "html_lang", "en"),
                            currency=currency,
                            country=getattr(source.config, "country", None),
                        )
                    )
                    continue
                failure = classify_failure(cards_or_exc)
                if failure.code in NON_RETRIABLE_CODES:
                    _maybe_reset(failure)
                    outcome = QueryFailure(query=trip, error=failure)
                    report_progress(f"  {outcome.error.code.value}: {outcome.error.message}")
                    results.append(
                        _stamp_google_flights_urls(
                            outcome,
                            html_lang=getattr(source.config, "html_lang", "en"),
                            currency=currency,
                            country=getattr(source.config, "country", None),
                        )
                    )
                    continue
                results.append(_search_one(trip, start_attempt=1))
            return SearchReport(
                searched_at=now(),
                queries=tuple(results),
                locale=locale,
                currency=currency,
                fetch_backend=fetch_backend,
                fetch_ms=fetch_ms,
            )

    for index, trip in enumerate(trips):
        report_progress(f"[{index + 1}/{len(trips)}] {_progress_label(trip)}")
        results.append(_search_one(trip))
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
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    retry_backoff: Callable[[int, random.Random], float] = retry_backoff_seconds,
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
            arrive_before=arrive_before,
            depart_after=depart_after,
            via=via,
            exclude_via=exclude_via,
            retry_backoff=retry_backoff,
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
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    currency: str = "EUR",
    country: Optional[str] = None,
) -> SearchReport:
    if not queries:
        raise ValueError("at least one query is required")
    if top <= 0:
        raise ValueError("top must be positive")
    if buffer_eur < 0:
        raise ValueError("buffer_eur must not be negative")
    validate_layover_hours(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    via = parse_via_airports(",".join(via), role="via") if via else None
    exclude_via = (
        parse_via_airports(",".join(exclude_via), role="exclude-via") if exclude_via else None
    )
    if via and exclude_via and set(via) & set(exclude_via):
        raise ValueError("via and exclude-via must not share a code")
    if sort not in FLIGHT_SORTS:
        raise ValueError(
            "sort must be 'ranked', 'fare', 'price', 'duration', 'departure', or 'arrival'"
        )
    if fetch not in ("auto", "sweep", "detail"):
        raise ValueError("fetch must be 'auto', 'sweep', or 'detail'")
    currency = normalize_currency(currency)
    country = normalize_country(country)
    trips = _overlay_carrier_filters(
        tuple(queries),
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
    )
    planned = resolve_fetch_mode(fetch, len(trips))
    if fetch == "auto" and any(
        trip.airlines or trip.exclude_airlines or trip.alliances or trip.exclude_alliances
        for trip in trips
    ):
        planned = "sweep"
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
            source=GoogleFlightsHttpSource(currency=currency, country=country),
            top=top,
            buffer_eur=buffer_eur,
            progress=progress,
            sort=sort,
            inter_query_delay=sweep_inter_query_delay_seconds,
            retry_backoff=lambda _attempt, _rng: 0.0,
            max_layover_hours=max_layover_hours,
            min_layover_hours=min_layover_hours,
            max_duration_hours=max_duration_hours,
            airlines=airlines,
            exclude_airlines=exclude_airlines,
            depart_window=depart_window,
            arrive_before=arrive_before,
            depart_after=depart_after,
            via=via,
            exclude_via=exclude_via,
        )
        retry_indexes = [
            index for index, result in enumerate(report.queries) if _needs_detail_fallback(result)
        ]
        if retry_indexes:
            report_progress("sweep empty/markup/block; falling back to detail")
            retry_trips = tuple(trips[index] for index in retry_indexes)
            detail_report = _search_with_source(
                retry_trips,
                source=GoogleFlightsSource(default_state_dir(), currency=currency, country=country),
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
                arrive_before=arrive_before,
                depart_after=depart_after,
                via=via,
                exclude_via=exclude_via,
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
            source=GoogleFlightsSource(default_state_dir(), currency=currency, country=country),
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
            arrive_before=arrive_before,
            depart_after=depart_after,
            via=via,
            exclude_via=exclude_via,
        )
        backend = "detail"
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return _attach_fetch_meta(report, fetch_backend=backend, fetch_ms=fetch_ms)


def write_report_atomic(report: SearchReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)
