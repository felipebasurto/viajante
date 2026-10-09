from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from random import Random
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.control import SearchDeadline
from viajante.hotels import _run_search, search_hotels
from viajante.models import (
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    SearchErrorCode,
)
from viajante.skiplagged_hotels import (
    SkiplaggedAmbiguousName,
    SkiplaggedHotelsSource,
    SkiplaggedNoHotels,
    SkiplaggedParseMiss,
    _normalized_name,
    build_applied_filters,
    parse_rooms_report,
    parse_search_page,
    resolve_hotel_id,
    search_hotel_rooms,
)

QUERY = HotelQuery("Prague", date(2026, 12, 1), date(2026, 12, 4), adults=3)
SEARCH_URL = (
    "https://skiplagged.com/hotels/37261/prague-czech-republic-hotels/2026-12-01/2026-12-04"
    "?utm_source=mcp"
)
TABLE = "\n".join(
    [
        "# Hotels in Prague",
        "",
        "| Hotel | Rating | Price/night | Total | Amenities | Booking |",
        "| --- | --- | --- | --- | --- | --- |",
        "| **a&o Prague Rhea**<br/>V Úžlabině 19 | 3★ · 7.6/10 | $32 | $1,117 | Free internet, "
        "Pets allowed | [View deal](https://skiplagged.com/hotel/47265/ao/2026-12-01) |",
        "| **Czech Inn**<br/>Francouzska 76 | 2★ · 8.2/10 | $56 | $190 | Kitchen(ette) | "
        "[View deal](https://skiplagged.com/hotel/25584/czech-inn/2026-12-01) |",
    ]
)


def _card(hotel_id: int, name: str, location: str, stars: int) -> dict:
    return {
        "type": "HotelCard",
        "id": f"hotel_{hotel_id}",
        "name": name,
        "rating": {"stars": stars, "text": f"{stars} stars"},
        "price": {"amount": 32.38, "currency": "USD", "text": "$32.38/night"},
        "location": location,
        "amenities": ["Free internet", "Pets allowed"],
        "deepLink": f"https://skiplagged.com/hotel/{hotel_id}/x/2026-12-01",
    }


def _search_result(table: str = TABLE, url: str = SEARCH_URL) -> dict:
    return {
        "content": [{"type": "text", "text": table}],
        "structuredContent": {
            "searchUrl": url,
            "results": [
                _card(47265, "a&o Prague Rhea", "V Úžlabině 19", 3),
                _card(25584, "Czech Inn", "Francouzska 76", 2),
            ],
        },
    }


def _details_result() -> dict:
    return {
        "content": [{"type": "text", "text": "# Czech Inn"}],
        "structuredContent": {
            "hotelId": "25584",
            "hotelName": "Czech Inn",
            "starRating": 2,
            "reviewRating": 8.2,
            "reviewCount": 1420,
            "address": "Francouzska 76",
            "cityName": "Prague",
            "bookingLink": "https://skiplagged.com/hotel/25584/czech-inn",
            "location": {"lat": 50.07, "lng": 14.44},
            "rooms": [
                {
                    "title": "Triple Deluxe",
                    "occupancyLimit": 6,
                    "pricePerNightInDollars": 81.12,
                    "totalPriceInDollars": 545.14,
                    "taxesAndFeesInDollars": 58.44,
                    "refundable": False,
                    "freeCancellation": False,
                    "bedTypes": [],
                    "bookingLink": "https://skiplagged.com/api/hotel_redirect.php?rate=x",
                },
                {
                    "title": "Triple Deluxe",
                    "occupancyLimit": 6,
                    "pricePerNightInDollars": 88.18,
                    "totalPriceInDollars": 592.52,
                    "taxesAndFeesInDollars": 63.42,
                    "refundable": True,
                    "freeCancellation": True,
                    "bedTypes": [],
                    "bookingLink": None,
                },
                {"title": "Broken", "totalPriceInDollars": None},
            ],
        },
    }


