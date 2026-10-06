"""MCP entry (stdio, or opt-in local Streamable HTTP). Importable only with the mcp extra."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, Literal, NoReturn, Optional, Sequence, TypeVar

from viajante.envelope import COMPLETENESS, OBSERVED_BASES, STATUSES, stamp_local
from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.flights import DEFAULT_TOP
from viajante.mcp_errors import structured_error, unknown_arguments_body, validation_body
from viajante.mcp_guide import GUIDE, INSTRUCTIONS
from viajante.mcp_handlers import (
    compare_awards_tool,
    lookup_airports_tool,
    lookup_transfers_tool,
    plan_stay_blocks_tool,
    recheck_offer_tool,
    search_dates_tool,
    search_explore_tool,
    search_flex_tool,
    search_flights_tool,
    search_hidden_city_tool,
    search_hotel_rooms_tool,
    search_hotels_tool,
    search_trip_tool,
    split_stay_costs_tool,
    validate_itinerary_tool,
    verify_answer_tool,
)
from viajante.models import EmptyReason
from viajante.runtime import get_runtime_info as runtime_info

_T = TypeVar("_T")
_SEARCH_BUSY = threading.Lock()
_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="viajante-mcp")
_SEARCH_BUSY_MESSAGE = "a viajante search is already running in this process"

_HELP = INSTRUCTIONS
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000
_TOOL_ERROR_PREFIX = "Error executing tool {name}: "
_USAGE = f"""\
viajante-mcp is the MCP server for local flight and hotel search (stdio by default).

usage: viajante-mcp [--transport {{stdio,streamable-http}}] [--host HOST] [--port PORT]

  --transport   stdio (default) or streamable-http (local, opt-in, no authentication)
  --host        streamable-http bind address (default {_DEFAULT_HOST})
  --port        streamable-http port (default {_DEFAULT_PORT}); the endpoint is /mcp

Install:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp]' viajante-mcp
Checkout: uv sync --extra mcp && viajante-mcp

Tools: search_flights, search_dates, search_flex, search_explore,
search_hotels, search_hotel_rooms, search_trip, lookup_airports, search_hidden_city,
compare_awards, lookup_transfers, validate_itinerary, recheck_offer, plan_stay_blocks,
split_stay_costs, verify_answer, get_runtime_info, get_guide.

