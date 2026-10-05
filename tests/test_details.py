from __future__ import annotations

import os
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from test_audit_regressions import FUTURE, _connecting_return, _direct, _Source
from test_audit_regressions import _card as _flight_card
from test_skiplagged_hotels import _card, _details_result, _routing_rpc, _search_result
from viajante import evidence, mcp_handlers
from viajante.details import get_flight_details, get_hotel_details
from viajante.flights import search_flights
from viajante.google_flights import RawFlightCard
from viajante.models import (
    FlightOffer,
    FlightQuery,
    QuerySuccess,
    RawJourneyLeg,
    RawSegment,
    RoundTrip,
    SearchReport,
)
from viajante.ratelimit import note_rate_limited
from viajante.skiplagged_hotels import search_hotel_rooms

DAY = date(2099, 7, 1)
QUERY = FlightQuery("JFK", "LHR", DAY)
NOW = datetime(2099, 1, 1)


def flight(price=100, token="old", *, number="BA100", complete=True):
    segment = RawSegment(
        "JFK",
        "LHR",
        "22:00",
        "10:00",
        "British Airways",
        number,
        DAY,
        "BA",
        date(2099, 7, 2),
        "America/New_York",
        "Europe/London",
    )
    return FlightOffer(
        "British Airways",
        "22:00",
        "10:00",
        f"${price}",
        price,
        "7 hr",
        7,
        "Nonstop",
        0,
        0,
        False,
        booking_token=token,
        checked_bags=1,
        carry_on=1,
        legs=(
            RawJourneyLeg(
                "22:00", "10:00", "7 hr", "Nonstop", segments=(segment,) if complete else ()
            ),
        ),
    )


def report(offers=None, *, options=None, query=QUERY):
    rows = tuple(offers or [flight()])
    return SearchReport(
        NOW,
        (QuerySuccess(query, len(rows), len(rows), rows),),
        currency="USD",
        search_options=options or {"fetch": "sweep", "top": 1, "currency": "USD"},
    )


def hotel(location="Prague", *, backend="google", currency="EUR", title="Czech Inn"):
    return {
        "provider": "google_hotels" if backend == "google" else backend,
        "currency": currency,
        "searched_at": "2099-01-01T00:00:00Z",
        "fetch_backend": backend,
        "queries": [
            {
                "status": "ok",
                "query": {
                    "location": location,
                    "check_in": "2099-07-01",
                    "check_out": "2099-07-04",
                    "adults": 5,
                    "rooms": 2,
                },
                "offers": [
                    {
                        "title": title,
                        "total_price": 150,
                        "details": "description does not prove a private room",
                        "priced_adults": None,
                        "sleeps": None,
                        "lodging_evidence_conflict": True,
                        "lodging_kind": "unknown",
                        "provider_id": "25584",
                    }
                ],
            }
        ],
    }


