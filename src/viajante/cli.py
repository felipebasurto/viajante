"""Argument parsing, terminal tables, and optional JSON saves."""

from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional, Sequence, Tuple

from viajante.airports import is_known_iata, lookup_airports
from viajante.bench import run_bench
from viajante.carriers import parse_airline_codes, parse_alliances
from viajante.dates import (
    MAX_DATE_WINDOW_DAYS,
    MAX_FLEX_DAYS,
    calendar_trip,
    flex_window,
    format_sparkline,
    format_summary_line,
    format_week_calendar,
    parse_route_pair,
    resolve_date_trip,
    search_dates,
    search_flex,
    validate_date_window,
    write_dates_reports_atomic,
    write_flex_reports_atomic,
)
from viajante.explore import (
    DEFAULT_EXPLORE_TOP,
    search_explore,
    validate_explore_window,
    write_explore_reports_atomic,
)
from viajante.flights import (
    DEFAULT_BAGGAGE_BUFFER_EUR,
    DEFAULT_TOP,
    FLIGHT_SORTS,
    FlightSort,
    _clock_minutes,
    expand_nearby_trips,
    nearby_notes,
    nearby_origin_notes,
    normalize_trip_kind,
    parse_depart_window,
    parse_flight_plan,
    parse_named_clock,
    parse_via_airports,
    search_flights,
    write_report_atomic,
)
from viajante.google_flights import google_flights_url
from viajante.hotels import search_hotels, write_hotel_report_atomic
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    DateCalendarReport,
    ExploreReport,
    FlexSearchReport,
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
    StopsCompare,
    StopsCompareSide,
    Trip,
    TripSearchReport,
    normalize_country,
    normalize_currency,
)
from viajante.prompt_bench import PROMPTS_ENV, run_prompt_bench
from viajante.trip import (
    format_trip_total,
    search_trip,
    stay_window_from_trips,
    write_trip_report_atomic,
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
"""

DATES_EXAMPLES = """\
Examples:
  viajante dates LAX-NRT --from 2026-10-01 --to 2026-10-31
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --fetch sweep
  viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 5
  viajante dates BOS-LHR --from 2026-09-01 --to 2026-09-14 --nearby
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --depart-window 7-12
  viajante dates JFK-LHR --from 2026-09-01 --to 2026-09-14 --max-layover 3
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
  viajante explore NRT --month 2026-10
  viajante explore SIN --from 2026-09-01 --price-cap 200
  viajante explore LHR --from 2026-09-15 --nearby
  viajante explore JFK --from 2026-09-15 --depart-window 7-12
  viajante explore JFK --from 2026-09-15 --max-layover 3
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
  viajante bench --prompts
  viajante bench --prompts --holdout
  viajante bench --prompts --timeit-sweep
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

TRIP_EXAMPLES = """\
Examples:
  viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --adults 2
  viajante trip DUB-JFK:2026-10-09:2026-10-13 --hotel "New York" --adults 2 --fetch sweep
  viajante trip LAX-NRT:2026-10-12:2026-10-20 --hotel Tokyo --trip rt --source google
  viajante trip SIN-MEL:2026-11-06:2026-11-10 --hotel Melbourne --trip rt --bags 1 --via DXB
  viajante trip BOS-LHR:2026-09-18:2026-09-22 --hotel London --trip rt --nearby
"""


def _parse_and_validate(args: argparse.Namespace) -> Tuple[Trip, ...]:
    if args.top <= 0:
        raise ValueError("--top must be a positive integer")
    if args.baggage_buffer < 0:
        raise ValueError("--baggage-buffer must not be negative")
    occupancy = _occupancy_from_args(args)
    args.currency = normalize_currency(args.currency)
    args.country = normalize_country(args.country)
    if args.bags is not None and args.bags < 0:
        raise ValueError("--bags must not be negative")
    carry_on = 1 if args.carry_on else None
    if args.price_cap is not None and args.price_cap <= 0:
        raise ValueError("--price-cap must be a positive EUR amount")
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
    parse_alliances(args.alliance)
    parse_alliances(args.exclude_alliance)
    parse_depart_window(args.depart_window)
    parse_named_clock(getattr(args, "arrive_before", None), role="arrive-before")
    parse_named_clock(getattr(args, "depart_after", None), role="depart-after")
    parse_via_airports(args.via)
    parse_via_airports(args.exclude_via, role="exclude-via")
    parse_via_airports(getattr(args, "exclude_airports", None), role="exclude-airports")
    if args.via and args.exclude_via:
        include = parse_via_airports(args.via) or ()
        exclude = parse_via_airports(args.exclude_via, role="exclude-via") or ()
        if set(include) & set(exclude):
            raise ValueError("--via and --exclude-via must not share a code")
    plan = parse_flight_plan(
        args.routes,
        trip=args.trip,
        max_stops=args.max_stops,
        adults=occupancy["adults"],
        children=occupancy["children"],
        infants_in_seat=occupancy["infants_in_seat"],
        infants_on_lap=occupancy["infants_on_lap"],
        cabin=args.cabin,
        bags=args.bags,
        carry_on=carry_on,
        price_cap_eur=args.price_cap,
    )
    today = date.today()
    for departure in _plan_departure_dates(plan):
        if departure < today:
            raise ValueError(f"departure date is in the past: {departure.isoformat()}")
    trips = _as_trips(plan)
    return expand_nearby_trips(trips, nearby=bool(getattr(args, "nearby", False)))


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
    if sort in ("fare", "price"):
        return offer.price_eur
    if sort == "duration":
        return offer.duration_hours if offer.duration_hours is not None else float("inf")
    if sort == "departure":
        minutes = _clock_minutes(offer.departure)
        return float(minutes) if minutes is not None else float("inf")
    if sort == "arrival":
        minutes = _clock_minutes(offer.arrival)
        return float(minutes) if minutes is not None else float("inf")
    return _ranked_total(offer)


def _format_ranking_columns(offer: FlightOffer) -> str:
    fare = f"{offer.price_eur:>7.0f} €"
    if offer.baggage_buffer_eur:
        return f"{fare}  {_ranked_total(offer):>7.0f} € ranked"
    extra = "  [baggage?]" if offer.needs_bag_verify else ""
    return f"{fare}{extra}"


def _format_compare_side(side: StopsCompareSide) -> str:
    label = _format_stops(side.stops_count)
    if side.layover_city:
        label = f"{label} {side.layover_city}"
    if side.layover_hours is not None:
        label = f"{label} {_format_layover_hours(side.layover_hours)}"
    duration = side.duration or "?"
    return f"{side.price_eur:.0f} €  {duration}  {label}  {_format_airline(side.airline)}"


def format_stops_compare(compare: StopsCompare) -> str:
    lines: list[str] = []
    if compare.nonstop is not None:
        lines.append(f"  Cheapest nonstop:  {_format_compare_side(compare.nonstop)}")
    else:
        lines.append("  Cheapest nonstop:  no nonstop")
    if compare.one_stop is not None:
        lines.append(f"  Cheapest 1-stop:   {_format_compare_side(compare.one_stop)}")
    return "\n".join(lines)


def _format_typical(offer: FlightOffer) -> str:
    line = offer.typical_deal()
    if not line:
        return ""
    text = f"  {line}"
    if offer.cheapest_date is not None and offer.cheapest_eur is not None:
        text += f"  cheapest {offer.cheapest_date.isoformat()} {offer.cheapest_eur:.0f} €"
    return text


def _format_parsed_bags(offer: FlightOffer) -> str:
    parts: list[str] = []
    if offer.checked_bags is not None:
        parts.append(f"{offer.checked_bags} checked")
    if offer.carry_on is not None:
        parts.append(f"{offer.carry_on} carry-on")
    if not parts:
        return ""
    return f"  {', '.join(parts)}"


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
        if sort == "ranked":
            out_value = _ranked_total(out_offer)
            back_value = _ranked_total(back_offer)
            unit = "ranked"
        else:
            out_value = out_offer.price_eur
            back_value = back_offer.price_eur
            unit = "fare"
        print(
            f"\nBest pair ({unit}): "
            f"{outbound.query.origin}->{outbound.query.destination} {out_value:.0f} € {unit} + "
            f"{inbound.query.origin}->{inbound.query.destination} {back_value:.0f} € {unit} = "
            f"{out_value + back_value:.0f} €"
        )
        index += 2


def _query_header(query: Trip) -> str:
    nearby = ""
    label = getattr(query, "nearby_label", None)
    if label:
        nearby = f"; {label}"
    if isinstance(query, RoundTrip):
        return (
            f"\n=== {query.origin} -> {query.destination}  "
            f"{query.departure_date.isoformat()} / {query.return_date.isoformat()} "
            f"(round-trip, max {query.max_stops} stop(s){nearby}) ==="
        )
    if isinstance(query, MultiCity):
        path = " / ".join(
            f"{leg.origin}->{leg.destination} {leg.departure_date.isoformat()}"
            for leg in query.legs
        )
        return f"\n=== {path} (multi-city) ==="
    return (
        f"\n=== {query.origin} -> {query.destination}  "
        f"{query.departure_date.isoformat()} (max {query.max_stops} stop(s){nearby}) ==="
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


def _google_flights_url_for(
    query,
    *,
    currency: str,
    country: Optional[str] = None,
    booking_token: Optional[str] = None,
    stored: Optional[str] = None,
) -> Optional[str]:
    if stored:
        return stored
    return google_flights_url(
        query, currency=currency, country=country, booking_token=booking_token
    )


def _print_google_flights_url(url: Optional[str], *, indent: str = "    ") -> None:
    if url:
        print(f"{indent}{url}")


def _print_report(report, *, sort: FlightSort = "ranked") -> None:
    any_success = False
    country = getattr(report, "country", None)
    currency = report.currency
    for result in report.queries:
        print(_query_header(result.query))
        query_url = _google_flights_url_for(
            result.query,
            currency=currency,
            country=country,
            stored=getattr(result, "google_flights_url", None),
        )
        if isinstance(result, QuerySuccess):
            any_success = True
            if not result.offers:
                print("  (no eligible offers)")
                _print_google_flights_url(query_url, indent="  ")
            print_per_offer = any(offer.booking_token for offer in result.offers)
            for offer in result.offers:
                times = f"{_format_clock(offer.departure)} -> {_format_clock(offer.arrival)}"
                print(
                    f"  {_format_ranking_columns(offer)}{_format_typical(offer)}"
                    f"{_format_parsed_bags(offer)}  "
                    f"{offer.duration or '?':<12} "
                    f"{_format_stops_with_layover(offer):<16} {times:<18} "
                    f"{_format_airline(offer.airline)}"
                )
                _print_offer_legs(offer)
                if print_per_offer:
                    _print_google_flights_url(
                        _google_flights_url_for(
                            result.query,
                            currency=currency,
                            country=country,
                            booking_token=offer.booking_token,
                            stored=offer.google_flights_url,
                        )
                    )
            if result.offers and not print_per_offer:
                _print_google_flights_url(query_url, indent="  ")
            if result.stops_compare is not None:
                print(format_stops_compare(result.stops_compare))
            print(
                f"  Raw: {result.raw_count}; "
                f"eligible: {result.eligible_count}; "
                f"shown: {len(result.offers)}"
            )
        elif isinstance(result, QueryFailure):
            print(f"  ERROR: {result.error.message}")
            _print_google_flights_url(query_url, indent="  ")
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

    if getattr(args, "nearby", False):
        for note in nearby_notes(
            queries,
            exclude_airports=parse_via_airports(
                getattr(args, "exclude_airports", None), role="exclude-airports"
            ),
        ):
            print(note, file=sys.stderr)

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
        alliances=parse_alliances(args.alliance),
        exclude_alliances=parse_alliances(args.exclude_alliance),
        depart_window=parse_depart_window(args.depart_window),
        arrive_before=parse_named_clock(args.arrive_before, role="arrive-before"),
        depart_after=parse_named_clock(args.depart_after, role="depart-after"),
        via=parse_via_airports(args.via),
        exclude_via=parse_via_airports(args.exclude_via, role="exclude-via"),
        exclude_airports=parse_via_airports(
            getattr(args, "exclude_airports", None), role="exclude-airports"
        ),
        currency=args.currency,
        country=args.country,
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


def _print_trip_total(report: TripSearchReport) -> None:
    if report.trip_total is None:
        return
    print(f"\n{format_trip_total(report.trip_total)}")


def _combined_exit_code(*reports: object) -> int:
    codes = [_exit_code(report) for report in reports]
    if all(code == 0 for code in codes):
        return 0
    if all(code == 2 for code in codes):
        return 2
    return 3


def _ensure_flight_validate_defaults(args: argparse.Namespace) -> None:
    defaults = {
        "children": 0,
        "infants_in_seat": 0,
        "infants_on_lap": 0,
        "bags": None,
        "carry_on": False,
        "price_cap": None,
        "max_layover": None,
        "min_layover": None,
        "max_duration": None,
        "airlines": None,
        "exclude_airlines": None,
        "alliance": None,
        "exclude_alliance": None,
        "depart_window": None,
        "arrive_before": None,
        "depart_after": None,
        "via": None,
        "exclude_via": None,
        "exclude_airports": None,
    }
    for key, value in defaults.items():
        if not hasattr(args, key):
            setattr(args, key, value)


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
    _ensure_flight_validate_defaults(args)
    try:
        trips = _parse_and_validate(args)
        hotel_query = _trip_hotel_query(args, trips)
        shop = _owned_shop_filters_from_args(args)
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
        buffer_eur=args.baggage_buffer,
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

    if args.save:
        destination = Path(args.save)
        write_trip_report_atomic(report, destination)
        print(f"\nSaved {destination}")

    return _combined_exit_code(report.flights, report.hotels)


def _print_dates_report(report: DateCalendarReport) -> None:
    stay = ""
    if report.trip == "rt" and report.nights is not None:
        night_word = "night" if report.nights == 1 else "nights"
        stay = f"  (rt, {report.nights} {night_word})"
    nearby = f"; {report.nearby_label}" if report.nearby_label else ""
    print(
        f"\n=== {report.origin} -> {report.destination}  "
        f"{report.start_date.isoformat()} .. {report.end_date.isoformat()}{stay}{nearby} ==="
    )
    _print_google_flights_url(report.google_flights_url, indent="  ")
    for line in format_week_calendar(report.days):
        print(line)
    spark = format_sparkline(report.days)
    if spark:
        print(f"  {spark}")
    if report.summary is not None:
        print(format_summary_line(report.summary))
    print()
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
        if row.stops_compare is not None:
            print(format_stops_compare(row.stops_compare))
    if any_price:
        print("\nVerify checked baggage on Google Flights before booking.")


def _print_explore_report(report: ExploreReport) -> None:
    nearby = f"; {report.nearby_label}" if report.nearby_label else ""
    print(
        f"\n=== From {report.origin}  {report.start_date.isoformat()}  "
        f"({report.days}-day window){nearby} ==="
    )
    _print_google_flights_url(report.google_flights_url, indent="  ")
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
        _print_google_flights_url(row.google_flights_url)
        if row.stops_compare is not None:
            print(format_stops_compare(row.stops_compare))
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


def _as_report_tuple(result: object) -> tuple:
    if isinstance(result, tuple):
        return result
    return (result,)


def _combine_exit_codes(codes: Sequence[int]) -> int:
    owned = tuple(codes)
    if not owned:
        return 0
    if all(code == 0 for code in owned):
        return 0
    if all(code == 2 for code in owned):
        return 2
    return 3


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


def _add_occupancy_flags(parser: argparse.ArgumentParser) -> None:
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


def _add_currency_country_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--currency",
        default="EUR",
        metavar="CODE",
        help="ISO 4217 currency for Google params (default EUR)",
    )
    parser.add_argument(
        "--country",
        default=None,
        metavar="CC",
        help="ISO country for Google gl (omit to leave unset; not a home-hub default)",
    )


def _market_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "currency": normalize_currency(getattr(args, "currency", "EUR") or "EUR"),
        "country": normalize_country(getattr(args, "country", None)),
    }


