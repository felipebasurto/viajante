"""Terminal tables and report text for the viajante CLI. Pure: no argument parsing."""

from __future__ import annotations

import re
import sys
from typing import Optional, Sequence, Tuple

from viajante.airports import lookup_airports
from viajante.dates import (
    format_sparkline,
    format_summary_line,
    format_week_calendar,
)
from viajante.flight_offers import FlightSort, _effective_cost
from viajante.google_flights import google_flights_url
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    DateCalendarReport,
    ExploreReport,
    FlexSearchReport,
    FlightOffer,
    HiddenCityReport,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    LodgingKind,
    MultiCity,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RoundTrip,
    StopsCompare,
    StopsCompareSide,
    Trip,
    TripSearchReport,
    format_money,
)
from viajante.parsers import clock_minutes as _clock_minutes
from viajante.recommend import Recommendation
from viajante.split import (
    MAX_OTHER_CURRENCY_ROWS,
    TIMING_UNPROVEN_NOTE,
    SplitReport,
)
from viajante.trip import (
    format_trip_total,
)


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


def _format_stops_with_layover(offer: FlightOffer | StopsCompareSide) -> str:
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


def _sort_value(offer: FlightOffer, sort: FlightSort) -> float:
    if sort in ("fare", "price"):
        return offer.price
    if sort == "duration":
        return offer.duration_hours if offer.duration_hours is not None else float("inf")
    if sort == "departure":
        minutes = _clock_minutes(offer.departure)
        return float(minutes) if minutes is not None else float("inf")
    if sort == "arrival":
        minutes = _clock_minutes(offer.arrival)
        return float(minutes) if minutes is not None else float("inf")
    return _effective_cost(offer)


def _format_ranking_columns(offer: FlightOffer, currency: str) -> str:
    fare = format_money(offer.price, currency, width=7)
    if offer.baggage_buffer:
        return f"{fare}  {format_money(_effective_cost(offer), currency, width=7)} ranked"
    extra = "  [baggage?]" if offer.needs_bag_verify else ""
    return f"{fare}{extra}"


def _format_offer_row(offer: FlightOffer, currency: str) -> str:
    times = f"{_format_clock(offer.departure)} -> {_format_clock(offer.arrival)}"
    return (
        f"  {_format_ranking_columns(offer, currency)}"
        f"{_format_typical(offer, currency)}"
        f"{_format_parsed_bags(offer)}  "
        f"{offer.duration or '?':<12} "
        f"{_format_stops_with_layover(offer):<16} {times:<18} "
        f"{_format_airline(offer.airline)}"
        f"{_format_flight_numbers(offer.flight_numbers)}"
    )


def format_recommendation(recommendation: Recommendation, currency: str) -> str:
    lines = ["  Recommendation (score 0-100, higher is better; weights in the JSON):"]
    if recommendation.relaxed_requirements:
        lines.append(f"    Relaxed requirements: {', '.join(recommendation.relaxed_requirements)}")
    for entry in recommendation.entries:
        lines.append(f"    [{', '.join(entry.labels)}] score {entry.score:g}")
        lines.append(f"  {_format_offer_row(entry.offer, currency)}")
        lines.append(f"      + {'; '.join(entry.highlights)}")
        lines.append(f"      - {'; '.join(entry.tradeoffs)}")
    lines.extend(f"    Note: {note}" for note in recommendation.notes)
    return "\n".join(lines)


def _format_compare_side(side: StopsCompareSide, currency: str) -> str:
    duration = side.duration or "?"
    return (
        f"{format_money(side.price, currency)}  {duration}  {_format_stops_with_layover(side)}  "
        f"{_format_airline(side.airline)}"
    )


def format_stops_compare(compare: StopsCompare, currency: str) -> str:
    lines: list[str] = []
    if compare.nonstop is not None:
        lines.append(f"  Cheapest nonstop:  {_format_compare_side(compare.nonstop, currency)}")
    else:
        lines.append("  Cheapest nonstop:  no nonstop")
    if compare.one_stop is not None:
        lines.append(f"  Cheapest 1-stop:   {_format_compare_side(compare.one_stop, currency)}")
    return "\n".join(lines)


def _format_typical_deal(row: object, currency: str) -> str:
    deal = getattr(row, "typical_deal", None)
    if not callable(deal):
        return ""
    line = deal(currency)
    if not line:
        return ""
    return f"  {line}"