def _sse(payload: dict) -> str:
    return "event: message\ndata: " + json.dumps(payload) + "\n\n"


def _fake_rpc(call_result: dict, calls: list):
    def rpc(url, payload, headers):
        method = payload.get("method")
        if method == "initialize":
            return 200, {}, _sse({"jsonrpc": "2.0", "id": 1, "result": {}})
        if method == "notifications/initialized":
            return 202, {}, ""
        calls.append(payload["params"])
        return 200, {}, _sse({"jsonrpc": "2.0", "id": 2, "result": call_result})

    return rpc


class SkiplaggedSearchParseTests(unittest.TestCase):
    def test_cards_join_structured_rows_with_table_total_and_rating(self) -> None:
        page = parse_search_page(_search_result())
        first, second = page.cards
        self.assertEqual(
            (first.title, first.total_price, first.rating), ("a&o Prague Rhea", "$1,117", "7.6")
        )
        self.assertEqual((first.provider_id, first.class_label), ("47265", "3 stars"))
        self.assertEqual(first.details, "Free internet, Pets allowed")
        self.assertEqual(second.total_price, "$190")
        self.assertEqual(page.resolved_place, "prague-czech-republic")
        self.assertEqual(page.search_url, SEARCH_URL)

    def test_resolved_place_exposes_a_fuzzy_city_match(self) -> None:
        url = (
            "https://skiplagged.com/hotels/46717/costa-mesa-california-hotels/2026-12-01/2026-12-04"
        )
        page = parse_search_page(_search_result(url=url))
        self.assertEqual(page.resolved_place, "costa-mesa-california")

    def test_unmatched_city_is_no_hotels(self) -> None:
        result = {
            "content": [{"type": "text", "text": "No matching city found for the provided name."}],
            "isError": True,
        }
        with self.assertRaises(SkiplaggedNoHotels):
            parse_search_page(result)

    def test_table_that_matches_nothing_is_a_parse_miss_not_a_guess(self) -> None:
        with self.assertRaises(SkiplaggedParseMiss):
            parse_search_page(_search_result(table="# Hotels\n\nnothing tabular"))

    def test_row_without_a_total_is_dropped(self) -> None:
        table = TABLE.replace("| $190 |", "| — |")
        page = parse_search_page(_search_result(table=table))
        self.assertEqual([card.title for card in page.cards], ["a&o Prague Rhea"])


class SkiplaggedCapturedSearchTests(unittest.TestCase):
    """The live `sk_hotels_search` reply in fixtures/skiplagged/hotels_miami_search.json."""

    @staticmethod
    def _captured() -> dict:
        path = Path(__file__).parent / "fixtures" / "skiplagged" / "hotels_miami_search.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_total_comes_from_the_table_not_the_structured_nightly_rate(self) -> None:
        page = parse_search_page(self._captured())
        self.assertEqual(len(page.cards), 8)
        first = page.cards[0]
        self.assertEqual(first.title, "Beachside All Suites Hotel")
        self.assertEqual(first.total_price, "$112")  # structured price is $30.67/night
        self.assertEqual((first.rating, first.class_label), ("5.8", "3 stars"))
        self.assertEqual(first.provider_id, "926127")
        self.assertEqual(page.resolved_place, "miami-beach-florida")

    def test_card_priced_in_another_currency_is_dropped(self) -> None:
        result = self._captured()
        cards = result["structuredContent"]["results"]
        cards[0]["price"]["currency"] = "EUR"
        page = parse_search_page(result)
        self.assertNotIn("Beachside All Suites Hotel", [card.title for card in page.cards])
        self.assertEqual(len(page.cards), 7)
        for card in cards:
            card["price"]["currency"] = "EUR"
        with self.assertRaises(SkiplaggedParseMiss):
            parse_search_page(result)


