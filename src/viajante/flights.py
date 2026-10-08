"""One-shot flight search and the Google Flights search loop."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional, Protocol, Sequence, Tuple

from viajante.browser import chromium_installed, playwright_available
from viajante.control import (
    SearchDeadline,
    checkpoint,
    controlled,
    current_control,
    interruptible_sleep,
)
from viajante.flight_evidence import _report_with_evidence, _stamp_google_flights_urls
from viajante.flight_filters import (
    NO_OFFER_FILTERS,
    OfferFilters,
    parse_code_list,
    parse_offer_filters,
)
from viajante.flight_offers import (
    FlightSort,
    _offer_sort_key,
    _rank_offers,
    _recommend,
    _shop_offers,
    compare_nonstop_vs_one_stop,
    validate_sort,
)
from viajante.flight_packages import _attach_missing_legs, _passes_packaged_filters
from viajante.flight_routes import (
    _is_route_spec,
    _overlay_carrier_filters,
    _progress_label,
    as_trips,
    drop_excluded_airport_trips,
    expand_nearby_trips,
    keep_included_dest_trips,
    overlay_trip_fields,
    parse_flight_plan,
)
from viajante.google_flights import (
    GoogleFlightsBlocked,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    GoogleFlightsSource,
    NoFlightsFound,
    RawFlightCard,
    SweepTransportError,
)
from viajante.google_flights_public import GoogleFlightsUnsupported
from viajante.google_flights_public import PublicGoogleFlightsHttpSource as GoogleFlightsHttpSource
from viajante.history import recorded_flights
from viajante.models import (
    FetchBackend,
    FlightCabin,
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
    normalize_country,
)
from viajante.orchestration import (
    BROWSER_INSTALL_HINT,
    MAX_ATTEMPTS,
    inter_query_delay_seconds,
    retry_backoff_seconds,
    should_retry,
    sweep_inter_query_delay_seconds,
)
from viajante.orchestration import classify_failure as classify_provider_failure
from viajante.quote import first_origin_iata, resolve_baggage_buffer, resolve_quote_currency
from viajante.ratelimit import cooldown_until
from viajante.storage import default_state_dir

DEFAULT_TOP = 8


NO_RESULTS_MESSAGE = "Google Flights returned no flights for this route and date."


REJECTED_MESSAGE = "Google Flights rejected this query; the provider did not identify the cause."


FetchMode = Literal["auto", "sweep", "detail"]


def resolve_fetch_mode(fetch: FetchMode) -> Literal["sweep", "detail"]:
    if fetch == "detail":
        return "detail"
    if fetch in ("auto", "sweep"):
        return "sweep"
    raise ValueError("fetch must be 'auto', 'sweep', or 'detail'")


def _needs_detail_fallback(result: QueryResult) -> bool:
    if isinstance(result, QuerySuccess):
        return result.raw_count == 0
    if not isinstance(result, QueryFailure) or result.error.rate_limited:
        return False
    if result.error.code == SearchErrorCode.NO_RESULTS:
        return True
    if result.error.code == SearchErrorCode.BLOCKED:
        return False
    if result.error.code == SearchErrorCode.FETCH_FAILED:
        return True
    return False


def classify_failure(exc: BaseException) -> SearchError:
    if isinstance(exc, NoFlightsFound):
        return SearchError(
            code=SearchErrorCode.NO_RESULTS,
            message=NO_RESULTS_MESSAGE,
        )
    if isinstance(exc, GoogleFlightsUnsupported):
        return SearchError(
            code=SearchErrorCode.REJECTED,
            message=str(exc),
            diagnostics={
                "request_sent": False,
                "attempts": 0,
                "http_status": None,
                "rpc_status": None,
                "endpoint": "www.google.com/travel/flights",
                "cooldown_basis": None,
            },
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
            rate_limited=exc.status == 429,
            retry_until=cooldown_until(str(exc)) if exc.status == 429 else None,
            diagnostics=getattr(exc, "diagnostics", None),
        )
    if isinstance(exc, GoogleFlightsMarkupError):
        return SearchError(
            code=SearchErrorCode.MARKUP_DRIFT,
            message=str(exc) or "Google Flights markup could not be parsed.",
        )
    return classify_provider_failure(exc)


class _SourceConfig(Protocol):
    html_lang: str
    currency: str


class _FlightSource(Protocol):
    config: _SourceConfig

    def fetch(self, trip: Trip) -> Sequence[RawFlightCard]: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


def _empty_excluded_flight_report(
    trips: Sequence[Trip],
    *,
    currency: str,
) -> SearchReport:
    """Named origin/dest excluded and nearby did not keep an owned alt."""
    seeds = tuple(trip for trip in trips if not getattr(trip, "nearby_label", None))
    if not seeds:
        seeds = tuple(trips[:1])
    return SearchReport(
        searched_at=datetime.now(timezone.utc),
        queries=tuple(
            QuerySuccess(
                query=trip, raw_count=0, eligible_count=0, offers=(), empty_reason="filtered_out"
            )
            for trip in seeds
        ),
        currency=currency,
        fetch_ms=0,
    )


def _run_search(
    trips: Sequence[Trip],
    *,
    top: int,
    source: _FlightSource,
    sleep: Callable[[float], None],
    random_gen: random.Random,
    now: Callable[[], datetime],
    baggage_buffer: int = 0,
    progress: Optional[Callable[[str], None]] = None,
    locale: str = "en",
    currency: str,
    fetch_backend: Optional[FetchBackend] = None,
    sort: FlightSort = "ranked",
    inter_query_delay: Callable[[random.Random], float] = inter_query_delay_seconds,
    filters: OfferFilters = NO_OFFER_FILTERS,
    retry_backoff: Callable[[int, random.Random], float] = retry_backoff_seconds,
) -> SearchReport:
    report_progress = progress or (lambda _: None)
    results: list[QueryResult] = []

    def _success_from_cards(trip: Trip, cards: Sequence[RawFlightCard]) -> QuerySuccess:
        packaged = len(trip.legs) > 1
        initial_filters = (
            replace(filters, via=None, exclude_via=None, no_overnight=None, require_overnight=None)
            if packaged
            else filters
        )
        pool, eligible = _shop_offers(cards, trip, initial_filters, baggage_buffer=baggage_buffer)
        if packaged:
            candidates = sorted(eligible, key=lambda offer: _offer_sort_key(offer, sort))
            eligible = []
            for start in range(0, len(candidates), top):
                completed = _attach_missing_legs(trip, candidates[start : start + top], source)
                eligible.extend(
                    offer for offer in completed if _passes_packaged_filters(offer, trip, filters)
                )
                if len(_rank_offers(eligible, top=top, sort=sort)) >= top:
                    break
            pool = eligible
        ranked = _rank_offers(eligible, top=top, sort=sort)
        recommendation = _recommend(trip, pool, filters, currency=currency, packaged=packaged)
        metadata_for = getattr(source, "metadata_for", None)
        page_error, scope_bound = metadata_for(trip) if callable(metadata_for) else (None, False)
        return QuerySuccess(
            query=trip,
            raw_count=len(cards),
            eligible_count=len(eligible),
            offers=ranked,
            stops_compare=compare_nonstop_vs_one_stop(eligible),
            recommendation=recommendation,
            page_errors=(classify_failure(page_error),) if page_error is not None else (),
            scope_bound=scope_bound,
        )

    def _stamp(result: QueryResult) -> QueryResult:
        return _stamp_google_flights_urls(
            result,
            html_lang=getattr(source.config, "html_lang", "en"),
            currency=currency,
            country=getattr(source.config, "country", None),
        )

    def _maybe_reset(failure: SearchError) -> None:
        # Empty/rejected/markup are owned outcomes. Drop TLS only when a
        # retry might succeed, or when the session may be poisoned (blocked).
        if should_retry(failure) or failure.code == SearchErrorCode.BLOCKED:
            source.reset()

    def _search_one(trip: Trip, *, start_attempt: int = 0) -> QueryResult:
        outcome: Optional[QueryResult] = None
        failure: Optional[SearchError] = None
        for attempt in range(start_attempt, MAX_ATTEMPTS):
            try:
                checkpoint()
                outcome = _success_from_cards(trip, source.fetch(trip))
                break
            except Exception as exc:
                failure = classify_failure(exc)
                _maybe_reset(failure)
                if not should_retry(failure, transport=isinstance(exc, SweepTransportError)):
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
        return _stamp(outcome)

    fetch_batch = getattr(source, "fetch_many", None)
    if (
        callable(fetch_batch)
        and len(trips) > 1
        and all(isinstance(trip, (FlightQuery, RoundTrip)) for trip in trips)
    ):
        for index, trip in enumerate(trips):
            report_progress(f"[{index + 1}/{len(trips)}] {_progress_label(trip)}")
        try:
            checkpoint()
            batch_rows = fetch_batch(list(trips))
        except Exception:
            batch_rows = None
        if batch_rows is not None:
            for trip, cards_or_exc in zip(trips, batch_rows, strict=True):
                if not isinstance(cards_or_exc, BaseException):
                    try:
                        results.append(_stamp(_success_from_cards(trip, cards_or_exc)))
                    except SearchDeadline as exc:
                        results.append(
                            _stamp(QueryFailure(query=trip, error=classify_failure(exc)))
                        )
                    continue
                failure = classify_failure(cards_or_exc)
                transport = isinstance(cards_or_exc, SweepTransportError)
                if not should_retry(failure, transport=transport):
                    _maybe_reset(failure)
                    outcome = QueryFailure(query=trip, error=failure)
                    report_progress(f"  {outcome.error.code.value}: {outcome.error.message}")
                    results.append(_stamp(outcome))
                    continue
                results.append(_search_one(trip, start_attempt=1))
            searched_at = now()
            return _report_with_evidence(
                results,
                searched_at=searched_at,
                locale=locale,
                currency=currency,
                fetch_backend=fetch_backend,
                fetch_ms=None,
            )

    for index, trip in enumerate(trips):
        report_progress(f"[{index + 1}/{len(trips)}] {_progress_label(trip)}")
        results.append(_search_one(trip))
        if index + 1 < len(trips):
            sleep(inter_query_delay(random_gen))
    searched_at = now()
    return _report_with_evidence(
        results,
        searched_at=searched_at,
        locale=locale,
        currency=currency,
        fetch_backend=fetch_backend,
        fetch_ms=None,
    )


def get_flights(
    query: str | Sequence[str] | Trip | Sequence[Trip],
    *,
    trip: str = "one-way",
    max_stops: Optional[int] = None,
    adults: Optional[int] = None,
    cabin: Optional[FlightCabin] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    children: Optional[int] = None,
    infants_in_seat: Optional[int] = None,
    infants_on_lap: Optional[int] = None,
    nearby: bool = False,
    top: int = DEFAULT_TOP,
    baggage_buffer: Optional[int] = None,
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
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    exclude_airports: Optional[Sequence[str]] = None,
    include_airports: Optional[Sequence[str]] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    cancel: Optional[threading.Event] = None,
    deadline_seconds: Optional[float] = None,
) -> SearchReport:
    """One-shot flight search from a route spec or trips.

    A string like ``ORIGIN-DEST:DATE`` (or a sequence of those) is parsed like
    the CLI. Any other string raises ``ValueError``.
    """
    search_kw: dict[str, Any] = {
        "top": top,
        "baggage_buffer": baggage_buffer,
        "progress": progress,
        "sort": sort,
        "fetch": fetch,
        "max_layover_hours": max_layover_hours,
        "min_layover_hours": min_layover_hours,
        "max_duration_hours": max_duration_hours,
        "airlines": airlines,
        "exclude_airlines": exclude_airlines,
        "alliances": alliances,
        "exclude_alliances": exclude_alliances,
        "depart_window": depart_window,
        "arrive_before": arrive_before,
        "depart_after": depart_after,
        "via": via,
        "exclude_via": exclude_via,
        "no_overnight": no_overnight,
        "require_overnight": require_overnight,
        "exclude_airports": exclude_airports,
        "include_airports": include_airports,
        "currency": currency,
        "country": country,
        "proxy": proxy,
        "cancel": cancel,
        "deadline_seconds": deadline_seconds,
    }

    def _search(trips: Sequence[Trip]) -> SearchReport:
        return search_flights(
            overlay_trip_fields(
                trips,
                adults=adults,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                cabin=cabin,
                max_stops=max_stops,
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
            ),
            **search_kw,
        )

    if isinstance(query, str) and not _is_route_spec(query):
        raise ValueError(
            "get_flights takes a route spec like JFK-LHR:YYYY-MM-DD or Trip objects; "
            "turning a natural-language prompt into a route is the caller's job"
        )
    if isinstance(query, str):
        specs: Sequence[str] = (query.strip(),)
    elif isinstance(query, (FlightQuery, RoundTrip, MultiCity)):
        trips = expand_nearby_trips((query,), nearby=nearby)
        return _search(trips)
    else:
        items = tuple(query)
        if items and isinstance(items[0], str):
            specs = items  # type: ignore[assignment]
        else:
            trips = expand_nearby_trips(as_trips(items), nearby=nearby)  # type: ignore[arg-type]
            return _search(trips)
    parsed_plan = parse_flight_plan(
        specs,
        trip=trip,
        max_stops=max_stops if max_stops is not None else 1,
        adults=adults if adults is not None else 1,
        cabin=cabin if cabin is not None else "economy",
        bags=bags,
        carry_on=carry_on,
        price_cap=price_cap,
        children=children if children is not None else 0,
        infants_in_seat=infants_in_seat if infants_in_seat is not None else 0,
        infants_on_lap=infants_on_lap if infants_on_lap is not None else 0,
    )
    trips = expand_nearby_trips(as_trips(parsed_plan), nearby=nearby)
    return _search(trips)


def validate_flight_search_args(
    *,
    top: int,
    sort: str,
    fetch: str,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    exclude_airports: Optional[Sequence[str]] = None,
    include_airports: Optional[Sequence[str]] = None,
) -> Tuple[OfferFilters, Optional[Tuple[str, ...]], Optional[Tuple[str, ...]]]:
    """Every check on search arguments alone, before any lock, fetch or state write.

    Shared by `search_flights`, the MCP tool and saved watches so they reject the same input.
    """
    if top <= 0:
        raise ValueError("top must be positive")
    filters = parse_offer_filters(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        via=via,
        exclude_via=exclude_via,
        no_overnight=no_overnight,
        require_overnight=require_overnight,
    )
    parsed_exclude = parse_code_list(exclude_airports, role="exclude-airports")
    parsed_include = parse_code_list(include_airports, role="include-airports")
    validate_sort(sort)
    if fetch not in ("auto", "sweep", "detail"):
        raise ValueError("fetch must be 'auto', 'sweep', or 'detail'")
    return filters, parsed_exclude, parsed_include


@controlled
@recorded_flights
def search_flights(
    queries: Sequence[Trip],
    *,
    top: int = DEFAULT_TOP,
    baggage_buffer: Optional[int] = None,
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
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
    exclude_airports: Optional[Sequence[str]] = None,
    include_airports: Optional[Sequence[str]] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    cancel: Optional[threading.Event] = None,
    deadline_seconds: Optional[float] = None,
) -> SearchReport:
    if not queries:
        raise ValueError("at least one query is required")
    filters, parsed_exclude_airports, parsed_include_airports = validate_flight_search_args(
        top=top,
        sort=sort,
        fetch=fetch,
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        via=via,
        exclude_via=exclude_via,
        no_overnight=no_overnight,
        require_overnight=require_overnight,
        exclude_airports=exclude_airports,
        include_airports=include_airports,
    )
    currency = resolve_quote_currency(currency, first_origin_iata(queries[0]))
    baggage_buffer = resolve_baggage_buffer(baggage_buffer, currency)
    country = normalize_country(country)
    original = tuple(queries)
    kept = keep_included_dest_trips(original, parsed_include_airports)
    kept = drop_excluded_airport_trips(kept, parsed_exclude_airports)
    if not kept:
        return _empty_excluded_flight_report(original, currency=currency)
    trips = _overlay_carrier_filters(
        kept,
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
    )
    planned = resolve_fetch_mode(fetch)
    report_progress = progress or (lambda _: None)
    started = time.perf_counter()

    def _zero_backoff(_attempt: int, _rng: random.Random) -> float:
        return 0.0

    if planned == "sweep":
        source: _FlightSource = GoogleFlightsHttpSource(
            currency=currency, country=country, proxy=proxy
        )
        delay = sweep_inter_query_delay_seconds
        backoff = _zero_backoff
    else:
        source = GoogleFlightsSource(default_state_dir(), currency=currency, country=country)
        delay = inter_query_delay_seconds
        backoff = retry_backoff_seconds

    def execute(
        trips_to_search: Sequence[Trip],
        *,
        source: _FlightSource,
        inter_query_delay: Callable[[random.Random], float],
        fetch_backend: FetchBackend,
        retry_backoff: Callable[[int, random.Random], float] = retry_backoff_seconds,
    ) -> SearchReport:
        try:
            return _run_search(
                trips_to_search,
                top=top,
                source=source,
                sleep=interruptible_sleep,
                random_gen=random.Random(),
                now=lambda: datetime.now(timezone.utc),
                baggage_buffer=baggage_buffer,
                progress=progress,
                locale=source.config.html_lang,
                currency=source.config.currency,
                fetch_backend=fetch_backend,
                sort=sort,
                inter_query_delay=inter_query_delay,
                filters=filters,
                retry_backoff=retry_backoff,
            )
        finally:
            source.close()

    noun = "query" if len(trips) == 1 else "queries"
    report_progress(f"fetch: {planned} ({len(trips)} {noun})")
    report = execute(
        trips,
        source=source,
        inter_query_delay=delay,
        fetch_backend=planned,
        retry_backoff=backoff,
    )
    backend: FetchBackend = planned
    if (
        planned == "sweep"
        and getattr(source, "transport", None) != "public_page"
        and playwright_available()
    ):
        retry_indexes = [
            index for index, result in enumerate(report.queries) if _needs_detail_fallback(result)
        ]
        control = current_control()
        if control is not None and control.expired():
            if retry_indexes:
                control.mark_cut()
            retry_indexes = []
        if retry_indexes and not chromium_installed():
            report_progress(BROWSER_INSTALL_HINT)
            retry_indexes = []
        if retry_indexes:
            report_progress("sweep empty/markup/block; falling back to detail")
            retry_trips = tuple(trips[index] for index in retry_indexes)
            detail_report = execute(
                retry_trips,
                source=GoogleFlightsSource(default_state_dir(), currency=currency, country=country),
                inter_query_delay=inter_query_delay_seconds,
                fetch_backend="detail",
            )
            merged = list(report.queries)
            for index, detail_result in zip(retry_indexes, detail_report.queries, strict=True):
                merged[index] = detail_result
            report = replace(report, queries=tuple(merged))
            backend = "sweep_then_detail"
    fetch_ms = max(0, int((time.perf_counter() - started) * 1000))
    return replace(report, fetch_backend=backend, fetch_ms=fetch_ms)