def _format_typical(offer: FlightOffer, currency: str) -> str:
    return _format_typical_deal(offer, currency)


def _format_parsed_bags(offer: FlightOffer) -> str:
    parts: list[str] = []
    if offer.checked_bags is not None:
        parts.append(f"{offer.checked_bags} checked")
    if offer.carry_on is not None:
        parts.append(f"{offer.carry_on} carry-on")
    if not parts:
        return ""
    return f"  {', '.join(parts)}"


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
            out_value = _effective_cost(out_offer)
            back_value = _effective_cost(back_offer)
            unit = "ranked"
        else:
            out_value = out_offer.price
            back_value = back_offer.price
            unit = "fare"
        currency = report.currency
        print(
            f"\nBest pair ({unit}): "
            f"{outbound.query.origin}->{outbound.query.destination} "
            f"{format_money(out_value, currency)} {unit} + "
            f"{inbound.query.origin}->{inbound.query.destination} "
            f"{format_money(back_value, currency)} {unit} = "
            f"{format_money(out_value + back_value, currency)}"
        )
        index += 2


VERIFY_BAGGAGE_LINE = "\nVerify checked baggage on Google Flights before booking."


def _nearby_suffix(label: Optional[str]) -> str:
    return f"; {label}" if label else ""


def _stay_label(report: DateCalendarReport | FlexSearchReport) -> str:
    if report.trip == "rt" and report.nights is not None:
        night_word = "night" if report.nights == 1 else "nights"
        return f"  (rt, {report.nights} {night_word})"
    return ""


def _query_header(query: Trip) -> str:
    nearby = _nearby_suffix(getattr(query, "nearby_label", None))
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


def _format_flight_numbers(numbers: Optional[Tuple[str, ...]]) -> str:
    if not numbers:
        return ""
    return "  " + " ".join(numbers)


def _leg_airline(leg: RawJourneyLeg) -> Optional[str]:
    names: list[str] = []
    for segment in leg.segments:
        if segment.airline and segment.airline not in names:
            names.append(segment.airline)
    if names:
        return ", ".join(names)
    return None


def _leg_flight_numbers(leg: RawJourneyLeg) -> Tuple[str, ...]:
    return tuple(segment.flight_number for segment in leg.segments if segment.flight_number)


def _format_leg_stops(leg: RawJourneyLeg) -> str:
    label = leg.stops or "?"
    layover = None
    if leg.layovers:
        layover = max(leg.layovers, key=lambda row: row.hours or 0.0)
    if layover is not None and layover.city:
        label = f"{label} {layover.city}"
    if layover is not None and layover.hours is not None:
        label = f"{label} {_format_layover_hours(layover.hours)}"
    return label


def _print_offer_legs(offer: FlightOffer, query: Trip) -> None:
    expected = len(query.legs)
    for index, leg in enumerate(offer.legs[1:], start=2):
        times = f"{_format_clock(leg.departure)} -> {_format_clock(leg.arrival)}"
        label = "return" if expected == 2 else f"leg {index}"
        print(
            f"    {label}  {leg.duration or '?':<12} {_format_leg_stops(leg):<16} "
            f"{times:<18} {_format_airline(_leg_airline(leg))}"
            f"{_format_flight_numbers(_leg_flight_numbers(leg))}"
        )
    for index in range(len(offer.legs) + 1, expected + 1):
        label = "return" if expected == 2 else f"leg {index}"
        print(f"    {label}  unknown")


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
    currency = report.currency
    for result in report.queries:
        print(_query_header(result.query))
        query_url = _google_flights_url_for(
            result.query,
            currency=currency,
            stored=getattr(result, "google_flights_url", None),
        )
        if isinstance(result, QuerySuccess):
            any_success = True
            if not result.offers:
                print("  (no eligible offers)")
                _print_google_flights_url(query_url, indent="  ")
            print_per_offer = any(offer.booking_token for offer in result.offers)
            for offer in result.offers:
                print(_format_offer_row(offer, currency))
                _print_offer_legs(offer, result.query)
                if print_per_offer:
                    _print_google_flights_url(
                        _google_flights_url_for(
                            result.query,
                            currency=currency,
                            booking_token=offer.booking_token,
                            stored=offer.google_flights_url,
                        )
                    )
            if result.offers and not print_per_offer:
                _print_google_flights_url(query_url, indent="  ")
            if result.stops_compare is not None:
                print(format_stops_compare(result.stops_compare, currency))
            if result.recommendation is not None:
                print(format_recommendation(result.recommendation, currency))
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
        print(VERIFY_BAGGAGE_LINE)


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
    label = {"booking.com": "Booking chips", "skiplagged": "Skiplagged chips"}.get(
        provider, "Google chips"
    )
    print(f"  Filters: {_format_hotel_filter_gloss(query)}")
    print(f"  {label}: {chips}")
    print(f"  Search link context: {applied.url_context}")
    if applied.not_applied:
        names = ", ".join(applied.not_applied)
        print(f"  Not applied by this source: {names} (rows carry no evidence for it)")


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
        "fc=2" in applied.chips or "free_cancellation=1" in applied.chips
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
    if offer.lodging_evidence_conflict:
        print("    Lodging evidence: conflicting room and entire-home labels")
    if offer.priced_adults is None:
        print("    Priced party: unverified; confirm the total for the requested occupancy")
    else:
        print(f"    Priced party: {offer.priced_adults} adult(s)")
    if offer.link:
        print(
            f"    Link context: {offer.link_context}; no guarantee of current price or availability"
        )
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
        if current is None or offer.total_price < current.total_price:
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
            min(offer.total_price for offer in (row[1], row[2]) if offer is not None),
            row[0].title.casefold(),
        )
    )
    return tuple(rows)


