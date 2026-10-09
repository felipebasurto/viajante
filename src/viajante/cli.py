"""Argument parsing, terminal tables, and optional JSON saves."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence, Tuple, get_args

from viajante.airports import is_known_iata, parse_exclude_regions
from viajante.bench import run_bench
from viajante.carriers import parse_airline_codes, parse_alliances
from viajante.cli_report import (
    _as_report_tuple,
    _combine_exit_codes,
    _dates_exit_code,
    _exit_code,
    _flex_exit_code,
    _print_airports,
    _print_dates_report,
    _print_explore_report,
    _print_flex_report,
    _print_hidden_city_report,
    _print_hotel_report,
    _print_hotel_rooms_report,
    _print_report,
    _print_split_report,
    _print_trip_total,
)
from viajante.dates import (
    MAX_DATE_WINDOW_DAYS,
    MAX_FLEX_DAYS,
    calendar_trip,
    flex_window,
    parse_route_pair,
    resolve_date_trip,
    search_dates,
    search_flex,
    validate_date_window,
)
from viajante.explore import (
    DEFAULT_EXPLORE_TOP,
    month_window,
    search_explore,
    validate_explore_window,
)
from viajante.flight_filters import (
    OfferFilters,
    parse_depart_window,
    parse_named_clock,
    parse_overnight_airports,
    parse_via_airports,
    validate_layover_hours,
    validate_via_pair,
)
from viajante.flight_offers import FLIGHT_SORTS
from viajante.flight_routes import (
    FlightPlan,
    _overlay_carrier_filters,
    as_trips,
    expand_nearby_trips,
    nearby_notes,
    nearby_origin_notes,
    normalize_trip_kind,
    parse_flight_plan,
)
from viajante.flights import DEFAULT_TOP, search_flights
from viajante.history_cli import add_parsers as add_history_parsers
from viajante.history_cli import run_history, run_watch
from viajante.hotels import (
    resolve_hotel_currency,
    search_hotels,
    validate_max_distance,
    validate_near,
)
from viajante.models import (
    FlightCabin,
    FlightQuery,
    HotelQuery,
    RoundTrip,
    Trip,
    format_money,
    normalize_country,
)
from viajante.points import (
    compare_award,
    load_award_offer,
    load_balances,
    transfer_paths,
)
from viajante.quote import (
    first_origin_iata,
    resolve_baggage_buffer,
    resolve_quote_currency,
)
from viajante.recheck import run_recheck_cli
from viajante.runtime import package_version
from viajante.skiplagged import search_hidden_city
from viajante.skiplagged_hotels import search_hotel_rooms
from viajante.split import (
    DEFAULT_MIN_CONNECTION_HOURS,
    DEFAULT_SPLIT_HUBS,
    MAX_SPLIT_HUBS,
    search_split_tickets,
    validate_split_request,
)
from viajante.split_filters import SplitFilters
from viajante.storage import reports_payload, write_json_atomic
from viajante.trip import (
    search_trip,
    stay_window_from_trips,
)

FLIGHTS_EXAMPLES = """\
Examples:
  viajante flights JFK-LHR:2026-09-15
  viajante flights BOS-LHR:2026-09-18 --nearby --fetch sweep
  viajante flights JFK-NRT:2026-10-09:2026-10-20
  viajante flights --trip rt LAX-NRT:2026-10-12:2026-10-26 --fetch sweep
  viajante flights SYD-AKL:2026-11-03 AKL-SYD:2026-11-10 --max-stops 0
  viajante flights JFK-LHR:2026-09-15,2026-09-16 --top 5 --sort fare --save results/search.json
  viajante flights JFK-LHR:2026-09-15 --fetch sweep
  viajante flights LAX-NRT:2026-10-12 --fetch sweep --max-layover 8
  viajante flights JFK-LHR:2026-09-15 --fetch detail
  viajante flights JFK-LHR:2026-09-15 --exclude-airlines F9,NK --depart-window 7-12 --fetch sweep
  viajante flights JFK-LHR:2026-09-15 --depart-window 06:00-20:00 --sort duration
  viajante flights JFK-LHR:2026-09-15 --airlines BA,AA --sort duration
  viajante flights JFK-LHR:2026-09-15 --price-cap 200 --fetch sweep
  viajante flights JFK-LHR:2026-09-15 --bags 1 --carry-on --fetch sweep
  viajante flights JFK-LHR:2026-09-15 --max-duration 16 --min-layover 1 --max-layover 8
  viajante flights JFK-SIN:2026-11-03 --via IST --exclude-via DXB --fetch sweep
  viajante flights JFK-NRT:2026-11-03 --split-tickets --split-via LAX,SFO
  viajante flights --trip rt JFK-NRT:2026-11-03:2026-11-17 --split-tickets
"""

DATES_EXAMPLES = """\
Examples:
  viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14
  viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 5
  viajante dates BOS-LHR --from 2026-09-01 --to 2026-09-14 --nearby
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --depart-window 7-12
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --max-layover 3
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --sort duration
"""

FLEX_EXAMPLES = """\
Examples:
  viajante flex BOS-LHR --around 2026-09-12 --flex 3 --nights 7
  viajante flex JFK-LHR --around 2026-09-15 --flex 3
  viajante flex BOS-LHR --around 2026-09-12 --flex 3 --nearby
  viajante flex JFK-LHR --around 2026-09-15 --flex 3 --depart-window 06:00-20:00
  viajante flex JFK-LHR --around 2026-09-15 --flex 3 --max-layover 3
"""

EXPLORE_EXAMPLES = """\
Examples:
  viajante explore JFK --from 2026-09-15 --days 7
  viajante explore JFK --from 2026-09-15 --days 7 --sort duration
  viajante explore NRT --month 2026-10
  viajante explore SIN --from 2026-09-01 --price-cap 200
  viajante explore LHR --from 2026-09-15 --nearby
  viajante explore JFK --from 2026-09-15 --depart-window 7-12
  viajante explore JFK --from 2026-09-15 --max-layover 3
  viajante explore NRT --from 2026-09-15 --days 7 --exclude-regions asia
"""

AIRPORTS_EXAMPLES = """\
Examples:
  viajante airports tokyo
  viajante airports london
  viajante airports JFK
  (city queries list codes; they do not pick one)
"""

BENCH_EXAMPLES = """\
Examples:
  viajante bench
"""

HOTELS_EXAMPLES = """\
Examples:
  viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY
  viajante hotels "Mexico City" 2026-12-04 2026-12-10 --currency MXN --top 5
  viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --entire-home --min-rating 8.5
  viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --compare-cancellation
  viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --save results/hotels.json
  viajante hotels Tokyo 2026-10-12 2026-10-16 --currency JPY --source google --top 3
"""

TRIP_EXAMPLES = """\
Examples:
  viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --adults 2
  viajante trip DUB-JFK:2026-10-09:2026-10-13 --hotel "New York" --adults 2 --fetch sweep
  viajante trip LAX-NRT:2026-10-12:2026-10-20 --hotel Tokyo --trip rt --source google
  viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --bags 1 --via DXB
  viajante trip BOS-LHR:2026-09-18:2026-09-22 --hotel London --trip rt --nearby
