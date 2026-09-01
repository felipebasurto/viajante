from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from viajante.airports import airport_geo
from viajante.cli import main
from viajante.explore import search_explore
from viajante.flights import (
    _calendar_summary_from_source,
    _normalize_offer,
    parse_depart_window,
    parse_named_clock,
)
from viajante.google_flights import RawFlightCard, google_flights_url
from viajante.google_flights_rpc import (
    CompactCalendarDay,
    CompactExplorePlace,
    CompactParseMiss,
    build_explore_inner,
    build_shopping_inner,
    parse_explore_body,
)
from viajante.models import ExploreDestination, FlightQuery, RawJourneyLeg, RawLayover, RawSegment
from viajante.prompt_plan import plan_prompt
from viajante.typical import (
    typical_eur_from_daily_prices,
    vs_typical,
    vs_typical_pct,
    with_typical_dest,
)


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


def _overnight_card(
    *,
    city: str = "IST",
    inbound_arr: str | None = "22:00",
    outbound_dep: str | None = "08:00",
    hours: float | None = 10.0,
    price: str = "€28",
    origin: str = "MAD",
    dest: str = "OPO",
) -> RawFlightCard:
    return _card(
        stops="1 stop",
        layover_city=city,
        layover_hours=hours,
        duration="20 hr",
        price=price,
        legs=(
            RawJourneyLeg(
                departure="10:00",
                arrival="20:00",
                duration="20 hr",
                stops="1 stop",
                segments=(
                    RawSegment(
                        origin=origin, destination=city, departure="10:00", arrival=inbound_arr
                    ),
                    RawSegment(
                        origin=city, destination=dest, departure=outbound_dep, arrival="20:00"
                    ),
                ),
                layovers=(RawLayover(city=city, hours=hours),),
            ),
        ),
    )


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


class FakeExploreCalendarSource(FakeExploreSource):
    def __init__(
        self,
        places: tuple[CompactExplorePlace, ...] | Exception,
        prices: dict[str, tuple[RawFlightCard, ...]] | None = None,
        calendars: dict[str, tuple[CompactCalendarDay, ...] | Exception] | None = None,
    ) -> None:
        super().__init__(places, prices)
        self.calendars = calendars or {}
        self.calendar_calls: list[tuple[str, str, date, date]] = []

    def fetch_calendar(self, query: FlightQuery, start: date, end: date):
        self.calendar_calls.append((query.origin, query.destination, start, end))
        days = self.calendars.get(query.destination, ())
        if isinstance(days, Exception):
            raise days
        return days


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
        self.assertIsNone(report.destinations[0].typical_eur)
        self.assertIsNone(report.destinations[1].typical_eur)
        self.assertNotIn("typical_eur", report.destinations[0].to_dict())
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

    def test_named_no_overnight_drops_dests_whose_cheapest_shop_is_overnight(self) -> None:
        daytime = _overnight_card(
            city="IST",
            inbound_arr="12:00",
            outbound_dep="14:00",
            hours=2.0,
            price="€90",
            dest="OPO",
        )
        overnight = _overnight_card(
            city="IST",
            inbound_arr="22:00",
            outbound_dep="08:00",
            hours=10.0,
            price="€28",
            dest="LIS",
        )
        unknown = _card(stops="1 stop", layover_city="IST", layover_hours=18.0, price="€40")
        nonstop = _card(stops="Nonstop", price="€70")
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (daytime, overnight),
                "LIS": (overnight,),
                "FCO": (unknown, nonstop),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, source=source, no_overnight=("IST",)
        )
        self.assertEqual([row.iata for row in report.destinations], ["FCO", "OPO"])
        self.assertEqual(report.destinations[0].price_eur, 70.0)
        self.assertEqual(report.destinations[1].price_eur, 90.0)
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        by_iata = {row.iata: row.price_eur for row in unnamed.destinations}
        self.assertEqual(set(by_iata), {"OPO", "LIS", "FCO"})
        self.assertEqual(by_iata["OPO"], 28.0)
        self.assertEqual(by_iata["LIS"], 28.0)
        self.assertEqual(by_iata["FCO"], 40.0)

    def test_named_require_overnight_keeps_only_owned_overnight_dests(self) -> None:
        overnight = _overnight_card(
            city="IST",
            inbound_arr="22:00",
            outbound_dep="08:00",
            hours=10.0,
            price="€80",
            dest="OPO",
        )
        daytime = _overnight_card(
            city="IST",
            inbound_arr="12:00",
            outbound_dep="14:00",
            hours=2.0,
            price="€28",
            dest="LIS",
        )
        unknown = _card(stops="1 stop", layover_city="IST", layover_hours=18.0, price="€40")
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={"OPO": (overnight,), "LIS": (daytime,), "FCO": (unknown,)},
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, source=source, require_overnight=("IST",)
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO"])
        self.assertEqual(report.destinations[0].price_eur, 80.0)

    def test_named_overnight_contradiction_does_not_pick_one(self) -> None:
        overnight = _overnight_card(
            city="IST",
            inbound_arr="22:00",
            outbound_dep="08:00",
            hours=10.0,
            price="€28",
            dest="OPO",
        )
        daytime = _overnight_card(
            city="IST",
            inbound_arr="12:00",
            outbound_dep="14:00",
            hours=2.0,
            price="€90",
            dest="LIS",
        )
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={"OPO": (overnight,), "LIS": (daytime,)},
        )
        report = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            source=source,
            no_overnight=("IST",),
            require_overnight=("IST",),
        )
        self.assertEqual(report.destinations, ())

    def test_explore_catalog_places_stay_unfiltered_for_named_overnight(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={},
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=2, source=source, no_overnight=("any",)
        )
        self.assertEqual([row.iata for row in report.destinations], [])

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

    def test_unnamed_currency_follows_origin_country_usd(self) -> None:
        source = FakeExploreSource((CompactExplorePlace("OPO", "Porto", "Portugal"),))
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_explore("JFK", date(2026, 9, 1), days=7, top=1)
        ctor.assert_called_once_with(currency="USD", country=None)
        self.assertEqual(report.currency, "USD")

    def test_unknown_origin_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            search_explore("XXX", date(2026, 9, 1))