class SkiplaggedAppliedFilterTests(unittest.TestCase):
    def test_free_cancellation_is_stamped_not_applied(self) -> None:
        applied = build_applied_filters(QUERY, currency="USD")
        self.assertEqual(applied.not_applied, ("free_cancellation",))
        self.assertEqual(applied.chips, ())
        self.assertEqual(applied.to_dict()["not_applied"], ["free_cancellation"])

    def test_nothing_is_stamped_when_non_refundable_stays_are_allowed(self) -> None:
        open_query = HotelQuery(
            "Prague", date(2026, 12, 1), date(2026, 12, 4), free_cancellation=False
        )
        self.assertEqual(build_applied_filters(open_query, currency="USD").not_applied, ())


class SkiplaggedSearchLoopTests(unittest.TestCase):
    def _run(self, source: SkiplaggedHotelsSource):
        return _run_search(
            (QUERY,),
            top=5,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(1),
            now=lambda: datetime(2026, 10, 2, tzinfo=timezone.utc),
            currency="USD",
            provider="skiplagged",
            applied_filters=build_applied_filters,
            delay_seconds=lambda _: 0.0,
            fetch_backend="skiplagged",
        )

    def test_success_carries_offers_ids_url_and_place(self) -> None:
        calls: list = []
        report = self._run(SkiplaggedHotelsSource(rpc=_fake_rpc(_search_result(), calls)))
        result = report.queries[0]
        self.assertIsInstance(result, HotelQuerySuccess)
        self.assertEqual([o.total_price for o in result.offers], [190.0, 1117.0])
        self.assertEqual(result.offers[0].provider_id, "25584")
        self.assertEqual(result.resolved_place, "prague-czech-republic")
        self.assertEqual(result.applied.url, SEARCH_URL)
        self.assertEqual(report.provider, "skiplagged")
        args = calls[0]["arguments"]
        self.assertEqual(calls[0]["name"], "sk_hotels_search")
        self.assertEqual((args["numAdults"], args["numRooms"], args["sort"]), (3, 1, "price"))
        self.assertEqual(args["checkin"], "2026-12-01")

    def test_unmatched_city_is_no_results_and_not_retried(self) -> None:
        calls: list = []
        miss = {"content": [{"type": "text", "text": "No matching city found."}], "isError": True}
        report = self._run(SkiplaggedHotelsSource(rpc=_fake_rpc(miss, calls)))
        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.NO_RESULTS)
        self.assertEqual(len(calls), 1)


class SkiplaggedSearchHotelsTests(unittest.TestCase):
    def test_other_currency_is_currency_mismatch_without_a_request(self) -> None:
        with patch("viajante.hotels.SkiplaggedHotelsSource") as source:
            report = search_hotels((QUERY,), source="skiplagged", currency="EUR")
        source.assert_not_called()
        error = report.queries[0].error
        self.assertEqual(error.code, SearchErrorCode.CURRENCY_MISMATCH)
        self.assertIn("does not convert", error.message)
        self.assertEqual(report.currency, "USD")

    def test_unnamed_currency_is_usd(self) -> None:
        calls: list = []
        source = SkiplaggedHotelsSource(rpc=_fake_rpc(_search_result(), calls))
        with patch("viajante.hotels.SkiplaggedHotelsSource", return_value=source):
            report = search_hotels((QUERY,), source="skiplagged")
        self.assertEqual(report.currency, "USD")
        self.assertIsInstance(report.queries[0], HotelQuerySuccess)

    def test_party_and_entire_home_limits_fail_before_any_request(self) -> None:
        big = HotelQuery("Prague", date(2026, 12, 1), date(2026, 12, 4), adults=11)
        home = HotelQuery("Prague", date(2026, 12, 1), date(2026, 12, 4), entire_home=True)
        with patch("viajante.hotels.SkiplaggedHotelsSource") as source:
            for query in (big, home):
                with self.subTest(query=query):
                    with self.assertRaises(ValueError):
                        search_hotels((query,), source="skiplagged")
        source.assert_not_called()


def _rate_limited_rpc(calls: list):
    def rpc(url, payload, headers):
        calls.append(payload.get("method"))
        return 429, {}, ""

    return rpc


