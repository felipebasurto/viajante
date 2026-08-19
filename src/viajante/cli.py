"""Argument parsing, terminal tables, and optional JSON saves."""

from __future__ import annotations

import argparse
import calendar
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence, Tuple

from viajante.airports import is_known_iata, lookup_airports
from viajante.bench import run_bench
from viajante.dates import (
    MAX_DATE_WINDOW_DAYS,
    parse_route_pair,
    search_dates,
    validate_date_window,
    write_dates_report_atomic,
)
from viajante.explore import (
    DEFAULT_EXPLORE_TOP,
    search_explore,
    validate_explore_window,
    write_explore_report_atomic,
)
from viajante.flights import (
    DEFAULT_BAGGAGE_BUFFER_EUR,
    DEFAULT_TOP,
    FlightSort,
    normalize_trip_kind,
    parse_airline_codes,
    parse_depart_window,
    parse_flight_plan,
    search_flights,
    write_report_atomic,
)
from viajante.google_flights import build_itinerary_url
from viajante.hotels import search_hotels, write_hotel_report_atomic
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    DateCalendarReport,
    ExploreReport,
    FlightOffer,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    LodgingKind,
    MultiCity,
    QueryFailure,
    QuerySuccess,
    RoundTrip,
    Trip,
)

FLIGHTS_EXAMPLES = """\
Examples:
  viajante flights JFK-LHR:2026-09-15
  viajante flights JFK-NRT:2026-10-09:2026-10-20
  viajante flights --trip rt LAX-NRT:2026-10-12:2026-10-26 --fetch sweep
  viajante flights SYD-AKL:2026-11-03 AKL-SYD:2026-11-10 --max-stops 0
  viajante flights JFK-LHR:2026-09-15,2026-09-16 --top 5 --sort fare --save results/search.json
  viajante flights JFK-LHR:2026-09-15 --fetch sweep
  viajante flights LAX-NRT:2026-10-12 --fetch sweep --max-layover 8
  viajante flights JFK-LHR:2026-09-15 --fetch detail
  viajante flights JFK-LHR:2026-09-15 --exclude-airlines F9,NK --depart-window 7-12 --fetch sweep
  viajante flights JFK-LHR:2026-09-15 --airlines BA,AA --sort duration
  viajante flights JFK-LHR:2026-09-15 --max-duration 16 --min-layover 1 --max-layover 8
"""

DATES_EXAMPLES = """\
Examples:
  viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --fetch sweep
"""

EXPLORE_EXAMPLES = """\
Examples:
  viajante explore JFK --from 2026-09-15 --days 7
  viajante explore NRT --month 2026-10
"""

AIRPORTS_EXAMPLES = """\
Examples:
  viajante airports tokyo
  viajante airports london
  viajante airports JFK
"""

BENCH_EXAMPLES = """\
Examples:
  viajante bench
"""

HOTELS_EXAMPLES = """\
Examples:
  viajante hotels Tokyo 2026-10-12 2026-10-16
  viajante hotels "Mexico City" 2026-12-04 2026-12-10 --top 5
  viajante hotels Tokyo 2026-10-12 2026-10-16 --entire-home --min-rating 8.5
  viajante hotels Tokyo 2026-10-12 2026-10-16 --compare-cancellation
  viajante hotels Tokyo 2026-10-12 2026-10-16 --save results/hotels.json
  viajante hotels Tokyo 2026-10-12 2026-10-16 --source google --top 3
"""