class ExploreSortTests(unittest.TestCase):
    def test_named_duration_sort_orders_by_owned_hours_not_fare(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={
                "OPO": (
                    _card(price="€40", duration="8 hr", departure="06:00", arrival="14:00"),
                    _card(price="€90", duration="1 hr", departure="09:00", arrival="10:00"),
                ),
                "FCO": (_card(price="€120", duration="2 hr", departure="08:00", arrival="10:00"),),
                "LIS": (_card(price="€50", duration="5 hr", departure="07:00", arrival="12:00"),),
            },
        )
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        self.assertEqual([row.iata for row in unnamed.destinations], ["OPO", "LIS", "FCO"])
        self.assertEqual(unnamed.destinations[0].price_eur, 40.0)
        self.assertEqual(unnamed.destinations[0].duration_hours, 8.0)
        self.assertNotEqual(unnamed.destinations[0].duration_hours, 1.0)
        ranked = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, sort="duration", source=source
        )
        self.assertEqual([row.iata for row in ranked.destinations], ["FCO", "LIS", "OPO"])
        self.assertEqual([row.duration_hours for row in ranked.destinations], [2.0, 5.0, 8.0])
        self.assertEqual(ranked.destinations[0].price_eur, 120.0)
        self.assertEqual(ranked.destinations[2].duration_hours, 8.0)

    def test_unnamed_sort_stays_cheapest_first(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("FCO", "Rome", "Italy"),
                CompactExplorePlace("OPO", "Porto", "Portugal"),
            ),
            prices={
                "FCO": (_card(price="€200", duration="1 hr"),),
                "OPO": (_card(price="€40", duration="8 hr"),),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        self.assertEqual([row.iata for row in report.destinations], ["OPO", "FCO"])
        priced = search_explore("MAD", date(2026, 9, 1), days=7, top=2, sort="price", source=source)
        self.assertEqual([row.iata for row in priced.destinations], ["OPO", "FCO"])
        fare = search_explore("MAD", date(2026, 9, 1), days=7, top=2, sort="fare", source=source)
        self.assertEqual([row.iata for row in fare.destinations], ["OPO", "FCO"])

    def test_missing_duration_is_not_given_made_up_hours(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (_card(price="€90", duration="2 hr"),),
                "LIS": (
                    _card(
                        price="€20",
                        duration=None,
                        departure=None,
                        arrival=None,
                    ),
                ),
            },
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=3, sort="duration", source=source
        )
        self.assertEqual([row.iata for row in report.destinations], ["OPO", "LIS", "FCO"])
        by_iata = {row.iata: row for row in report.destinations}
        self.assertEqual(by_iata["OPO"].duration_hours, 2.0)
        self.assertIsNone(by_iata["LIS"].duration_hours)
        self.assertNotIn("duration_hours", by_iata["LIS"].to_dict())
        self.assertIsNone(by_iata["FCO"].price_eur)
        self.assertIsNone(by_iata["FCO"].duration_hours)
        self.assertNotIn("duration_hours", by_iata["FCO"].to_dict())
        self.assertNotEqual(by_iata["LIS"].duration_hours, 0.0)
        self.assertNotEqual(by_iata["FCO"].duration_hours, 0.0)

    def test_departure_sort_uses_owned_clock_of_cheapest_offer(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={
                "OPO": (_card(price="€30", duration="2 hr", departure="18:00", arrival="20:00"),),
                "LIS": (_card(price="€80", duration="2 hr", departure="07:00", arrival="09:00"),),
            },
        )
        unnamed = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        self.assertEqual([row.iata for row in unnamed.destinations], ["OPO", "LIS"])
        ranked = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=2, sort="departure", source=source
        )
        self.assertEqual([row.iata for row in ranked.destinations], ["LIS", "OPO"])
        self.assertEqual(ranked.destinations[0].departure, "07:00")
        self.assertEqual(ranked.destinations[1].departure, "18:00")

    def test_unknown_sort_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            search_explore("MAD", date(2026, 9, 1), sort="fastest")  # type: ignore[arg-type]


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
        self.assertIn("--no-overnight", help_text)
        self.assertIn("--require-overnight", help_text)
        self.assertIn("--exclude-airports", help_text)
        self.assertIn("--include-airports", help_text)
        self.assertIn("--exclude-regions", help_text)
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
        self.assertIn("--sort", help_text)
        self.assertIn("--sort duration", help_text)
        self.assertIn("--baggage-buffer", help_text)

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
                    "--no-overnight",
                    "IST",
                    "--require-overnight",
                    "IST",
                    "--exclude-airports",
                    "HND",
                    "--include-airports",
                    "NRT,HND",
                    "--exclude-regions",
                    "asia",
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
        self.assertEqual(kwargs["no_overnight"], ("IST",))
        self.assertEqual(kwargs["require_overnight"], ("IST",))
        self.assertEqual(kwargs["exclude_airports"], ("HND",))
        self.assertEqual(kwargs["include_airports"], ("NRT", "HND"))
        self.assertEqual(kwargs["exclude_regions"], ("asia",))
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
        self.assertIsNone(kwargs["no_overnight"])
        self.assertIsNone(kwargs["require_overnight"])
        self.assertIsNone(kwargs["exclude_airports"])
        self.assertIsNone(kwargs["include_airports"])
        self.assertIsNone(kwargs["exclude_regions"])
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
        self.assertEqual(kwargs["sort"], "price")
        self.assertEqual(kwargs["buffer_eur"], 70)

    def test_explore_forwards_named_sort_duration(self) -> None:
        with (
            patch("viajante.cli.search_explore") as search,
            patch("viajante.cli._print_explore_report"),
        ):
            search.return_value = SimpleNamespace(error=None, destinations=())
            code = main(["explore", "MAD", "--from", "2026-09-01", "--sort", "duration"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["sort"], "duration")

    def test_explore_cli_prints_duration_sort_order(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (_card(price="€40", duration="8 hr"),),
                "FCO": (_card(price="€120", duration="2 hr"),),
            },
        )
        with patch("viajante.explore.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "explore",
                        "MAD",
                        "--from",
                        "2026-09-01",
                        "--days",
                        "7",
                        "--sort",
                        "duration",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertLess(output.index("FCO"), output.index("OPO"))
        self.assertIn("2h", output)
        self.assertIn("8h", output)

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


class ExcludeAirportsExploreTests(unittest.TestCase):
    def test_named_exclude_drops_hnd_unnamed_still_lists_it(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
            CompactExplorePlace("KIX", "Osaka", "Japan"),
        )
        prices = {
            "HND": (_card(price="€40"),),
            "NRT": (_card(price="€55"),),
            "KIX": (_card(price="€70"),),
        }
        named = FakeExploreSource(places, prices=prices)
        report = search_explore(
            "SIN", date(2026, 9, 15), days=7, top=3, exclude_airports=("HND",), source=named
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["NRT", "KIX"])
        self.assertNotIn("HND", iata)
        self.assertEqual([query.destination for query in named.fetched_queries], ["NRT", "KIX"])
        unnamed = FakeExploreSource(places, prices=prices)
        kept = search_explore("SIN", date(2026, 9, 15), days=7, top=3, source=unnamed)
        unnamed_iata = [row.iata for row in kept.destinations]
        self.assertEqual(unnamed_iata, ["HND", "NRT", "KIX"])
        self.assertEqual(
            [query.destination for query in unnamed.fetched_queries], ["HND", "NRT", "KIX"]
        )

    def test_named_exclude_does_not_invent_a_replacement_dest(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
        )
        source = FakeExploreSource(places, prices={"HND": (_card(price="€40"),)})
        report = search_explore(
            "SIN", date(2026, 9, 15), days=7, top=2, exclude_airports=("HND",), source=source
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["NRT"])
        self.assertIsNone(report.destinations[0].price_eur)
        self.assertNotIn("TYO", iata)
        self.assertNotIn("HND", iata)
        self.assertEqual([query.destination for query in source.fetched_queries], ["NRT"])

    def test_nearby_does_not_sneak_excluded_origin_back(self) -> None:
        places = (CompactExplorePlace("OPO", "Porto", "Portugal"),)
        prices = {"OPO": (_card(price="€28"),)}
        source = FakeExploreSource(places, prices=prices)
        reports = search_explore(
            "HND",
            date(2026, 9, 15),
            days=7,
            top=1,
            nearby=True,
            exclude_airports=("HND",),
            source=source,
        )
        rows = reports if isinstance(reports, tuple) else (reports,)
        origins = [row.origin for row in rows]
        self.assertNotIn("HND", origins)
        self.assertIn("NRT", origins)
        self.assertEqual(source.explore_origins, origins)
        named_only = FakeExploreSource(places, prices=prices)
        empty = search_explore(
            "HND", date(2026, 9, 15), days=7, top=1, exclude_airports=("HND",), source=named_only
        )
        self.assertEqual(empty.origin, "HND")
        self.assertEqual(empty.destinations, ())
        self.assertEqual(named_only.explore_origins, [])


class IncludeAirportsExploreTests(unittest.TestCase):
    def test_named_include_keeps_only_nrt_and_hnd(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
            CompactExplorePlace("KIX", "Osaka", "Japan"),
        )
        prices = {
            "HND": (_card(price="€40"),),
            "NRT": (_card(price="€55"),),
            "KIX": (_card(price="€70"),),
        }
        named = FakeExploreSource(places, prices=prices)
        report = search_explore(
            "SIN",
            date(2026, 9, 15),
            days=7,
            top=3,
            include_airports=("NRT", "HND"),
            source=named,
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["HND", "NRT"])
        self.assertNotIn("KIX", iata)
        self.assertEqual([query.destination for query in named.fetched_queries], ["HND", "NRT"])
        unnamed = FakeExploreSource(places, prices=prices)
        kept = search_explore("SIN", date(2026, 9, 15), days=7, top=3, source=unnamed)
        unnamed_iata = [row.iata for row in kept.destinations]
        self.assertEqual(unnamed_iata, ["HND", "NRT", "KIX"])
        self.assertEqual(
            [query.destination for query in unnamed.fetched_queries], ["HND", "NRT", "KIX"]
        )

    def test_named_include_empty_when_no_catalog_overlap(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
        )
        source = FakeExploreSource(places, prices={"HND": (_card(price="€40"),)})
        report = search_explore(
            "SIN",
            date(2026, 9, 15),
            days=7,
            top=3,
            include_airports=("LHR",),
            source=source,
        )
        self.assertEqual(report.destinations, ())
        self.assertEqual(source.fetched_queries, [])
        self.assertNotIn("LHR", [row.iata for row in report.destinations])
        self.assertNotIn("TYO", [row.iata for row in report.destinations])

    def test_exclude_wins_on_include_overlap(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
            CompactExplorePlace("KIX", "Osaka", "Japan"),
        )
        prices = {
            "HND": (_card(price="€40"),),
            "NRT": (_card(price="€55"),),
            "KIX": (_card(price="€70"),),
        }
        source = FakeExploreSource(places, prices=prices)
        report = search_explore(
            "SIN",
            date(2026, 9, 15),
            days=7,
            top=3,
            include_airports=("NRT", "HND"),
            exclude_airports=("HND",),
            source=source,
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["NRT"])
        self.assertNotIn("HND", iata)
        self.assertNotIn("KIX", iata)
        self.assertNotIn("TYO", iata)
        self.assertEqual([query.destination for query in source.fetched_queries], ["NRT"])


class ExcludeRegionsExploreTests(unittest.TestCase):
    def test_named_asia_drops_asia_tz_unnamed_keeps_them(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("LHR", "London", "United Kingdom"),
            CompactExplorePlace("AKL", "Auckland", "New Zealand"),
        )
        prices = {
            "HND": (_card(price="€40"),),
            "LHR": (_card(price="€55"),),
            "AKL": (_card(price="€70"),),
        }
        named = FakeExploreSource(places, prices=prices)
        report = search_explore(
            "NRT",
            date(2026, 9, 15),
            days=7,
            top=3,
            exclude_regions=("asia",),
            source=named,
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["LHR", "AKL"])
        self.assertNotIn("HND", iata)
        self.assertEqual(report.origin, "NRT")
        self.assertEqual([query.destination for query in named.fetched_queries], ["LHR", "AKL"])
        unnamed = FakeExploreSource(places, prices=prices)
        kept = search_explore("NRT", date(2026, 9, 15), days=7, top=3, source=unnamed)
        unnamed_iata = [row.iata for row in kept.destinations]
        self.assertEqual(unnamed_iata, ["HND", "LHR", "AKL"])
        self.assertEqual(
            [query.destination for query in unnamed.fetched_queries], ["HND", "LHR", "AKL"]
        )

    def test_unknown_tz_drops_under_named_filter(self) -> None:
        places = (
            CompactExplorePlace("OPO", "Porto", "Portugal"),
            CompactExplorePlace("LHR", "London", "United Kingdom"),
        )
        prices = {"OPO": (_card(price="€10"),), "LHR": (_card(price="€55"),)}
        real_geo = airport_geo

        def fake_geo(code: str):
            if code.strip().upper() == "OPO":
                return None
            return real_geo(code)

        with patch("viajante.airports.airport_geo", side_effect=fake_geo):
            named = FakeExploreSource(places, prices=prices)
            report = search_explore(
                "NRT",
                date(2026, 9, 15),
                days=7,
                top=3,
                exclude_regions=("asia",),
                source=named,
            )
            iata = [row.iata for row in report.destinations]
            self.assertEqual(iata, ["LHR"])
            self.assertNotIn("OPO", iata)
            self.assertEqual([query.destination for query in named.fetched_queries], ["LHR"])
        unnamed = FakeExploreSource(places, prices=prices)
        kept = search_explore("NRT", date(2026, 9, 15), days=7, top=3, source=unnamed)
        self.assertEqual([row.iata for row in kept.destinations], ["OPO", "LHR"])

    def test_empty_shortlist_does_not_invent_dests(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("ICN", "Seoul", "South Korea"),
        )
        source = FakeExploreSource(places, prices={"HND": (_card(price="€40"),)})
        report = search_explore(
            "NRT",
            date(2026, 9, 15),
            days=7,
            top=3,
            exclude_regions=("asia",),
            source=source,
        )
        self.assertEqual(report.origin, "NRT")
        self.assertEqual(report.destinations, ())
        self.assertEqual(source.fetched_queries, [])
        self.assertNotIn("LHR", [row.iata for row in report.destinations])
        self.assertNotIn("AKL", [row.iata for row in report.destinations])
        self.assertNotIn("SYD", [row.iata for row in report.destinations])

    def test_include_exclude_iata_still_win_then_region_is_additional(self) -> None:
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("NRT", "Tokyo", "Japan"),
            CompactExplorePlace("LHR", "London", "United Kingdom"),
        )
        prices = {
            "HND": (_card(price="€40"),),
            "NRT": (_card(price="€55"),),
            "LHR": (_card(price="€70"),),
        }
        source = FakeExploreSource(places, prices=prices)
        report = search_explore(
            "SIN",
            date(2026, 9, 15),
            days=7,
            top=3,
            include_airports=("NRT", "HND", "LHR"),
            exclude_airports=("HND",),
            exclude_regions=("asia",),
            source=source,
        )
        iata = [row.iata for row in report.destinations]
        self.assertEqual(iata, ["LHR"])
        self.assertNotIn("HND", iata)
        self.assertNotIn("NRT", iata)
        self.assertEqual([query.destination for query in source.fetched_queries], ["LHR"])

    def test_planner_not_asia_reaches_explore_filter(self) -> None:
        plan = plan_prompt("Destinations from NRT on 2026-09-15, not Asia")
        self.assertEqual(plan.intent, "explore")
        self.assertIn("asia", plan.exclude_regions)
        places = (
            CompactExplorePlace("HND", "Tokyo", "Japan"),
            CompactExplorePlace("LHR", "London", "United Kingdom"),
        )
        prices = {"HND": (_card(price="€40"),), "LHR": (_card(price="€55"),)}
        source = FakeExploreSource(places, prices=prices)
        report = search_explore(
            plan.origin or "NRT",
            plan.departure_date or date(2026, 9, 15),
            days=7,
            top=3,
            exclude_regions=plan.exclude_regions,
            source=source,
        )
        self.assertEqual([row.iata for row in report.destinations], ["LHR"])
        self.assertNotIn("HND", [row.iata for row in report.destinations])
        self.assertEqual([query.destination for query in source.fetched_queries], ["LHR"])


