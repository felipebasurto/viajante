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
import viajante.mcp_handlers as handlers
from viajante.models import (
    HiddenCityOffer, HiddenCityReport, RawSegment, RawJourneyLeg, RawHotelCard, HotelPage,
    HotelRoomRate, HotelRoomsReport, SearchError, SearchErrorCode,
)
from viajante.google_flights import RawFlightCard

class FlightSource:
    def __init__(self, **kwargs):
        self.config = SimpleNamespace(html_lang="en", currency=kwargs["currency"], country=None)
    def fetch(self, query):
        segment = RawSegment("JFK", "LHR", "22:00", "10:00", "British Airways", "BA100",
            date(2099, 7, 1), "BA", date(2099, 7, 2), "America/New_York", "Europe/London")
        journey = RawJourneyLeg("22:00", "10:00", "7 hr", "Nonstop", (segment,))
        return [RawFlightCard("British Airways", "22:00", "10:00", "7 hr", "Nonstop",
            "$100", booking_token="token-100", legs=(journey,), checked_bags=1, carry_on=1)]
    def close(self):
        pass

class HotelSource:
    def __init__(self, **kwargs):
        self.config = SimpleNamespace(html_lang="en", currency=kwargs["currency"])
    def fetch(self, query, applied, limit):
        lat = lng = None
        if query.location == "Springfield":
            lat, lng = 37.20, -93.30
        return HotelPage((RawHotelCard("Czech Inn", None, "EUR 150", "4.5", "Free cancellation",
            None, priced_adults=2, unit_details="Private room; Free cancellation",
            latitude=lat, longitude=lng),), resolved_place=query.location)
    def close(self):
        pass

def rooms(hotel_id, check_in, check_out, **kwargs):
    import time
    city = kwargs.get("city") or ""
    now = datetime.now(timezone.utc)
    rate = HotelRoomRate("Triple", 545.0, 81.0, 58.0, 6, True, True, (), None)
    echoed = dict(answered_adults=kwargs["adults"], answered_rooms=kwargs["rooms"],
        answered_check_in=check_in, answered_check_out=check_out, rates=(rate,))
    common = dict(hotel_id="25584", check_in=check_in, check_out=check_out,
        adults=kwargs["adults"], rooms=kwargs["rooms"], currency="USD", name="Czech Inn")
    if city.startswith("Lisbon"):
        return HotelRoomsReport(now, **common, **{**echoed, "answered_adults": 1})
    if city.startswith("Paris"):
        shifted = {**echoed, "answered_check_in": date(2099, 8, 1),
            "answered_check_out": date(2099, 8, 4)}
        return HotelRoomsReport(now, **common, **shifted)
    if city.startswith("Oslo"):
        return HotelRoomsReport(now, **common, rates=(rate,))
    if city.startswith("Rome"):
        return HotelRoomsReport(now, **common, **{k: v for k, v in echoed.items() if k != "rates"})
    if city.startswith("Tokyo"):
        return HotelRoomsReport(now, **common, error=SearchError(
            SearchErrorCode.FETCH_FAILED, "limited", rate_limited=True,
            retry_until=time.time() + 90))
    if city.startswith("Dublin"):
        return HotelRoomsReport(now, **common, error=SearchError(SearchErrorCode.BLOCKED, "wall"))
    if city.startswith("Vienna"):
        return HotelRoomsReport(now, **common, error=SearchError(
            SearchErrorCode.FETCH_FAILED, "slow", timeout=True))
    if city.startswith("Athens"):
        return HotelRoomsReport(
            now, **common, error=SearchError(SearchErrorCode.FETCH_FAILED, "nope"))
    if city.startswith("Springfield"):
        return HotelRoomsReport(now, **common, **echoed, latitude=50.07, longitude=14.44)
    return HotelRoomsReport(now, **common, **echoed)

def hidden(origin, destination, departure_date, **kwargs):
    offer = HiddenCityOffer(
        origin, destination, departure_date, 622.0, currency="USD",
        evidence="confirmed", airline="Delta", hidden_city=True,
    )
    return HiddenCityReport(
        datetime.now(timezone.utc), origin, destination, departure_date,
        currency="USD", offers=(offer,),
    )