def _parse_and_validate(args: argparse.Namespace) -> Tuple[Trip, ...]:
    if args.top <= 0:
        raise ValueError("--top must be a positive integer")
    if args.baggage_buffer < 0:
        raise ValueError("--baggage-buffer must not be negative")
    if args.adults < 1:
        raise ValueError("--adults must be at least 1")
    if args.max_layover is not None and args.max_layover < 0:
        raise ValueError("--max-layover must not be negative")
    if args.min_layover is not None and args.min_layover < 0:
        raise ValueError("--min-layover must not be negative")
    if args.max_duration is not None and args.max_duration < 0:
        raise ValueError("--max-duration must not be negative")
    if (
        args.min_layover is not None
        and args.max_layover is not None
        and args.min_layover > args.max_layover
    ):
        raise ValueError("--min-layover must be at or below --max-layover")
    parse_airline_codes(args.airlines)
    parse_airline_codes(args.exclude_airlines)
    parse_depart_window(args.depart_window)
    plan = parse_flight_plan(
        args.routes,
        trip=args.trip,
        max_stops=args.max_stops,
        adults=args.adults,
        cabin=args.cabin,
    )
    today = date.today()
    for departure in _plan_departure_dates(plan):
        if departure < today:
            raise ValueError(f"departure date is in the past: {departure.isoformat()}")
    return _as_trips(plan)


def _as_trips(plan: object) -> Tuple[Trip, ...]:
    if isinstance(plan, (RoundTrip, MultiCity)):
        return (plan,)
    return tuple(plan)  # type: ignore[arg-type]


def _plan_departure_dates(plan: object) -> Tuple[date, ...]:
    if isinstance(plan, (RoundTrip, MultiCity)):
        return tuple(leg.departure_date for leg in plan.legs)
    return tuple(query.departure_date for query in plan)  # type: ignore[union-attr]


def _format_stops(stops_count: Optional[int]) -> str:
    if stops_count is None:
        return "?"
    if stops_count == 0:
        return "direct"
    return f"{stops_count} stop" + ("s" if stops_count > 1 else "")


