from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from viajante.cli import main
from viajante.explore import search_explore
from viajante.google_flights import RawFlightCard
from viajante.google_flights_rpc import (
    CompactExplorePlace,
    CompactParseMiss,
    build_explore_inner,
    parse_explore_body,
)
from viajante.models import FlightQuery


def _explore_row(iata: str, city: str, country: str) -> list[object]:
    row: list[object] = [None] * 29
    row[0] = "/m/x"
    row[1] = [0.0, 0.0]
    row[2] = city
    row[4] = country
    row[15] = iata
    return row


def _explore_body(*places: tuple[str, str, str]) -> str:
    dests = [_explore_row(*place) for place in places]
    data: list[object] = [None, None, None, [dests, None, None, []], None, None, None, None, None]
    wrb = [["wrb.fr", None, json.dumps(data, separators=(",", ":"))]]
    raw = json.dumps(wrb, separators=(",", ":"))
    return f")]}}'\n\n{len(raw)}\n{raw}"


class FakeExploreSource:
    def __init__(
        self,
        places: tuple[CompactExplorePlace, ...] | Exception,
        prices: dict[str, tuple[RawFlightCard, ...]] | None = None,
    ) -> None:
        self.places = places
        self.prices = prices or {}
        self.closed = False
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch_explore(self, origin, departure_date, *, adults=1, cabin="economy"):
        if isinstance(self.places, Exception):
            raise self.places
        return self.places

    def fetch(self, query: FlightQuery):
        return self.prices.get(query.destination, ())

    def close(self) -> None:
        self.closed = True


class ExploreParseTests(unittest.TestCase):
    def test_recorded_shape_yields_dest_rows(self) -> None:
        body = _explore_body(
            ("OPO", "Porto", "Portugal"),
            ("LIS", "Lisbon", "Portugal"),
            ("FCO", "Rome", "Italy"),
        )
        places = parse_explore_body(body)
        self.assertEqual([place.iata for place in places], ["OPO", "LIS", "FCO"])
        self.assertEqual(places[0].city, "Porto")
        self.assertEqual(places[2].country, "Italy")

    def test_unreadable_explore_is_a_miss(self) -> None:
        with self.assertRaises(CompactParseMiss):
            parse_explore_body("not explore")

    def test_explore_inner_clears_the_destination(self) -> None:
        inner = build_explore_inner("MAD", date(2026, 9, 1))
        self.assertEqual(inner[3][13][0][0], [[["MAD", 0]]])
        self.assertEqual(inner[3][13][0][1], [])


class ExploreSearchTests(unittest.TestCase):
    def test_prices_the_shortlist(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={
                "OPO": (
                    RawFlightCard(
                        airline="Ryanair",
                        departure="07:00",
                        arrival="07:50",
                        duration="1 hr",
                        stops="Nonstop",
                        price="€28",
                    ),
                ),
                "LIS": (
                    RawFlightCard(
                        airline="Iberia",
                        departure="09:00",
                        arrival="09:50",
                        duration="1 hr",
                        stops="Nonstop",
                        price="€61",
                    ),
                ),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        self.assertEqual(report.origin, "MAD")
        self.assertEqual(report.destinations[0].iata, "OPO")
        self.assertEqual(report.destinations[0].price_eur, 28.0)
        self.assertEqual(report.destinations[1].iata, "LIS")
        self.assertEqual(report.destinations[1].price_eur, 61.0)
        self.assertTrue(source.closed)

    def test_named_price_cap_drops_dests_without_an_owned_under_cap_fare(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (
                    RawFlightCard(
                        airline="Ryanair",
                        departure="07:00",
                        arrival="07:50",
                        duration="1 hr",
                        stops="Nonstop",
                        price="€28",
                    ),
                ),
                "LIS": (
                    RawFlightCard(
                        airline="Iberia",
                        departure="09:00",
                        arrival="09:50",
                        duration="1 hr",
                        stops="Nonstop",
                        price="€250",
                    ),
                ),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, price_cap_eur=200, source=source
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO"])
        self.assertEqual(report.destinations[0].price_eur, 28.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        self.assertEqual([row.iata for row in unnamed.destinations], ["OPO", "LIS", "FCO"])
        self.assertIsNone(unnamed.destinations[2].price_eur)

    def test_unknown_origin_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            search_explore("XXX", date(2026, 9, 1))


class ExploreCliTests(unittest.TestCase):
    def test_unknown_origin_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_explore") as search:
            code = main(["explore", "XXX", "--from", "2026-09-01"])
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_prints_dest_table(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={
                "OPO": (
                    RawFlightCard(
                        airline="Ryanair",
                        departure="07:00",
                        arrival="07:50",
                        duration="1 hr",
                        stops="Nonstop",
                        price="€28",
                    ),
                )
            },
        )
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["explore", "MAD", "--from", "2026-09-01", "--days", "7"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("OPO", output)
        self.assertIn("Porto", output)
        self.assertIn("28 €", output)

    def test_explore_help_has_examples(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["explore", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("viajante explore JFK", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