flights.GoogleFlightsHttpSource = FlightSource
flights.playwright_available = lambda: False
hotels.GoogleHotelsSource = HotelSource
details.search_hotel_rooms = rooms
handlers.search_hidden_city = hidden
from viajante.mcp_server import build_server
build_server().run(transport="stdio")
"""

_SEGMENT = {
    "origin": "JFK",
    "destination": "LHR",
    "departure_date": "2099-07-01",
    "arrival_date": "2099-07-02",
    "departure": "22:00",
    "arrival": "10:00",
    "departure_timezone": "America/New_York",
    "arrival_timezone": "Europe/London",
}
_QUERY = {
    "origin": "JFK",
    "destination": "LHR",
    "departure_date": "2099-07-01",
    "trip": "one-way",
}
DEADLINE_ARGS = {
    "legs": [
        {
            "status": "ok",
            "query": _QUERY,
            "offer": {
                "price": 100,
                "stops_count": 0,
                "evidence": {"evidence_id": "one", "query": dict(_QUERY), "currency": "USD"},
                "legs": [{"segments": [_SEGMENT]}],
            },
        }
    ],
    "constraints": {"arrival_deadline": "2099-07-02T09:00Z"},
}

EXPECTED_TOOLS = [
    "get_runtime_info",
    "search_flights",
    "get_hotel_details",
    "search_dates",
    "search_flex",
    "search_explore",
    "search_hotels",
    "search_hotel_rooms",
    "search_trip",
    "search_split_tickets",
    "lookup_airports",
    "search_hidden_city",
    "compare_awards",
    "lookup_transfers",
    "validate_itinerary",
    "recheck_offer",
    "plan_stay_blocks",
    "split_stay_costs",
    "verify_answer",
    "get_guide",
]


@unittest.skipUnless(importlib.util.find_spec("mcp"), "mcp extra is not installed")
class StdioFinalistTests(unittest.TestCase):
    def test_real_stdio_session_keeps_reads_and_rejects_bad_inputs(self):
        import jsonschema
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        def payload(result):
            self.assertFalse(result.isError, getattr(result, "content", result))
            return json.loads(next(item.text for item in result.content if item.type == "text"))

        async def exercise():
            parameters = StdioServerParameters(command=sys.executable, args=["-c", BOOTSTRAP])
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    names = [tool.name for tool in listing.tools]
                    self.assertEqual(names, EXPECTED_TOOLS)
                    flights_tool = next(
                        tool for tool in listing.tools if tool.name == "search_flights"
                    )
                    self.assertNotIn("selection", flights_tool.inputSchema["properties"])
                    original = payload(
                        await session.call_tool(
                            "search_flights",
                            {"routes": ["JFK-LHR:2099-07-01"], "fetch": "sweep", "top": 1},
                        )
                    )
                    offer = original["queries"][0]["offers"][0]
                    self.assertNotIn("selection_id", offer)
                    self.assertEqual(offer["price"], 100)
                    hidden_city = payload(
                        await session.call_tool(
                            "search_hidden_city",
                            {"route": "JFK-LHR", "departure": "2099-07-01"},
                        )
                    )
                    hidden_offer = hidden_city["offers"][0]
                    self.assertEqual(hidden_offer["price"], 622)
                    self.assertEqual(hidden_offer["evidence"], "confirmed")
                    self.assertNotIn("selection_id", hidden_offer)
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
                    last = None
                    for _ in range(25):
                        last = payload(
                            await session.call_tool(
                                "get_hotel_details", {"selection_id": hotel_ref}
                            )
                        )
                    self.assertIsNone(last["room_quotes"])
                    self.assertEqual(last["original_quote"]["offer"]["title"], "Czech Inn")
                    self.assertEqual((last["status"], last["completeness"]), ("ok", "complete"))
                    checked = payload(
                        await session.call_tool("verify_answer", {"answer": "The stay is EUR 150."})
                    )
                    self.assertTrue(checked["ok"])
                    rate = payload(
                        await session.call_tool(
                            "get_hotel_details",
                            {"selection_id": hotel_ref, "room_rates": True},
                        )
                    )
                    self.assertEqual(rate["original_quote"]["currency"], "EUR")
                    self.assertEqual(rate["room_quotes"]["currency"], "USD")
                    self.assertEqual(rate["echo"], "matched")
                    self.assertEqual((rate["status"], rate["completeness"]), ("ok", "complete"))

                    async def detail(location):
                        found = payload(
                            await session.call_tool(
                                "search_hotels",
                                {
                                    "location": location,
                                    "check_in": "2099-07-01",
                                    "check_out": "2099-07-04",
                                    "currency": "EUR",
                                    "adults": 2,
                                    "rooms": 1,
                                },
                            )
                        )
                        ref = found["queries"][0]["offers"][0]["selection_id"]
                        return payload(
                            await session.call_tool(
                                "get_hotel_details",
                                {"selection_id": ref, "room_rates": True},
                            )
                        )

                    unknown = await detail("Oslo, NO")
                    self.assertEqual(unknown["echo"], "unknown")
                    self.assertNotEqual(
                        (unknown["status"], unknown["completeness"]), ("ok", "complete")
                    )
                    empty = await detail("Rome, IT")
                    self.assertEqual(
                        (empty["status"], empty["empty_reason"]), ("no_results", "provider_empty")
                    )
                    limited = await detail("Tokyo, JP")
                    self.assertEqual(limited["status"], "rate_limited")
                    self.assertEqual(
                        limited["retry_after"], limited["room_quotes"]["error"]["retry_after"]
                    )
                    self.assertEqual(
                        limited["retry_after_seconds"],
                        limited["room_quotes"]["error"]["retry_after_seconds"],
                    )
                    self.assertEqual((await detail("Dublin, IE"))["status"], "blocked")
                    self.assertEqual((await detail("Vienna, AT"))["status"], "timeout")
                    self.assertEqual((await detail("Athens, GR"))["status"], "failed")
                    party = await detail("Lisbon, PT")
                    self.assertEqual(
                        (party["status"], party["empty_reason"], party["error_code"]),
                        ("no_results", "filtered_out", "occupancy_mismatch"),
                    )
                    self.assertIsNone(party["room_quotes"])
                    dates = await detail("Paris, FR")
                    self.assertEqual(dates["error_code"], "dates_mismatch")
                    self.assertIsNone(dates["room_quotes"])
                    ambiguous = await detail("Columbus")
                    self.assertEqual(
                        (ambiguous["status"], ambiguous["completeness"], ambiguous["error_code"]),
                        ("ok", "partial", "ambiguous_city"),
                    )
                    self.assertIsNone(ambiguous["room_quotes"])
                    missed = await detail("Springfield")
                    self.assertEqual(
                        (missed["status"], missed["empty_reason"], missed["error_code"]),
                        ("no_results", "filtered_out", "property_mismatch"),
                    )
                    self.assertNotEqual(
                        (missed["status"], missed["completeness"]), ("ok", "complete")
                    )
                    rejected = await session.call_tool(
                        "get_hotel_details",
                        {"selection_id": hotel_ref, "room_rates": "yes"},
                    )
                    self.assertTrue(rejected.isError)
                    self.assertIn("boolean", rejected.content[0].text)
                    missing = await session.call_tool(
                        "get_hotel_details", {"selection_id": "sel_missing"}
                    )
                    self.assertTrue(missing.isError)
                    self.assertIn("unknown or evicted", missing.content[0].text)
                    metro = await session.call_tool(
                        "search_flights",
                        {"routes": ["LON-CDG:2099-07-01"], "fetch": "sweep", "top": 1},
                    )
                    metro_body = payload(metro)
                    jsonschema.validate(metro_body, flights_tool.outputSchema)
                    self.assertEqual(
                        {row["query"]["origin"] for row in metro_body["queries"]},
                        {"LHR", "LGW", "STN", "LTN", "LCY", "SEN"},
                    )
                    for metro_row in metro_body["queries"]:
                        self.assertEqual(metro_row["query"]["destination"], "CDG")
                        self.assertIn("recommendation", metro_row)
                        shown = metro_row["offers"][0]["legs"][0]["segments"][0]
                        picked = metro_row["recommendation"]["shortlist"][0]["offer"]["legs"][0][
                            "segments"
                        ][0]
                        for key in ("arrival_date", "departure_timezone", "arrival_timezone"):
                            self.assertEqual(picked[key], shown[key])
                            self.assertEqual(shown[key], _SEGMENT[key])
                    same_metro = await session.call_tool(
                        "search_flights",
                        {"routes": ["LON-LON:2099-07-01"], "fetch": "sweep"},
                    )
                    self.assertTrue(same_metro.isError)
                    self.assertIn("same metro", same_metro.content[0].text)
                    member = await session.call_tool(
                        "search_flights",
                        {"routes": ["JFK-NYC:2099-07-01"], "fetch": "sweep"},
                    )
                    self.assertTrue(member.isError)
                    self.assertIn("same metro", member.content[0].text)
                    deadline = payload(await session.call_tool("validate_itinerary", DEADLINE_ARGS))
                    status = next(
                        item["status"]
                        for item in deadline["checks"]
                        if item["constraint"] == "arrival_deadline"
                    )
                    self.assertEqual(status, "pass")

        asyncio.run(exercise())
