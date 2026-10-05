"""Real stdio protocol smoke with offline fixture sources, never live providers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest

BOOTSTRAP = """
from datetime import date, datetime, timezone
from types import SimpleNamespace
import viajante.flights as flights
import viajante.hotels as hotels
import viajante.details as details
from viajante.models import RawSegment, RawJourneyLeg, RawHotelCard, HotelPage, HotelRoomsReport
from viajante.google_flights import RawFlightCard

class FlightSource:
    calls = 0
    def __init__(self, **kwargs):
        self.config = SimpleNamespace(html_lang="en", currency=kwargs["currency"], country=None)
    def fetch(self, query):
        FlightSource.calls += 1
        price = 100 if FlightSource.calls == 1 else 130
        segment = RawSegment("JFK", "LHR", "22:00", "10:00", "British Airways", "BA100",
            date(2099, 7, 1), "BA", date(2099, 7, 2), "America/New_York", "Europe/London")
        journey = RawJourneyLeg("22:00", "10:00", "7 hr", "Nonstop", (segment,))
        return [RawFlightCard("British Airways", "22:00", "10:00", "7 hr", "Nonstop",
            f"${price}", booking_token=f"token-{price}", legs=(journey,),
            checked_bags=1, carry_on=1)]
    def close(self):
        pass

class HotelSource:
    def __init__(self, **kwargs):
        self.config = SimpleNamespace(html_lang="en", currency=kwargs["currency"])
    def fetch(self, query, applied, limit):
        return HotelPage((RawHotelCard("Czech Inn", None, "EUR 150", "4.5", "Free cancellation",
            None, priced_adults=2, unit_details="Private room; Free cancellation"),),
            resolved_place="Prague")
    def close(self):
        pass

def rooms(hotel_id, check_in, check_out, **kwargs):
    return HotelRoomsReport(datetime.now(timezone.utc), "25584", check_in, check_out,
        kwargs["adults"], kwargs["rooms"], currency="USD", name="Czech Inn")

flights.GoogleFlightsHttpSource = FlightSource
flights.playwright_available = lambda: False
hotels.GoogleHotelsSource = HotelSource
details.search_hotel_rooms = rooms
from viajante.mcp_server import build_server
build_server().run(transport="stdio")
"""


@unittest.skipUnless(importlib.util.find_spec("mcp"), "mcp extra is not installed")
class StdioFinalistTests(unittest.TestCase):
    def test_real_stdio_tools_snapshot_refresh_room_quotes_and_cache_replay(self):
        # Import lazily: SDK-free handler tests must be able to import the tree first.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        def payload(result):
            self.assertFalse(result.isError)
            return json.loads(next(item.text for item in result.content if item.type == "text"))

        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=["-c", BOOTSTRAP])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    by_name = {tool.name: tool for tool in listing.tools}
                    self.assertEqual(len(by_name), 19)
                    self.assertEqual(
                        by_name["search_flights"].inputSchema["properties"]["selection"]["enum"],
                        ["top", "pareto"],
                    )
                    request = {
                        "routes": ["JFK-LHR:2099-07-01"],
                        "fetch": "sweep",
                        "selection": "pareto",
                        "top": 1,
                    }
                    original = payload(await session.call_tool("search_flights", request))
                    offer = original["queries"][0]["offers"][0]
                    ref = offer["selection_id"]
                    snapshot = payload(
                        await session.call_tool("get_flight_details", {"selection_id": ref})
                    )
                    self.assertEqual(snapshot["match_status"], "snapshot")
                    fresh = payload(
                        await session.call_tool(
                            "get_flight_details", {"selection_id": ref, "refresh": True}
                        )
                    )
                    self.assertEqual(fresh["match_status"], "matched")
                    self.assertEqual(fresh["price_change"], 30)
                    replay = payload(await session.call_tool("search_flights", request))
                    self.assertTrue(replay["cached"])
                    self.assertEqual(replay["searched_at"], original["searched_at"])
                    self.assertEqual(replay["queries"][0]["offers"][0], offer)
                    stays = payload(
                        await session.call_tool(
                            "search_hotels",
                            {
                                "location": "Prague",
                                "check_in": "2099-07-01",
                                "check_out": "2099-07-04",
                                "currency": "EUR",
                            },
                        )
                    )
                    hotel_ref = stays["queries"][0]["offers"][0]["selection_id"]
                    stay = payload(
                        await session.call_tool("get_hotel_details", {"selection_id": hotel_ref})
                    )
                    self.assertIsNone(stay["room_quotes"])
                    rate = payload(
                        await session.call_tool(
                            "get_hotel_details", {"selection_id": hotel_ref, "room_rates": True}
                        )
                    )
                    self.assertEqual(rate["original_quote"]["currency"], "EUR")
                    self.assertEqual(rate["room_quotes"]["currency"], "USD")
                    missing = await session.call_tool(
                        "get_flight_details", {"selection_id": "sel_missing", "refresh": True}
                    )
                    self.assertTrue(missing.isError)
                    self.assertIn("unknown or evicted", missing.content[0].text)

        asyncio.run(exercise())