class FlightDetailsTests(unittest.TestCase):
    def test_packaged_refresh_checks_filters_after_attaching_return(self):
        query = RoundTrip("JFK", "LHR", FUTURE, FUTURE + timedelta(days=3))
        source = _Source([_flight_card(price="$100", legs=(_direct(),))])
        source.fetch_selected = lambda trip, selections: [
            (_flight_card(price="$100", legs=(_connecting_return(trip.return_date),)),)
            for _ in selections
        ]
        for selected, filters, expected in (
            (
                query,
                {"via": ("BOS",), "require_overnight": ("BOS",), "max_duration_hours": 3},
                {"max_duration_hours"},
            ),
            (
                query,
                {"exclude_via": ("BOS",), "max_duration_hours": 3},
                {"exclude_via", "max_duration_hours"},
            ),
            (replace(query, max_stops=0), {"max_duration_hours": 20}, {"max_stops"}),
        ):
            with (
                self.subTest(filters=filters),
                patch("viajante.flights.GoogleFlightsHttpSource", return_value=source),
                patch("viajante.flights.playwright_available", return_value=False),
            ):
                fresh = search_flights(
                    [selected], fetch="sweep", _details_candidates=True, **filters
                )
            self.assertEqual(len(fresh.queries[0].offers), 1)
            self.assertEqual(set(fresh.queries[0].offers[0].refresh_filter_violations), expected)

    def test_snapshot_is_offline_with_age_and_unknowns(self):
        with patch("viajante.details.search_flights") as search:
            result = get_flight_details(report(), 0, 0, now=datetime(2099, 1, 1, 0, 5))
        search.assert_not_called()
        self.assertEqual(result["age_seconds"], 300)
        self.assertEqual(result["match_status"], "snapshot")
        self.assertIn("refund_terms", result["unknown_fields"])
        self.assertEqual(result["original_quote"]["offer"]["booking_token"], "old")

    def test_changed_price_and_token_keep_identity_and_original_quote(self):
        fresh = report([flight(130, "new")])
        with patch("viajante.details.search_flights", return_value=fresh) as search:
            result = get_flight_details(report(), 0, 0, refresh=True)
        self.assertTrue(search.call_args.kwargs["_details_candidates"])
        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["price_change"], 30)
        self.assertEqual(result["original_quote"]["offer"]["price"], 100)
        self.assertEqual(result["new_quote"]["offer"]["booking_token"], "new")

    def test_incomplete_identity_sends_nothing(self):
        with patch("viajante.details.search_flights") as search:
            result = get_flight_details(report([flight(complete=False)]), 0, 0, refresh=True)
        self.assertEqual(result["match_status"], "incomplete_identity")
        search.assert_not_called()

    def test_multiple_and_no_match_never_substitute(self):
        for fresh, expected in [
            (report([flight(120), flight(130)]), "multiple_matches"),
            (report([flight(number="BA200")]), "no_match"),
        ]:
            with patch("viajante.details.search_flights", return_value=fresh):
                result = get_flight_details(report(), 0, 0, refresh=True)
            self.assertEqual(result["match_status"], expected)
            self.assertIsNone(result["new_quote"])

    def test_changed_currency_does_not_report_price_delta(self):
        fresh = replace(report([flight(130)]), currency="EUR")
        with patch("viajante.details.search_flights", return_value=fresh):
            result = get_flight_details(report(), 0, 0, refresh=True)
        self.assertEqual(result["match_status"], "matched")
        self.assertNotIn("price_change", result)
        self.assertEqual(result["original_quote"]["currency"], "USD")
        self.assertEqual(result["new_quote"]["currency"], "EUR")

    def test_query_occupancy_mismatch_is_inconclusive(self):
        fresh = report(query=replace(QUERY, adults=2))
        with patch("viajante.details.search_flights", return_value=fresh):
            result = get_flight_details(report(), 0, 0, refresh=True)
        self.assertEqual(result["match_status"], "query_mismatch")

    def test_refresh_search_before_top_and_filters_preserves_new_price(self):
        target = flight(130, "new")
        competitor = flight(50, "cheap", number="BA200")

        def raw(offer):
            return RawFlightCard(
                offer.airline,
                offer.departure,
                offer.arrival,
                offer.duration,
                offer.stops,
                offer.price_text,
                booking_token=offer.booking_token,
                legs=offer.legs,
                checked_bags=1,
                carry_on=1,
            )

        class Source:
            config = SimpleNamespace(html_lang="en", currency="USD", country=None)

            def fetch(self, trip):
                return [raw(competitor), raw(target)]

            def close(self):
                pass

        original_query = replace(QUERY, price_cap=110)
        original = report(query=original_query)
        with (
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=Source()),
            patch("viajante.flights.playwright_available", return_value=False),
        ):
            result = get_flight_details(original, 0, 0, refresh=True)
        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["new_quote"]["offer"]["price"], 130)
        self.assertEqual(result["filter_violations"], ["price_cap"])
        self.assertEqual(len(result["refresh_result"]["queries"][0]["offers"]), 2)

    def test_cooldown_stops_refresh_before_network_and_browser(self):
        with (
            tempfile.TemporaryDirectory() as state,
            patch.dict(os.environ, {"VIAJANTE_STATE_DIR": state}),
        ):
            note_rate_limited()
            with (
                patch("viajante.google_flights.shared_chrome_sweep_client") as client,
                patch("viajante.flights.GoogleFlightsSource") as browser,
                patch("viajante.flights.playwright_available", return_value=True),
            ):
                result = get_flight_details(report(), 0, 0, refresh=True)
        self.assertEqual(result["match_status"], "provider_error")
        self.assertTrue(result["refresh_result"]["queries"][0]["error"]["rate_limited"])
        client.assert_not_called()
        browser.assert_not_called()

    def test_real_search_retains_original_options(self):
        class Source:
            config = SimpleNamespace(html_lang="en", currency="USD", country=None)

            def fetch(self, query):
                offer = flight()
                return [
                    RawFlightCard(
                        offer.airline,
                        offer.departure,
                        offer.arrival,
                        offer.duration,
                        offer.stops,
                        offer.price_text,
                        legs=offer.legs,
                    )
                ]

            def close(self):
                pass

        with (
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=Source()),
            patch("viajante.flights.playwright_available", return_value=False),
        ):
            original = search_flights([QUERY], fetch="sweep", max_duration_hours=9, top=1)
        self.assertEqual(original.search_options["max_duration_hours"], 9)
        self.assertEqual(original.search_options["top"], 1)
        self.assertNotIn("search_options", original.to_dict())