def _print_cancellation_compare(
    free_result: HotelQuerySuccess,
    open_result: HotelQuerySuccess,
    currency: str,
) -> None:
    print("\n=== Cancellation compare ===")
    rows = _join_cancellation_rows(free_result.offers, open_result.offers)
    if not rows:
        print("  (no matching stays)")
        return
    for sample, free_offer, open_offer in rows:
        free_label = (
            format_money(free_offer.total_price, currency) if free_offer is not None else "—"
        )
        open_label = (
            format_money(open_offer.total_price, currency) if open_offer is not None else "—"
        )
        delta = ""
        if free_offer is not None and open_offer is not None:
            diff = free_offer.total_price - open_offer.total_price
            delta = f"  delta {format_money(diff, currency)}"
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
        _print_cancellation_compare(queries[0], queries[1], report.currency)
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
            for error in result.page_errors:
                print(f"  Partial results: additional page failed: {error.message}")
            if result.resolved_place:
                print(f"  Resolved place: {result.resolved_place}")
            if report.near:
                print("  Distances are straight-line distances to the named near point")
            if report.max_distance_km is not None:
                print(
                    f"  Maximum distance: {report.max_distance_km:g} km; "
                    "unknown coordinates excluded"
                )
            if not result.offers:
                print("  (no eligible stays)")
                if report.max_distance_km is not None:
                    print(
                        "  No returned offer proved the requested radius; "
                        "not proof of unavailability"
                    )
            for offer in result.offers:
                rating = f"{offer.rating_score:.1f}" if offer.rating_score is not None else "-"
                if offer.review_count is not None:
                    rating += f" ({offer.review_count} reviews)"
                address = f"  {offer.address}" if offer.address else ""
                away = f"  {offer.distance_km:.1f} km away" if offer.distance_km is not None else ""
                print(
                    f"  {format_money(offer.total_price, report.currency)} total stay  "
                    f"rating {rating}  {offer.title}{address}{away}"
                )
                _print_hotel_offer_details(offer, query=query, applied=result.applied)
            print(
                f"  Raw cards: {result.raw_count}; "
                f"eligible: {result.eligible_count}; "
                f"shown: {len(result.offers)}"
            )
        elif isinstance(result, HotelQueryFailure):
            print(f"  ERROR: {result.error.message}")
    if any_success:
        site = {"google-hotels": "Google Hotels", "skiplagged": "Skiplagged"}.get(
            report.provider, "Booking.com"
        )
        print(
            f"\nVerify the final total stay price and cancellation terms on {site} before booking."
        )


def _exit_code(report) -> int:
    return _status_exit_code(report.queries)


def _status_exit_code(rows: Sequence) -> int:
    failures = sum(row.status == "error" for row in rows)
    if failures == 0:
        return 0
    return 2 if failures == len(rows) else 3


def _format_connection(minutes: Optional[int]) -> str:
    if minutes is None:
        return "?"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest:02d}m"