Server instructions (the full guide is the viajante://guide resource):

"""


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _reraise(exc: ValueError, params: dict[str, object]) -> NoReturn:
    converted = structured_error(exc, params)
    if converted is exc:
        raise exc
    raise converted from exc


async def run_mcp_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Run a search on the one-worker pool. Fail immediately if a search is in flight."""
    if not _SEARCH_BUSY.acquire(blocking=False):
        _reraise(ValueError(_SEARCH_BUSY_MESSAGE), kwargs)
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
    except ValueError as exc:
        _reraise(exc, kwargs)


async def run_lookup_tool(fn: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Airport lookup stays off the search worker so it can run during a search."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))
    except ValueError as exc:
        _reraise(exc, kwargs)


def _loopback_security(host: str):
    """Reject a foreign Host or Origin. The SDK only does this by itself from 1.23."""
    from mcp.server.transport_security import TransportSecuritySettings

    names = ["127.0.0.1", "localhost", "[::1]"]
    own = f"[{host.strip('[]')}]" if ":" in host else host
    if own not in names:
        names.append(own)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{name}:*" for name in names],
        allowed_origins=[f"http://{name}:*" for name in names],
    )


def build_server(*, host: Optional[str] = None, port: Optional[int] = None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from pydantic import BaseModel, ConfigDict

    class ToolEnvelope(BaseModel):
        """Top-level fields on every tool result except lookup_airports (a bare list).

        Read these first. Every other key of the tool's payload rides alongside them.
        """

        model_config = ConfigDict(extra="allow")

        status: Literal[STATUSES]
        completeness: Literal[COMPLETENESS]
        empty_reason: Optional[EmptyReason]
        empty_note: Optional[str]
        error_code: Optional[str]
        retry_after: Optional[str]
        retry_after_seconds: Optional[int]
        observed_at: Optional[str]
        observed_at_basis: Optional[Literal[OBSERVED_BASES]]

    options: dict[str, object] = {}
    if host is not None:
        options = {"host": host, "port": port}
        if _is_loopback(host):
            options["transport_security"] = _loopback_security(host)

    class ViajanteServer(FastMCP):
        async def call_tool(self, name, arguments):
            # The SDK rejects missing or mistyped arguments before any handler runs, with
            # pydantic text. Give those the same JSON body as a handler's ValueError.
            # An argument the tool does not declare is a misspelled filter, never ignored.
            declared = next(
                (
                    t.inputSchema.get("properties", {})
                    for t in await self.list_tools()
                    if t.name == name
                ),
                None,
            )
            unknown = sorted(set(arguments or {}) - set(declared)) if declared is not None else []
            if unknown:
                raise ValueError(
                    _TOOL_ERROR_PREFIX.format(name=name) + unknown_arguments_body(unknown)
                )
            try:
                return await super().call_tool(name, arguments)
            except Exception as exc:
                cause = exc.__cause__
                if not (isinstance(cause, ValueError) and callable(getattr(cause, "errors", None))):
                    raise
                body = validation_body(
                    cause.errors(include_url=False, include_context=False, include_input=False)
                )
                raise type(exc)(_TOOL_ERROR_PREFIX.format(name=name) + body) from cause

    server = ViajanteServer("viajante", instructions=_HELP, **options)

    def tool(title: str, *, network: bool, envelope: bool = True):
        # Every tool only reads; openWorldHint is True only when the tool asks a provider.
        register = server.tool(
            title=title,
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=network,
            ),
        )

        def decorate(fn):
            if envelope:
                # `from __future__ import annotations` makes the return a string that cannot
                # see the class above; FastMCP only builds an outputSchema from a real annotation.
                fn.__annotations__["return"] = ToolEnvelope
            return register(fn)

        return decorate

    @server.resource(
        "viajante://guide",
        name="guide",
        title="viajante operational guide",
        description="Long operational rules for the viajante tools: evidence, currency, "
        "rate limits, hotels and stays.",
        mime_type="text/markdown",
    )
    def guide_resource() -> str:
        return GUIDE

    @tool("Runtime info", network=False)
    def get_runtime_info() -> dict:
        """Offline executing package, Python and hotel schema versions.

        Check before searches; an npm MCP does not upgrade a separate uv tool
        installation. May run during a search. No network or personal paths.
        """
        return stamp_local(dict(runtime_info()))

    @tool("Search flights", network=True)
    async def search_flights(
        routes: list[str],
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        cabin: str = "economy",
        top: int = DEFAULT_TOP,
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
        A successful query may carry recommendation (none when the provider
        returned nothing): a pick that meets the named requirements
        (relaxed_requirements names any it relaxed), a varied shortlist, and
        highlights/tradeoffs from returned fields only.
        It is evidence for your judgment; offers are unchanged. Check
        relaxed_requirements before presenting a pick as a match. A query
        with empty_reason filtered_out (envelope status no_results when every
        query is filtered_out) that still has a recommendation means the pick
        is a relaxed one, not an exact match: the named filters removed every
        offer.
        """
        return dict(await run_mcp_tool(search_flights_tool, **locals()))

    @tool("Cheapest-dates calendar", network=True)
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

    @tool("Flexible-date flight search", network=True)
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

    @tool("Explore destinations", network=True)
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

    @tool("Search hotels", network=True)
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

    @tool("Hotel room rates", network=True)
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

    @tool("Search flights and hotel", network=True)
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
        country is Google gl (origin market); omit when unset. The flights
        queries carry the same recommendation block as search_flights.
        """
        return dict(await run_mcp_tool(search_trip_tool, **locals()))

    @tool("Look up airports", network=False, envelope=False)
    async def lookup_airports(query: str, limit: int = 20) -> list:
        return await run_lookup_tool(lookup_airports_tool, **locals())

    @tool("Search hidden-city fares", network=True)
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

    @tool("Compare award to cash", network=False)
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

    @tool("Look up point transfers", network=False)
    async def lookup_transfers(
        program: str,
        points: int,
        balances: list | None = None,
    ) -> dict:
        """Local card-to-program transfer table. Not live award availability."""
        return dict(await run_lookup_tool(lookup_transfers_tool, **locals()))

    @tool("Validate itinerary", network=False)
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

    @tool("Re-check offer", network=True)
    async def recheck_offer(
        offer: dict,
        query: dict | None = None,
        currency: str | None = None,
        country: str | None = None,
        fetch: str | None = None,
        proxy: str | None = None,
        allow_loose_match: bool = False,
        allow_substitute: bool = False,
    ) -> dict:
        """Re-check an earlier flight offer with one fresh Google Flights search.

        offer is an offer from a prior search_flights result (or a {query, offer}
        row), or enough of one: price plus legs[].segments[] with flight_number,
        origin, destination and departure clock for every segment. query
        defaults to the offer's evidence query and is replayed (cabin, stops,
        bags, airline and alliance filters); a price_cap in it is not sent but
        reported in filter_violations when the fresh offer breaks it. A
        hand-built offer also needs query adults, cabin and max_stops, and
        currency. Matches by flight numbers plus departure times. Returns
        exactly one outcome: same_price, price_changed, not_found,
        multiple_matches (more than one identical fresh offer: candidates are
        listed, no price verdict), incomplete_identity (the offer lacks a full
        segment identity: no search was sent), check_failed, or substituted
        (only with allow_substitute). allow_loose_match accepts carrier plus
        departure times when flight numbers are absent (loose_match true).
        allow_substitute reports a close same-carrier alternative as
        substituted; by default it is only listed as closest_candidate on a
        not_found. Positive outcomes always rest on a fresh provider match.
        check_failed (check_completed false, with reason and error: blocked,
        rate_limited, incomplete_offers, ...) means the check could not be
        completed, not that the offer is gone; do not retry a rate limit.
        currency must be the offer's own: a different one is refused (no
        conversion). Caller-typed values are not recorded as owned evidence.
        Read the envelope first: only empty_reason provider_empty means Google
        returned nothing; check_failed is not_loaded, never "gone".
        Not a booking guarantee: confirm the price on the provider's own page.
        """
        return dict(await run_mcp_tool(recheck_offer_tool, **locals()))

    @tool("Plan stay blocks", network=False)
    async def plan_stay_blocks(roster: dict[str, list[str]]) -> dict:
        """Local: group consecutive nights with the same people into blocks.

        roster maps each night (YYYY-MM-DD, the night that starts that day) to the
        names sleeping. Nights must be consecutive. Each block gives check_in,
        check_out, nights, headcount and people, so one search_hotels stay per block
        can use headcount as adults. Never decides where anyone sleeps.
        """
        return dict(await run_lookup_tool(plan_stay_blocks_tool, **locals()))

    @tool("Split stay costs", network=False)
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

    @tool("Verify draft answer", network=False)
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

    @tool("Operational guide", network=False)
    def get_guide() -> dict:
        """The long operational guide (markdown); the same text as the viajante://guide resource.

        For clients that do not read MCP resources. Returns {"guide": markdown}. Local;
        may run during a search.
        """
        return stamp_local({"guide": GUIDE})

    return server


def _parse_args(args: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="viajante-mcp", add_help=False)
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parsed = parser.parse_args(args)
    if parsed.transport == "stdio" and (parsed.host is not None or parsed.port is not None):
        parser.error("--host and --port apply only to --transport streamable-http")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-h", "--help"} for arg in args):
        # Help must work without the mcp extra.
        print(_USAGE + _HELP.strip())
        return
    parsed = _parse_args(args)
    http = parsed.transport == "streamable-http"
    host = parsed.host or _DEFAULT_HOST
    port = parsed.port or _DEFAULT_PORT
    if http and not _is_loopback(host):
        print(
            f"warning: binding to {host}, not loopback. viajante-mcp has no authentication; "
            "every client that can reach this port searches from this machine's IP, and the "
            "machine-wide provider cooldown applies to all of them.",
            file=sys.stderr,
        )
    try:
        server = build_server(host=host, port=port) if http else build_server()
    except ImportError as exc:
        raise SystemExit(
            "viajante-mcp requires the mcp extra. Install with: uv sync --extra mcp"
        ) from exc
    server.run(transport=parsed.transport)


if __name__ == "__main__":
    main()
