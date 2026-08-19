from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from viajante.google_hotels import build_applied_filters
from viajante.google_hotels_rpc import (
    EmptyHotelResults,
    HotelsParseMiss,
    HotelsRejected,
    build_hotels_inner,
    build_hotels_request,
    parse_hotels_body,
)
from viajante.models import HotelQuery

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
        inner = build_hotels_inner(QUERY)
        self.assertEqual(inner[0], "Prague hotels")
        self.assertEqual(inner[2], [1, None, None, None, None, None, 13, None, 0])
        self.assertEqual(inner[1][0], 1)
        self.assertIsNone(inner[1][1])
        self.assertEqual(inner[1][4][0][3], 1)
        self.assertEqual(inner[1][4][0][4], 3)
        self.assertEqual(inner[1][4][0][6], "EUR")
        self.assertEqual(inner[1][2][1][1][2], 3)

    def test_entire_home_asks_for_vacation_rentals(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), entire_home=True)
        self.assertEqual(build_hotels_inner(query)[1][0], 2)

    def test_non_default_adults_emit_an_extras_block(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), adults=1)
        self.assertEqual(build_hotels_inner(query)[1][1], [[[3]], 1])

    def test_rooms_change_the_occupancy_block(self) -> None:
        dates = (date(2026, 12, 4), date(2026, 12, 7))
        default = HotelQuery("Prague", *dates, adults=2, rooms=1)
        one_adult = HotelQuery("Prague", *dates, adults=1, rooms=1)
        two_rooms = HotelQuery("Prague", *dates, adults=2, rooms=2)
        four_rooms = HotelQuery("Prague", *dates, adults=2, rooms=4)
        self.assertIsNone(build_hotels_inner(default)[1][1])
        self.assertEqual(build_hotels_inner(one_adult)[1][1], [[[3]], 1])
        self.assertEqual(build_hotels_inner(two_rooms)[1][1], [[[3], [3]], 2])
        self.assertEqual(build_hotels_inner(four_rooms)[1][1], [[[3], [3]], 4])
        self.assertNotEqual(
            build_hotels_inner(two_rooms)[1][1],
            build_hotels_inner(four_rooms)[1][1],
        )
        self.assertNotEqual(build_hotels_inner(two_rooms), build_hotels_inner(four_rooms))

    def test_request_url_is_batchexecute_with_eur(self) -> None:
        url, body = build_hotels_request(QUERY)
        parsed = urlparse(url)
        self.assertEqual(parsed.path, "/_/TravelFrontendUi/data/batchexecute")
        query = parse_qs(parsed.query)
        self.assertEqual(query["hl"], ["en"])
        self.assertEqual(query["curr"], ["EUR"])
        self.assertTrue(body.startswith("f.req="))
        envelope = json.loads(unquote(body[len("f.req=") :]))
        self.assertEqual(envelope[0][0][0], "AtySUc")


class HotelsParseTests(unittest.TestCase):
    def test_fixture_uses_the_stay_total_not_the_nightly(self) -> None:
        body = _wrap_wrb(_search_payload(_hotel_record()))
        cards = parse_hotels_body(body)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].title, "Plus Prague Hostel")
        self.assertEqual(cards[0].total_price, "€77")
        self.assertNotEqual(cards[0].total_price, "€14")
        self.assertEqual(cards[0].address, "Přívozní 1")
        self.assertEqual(cards[0].rating, "4")
        self.assertEqual(cards[0].link, "https://www.google.com/travel/clk/hi?qid=tok")

    def test_missing_stay_total_is_a_parse_miss(self) -> None:
        record = _hotel_record()
        record[6][2][9] = None
        with self.assertRaises(HotelsParseMiss):
            parse_hotels_body(_wrap_wrb(_search_payload(record)))

    def test_search_echo_without_hotels_is_empty(self) -> None:
        with self.assertRaises(EmptyHotelResults):
            parse_hotels_body(_wrap_wrb([[[[9, []]]], [1, "Prague hotels"]]))

    def test_null_wrb_payload_is_rejected(self) -> None:
        frame = json.dumps([["wrb.fr", "AtySUc", None, None, None, 1]], separators=(",", ":"))
        with self.assertRaises(HotelsRejected):
            parse_hotels_body(")]}'\n\n" + frame)

    def test_unknown_shell_is_a_parse_miss(self) -> None:
        with self.assertRaises(HotelsParseMiss):
            parse_hotels_body(_wrap_wrb(["not", "hotels"]))

    def test_closed_title_is_not_a_property(self) -> None:
        body = _wrap_wrb(
            _search_payload(
                _hotel_record(title="closed", stay_total="€122"),
                _hotel_record(title="Plus Prague Hostel", stay_total="€95"),
            )
        )
        cards = parse_hotels_body(body)
        self.assertEqual([card.title for card in cards], ["Plus Prague Hostel"])


class GoogleAppliedFiltersTests(unittest.TestCase):
    def test_default_chips_and_en_url(self) -> None:
        applied = build_applied_filters(QUERY)
        self.assertEqual(applied.chips, ("free_cancellation=1",))
        self.assertIn("hl=en", applied.url)
        self.assertIn("curr=EUR", applied.url)
        self.assertIn("travel/search", applied.url)

    def test_entire_home_chip(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7), entire_home=True)
        applied = build_applied_filters(query)
        self.assertIn("property_type=vacation_rentals", applied.chips)


class ModuleBoundaryTests(unittest.TestCase):
    def test_google_hotels_modules_do_not_import_playwright(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "viajante"
        for name in ("google_hotels.py", "google_hotels_rpc.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("playwright", text.casefold())


if __name__ == "__main__":
    unittest.main()