def _print_split_report(report: SplitReport) -> None:
    query = report.query
    label = "self-transfer via a hub" if report.kind == "hub" else "mixed one-ways"
    print(f"\n=== SPLIT TICKETS ({label}): {query.origin} -> {query.destination} ===")
    print(f"  {report.warning}")
    if report.kind == "hub":
        hubs = ", ".join(report.hubs) or "none"
        source = f" ({report.hubs_source.replace('_', ' ')})" if report.hubs_source else ""
        overnight = "; next-day connections allowed" if report.allow_overnight else ""
        print(
            f"  Hubs tried: {hubs}{source}; minimum connection "
            f"{_format_connection(report.min_connection_minutes)}{overnight}"
        )
        for row in report.skipped_hubs:
            print(f"  Hub {row['hub']} not searched: {row['reason'].replace('_', ' ')}")
    print(f"  Extra searches: {report.extra_searches} of at most {report.max_extra_searches}")
    if report.error is not None:
        print(f"  ERROR: {report.error.message}")
    if not report.itineraries:
        print("  (no split itinerary from the quotes returned)")
    for number, row in enumerate(report.itineraries, start=1):
        total = (
            f"{format_money(row.total, row.currency)} total"
            if row.total is not None and row.currency
            else "total unknown (parts are in different currencies; nothing converts)"
        )
        via = ""
        if row.kind == "hub":
            overnight = ", overnight" if row.overnight_at_hub else ""
            via = f"  via {row.hub}, {_format_connection(row.connection_minutes)} connection"
            via += overnight
        print(f"\n  {number}. {total}{via}")
        if row.kind == "mixed_one_ways" and not row.timing_proven:
            print(f"     NOTE: {TIMING_UNPROVEN_NOTE}")
        if row.savings is not None and row.packaged is not None:
            amount = format_money(abs(row.savings), row.packaged.currency)
            verb = "saves" if row.savings >= 0 else "costs"
            tail = "" if row.savings >= 0 else " more"
            print(
                f"     {verb} {amount}{tail} vs best packaged "
                f"{format_money(row.packaged.price, row.packaged.currency)}"
            )
        elif row.packaged is not None and row.total is not None:
            print(f"     not compared: packaged quote is in {row.packaged.currency}")
        for ticket, part in enumerate(row.parts, start=1):
            leg = part.query
            print(
                f"     ticket {ticket} ({part.role.replace('_', ' ')})  "
                f"{leg.origin} -> {leg.destination}  {leg.departure_date.isoformat()}"
            )
            print(f"  {_format_offer_row(part.offer, part.currency)}")
            _print_google_flights_url(part.google_flights_url, indent="       ")
    if report.omitted_other_currency:
        print(
            f"\n  {report.omitted_other_currency} more row(s) in other currencies or with an "
            f"unknown total not shown (at most {MAX_OTHER_CURRENCY_ROWS} per group)"
        )
    if report.rejected:
        rejected = ", ".join(f"{key.replace('_', ' ')} {n}" for key, n in report.rejected.items())
        print(f"\n  Pairings rejected: {rejected}")
    if report.itineraries:
        print("\nConfirm each ticket on its own link and verify its baggage before booking.")


def _print_trip_total(report: TripSearchReport) -> None:
    if report.trip_total is None:
        return
    print(f"\n{format_trip_total(report.trip_total, report.currency)}")


def _print_dates_report(report: DateCalendarReport) -> None:
    stay = _stay_label(report)
    nearby = _nearby_suffix(report.nearby_label)
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
        print(format_summary_line(report.summary, report.currency))
    print()
    any_price = False
    currency = report.currency
    for row in report.days:
        if row.status == "error" and row.error is not None:
            print(f"  {row.departure_date.isoformat()}   ERROR: {row.error.message}")
            continue
        if row.price is None:
            print(f"  {row.departure_date.isoformat()}      —")
            continue
        any_price = True
        extra = ""
        if row.airline:
            extra += f"  {row.airline}"
        if row.stops_count is not None:
            extra += f"  {_format_stops(row.stops_count)}"
        deal = _format_typical_deal(row, currency)
        print(
            f"  {row.departure_date.isoformat()}  "
            f"{format_money(row.price, currency, width=7)}{extra}{deal}"
        )
        if row.stops_compare is not None:
            print(format_stops_compare(row.stops_compare, currency))
    if any_price:
        print(VERIFY_BAGGAGE_LINE)