"""

HIDDEN_CITY_EXAMPLES = """\
Examples:
  viajante hidden-city JFK-LHR:2026-11-15
  viajante hidden-city NRT-SIN:2026-11-03 --return 2026-11-10
  viajante hidden-city GRU-EZE:2026-11-20 --save results/hidden.json
"""

AWARDS_EXAMPLES = """\
Examples:
  viajante awards --offer award.json --cash 1200 --currency USD
  viajante awards --offer award.json --balances balances.json --cash 1800 --currency GBP
"""

RECHECK_EXAMPLES = """\
Examples:
  viajante recheck-offer --offer offer.json
  viajante recheck-offer --offer offer.json --save results/recheck.json
  viajante recheck-offer --offer identity.json --query query.json --currency USD
"""

POINTS_EXAMPLES = """\
Examples:
  viajante points --program aeroplan --points 70000
  viajante points --program avios --points 50000 --balances balances.json
"""


def _parse_and_validate(args: argparse.Namespace) -> tuple[Tuple[Trip, ...], dict[str, object]]:
    if args.top <= 0:
        raise ValueError("--top must be a positive integer")
    occupancy = _occupancy_from_args(args)
    shop = _owned_shop_filters_from_args(args)
    plan = parse_flight_plan(
        args.routes,
        trip=args.trip,
        max_stops=args.max_stops,
        cabin=args.cabin,
        bags=args.bags,
        carry_on=shop["carry_on"],
        price_cap=args.price_cap,
        **occupancy,
    )
    today = date.today()
    for departure in _plan_departure_dates(plan):
        if departure < today:
            raise ValueError(f"departure date is in the past: {departure.isoformat()}")
    trips = as_trips(plan)
    trips = expand_nearby_trips(trips, nearby=bool(getattr(args, "nearby", False)))
    _resolve_quote_from_args(args, first_origin_iata(trips[0]))
    return trips, shop


def _plan_departure_dates(plan: FlightPlan) -> Tuple[date, ...]:
    return tuple(leg.departure_date for trip in as_trips(plan) for leg in trip.legs)


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


def _parse_near(raw: Optional[str]) -> Optional[Tuple[float, float]]:
    if raw is None:
        return None
    parts = [part.strip() for part in raw.split(",")]
    try:
        lat, lng = (float(part) for part in parts) if len(parts) == 2 else (None, None)
    except ValueError:
        lat = lng = None
    if lat is None or lng is None:
        raise ValueError("--near must be LAT,LNG, e.g. 50.0875,14.4213")
    return (lat, lng)


def _validate_hotel_args(args: argparse.Namespace) -> Tuple[HotelQuery, ...]:
    if args.top <= 0:
        raise ValueError("--top must be a positive integer")
    queries = _build_hotel_queries(args)
    args.near = validate_near(_parse_near(getattr(args, "near", None)))
    args.max_distance_km = validate_max_distance(getattr(args, "max_distance_km", None), args.near)
    args.currency = resolve_hotel_currency(
        getattr(args, "source", "booking"), getattr(args, "currency", None)
    )
    return queries


def _split_via(args: argparse.Namespace) -> Optional[list[str]]:
    return args.split_via.split(",") if args.split_via else None


def _split_min_connection(args: argparse.Namespace) -> float:
    named = args.split_min_connection
    return DEFAULT_MIN_CONNECTION_HOURS if named is None else named


def _split_query_from_args(
    args: argparse.Namespace, queries: Tuple[Trip, ...], shop: dict[str, object]
) -> Optional[FlightQuery | RoundTrip]:
    """The one query a split search applies to, validated before any search runs."""
    named = (
        args.split_via,
        args.split_max_hubs,
        args.split_min_connection,
        args.split_overnight,
        args.split_leg_stops,
    )
    if not args.split_tickets:
        if any(value not in (None, False) for value in named):
            raise ValueError("--split-via and the other --split-* options need --split-tickets")
        return None
    if len(queries) != 1 or not isinstance(queries[0], (FlightQuery, RoundTrip)):
        raise ValueError(
            "--split-tickets takes exactly one one-way route or one --trip rt route "
            "(not --nearby, multi-city, or several routes)"
        )
    (query,) = _overlay_carrier_filters(
        (queries[0],),
        airlines=shop["airlines"],  # type: ignore[arg-type]
        exclude_airlines=shop["exclude_airlines"],  # type: ignore[arg-type]
        alliances=shop["alliances"],  # type: ignore[arg-type]
        exclude_alliances=shop["exclude_alliances"],  # type: ignore[arg-type]
    )
    validate_split_request(
        query,
        via=_split_via(args),
        max_hubs=args.split_max_hubs,
        min_connection_hours=_split_min_connection(args),
        leg_max_stops=args.split_leg_stops or 0,
        top=args.top,
    )
    return query


def _split_filters_from_shop(shop: dict[str, object]) -> SplitFilters:
    """The same named filters the one-way search applies, carried into split itineraries."""
    offer = OfferFilters(
        max_layover_hours=shop["max_layover_hours"],  # type: ignore[arg-type]
        min_layover_hours=shop["min_layover_hours"],  # type: ignore[arg-type]
        max_duration_hours=shop["max_duration_hours"],  # type: ignore[arg-type]
        depart_window=shop["depart_window"],  # type: ignore[arg-type]
        arrive_before=shop["arrive_before"],  # type: ignore[arg-type]
        depart_after=shop["depart_after"],  # type: ignore[arg-type]
        via=shop["via"],  # type: ignore[arg-type]
        exclude_via=shop["exclude_via"],  # type: ignore[arg-type]
        no_overnight=shop["no_overnight"],  # type: ignore[arg-type]
        require_overnight=shop["require_overnight"],  # type: ignore[arg-type]
    )
    return SplitFilters(
        offer=offer,
        exclude_airports=shop["exclude_airports"] or (),  # type: ignore[arg-type]
        include_airports=shop["include_airports"] or (),  # type: ignore[arg-type]
    )


def _run_flights(args: argparse.Namespace) -> int:
    try:
        queries, shop = _parse_and_validate(args)
        split_query = _split_query_from_args(args, queries, shop)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for note in nearby_notes(queries, exclude_airports=shop["exclude_airports"]):
        print(note, file=sys.stderr)

    report = search_flights(
        queries,
        top=args.top,
        baggage_buffer=args.baggage_buffer,
        progress=lambda line: print(line, file=sys.stderr),
        sort=args.sort,
        fetch=args.fetch,
        currency=args.currency,
        country=args.country,
        proxy=getattr(args, "proxy", None) or None,
        **{key: value for key, value in shop.items() if key not in _TRIP_SHOP_FIELDS},
    )
    _print_report(report, sort=args.sort)

    split = None
    if split_query is not None:
        try:
            split = search_split_tickets(
                split_query,
                packaged=report,
                top=args.top,
                filters=_split_filters_from_shop(shop),
                baggage_buffer=args.baggage_buffer or 0,
                via=_split_via(args),
                max_hubs=args.split_max_hubs,
                min_connection_hours=_split_min_connection(args),
                allow_overnight=args.split_overnight,
                leg_max_stops=args.split_leg_stops or 0,
                fetch="sweep" if args.fetch == "auto" else args.fetch,
                currency=args.currency,
                country=args.country,
                proxy=getattr(args, "proxy", None) or None,
                progress=lambda line: print(line, file=sys.stderr),
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_split_report(split)

    _save(args, report, extra={"split_tickets": split.to_dict()} if split else None)

    code = _exit_code(report)
    if split is not None and split.error is not None and split.error.rate_limited:
        return code or 3
    return code


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
        currency=args.currency,
        near=args.near,
        max_distance_km=args.max_distance_km,
    )
    _print_hotel_report(report)

    _save(args, report)

    return _exit_code(report)


def _trip_hotel_query(args: argparse.Namespace, trips: Tuple[Trip, ...]) -> HotelQuery:
    if args.check_in and args.check_out:
        check_in = _parse_iso_date(args.check_in, "check-in")
        check_out = _parse_iso_date(args.check_out, "check-out")
    else:
        window = stay_window_from_trips(trips)
        if window is None:
            raise ValueError(
                "hotel stay needs --check-in and --check-out (or a two-date flight route)"
            )
        check_in, check_out = window
        if args.check_in:
            check_in = _parse_iso_date(args.check_in, "check-in")
        if args.check_out:
            check_out = _parse_iso_date(args.check_out, "check-out")
    today = date.today()
    if check_in < today:
        raise ValueError(f"check-in date is in the past: {check_in.isoformat()}")
    source = getattr(args, "source", "booking")
    if source == "google" and args.min_rating is not None and args.min_rating > 5:
        raise ValueError("--min-rating must be at most 5 with --source google")
    return HotelQuery(
        args.hotel,
        check_in,
        check_out,
        adults=args.adults,
        rooms=args.rooms,
        min_rating=args.min_rating,
        entire_home=args.entire_home,
        free_cancellation=not args.allow_non_refundable,
    )


def _run_trip(args: argparse.Namespace) -> int:
    try:
        trips, shop = _parse_and_validate(args)
        hotel_query = _trip_hotel_query(args, trips)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "nearby", False):
        for note in nearby_notes(
            trips,
            exclude_airports=shop.get("exclude_airports"),
        ):
            print(note, file=sys.stderr)

    report = search_trip(
        trips,
        hotel_query,
        top=args.top,
        baggage_buffer=args.baggage_buffer,
        progress=lambda line: print(line, file=sys.stderr),
        sort=args.sort,
        fetch=args.fetch,
        currency=args.currency,
        country=args.country,
        hotel_source=getattr(args, "source", "booking"),
        **shop,
    )
    _print_report(report.flights, sort=args.sort)
    _print_hotel_report(report.hotels)
    _print_trip_total(report)

    _save(args, report)

    return _combine_exit_codes((_exit_code(report.flights), _exit_code(report.hotels)))


def _add_nearby_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--nearby",
        action="store_true",
        default=False,
        help=(
            "Expand origin or dest to owned same-city IATA and search each as a "
            "labeled alternative (default off; named open-jaw airports stay)"
        ),
    )


def _save(
    args: argparse.Namespace, result: object, *, extra: Optional[dict[str, object]] = None
) -> None:
    if args.save:
        destination = Path(args.save)
        write_json_atomic({**reports_payload(result), **(extra or {})}, destination)
        print(f"\nSaved {destination}")


def _occupancy_from_args(args: argparse.Namespace) -> dict[str, int]:
    children = int(getattr(args, "children", 0) or 0)
    infants_in_seat = int(getattr(args, "infants_in_seat", 0) or 0)
    infants_on_lap = int(getattr(args, "infants_on_lap", 0) or 0)
    if args.adults < 1:
        raise ValueError("--adults must be at least 1")
    if children < 0:
        raise ValueError("--children must not be negative")
    if infants_in_seat < 0:
        raise ValueError("--infants-in-seat must not be negative")
    if infants_on_lap < 0:
        raise ValueError("--infants-on-lap must not be negative")
    if infants_on_lap > args.adults:
        raise ValueError("--infants-on-lap cannot exceed --adults")
    return {
        "adults": args.adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
    }


def _add_max_stops_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Maximum stops (default 1). 2 means two-or-fewer.",
    )


def _add_cabin_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cabin",
        default="economy",
        choices=list(get_args(FlightCabin)),
        help="Cabin class (default economy)",
    )


def _add_trip_flag(parser: argparse.ArgumentParser, *, metavar: str, help_text: str) -> None:
    parser.add_argument(
        "--trip",
        default="one-way",
        type=normalize_trip_kind,
        metavar=metavar,
        help=help_text,
    )


def _add_nights_flag(parser: argparse.ArgumentParser, *, help_text: str) -> None:
    parser.add_argument(
        "--nights",
        type=int,
        default=None,
        metavar="N",
        help=help_text,
    )


def _add_save_flag(
    parser: argparse.ArgumentParser, help_text: str = "Write JSON report atomically to FILE"
) -> None:
    parser.add_argument("--save", default=None, metavar="FILE", help=help_text)


def _add_hotel_filter_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rooms", type=int, default=1, help="Hotel rooms (default 1)")
    parser.add_argument(
        "--min-rating",
        type=float,
        default=None,
        dest="min_rating",
        metavar="SCORE",
        help="Minimum hotel review score (Booking 0-10; Google Hotels 0-5)",
    )
    parser.add_argument(
        "--entire-home",
        action="store_true",
        help="Require entire homes/apartments (cards with unknown property type may remain)",
    )
    parser.add_argument(
        "--allow-non-refundable",
        action="store_true",
        help="Include non-refundable stays (default filters to free cancellation)",
    )
    parser.add_argument(
        "--source",
        default="booking",
        choices=["booking", "google", "skiplagged"],
        help=(
            "Hotel source. booking is the Playwright evidence path (CLI default). "
            "google is the HTTP shortlist (MCP default). skiplagged is the opt-in "
            "Skiplagged MCP: USD only, up to 10 adults, no entire-home filter."
        ),
    )


def _add_split_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--split-tickets",
        action="store_true",
        default=False,
        help=(
            "Opt in to separately ticketed itineraries built from real one-way quotes: a "
            "self-transfer via a hub for one one-way route, or mixed one-ways for --trip rt. "
            "Not protected: a missed connection between tickets is not rebooked. Costs extra "
            "searches (capped)."
        ),
    )
    parser.add_argument(
        "--split-via",
        default=None,
        metavar="CODES",
        help="Comma-separated hub IATA codes (default: layover airports in the packaged results)",
    )
    parser.add_argument(
        "--split-max-hubs",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"Hubs to try (default {DEFAULT_SPLIT_HUBS}, or all named with --split-via; "
            f"at most {MAX_SPLIT_HUBS})"
        ),
    )
    parser.add_argument(
        "--split-min-connection",
        type=float,
        default=None,
        metavar="HOURS",
        help=(
            f"Minimum hub connection between tickets (default {DEFAULT_MIN_CONNECTION_HOURS:g}; "
            "a planning default, not provider evidence)"
        ),
    )
    parser.add_argument(
        "--split-overnight",
        action="store_true",
        default=False,
        help="Also search the second ticket on the next day (one more search per hub)",
    )
    parser.add_argument(
        "--split-leg-stops",
        type=int,
        default=None,
        choices=[0, 1, 2],
        help="Maximum stops on each hub ticket (default 0)",
    )


def _add_flight_query_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adults", type=int, default=1, help="Number of adults (default 1)")
    parser.add_argument(
        "--children",
        type=int,
        default=0,
        help="Children aged 2-11 (default 0)",
    )
    parser.add_argument(
        "--infants-in-seat",
        type=int,
        default=0,
        dest="infants_in_seat",
        help="Infants in their own seat (default 0)",
    )
    parser.add_argument(
        "--infants-on-lap",
        type=int,
        default=0,
        dest="infants_on_lap",
        help="Infants on lap (default 0)",
    )
    _add_cabin_flag(parser)
    _add_currency_country_flags(parser)
    _add_proxy_flag(parser)


def _add_proxy_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help=(
            "HTTP(S) proxy for sweep fetch (curl_cffi Chrome TLS). Detail/Playwright is unchanged."
        ),
    )


def _add_currency_country_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--currency",
        default=None,
        metavar="CODE",
        help=(
            "ISO 4217 for Google curr. Unnamed infers from the named origin airport's "
            "country (JFK USD, LHR GBP, NRT JPY, GRU BRL). Required when that mapping "
            "is unproven. A city with several airports, Europe, unnamed origin, or two "
            "possible currencies does not pick: ask or error. Do not invent IATA, gl, "
            "or ISO 4217 from vibe. Viajante does not convert; the caller may convert "
            "for the user."
        ),
    )
    parser.add_argument(
        "--country",
        default=None,
        metavar="CC",
        help="ISO country for Google gl. Omitted when unset. Do not invent gl from vibe.",
    )


def _add_baggage_buffer_flag(parser: argparse.ArgumentParser, extra: str = "") -> None:
    help_text = (
        "Ranking add-on in the quote currency. Unnamed is 0. Named value is used "
        "as-is. Viajante does not invent a bag fee."
    )
    if extra:
        help_text = f"{help_text} {extra}"
    parser.add_argument(
        "--baggage-buffer",
        type=int,
        default=None,
        metavar="N",
        help=help_text,
    )


def _resolve_quote_from_args(args: argparse.Namespace, origin: Optional[str]) -> None:
    if getattr(args, "baggage_buffer", None) is not None and args.baggage_buffer < 0:
        raise ValueError("--baggage-buffer must not be negative")
    args.country = normalize_country(getattr(args, "country", None))
    args.currency = resolve_quote_currency(getattr(args, "currency", None), origin)
    args.baggage_buffer = resolve_baggage_buffer(
        getattr(args, "baggage_buffer", None), args.currency
    )


def _market_from_args(args: argparse.Namespace, origin: Optional[str] = None) -> dict[str, object]:
    _resolve_quote_from_args(args, origin)
    return {
        "currency": args.currency,
        "country": args.country,
        "proxy": getattr(args, "proxy", None) or None,
    }


def _add_owned_shop_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bags",
        type=int,
        default=None,
        metavar="N",
        help="Checked bags requested; unverifiable on public-page transport (omit to leave unset)",
    )
    parser.add_argument(
        "--carry-on",
        action="store_true",
        dest="carry_on",
        help="Request one carry-on (omit to leave unset)",
    )
    parser.add_argument(
        "--price-cap",
        type=int,
        default=None,
        metavar="AMOUNT",
        dest="price_cap",
        help="Drop owned fares above this amount in the quote currency (omit to leave unset)",
    )
    parser.add_argument(
        "--airlines",
        default=None,
        metavar="CODES",
        help="Airline IATA codes on the shopping request (comma-separated, e.g. BA,KL)",
    )
    parser.add_argument(
        "--exclude-airlines",
        default=None,
        dest="exclude_airlines",
        metavar="CODES",
        help="Airline IATA codes to exclude from shopping (comma-separated, e.g. DL)",
    )
    parser.add_argument(
        "--alliance",
        default=None,
        metavar="NAMES",
        help="Restrict the shopping request to these alliances (oneworld, skyteam, star)",
    )
    parser.add_argument(
        "--exclude-alliance",
        default=None,
        dest="exclude_alliance",
        metavar="NAMES",
        help="Exclude these alliances from the shopping request (oneworld, skyteam, star)",
    )
    parser.add_argument(
        "--via",
        default=None,
        metavar="CODES",
        dest="via",
        help=(
            "Keep connecting offers whose parsed layover matches these IATA codes "
            "(comma-separated). Post-filter only; unknown layover cannot prove a via"
        ),
    )
    parser.add_argument(
        "--exclude-via",
        default=None,
        metavar="CODES",
        dest="exclude_via",
        help=(
            "Drop connecting offers whose parsed layover matches these IATA codes "
            "(comma-separated). Unknown layover stays"
        ),
    )
    parser.add_argument(
        "--no-overnight",
        default=None,
        metavar="CODES",
        dest="no_overnight",
        help=(
            "Drop offers whose owned layover city+clock is overnight at these IATA codes "
            "(or any). Unknown city/clock cannot prove exclude"
        ),
    )
    parser.add_argument(
        "--require-overnight",
        default=None,
        metavar="CODES",
        dest="require_overnight",
        help=(
            "Keep only offers with an owned overnight layover at these IATA codes "
            "(or any). Unknown city/clock cannot prove include"
        ),
    )
    parser.add_argument(
        "--exclude-airports",
        default=None,
        metavar="CODES",
        dest="exclude_airports",
        help=(
            "Drop named origin/dest IATA in this list (comma-separated). "
            "Explore catalog dests with those codes are dropped. Nearby cannot "
            "sneak an excluded same-city code back. Unnamed stays unset"
        ),
    )
    parser.add_argument(
        "--include-airports",
        default=None,
        metavar="CODES",
        dest="include_airports",
        help=(
            "Keep only dests whose IATA is in this list (comma-separated). "
            "Explore catalog dests not in the list are dropped before shop. "
            "Dates/flex/flights keep only when dest is in the list (or nearby "
            "already produced an owned same-city code in the list). Exclude "
            "wins on overlap. Unnamed stays unset (full catalog)"
        ),
    )
    parser.add_argument(
        "--depart-window",
        default=None,
        dest="depart_window",
        metavar="START-END",
        help="Keep local departures in START-END inclusive (hours 6-20 or clocks 06:00-20:00)",
    )
    parser.add_argument(
        "--arrive-before",
        default=None,
        dest="arrive_before",
        metavar="HH:MM",
        help=(
            "Keep local arrivals at or before HH:MM. Post-filter on owned offer clocks; "
            "unknown arrival cannot prove the bound"
        ),
    )
    parser.add_argument(
        "--depart-after",
        default=None,
        dest="depart_after",
        metavar="HH:MM",
        help=(
            "Keep local departures at or after HH:MM. Post-filter on owned offer clocks; "
            "unknown departure cannot prove the bound"
        ),
    )
    parser.add_argument(
        "--max-layover",
        type=float,
        default=None,
        metavar="HOURS",
        dest="max_layover",
        help="Drop 1-stop offers whose layover exceeds HOURS (shop cards only)",
    )
    parser.add_argument(
        "--min-layover",
        type=float,
        default=None,
        metavar="HOURS",
        dest="min_layover",
        help="Drop 1-stop offers whose layover is shorter than HOURS (shop cards only)",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=None,
        metavar="HOURS",
        dest="max_duration",
        help="Drop offers whose elapsed time exceeds HOURS (shop cards only)",
    )


_TRIP_SHOP_FIELDS = frozenset({"bags", "carry_on", "price_cap"})


def _owned_shop_filters_from_args(args: argparse.Namespace) -> dict[str, object]:
    carry_on = 1 if args.carry_on else None
    if args.bags is not None and args.bags < 0:
        raise ValueError("--bags must not be negative")
    if args.price_cap is not None and args.price_cap <= 0:
        raise ValueError("--price-cap must be a positive amount in the quote currency")
    via = parse_via_airports(args.via)
    exclude_via = parse_via_airports(args.exclude_via, role="exclude-via")
    validate_via_pair(via, exclude_via, cli=True)
    no_overnight = parse_overnight_airports(
        getattr(args, "no_overnight", None), role="no-overnight"
    )
    require_overnight = parse_overnight_airports(
        getattr(args, "require_overnight", None), role="require-overnight"
    )
    max_layover = getattr(args, "max_layover", None)
    min_layover = getattr(args, "min_layover", None)
    max_duration = getattr(args, "max_duration", None)
    validate_layover_hours(
        max_layover_hours=max_layover,
        min_layover_hours=min_layover,
        max_duration_hours=max_duration,
        cli=True,
    )
    return {
        "bags": args.bags,
        "carry_on": carry_on,
        "price_cap": args.price_cap,
        "airlines": parse_airline_codes(args.airlines),
        "exclude_airlines": parse_airline_codes(args.exclude_airlines),
        "alliances": parse_alliances(getattr(args, "alliance", None)),
        "exclude_alliances": parse_alliances(getattr(args, "exclude_alliance", None)),
        "via": via,
        "exclude_via": exclude_via,
        "no_overnight": no_overnight,
        "require_overnight": require_overnight,
        "exclude_airports": parse_via_airports(
            getattr(args, "exclude_airports", None), role="exclude-airports"
        ),
        "include_airports": parse_via_airports(
            getattr(args, "include_airports", None), role="include-airports"
        ),
        "depart_window": parse_depart_window(getattr(args, "depart_window", None)),
        "arrive_before": parse_named_clock(
            getattr(args, "arrive_before", None), role="arrive-before"
        ),
        "depart_after": parse_named_clock(getattr(args, "depart_after", None), role="depart-after"),
        "max_layover_hours": max_layover,
        "min_layover_hours": min_layover,
        "max_duration_hours": max_duration,
    }


def _run_dates(args: argparse.Namespace) -> int:
    try:
        origin, destination = parse_route_pair(args.route)
        start = _parse_iso_date(args.start, "--from")
        end = _parse_iso_date(args.end, "--to")
        occupancy = _occupancy_from_args(args)
        validate_date_window(start, end)
        trip, nights = resolve_date_trip(args.trip, args.nights)
        shop = _owned_shop_filters_from_args(args)
        market = _market_from_args(args, origin)
        seed = calendar_trip(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            cabin=args.cabin,
            nights=nights,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap=shop["price_cap"],
            airlines=shop["airlines"],
            exclude_airlines=shop["exclude_airlines"],
            alliances=shop["alliances"],
            exclude_alliances=shop["exclude_alliances"],
            **occupancy,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    nearby = bool(getattr(args, "nearby", False))
    if nearby:
        for note in nearby_notes(
            expand_nearby_trips((seed,), nearby=True),
            exclude_airports=shop["exclude_airports"],
        ):
            print(note, file=sys.stderr)

    result = search_dates(
        origin,
        destination,
        start,
        end,
        cabin=args.cabin,
        max_stops=args.max_stops,
        trip=trip,
        nights=nights,
        nearby=nearby,
        baggage_buffer=args.baggage_buffer,
        sort=args.sort,
        progress=lambda line: print(line, file=sys.stderr),
        **shop,
        **occupancy,
        **market,
    )
    reports = _as_report_tuple(result)
    for report in reports:
        _print_dates_report(report)
    _save(args, reports)
    return _combine_exit_codes(_dates_exit_code(report) for report in reports)


def _run_flex(args: argparse.Namespace) -> int:
    try:
        origin, destination = parse_route_pair(args.route)
        around = _parse_iso_date(args.around, "--around")
        if args.flex_days < 1:
            raise ValueError("--flex must be at least 1")
        occupancy = _occupancy_from_args(args)
        if args.top <= 0:
            raise ValueError("--top must be a positive integer")
        start, _end = flex_window(around, args.flex_days)
        trip, nights = resolve_date_trip(args.trip, args.nights)
        shop = _owned_shop_filters_from_args(args)
        market = _market_from_args(args, origin)
        seed = calendar_trip(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            cabin=args.cabin,
            nights=nights,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap=shop["price_cap"],
            airlines=shop["airlines"],
            exclude_airlines=shop["exclude_airlines"],
            alliances=shop["alliances"],
            exclude_alliances=shop["exclude_alliances"],
            **occupancy,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    nearby = bool(getattr(args, "nearby", False))
    if nearby:
        for note in nearby_notes(
            expand_nearby_trips((seed,), nearby=True),
            exclude_airports=shop["exclude_airports"],
        ):
            print(note, file=sys.stderr)

    result = search_flex(
        origin,
        destination,
        around,
        args.flex_days,
        cabin=args.cabin,
        max_stops=args.max_stops,
        trip=trip,
        nights=nights,
        top=args.top,
        baggage_buffer=args.baggage_buffer,
        sort=args.sort,
        nearby=nearby,
        progress=lambda line: print(line, file=sys.stderr),
        **shop,
        **occupancy,
        **market,
    )
    reports = _as_report_tuple(result)
    for report in reports:
        _print_flex_report(report)
    _save(args, reports)
    return _combine_exit_codes(_flex_exit_code(report) for report in reports)


def _run_explore(args: argparse.Namespace) -> int:
    try:
        origin = args.origin.strip().upper()
        if not is_known_iata(origin):
            raise ValueError(f"unknown origin IATA code: {origin!r}")
        if args.month and (args.start or args.days != 7):
            raise ValueError("use either --month or --from/--days, not both")
        if args.month:
            start, days = month_window(args.month, flag="--month")
        else:
            if not args.start:
                raise ValueError("--from or --month is required")
            start = _parse_iso_date(args.start, "--from")
            days = args.days
        if args.top <= 0:
            raise ValueError("--top must be a positive integer")
        occupancy = _occupancy_from_args(args)
        shop = _owned_shop_filters_from_args(args)
        market = _market_from_args(args, origin)
        validate_explore_window(start, days)
        exclude_regions = parse_exclude_regions(getattr(args, "exclude_regions", None))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    nearby = bool(getattr(args, "nearby", False))
    if nearby:
        for note in nearby_origin_notes(origin, exclude_airports=shop.get("exclude_airports")):
            print(note, file=sys.stderr)

    result = search_explore(
        origin,
        start,
        days=days,
        top=args.top,
        cabin=args.cabin,
        max_stops=args.max_stops,
        nearby=nearby,
        sort=args.sort,
        baggage_buffer=args.baggage_buffer,
        exclude_regions=exclude_regions,
        progress=lambda line: print(line, file=sys.stderr),
        **shop,
        **occupancy,
        **market,
    )
    reports = _as_report_tuple(result)
    codes = []
    for report in reports:
        _print_explore_report(report)
        failures = report.coverage.failed
        codes.append(0 if not failures else 2 if failures == report.coverage.attempted else 3)
    _save(args, reports)
    return _combine_exit_codes(codes)


def _run_airports(args: argparse.Namespace) -> int:
    return _print_airports(args.query)


def _hidden_city_route(
    args: argparse.Namespace,
) -> tuple[str, str, date, Optional[date]]:
    spec = args.route
    try:
        _pair, dates_part = spec.split(":", 1)
    except ValueError as exc:
        raise ValueError(
            f"invalid route: {spec!r}. Expected ORIGIN-DESTINATION:DATE or "
            "ORIGIN-DESTINATION:OUT:BACK"
        ) from exc
    if "," in dates_part:
        raise ValueError("hidden-city takes one DATE or ORIGIN-DESTINATION:OUT:BACK")
    kind = "rt" if ":" in dates_part else "one-way"
    plan = parse_flight_plan([spec], trip=kind, max_stops=1, adults=args.adults)
    if isinstance(plan, RoundTrip):
        origin, destination, departure, back = (
            plan.origin,
            plan.destination,
            plan.departure_date,
            plan.return_date,
        )
    else:
        trips = as_trips(plan)
        if len(trips) != 1:
            raise ValueError(
                "hidden-city takes one airport pair (no metro codes) and one DATE or "
                "ORIGIN-DESTINATION:OUT:BACK"
            )
        query = trips[0]
        origin, destination, departure, back = (
            query.origin,
            query.destination,
            query.departure_date,
            None,
        )
    if args.return_date:
        named_back = _parse_iso_date(args.return_date, "--return")
        if back is not None and back != named_back:
            raise ValueError("return date in the route and --return must match")
        back = named_back
    return origin, destination, departure, back


def _run_hidden_city(args: argparse.Namespace) -> int:
    try:
        if args.top <= 0:
            raise ValueError("--top must be a positive integer")
        if args.adults < 1:
            raise ValueError("--adults must be at least 1")
        origin, dest, departure, back = _hidden_city_route(args)
        today = date.today()
        if departure < today:
            raise ValueError(f"departure date is in the past: {departure.isoformat()}")
        if back is not None and back < departure:
            raise ValueError("return date must not be before departure")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    report = search_hidden_city(
        origin,
        dest,
        departure,
        return_date=back,
        adults=args.adults,
        top=args.top,
        currency=args.currency,
    )
    _print_hidden_city_report(report)
    _save(args, report)
    if report.error is not None and not report.offers:
        return 2
    return 0


def _run_hotel_rooms(args: argparse.Namespace) -> int:
    try:
        check_in = date.fromisoformat(args.check_in)
        check_out = date.fromisoformat(args.check_out)
        if check_in < date.today():
            raise ValueError(f"check-in date is in the past: {check_in.isoformat()}")
        report = search_hotel_rooms(
            args.hotel_id,
            check_in,
            check_out,
            hotel_name=args.name,
            city=args.city,
            adults=args.adults,
            rooms=args.rooms,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_hotel_rooms_report(report)
    _save(args, report)
    return 2 if report.error is not None else 0


def _run_awards(args: argparse.Namespace) -> int:
    try:
        award = load_award_offer(Path(args.offer))
        balances = load_balances(Path(args.balances)) if args.balances else ()
        currency = args.currency
        if args.cash is not None:
            if args.cash <= 0:
                raise ValueError("--cash must be positive")
        report = compare_award(
            award,
            cash_price=args.cash,
            currency=currency,
            balances=balances,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"\n=== {report.award.origin} -> {report.award.destination}  "
        f"{report.award.departure_date.isoformat()} ({report.award.program}) ==="
    )
    print(
        f"points {report.award.points}  evidence {report.award.evidence}  "
        f"cabin {report.award.cabin}"
    )
    if report.cpp_cents is not None and report.currency:
        print(
            f"cpp {report.cpp_cents:.2f} cents  "
            f"cash {format_money(report.cash_price or 0, report.currency)}"
        )
    for path in report.transfer_paths:
        cover = "covers" if path.covers else "short"
        print(
            f"  {path.currency} -> {path.program}  {path.effective_points} "
            f"({cover}, table {path.last_verified.isoformat()})"
        )
    for step in report.playbook:
        print(f"{step.kind}: {step.title}")
        print(f"  {step.body}")
    _save(args, report)
    return 0


def _run_points(args: argparse.Namespace) -> int:
    try:
        balances = load_balances(Path(args.balances)) if args.balances else ()
        paths = transfer_paths(args.program, args.points, balances)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not paths:
        print(f"No local transfer partner is recorded for {args.program}.")
    for path in paths:
        cover = "covers" if path.covers else "short"
        print(
            f"{path.currency} -> {path.program}  {path.effective_points} "
            f"({cover}, table {path.last_verified.isoformat()})"
        )
    return 0


_PARSER: Optional[argparse.ArgumentParser] = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local Google Flights and hotel search. Any IATA pair. Currency is "
            "--currency or inferred from a named origin's country; if unknown, ask. "
            "Viajante does not convert. A city with several airports, Europe, unnamed "
            "origin, or two possible currencies does not pick: ask or error. "
            "One-way, packaged round-trip, or multi-city; Booking or Google Hotels."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=FLIGHTS_EXAMPLES,
    )
    parser.add_argument("--version", action="version", version=f"viajante {package_version()}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    flights = sub.add_parser(
        "flights",
        help="Google Flights search (one-way, packaged RT, or multi-city)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=FLIGHTS_EXAMPLES,
    )
    flights.add_argument(
        "routes",
        nargs="+",
        help="ORIGIN-DESTINATION:DATE[,DATE...] or ORIGIN-DESTINATION:OUT:BACK (IATA codes)",
    )
    _add_max_stops_flag(flights)
    _add_trip_flag(
        flights,
        metavar="{one-way,rt,multi}",
        help_text=(
            "Trip kind (default one-way). rt/round-trip and multi POST one package. "
            "Sugar without --trip stays two one-ways. "
            "Open-jaw --trip rt is two ORIGIN-DEST:DATE routes (one package). "
            "Aliases: oneway, one_way, round-trip, round_trip."
        ),
    )
    _add_flight_query_flags(flights)
    _add_nearby_flag(flights)
    flights.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers per query (default {DEFAULT_TOP})",
    )
    _add_baggage_buffer_flag(flights)
    flights.add_argument(
        "--sort",
        default="ranked",
        choices=list(FLIGHT_SORTS),
        help=(
            "Order and select --top by ranked total (default), fare/price, duration, "
            "departure, or arrival"
        ),
    )
    flights.add_argument(
        "--fetch",
        default="auto",
        choices=["auto", "sweep", "detail"],
        help=(
            "sweep reads the public results page over a Chrome-TLS HTTP/2 session; "
            "detail is the Playwright scrape (required for --trip multi; "
            "refuses bag and carrier filters). "
            "auto uses public-page sweep; detail is an explicit browser mode (default auto)"
        ),
    )
    _add_owned_shop_filters(flights)
    _add_split_flags(flights)
    _add_save_flag(flights)

    hotels = sub.add_parser(
        "hotels",
        help=(
            "Hotel search (total-stay). Currency required (no origin airport). "
            "Default Booking; --source google is HTTP."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HOTELS_EXAMPLES,
    )
    hotels.add_argument("location", help="City or area name")
    hotels.add_argument("check_in", help="Check-in date (YYYY-MM-DD)")
    hotels.add_argument("check_out", help="Check-out date (YYYY-MM-DD)")
    hotels.add_argument(
        "--currency",
        default=None,
        metavar="CODE",
        help=(
            "ISO 4217 for hotel quotes. Required (hotels have no origin airport). "
            "Do not invent ISO 4217 from vibe. Viajante does not convert; the caller "
            "may convert for the user."
        ),
    )
    hotels.add_argument(
        "--adults",
        type=int,
        default=2,
        help="Number of adults (default 2)",
    )
    hotels.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Stays to show (default {DEFAULT_TOP})",
    )
    _add_hotel_filter_flags(hotels)
    hotels.add_argument(
        "--near",
        default=None,
        metavar="LAT,LNG",
        help="A point you name; each stay with coordinates shows its straight-line distance to it",
    )
    hotels.add_argument(
        "--max-distance-km",
        type=float,
        default=None,
        help="Maximum straight-line distance; requires --near and excludes unknown coordinates",
    )
    hotels.add_argument(
        "--compare-cancellation",
        action="store_true",
        help=(
            "Run two sequential searches (free cancellation, then rates without that chip) "
            "and print a joined price table"
        ),
    )
    _add_save_flag(hotels)

    rooms = sub.add_parser(
        "hotel-rooms",
        help=(
            "Room rates for one Skiplagged hotel (USD): occupancy and refund flags. "
            "Name it with --hotel-id (provider_id from `hotels --source skiplagged`) "
            "or --name plus --city."
        ),
    )
    rooms.add_argument("check_in", help="Check-in date (YYYY-MM-DD)")
    rooms.add_argument("check_out", help="Check-out date (YYYY-MM-DD)")
    rooms.add_argument("--hotel-id", type=int, default=None, help="Skiplagged hotel id")
    rooms.add_argument(
        "--name", default=None, help="Exact hotel name (normalized match); needs --city"
    )
    rooms.add_argument("--city", default=None, help="City to look the name up in")
    rooms.add_argument("--adults", type=int, default=2, help="Number of adults (default 2)")
    rooms.add_argument("--rooms", type=int, default=1, help="Rooms, 1 to 5 (default 1)")
    _add_save_flag(rooms)

    trip = sub.add_parser(
        "trip",
        help=(
            "Flights plus hotel: print owned fare + hotel total stay + sum "
            "when dates overlap and both searches succeed"
        ),
        description=(
            "Search flights then a hotel. Print owned fare + hotel total stay + sum "
            "when dates overlap and both searches succeed. Omit the sum if either side missed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=TRIP_EXAMPLES,
    )
    trip.add_argument(
        "routes",
        nargs="+",
        help="ORIGIN-DESTINATION:DATE[,DATE...] or ORIGIN-DESTINATION:OUT:BACK (IATA codes)",
    )
    trip.add_argument(
        "--hotel",
        required=True,
        help="Hotel city or area (same occupancy adults as the flights)",
    )
    trip.add_argument(
        "--check-in",
        dest="check_in",
        default=None,
        help="Hotel check-in (YYYY-MM-DD). Default: earliest flight date when a return exists",
    )
    trip.add_argument(
        "--check-out",
        dest="check_out",
        default=None,
        help="Hotel check-out (YYYY-MM-DD). Default: latest flight date when a return exists",
    )
    _add_trip_flag(
        trip,
        metavar="{one-way,rt,multi}",
        help_text=(
            "Trip kind (default one-way). rt/round-trip POSTs one package. "
            "Sugar without --trip stays two one-ways."
        ),
    )
    _add_max_stops_flag(trip)
    trip.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Adults for both flights and the hotel (default 1)",
    )
    _add_cabin_flag(trip)
    _add_currency_country_flags(trip)
    trip.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers per query (default {DEFAULT_TOP})",
    )
    _add_baggage_buffer_flag(trip, extra="Trip total uses the owned cabin fare, not this buffer.")
    trip.add_argument(
        "--sort",
        default="ranked",
        choices=list(FLIGHT_SORTS),
        help="Flight offer order (default ranked). Trip total still uses owned fare.",
    )
    trip.add_argument(
        "--fetch",
        default="auto",
        choices=["auto", "sweep", "detail"],
        help=(
            "sweep is a fast HTTP shortlist; detail is the Playwright scrape. "
            "auto uses public-page sweep; detail is an explicit browser mode (default auto)"
        ),
    )
    _add_hotel_filter_flags(trip)
    _add_owned_shop_filters(trip)
    _add_nearby_flag(trip)
    _add_save_flag(trip, "Write JSON (flights, hotels, optional trip_total) atomically to FILE")

    dates = sub.add_parser(
        "dates",
        help="Cheapest fare per day for one route (public results page per day)",
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
    _add_trip_flag(
        dates,
        metavar="{one-way,rt}",
        help_text=(
            "Trip kind (default one-way). rt/round-trip is one packaged stay per "
            "departure day and needs --nights. --nights without --trip is rt. "
            "multi is not supported."
        ),
    )
    _add_nights_flag(
        dates,
        help_text="Stay length in nights for a round-trip calendar. Implies --trip rt.",
    )
    _add_max_stops_flag(dates)
    _add_flight_query_flags(dates)
    _add_owned_shop_filters(dates)
    _add_nearby_flag(dates)
    _add_baggage_buffer_flag(dates)
    dates.add_argument(
        "--sort",
        default=None,
        choices=list(FLIGHT_SORTS),
        help=(
            "Order day rows. Unnamed stays date order. Named duration/departure/arrival "
            "re-order shopped sweep-fallback rows that already own that key. "
            "fare/price/ranked may re-order priced rows by owned fare (ranked is fare+buffer). "
            "A compact cell missing the key is not given a made-up duration or clock. "
            "Sort is order, not a cut"
        ),
    )
    dates.add_argument(
        "--fetch",
        default="sweep",
        choices=["auto", "sweep", "detail"],
        help="Days are shopped on the public results page (sweep). detail is accepted and ignored.",
    )
    _add_save_flag(dates)

    flex = sub.add_parser(
        "flex",
        help="Cheapest day in a ±N window, then one shopping search",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=FLEX_EXAMPLES,
    )
    flex.add_argument("route", help="ORIGIN-DESTINATION (IATA codes)")
    flex.add_argument(
        "--around",
        required=True,
        help="Anchor departure date (YYYY-MM-DD)",
    )
    flex.add_argument(
        "--flex",
        dest="flex_days",
        type=int,
        required=True,
        metavar="N",
        help=f"Days either side of --around (1–{MAX_FLEX_DAYS}; window cap {MAX_DATE_WINDOW_DAYS})",
    )
    _add_trip_flag(
        flex,
        metavar="{one-way,rt}",
        help_text=(
            "Trip kind (default one-way). rt/round-trip needs --nights. "
            "--nights without --trip is rt. multi is not supported."
        ),
    )
    _add_nights_flag(
        flex,
        help_text="Stay length in nights for a packaged round-trip. Implies --trip rt.",
    )
    _add_max_stops_flag(flex)
    _add_flight_query_flags(flex)
    flex.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers to show from the chosen day (default {DEFAULT_TOP})",
    )
    _add_baggage_buffer_flag(flex)
    flex.add_argument(
        "--sort",
        default="ranked",
        choices=FLIGHT_SORTS,
        help="Offer order for the chosen day (default ranked)",
    )
    _add_owned_shop_filters(flex)
    _add_nearby_flag(flex)
    flex.add_argument(
        "--fetch",
        default="sweep",
        choices=["auto", "sweep", "detail"],
        help=(
            "Shops each day on the public results page, then one fresh shop on the chosen day. "
            "detail is accepted and ignored."
        ),
    )
    _add_save_flag(flex)

    explore = sub.add_parser(
        "explore",
        help="Cheap destinations from one origin",
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
        help=(
            "Stay length in days (default 7). Stored on the report; "
            "catalog and dest shops use --from only"
        ),
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
    _add_flight_query_flags(explore)
    _add_owned_shop_filters(explore)
    explore.add_argument(
        "--exclude-regions",
        default=None,
        metavar="REGIONS",
        dest="exclude_regions",
        help=(
            "Drop catalog dests whose owned IANA timezone sits in these regions "
            "(comma-separated, e.g. asia,europe). Unknown tz cannot prove keep. "
            "Unnamed stays unset (full catalog)"
        ),
    )
    _add_nearby_flag(explore)
    explore.add_argument(
        "--sort",
        default="price",
        choices=list(FLIGHT_SORTS),
        help=(
            "Order priced dests by cheapest fare (default), ranked total (fare+buffer), "
            "duration of that cheapest offer, or its owned departure/arrival clock. "
            "Unnamed stays price. Buffer ranking only applies when sort is ranked. "
            "A dest missing the sort key is not given a made-up duration, clock, or buffer"
        ),
    )
    _add_baggage_buffer_flag(explore)
    _add_save_flag(explore)

    hidden = sub.add_parser(
        "hidden-city",
        help="Skiplagged search (opt-in; not Google Flights)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HIDDEN_CITY_EXAMPLES,
    )
    hidden.add_argument(
        "route",
        help="ORIGIN-DESTINATION:DATE or ORIGIN-DESTINATION:OUT:BACK (IATA codes)",
    )
    hidden.add_argument(
        "--return",
        dest="return_date",
        default=None,
        metavar="DATE",
        help="Return date YYYY-MM-DD",
    )
    hidden.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    hidden.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Cheapest priced offers to keep (fare order, default {DEFAULT_TOP})",
    )
    hidden.add_argument(
        "--currency",
        default=None,
        help=(
            "ISO 4217 keep of owned card currency. Skiplagged cards are USD; "
            "omit or pass USD. Other codes are currency_mismatch (no FX)"
        ),
    )
    _add_save_flag(hidden)

    awards = sub.add_parser(
        "awards",
        help="Compare a named award offer to cash (local; no live seats)",
        description="Compare a named award offer to cash. Local math; no live seats.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=AWARDS_EXAMPLES,
    )
    awards.add_argument(
        "--offer",
        required=True,
        metavar="FILE",
        help="JSON award offer (origin, destination, departure_date, program, points, evidence)",
    )
    awards.add_argument(
        "--cash",
        type=float,
        default=None,
        help="Owned cash fare in the quote currency (omits CPP when unnamed)",
    )
    awards.add_argument(
        "--currency",
        default=None,
        help="ISO 4217 code for cash (or inferred from the offer origin)",
    )
    awards.add_argument(
        "--balances",
        default=None,
        metavar="FILE",
        help="JSON {balances:[{program,balance},...]} of named card currencies",
    )
    _add_save_flag(awards)

    points = sub.add_parser(
        "points",
        help="Local card-to-program transfer table (not live award seats)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=POINTS_EXAMPLES,
    )
    points.add_argument(
        "--program",
        required=True,
        help="Loyalty program code for the local transfer table (e.g. aeroplan)",
    )
    points.add_argument("--points", type=int, required=True, help="Award points needed")
    points.add_argument(
        "--balances",
        default=None,
        metavar="FILE",
        help="JSON {balances:[{program,balance},...]} of named card currencies",
    )

    recheck = sub.add_parser(
        "recheck-offer",
        help="Re-check an earlier flight offer with one fresh Google Flights search",
        description=(
            "Re-check an earlier offer. Runs one fresh search (no cache) and matches by "
            "flight numbers plus departure times. Outcome: same_price, price_changed, "
            "not_found, multiple_matches, incomplete_identity, check_failed, or substituted "
            "(only with --allow-substitute). Not a booking guarantee: confirm the price on "
            "the provider's own page. Exit 0 check ran to an answer, 1 bad input, 2 check "
            "not completed (check_failed, or incomplete_identity: no search was sent)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=RECHECK_EXAMPLES,
    )
    recheck.add_argument(
        "--offer",
        required=True,
        metavar="FILE",
        help="JSON offer (or {query, offer} row) from an earlier result; - reads stdin",
    )
    recheck.add_argument(
        "--query",
        default=None,
        metavar="FILE",
        help="JSON query when the offer carries no evidence query",
    )
    recheck.add_argument(
        "--currency",
        default=None,
        metavar="CODE",
        help="The offer's own ISO 4217 currency; required if it has none, refused if it differs",
    )
    recheck.add_argument(
        "--country", default=None, metavar="CC", help="ISO country for Google gl (omit when unset)"
    )
    recheck.add_argument(
        "--fetch",
        default=None,
        choices=["auto", "sweep", "detail"],
        help="Fetch mode; defaults to the offer's own backend",
    )
    recheck.add_argument(
        "--allow-loose-match",
        action="store_true",
        help="When flight numbers are absent, match by carrier and departure times (weaker)",
    )
    recheck.add_argument(
        "--allow-substitute",
        action="store_true",
        help="Report a close same-carrier alternative as substituted",
    )
    _add_proxy_flag(recheck)
    _add_save_flag(recheck)

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
    add_history_parsers(sub)
    return parser


def _cli_parser() -> argparse.ArgumentParser:
    global _PARSER
    if _PARSER is None:
        _PARSER = _build_parser()
    return _PARSER


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _cli_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return 0 if code == 0 else 1

    if args.cmd == "flights":
        return _run_flights(args)
    if args.cmd == "hotels":
        return _run_hotels(args)
    if args.cmd == "trip":
        return _run_trip(args)
    if args.cmd == "dates":
        return _run_dates(args)
    if args.cmd == "flex":
        return _run_flex(args)
    if args.cmd == "explore":
        return _run_explore(args)
    if args.cmd == "airports":
        return _run_airports(args)
    if args.cmd == "hotel-rooms":
        return _run_hotel_rooms(args)
    if args.cmd == "hidden-city":
        return _run_hidden_city(args)
    if args.cmd == "awards":
        return _run_awards(args)
    if args.cmd == "points":
        return _run_points(args)
    if args.cmd == "recheck-offer":
        return run_recheck_cli(args)
    if args.cmd == "bench":
        return run_bench()
    if args.cmd == "history":
        return run_history(args)
    if args.cmd == "watch":
        return run_watch(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
