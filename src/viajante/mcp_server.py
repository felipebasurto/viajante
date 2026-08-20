"""Stdio MCP entry. Importable only when the mcp extra is installed."""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.flights import DEFAULT_BAGGAGE_BUFFER_EUR
from viajante.mcp_handlers import (
    lookup_airports_tool,
    search_dates_tool,
    search_explore_tool,
    search_flights_tool,
    search_hotels_tool,
)

_HELP = """\
viajante-mcp — stdio MCP server for local flight and hotel search.

Install:  uv sync --extra mcp
Run:      viajante-mcp

Tools: search_flights, search_dates, search_explore, search_hotels, lookup_airports.
No auth. One search at a time in this process.
"""


def build_server():
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("viajante")

    @server.tool()
    def search_flights(
        routes: list[str],
        trip: str = "one-way",
        max_stops: int = 1,
        adults: int = 1,
        cabin: str = "economy",
        top: int = 8,
        fetch: str = "auto",
        airlines: str | None = None,
        exclude_airlines: str | None = None,
        alliance: str | None = None,
        exclude_alliance: str | None = None,
        depart_window: str | None = None,
        max_duration: float | None = None,
        min_layover: float | None = None,
        max_layover: float | None = None,
        baggage_buffer: int = DEFAULT_BAGGAGE_BUFFER_EUR,
        sort: str = "ranked",
        bags: int | None = None,
        carry_on: int | None = None,
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
        currency: str = "EUR",
        country: str | None = None,
    ) -> dict:
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
                max_duration=max_duration,
                min_layover=min_layover,
                max_layover=max_layover,
                baggage_buffer=baggage_buffer,
                sort=sort,  # type: ignore[arg-type]
                bags=bags,
                carry_on=carry_on,
                children=children,
                infants_in_seat=infants_in_seat,
                infants_on_lap=infants_on_lap,
                currency=currency,
                country=country,
            )
        )

    @server.tool()
    def search_dates(
        route: str,
        start: str,
        end: str,
        max_stops: int = 1,
        adults: int = 1,
        cabin: str = "economy",
        trip: str = "one-way",
        nights: int | None = None,
    ) -> dict:
        return dict(
            search_dates_tool(
                route,
                start,
                end,
                max_stops=max_stops,
                adults=adults,
                cabin=cabin,  # type: ignore[arg-type]
                trip=trip,
                nights=nights,
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
        cabin: str = "economy",
        max_stops: int = 1,
    ) -> dict:
        return dict(
            search_explore_tool(
                origin,
                start,
                days=days,
                top=top,
                month=month,
                adults=adults,
                cabin=cabin,  # type: ignore[arg-type]
                max_stops=max_stops,
            )
        )

    @server.tool()
    def search_hotels(
        location: str,
        check_in: str,
        check_out: str,
        adults: int = 2,
        rooms: int = 1,
        top: int = 8,
        min_rating: float | None = None,
        entire_home: bool = False,
        free_cancellation: bool = True,
        source: str = "google",
    ) -> dict:
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