def _print_explore_report(report: ExploreReport) -> None:
    nearby = _nearby_suffix(report.nearby_label)
    print(
        f"\n=== From {report.origin}  {report.start_date.isoformat()}  "
        f"({report.days}-day stay; dests priced on this date){nearby} ==="
    )
    _print_google_flights_url(report.google_flights_url, indent="  ")
    for failure in report.pricing_errors:
        print(f"  ERROR pricing {failure.query.destination}: {failure.error.message}")
    if report.error is not None and not report.destinations:
        print(f"  ERROR: {report.error.message}")
        return
    if not report.destinations:
        print("  (no destinations)")
        return
    currency = report.currency
    for row in report.destinations:
        price = format_money(row.price, currency, width=7) if row.price is not None else "      —"
        country = f"  {row.country}" if row.country else ""
        hours = ""
        if row.duration_hours is not None:
            hours = f"  {_format_layover_hours(row.duration_hours)}"
        print(
            f"  {price}  {row.iata}  {row.city}{country}{hours}"
            f"{_format_typical_deal(row, currency)}"
        )
        _print_google_flights_url(row.google_flights_url)
        if row.stops_compare is not None:
            print(format_stops_compare(row.stops_compare, currency))
    print(VERIFY_BAGGAGE_LINE)


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
        metro = row.to_dict().get("metro")
        print(
            f"  {row.iata}  {row.name}  {city}  {country}" + (f"  metro {metro}" if metro else "")
        )
    return 0


def _dates_exit_code(report: DateCalendarReport) -> int:
    return _status_exit_code(report.days)


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


def _flex_exit_code(report: FlexSearchReport) -> int:
    if report.error is not None:
        if report.chosen_date is None:
            return 2
        return 3
    return 0


def _print_flex_report(report: FlexSearchReport) -> None:
    stay = _stay_label(report)
    nearby = _nearby_suffix(report.nearby_label)
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
        print(_format_offer_row(offer, report.currency))
        if print_per_offer:
            _print_google_flights_url(offer.google_flights_url)
    if not print_per_offer:
        _print_google_flights_url(report.google_flights_url, indent="  ")
    if report.stops_compare is not None:
        print(format_stops_compare(report.stops_compare, report.currency))
    print(VERIFY_BAGGAGE_LINE)


def _print_hidden_city_report(report: HiddenCityReport) -> None:
    back = f" / {report.return_date.isoformat()}" if report.return_date else ""
    print(
        f"\n=== {report.origin} -> {report.destination}  "
        f"{report.departure_date.isoformat()}{back} (skiplagged) ==="
    )
    for line in report.warnings:
        print(f"note: {line}", file=sys.stderr)
    if report.error is not None:
        print(f"error: {report.error.code.value}: {report.error.message}", file=sys.stderr)
    if not report.offers:
        print("No priced Skiplagged itineraries.")
        return
    print(f"{'price':>12}  {'hidden':<7}  airline")
    for offer in report.offers:
        flag = {True: "yes", False: "no"}.get(offer.hidden_city, "unknown")
        airline = offer.airline or "?"
        extra = ""
        if offer.ticketed_destination:
            extra += f"  ticketed {offer.ticketed_destination}"
        if offer.layover_city:
            extra += f"  via {offer.layover_city}"
        print(f"{format_money(offer.price, offer.currency, width=10)}  {flag:<7}  {airline}{extra}")
        if offer.booking_url:
            print(f"    {offer.booking_url}")


def _print_hotel_rooms_report(report) -> None:
    name = report.name or report.requested_name or f"hotel {report.hotel_id}"
    print(
        f"\n=== {name}  {report.check_in.isoformat()} -> {report.check_out.isoformat()} "
        f"({report.adults} adult(s), {report.rooms} room(s)) ==="
    )
    if report.error is not None:
        print(f"  ERROR: {report.error.message}")
        return
    for rate in report.rates:
        refund = {True: "refundable", False: "non-refundable", None: "refund unknown"}[
            rate.refundable
        ]
        free = {True: ", free cancellation", False: "", None: ""}[rate.free_cancellation]
        limit = f"  up to {rate.occupancy_limit}" if rate.occupancy_limit is not None else ""
        print(
            f"  {format_money(rate.total_price, report.currency)} total stay  "
            f"{rate.title}{limit}  ({refund}{free})"
        )
    print(f"\nQuotes are {report.currency} and are not converted. Verify on Skiplagged.")