class HotelDetailsTests(unittest.TestCase):
    def test_snapshot_keeps_conflict_unknown_occupancy_and_original_terms(self):
        with patch("viajante.details.search_hotel_rooms") as rooms:
            result = get_hotel_details(hotel(), 0, 0)
        rooms.assert_not_called()
        quote = result["original_quote"]
        self.assertTrue(quote["offer"]["lodging_evidence_conflict"])
        self.assertIsNone(quote["offer"]["priced_adults"])
        self.assertEqual(quote["currency"], "EUR")

    def _rates(self, original, search_result=None, details_result=None):
        calls = []
        rpc = _routing_rpc(
            search_result or _search_result(), details_result or _details_result(), calls
        )

        def rooms(*args, **kwargs):
            return search_hotel_rooms(*args, **kwargs, rpc=rpc, sleep=lambda _: None)

        with (
            patch("viajante.details.search_hotel_rooms", side_effect=rooms),
            patch("viajante.skiplagged.rate_limit_status", return_value=None),
        ):
            result = get_hotel_details(original, 0, 0, room_rates=True)
        return result, calls

    def test_external_exact_name_usd_quote_and_occupancy_stay_separate(self):
        result, calls = self._rates(hotel(title="CZECH  Inn!"))
        self.assertEqual(result["original_quote"]["currency"], "EUR")
        self.assertEqual(result["room_quotes"]["currency"], "USD")
        self.assertEqual(result["original_quote"]["offer"]["total_price"], 150)
        for call in calls:
            self.assertEqual(call["arguments"]["numAdults"], 5)
            self.assertEqual(call["arguments"]["numRooms"], 2)
        self.assertEqual(calls[0]["arguments"]["city"], "Prague")
        self.assertEqual(calls[-1]["arguments"]["hotelId"], 25584)
        self.assertEqual(result["room_quotes"]["rates"][0]["occupancy_limit"], 6)
        self.assertNotIn("combined_capacity", result)

    def test_skiplagged_uses_only_its_id(self):
        result, calls = self._rates(
            hotel(backend="skiplagged", currency="USD", location="unproven region")
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "sk_hotel_details")
        self.assertEqual(result["room_quotes"]["hotel_id"], "25584")

    def test_missing_city_rejects_before_provider(self):
        with patch("viajante.details.search_hotel_rooms") as rooms:
            with self.assertRaisesRegex(ValueError, "unambiguous city"):
                get_hotel_details(hotel(location="near a museum"), 0, 0, room_rates=True)
        rooms.assert_not_called()

    def test_city_country_qualifier_disambiguates_catalogue_city(self):
        from viajante.details import _city

        result = hotel(location="London, GB")["queries"][0]
        self.assertEqual(_city(result), "London, GB")
        result["resolved_place"] = "London"
        self.assertEqual(_city(result), "London, GB")
        result["resolved_place"] = "Paris"
        with self.assertRaisesRegex(ValueError, "resolved place"):
            _city(result)

    def test_skiplagged_city_mismatch_keeps_original_and_sends_no_room_request(self):
        wrong_city = _search_result(
            url="https://skiplagged.com/hotels/1/costa-mesa-california-hotels/x/"
        )
        result, calls = self._rates(hotel(), search_result=wrong_city)
        self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
        self.assertIn("requested city was not searched", result["room_quotes"]["error"]["message"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["original_quote"]["offer"]["total_price"], 150)

    def test_provider_failure_keeps_original_quote(self):
        empty = {"content": [], "structuredContent": {"rooms": []}}
        result, _ = self._rates(hotel(), details_result=empty)
        self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
        self.assertEqual(result["original_quote"]["offer"]["total_price"], 150)

    def test_homonyms_remain_ambiguous_and_near_names_never_substitute(self):
        ambiguous = _search_result()
        ambiguous["structuredContent"]["results"].append(_card(99, "CZECH INN", "Other street", 2))
        ambiguous["content"][0]["text"] += (
            "\n| **CZECH INN**<br/>Other street | 2★ · 8/10 | $50 | $190 | Kitchen | "
            "[View](https://skiplagged.com/hotel/99/x/) |"
        )
        for original, page in [(hotel(), ambiguous), (hotel(title="Czech Inne"), _search_result())]:
            result, calls = self._rates(original, search_result=page)
            self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["original_quote"]["offer"]["total_price"], 150)


