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
from viajante.flights import _normalize_offer, parse_depart_window, parse_named_clock
from viajante.google_flights import RawFlightCard, google_flights_url
from viajante.google_flights_rpc import (
    CompactExplorePlace,
    CompactParseMiss,
    build_explore_inner,
    build_shopping_inner,
    parse_explore_body,
)
from viajante.models import ExploreDestination, FlightQuery


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
        self.explore_origins: list[str] = []
        self.explore_occupancy: list[tuple[int, int, int, int]] = []
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch_explore(
        self,
        origin,
        departure_date,
        *,
        adults=1,
        cabin="economy",
        children=0,
        infants_in_seat=0,
        infants_on_lap=0,
    ):
        self.explore_origins.append(origin)
        self.explore_occupancy.append((adults, children, infants_in_seat, infants_on_lap))
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
        self.assertEqual(inner[3][6], [1, 0, 0, 0])
        named = build_explore_inner(
            "MAD",
            date(2026, 9, 1),
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertEqual(named[3][6], [2, 1, 1, 1])


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

    def test_named_depart_window_drops_dests_without_an_in_window_fare(self) -> None:
        morning = _card(departure="08:00", price="€90")
        evening = _card(departure="21:00", price="€28")
        silent = _card(departure=None, price="€40")
        window = parse_depart_window("7-12")
        self.assertIsNotNone(_normalize_offer(morning, 1, buffer_eur=0, depart_window=window))
        self.assertIsNone(_normalize_offer(evening, 1, buffer_eur=0, depart_window=window))
        self.assertIsNone(_normalize_offer(silent, 1, buffer_eur=0, depart_window=window))
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (morning, evening),
                "LIS": (evening, silent),
                "FCO": (silent,),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, source=source, depart_window=window
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO"])
        self.assertEqual(report.destinations[0].price_eur, 90.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 40.0)

    def test_named_arrive_before_drops_dests_without_an_on_time_fare(self) -> None:
        early = _card(arrival="09:00", price="€90")
        late = _card(arrival="23:00", price="€28")
        silent = _card(arrival=None, price="€40")
        bound = parse_named_clock("10:00", role="arrive-before")
        self.assertIsNotNone(_normalize_offer(early, 1, buffer_eur=0, arrive_before=bound))
        self.assertIsNone(_normalize_offer(late, 1, buffer_eur=0, arrive_before=bound))
        self.assertIsNone(_normalize_offer(silent, 1, buffer_eur=0, arrive_before=bound))
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (early, late),
                "LIS": (late, silent),
                "FCO": (silent,),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, source=source, arrive_before=bound
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO"])
        self.assertEqual(report.destinations[0].price_eur, 90.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 40.0)

    def test_named_depart_after_drops_dests_without_a_late_enough_fare(self) -> None:
        late = _card(departure="19:00", price="€90")
        early = _card(departure="08:00", price="€28")
        silent = _card(departure=None, price="€40")
        bound = parse_named_clock("18:00", role="depart-after")
        self.assertIsNotNone(_normalize_offer(late, 1, buffer_eur=0, depart_after=bound))
        self.assertIsNone(_normalize_offer(early, 1, buffer_eur=0, depart_after=bound))
        self.assertIsNone(_normalize_offer(silent, 1, buffer_eur=0, depart_after=bound))
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (late, early),
                "LIS": (early, silent),
                "FCO": (silent,),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, source=source, depart_after=bound
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO"])
        self.assertEqual(report.destinations[0].price_eur, 90.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 40.0)

    def test_named_layover_and_duration_drop_dests_without_an_eligible_shop(self) -> None:
        nonstop = _card(stops="Nonstop", duration="1 hr 20 min", price="€90")
        short_hop = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=2.0,
            duration="4 hr",
            price="€70",
        )
        overnight = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=18.0,
            duration="20 hr",
            price="€28",
        )
        silent = _card(stops="1 stop", layover_hours=None, duration="4 hr", price="€40")
        long_elapsed = _card(stops="Nonstop", duration="12 hr", price="€35")
        filters = dict(max_layover_hours=3.0, min_layover_hours=1.0, max_duration_hours=5.0)
        self.assertIsNotNone(_normalize_offer(nonstop, 1, buffer_eur=0, **filters))
        self.assertIsNotNone(_normalize_offer(short_hop, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(overnight, 1, buffer_eur=0, **filters))
        self.assertIsNotNone(_normalize_offer(silent, 1, buffer_eur=0, **filters))
        self.assertIsNone(_normalize_offer(long_elapsed, 1, buffer_eur=0, **filters))
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (nonstop, overnight),
                "LIS": (overnight, silent),
                "FCO": (overnight, long_elapsed),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source, **filters)
        self.assertEqual([row.iata for row in report.destinations], ["LIS", "OPO"])
        self.assertEqual(report.destinations[0].price_eur, 40.0)
        self.assertEqual(report.destinations[1].price_eur, 90.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 28.0)

    def test_named_alliance_rides_dest_shop_catalog_places_stay(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€61")
        ryanair = _card(airline="Ryanair", airline_codes=("FR",), price="€28")
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={"OPO": (ryanair,), "LIS": (iberia, ryanair), "FCO": (iberia,)},
        )
        report = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=3,
            source=source,
            alliances=("star",),
            exclude_alliances=("oneworld",),
        )
        self.assertEqual(source.explore_origins, ["MAD"])
        self.assertEqual(
            [query.destination for query in source.fetched_queries], ["OPO", "LIS", "FCO"]
        )
        shop = source.fetched_queries[0]
        self.assertEqual(shop.alliances, ("star",))
        self.assertEqual(shop.exclude_alliances, ("oneworld",))
        self.assertEqual(
            build_shopping_inner(shop)[1][13][0][7],
            [None, [["*A"]], [["*O"]]],
        )
        by_iata = {row.iata: row.price_eur for row in report.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 61.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        self.assertEqual({row.iata for row in unnamed.destinations}, {"OPO", "LIS", "FCO"})
        self.assertIsNone(source.fetched_queries[-1].alliances)
        self.assertIsNone(source.fetched_queries[-1].exclude_alliances)

    def test_named_occupancy_rides_dest_shop_unnamed_stays_default(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€61")
        ryanair = _card(airline="Ryanair", airline_codes=("FR",), price="€28")
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={"OPO": (ryanair,), "LIS": (iberia,)},
        )
        report = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            source=source,
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertEqual(source.explore_origins, ["MAD"])
        self.assertEqual(source.explore_occupancy, [(2, 1, 1, 1)])
        shop = source.fetched_queries[0]
        self.assertEqual(shop.adults, 2)
        self.assertEqual(shop.children, 1)
        self.assertEqual(shop.infants_in_seat, 1)
        self.assertEqual(shop.infants_on_lap, 1)
        self.assertEqual(build_shopping_inner(shop)[1][6], [2, 1, 1, 1])
        self.assertEqual({row.iata for row in report.destinations}, {"OPO", "LIS"})
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        self.assertEqual(source.explore_occupancy[-1], (1, 0, 0, 0))
        unnamed_shop = source.fetched_queries[-1]
        self.assertEqual(unnamed_shop.children, 0)
        self.assertEqual(unnamed_shop.infants_in_seat, 0)
        self.assertEqual(unnamed_shop.infants_on_lap, 0)
        self.assertEqual(build_shopping_inner(unnamed_shop)[1][6], [1, 0, 0, 0])
        self.assertEqual({row.iata for row in unnamed.destinations}, {"OPO", "LIS"})

    def test_named_currency_country_reach_http_source(self) -> None:
        source = FakeExploreSource((CompactExplorePlace("OPO", "Porto", "Portugal"),))
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_explore(
                "JFK",
                date(2026, 9, 1),
                days=7,
                top=1,
                currency="usd",
                country="us",
            )
        ctor.assert_called_once_with(currency="USD", country="US")
        self.assertEqual(report.currency, "USD")

    def test_unnamed_currency_stays_eur_country_omitted(self) -> None:
        source = FakeExploreSource((CompactExplorePlace("OPO", "Porto", "Portugal"),))
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_explore("JFK", date(2026, 9, 1), days=7, top=1)
        ctor.assert_called_once_with(currency="EUR", country=None)
        self.assertEqual(report.currency, "EUR")

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
        self.assertIn("--alliance", help_text)
        self.assertIn("--exclude-alliance", help_text)
        self.assertIn("--children", help_text)
        self.assertIn("--infants-in-seat", help_text)
        self.assertIn("--infants-on-lap", help_text)
        self.assertIn("--currency", help_text)
        self.assertIn("--country", help_text)
        self.assertIn("--price-cap", help_text)
        self.assertIn("--nearby", help_text)
        self.assertIn("--depart-window", help_text)
        self.assertIn("--arrive-before", help_text)
        self.assertIn("--depart-after", help_text)
        self.assertIn("--max-layover", help_text)
        self.assertIn("--min-layover", help_text)
        self.assertIn("--max-duration", help_text)

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
                    "--alliance",
                    "star",
                    "--exclude-alliance",
                    "oneworld",
                    "--price-cap",
                    "200",
                    "--depart-window",
                    "7-12",
                    "--arrive-before",
                    "10:00",
                    "--depart-after",
                    "18:00",
                    "--max-layover",
                    "3",
                    "--min-layover",
                    "1",
                    "--max-duration",
                    "8",
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
        self.assertEqual(kwargs["alliances"], ("star",))
        self.assertEqual(kwargs["exclude_alliances"], ("oneworld",))
        self.assertEqual(kwargs["price_cap_eur"], 200)
        self.assertEqual(kwargs["depart_window"], (7 * 60, 12 * 60 + 59))
        self.assertEqual(kwargs["arrive_before"], 10 * 60)
        self.assertEqual(kwargs["depart_after"], 18 * 60)
        self.assertEqual(kwargs["max_layover_hours"], 3)
        self.assertEqual(kwargs["min_layover_hours"], 1)
        self.assertEqual(kwargs["max_duration_hours"], 8)

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
        self.assertIsNone(kwargs["alliances"])
        self.assertIsNone(kwargs["exclude_alliances"])
        self.assertIsNone(kwargs["price_cap_eur"])
        self.assertIsNone(kwargs["depart_window"])
        self.assertIsNone(kwargs["arrive_before"])
        self.assertIsNone(kwargs["depart_after"])
        self.assertIsNone(kwargs["max_layover_hours"])
        self.assertIsNone(kwargs["min_layover_hours"])
        self.assertIsNone(kwargs["max_duration_hours"])
        self.assertFalse(kwargs["nearby"])
        self.assertEqual(kwargs["children"], 0)
        self.assertEqual(kwargs["infants_in_seat"], 0)
        self.assertEqual(kwargs["infants_on_lap"], 0)
        self.assertEqual(kwargs["currency"], "EUR")
        self.assertIsNone(kwargs["country"])

    def test_explore_forwards_named_occupancy(self) -> None:
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
                    "--adults",
                    "2",
                    "--children",
                    "1",
                    "--infants-in-seat",
                    "1",
                    "--infants-on-lap",
                    "1",
                ]
            )
        self.assertEqual(code, 0)
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["adults"], 2)
        self.assertEqual(kwargs["children"], 1)
        self.assertEqual(kwargs["infants_in_seat"], 1)
        self.assertEqual(kwargs["infants_on_lap"], 1)

    def test_explore_forwards_named_currency_country(self) -> None:
        with (
            patch("viajante.cli.search_explore") as search,
            patch("viajante.cli._print_explore_report"),
        ):
            search.return_value = SimpleNamespace(error=None, destinations=())
            code = main(
                [
                    "explore",
                    "JFK",
                    "--from",
                    "2026-09-01",
                    "--currency",
                    "usd",
                    "--country",
                    "us",
                ]
            )
        self.assertEqual(code, 0)
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["currency"], "USD")
        self.assertEqual(kwargs["country"], "US")


