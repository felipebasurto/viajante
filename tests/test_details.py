from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from test_skiplagged_hotels import _details_result, _routing_rpc, _search_result
from viajante import evidence, mcp_handlers
from viajante.details import _city_decision, get_hotel_details
from viajante.skiplagged_hotels import search_hotel_rooms


def hotel(
    location="Prague",
    *,
    backend="google",
    currency="EUR",
    title="Czech Inn",
    latitude=None,
    longitude=None,
    provider="keep",
):
    offer = {
        "title": title,
        "total_price": 150,
        "details": "description does not prove a private room",
        "priced_adults": None,
        "sleeps": None,
        "lodging_evidence_conflict": True,
        "lodging_kind": "unknown",
        "provider_id": "25584",
    }
    if latitude is not None:
        offer["latitude"] = latitude
        offer["longitude"] = longitude
    payload = {
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
                "offers": [offer],
            }
        ],
    }
    if provider != "keep":
        payload["provider"] = provider
    return payload


class HotelDetailsTests(unittest.TestCase):
    def test_snapshot_keeps_conflict_unknown_occupancy_and_original_terms(self):
        with patch("viajante.details.search_hotel_rooms") as rooms:
            result = get_hotel_details(hotel(), 0, 0)
        rooms.assert_not_called()
        quote = result["original_quote"]
        self.assertTrue(quote["offer"]["lodging_evidence_conflict"])
        self.assertIsNone(quote["offer"]["priced_adults"])
        self.assertEqual(quote["currency"], "EUR")

    def test_missing_provider_stays_unknown(self):
        result = get_hotel_details(hotel(provider=None), 0, 0)
        self.assertIsNone(result["original_quote"]["provider"])

    def test_room_rates_rejects_non_booleans_before_a_search(self):
        with patch("viajante.details.search_hotel_rooms") as rooms:
            for value in ("yes", 1, "true"):
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(ValueError, "room_rates must be a boolean"),
                ):
                    get_hotel_details(hotel(), 0, 0, room_rates=value)
        rooms.assert_not_called()

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
        self.assertNotIn("occupancy_mismatch", result)
        self.assertNotIn("dates_mismatch", result)

    def test_provider_echo_of_a_different_party_or_dates_is_not_the_quote(self):
        echoed = _details_result()
        echoed["structuredContent"]["numAdults"] = 2
        echoed["structuredContent"]["numRooms"] = 1
        echoed["structuredContent"]["checkin"] = "2099-08-01"
        echoed["structuredContent"]["checkout"] = "2099-08-04"
        result, _calls = self._rates(hotel(), details_result=echoed)
        self.assertTrue(result["occupancy_mismatch"])
        self.assertTrue(result["dates_mismatch"])
        self.assertIsNone(result["room_quotes"])
        self.assertEqual(result["provider_answer"]["adults"], 2)
        self.assertEqual(result["provider_answer"]["check_in"], "2099-08-01")
        self.assertEqual(result["original_quote"]["query"]["adults"], 5)

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
        result = hotel(location="London, GB")["queries"][0]
        location, anchor = _city_decision(result, result["offers"][0])
        self.assertEqual(location, "London, GB")
        self.assertIsNone(anchor)
        result["resolved_place"] = "London"
        self.assertEqual(_city_decision(result, result["offers"][0])[0], "London, GB")
        result["resolved_place"] = "Paris"
        with self.assertRaisesRegex(ValueError, "resolved place"):
            _city_decision(result, result["offers"][0])

    def test_homonym_cities_are_inconclusive_without_a_live_call(self):
        for city in ("Springfield", "Portland", "Columbus"):
            with self.subTest(city=city), patch("viajante.details.search_hotel_rooms") as rooms:
                result = get_hotel_details(hotel(location=city), 0, 0, room_rates=True)
            rooms.assert_not_called()
            self.assertEqual(result["room_rates_status"], "inconclusive")
            self.assertIsNone(result["room_quotes"])
            self.assertIn(city, result["reason"])

    def test_coordinates_pick_one_place_and_must_match_the_property(self):
        # Springfield, Missouri, not Springfield, Illinois.
        original = hotel(location="Springfield", latitude=37.20, longitude=-93.30)
        page = _search_result(
            url=(
                "https://skiplagged.com/hotels/1/springfield-missouri-hotels/2099-07-01/2099-07-04"
            )
        )
        near = _details_result()
        near["structuredContent"]["location"] = {"lat": 37.201, "lng": -93.301}
        result, calls = self._rates(original, search_result=page, details_result=near)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(result["room_quotes"]["currency"], "USD")
        far = _details_result()
        far["structuredContent"]["location"] = {"lat": 50.07, "lng": 14.44}
        missed, _calls = self._rates(original, search_result=page, details_result=far)
        self.assertEqual(missed["room_rates_status"], "inconclusive")
        self.assertIsNone(missed["room_quotes"])

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
        result, _calls = self._rates(hotel(), details_result=empty)
        self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
        self.assertEqual(result["original_quote"]["offer"]["total_price"], 150)


class SelectionReferenceTests(unittest.TestCase):
    def setUp(self):
        evidence.clear()
        mcp_handlers._CACHE.clear()
        self.addCleanup(evidence.clear)
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_reads_do_not_evict_the_search_or_the_ledger(self):
        payload = mcp_handlers._owned(hotel())
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        for _ in range(25):
            detail = mcp_handlers.get_hotel_details_tool(selection_id)
        self.assertEqual(detail["original_quote"]["offer"]["title"], "Czech Inn")
        self.assertEqual(len(evidence._ledger), 1)
        self.assertTrue(evidence.verify_answer("The stay is EUR 150.")["ok"])

    def test_non_boolean_room_rates_fails_before_lookup(self):
        with self.assertRaisesRegex(ValueError, "room_rates must be a boolean"):
            mcp_handlers.get_hotel_details_tool("sel_missing", room_rates="yes")

    def test_flight_and_hidden_city_rows_are_not_stored(self):
        flight = {"queries": [{"offers": [{"price": 100.0, "evidence": {"query": {}}}]}]}
        hidden = {"offers": [{"price": 622.0, "evidence": "confirmed"}]}
        self.assertEqual(evidence.selection_records(flight), {})
        self.assertEqual(evidence.selection_records(hidden), {})
        self.assertNotIn("selection_id", flight["queries"][0]["offers"][0])
        self.assertNotIn("selection_id", hidden["offers"][0])

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

    def test_nested_trip_hotels_are_stored_and_flights_are_not(self):
        stay = hotel()
        flights = {"queries": [{"offers": [{"price": 100.0, "evidence": "confirmed"}]}]}
        payload = {"flights": flights, "hotels": stay}
        typed = type(
            "Trip", (), {"flights": object(), "hotels": type("Hotels", (), {"queries": []})()}
        )()
        records = evidence.selection_records(payload, typed)
        self.assertNotIn("selection_id", flights["queries"][0]["offers"][0])
        self.assertIn("selection_id", stay["queries"][0]["offers"][0])
        self.assertEqual(len(records), 1)