class SkiplaggedRateLimitTests(unittest.TestCase):
    def test_search_429_is_blocked_rate_limited_and_not_retried(self) -> None:
        calls: list = []
        source = SkiplaggedHotelsSource(rpc=_rate_limited_rpc(calls))
        report = SkiplaggedSearchLoopTests()._run(source)
        error = report.queries[0].error
        self.assertEqual(error.code, SearchErrorCode.BLOCKED)
        self.assertTrue(error.rate_limited)
        self.assertEqual(calls, ["initialize"])

    def test_rooms_429_is_blocked_rate_limited_and_not_retried(self) -> None:
        calls: list = []
        report = search_hotel_rooms(
            1,
            date(2026, 12, 1),
            date(2026, 12, 4),
            rpc=_rate_limited_rpc(calls),
            sleep=lambda _: None,
        )
        self.assertEqual(report.error.code, SearchErrorCode.BLOCKED)
        self.assertTrue(report.error.rate_limited)
        self.assertEqual(calls, ["initialize"])

    def test_rooms_deadline_is_not_retried_or_reported_as_fetch_failure(self) -> None:
        calls: list = []
        sleeps: list = []

        def rpc(url, payload, headers):
            calls.append(payload.get("method"))
            raise SearchDeadline()

        report = search_hotel_rooms(
            1,
            date(2026, 12, 1),
            date(2026, 12, 4),
            rpc=rpc,
            sleep=sleeps.append,
        )
        self.assertEqual(report.error.code, SearchErrorCode.DEADLINE)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])


def _routing_rpc(search_result: dict, details_result: dict, calls: list):
    """Answers sk_hotels_search and sk_hotel_details with the matching fake result."""

    def rpc(url, payload, headers):
        method = payload.get("method")
        if method == "initialize":
            return 200, {}, _sse({"jsonrpc": "2.0", "id": 1, "result": {}})
        if method == "notifications/initialized":
            return 202, {}, ""
        calls.append(payload["params"])
        name = payload["params"]["name"]
        result = search_result if name == "sk_hotels_search" else details_result
        return 200, {}, _sse({"jsonrpc": "2.0", "id": 2, "result": result})

    return rpc


