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
from viajante.flights import _normalize_offer
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


def _card(**kwargs: object) -> RawFlightCard:
    fields: dict[str, object] = {
        "airline": "Iberia",
        "departure": "08:00",
        "arrival": "09:20",
        "duration": "1 hr 20 min",
        "stops": "Nonstop",
        "price": "€90",
    }
    fields.update(kwargs)
    return RawFlightCard(**fields)  # type: ignore[arg-type]


class FakeExploreSource:
    def __init__(
        self,
        places: tuple[CompactExplorePlace, ...] | Exception,
        prices: dict[str, tuple[RawFlightCard, ...]] | None = None,
    ) -> None:
        self.places = places
        self.prices = prices or {}
        self.closed = False
        self.fetched_queries: list[FlightQuery] = []
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch_explore(self, origin, departure_date, *, adults=1, cabin="economy"):
        if isinstance(self.places, Exception):
            raise self.places
        return self.places

    def fetch(self, query: FlightQuery):
        self.fetched_queries.append(query)
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

    def test_named_bags_via_airlines_drop_dests_whose_surviving_fare_contradicts(self) -> None:
        silent = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€40",
        )
        too_few = _card(
            checked_bags=0,
            carry_on=1,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€28",
        )
        enough = _card(
            checked_bags=1,
            carry_on=1,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€55",
        )
        via_lis = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€80",
        )
        via_dxb = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="DXB",
            price="€60",
        )
        ryanair = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="LIS",
            price="€35",
        )
        over_cap = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€250",
        )
        extra = _card(
            checked_bags=1,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€70",
        )
        filters = dict(bags=1, carry_on=1, via=("LIS",), airlines=("IB",), price_cap_eur=200)
        self.assertIsNotNone(_normalize_offer(silent, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(too_few, 1, buffer_eur=0, **filters))
        self.assertIsNotNone(_normalize_offer(enough, 1, buffer_eur=0, **filters))
        self.assertIsNotNone(_normalize_offer(via_lis, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(via_dxb, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(ryanair, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(over_cap, 1, buffer_eur=0, **filters))
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
                CompactExplorePlace("BCN", "Barcelona", "Spain"),
            ),
            prices={
                "OPO": (silent, too_few, enough),
                "LIS": (via_lis, via_dxb, ryanair, over_cap),
                "FCO": (too_few, via_dxb, ryanair, over_cap),
                "BCN": (extra,),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source, **filters)
        self.assertEqual([row.iata for row in report.destinations], ["OPO", "LIS"])
        self.assertEqual(report.destinations[0].price_eur, 40.0)
        self.assertEqual(report.destinations[1].price_eur, 80.0)
        self.assertEqual(
            [query.destination for query in source.fetched_queries], ["OPO", "LIS", "FCO"]
        )
        self.assertEqual(source.fetched_queries[0].bags, 1)
        self.assertEqual(source.fetched_queries[0].carry_on, 1)
        self.assertEqual(source.fetched_queries[0].airlines, ("IB",))
        self.assertEqual(source.fetched_queries[0].price_cap_eur, 200)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 35.0)
        self.assertEqual(by_iata["FCO"], 28.0)
        self.assertNotIn("BCN", by_iata)
        self.assertIsNone(source.fetched_queries[-1].bags)

    def test_exclude_via_and_exclude_airlines_match_normalize(self) -> None:
        keep = _card(stops="1 stop", layover_city="OPO", airline_codes=("IB",), price="€100")
        drop_via = _card(stops="1 stop", layover_city="LIS", airline_codes=("IB",), price="€80")
        drop_airline = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="OPO",
            price="€70",
        )
        filters = dict(exclude_via=("LIS",), exclude_airlines=("FR",))
        self.assertIsNotNone(_normalize_offer(keep, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(drop_via, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(drop_airline, 1, buffer_eur=0, **filters))
        source = FakeExploreSource(
            (
                CompactExplorePlace("BCN", "Barcelona", "Spain"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "BCN": (keep, drop_via, drop_airline),
                "FCO": (drop_via, drop_airline),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source, **filters)
        self.assertEqual([row.iata for row in report.destinations], ["BCN"])
        self.assertEqual(report.destinations[0].price_eur, 100.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"BCN", "FCO"})
        self.assertEqual(by_iata["FCO"], 70.0)
        self.assertEqual(by_iata["BCN"], 70.0)

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
        help_text = buffer.getvalue()
        self.assertIn("viajante explore JFK", help_text)
        self.assertIn("--bags", help_text)
        self.assertIn("--via", help_text)
        self.assertIn("--airlines", help_text)
        self.assertIn("--price-cap", help_text)

    def test_explore_forwards_owned_shop_filters(self) -> None:
        with (
            patch("viajante.cli.search_explore") as search,
            patch("viajante.cli._print_explore_report"),
        ):
            search.return_value = SimpleNamespace(error=None, destinations=())
            code = main(
                [
                    "explore",
                    "MAD",
                    "--from",
                    "2026-09-01",
                    "--bags",
                    "1",
                    "--carry-on",
                    "--via",
                    "LIS",
                    "--exclude-via",
                    "DXB",
                    "--airlines",
                    "IB",
                    "--exclude-airlines",
                    "FR",
                    "--price-cap",
                    "200",
                ]
            )
        self.assertEqual(code, 0)
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["bags"], 1)
        self.assertEqual(kwargs["carry_on"], 1)
        self.assertEqual(kwargs["via"], ("LIS",))
        self.assertEqual(kwargs["exclude_via"], ("DXB",))
        self.assertEqual(kwargs["airlines"], ("IB",))
        self.assertEqual(kwargs["exclude_airlines"], ("FR",))
        self.assertEqual(kwargs["price_cap_eur"], 200)

    def test_explore_unnamed_shop_filters_stay_unset(self) -> None:
        with (
            patch("viajante.cli.search_explore") as search,
            patch("viajante.cli._print_explore_report"),
        ):
            search.return_value = SimpleNamespace(error=None, destinations=())
            code = main(["explore", "MAD", "--from", "2026-09-01"])
        self.assertEqual(code, 0)
        kwargs = search.call_args.kwargs
        self.assertIsNone(kwargs["bags"])
        self.assertIsNone(kwargs["carry_on"])
        self.assertIsNone(kwargs["via"])
        self.assertIsNone(kwargs["exclude_via"])
        self.assertIsNone(kwargs["airlines"])
        self.assertIsNone(kwargs["exclude_airlines"])
        self.assertIsNone(kwargs["price_cap_eur"])


if __name__ == "__main__":
    unittest.main()
