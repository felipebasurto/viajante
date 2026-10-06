from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from random import Random
from urllib.parse import parse_qs, unquote, urlparse

from viajante.google_flights import SWEEP_TRANSPORT_STATUS, SweepHttpResponse
from viajante.google_hotels import GoogleHotelsSource, build_applied_filters
from viajante.google_hotels_rpc import (
    EmptyHotelResults,
    HotelsParseMiss,
    HotelsRejected,
    build_hotels_inner,
    build_hotels_request,
    parse_hotels_page,
)
from viajante.hotels import _run_search
from viajante.models import HotelQuery, HotelQuerySuccess

QUERY = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7))


def _wrap_wrb(payload: object, *, rpc: str = "AtySUc") -> str:
    inner = json.dumps(payload, separators=(",", ":"))
    frame = json.dumps([["wrb.fr", rpc, inner, None, None, 1]], separators=(",", ":"))
    return ")]}'\n\n" + frame


def _hotel_record(
    *,
    title: str = "Plus Prague Hostel",
    nightly: str = "€14",
    stay_total: str = "€77",
    address: str | None = "Přívozní 1",
    rating: float | None = 4.0,
) -> list:
    streets = [[[address]]] if address else None
    rating_block = [[rating, 10]] if rating is not None else None
    return [
        None,
        title,
        [[50.1, 14.4], streets],
        None,
        None,
        None,
        [
            None,
            None,
            [
                None,
                [nightly, "€26", 13.67, None, 14],
                None,
                None,
                ["/travel/clk/hi?qid=tok"],
                None,
                None,
                None,
                [[2026, 12, 4], [2026, 12, 7], 3, None, 0],
                ["€41", stay_total],
            ],
        ],
        rating_block,
        None,
        "0xredacted",
        None,
        ["Simple dorms."],
        None,
        1,
        None,
        None,
        None,
        None,
        None,
        None,
        "ChgIredacted",
    ]


def _search_payload(*records: list) -> list:
    entries = [[8, {"397419284": [record]}] for record in records]
    return [
        [[[9, entries]]],
        [1, "Prague hotels"],
    ]


class HotelsEncodeTests(unittest.TestCase):
    def test_request_meta_tail_is_present(self) -> None:
        inner = build_hotels_inner(QUERY, currency="CZK")
        self.assertEqual(inner[0], "Prague hotels")
        self.assertEqual(inner[2], [1, None, None, None, None, None, 13, None, 0])
        self.assertEqual(inner[1][0], 1)
        self.assertIsNone(inner[1][1])
        self.assertEqual(inner[1][4][0][3], 1)
        self.assertEqual(inner[1][4][0][4], 3)
        self.assertEqual(inner[1][4][0][6], "CZK")
        self.assertEqual(inner[1][2][1][1][2], 3)

    def test_entire_home_asks_for_vacation_rentals(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), entire_home=True)
        self.assertEqual(build_hotels_inner(query, currency="CZK")[1][0], 2)

    def test_non_default_adults_emit_an_extras_block(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), adults=1)
        self.assertEqual(build_hotels_inner(query, currency="CZK")[1][1], [[[3]], 1])

    def test_rooms_change_the_occupancy_block(self) -> None:
        dates = (date(2026, 12, 4), date(2026, 12, 7))
        default = HotelQuery("Prague", *dates, adults=2, rooms=1)
        one_adult = HotelQuery("Prague", *dates, adults=1, rooms=1)
        two_rooms = HotelQuery("Prague", *dates, adults=2, rooms=2)
        four_rooms = HotelQuery("Prague", *dates, adults=2, rooms=4)
        self.assertIsNone(build_hotels_inner(default, currency="CZK")[1][1])
        self.assertEqual(build_hotels_inner(one_adult, currency="CZK")[1][1], [[[3]], 1])
        self.assertEqual(build_hotels_inner(two_rooms, currency="CZK")[1][1], [[[3], [3]], 2])
        self.assertEqual(build_hotels_inner(four_rooms, currency="CZK")[1][1], [[[3], [3]], 4])
        self.assertNotEqual(
            build_hotels_inner(two_rooms, currency="CZK")[1][1],
            build_hotels_inner(four_rooms, currency="CZK")[1][1],
        )
        self.assertNotEqual(
            build_hotels_inner(two_rooms, currency="CZK"),
            build_hotels_inner(four_rooms, currency="CZK"),
        )

    def test_request_url_is_batchexecute_with_named_currency(self) -> None:
        url, body = build_hotels_request(QUERY, currency="CZK")
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/_/TravelFrontendUi/data/batchexecute")
        query = parse_qs(parsed.query)
        self.assertEqual(query["hl"], ["en"])
        self.assertEqual(query["curr"], ["CZK"])
        self.assertTrue(body.startswith("f.req="))
        envelope = json.loads(unquote(body[len("f.req=") :]))
        self.assertEqual(envelope[0][0][0], "AtySUc")