class SkiplaggedNameLookupTests(unittest.TestCase):
    DAY = (date(2026, 12, 1), date(2026, 12, 4))

    def test_city_prefix_cannot_resolve_to_a_different_catalogue_city(self) -> None:
        result = _search_result(
            url=(
                "https://skiplagged.com/hotels/1/"
                "san-jose-del-cabo-mexico-hotels/2026-12-01/2026-12-04"
            )
        )
        # Even a provider heading that echoes the shorter input city cannot
        # override a slug whose longer prefix is another owned city.
        result["content"][0]["text"] = result["content"][0]["text"].replace(
            "# Hotels in Prague", "# Hotels in San Jose"
        )
        with self.assertRaisesRegex(SkiplaggedNoHotels, "different city"):
            resolve_hotel_id(
                "Czech Inn",
                "San Jose, US",
                *self.DAY,
                adults=3,
                rpc=_routing_rpc(result, {}, []),
            )

    def test_city_country_slug_and_provider_heading_keep_the_owned_city(self) -> None:
        result = _search_result(
            url=(
                "https://skiplagged.com/hotels/1/prague-czech-republic-hotels/2026-12-01/2026-12-04"
            )
        )
        self.assertEqual(
            resolve_hotel_id(
                "Czech Inn",
                "Prague, CZ",
                *self.DAY,
                adults=3,
                rpc=_routing_rpc(result, {}, []),
            ),
            (25584, "Czech Inn"),
        )

    def test_explicit_provider_heading_country_cannot_contradict_the_request(self) -> None:
        result = _search_result()
        result["content"][0]["text"] = result["content"][0]["text"].replace(
            "# Hotels in Prague", "# Hotels in Prague, US"
        )
        with self.assertRaisesRegex(SkiplaggedNoHotels, "requested city was not searched"):
            resolve_hotel_id(
                "Czech Inn",
                "Prague, CZ",
                *self.DAY,
                adults=3,
                rpc=_routing_rpc(result, {}, []),
            )

    def test_non_latin_marks_remain_part_of_property_identity(self):
        for left, right in (("ガホテル", "カホテル"), ("होटल", "हटल")):
            with self.subTest(left=left, right=right):
                self.assertNotEqual(_normalized_name(left), _normalized_name(right))
        self.assertEqual(_normalized_name("ガホテル"), _normalized_name("カ\u3099ホテル"))
        self.assertEqual(_normalized_name("Hôtel"), _normalized_name("Hotel"))

    def test_unicode_property_names_match_only_the_same_name(self):
        result = _search_result()
        result["structuredContent"]["results"][1]["name"] = "東京ホテル"
        result["content"][0]["text"] = result["content"][0]["text"].replace(
            "Czech Inn", "東京ホテル"
        )
        rpc = _routing_rpc(result, {}, [])
        self.assertEqual(
            resolve_hotel_id("東京ホテル", "Prague", *self.DAY, adults=3, rpc=rpc),
            (25584, "東京ホテル"),
        )
        for other in ("大阪ホテル", "---"):
            with self.subTest(other=other), self.assertRaises(SkiplaggedNoHotels):
                resolve_hotel_id(other, "Prague", *self.DAY, adults=3, rpc=rpc)

    def test_exact_name_ignoring_case_accents_and_punctuation_resolves_one_id(self) -> None:
        rpc = _routing_rpc(_search_result(), {}, [])
        found = resolve_hotel_id("CZECH  inn!", "Prague", *self.DAY, adults=3, rpc=rpc)
        self.assertEqual(found, (25584, "Czech Inn"))

    def test_no_match_lists_close_names_and_does_not_guess(self) -> None:
        rpc = _routing_rpc(_search_result(), {}, [])
        with self.assertRaises(SkiplaggedNoHotels) as ctx:
            resolve_hotel_id("Czech Inne Hostel", "Prague", *self.DAY, adults=3, rpc=rpc)
        message = str(ctx.exception)
        self.assertIn("Closest returned: Czech Inn", message)
        self.assertIn("Nothing was guessed", message)

    def test_two_hotels_with_one_name_ask_for_the_id(self) -> None:
        result = _search_result()
        result["structuredContent"]["results"].append(_card(99, "CZECH INN", "Elsewhere 1", 2))
        result["content"][0]["text"] += (
            "\n| **CZECH INN**<br/>Elsewhere 1 | 2★ · 7.0/10 | $40 | $120 | — | "
            "[View deal](https://skiplagged.com/hotel/99/x/2026-12-01) |"
        )
        with self.assertRaises(SkiplaggedAmbiguousName) as ctx:
            resolve_hotel_id(
                "Czech Inn", "Prague", *self.DAY, adults=3, rpc=_routing_rpc(result, {}, [])
            )
        self.assertIn("25584", str(ctx.exception))
        self.assertIn("99", str(ctx.exception))

    def test_a_tool_error_reply_is_settled_and_not_retried(self) -> None:
        calls: list = []
        rejected = {"isError": True, "content": [{"type": "text", "text": "hotel 99 is delisted"}]}
        sleeps: list = []
        report = search_hotel_rooms(
            99,
            *self.DAY,
            rpc=_routing_rpc(_search_result(), rejected, calls),
            sleep=sleeps.append,
        )
        self.assertEqual([params["name"] for params in calls], ["sk_hotel_details"])
        self.assertEqual(report.error.code, SearchErrorCode.REJECTED)
        self.assertEqual(sleeps, [])

    def test_search_hotel_rooms_by_name_resolves_then_fetches_the_rates(self) -> None:
        calls: list = []
        report = search_hotel_rooms(
            None,
            *self.DAY,
            hotel_name="Czech Inn",
            city="Prague",
            adults=5,
            rpc=_routing_rpc(_search_result(), _details_result(), calls),
            sleep=lambda _: None,
        )
        self.assertIsNone(report.error)
        self.assertEqual([c["name"] for c in calls], ["sk_hotels_search", "sk_hotel_details"])
        self.assertEqual(calls[1]["arguments"]["hotelId"], 25584)
        self.assertEqual((report.hotel_id, report.requested_name), ("25584", "Czech Inn"))

    def test_unknown_name_is_a_typed_no_results_and_never_asks_for_details(self) -> None:
        calls: list = []
        report = search_hotel_rooms(
            None,
            *self.DAY,
            hotel_name="Nowhere Lodge",
            city="Prague",
            rpc=_routing_rpc(_search_result(), _details_result(), calls),
            sleep=lambda _: None,
        )
        self.assertEqual(report.error.code, SearchErrorCode.NO_RESULTS)
        self.assertIsNone(report.hotel_id)
        self.assertEqual([c["name"] for c in calls], ["sk_hotels_search"])

    def test_naming_rules_fail_before_any_request(self) -> None:
        for kwargs in (
            {"hotel_id": None},
            {"hotel_id": None, "hotel_name": "Czech Inn"},
            {"hotel_id": 1, "hotel_name": "Czech Inn", "city": "Prague"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    search_hotel_rooms(
                        kwargs.pop("hotel_id"), *self.DAY, rpc=_fake_rpc({}, []), **kwargs
                    )


class SkiplaggedRoomsTests(unittest.TestCase):
    def test_rates_keep_occupancy_and_refund_flags_in_provider_order(self) -> None:
        report = parse_rooms_report(
            _details_result(),
            hotel_id="25584",
            check_in=date(2026, 12, 1),
            check_out=date(2026, 12, 4),
            adults=5,
            rooms=2,
            searched_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(report.currency, "USD")
        self.assertEqual((report.name, report.review_count), ("Czech Inn", 1420))
        self.assertEqual((report.latitude, report.longitude), (50.07, 14.44))
        first, second = report.rates
        self.assertEqual(
            (first.occupancy_limit, first.refundable, first.free_cancellation), (6, False, False)
        )
        self.assertEqual(
            (second.total_price, second.refundable, second.free_cancellation), (592.52, True, True)
        )
        self.assertEqual(len(report.rates), 2)
        keys = set(report.to_dict())
        self.assertTrue({"rates", "currency", "hotel_id", "price_basis", "error"} <= keys)

    def test_search_hotel_rooms_sends_the_party_and_returns_the_report(self) -> None:
        calls: list = []
        report = search_hotel_rooms(
            25584,
            date(2026, 12, 1),
            date(2026, 12, 4),
            adults=5,
            rooms=2,
            rpc=_fake_rpc(_details_result(), calls),
            sleep=lambda _: None,
        )
        self.assertIsNone(report.error)
        self.assertEqual(calls[0]["name"], "sk_hotel_details")
        self.assertEqual(calls[0]["arguments"]["numAdults"], 5)
        self.assertEqual(calls[0]["arguments"]["hotelId"], 25584)
        self.assertEqual(len(report.rates), 2)

    def test_no_rates_is_a_typed_no_results_not_an_empty_success(self) -> None:
        empty = {"content": [], "structuredContent": {"rooms": []}}
        report = search_hotel_rooms(
            1,
            date(2026, 12, 1),
            date(2026, 12, 4),
            rpc=_fake_rpc(empty, []),
            sleep=lambda _: None,
        )
        self.assertEqual(report.error.code, SearchErrorCode.NO_RESULTS)
        self.assertEqual(report.rates, ())

    def test_limits_fail_before_any_request(self) -> None:
        for kwargs in ({"adults": 11}, {"rooms": 6}, {"adults": 0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    search_hotel_rooms(1, date(2026, 12, 1), date(2026, 12, 4), **kwargs)


if __name__ == "__main__":
    unittest.main()