class TypicalExploreDestTests(unittest.TestCase):
    def test_shopped_dest_without_calendar_omits_typical(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={
                "OPO": (_card(price="€28"),),
                "LIS": (_card(price="€61"),),
                "FCO": (_card(price="€90"),),
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=3, source=source)
        self.assertEqual([row.iata for row in report.destinations], ["OPO", "LIS", "FCO"])
        mix = typical_eur_from_daily_prices([row.price_eur for row in report.destinations])
        self.assertEqual(mix, 61.0)
        for dest in report.destinations:
            self.assertIsNone(dest.typical_eur)
            self.assertIsNone(dest.vs_typical)
            self.assertIsNone(dest.vs_typical_pct)
            self.assertIsNone(dest.typical_deal())
            self.assertNotIn("typical_eur", dest.to_dict())
            self.assertNotEqual(dest.typical_eur, mix)

    def test_unnamed_still_lists_dests_without_typical(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={"OPO": (_card(price="€28"),)},
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        self.assertEqual([row.iata for row in report.destinations], ["OPO", "FCO"])
        self.assertEqual(report.destinations[0].price_eur, 28.0)
        self.assertIsNone(report.destinations[0].typical_eur)
        self.assertIsNone(report.destinations[1].price_eur)
        self.assertIsNone(report.destinations[1].typical_eur)
        self.assertNotIn("typical_eur", report.destinations[0].to_dict())

    def test_shopped_dest_stamps_the_same_triple_as_flights(self) -> None:
        days = (
            CompactCalendarDay(date(2026, 9, 1), 100.0),
            CompactCalendarDay(date(2026, 9, 2), 120.0),
            CompactCalendarDay(date(2026, 9, 3), 80.0),
        )
        source = FakeExploreCalendarSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(price="€80"),)},
            calendars={"OPO": days},
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=source)
        dest = report.destinations[0]
        self.assertEqual(dest.price_eur, 80.0)
        shop = FlightQuery("MAD", "OPO", date(2026, 9, 1))
        summary = _calendar_summary_from_source(source, shop, {})
        assert summary is not None
        self.assertEqual(summary.median_eur, typical_eur_from_daily_prices((100.0, 120.0, 80.0)))
        self.assertEqual(dest.typical_eur, summary.median_eur)
        self.assertEqual(dest.vs_typical, vs_typical(80.0, summary.median_eur))
        self.assertEqual(dest.vs_typical_pct, vs_typical_pct(80.0, summary.median_eur))
        self.assertEqual(dest.typical_deal(), "below typical 100 € (−20%)")
        stamped = with_typical_dest(dest, summary.median_eur)
        self.assertEqual(stamped.typical_eur, dest.typical_eur)
        self.assertEqual(stamped.vs_typical, dest.vs_typical)
        self.assertEqual(stamped.vs_typical_pct, dest.vs_typical_pct)
        self.assertTrue(any(call[1] == "OPO" for call in source.calendar_calls))

    def test_thin_or_missing_calendar_omits_typical(self) -> None:
        thin = FakeExploreCalendarSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(price="€80"),)},
            calendars={
                "OPO": (
                    CompactCalendarDay(date(2026, 9, 1), 80.0),
                    CompactCalendarDay(date(2026, 9, 2), 90.0),
                )
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=thin)
        self.assertEqual(report.destinations[0].price_eur, 80.0)
        self.assertIsNone(report.destinations[0].typical_eur)
        self.assertNotIn("typical_eur", report.destinations[0].to_dict())
        missed = FakeExploreCalendarSource(
            (CompactExplorePlace("LIS", "Lisbon", "Portugal"),),
            prices={"LIS": (_card(price="€61"),)},
            calendars={"LIS": CompactParseMiss("no wrb.fr calendar payload")},
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=1, source=missed)
        self.assertEqual(report.destinations[0].price_eur, 61.0)
        self.assertIsNone(report.destinations[0].typical_eur)
        self.assertNotIn("typical_eur", report.destinations[0].to_dict())

    def test_does_not_copy_typical_from_another_dest(self) -> None:
        source = FakeExploreCalendarSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("LIS", "Lisbon", "Portugal"),
            ),
            prices={
                "OPO": (_card(price="€80"),),
                "LIS": (_card(price="€61"),),
            },
            calendars={
                "OPO": (
                    CompactCalendarDay(date(2026, 9, 1), 100.0),
                    CompactCalendarDay(date(2026, 9, 2), 120.0),
                    CompactCalendarDay(date(2026, 9, 3), 80.0),
                )
            },
        )
        report = search_explore("MAD", date(2026, 9, 1), days=7, top=2, source=source)
        by_iata = {row.iata: row for row in report.destinations}
        self.assertEqual(by_iata["OPO"].typical_eur, 100.0)
        self.assertEqual(by_iata["OPO"].vs_typical, "below")
        self.assertIsNone(by_iata["LIS"].typical_eur)
        self.assertNotIn("typical_eur", by_iata["LIS"].to_dict())
        self.assertNotEqual(by_iata["LIS"].typical_eur, by_iata["OPO"].typical_eur)

    def test_explore_cli_prints_typical_deal_for_shopped_dest(self) -> None:
        source = FakeExploreCalendarSource(
            (CompactExplorePlace("OPO", "Porto", "Portugal"),),
            prices={"OPO": (_card(price="€80"),)},
            calendars={
                "OPO": (
                    CompactCalendarDay(date(2026, 9, 1), 100.0),
                    CompactCalendarDay(date(2026, 9, 2), 120.0),
                    CompactCalendarDay(date(2026, 9, 3), 80.0),
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
        self.assertIn("80 €", output)
        self.assertIn("below typical 100 € (−20%)", output)


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
        self.assertIsNone(catalog.typical_eur)
        self.assertNotIn("typical_eur", catalog.to_dict())
        self.assertIsNone(by_iata["FCO"].typical_eur)
        self.assertNotIn("typical_eur", by_iata["FCO"].to_dict())
        self.assertIsNone(by_iata["OPO"].typical_eur)
        self.assertNotIn("typical_eur", by_iata["OPO"].to_dict())

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


class ExploreBaggageBufferTests(unittest.TestCase):
    def test_ranked_buffer_reorders_shopped_dests_versus_zero(self) -> None:
        places = (
            CompactExplorePlace("OPO", "Porto", "Portugal"),
            CompactExplorePlace("LIS", "Lisbon", "Portugal"),
        )
        prices = {
            "OPO": (_card(airline="Ryanair", price="€40"),),
            "LIS": (_card(airline="Iberia", price="€90"),),
        }
        by_fare = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            source=FakeExploreSource(places, prices),
            buffer_eur=70,
        )
        self.assertEqual([row.iata for row in by_fare.destinations], ["OPO", "LIS"])
        self.assertEqual(by_fare.destinations[0].price_eur, 40.0)
        ranked_off = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            sort="ranked",
            buffer_eur=0,
            source=FakeExploreSource(places, prices),
        )
        self.assertEqual([row.iata for row in ranked_off.destinations], ["OPO", "LIS"])
        ranked = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            sort="ranked",
            buffer_eur=70,
            source=FakeExploreSource(places, prices),
        )
        self.assertEqual([row.iata for row in ranked.destinations], ["LIS", "OPO"])
        self.assertEqual(ranked.destinations[0].price_eur, 90.0)
        self.assertEqual(ranked.destinations[1].price_eur, 40.0)
        self.assertEqual(ranked.destinations[1].baggage_buffer_eur, 70)
        self.assertNotEqual(ranked.destinations[1].price_eur, 110.0)

    def test_unnamed_sort_ignores_buffer_and_unnamed_buffer_uses_default(self) -> None:
        places = (
            CompactExplorePlace("OPO", "Porto", "Portugal"),
            CompactExplorePlace("LIS", "Lisbon", "Portugal"),
        )
        prices = {
            "OPO": (_card(airline="Ryanair", price="€40"),),
            "LIS": (_card(airline="Iberia", price="€90"),),
        }
        unnamed = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=2, source=FakeExploreSource(places, prices)
        )
        self.assertEqual([row.iata for row in unnamed.destinations], ["OPO", "LIS"])
        default_ranked = search_explore(
            "MAD",
            date(2026, 9, 1),
            days=7,
            top=2,
            sort="ranked",
            source=FakeExploreSource(places, prices),
        )
        self.assertEqual([row.iata for row in default_ranked.destinations], ["LIS", "OPO"])

    def test_catalog_only_dest_omits_buffer_stamp(self) -> None:
        source = FakeExploreSource(
            (
                CompactExplorePlace("OPO", "Porto", "Portugal"),
                CompactExplorePlace("FCO", "Rome", "Italy"),
            ),
            prices={"OPO": (_card(airline="Ryanair", price="€40"),)},
        )
        report = search_explore(
            "MAD", date(2026, 9, 1), days=7, top=2, sort="ranked", source=source
        )
        by_iata = {row.iata: row for row in report.destinations}
        self.assertEqual(by_iata["OPO"].price_eur, 40.0)
        self.assertEqual(by_iata["OPO"].baggage_buffer_eur, 70)
        self.assertIsNone(by_iata["FCO"].price_eur)
        self.assertIsNone(by_iata["FCO"].baggage_buffer_eur)
        self.assertNotIn("baggage_buffer_eur", by_iata["FCO"].to_dict())
        self.assertNotIn("needs_bag_verify", by_iata["FCO"].to_dict())

    def test_cli_forwards_named_buffer(self) -> None:
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
                    "--sort",
                    "ranked",
                    "--baggage-buffer",
                    "0",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["buffer_eur"], 0)
        self.assertEqual(search.call_args.kwargs["sort"], "ranked")


if __name__ == "__main__":
    unittest.main()
