"""Stdio MCP entry. Importable only when the mcp extra is installed."""

from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Literal, Optional, Sequence, TypeVar

from viajante.evidence import verify_answer as verify_answer_tool
from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.flights import DEFAULT_TOP
from viajante.mcp_handlers import (
    compare_awards_tool,
    get_flight_details_tool,
    get_hotel_details_tool,
    lookup_airports_tool,
    lookup_transfers_tool,
    plan_stay_blocks_tool,
    search_dates_tool,
    search_explore_tool,
    search_flex_tool,
    search_flights_tool,
    search_hidden_city_tool,
    search_hotel_rooms_tool,
    search_hotels_tool,
    search_self_transfer_tool,
    search_trip_tool,
    split_stay_costs_tool,
    validate_itinerary_tool,
)
from viajante.runtime import get_runtime_info as runtime_info

_T = TypeVar("_T")
_SEARCH_BUSY = threading.Lock()
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viajante-mcp")
_SEARCH_BUSY_MESSAGE = "a viajante search is already running in this process"

_HELP = """\
viajante-mcp is the stdio MCP server for local flight and hotel search.

Install:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp]' viajante-mcp
Checkout: uv sync --extra mcp && viajante-mcp
Browser:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp,browser]' \\
            playwright install chromium
          (only for --fetch detail and Booking.com; extras must match the MCP env)

Tools: search_flights, search_dates, search_flex, search_explore,
search_hotels, search_hotel_rooms, get_flight_details, get_hotel_details,
search_trip, search_self_transfer, lookup_airports, search_hidden_city,
compare_awards, lookup_transfers, validate_itinerary, plan_stay_blocks,
split_stay_costs, verify_answer, get_runtime_info.
No auth. One search at a time in this process. A second search while one is
running raises "a viajante search is already running in this process" immediately.
That busy error is not MCP timeout -32001; do not treat timeouts as lock-busy
or retry them 8×60s. lookup_airports, compare_awards, lookup_transfers,
validate_itinerary, plan_stay_blocks, split_stay_costs, verify_answer, and get_runtime_info may run
during a search.

Results are raw owned evidence, not a recommendation. You choose: read the
payload, weigh price against duration, stops, clocks, and rating, and say why.
Before replying, pass the draft to verify_answer; it flags amounts,
currencies, codes, dates, and links no search in this process returned.
verify_answer checks provenance, not link reachability, availability or room fit.
Check get_runtime_info before searching; an npm MCP and a separate installed
uv tool may execute different package versions. Do not assume uvx updates either.

If an error has rate_limited true, tell the user to wait until the UTC time
named in its message.
Do not retry, switch fetch mode, or fan out other searches; they are paused
locally and send nothing. An identical successful search within 5 minutes
comes back cached (cached: true) without a new request.

search_dates is the cheapest week. search_flex is ±N around a named date.
Do not brute-force a date matrix. search_explore is dest triage from an origin.
search_dates is HTTP-calendar only and has no fetch parameter. If it returns
blocked, stop that request: a separate browser's consent or prices are not MCP
evidence. fetch=detail applies only to search_flights and needs the browser
extra plus Chromium in the MCP environment. max_stops is 0, 1, or 2; the
product cannot require 3+ stops.
search_hidden_city is Skiplagged, not Google. After a named-route
search_flights on a hub or leisure trunk, the caller may run it once
sequentially. Do not mix evidence. Skip when bags were named.
Skiplagged cards are USD; omit currency or pass USD. Do not copy a
Google/origin quote keep (GBP, JPY, …). A keep that matches no owned card is
currency_mismatch (owned quote stamped), not no_results. No FX.
compare_awards is local points math from a named offer; it does not invent seats.
lookup_transfers is a local partner table, not live award inventory.
plan_stay_blocks and split_stay_costs are local arithmetic over a per-night roster
the caller supplies; they never search, never convert money, never pick a stay.
search_self_transfer joins two one-way flight legs through a caller-named via
into separate-ticket pairings. protected is always false. The measured
connection is evidence, not an endorsement, and does not prove baggage recheck
or immigration feasibility. Unnamed connection bounds mean no bound.
validate_itinerary is local and offline. It returns pass, fail, or unknown from
owned v2 offer evidence; unknown evidence never becomes pass. It never fills
missing segment, baggage, or fare facts.

Every MCP call is synchronous: never say you are still searching or will
report back; call the tool now or name the next step. Hotel location is one
named place; ask rather than substitute a nearby town. Hotel total_price is a
total-stay quote, not per person. Only claim the requested party total when
priced_adults agrees; unknown is unverified. General property descriptions do
not prove a private room. Check finalists' room rates and actual sleeping layout.
near names a reference point; max_distance_km requires it and excludes unknown
coordinates before ranking. Distances are straight lines, not walking routes.
Read resolved_place and report an unexpected place. Do not infer a city center.
link_context and applied.url_context distinguish stay, property, location and
none. A stay link preserves dates/adults/rooms, not guaranteed price or availability.
Never present an internal /travel/clk/hi tracker as a usable property link.
For changing groups use the latest confirmed nightly roster, group identical
people with plan_stay_blocks, and report unallocated_nights from split_stay_costs.
Do not extend a departing person's last night. Quote replacements before
recommending cancellation. A dorm room may lose exclusivity if beds are removed.
Use the exact cancellation deadline and property-local time from the reservation;
do not assume altered bookings retain prices, rooms or policy. Separate arithmetic
estimates from fresh quotes. Flight timing needs a verified transfer and airport
arrival margin; a latest check-out time alone does not prove a flight is reachable.
Keep warnings in the final response. Browser access denial is not a broken URL
or provider throttling: name the actual limitation and use available permitted
read-only evidence; never ask for permission the user already granted.

Currency is currency or inferred from a named origin's owned country.
If unknown, ask. Hotels require currency (no origin airport). Viajante
does not convert. The calling agent may convert for the user. If country,
destination, or currency is not proven (a city with several airports,
Europe, unnamed origin, two possible currencies), do not pick: ask or
error. Unknown cannot prove include. Do not invent IATA, gl, or ISO 4217
from vibe. Optional country is Google gl (origin market); omit when unset;
do not pass a destination ISO. Unnamed baggage_buffer is 0. Prefer bags /
carry_on on the shopping request so Google prices the bag. Do not invent a
bag fee. Fetch locale is English. User prompts may be any language.
Compute ISO dates from today; do not send a past start.
"""