class NearbyExploreTests(unittest.TestCase):
    def test_nearby_expands_london_origin_and_default_keeps_heathrow(self) -> None:
        places = (CompactExplorePlace("OPO", "Porto", "Portugal"),)
        prices = {
            "OPO": (
                _card(
                    airline="Ryanair",
                    departure="07:00",
                    arrival="07:50",
                    duration="1 hr",
                    stops="Nonstop",
                    price="€28",
                ),
            )
        }
        off_source = FakeExploreSource(places, prices=prices)
        off = search_explore("LHR", date(2026, 9, 15), days=7, top=1, source=off_source)
        self.assertEqual(off.origin, "LHR")
        self.assertIsNone(off.nearby_label)
        self.assertEqual(off_source.explore_origins, ["LHR"])

        on_source = FakeExploreSource(places, prices=prices)
        reports = search_explore(
            "LHR", date(2026, 9, 15), days=7, top=1, nearby=True, source=on_source
        )
        self.assertIsInstance(reports, tuple)
        origins = [row.origin for row in reports]
        self.assertEqual(origins[0], "LHR")
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= set(origins))
        self.assertNotIn("BQH", origins)
        self.assertTrue(all(row.nearby_label for row in reports))
        self.assertNotIn("nearby_label", reports[0].to_dict())
        self.assertEqual(on_source.explore_origins, origins)

    def test_nearby_unknown_city_does_not_invent_codes(self) -> None:
        source = FakeExploreSource((CompactExplorePlace("OPO", "Porto", "Portugal"),))
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, nearby=True, source=source)
        self.assertEqual(report.origin, "MAD")
        self.assertIsNone(report.nearby_label)
        self.assertEqual(source.explore_origins, ["MAD"])

    def test_explore_nearby_flag_forwards(self) -> None:
        with (
            patch("viajante.cli.search_explore") as search,
            patch("viajante.cli._print_explore_report"),
        ):
            search.return_value = SimpleNamespace(error=None, destinations=())
            err = io.StringIO()
            with patch("sys.stderr", err):
                code = main(["explore", "LHR", "--from", "2026-09-15", "--nearby"])
        self.assertEqual(code, 0)
        self.assertTrue(search.call_args.kwargs["nearby"])
        self.assertIn("nearby London", err.getvalue())


