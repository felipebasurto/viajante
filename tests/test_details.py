from __future__ import annotations

import time
import unittest
from copy import deepcopy
from datetime import date, datetime, timezone
from unittest.mock import patch

import _isolate  # noqa: F401
from test_skiplagged_hotels import _details_result, _routing_rpc, _search_result
from viajante import evidence, mcp_handlers
from viajante.details import _city_decision, get_hotel_details
from viajante.models import HotelRoomRate, HotelRoomsReport, SearchError, SearchErrorCode
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
    def test_owned_city_aliases_use_the_catalogue_name_for_room_search(self):
        for alias, canonical, country in (
            ("Lisboa", "Lisbon", "PT"),
            ("Ciudad de México", "Mexico City", "MX"),
        ):
            for resolved in (alias, canonical, f"{canonical}, {country}"):
                with self.subTest(alias=alias, resolved=resolved):
                    qualified = "," in resolved
                    location = f"{alias}, {country}" if qualified else alias
                    expected = f"{canonical}, {country}" if qualified else canonical
                    original = hotel(location=location)
                    original["queries"][0]["resolved_place"] = resolved
                    with patch(
                        "viajante.details.search_hotel_rooms",
                        return_value=_rooms(city=canonical),
                    ) as rooms:
                        detail = get_hotel_details(original, 0, 0, room_rates=True)
                    self.assertEqual(rooms.call_args.kwargs["city"], expected)
                    self.assertEqual((detail["status"], detail["completeness"]), ("ok", "complete"))

    def test_distinct_unicode_property_does_not_resolve_or_fetch_rooms(self):
        search = _search_result()
        for card in search["structuredContent"]["results"]:
            card["name"] = "大阪ホテル"
        search["content"][0]["text"] = (
            search["content"][0]["text"]
            .replace("Czech Inn", "大阪ホテル")
            .replace("a&o Prague Rhea", "大阪ホテル")
        )
        result, calls = self._rates(hotel(title="東京ホテル"), search_result=search)
        self.assertEqual(result["room_quotes"]["rates"], [])
        self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
        self.assertEqual(result["status"], "no_results")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "sk_hotels_search")

    def test_room_detail_name_cannot_contradict_the_selected_property(self):
        with patch("viajante.details.search_hotel_rooms", return_value=_rooms(name="大阪ホテル")):
            detail = get_hotel_details(hotel(title="東京ホテル"), 0, 0, room_rates=True)
        self.assertEqual(detail["error_code"], "property_mismatch")
        self.assertIsNone(detail["room_quotes"])

    def test_room_detail_city_cannot_contradict_the_selected_city(self):
        original = hotel(location="San Jose, US", title="Holiday Inn Express")
        for city in ("San Jose del Cabo", "San Jose, MX"):
            with (
                self.subTest(city=city),
                patch(
                    "viajante.details.search_hotel_rooms",
                    return_value=_rooms(name="Holiday Inn Express", city=city),
                ),
            ):
                detail = get_hotel_details(original, 0, 0, room_rates=True)
            self.assertEqual(detail["error_code"], "property_mismatch")
            self.assertIsNone(detail["room_quotes"])
            self.assertIn("city differs", detail["reason"])

    def test_city_prefix_slug_cannot_supply_same_chain_rates_from_wrong_city(self):
        wrong_city = _search_result(
            url=(
                "https://skiplagged.com/hotels/1/"
                "san-jose-del-cabo-mexico-hotels/2099-07-01/2099-07-04"
            )
        )
        wrong_city["content"][0]["text"] = wrong_city["content"][0]["text"].replace(
            "# Hotels in Prague", "# Hotels in San Jose"
        )
        result, calls = self._rates(
            hotel(location="San Jose, US", title="Czech Inn"), search_result=wrong_city
        )
        self.assertEqual(result["room_quotes"]["error"]["code"], "no_results")
        self.assertIn("different city", result["room_quotes"]["error"]["message"])
        self.assertEqual([call["name"] for call in calls], ["sk_hotels_search"])

    def test_unambiguous_city_keeps_property_coordinates(self):
        original = hotel(latitude=50.07, longitude=14.44)
        for latitude, longitude, accepted in ((48.85, 2.35, False), (50.0701, 14.4401, True)):
            with (
                self.subTest(accepted=accepted),
                patch(
                    "viajante.details.search_hotel_rooms",
                    return_value=_rooms(latitude=latitude, longitude=longitude),
                ),
            ):
                detail = get_hotel_details(original, 0, 0, room_rates=True)
            if accepted:
                self.assertEqual((detail["status"], detail["completeness"]), ("ok", "complete"))
                self.assertIsNotNone(detail["room_quotes"])
            else:
                self.assertEqual(detail["error_code"], "property_mismatch")
                self.assertIsNone(detail["room_quotes"])

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
        self.assertEqual(result["echo"], "unknown")
        self.assertNotEqual((result["status"], result["completeness"]), ("ok", "complete"))

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

    def test_missing_city_is_inconclusive_before_provider(self):
        # A stored search whose city the catalogue cannot place is a result condition, not a
        # caller error: the answer is inconclusive and nothing is sent.
        with patch("viajante.details.search_hotel_rooms") as rooms:
            result = get_hotel_details(hotel(location="near a museum"), 0, 0, room_rates=True)
        rooms.assert_not_called()
        self.assertEqual(result["room_rates_status"], "inconclusive")
        self.assertIn("unambiguous city", result["reason"])

    def test_city_country_qualifier_disambiguates_catalogue_city(self):
        result = hotel(location="London, GB")["queries"][0]
        location, anchor = _city_decision(result, result["offers"][0])
        self.assertEqual(location, "London, GB")
        self.assertIsNone(anchor)
        result["resolved_place"] = "London"
        self.assertEqual(_city_decision(result, result["offers"][0])[0], "London, GB")
        result["resolved_place"] = "Paris"
        location, anchor = _city_decision(result, result["offers"][0])
        self.assertIsNone(location)
        self.assertIn("resolved place", anchor)

    def test_homonym_cities_are_inconclusive_without_a_live_call(self):
        for city in ("Springfield", "Portland", "Columbus"):
            with self.subTest(city=city), patch("viajante.details.search_hotel_rooms") as rooms:
                result = get_hotel_details(hotel(location=city), 0, 0, room_rates=True)
            rooms.assert_not_called()
            self.assertEqual(result["room_rates_status"], "inconclusive")
            self.assertIsNone(result["room_quotes"])
            self.assertIn(city, result["reason"])
            self.assertEqual(
                (result["status"], result["completeness"], result["error_code"]),
                ("ok", "partial", "ambiguous_city"),
            )

    def test_coordinates_pick_one_place_and_must_match_the_property(self):
        # Springfield, Missouri, not Springfield, Illinois.
        original = hotel(location="Springfield", latitude=37.20, longitude=-93.30)
        page = _search_result(
            url=(
                "https://skiplagged.com/hotels/1/springfield-missouri-hotels/2099-07-01/2099-07-04"
            )
        )
        page["content"][0]["text"] = page["content"][0]["text"].replace(
            "# Hotels in Prague", "# Hotels in Springfield"
        )
        near = _details_result()
        near["structuredContent"]["cityName"] = "Springfield"
        near["structuredContent"]["location"] = {"lat": 37.201, "lng": -93.301}
        result, calls = self._rates(original, search_result=page, details_result=near)
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(result["room_quotes"]["currency"], "USD")
        far = _details_result()
        far["structuredContent"]["cityName"] = "Springfield"
        far["structuredContent"]["location"] = {"lat": 50.07, "lng": 14.44}
        missed, _calls = self._rates(original, search_result=page, details_result=far)
        self.assertEqual(missed["room_rates_status"], "inconclusive")
        self.assertIsNone(missed["room_quotes"])
        self.assertEqual(
            (missed["status"], missed["empty_reason"], missed["error_code"]),
            ("no_results", "filtered_out", "property_mismatch"),
        )

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
    def test_new_room_quotes_are_verifiable_without_renewing_original(self):
        payload = mcp_handlers._owned(hotel())
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        with patch("viajante.details.search_hotel_rooms", return_value=_rooms()):
            detail = mcp_handlers.get_hotel_details_tool(selection_id, room_rates=True)
        self.assertEqual(detail["room_quotes"]["rates"][0]["total_price"], 545)
        self.assertTrue(mcp_handlers.verify_answer_tool("The room is USD 545.")["ok"])
        self.assertEqual(len(evidence._ledger), 2)
        self.assertNotIn("original_quote", evidence._ledger[-1])
        self.assertEqual(evidence._ledger[-1]["currency"], "USD")
        self.assertEqual(
            mcp_handlers.get_hotel_details_tool(selection_id)["original_quote"]["currency"], "EUR"
        )
        self.assertEqual(len(evidence._ledger), 2)

    def test_failed_or_rejected_room_quotes_do_not_enter_the_ledger(self):
        payload = mcp_handlers._owned(hotel(latitude=50.07, longitude=14.44))
        selection_id = payload["queries"][0]["offers"][0]["selection_id"]
        quotes = (
            _rooms(error=SearchError(SearchErrorCode.NO_RESULTS, "No rooms"), rates=()),
            _rooms(latitude=48.85, longitude=2.35),
            _rooms(answered_adults=1),
        )
        for quote in quotes:
            with patch("viajante.details.search_hotel_rooms", return_value=quote):
                mcp_handlers.get_hotel_details_tool(selection_id, room_rates=True)
            self.assertEqual(len(evidence._ledger), 1)
            self.assertFalse(evidence.verify_answer("The room is USD 545.")["ok"])

    def setUp(self):
        evidence.clear()
        mcp_handlers._CACHE.clear()
        self.addCleanup(evidence.clear)
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_deadline_cache_replay_keeps_hotel_reference_and_original_timestamp(self):
        @mcp_handlers._cached
        def search(*, deadline_seconds=None):
            return mcp_handlers._searched(hotel())

        first = search(deadline_seconds=30)
        selection_id = first["queries"][0]["offers"][0]["selection_id"]
        evidence.clear()
        replay = search(deadline_seconds=1)
        self.assertTrue(replay["cached"])
        self.assertEqual(replay["queries"][0]["offers"][0]["selection_id"], selection_id)
        detail = mcp_handlers.get_hotel_details_tool(selection_id)
        self.assertEqual(detail["original_quote"]["searched_at"], first["searched_at"])

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