def _format_layover_hours(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def _format_stops_with_layover(offer: FlightOffer) -> str:
    label = _format_stops(offer.stops_count)
    if offer.layover_city:
        label = f"{label} {offer.layover_city}"
    if offer.layover_hours is not None:
        label = f"{label} {_format_layover_hours(offer.layover_hours)}"
    return label


def _format_airline(airline: Optional[str]) -> str:
    text = airline or "?"
    return text if len(text) <= 40 else text[:39] + "…"


_CLOCK_ON_DATE = re.compile(r"\s+on\s+\S.*$", re.IGNORECASE)


def _format_clock(text: Optional[str]) -> str:
    if not text:
        return "?"
    cleaned = _CLOCK_ON_DATE.sub("", text.replace("\xa0", " ")).strip()
    return cleaned or text.strip()


def _ranked_total(offer: FlightOffer) -> float:
    return offer.price_eur + offer.baggage_buffer_eur


def _sort_value(offer: FlightOffer, sort: FlightSort) -> float:
    if sort == "fare":
        return offer.price_eur
    if sort == "duration":
        return offer.duration_hours if offer.duration_hours is not None else float("inf")
    return _ranked_total(offer)


def _format_ranking_columns(offer: FlightOffer) -> str:
    fare = f"{offer.price_eur:>7.0f} €"
    if offer.baggage_buffer_eur:
        return f"{fare}  {_ranked_total(offer):>7.0f} € ranked"
    extra = "  [baggage?]" if offer.needs_bag_verify else ""
    return f"{fare}{extra}"


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def _build_hotel_queries(args: argparse.Namespace) -> Tuple[HotelQuery, ...]:
    check_in = _parse_iso_date(args.check_in, "check-in")
    check_out = _parse_iso_date(args.check_out, "check-out")
    if args.compare_cancellation and args.allow_non_refundable:
        raise ValueError("--compare-cancellation cannot be combined with --allow-non-refundable")
    source = getattr(args, "source", "booking")
    if source == "google" and args.compare_cancellation:
        raise ValueError("--compare-cancellation cannot be combined with --source google")
    if source == "google" and args.min_rating is not None and args.min_rating > 5:
        raise ValueError("--min-rating must be at most 5 with --source google")
    shared = {
        "location": args.location,
        "check_in": check_in,
        "check_out": check_out,
        "adults": args.adults,
        "rooms": args.rooms,
        "min_rating": args.min_rating,
        "entire_home": args.entire_home,
    }
    if args.compare_cancellation:
        return (
            HotelQuery(**shared, free_cancellation=True),
            HotelQuery(**shared, free_cancellation=False),
        )
    return (HotelQuery(**shared, free_cancellation=not args.allow_non_refundable),)


def _validate_hotel_args(args: argparse.Namespace) -> Tuple[HotelQuery, ...]:
    if args.top <= 0:
        raise ValueError("--top must be a positive integer")
    return _build_hotel_queries(args)


def _print_best_pairs(report, sort: FlightSort) -> None:
    results = report.queries
    index = 0
    while index + 1 < len(results):
        outbound, inbound = results[index], results[index + 1]
        if not (
            isinstance(outbound, QuerySuccess)
            and isinstance(inbound, QuerySuccess)
            and outbound.query.origin == inbound.query.destination
            and outbound.query.destination == inbound.query.origin
            and outbound.offers
            and inbound.offers
        ):
            index += 1
            continue
        out_offer = min(outbound.offers, key=lambda offer: _sort_value(offer, sort))
        back_offer = min(inbound.offers, key=lambda offer: _sort_value(offer, sort))
        out_value = _sort_value(out_offer, sort)
        back_value = _sort_value(back_offer, sort)
        unit = "ranked" if sort == "ranked" else "fare"
        print(
            f"\nBest pair ({unit}): "
            f"{outbound.query.origin}->{outbound.query.destination} {out_value:.0f} € {unit} + "
            f"{inbound.query.origin}->{inbound.query.destination} {back_value:.0f} € {unit} = "
            f"{out_value + back_value:.0f} €"
        )
        index += 2


def _query_header(query: Trip) -> str:
    if isinstance(query, RoundTrip):
        return (
            f"\n=== {query.origin} -> {query.destination}  "
            f"{query.departure_date.isoformat()} / {query.return_date.isoformat()} "
            f"(round-trip, max {query.max_stops} stop(s)) ==="
        )
    if isinstance(query, MultiCity):
        path = " / ".join(
            f"{leg.origin}->{leg.destination} {leg.departure_date.isoformat()}"
            for leg in query.legs
        )
        return f"\n=== {path} (multi-city) ==="
    return (
        f"\n=== {query.origin} -> {query.destination}  "
        f"{query.departure_date.isoformat()} (max {query.max_stops} stop(s)) ==="
    )


def _print_offer_legs(offer: FlightOffer) -> None:
    if len(offer.legs) < 2:
        return
    for index, leg in enumerate(offer.legs[1:], start=2):
        times = f"{_format_clock(leg.departure)} -> {_format_clock(leg.arrival)}"
        label = "return" if len(offer.legs) == 2 else f"leg {index}"
        print(
            f"    {label}  {leg.duration or '?':<12} {times:<18} {_format_airline(offer.airline)}"
        )


def _print_report(report, *, sort: FlightSort = "ranked") -> None:
    any_success = False
    for result in report.queries:
        print(_query_header(result.query))
        if isinstance(result, QuerySuccess):
            any_success = True
            if not result.offers:
                print("  (no eligible offers)")
            for offer in result.offers:
                times = f"{_format_clock(offer.departure)} -> {_format_clock(offer.arrival)}"
                print(
                    f"  {_format_ranking_columns(offer)}  {offer.duration or '?':<12} "
                    f"{_format_stops_with_layover(offer):<16} {times:<18} "
                    f"{_format_airline(offer.airline)}"
                )
                _print_offer_legs(offer)
                if offer.booking_token:
                    print(f"    {build_itinerary_url(offer.booking_token)}")
            print(
                f"  Raw: {result.raw_count}; "
                f"eligible: {result.eligible_count}; "
                f"shown: {len(result.offers)}"
            )
        elif isinstance(result, QueryFailure):
            print(f"  ERROR: {result.error.message}")
    _print_best_pairs(report, sort)
    if any_success:
        print("\nVerify checked baggage on Google Flights before booking.")


def _format_hotel_filter_gloss(query: HotelQuery) -> str:
    parts: list[str] = []
    if query.free_cancellation:
        parts.append("Free cancellation required")
    else:
        parts.append("Non-refundable rates allowed")
    if query.entire_home:
        parts.append(
            "Entire homes/apartments required (cards with unknown property type may remain)"
        )
    if query.min_rating is not None:
        parts.append(f"Minimum rating {query.min_rating:g}")
    return "; ".join(parts)


def _print_hotel_filters(
    query: HotelQuery,
    applied: AppliedHotelFilters,
    *,
    provider: str = "booking.com",
) -> None:
    chips = "; ".join(applied.chips) if applied.chips else "(none)"
    label = "Booking chips" if provider == "booking.com" else "Google chips"
    print(f"  Filters: {_format_hotel_filter_gloss(query)}")
    print(f"  {label}: {chips}")


def _format_cancellation_evidence(
    evidence: CancellationEvidence,
    *,
    query: HotelQuery,
    applied: AppliedHotelFilters,
) -> str:
    if evidence is CancellationEvidence.FREE:
        return "Cancellation: free"
    if evidence is CancellationEvidence.NON_REFUNDABLE:
        return "Cancellation: non-refundable"
    if query.free_cancellation and (
        "oos=1" in applied.chips or "free_cancellation=1" in applied.chips
    ):
        return "Cancellation: filter applied; card silent"
    return "Cancellation: unknown"


def _format_lodging_kind(kind: LodgingKind) -> str:
    if kind is LodgingKind.ENTIRE_HOME:
        return "Lodging: entire home"
    if kind is LodgingKind.PRIVATE_ROOM:
        return "Lodging: private room"
    if kind is LodgingKind.HOTEL:
        return "Lodging: hotel"
    return "Lodging: unknown"


def _format_unit_hints(offer: HotelOffer) -> Optional[str]:
    parts: list[str] = []
    if offer.bedrooms is not None:
        parts.append(f"{offer.bedrooms} bedroom" + ("" if offer.bedrooms == 1 else "s"))
    if offer.bathrooms is not None:
        parts.append(f"{offer.bathrooms} bathroom" + ("" if offer.bathrooms == 1 else "s"))
    if offer.beds is not None:
        parts.append(f"{offer.beds} bed" + ("" if offer.beds == 1 else "s"))
    return ", ".join(parts) if parts else None


def _print_hotel_offer_details(
    offer: HotelOffer,
    *,
    query: HotelQuery,
    applied: AppliedHotelFilters,
) -> None:
    cancellation = _format_cancellation_evidence(
        offer.cancellation_evidence,
        query=query,
        applied=applied,
    )
    print(f"    {cancellation}")
    print(f"    {_format_lodging_kind(offer.lodging_kind)}")
    units = _format_unit_hints(offer)
    if units:
        print(f"    {units}")


def _hotel_offer_identity(offer: HotelOffer) -> Tuple[str, str]:
    title = " ".join(offer.title.split()).casefold()
    address = " ".join((offer.address or "").split()).casefold()
    return (title, address)


def _cheapest_by_identity(offers: Sequence[HotelOffer]) -> dict[Tuple[str, str], HotelOffer]:
    chosen: dict[Tuple[str, str], HotelOffer] = {}
    for offer in offers:
        key = _hotel_offer_identity(offer)
        current = chosen.get(key)
        if current is None or offer.total_price_eur < current.total_price_eur:
            chosen[key] = offer
    return chosen


def _join_cancellation_rows(
    free_offers: Sequence[HotelOffer],
    open_offers: Sequence[HotelOffer],
) -> Tuple[Tuple[HotelOffer, Optional[HotelOffer], Optional[HotelOffer]], ...]:
    free_map = _cheapest_by_identity(free_offers)
    open_map = _cheapest_by_identity(open_offers)
    rows: list[Tuple[HotelOffer, Optional[HotelOffer], Optional[HotelOffer]]] = []
    for key in set(free_map) | set(open_map):
        free_offer = free_map.get(key)
        open_offer = open_map.get(key)
        sample = free_offer or open_offer
        assert sample is not None
        rows.append((sample, free_offer, open_offer))
    rows.sort(
        key=lambda row: (
            min(offer.total_price_eur for offer in (row[1], row[2]) if offer is not None),
            row[0].title.casefold(),
        )
    )
    return tuple(rows)


def _print_cancellation_compare(
    free_result: HotelQuerySuccess,
    open_result: HotelQuerySuccess,
) -> None:
    print("\n=== Cancellation compare ===")
    rows = _join_cancellation_rows(free_result.offers, open_result.offers)
    if not rows:
        print("  (no matching stays)")
        return
    for sample, free_offer, open_offer in rows:
        free_label = f"{free_offer.total_price_eur:.0f} €" if free_offer is not None else "—"
        open_label = f"{open_offer.total_price_eur:.0f} €" if open_offer is not None else "—"
        delta = ""
        if free_offer is not None and open_offer is not None:
            diff = free_offer.total_price_eur - open_offer.total_price_eur
            delta = f"  delta {diff:.0f} €"
        print(f"  {sample.title}  free cancel {free_label}  no free cancel {open_label}{delta}")
        detail_query = free_result.query if free_offer is not None else open_result.query
        detail_applied = free_result.applied if free_offer is not None else open_result.applied
        _print_hotel_offer_details(sample, query=detail_query, applied=detail_applied)


def _print_hotel_report(report) -> None:
    any_success = False
    queries = report.queries
    if (
        len(queries) == 2
        and isinstance(queries[0], HotelQuerySuccess)
        and isinstance(queries[1], HotelQuerySuccess)
        and queries[0].query.free_cancellation
        and not queries[1].query.free_cancellation
    ):
        _print_cancellation_compare(queries[0], queries[1])
    for result in queries:
        query = result.query
        nights_label = "night" if query.nights == 1 else "nights"
        header = (
            f"\n=== {query.location}  "
            f"{query.check_in.isoformat()} -> {query.check_out.isoformat()} "
            f"({query.nights} {nights_label}, {query.adults} adult(s), "
            f"{query.rooms} room(s)) ==="
        )
        print(header)
        _print_hotel_filters(query, result.applied, provider=report.provider)
        if isinstance(result, HotelQuerySuccess):
            any_success = True
            if not result.offers:
                print("  (no eligible stays)")
            for offer in result.offers:
                rating = f"{offer.rating_score:.1f}" if offer.rating_score is not None else "-"
                address = f"  {offer.address}" if offer.address else ""
                print(f"  {offer.total_price} total stay  rating {rating}  {offer.title}{address}")
                _print_hotel_offer_details(offer, query=query, applied=result.applied)
            print(
                f"  Raw cards: {result.raw_count}; "
                f"eligible: {result.eligible_count}; "
                f"shown: {len(result.offers)}"
            )
        elif isinstance(result, HotelQueryFailure):
            print(f"  ERROR: {result.error.message}")
    if any_success:
        site = "Google Hotels" if report.provider == "google-hotels" else "Booking.com"
        print(
            f"\nVerify the final total stay price and cancellation terms on {site} before booking."
        )


def _exit_code(report) -> int:
    failures = sum(result.status == "error" for result in report.queries)
    if failures == 0:
        return 0
    if failures == len(report.queries):
        return 2
    return 3


def _run_flights(args: argparse.Namespace) -> int:
    try:
        queries = _parse_and_validate(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = search_flights(
        queries,
        top=args.top,
        buffer_eur=args.baggage_buffer,
        progress=lambda line: print(line, file=sys.stderr),
        sort=args.sort,
        fetch=args.fetch,
        max_layover_hours=args.max_layover,
        min_layover_hours=args.min_layover,
        max_duration_hours=args.max_duration,
        airlines=parse_airline_codes(args.airlines),
        exclude_airlines=parse_airline_codes(args.exclude_airlines),
        depart_window=parse_depart_window(args.depart_window),
    )
    _print_report(report, sort=args.sort)

    if args.save:
        destination = Path(args.save)
        write_report_atomic(report, destination)
        print(f"\nSaved {destination}")

    return _exit_code(report)


def _run_hotels(args: argparse.Namespace) -> int:
    try:
        queries = _validate_hotel_args(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = search_hotels(
        queries,
        top=args.top,
        progress=lambda line: print(line, file=sys.stderr),
        source=getattr(args, "source", "booking"),
    )
    _print_hotel_report(report)

    if args.save:
        destination = Path(args.save)
        write_hotel_report_atomic(report, destination)
        print(f"\nSaved {destination}")

    return _exit_code(report)


def _print_dates_report(report: DateCalendarReport) -> None:
    print(
        f"\n=== {report.origin} -> {report.destination}  "
        f"{report.start_date.isoformat()} .. {report.end_date.isoformat()} ==="
    )
    any_price = False
    for row in report.days:
        if row.status == "error" and row.error is not None:
            print(f"  {row.departure_date.isoformat()}   ERROR: {row.error.message}")
            continue
        if row.price_eur is None:
            print(f"  {row.departure_date.isoformat()}      —")
            continue
        any_price = True
        extra = ""
        if row.airline:
            extra += f"  {row.airline}"
        if row.stops_count is not None:
            extra += f"  {_format_stops(row.stops_count)}"
        print(f"  {row.departure_date.isoformat()}  {row.price_eur:>7.0f} €{extra}")
    if any_price:
        print("\nVerify checked baggage on Google Flights before booking.")


def _print_explore_report(report: ExploreReport) -> None:
    print(
        f"\n=== From {report.origin}  {report.start_date.isoformat()}  "
        f"({report.days}-day window) ==="
    )
    if report.error is not None and not report.destinations:
        print(f"  ERROR: {report.error.message}")
        return
    if not report.destinations:
        print("  (no destinations)")
        return
    for row in report.destinations:
        price = f"{row.price_eur:>7.0f} €" if row.price_eur is not None else "      —"
        country = f"  {row.country}" if row.country else ""
        print(f"  {price}  {row.iata}  {row.city}{country}")
    print("\nVerify checked baggage on Google Flights before booking.")


def _print_airports(query: str) -> int:
    try:
        rows = lookup_airports(query)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("  (no airports)")
        return 0
    for row in rows:
        city = row.city or "?"
        country = row.country or "?"
        print(f"  {row.iata}  {row.name}  {city}  {country}")
    return 0


def _dates_exit_code(report: DateCalendarReport) -> int:
    failures = sum(row.status == "error" for row in report.days)
    if failures == 0:
        return 0
    if failures == len(report.days):
        return 2
    return 3


def _run_dates(args: argparse.Namespace) -> int:
    try:
        origin, destination = parse_route_pair(args.route)
        start = _parse_iso_date(args.start, "--from")
        end = _parse_iso_date(args.end, "--to")
        if args.adults < 1:
            raise ValueError("--adults must be at least 1")
        validate_date_window(start, end)
        FlightQuery(origin, destination, start, max_stops=args.max_stops, adults=args.adults)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = search_dates(
        origin,
        destination,
        start,
        end,
        adults=args.adults,
        cabin=args.cabin,
        max_stops=args.max_stops,
        progress=lambda line: print(line, file=sys.stderr),
    )
    _print_dates_report(report)
    if args.save:
        destination_path = Path(args.save)
        write_dates_report_atomic(report, destination_path)
        print(f"\nSaved {destination_path}")
    return _dates_exit_code(report)


def _month_start(value: str) -> date:
    try:
        year_text, month_text = value.split("-", 1)
        year, month = int(year_text), int(month_text)
        return date(year, month, 1)
    except ValueError as exc:
        raise ValueError("--month must look like YYYY-MM") from exc


def _run_explore(args: argparse.Namespace) -> int:
    try:
        origin = args.origin.strip().upper()
        if not is_known_iata(origin):
            raise ValueError(f"unknown origin IATA code: {origin!r}")
        if args.month and (args.start or args.days != 7):
            raise ValueError("use either --month or --from/--days, not both")
        if args.month:
            start = _month_start(args.month)
            days = calendar.monthrange(start.year, start.month)[1]
        else:
            if not args.start:
                raise ValueError("--from or --month is required")
            start = _parse_iso_date(args.start, "--from")
            days = args.days
        if args.top <= 0:
            raise ValueError("--top must be a positive integer")
        if args.adults < 1:
            raise ValueError("--adults must be at least 1")
        validate_explore_window(start, days)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    report = search_explore(
        origin,
        start,
        days=days,
        top=args.top,
        adults=args.adults,
        cabin=args.cabin,
        max_stops=args.max_stops,
        progress=lambda line: print(line, file=sys.stderr),
    )
    _print_explore_report(report)
    if args.save:
        destination_path = Path(args.save)
        write_explore_report_atomic(report, destination_path)
        print(f"\nSaved {destination_path}")
    if report.error is not None and not report.destinations:
        return 2
    return 0


def _run_airports(args: argparse.Namespace) -> int:
    return _print_airports(args.query)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Local Google Flights and hotel search. Any IATA pair; quotes in EUR. "
            "One-way, packaged round-trip, or multi-city; Booking or Google Hotels."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=FLIGHTS_EXAMPLES,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    flights = sub.add_parser(
        "flights",
        help="Google Flights search (one-way, packaged RT, or multi-city; quotes in EUR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=FLIGHTS_EXAMPLES,
    )
    flights.add_argument(
        "routes",
        nargs="+",
        help="ORIGIN-DESTINATION:DATE[,DATE...] or ORIGIN-DESTINATION:OUT:BACK (IATA codes)",
    )
    flights.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Maximum stops (default 1). 2 means two-or-fewer.",
    )
    flights.add_argument(
        "--trip",
        default="one-way",
        type=normalize_trip_kind,
        metavar="{one-way,rt,multi}",
        help=(
            "Trip kind (default one-way). rt/round-trip and multi POST one package. "
            "Sugar without --trip stays two one-ways. "
            "Aliases: oneway, one_way, round-trip, round_trip."
        ),
    )
    flights.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    flights.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    flights.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers per query (default {DEFAULT_TOP})",
    )
    flights.add_argument(
        "--baggage-buffer",
        type=int,
        default=DEFAULT_BAGGAGE_BUFFER_EUR,
        metavar="EUR",
        help=(
            f"EUR added to low-cost fares when ranking (default {DEFAULT_BAGGAGE_BUFFER_EUR}, "
            "0 to rank on fare alone)"
        ),
    )
    flights.add_argument(
        "--sort",
        default="ranked",
        choices=["ranked", "fare", "duration"],
        help="Order and select --top by ranked total (default), fare, or duration",
    )
    flights.add_argument(
        "--airlines",
        default=None,
        metavar="CODES",
        help="Keep only these airline IATA codes (comma-separated, e.g. IB,I2)",
    )
    flights.add_argument(
        "--exclude-airlines",
        default=None,
        dest="exclude_airlines",
        metavar="CODES",
        help="Drop these airline IATA codes (comma-separated, e.g. FR,RK)",
    )
    flights.add_argument(
        "--depart-window",
        default=None,
        dest="depart_window",
        metavar="START-END",
        help="Keep departures whose local hour is in START-END inclusive (e.g. 6-20)",
    )
    flights.add_argument(
        "--fetch",
        default="auto",
        choices=["auto", "sweep", "detail"],
        help=(
            "sweep is a fast HTTP shortlist (owned shopping RPC, Chrome TLS session); "
            "detail is the Playwright scrape. "
            "auto uses sweep for 3+ queries and detail for 1-2 (default auto)"
        ),
    )
    flights.add_argument(
        "--max-layover",
        type=float,
        default=None,
        metavar="HOURS",
        dest="max_layover",
        help="Drop 1-stop offers whose layover exceeds HOURS (sweep and detail)",
    )
    flights.add_argument(
        "--min-layover",
        type=float,
        default=None,
        metavar="HOURS",
        dest="min_layover",
        help="Drop 1-stop offers whose layover is shorter than HOURS",
    )
    flights.add_argument(
        "--max-duration",
        type=float,
        default=None,
        metavar="HOURS",
        dest="max_duration",
        help="Drop offers whose elapsed time exceeds HOURS",
    )
    flights.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON report atomically to FILE",
    )

    hotels = sub.add_parser(
        "hotels",
        help="Hotel search (total-stay, quoted in EUR). Default Booking; --source google is HTTP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HOTELS_EXAMPLES,
    )
    hotels.add_argument("location", help="City or area name")
    hotels.add_argument("check_in", help="Check-in date (YYYY-MM-DD)")
    hotels.add_argument("check_out", help="Check-out date (YYYY-MM-DD)")
    hotels.add_argument(
        "--adults",
        type=int,
        default=2,
        help="Number of adults (default 2)",
    )
    hotels.add_argument(
        "--rooms",
        type=int,
        default=1,
        help="Number of rooms (default 1)",
    )
    hotels.add_argument(
        "--top",
        type=int,
        default=8,
        help="Stays to show (default 8)",
    )
    hotels.add_argument(
        "--min-rating",
        type=float,
        default=None,
        dest="min_rating",
        metavar="SCORE",
        help="Minimum review score (Booking 0-10; Google Hotels 0-5)",
    )
    hotels.add_argument(
        "--entire-home",
        action="store_true",
        help=("Require entire homes/apartments (cards with unknown property type may remain)"),
    )
    hotels.add_argument(
        "--allow-non-refundable",
        action="store_true",
        help="Include non-refundable stays (default filters to free cancellation)",
    )
    hotels.add_argument(
        "--source",
        default="booking",
        choices=["booking", "google"],
        help=(
            "booking is the Playwright evidence path (CLI default). "
            "google is the HTTP shortlist (MCP default)."
        ),
    )
    hotels.add_argument(
        "--compare-cancellation",
        action="store_true",
        help=(
            "Run two sequential searches (free cancellation, then rates without that chip) "
            "and print a joined price table"
        ),
    )
    hotels.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON report atomically to FILE",
    )

    dates = sub.add_parser(
        "dates",
        help="Cheapest fare per day for one route (compact table, quoted in EUR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=DATES_EXAMPLES,
    )
    dates.add_argument("route", help="ORIGIN-DESTINATION (IATA codes)")
    dates.add_argument(
        "--from",
        dest="start",
        required=True,
        help="First departure date (YYYY-MM-DD)",
    )
    dates.add_argument(
        "--to",
        dest="end",
        required=True,
        help=f"Last departure date (YYYY-MM-DD); window cap is {MAX_DATE_WINDOW_DAYS} days",
    )
    dates.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1],
        help="Maximum stops (default 1)",
    )
    dates.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    dates.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    dates.add_argument(
        "--fetch",
        default="sweep",
        choices=["auto", "sweep", "detail"],
        help="Calendar uses the compact date-grid RPC (sweep). detail is accepted and ignored.",
    )
    dates.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON report atomically to FILE",
    )

    explore = sub.add_parser(
        "explore",
        help="Cheap destinations from one origin (quoted in EUR)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXPLORE_EXAMPLES,
    )
    explore.add_argument("origin", help="Origin IATA code")
    explore.add_argument(
        "--from",
        dest="start",
        default=None,
        help="Outbound date (YYYY-MM-DD)",
    )
    explore.add_argument(
        "--days",
        type=int,
        default=7,
        help="Trip length in days (default 7; used as the explore window label)",
    )
    explore.add_argument(
        "--month",
        default=None,
        help="Use the first day of YYYY-MM and that month's length as --days",
    )
    explore.add_argument(
        "--top",
        type=int,
        default=DEFAULT_EXPLORE_TOP,
        help=f"Destinations to price (default {DEFAULT_EXPLORE_TOP})",
    )
    explore.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1],
        help="Maximum stops when pricing a destination (default 1)",
    )
    explore.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    explore.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    explore.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON report atomically to FILE",
    )

    airports = sub.add_parser(
        "airports",
        help="Offline IATA airport lookup",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=AIRPORTS_EXAMPLES,
    )
    airports.add_argument("query", help="IATA code or city/name fragment")

    sub.add_parser(
        "bench",
        help="Offline keep-or-revert bench (unittest + owned parse corpus)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=BENCH_EXAMPLES,
    )

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return 0 if code == 0 else 1

    if args.cmd == "flights":
        return _run_flights(args)
    if args.cmd == "hotels":
        return _run_hotels(args)
    if args.cmd == "dates":
        return _run_dates(args)
    if args.cmd == "explore":
        return _run_explore(args)
    if args.cmd == "airports":
        return _run_airports(args)
    if args.cmd == "bench":
        return run_bench()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