def _with_google_evidence(record: list) -> list:
    """Slots seen in a live AtySUc capture: type tags, class, occupancy echo, place, chips."""
    record = json.loads(json.dumps(record))
    record[2] += [None] * (32 - len(record[2]))
    record[2][31] = [[["gcid:hostel", True], ["gcid:hotel", False]]]
    record[3] = ["2-star hotel"]
    record[6][1] = record[6][1] or []
    stay = record[6][1]
    stay += [None] * (19 - len(stay))
    stay[13] = [5, None, 3]
    stay[16] = [[50.0, 14.3], [50.2, 14.6]]
    stay[18] = [None, "Prague", "0xredacted"]
    record[10] = [None, None, None, ["Essential info", [["Entire apartment"], ["Sleeps 5"]]]]
    return record


class HotelsEvidenceTests(unittest.TestCase):
    def test_page_carries_type_class_occupancy_and_resolved_place(self) -> None:
        body = _wrap_wrb(_search_payload(_with_google_evidence(_hotel_record())))
        page = parse_hotels_page(body)
        card = page.cards[0]
        self.assertEqual(card.place_types, ("hostel",))
        self.assertEqual(card.class_label, "2-star hotel")
        self.assertEqual(card.priced_adults, 5)
        self.assertEqual(page.resolved_place, "Prague")
        self.assertEqual(page.place_bounds, (50.0, 14.3, 50.2, 14.6))
        self.assertIn("Sleeps 5", card.details)

    def test_missing_slots_stay_none_not_guessed(self) -> None:
        page = parse_hotels_page(_wrap_wrb(_search_payload(_hotel_record())))
        card = page.cards[0]
        self.assertEqual(card.place_types, ())
        self.assertIsNone(card.class_label)
        self.assertIsNone(card.priced_adults)
        self.assertIsNone(page.resolved_place)
        self.assertIsNone(page.place_bounds)


class HotelsParseTests(unittest.TestCase):
    def test_fixture_uses_the_stay_total_not_the_nightly(self) -> None:
        body = _wrap_wrb(_search_payload(_hotel_record()))
        cards = parse_hotels_page(body).cards
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "Plus Prague Hostel")
        self.assertEqual(cards[0].total_price, "€77")
        self.assertNotEqual(cards[0].total_price, "€14")
        self.assertEqual(cards[0].address, "Přívozní 1")
        self.assertEqual(cards[0].rating, "4")
        self.assertEqual(cards[0].link, "https://www.google.com/travel/hotels/entity/ChgIredacted")

    def test_card_carries_owned_coordinates_and_review_count(self) -> None:
        card = parse_hotels_page(_wrap_wrb(_search_payload(_hotel_record()))).cards[0]
        self.assertEqual((card.latitude, card.longitude, card.review_count), (50.1, 14.4, 10))

    def test_missing_stay_total_is_a_parse_miss(self) -> None:
        record = _hotel_record()
        record[6][2][9] = None
        with self.assertRaises(HotelsParseMiss):
            parse_hotels_page(_wrap_wrb(_search_payload(record)))

    def test_single_element_stay_total_slot_parses(self) -> None:
        record = _hotel_record()
        record[6][2][9] = ["€71"]
        cards = parse_hotels_page(_wrap_wrb(_search_payload(record))).cards
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].total_price, "€71")

    def test_pair_slot_with_missing_total_does_not_fall_back(self) -> None:
        for slot in (["€14", None], ["€14", ""]):
            with self.subTest(slot=slot):
                record = _hotel_record()
                record[6][2][9] = slot
                with self.assertRaises(HotelsParseMiss):
                    parse_hotels_page(_wrap_wrb(_search_payload(record)))

    def test_search_echo_without_hotels_is_empty(self) -> None:
        with self.assertRaises(EmptyHotelResults):
            parse_hotels_page(_wrap_wrb([[[[9, []]]], [1, "Prague hotels"]]))

    def test_null_wrb_payload_is_rejected(self) -> None:
        frame = json.dumps([["wrb.fr", "AtySUc", None, None, None, 1]], separators=(",", ":"))
        with self.assertRaises(HotelsRejected):
            parse_hotels_page(")]}'\n\n" + frame)

    def test_unknown_shell_is_a_parse_miss(self) -> None:
        with self.assertRaises(HotelsParseMiss):
            parse_hotels_page(_wrap_wrb(["not", "hotels"]))

    def test_closed_title_is_not_a_property(self) -> None:
        body = _wrap_wrb(
            _search_payload(
                _hotel_record(title="closed", stay_total="€122"),
                _hotel_record(title="Plus Prague Hostel", stay_total="€95"),
            )
        )
        cards = parse_hotels_page(body).cards
        self.assertEqual([card.title for card in cards], ["Plus Prague Hostel"])