def _add_owned_shop_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bags",
        type=int,
        default=None,
        metavar="N",
        help="Checked bags on the shopping request (omit to leave unset)",
    )
    parser.add_argument(
        "--carry-on",
        action="store_true",
        dest="carry_on",
        help="Ask the shopping request for one carry-on (omit to leave unset)",
    )
    parser.add_argument(
        "--price-cap",
        type=int,
        default=None,
        metavar="EUR",
        dest="price_cap",
        help="Drop owned fares above this EUR amount (omit to leave unset; unnamed stays None)",
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


def _owned_shop_filters_from_args(args: argparse.Namespace) -> dict[str, object]:
    carry_on = 1 if args.carry_on else None
    if args.bags is not None and args.bags < 0:
        raise ValueError("--bags must not be negative")
    if args.price_cap is not None and args.price_cap <= 0:
        raise ValueError("--price-cap must be a positive EUR amount")
    via = parse_via_airports(args.via)
    exclude_via = parse_via_airports(args.exclude_via, role="exclude-via")
    if via and exclude_via and set(via) & set(exclude_via):
        raise ValueError("--via and --exclude-via must not share a code")
    max_layover = getattr(args, "max_layover", None)
    min_layover = getattr(args, "min_layover", None)
    max_duration = getattr(args, "max_duration", None)
    if max_layover is not None and max_layover < 0:
        raise ValueError("--max-layover must not be negative")
    if min_layover is not None and min_layover < 0:
        raise ValueError("--min-layover must not be negative")
    if max_duration is not None and max_duration < 0:
        raise ValueError("--max-duration must not be negative")
    if min_layover is not None and max_layover is not None and min_layover > max_layover:
        raise ValueError("--min-layover must be at or below --max-layover")
    return {
        "bags": args.bags,
        "carry_on": carry_on,
        "price_cap_eur": args.price_cap,
        "airlines": parse_airline_codes(args.airlines),
        "exclude_airlines": parse_airline_codes(args.exclude_airlines),
        "alliances": parse_alliances(getattr(args, "alliance", None)),
        "exclude_alliances": parse_alliances(getattr(args, "exclude_alliance", None)),
        "via": via,
        "exclude_via": exclude_via,
        "exclude_airports": parse_via_airports(
            getattr(args, "exclude_airports", None), role="exclude-airports"
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
        market = _market_from_args(args)
        FlightQuery(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap_eur=shop["price_cap_eur"],
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
        seed = calendar_trip(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            cabin=args.cabin,
            nights=nights,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap_eur=shop["price_cap_eur"],
            airlines=shop["airlines"],
            exclude_airlines=shop["exclude_airlines"],
            alliances=shop["alliances"],
            exclude_alliances=shop["exclude_alliances"],
            **occupancy,
        )
        for note in nearby_notes(
            expand_nearby_trips((seed,), nearby=True),
            exclude_airports=shop.get("exclude_airports"),
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
        progress=lambda line: print(line, file=sys.stderr),
        **shop,
        **occupancy,
        **market,
    )
    reports = _as_report_tuple(result)
    for report in reports:
        _print_dates_report(report)
    if args.save:
        destination_path = Path(args.save)
        write_dates_reports_atomic(reports, destination_path)
        print(f"\nSaved {destination_path}")
    return _combine_exit_codes(_dates_exit_code(report) for report in reports)


def _flex_exit_code(report: FlexSearchReport) -> int:
    if report.error is not None:
        if report.chosen_date is None:
            return 2
        return 3
    return 0


def _print_flex_report(report: FlexSearchReport) -> None:
    stay = ""
    if report.trip == "rt" and report.nights is not None:
        night_word = "night" if report.nights == 1 else "nights"
        stay = f"  (rt, {report.nights} {night_word})"
    nearby = f"; {report.nearby_label}" if report.nearby_label else ""
    print(
        f"\n=== {report.origin} -> {report.destination}  around {report.around.isoformat()} "
        f"±{report.flex_days}  {report.start_date.isoformat()} .. {report.end_date.isoformat()}"
        f"{stay}{nearby} ==="
    )
    if report.error is not None and report.chosen_date is None:
        print(f"  ERROR: {report.error.message}")
        _print_google_flights_url(report.google_flights_url, indent="  ")
        return
    if report.chosen_date is None:
        print("  (no priced day in window)")
        _print_google_flights_url(report.google_flights_url, indent="  ")
        return
    returning = (
        f"  return {report.return_date.isoformat()}" if report.return_date is not None else ""
    )
    print(f"  chosen {report.chosen_date.isoformat()}{returning}")
    if not report.offers:
        if report.error is not None:
            print(f"  ERROR: {report.error.message}")
        else:
            print("  (no eligible offers)")
        _print_google_flights_url(report.google_flights_url, indent="  ")
        return
    print_per_offer = any(offer.booking_token for offer in report.offers)
    for offer in report.offers:
        times = f"{_format_clock(offer.departure)} -> {_format_clock(offer.arrival)}"
        print(
            f"  {_format_ranking_columns(offer)}{_format_typical(offer)}"
            f"{_format_parsed_bags(offer)}  "
            f"{offer.duration or '?':<12} "
            f"{_format_stops_with_layover(offer):<16} {times:<18} "
            f"{_format_airline(offer.airline)}"
        )
        if print_per_offer:
            _print_google_flights_url(offer.google_flights_url)
    if not print_per_offer:
        _print_google_flights_url(report.google_flights_url, indent="  ")
    if report.stops_compare is not None:
        print(format_stops_compare(report.stops_compare))
    print("\nVerify checked baggage on Google Flights before booking.")


def _run_flex(args: argparse.Namespace) -> int:
    try:
        origin, destination = parse_route_pair(args.route)
        around = _parse_iso_date(args.around, "--around")
        if args.flex_days < 1:
            raise ValueError("--flex must be at least 1")
        occupancy = _occupancy_from_args(args)
        if args.top <= 0:
            raise ValueError("--top must be a positive integer")
        if args.baggage_buffer < 0:
            raise ValueError("--baggage-buffer must not be negative")
        start, _end = flex_window(around, args.flex_days)
        trip, nights = resolve_date_trip(args.trip, args.nights)
        shop = _owned_shop_filters_from_args(args)
        market = _market_from_args(args)
        FlightQuery(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap_eur=shop["price_cap_eur"],
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
        seed = calendar_trip(
            origin,
            destination,
            start,
            max_stops=args.max_stops,
            cabin=args.cabin,
            nights=nights,
            bags=shop["bags"],
            carry_on=shop["carry_on"],
            price_cap_eur=shop["price_cap_eur"],
            airlines=shop["airlines"],
            exclude_airlines=shop["exclude_airlines"],
            alliances=shop["alliances"],
            exclude_alliances=shop["exclude_alliances"],
            **occupancy,
        )
        for note in nearby_notes(
            expand_nearby_trips((seed,), nearby=True),
            exclude_airports=shop.get("exclude_airports"),
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
        buffer_eur=args.baggage_buffer,
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
    if args.save:
        destination_path = Path(args.save)
        write_flex_reports_atomic(reports, destination_path)
        print(f"\nSaved {destination_path}")
    return _combine_exit_codes(_flex_exit_code(report) for report in reports)


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
        occupancy = _occupancy_from_args(args)
        shop = _owned_shop_filters_from_args(args)
        market = _market_from_args(args)
        validate_explore_window(start, days)
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
        progress=lambda line: print(line, file=sys.stderr),
        **shop,
        **occupancy,
        **market,
    )
    reports = _as_report_tuple(result)
    for report in reports:
        _print_explore_report(report)
    if args.save:
        destination_path = Path(args.save)
        write_explore_reports_atomic(reports, destination_path)
        print(f"\nSaved {destination_path}")
    return _combine_exit_codes(
        2 if report.error is not None and not report.destinations else 0 for report in reports
    )


def _run_airports(args: argparse.Namespace) -> int:
    return _print_airports(args.query)


_PARSER: Optional[argparse.ArgumentParser] = None


def _build_parser() -> argparse.ArgumentParser:
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
            "Open-jaw --trip rt is two ORIGIN-DEST:DATE routes (one package). "
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
        "--children",
        type=int,
        default=0,
        help="Children aged 2-11 (default 0)",
    )
    flights.add_argument(
        "--infants-in-seat",
        type=int,
        default=0,
        dest="infants_in_seat",
        help="Infants in their own seat (default 0)",
    )
    flights.add_argument(
        "--infants-on-lap",
        type=int,
        default=0,
        dest="infants_on_lap",
        help="Infants on lap (default 0)",
    )
    flights.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    flights.add_argument(
        "--currency",
        default="EUR",
        metavar="CODE",
        help="ISO 4217 currency for Google params (default EUR)",
    )
    flights.add_argument(
        "--country",
        default=None,
        metavar="CC",
        help="ISO country for Google gl (omit to leave unset; not a home-hub default)",
    )
    flights.add_argument(
        "--bags",
        type=int,
        default=None,
        metavar="N",
        help="Checked bags on the shopping request (omit to leave unset)",
    )
    flights.add_argument(
        "--carry-on",
        action="store_true",
        dest="carry_on",
        help="Ask the shopping request for one carry-on (omit to leave unset)",
    )
    flights.add_argument(
        "--price-cap",
        type=int,
        default=None,
        metavar="EUR",
        dest="price_cap",
        help="Drop owned fares above this EUR amount (omit to leave unset; unnamed stays None)",
    )
    _add_nearby_flag(flights)
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
        choices=list(FLIGHT_SORTS),
        help=(
            "Order and select --top by ranked total (default), fare/price, duration, "
            "departure, or arrival"
        ),
    )
    flights.add_argument(
        "--airlines",
        default=None,
        metavar="CODES",
        help="Airline IATA codes on the shopping request (comma-separated, e.g. BA,KL)",
    )
    flights.add_argument(
        "--exclude-airlines",
        default=None,
        dest="exclude_airlines",
        metavar="CODES",
        help="Airline IATA codes to exclude from shopping (comma-separated, e.g. DL)",
    )
    flights.add_argument(
        "--alliance",
        default=None,
        metavar="NAMES",
        help="Restrict the shopping request to these alliances (oneworld, skyteam, star)",
    )
    flights.add_argument(
        "--exclude-alliance",
        default=None,
        dest="exclude_alliance",
        metavar="NAMES",
        help="Exclude these alliances from the shopping request (oneworld, skyteam, star)",
    )
    flights.add_argument(
        "--depart-window",
        default=None,
        dest="depart_window",
        metavar="START-END",
        help="Keep local departures in START-END inclusive (hours 6-20 or clocks 06:00-20:00)",
    )
    flights.add_argument(
        "--arrive-before",
        default=None,
        dest="arrive_before",
        metavar="HH:MM",
        help=(
            "Keep local arrivals at or before HH:MM. Post-filter on owned offer clocks; "
            "unknown arrival cannot prove the bound"
        ),
    )
    flights.add_argument(
        "--depart-after",
        default=None,
        dest="depart_after",
        metavar="HH:MM",
        help=(
            "Keep local departures at or after HH:MM. Post-filter on owned offer clocks; "
            "unknown departure cannot prove the bound"
        ),
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
        "--via",
        default=None,
        metavar="CODES",
        dest="via",
        help=(
            "Keep connecting offers whose parsed layover matches these IATA codes "
            "(comma-separated). Post-filter only; unknown layover cannot prove a via"
        ),
    )
    flights.add_argument(
        "--exclude-via",
        default=None,
        metavar="CODES",
        dest="exclude_via",
        help=(
            "Drop connecting offers whose parsed layover matches these IATA codes "
            "(comma-separated). Unknown layover stays"
        ),
    )
    flights.add_argument(
        "--exclude-airports",
        default=None,
        metavar="CODES",
        dest="exclude_airports",
        help=(
            "Drop named origin/dest IATA in this list (comma-separated). "
            "Nearby cannot sneak an excluded same-city code back. Unnamed stays unset"
        ),
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
    trip.add_argument(
        "--trip",
        default="one-way",
        type=normalize_trip_kind,
        metavar="{one-way,rt,multi}",
        help=(
            "Trip kind (default one-way). rt/round-trip POSTs one package. "
            "Sugar without --trip stays two one-ways."
        ),
    )
    trip.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Maximum stops (default 1). 2 means two-or-fewer.",
    )
    trip.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Adults for both flights and the hotel (default 1)",
    )
    trip.add_argument(
        "--rooms",
        type=int,
        default=1,
        help="Hotel rooms (default 1)",
    )
    trip.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    trip.add_argument(
        "--currency",
        default="EUR",
        metavar="CODE",
        help="ISO 4217 currency for Google params (default EUR)",
    )
    trip.add_argument(
        "--country",
        default=None,
        metavar="CC",
        help="ISO country for Google gl (omit to leave unset; not a home-hub default)",
    )
    trip.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers per query (default {DEFAULT_TOP})",
    )
    trip.add_argument(
        "--baggage-buffer",
        type=int,
        default=DEFAULT_BAGGAGE_BUFFER_EUR,
        metavar="EUR",
        help=(
            f"EUR added to low-cost fares when ranking (default {DEFAULT_BAGGAGE_BUFFER_EUR}). "
            "Trip total uses the owned cabin fare, not this buffer."
        ),
    )
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
            "auto uses sweep for 3+ flight queries and detail for 1-2 (default auto)"
        ),
    )
    trip.add_argument(
        "--source",
        default="booking",
        choices=["booking", "google"],
        help=(
            "Hotel source. booking is the Playwright evidence path (CLI default). "
            "google is the HTTP shortlist (MCP default)."
        ),
    )
    trip.add_argument(
        "--min-rating",
        type=float,
        default=None,
        dest="min_rating",
        metavar="SCORE",
        help="Minimum hotel review score (Booking 0-10; Google Hotels 0-5)",
    )
    trip.add_argument(
        "--entire-home",
        action="store_true",
        help=("Require entire homes/apartments (cards with unknown property type may remain)"),
    )
    trip.add_argument(
        "--allow-non-refundable",
        action="store_true",
        help="Include non-refundable stays (default filters to free cancellation)",
    )
    _add_owned_shop_filters(trip)
    _add_nearby_flag(trip)
    trip.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON (flights, hotels, optional trip_total) atomically to FILE",
    )

    dates = sub.add_parser(
        "dates",
        help="Cheapest fare per day for one route (compact calendar; --currency, default EUR)",
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
        "--trip",
        default="one-way",
        type=normalize_trip_kind,
        metavar="{one-way,rt}",
        help=(
            "Trip kind (default one-way). rt/round-trip is one packaged stay per "
            "departure day and needs --nights. --nights without --trip is rt. "
            "multi is not supported."
        ),
    )
    dates.add_argument(
        "--nights",
        type=int,
        default=None,
        metavar="N",
        help="Stay length in nights for a round-trip calendar. Implies --trip rt.",
    )
    dates.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Maximum stops (default 1). 2 means two-or-fewer.",
    )
    dates.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    _add_occupancy_flags(dates)
    dates.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    _add_currency_country_flags(dates)
    _add_owned_shop_filters(dates)
    _add_nearby_flag(dates)
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

    flex = sub.add_parser(
        "flex",
        help="Cheapest day in a ±N window, then one shopping search (--currency, default EUR)",
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
    flex.add_argument(
        "--trip",
        default="one-way",
        type=normalize_trip_kind,
        metavar="{one-way,rt}",
        help=(
            "Trip kind (default one-way). rt/round-trip needs --nights. "
            "--nights without --trip is rt. multi is not supported."
        ),
    )
    flex.add_argument(
        "--nights",
        type=int,
        default=None,
        metavar="N",
        help="Stay length in nights for a packaged round-trip. Implies --trip rt.",
    )
    flex.add_argument(
        "--max-stops",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Maximum stops (default 1). 2 means two-or-fewer.",
    )
    flex.add_argument(
        "--adults",
        type=int,
        default=1,
        help="Number of adults (default 1)",
    )
    _add_occupancy_flags(flex)
    flex.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    _add_currency_country_flags(flex)
    flex.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Offers to show from the chosen day (default {DEFAULT_TOP})",
    )
    flex.add_argument(
        "--baggage-buffer",
        type=int,
        default=DEFAULT_BAGGAGE_BUFFER_EUR,
        metavar="EUR",
        help=(f"EUR added to low-cost fares when ranking (default {DEFAULT_BAGGAGE_BUFFER_EUR})"),
    )
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
        help="Uses the date-grid RPC plus one HTTP shopping POST. detail is accepted and ignored.",
    )
    flex.add_argument(
        "--save",
        default=None,
        metavar="FILE",
        help="Write JSON report atomically to FILE",
    )

    explore = sub.add_parser(
        "explore",
        help="Cheap destinations from one origin (--currency, default EUR)",
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
    _add_occupancy_flags(explore)
    explore.add_argument(
        "--cabin",
        default="economy",
        choices=["economy", "premium-economy", "business", "first"],
        help="Cabin class (default economy)",
    )
    _add_currency_country_flags(explore)
    _add_owned_shop_filters(explore)
    _add_nearby_flag(explore)
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

    bench = sub.add_parser(
        "bench",
        help="Offline keep-or-revert bench (unittest + owned parse corpus)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=BENCH_EXAMPLES,
    )
    bench.add_argument(
        "--prompts",
        action="store_true",
        help=(
            "Run the graded prompt battery instead of the speed bench. "
            "Deterministic tiers are offline. LLM judge is opt-in "
            "(VIAJANTE_BENCH_JUDGE=1, DEEPSEEK_API_KEY or VIAJANTE_JUDGE_KEY), "
            "scores 1-100, and is never the score_ms. Unset key prints judge: skip."
        ),
    )
    bench.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "With --prompts, load only tests/prompts/holdout.jsonl. "
            "Not part of the weekday 90. Operator overfitting check."
        ),
    )
    bench.add_argument(
        "--timeit-sweep",
        action="store_true",
        help=(
            "With --prompts, time HTTP sweep (fetch=sweep) for up to 8 planned "
            "IATA+date flight queries. Same as VIAJANTE_BENCH_SWEEP=1. Off by "
            "default. Never judge_mean or score_ms. No Playwright."
        ),
    )
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
    if args.cmd == "bench":
        sweep_kw = {"timeit_sweep": True} if args.timeit_sweep else {}
        if args.holdout:
            return run_prompt_bench(holdout=True, **sweep_kw)
        if args.prompts or os.environ.get(PROMPTS_ENV) == "1" or args.timeit_sweep:
            return run_prompt_bench(**sweep_kw)
        return run_bench()

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