async def run_mcp_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Run a search on the one-worker pool. Fail immediately if a search is in flight."""
    if not _SEARCH_BUSY.acquire(blocking=False):
        raise ValueError(_SEARCH_BUSY_MESSAGE)
    loop = asyncio.get_running_loop()

    def run() -> _T:
        try:
            return fn(*args, **kwargs)
        finally:
            _SEARCH_BUSY.release()

    try:
        future = loop.run_in_executor(_SEARCH_EXECUTOR, run)
    except BaseException:
        _SEARCH_BUSY.release()
        raise
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise


async def run_lookup_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Airport lookup stays off the search worker so it can run during a search."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("viajante", instructions=_HELP)

    @server.tool()
    def get_runtime_info() -> dict:
        """Offline executing package, Python and hotel schema versions.

        Check before searches; an npm MCP does not upgrade a separate uv tool
        installation. May run during a search. No network or personal paths.
        """
        return runtime_info()

    @server.tool()
    async def search_flights(
        routes: list[str],
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
        selection: Literal["top", "pareto"] = "top",
        fetch: str = "auto",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        currency: str | None = None,
        country: str | None = None,
        nearby: bool = False,
        proxy: str | None = None,
    ) -> dict:
        """Search Google Flights for named routes and dates.

        Use search_dates for the cheapest week and search_flex for ±N days.
        Each routes entry is ORIGIN-DEST:YYYY-MM-DD. fetch=detail requires the
        browser extra and Chromium in the MCP environment.
        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Unproven country, dest, or currency (city with
        several airports, Europe, unnamed origin, two currencies) must not be
        guessed. Optional country is Google gl (origin market); omit when
        unset. Unnamed baggage_buffer is 0. Prefer bags / carry_on on the
        shopping request. Do not invent a bag fee. max_stops is 0, 1, or 2.
        After a named hub or leisure trunk returns, the caller may run
        search_hidden_city once with the same route and date. Sequential; do
        not mix payloads. Skip if bags were named. Omit hidden-city currency
        (Skiplagged cards are USD); do not copy this Google quote currency.
        """
        return dict(await run_mcp_tool(search_flights_tool, **locals()))

    @server.tool()
    async def get_flight_details(selection_id: str, refresh: bool = False) -> dict:
        """Read a process-local finalist snapshot. Refresh re-shops the exact query.

        Matching requires complete segment identity; tokens and prices may change.
        Original and new quotes stay separate. Unknown/evicted ids send nothing.
        """
        return dict(await run_mcp_tool(get_flight_details_tool, **locals()))

    @server.tool()
    async def get_hotel_details(selection_id: str, room_rates: bool = False) -> dict:
        """Read a finalist snapshot; optional Skiplagged room quotes are separate USD evidence.

        Exact name and an unambiguous city are required for external finalists.
        Room conditions never attach to the original Google/Booking quote.
        """
        return dict(await run_mcp_tool(get_hotel_details_tool, **locals()))

    @server.tool()
    async def search_dates(
        route: str,
        start: str,
        end: str,
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        trip: str = "one-way",
        nights: int | None = None,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        baggage_buffer: int | None = None,
        sort: str | None = None,
        proxy: str | None = None,
    ) -> dict:
        """Cheapest-per-day calendar for a named route (up to 31 days).

        Use this for the cheapest week. Use search_flex for ±N around one date.
        route is ORIGIN-DEST and start/end are ISO dates. This HTTP calendar
        has no fetch mode; if blocked, do not use a separate browser as MCP
        recovery or evidence. Stop that calendar; do not follow with flex or
        search_flights.
        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Optional country is Google gl (origin market);
        omit when unset. Unnamed baggage_buffer is 0.
        """
        return dict(await run_mcp_tool(search_dates_tool, **locals()))

    @server.tool()
    async def search_flex(
        route: str,
        around: str,
        flex: int,
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        trip: str = "one-way",
        nights: int | None = None,
        top: int = DEFAULT_TOP,
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        proxy: str | None = None,
    ) -> dict:
        """Flex window (±N), then one shopping search on the cheapest day.

        Use search_dates for a cheapest-week calendar. Do not brute-force a date matrix.
        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Unnamed baggage_buffer is 0. Optional country is
        Google gl (origin market); omit when unset. A flex calendar miss
        (error markup_drift, empty days) is not no_results: do not invent a
        cheapest week; a named-date search_flights is allowed.
        """
        return dict(await run_mcp_tool(search_flex_tool, **locals()))

    @server.tool()
    async def search_explore(
        origin: str,
        start: str | None = None,
        days: int = 7,
        top: int = DEFAULT_EXPLORE_TOP,
        month: str | None = None,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        max_stops: int = 1,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        exclude_regions: str | None = None,
        bags: int | None = None,
        carry_on: int | None = None,
        price_cap: int | None = None,
        nearby: bool = False,
        depart_window: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        currency: str | None = None,
        country: str | None = None,
        sort: str = "price",
        baggage_buffer: int | None = None,
        proxy: str | None = None,
    ) -> dict:
        """Destinations from one origin, then a priced shortlist.

        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Unnamed baggage_buffer is 0. Optional country is
        Google gl (origin market); omit when unset.
        """
        return dict(await run_mcp_tool(search_explore_tool, **locals()))

    @server.tool()
    async def search_hotels(
        location: str | None = None,
        check_in: str | None = None,
        check_out: str | None = None,
        adults: int = 2,
        rooms: int = 1,
        top: int = DEFAULT_TOP,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
        currency: str | None = None,
        stays: list[dict] | None = None,
        near: dict[str, float] | None = None,
        max_distance_km: float | None = None,
    ) -> dict:
        """Hotel search. Currency is required (no origin airport) except source
        skiplagged, whose quotes are USD (omit currency or pass USD).

        Quotes are in the requested ISO 4217 currency as the provider returned
        them. Viajante does not convert. The calling agent may convert for the
        user. Do not invent ISO 4217 from vibe. Google offers carry owned
        latitude, longitude, and review_count: weigh location and how many
        reviews back a rating yourself; the list is price order, not advice.

        location is one named place. Ask when it is a typo, a region, or has
        several candidate towns; never substitute a nearby town. total_price is
        a total-stay quote, not per person. Matching priced_adults proves the
        quoted adult party; unknown occupancy needs verification. The request
        carries adults and rooms only, so do not state a room
        split. Vacation rentals (entire_home=true) carry sleeps, bedrooms and
        beds; hotels do not. priced_adults is the party Google priced. place_types
        and class_label say what a property is (a hotel search returns hostels).
        resolved_place and place_bounds say where Google searched; neighbors
        outside place_bounds are not the named place. entire_home=true is how a
        house or villa is requested.

        stays batches several stays in one call instead of location/check_in/
        check_out: up to 8 objects with location, check_in, check_out and
        optional adults and rooms (defaults come from the top-level values).
        Use it for a headcount that changes by night: one stay per block of
        identical people, using plan_stay_blocks after each roster change. Equal
        headcounts alone do not identify the same block. Results come back as
        one query per stay, and
        property_matrix lists each property with its total per stay (null where it was
        not among that stay's returned offers, which is not proof it is unavailable;
        raise top to see more). Rows are sorted by name, never by price.

        near is a point you name, {lat, lng}; each offer then carries distance_km, its
        straight-line distance to it. No point is assumed. max_distance_km is
        optional, requires near, and excludes distant or unlocated offers before
        top. A location query or property title does not prove centrality. Generic
        hostel descriptions do not prove a private room for the quoted price.
        lodging_evidence_conflict marks explicit room/entire-home contradictions;
        lodging_kind and property_type_evidence then stay unknown.
        Unknown evidence is a candidate, not proof of compliance. link_context
        and applied.url_context distinguish stay, property, location and none;
        only stay reproduces dates and occupancy, never availability.
        """
        return dict(await run_mcp_tool(search_hotels_tool, **locals()))

    @server.tool()
    async def search_hotel_rooms(
        check_in: str,
        check_out: str,
        hotel_id: int | None = None,
        hotel_name: str | None = None,
        city: str | None = None,
        adults: int = 2,
        rooms: int = 1,
    ) -> dict:
        """Room rates for one Skiplagged hotel, for 1-3 finalists.

        Name the hotel by hotel_id (the provider_id of a search_hotels offer with
        source skiplagged) or by hotel_name plus city. A name must match exactly
        (case, accents and punctuation ignored); no match or several matches come
        back as no_results listing what Skiplagged returned, never a guess. Use the
        name form for a Google finalist: a Google price for a hostel can be a bed in
        a dormitory, and these rates say what the room is.
        Each rate carries the provider's occupancy_limit, refundable,
        free_cancellation and taxes_and_fees as listed. Quotes are USD and are not
        converted. total_price is the whole stay for the party and rooms searched.
        occupancy_limit is the provider's number for that room type; it is not proof
        that your party fits across several rooms. Rates come in provider order, not
        ranked. Skiplagged only: do not mix these rows with Google or Booking prices.
        """
        return dict(await run_mcp_tool(search_hotel_rooms_tool, **locals()))

    @server.tool()
    async def search_trip(
        routes: list[str],
        location: str,
        check_in: str | None = None,
        check_out: str | None = None,
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        rooms: int = 1,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
        fetch: str = "auto",
        baggage_buffer: int | None = None,
        sort: str = "ranked",
        bags: int | None = None,
        carry_on: int | None = None,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        via: str | None = None,
        exclude_via: str | None = None,
        no_overnight: str | None = None,
        require_overnight: str | None = None,
        exclude_airports: str | None = None,
        include_airports: str | None = None,
        arrive_before: str | None = None,
        depart_after: str | None = None,
        price_cap: int | None = None,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        currency: str | None = None,
        country: str | None = None,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
        nearby: bool = False,
    ) -> dict:
        """Flights then hotel. Currency follows the flight origin or an explicit code.

        If unknown, ask. Viajante does not convert. The calling agent may convert
        for the user. Unnamed baggage_buffer is 0. Prefer bags / carry_on on the
        shopping request. The same currency is passed to hotels. Optional
        country is Google gl (origin market); omit when unset.
        """
        return dict(await run_mcp_tool(search_trip_tool, **locals()))

    @server.tool()
    async def search_self_transfer(
        origin: str,
        via: str,
        destination: str,
        departure: str,
        second_date: str | None = None,
        min_connection_hours: float | None = None,
        max_connection_hours: float | None = None,
        top: int = DEFAULT_TOP,
        max_stops: int = 1,
        adults: int = 1,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        cabin: str = "economy",
        bags: int | None = None,
        carry_on: int | None = None,
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        currency: str | None = None,
        country: str | None = None,
        proxy: str | None = None,
    ) -> dict:
        """Two one-way flight legs, origin-via and via-destination, on separate tickets.

        Use only when the caller names the via airport. Pairings are unprotected:
        protected is always false. connection_minutes is measured in UTC from
        provider clocks; null means unknown and status is unknown, never ok.
        Connection bounds are caller-named; unnamed means no bound. A margin is
        evidence, not an endorsed minimum, and does not prove baggage recheck,
        immigration or transit feasibility. second_date defaults to departure.
        total_price appears only when both fares share one owned currency.
        A failed leg is kept as error evidence and yields no pairings.
        """
        return dict(await run_mcp_tool(search_self_transfer_tool, **locals()))

    @server.tool()
    async def lookup_airports(query: str, limit: int = 20) -> list:
        return await run_lookup_tool(lookup_airports_tool, **locals())

    @server.tool()
    async def search_hidden_city(
        route: str,
        departure: str,
        return_date: str | None = None,
        adults: int = 1,
        top: int = DEFAULT_TOP,
        currency: str | None = None,
    ) -> dict:
        """Search Skiplagged for a named route. Opt-in. Does not mix Google Flights.

        route is ORIGIN-DEST. After search_flights on a named common route
        (hub or leisure trunk, or Google looking like a through-fare), call
        this once with the same route and date. Sequential. Skip explore,
        dates, flex, multi-city, unproven dests, and named bags. Hidden-city
        tickets can violate airline contracts. Lead with hidden_city true
        rows; confirm the fare on booking_url (do not scrape). Viajante does
        not book. Currency is an optional keep of owned card ISO 4217.
        Skiplagged cards are USD; omit currency or pass USD. Do not copy a
        Google/origin quote keep (GBP, JPY, …). A keep that matches no owned card is
        currency_mismatch (owned quote stamped), not no_results. Does not
        infer from origin or convert.
        """
        return dict(await run_mcp_tool(search_hidden_city_tool, **locals()))

    @server.tool()
    async def compare_awards(
        offer: dict,
        cash_price: float | None = None,
        currency: str | None = None,
        balances: list | None = None,
    ) -> dict:
        """Compare a named award offer to cash locally. Does not invent seats.

        offer must include origin, destination, departure_date, program, points,
        and evidence. confirmed evidence needs a named source. Unnamed cash_price
        omits cpp_cents. Do not treat estimated or user_supplied as live inventory.
        """
        return dict(await run_lookup_tool(compare_awards_tool, **locals()))

    @server.tool()
    async def lookup_transfers(
        program: str,
        points: int,
        balances: list | None = None,
    ) -> dict:
        """Local card-to-program transfer table. Not live award availability."""
        return dict(await run_lookup_tool(lookup_transfers_tool, **locals()))

    @server.tool()
    async def validate_itinerary(
        legs: list[dict],
        constraints: dict,
        currency: str | None = None,
    ) -> dict:
        """Validate selected v2 flight offers locally without fetching.

        Each leg must preserve its exact query and one selected offer. The
        result is tri-state: unknown evidence never becomes pass.
        """
        return dict(await run_lookup_tool(validate_itinerary_tool, **locals()))

    @server.tool()
    async def plan_stay_blocks(roster: dict[str, list[str]]) -> dict:
        """Local: group consecutive nights with the same people into blocks.

        roster maps each night (YYYY-MM-DD, the night that starts that day) to the
        names sleeping. Nights must be consecutive. Each block gives check_in,
        check_out, nights, headcount and people, so one search_hotels stay per block
        can use headcount as adults. Never decides where anyone sleeps.
        """
        return dict(await run_lookup_tool(plan_stay_blocks_tool, **locals()))

    @server.tool()
    async def split_stay_costs(
        stays: list[dict],
        roster: dict[str, list[str]],
        currency: str,
        fee_per_person_night: float | None = None,
    ) -> dict:
        """Local: split each stay's total among the people who sleep there.

        stays are {name, check_in, check_out, total}; roster is as for
        plan_stay_blocks. A stay's rate is its total over the person-nights the
        roster puts inside it, so someone who never sleeps there pays nothing toward
        it. currency is named by the caller and never converted. fee_per_person_night
        is a per-night charge the caller names (a city tax) added per person. Cents
        are allocated so each stay sums exactly. Nights no stay covers come back as
        unallocated_nights. Arithmetic only: it does not price or recommend a stay.
        """
        return dict(await run_lookup_tool(split_stay_costs_tool, **locals()))

    @server.tool()
    async def verify_answer(answer: str) -> dict:
        """Check a draft reply against this process's recent search payloads.

        Call before sending a reply that quotes fares, stays, dates, airport
        codes, or links. Lists each amount, currency, IATA code, ISO date, and
        URL in the draft that no recorded search returned. Drop or re-search
        every unowned row; do not quote it as found. Sums you computed are
        unowned unless a payload carries them (e.g. trip_total). Local; may run
        during a search.
        """
        return dict(await run_lookup_tool(verify_answer_tool, **locals()))

    return server


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"-h", "--help"}:
        # Help must work without the mcp extra.
        print(_HELP.strip())
        return
    try:
        server = build_server()
    except ImportError as exc:
        raise SystemExit(
            "viajante-mcp requires the mcp extra. Install with: uv sync --extra mcp"
        ) from exc
    server.run()


if __name__ == "__main__":
    main()