class SelectionReferenceTests(unittest.TestCase):
    def setUp(self):
        evidence.clear()
        mcp_handlers._CACHE.clear()
        self.addCleanup(evidence.clear)
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_registered_reference_snapshot_and_eviction(self):
        payload = mcp_handlers._owned(dict(report().to_dict()), report())
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        result = mcp_handlers.get_flight_details_tool(selection_id)
        self.assertEqual(result["match_status"], "snapshot")
        for _ in range(evidence.LEDGER_SIZE):
            evidence.record({})
        with patch("viajante.details.search_flights") as search:
            with self.assertRaisesRegex(ValueError, "unknown or evicted"):
                mcp_handlers.get_flight_details_tool(selection_id, refresh=True)
        search.assert_not_called()

    def test_wrong_tool_unknown_id_and_foreign_process_reject_before_network(self):
        payload = mcp_handlers._owned(hotel())
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        with patch("viajante.details.search_flights") as flights:
            for ref in [selection_id, "sel_nonexistent"]:
                with self.assertRaises(ValueError):
                    mcp_handlers.get_flight_details_tool(ref, refresh=True)
        flights.assert_not_called()
        evidence.clear()
        with self.assertRaises(ValueError):
            mcp_handlers.get_hotel_details_tool(selection_id)

    def test_cache_replay_restores_evicted_ref_without_freshness_changes(self):
        original = report()
        with patch("viajante.mcp_handlers.search_flights", return_value=original) as search:
            payload = mcp_handlers.search_flights_tool(["JFK-LHR:2099-07-01"])
            saved = deepcopy(payload)
            selection_id = payload["queries"][0]["offers"][0]["selection_id"]
            payload["queries"][0]["offers"][0]["price"] = 999
            for _ in range(evidence.LEDGER_SIZE):
                evidence.record({})
            cached = mcp_handlers.search_flights_tool(["JFK-LHR:2099-07-01"])
        search.assert_called_once()
        self.assertTrue(cached.pop("cached"))
        self.assertEqual(cached, saved)
        detail = mcp_handlers.get_flight_details_tool(selection_id)
        self.assertEqual(detail["original_quote"]["searched_at"], saved["searched_at"])
        self.assertEqual(detail["original_quote"]["offer"]["price"], 100)

    def test_refresh_bypasses_cache_and_honors_process_lock(self):
        original = report()
        with patch("viajante.mcp_handlers.search_flights", return_value=original):
            payload = mcp_handlers.search_flights_tool(["JFK-LHR:2099-07-01"])
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        with patch(
            "viajante.details.search_flights", return_value=report([flight(120)])
        ) as refresh:
            result = mcp_handlers.get_flight_details_tool(selection_id, refresh=True)
        refresh.assert_called_once()
        self.assertEqual(result["price_change"], 20)
        self.assertTrue(evidence.verify_answer("Price change: USD 20.")["ok"])
        evidence.record({"currency": "EUR", "price": 999})
        self.assertFalse(evidence.verify_answer("Price change: EUR 20.")["ok"])
        mcp_handlers._SEARCH_LOCK.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "already running"):
                mcp_handlers.get_flight_details_tool(selection_id, refresh=True)
        finally:
            mcp_handlers._SEARCH_LOCK.release()

    def test_flex_reference_keeps_chosen_shop_context(self):
        original = report()
        payload = dict(original.to_dict())
        offer = payload.pop("queries")[0]["offers"][0]
        offer["evidence"] = {"query": QUERY.to_dict()}
        payload["offers"] = [offer]
        flex = SimpleNamespace(details_report=original)
        mcp_handlers._owned(payload, flex)
        ref = offer["selection_id"]
        with patch("viajante.details.search_flights", return_value=report([flight(130)])):
            result = mcp_handlers.get_flight_details_tool(ref, refresh=True)
        self.assertEqual(result["match_status"], "matched")

    def test_multi_stay_references_keep_each_original_occupancy(self):
        payload = hotel()
        other = deepcopy(payload["queries"][0])
        other["query"]["adults"] = 3
        other["offers"][0]["title"] = "Other Hotel"
        payload["queries"].append(other)
        mcp_handlers._owned(payload)
        first, second = [result["offers"][0]["selection_id"] for result in payload["queries"]]
        self.assertNotEqual(first, second)
        result = mcp_handlers.get_hotel_details_tool(second)
        self.assertEqual(result["original_quote"]["query"]["adults"], 3)
        self.assertEqual(result["original_quote"]["offer"]["title"], "Other Hotel")

    def test_nested_trip_references_keep_typed_refresh_context(self):
        original = report()
        trip = SimpleNamespace(flights=original, hotels=None)
        payload = {"flights": dict(original.to_dict())}
        payload = mcp_handlers._owned(payload, trip)
        selection_id = payload["flights"]["queries"][0]["offers"][0]["selection_id"]
        with patch("viajante.details.search_flights", return_value=report([flight(120)])):
            result = mcp_handlers.get_flight_details_tool(selection_id, refresh=True)
        self.assertEqual(result["match_status"], "matched")
