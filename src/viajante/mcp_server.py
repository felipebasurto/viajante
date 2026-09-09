"""Stdio MCP entry. Importable only when the mcp extra is installed."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.flights import DEFAULT_TOP
from viajante.mcp_handlers import (
    lookup_airports_tool,
    search_dates_tool,
    search_explore_tool,
    search_flex_tool,
    search_flights_tool,
    search_hotels_tool,
    search_trip_tool,
)

_HELP = """\
viajante-mcp is the stdio MCP server for local flight and hotel search.

Install:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp]' viajante-mcp
Checkout: uv sync --extra mcp && viajante-mcp
Browser:  pip install 'viajante[browser]' && playwright install chromium
          (only for --fetch detail and Booking.com)

Tools: search_flights, search_dates, search_flex, search_explore,
search_hotels, search_trip, lookup_airports.
No auth. One search at a time in this process.

search_dates is the cheapest week. search_flex is ±N around a named date.
Do not brute-force a date matrix. search_explore is dest triage from an origin.

Currency is currency or inferred from a named origin's owned country.
If unknown, ask. Hotels require currency (no origin airport). Viajante
does not convert. The calling agent may convert for the user. If country,
destination, or currency is not proven (a city with several airports,
Europe, unnamed origin, two possible currencies), do not pick: ask or
error. Unknown cannot prove include. Do not invent IATA, gl, or ISO 4217
from vibe. Unnamed baggage_buffer is 0. Prefer bags / carry_on on the
shopping request so Google prices the bag. Do not invent a bag fee.
Fetch locale is English. User prompts may be any language.
"""


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("viajante", instructions=_HELP)

    @server.tool()
    def search_flights(
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
        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Unproven country, dest, or currency (city with
        several airports, Europe, unnamed origin, two currencies) must not be
        guessed. Unnamed baggage_buffer is 0. Prefer bags / carry_on on the
        shopping request. Do not invent a bag fee.
        """
        return dict(
            search_flights_tool(
                routes,
                trip=trip,
                max_stops=max_stops,
                adults=adults,
                cabin=cabin,  # type: ignore[arg-type]
                top=top,
                fetch=fetch,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliance=alliance,
                exclude_alliance=exclude_alliance,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_duration=max_duration,
                min_layover=min_layover,
                max_layover=max_layover,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                exclude_airports=exclude_airports,
                include_airports=include_airports,
                baggage_buffer=baggage_buffer,
                sort=sort,  # type: ignore[arg-type]
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                currency=currency,
                country=country,
                nearby=nearby,
                proxy=proxy,
            )
        )

    @server.tool()
    def search_dates(
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
        Currency is currency or inferred from a named origin's owned country.
        If unknown, ask. Viajante does not convert. The calling agent may
        convert for the user. Unnamed baggage_buffer is 0.
        """
        return dict(
            search_dates_tool(
                route,
                start,
                end,
                max_stops=max_stops,
                adults=adults,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                cabin=cabin,  # type: ignore[arg-type]
                trip=trip,
                nights=nights,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliance=alliance,
                exclude_alliance=exclude_alliance,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                exclude_airports=exclude_airports,
                include_airports=include_airports,
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
                nearby=nearby,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_duration=max_duration,
                min_layover=min_layover,
                max_layover=max_layover,
                currency=currency,
                country=country,
                baggage_buffer=baggage_buffer,
                sort=sort,  # type: ignore[arg-type]
                proxy=proxy,
            )
        )

    @server.tool()
    def search_flex(
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
        convert for the user. Unnamed baggage_buffer is 0.
        """
        return dict(
            search_flex_tool(
                route,
                around,
                flex,
                max_stops=max_stops,
                adults=adults,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                cabin=cabin,  # type: ignore[arg-type]
                trip=trip,
                nights=nights,
                top=top,
                baggage_buffer=baggage_buffer,
                sort=sort,  # type: ignore[arg-type]
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliance=alliance,
                exclude_alliance=exclude_alliance,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                exclude_airports=exclude_airports,
                include_airports=include_airports,
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
                nearby=nearby,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_duration=max_duration,
                min_layover=min_layover,
                max_layover=max_layover,
                currency=currency,
                country=country,
                proxy=proxy,
            )
        )

    @server.tool()
    def search_explore(
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
        convert for the user. Unnamed baggage_buffer is 0.
        """
        return dict(
            search_explore_tool(
                origin,
                start,
                days=days,
                top=top,
                month=month,
                adults=adults,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                cabin=cabin,  # type: ignore[arg-type]
                max_stops=max_stops,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliance=alliance,
                exclude_alliance=exclude_alliance,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                exclude_airports=exclude_airports,
                include_airports=include_airports,
                exclude_regions=exclude_regions,
                bags=bags,
                carry_on=carry_on,
                price_cap=price_cap,
                nearby=nearby,
                depart_window=depart_window,
                arrive_before=arrive_before,
                depart_after=depart_after,
                max_duration=max_duration,
                min_layover=min_layover,
                max_layover=max_layover,
                currency=currency,
                country=country,
                sort=sort,  # type: ignore[arg-type]
                baggage_buffer=baggage_buffer,
                proxy=proxy,
            )
        )

    @server.tool()
    def search_hotels(
        location: str,
        check_in: str,
        check_out: str,
        adults: int = 2,
        rooms: int = 1,
        top: int = DEFAULT_TOP,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
        currency: str | None = None,
    ) -> dict:
        """Hotel search. Currency is required (no origin airport).

        Quotes are in the requested ISO 4217 currency as the provider returned
        them. Viajante does not convert. The calling agent may convert for the
        user. Do not invent ISO 4217 from vibe.
        """
        return dict(
            search_hotels_tool(
                location,
                check_in,
                check_out,
                adults=adults,
                rooms=rooms,
                top=top,
                min_rating=min_rating,
                entire_home=entire_home,
                free_cancellation=free_cancellation,
                source=source,  # type: ignore[arg-type]
                currency=currency,
            )
        )

    @server.tool()
    def search_trip(
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
        shopping request. The same currency is passed to hotels.
        """
        return dict(
            search_trip_tool(
                routes,
                location,
                check_in=check_in,
                check_out=check_out,
                trip=trip,
                max_stops=max_stops,
                adults=adults,
                rooms=rooms,
                cabin=cabin,  # type: ignore[arg-type]
                top=top,
                fetch=fetch,
                baggage_buffer=baggage_buffer,
                sort=sort,  # type: ignore[arg-type]
                bags=bags,
                carry_on=carry_on,
                airlines=airlines,
                exclude_airlines=exclude_airlines,
                alliance=alliance,
                exclude_alliance=exclude_alliance,
                via=via,
                exclude_via=exclude_via,
                no_overnight=no_overnight,
                require_overnight=require_overnight,
                exclude_airports=exclude_airports,
                include_airports=include_airports,
                arrive_before=arrive_before,
                depart_after=depart_after,
                price_cap=price_cap,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                currency=currency,
                country=country,
                min_rating=min_rating,
                entire_home=entire_home,
                free_cancellation=free_cancellation,
                source=source,  # type: ignore[arg-type]
                nearby=nearby,
            )
        )

    @server.tool()
    def lookup_airports(query: str, limit: int = 20) -> list:
        return lookup_airports_tool(query, limit=limit)

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