class _ScriptedHotelClient:
    def __init__(self, replies: list[object]) -> None:
        self._replies = list(replies)
        self.posts: list[str] = []

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: object,
        timeout: float,
    ) -> SweepHttpResponse:
        del data, headers, timeout
        self.posts.append(url)
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if not isinstance(reply, SweepHttpResponse):
            raise TypeError(f"unexpected scripted hotel reply: {type(reply)!r}")
        return reply

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        raise AssertionError("Google Hotels sweep posts only")

    def close(self) -> None:
        return None


class _TransportDownClient:
    """Multiplexed transport whose every request raises before a response."""

    def __init__(self) -> None:
        self.post_many_calls = 0
        self.posts = 0

    def post_many(self, jobs, *, timeout: float) -> list[SweepHttpResponse]:
        self.post_many_calls += 1
        self.posts += len(jobs)
        return [SweepHttpResponse(SWEEP_TRANSPORT_STATUS, "ConnectError: reset") for _ in jobs]

    def post(self, url: str, *, data: str, headers: object, timeout: float):
        raise AssertionError("a multiplexed search must not fall back to single posts")

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        raise AssertionError("Google Hotels sweep posts only")

    def close(self) -> None:
        return None


class GoogleHotelsFetchTests(unittest.TestCase):
    def test_multi_post_transport_failure_replays_once_with_no_backoff_ladder(self) -> None:
        rated = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), min_rating=4.5)
        client = _TransportDownClient()
        sleeps: list[float] = []
        report = _run_search(
            (rated,),
            top=1,
            source=GoogleHotelsSource(client=client, currency="CZK"),
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            provider="google-hotels",
            currency="CZK",
        )
        self.assertEqual((client.post_many_calls, client.posts), (2, 4))
        self.assertEqual(sleeps, [])
        failure = report.queries[0]
        self.assertEqual(failure.error.code.value, "fetch_failed")
        self.assertFalse(failure.error.rate_limited)

    def test_temporary_network_failure_can_retry(self) -> None:
        body = _wrap_wrb(_search_payload(_hotel_record()))
        client = _ScriptedHotelClient(
            [
                ConnectionError("temporary"),
                SweepHttpResponse(200, body, "https://www.google.com/travel/search"),
            ]
        )
        source = GoogleHotelsSource(client=client, currency="CZK")
        sleeps: list[float] = []
        report = _run_search(
            (QUERY,),
            top=1,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            provider="google-hotels",
            currency="EUR",
        )
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertIsInstance(report.queries[0], HotelQuerySuccess)

    def test_min_rating_adds_a_relevance_page_and_survives_its_miss(self) -> None:
        cheap = _wrap_wrb(_search_payload(_hotel_record(title="Cheap", rating=2.0)))
        good = _wrap_wrb(_search_payload(_hotel_record(title="Good", rating=4.6)))
        url = "https://www.google.com/travel/search"
        rated = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), min_rating=4.5)
        client = _ScriptedHotelClient([SweepHttpResponse(200, cheap, url)] * 2)
        GoogleHotelsSource(client=client, currency="CZK").fetch(
            QUERY, build_applied_filters(QUERY, currency="CZK"), 24
        )
        self.assertEqual(len(client.posts), 1)
        client = _ScriptedHotelClient(
            [SweepHttpResponse(200, cheap, url), SweepHttpResponse(200, good, url)]
        )
        page = GoogleHotelsSource(client=client, currency="CZK").fetch(
            rated, build_applied_filters(rated, currency="CZK"), 24
        )
        self.assertEqual([card.title for card in page.cards], ["Cheap", "Good"])
        client = _ScriptedHotelClient(
            [SweepHttpResponse(200, cheap, url), SweepHttpResponse(200, "junk", url)]
        )
        page = GoogleHotelsSource(client=client, currency="CZK").fetch(
            rated, build_applied_filters(rated, currency="CZK"), 24
        )
        self.assertEqual([card.title for card in page.cards], ["Cheap"])

    def test_relevance_page_leaves_the_sort_slot_empty(self) -> None:
        self.assertEqual(build_hotels_inner(QUERY, currency="CZK")[1][4][0][4], 3)
        self.assertIsNone(build_hotels_inner(QUERY, currency="CZK", sort=None)[1][4][0][4])


class GoogleAppliedFiltersTests(unittest.TestCase):
    def test_default_chips_and_en_url(self) -> None:
        applied = build_applied_filters(QUERY, currency="CZK")
        self.assertEqual(applied.chips, ("free_cancellation=1",))
        self.assertIn("hl=en", applied.url)
        self.assertIn("curr=CZK", applied.url)
        self.assertIn("travel/search", applied.url)

    def test_entire_home_chip(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), entire_home=True)
        applied = build_applied_filters(query, currency="CZK")
        self.assertIn("property_type=vacation_rentals", applied.chips)


class ModuleBoundaryTests(unittest.TestCase):
    def test_google_hotels_modules_do_not_import_playwright(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "viajante"
        for name in ("google_hotels.py", "google_hotels_rpc.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("playwright", text.casefold())


if __name__ == "__main__":
    unittest.main()