def _rooms(**overrides):
    rate = HotelRoomRate("Triple", 545.0, 81.0, 58.0, 6, True, True, (), None)
    fields = dict(
        searched_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        hotel_id="25584",
        check_in=date(2099, 7, 1),
        check_out=date(2099, 7, 4),
        adults=5,
        rooms=2,
        currency="USD",
        name="Czech Inn",
        city="Prague",
        rates=(rate,),
        answered_adults=5,
        answered_rooms=2,
        answered_check_in=date(2099, 7, 1),
        answered_check_out=date(2099, 7, 4),
    )
    fields.update(overrides)
    return HotelRoomsReport(**fields)


class HotelDetailsEnvelopeTests(unittest.TestCase):
    def _quote(self, report, **hotel_kwargs):
        with patch("viajante.details.search_hotel_rooms", return_value=report) as rooms:
            result = get_hotel_details(hotel(**hotel_kwargs), 0, 0, room_rates=True)
        return result, rooms

    def test_stored_read_is_ok_and_complete(self):
        result = get_hotel_details(hotel(), 0, 0)
        self.assertEqual((result["status"], result["completeness"]), ("ok", "complete"))
        self.assertIsNone(result["room_quotes"])
        self.assertNotIn("echo", result)

    def test_matched_echo_with_rates_is_ok_and_complete(self):
        result, _rooms_fn = self._quote(_rooms())
        self.assertEqual(result["echo"], "matched")
        self.assertEqual(
            (result["status"], result["completeness"], result["empty_reason"]),
            ("ok", "complete", None),
        )
        self.assertEqual(result["room_quotes"]["currency"], "USD")

    def test_missing_echo_is_unknown_and_not_a_confident_match(self):
        result, _rooms_fn = self._quote(
            _rooms(
                answered_adults=None,
                answered_rooms=None,
                answered_check_in=None,
                answered_check_out=None,
            )
        )
        self.assertEqual(result["echo"], "unknown")
        self.assertIsNotNone(result["room_quotes"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["completeness"], "partial")
        self.assertNotEqual((result["status"], result["completeness"]), ("ok", "complete"))

    def test_provider_empty_room_quote(self):
        result, _rooms_fn = self._quote(_rooms(rates=()))
        self.assertEqual(
            (result["status"], result["empty_reason"], result["error_code"]),
            ("no_results", "provider_empty", None),
        )

    def test_provider_failures_keep_the_error_retry_fields(self):
        until = time.time() + 120
        cases = {
            "rate_limited": SearchError(
                SearchErrorCode.FETCH_FAILED, "limited", rate_limited=True, retry_until=until
            ),
            "blocked": SearchError(SearchErrorCode.BLOCKED, "wall"),
            "timeout": SearchError(SearchErrorCode.FETCH_FAILED, "slow", timeout=True),
            "failed": SearchError(SearchErrorCode.FETCH_FAILED, "nope"),
        }
        for status, error in cases.items():
            with self.subTest(status=status):
                result, _rooms_fn = self._quote(_rooms(rates=(), error=error))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["empty_reason"], "not_loaded")
                self.assertEqual(
                    result["retry_after"], result["room_quotes"]["error"].get("retry_after")
                )
                self.assertEqual(
                    result["retry_after_seconds"],
                    result["room_quotes"]["error"].get("retry_after_seconds"),
                )
                self.assertNotEqual((result["status"], result["completeness"]), ("ok", "complete"))

    def test_occupancy_or_dates_mismatch_is_filtered_out(self):
        party, _rooms_fn = self._quote(_rooms(answered_adults=1))
        self.assertEqual(
            (party["status"], party["empty_reason"], party["error_code"]),
            ("no_results", "filtered_out", "occupancy_mismatch"),
        )
        self.assertIsNone(party["room_quotes"])
        dates, _rooms_fn = self._quote(
            _rooms(answered_check_in=date(2099, 8, 1), answered_check_out=date(2099, 8, 4))
        )
        self.assertEqual(dates["error_code"], "dates_mismatch")
        self.assertIsNone(dates["room_quotes"])
        both, _rooms_fn = self._quote(
            _rooms(
                answered_adults=1,
                answered_check_in=date(2099, 8, 1),
                answered_check_out=date(2099, 8, 4),
            )
        )
        self.assertTrue(both["occupancy_mismatch"])
        self.assertTrue(both["dates_mismatch"])
        self.assertEqual(both["error_code"], "occupancy_mismatch")

    def test_inconclusive_reads_are_never_ok_and_complete(self):
        ambiguous = get_hotel_details(hotel(location="Springfield"), 0, 0, room_rates=True)
        self.assertNotEqual((ambiguous["status"], ambiguous["completeness"]), ("ok", "complete"))
        self.assertEqual(ambiguous["error_code"], "ambiguous_city")