class StopsCompareExploreShopTests(unittest.TestCase):
    def test_shopped_dest_stamps_compare_from_owned_offers(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={
                "OPO": (
                    _card(airline="Iberia", price="€88", stops="Nonstop"),
                    _card(
                        airline="Ryanair",
                        price="€49",
                        duration="5 hr",
                        stops="1 stop",
                        layover_city="OPO",
                    ),
                    _card(airline="Vueling", price="€120", stops="Nonstop"),
                ),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=source)
        dest = report.destinations[0]
        self.assertEqual(dest.price_eur, 49.0)
        compare = dest.stops_compare
        assert compare is not None
        assert compare.nonstop is not None
        assert compare.one_stop is not None
        self.assertEqual(compare.nonstop.price_eur, 88.0)
        self.assertEqual(compare.one_stop.price_eur, 49.0)
        self.assertEqual(compare.one_stop.layover_city, "OPO")
        payload = dest.to_dict()["stops_compare"]
        self.assertEqual(payload["nonstop"]["price_eur"], 88.0)
        self.assertEqual(payload["one_stop"]["price_eur"], 49.0)

    def test_catalog_place_without_shop_offers_omits_compare(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (_card(airline="Ryanair", price="€28", stops="Nonstop"),),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        by_iata = {row.iata: row for row in report.destinations}
        self.assertEqual(by_iata["OPO"].price_eur, 28.0)
        assert by_iata["OPO"].stops_compare is not None
        self.assertEqual(by_iata["OPO"].stops_compare.nonstop.price_eur, 28.0)
        self.assertIsNone(by_iata["OPO"].stops_compare.one_stop)
        self.assertIsNone(by_iata["FCO"].price_eur)
        self.assertIsNone(by_iata["FCO"].stops_compare)
        self.assertNotIn("stops_compare", by_iata["FCO"].to_dict())
        catalog = ExploreDestination(iata="LIS", city="Lisbon", country="Portugal", price_eur=61.0)
        self.assertIsNone(catalog.stops_compare)
        self.assertNotIn("stops_compare", catalog.to_dict())

    def test_omits_empty_side_and_block(self) -> None:
        only_one = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={
                "OPO": (
                    _card(
                        airline="Ryanair",
                        price="€49",
                        duration="5 hr",
                        stops="1 stop",
                    ),
                ),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=only_one)
        compare = report.destinations[0].stops_compare
        assert compare is not None
        self.assertIsNone(compare.nonstop)
        self.assertEqual(compare.one_stop.price_eur, 49.0)
        self.assertEqual(set(report.destinations[0].to_dict()["stops_compare"]), {"one_stop"})
        two_stop = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={
                "OPO": (
                    _card(
                        airline="China Southern",
                        price="€314",
                        duration="21 hr",
                        stops="2 stops",
                    ),
                ),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=1, max_stops=2, source=two_stop
        )
        dest = report.destinations[0]
        self.assertEqual(dest.price_eur, 314.0)
        self.assertIsNone(dest.stops_compare)
        self.assertNotIn("stops_compare", dest.to_dict())

    def test_explore_cli_prints_compare_for_shopped_dest(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={
                "OPO": (
                    _card(airline="Iberia", price="€88", stops="Nonstop"),
                    _card(airline="Ryanair", price="€49", duration="5 hr", stops="1 stop"),
                ),
            },
        )
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["explore", "MAD", "--from", "2026-09-01", "--days", "7"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Cheapest nonstop:", output)
        self.assertIn("Cheapest 1-stop:", output)
        self.assertIn("49 €", output)


class GoogleFlightsUrlShopParityTests(unittest.TestCase):
    def test_shopped_dest_stamps_owned_query_url(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(booking_token="tok"),)},
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=source)
        shop = FlightQuery("MAD", "OPO", date(2026, 9, 1), max_stops=1)
        expected = google_flights_url(shop, currency="EUR")
        dest = report.destinations[0]
        self.assertEqual(dest.google_flights_url, expected)
        self.assertEqual(dest.to_dict()["google_flights_url"], expected)
        self.assertNotIn("booking_token=", dest.google_flights_url or "")
        self.assertNotIn("booking_token", dest.to_dict())
        self.assertIsNone(report.google_flights_url)
        self.assertNotIn("google_flights_url", report.to_dict())

    def test_catalog_place_does_not_invent_a_token_or_url(self) -> None:
        catalog = ExploreDestination(iata="LIS", city="Lisbon", country="Portugal", price_eur=61.0)
        self.assertIsNone(catalog.google_flights_url)
        self.assertNotIn("google_flights_url", catalog.to_dict())
        self.assertNotIn("booking_token", catalog.to_dict())
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={"OPO": (_card(airline="Ryanair", price="€28", stops="Nonstop"),)},
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        by_iata = {row.iata: row for row in report.destinations}
        self.assertIsNotNone(by_iata["OPO"].google_flights_url)
        self.assertNotIn("booking_token=", by_iata["OPO"].google_flights_url or "")
        self.assertNotIn("booking_token", by_iata["FCO"].to_dict())
        self.assertNotIn("booking_token=", by_iata["FCO"].google_flights_url or "")

    def test_omits_url_when_encode_cannot_run(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(),)},
        )
        with patch("viajante.explore.google_flights_url", return_value=None):
            report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=source)
        self.assertIsNone(report.google_flights_url)
        self.assertNotIn("google_flights_url", report.to_dict())
        self.assertIsNone(report.destinations[0].google_flights_url)
        self.assertNotIn("google_flights_url", report.destinations[0].to_dict())

    def test_explore_cli_prints_dest_url(self) -> None:
        source = FakeExploreSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(),)},
        )
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["explore", "MAD", "--from", "2026-09-01", "--days", "7"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("https://www.google.com/travel/flights", output)
        self.assertIn("tfs=", output)
        self.assertNotIn("booking_token=", output)


if __name__ == "__main__":
    unittest.main()
