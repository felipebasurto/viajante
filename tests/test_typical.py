from __future__ import annotations

import unittest
from datetime import date, datetime
from random import Random
from types import SimpleNamespace
from typing import Sequence

from viajante.dates import MAX_DATE_WINDOW_DAYS
from viajante.flights import _run_search
from viajante.google_flights import NoFlightsFound, RawFlightCard
from viajante.google_flights_rpc import CompactCalendarDay, CompactParseMiss
from viajante.models import (
    FlightLeg,
    FlightOffer,
    FlightQuery,
    MultiCity,
    QuerySuccess,
    RoundTrip,
)
from viajante.typical import (
    MIN_DAILY_PRICES,
    NEAR_RATIO,
    TYPICAL_WINDOW_DAYS,
    typical_eur_from_daily_prices,
    vs_typical,
    vs_typical_pct,
    with_typical,
)


def card(*, airline: str = "Air", price: str = "99 €") -> RawFlightCard:
    return RawFlightCard(
        airline=airline,
        departure="08:00",
        arrival="09:00",
        price=price,
        duration="1 h",
        stops="Nonstop",
    )


class FakeSource:
    def __init__(self, responses: dict[tuple, object]) -> None:
        self.responses = responses
        self.fetch_calls = 0
        self.reset_calls = 0
        self.closed = False
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(self, query) -> Sequence[RawFlightCard]:
        self.fetch_calls += 1
        if isinstance(query, FlightQuery):
            key = (
                query.origin,
                query.destination,
                query.departure_date.isoformat(),
                query.max_stops,
            )
        else:
            first = query.legs[0]
            key = (
                first.origin,
                first.destination,
                first.departure_date.isoformat(),
                first.max_stops,
            )
        response = self.responses.get(key)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise RuntimeError("missing fake response")
        return response

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.closed = True


def _offer(*, price_eur: float = 100.0) -> FlightOffer:
    return FlightOffer(
        airline="Example Air",
        departure="08:00",
        arrival="10:00",
        price=f"€{price_eur:.0f}",
        price_eur=price_eur,
        duration="2 hr",
        duration_hours=2.0,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer_eur=0,
        needs_bag_verify=False,
    )


class FakeCalendarFlightSource(FakeSource):
    def __init__(
        self,
        responses: dict[tuple, object],
        days: tuple[CompactCalendarDay, ...] | Exception,
    ) -> None:
        super().__init__(responses)
        self.days = days
        self.calendar_calls: list[tuple[str, str, date, date, int, int, str]] = []

    def fetch_calendar(self, query, start: date, end: date):
        nights = None
        if isinstance(query, RoundTrip):
            nights = (query.return_date - query.departure_date).days
        self.calendar_calls.append(
            (
                query.origin,
                query.destination,
                start,
                end,
                query.max_stops,
                query.adults,
                query.cabin,
                nights,
            )
        )
        if isinstance(self.days, Exception):
            raise self.days
        return self.days


class TypicalFromDailyPricesTests(unittest.TestCase):
    def test_window_cap_matches_the_dates_calendar(self) -> None:
        self.assertEqual(TYPICAL_WINDOW_DAYS, MAX_DATE_WINDOW_DAYS)
        self.assertEqual(MIN_DAILY_PRICES, 3)
        self.assertEqual(NEAR_RATIO, 0.10)

    def test_median_of_owned_daily_prices(self) -> None:
        self.assertEqual(
            typical_eur_from_daily_prices((300.0, 340.0, 360.0, 400.0, 420.0)),
            360.0,
        )
        self.assertEqual(typical_eur_from_daily_prices((81.0, 67.0, 81.0)), 81.0)
        self.assertEqual(typical_eur_from_daily_prices((10.0, 20.0, 30.0, 40.0)), 25.0)

    def test_calendar_fixture_days_are_enough_for_a_median(self) -> None:
        prices = (81.0, 81.0, 67.0, None)
        self.assertEqual(typical_eur_from_daily_prices(prices), 81.0)

    def test_missing_or_thin_lists_are_not_invented(self) -> None:
        self.assertIsNone(typical_eur_from_daily_prices(()))
        self.assertIsNone(typical_eur_from_daily_prices((None, None)))
        self.assertIsNone(typical_eur_from_daily_prices((120.0,)))
        self.assertIsNone(typical_eur_from_daily_prices((120.0, 80.0)))
        self.assertIsNone(typical_eur_from_daily_prices((0.0, -5.0, 40.0, 50.0)))
        self.assertIsNone(typical_eur_from_daily_prices((None, 40.0, None, 50.0)))


class VsTypicalTests(unittest.TestCase):
    def test_coarse_band_is_ten_percent(self) -> None:
        self.assertEqual(vs_typical(89.0, 100.0), "below")
        self.assertEqual(vs_typical(90.0, 100.0), "near")
        self.assertEqual(vs_typical(100.0, 100.0), "near")
        self.assertEqual(vs_typical(110.0, 100.0), "near")
        self.assertEqual(vs_typical(111.0, 100.0), "above")

    def test_no_typical_means_no_label(self) -> None:
        self.assertIsNone(vs_typical(50.0, None))
        self.assertIsNone(vs_typical(50.0, 0.0))
        self.assertIsNone(vs_typical_pct(50.0, None))
        self.assertIsNone(vs_typical_pct(50.0, 0.0))

    def test_signed_percent_is_fare_versus_median(self) -> None:
        self.assertEqual(vs_typical_pct(289.0, 340.0), -15)
        self.assertEqual(vs_typical_pct(340.0, 340.0), 0)
        self.assertEqual(vs_typical_pct(400.0, 340.0), 18)

    def test_offer_stamp_is_all_or_nothing(self) -> None:
        stamped = with_typical(_offer(price_eur=289.0), 340.0)
        self.assertEqual(stamped.typical_eur, 340.0)
        self.assertEqual(stamped.vs_typical, "below")
        self.assertEqual(stamped.vs_typical_pct, -15)
        self.assertEqual(stamped.typical_deal(), "below typical 340 € (−15%)")
        omitted = with_typical(_offer(price_eur=289.0), None)
        self.assertIsNone(omitted.typical_eur)
        self.assertIsNone(omitted.vs_typical)
        self.assertIsNone(omitted.vs_typical_pct)
        self.assertIsNone(omitted.typical_deal())
        self.assertIsNone(stamped.cheapest_date)
        self.assertIsNone(omitted.cheapest_date)


class TypicalSearchTests(unittest.TestCase):
    def test_one_way_offer_uses_same_route_calendar_median(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-09-15", 1): (card(airline="Norse Atlantic", price="289 €"),)},
            (
                CompactCalendarDay(date(2026, 9, 15), 300.0),
                CompactCalendarDay(date(2026, 9, 16), 340.0),
                CompactCalendarDay(date(2026, 9, 17), 360.0),
                CompactCalendarDay(date(2026, 9, 18), 400.0),
                CompactCalendarDay(date(2026, 9, 19), 420.0),
            ),
        )
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(len(source.calendar_calls), 1)
        origin, dest, start, end, *_rest = source.calendar_calls[0]
        self.assertEqual(origin, "JFK")
        self.assertEqual(dest, "LHR")
        self.assertEqual(start, date(2026, 9, 15))
        self.assertEqual(end, date(2026, 10, 15))
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.price_eur, 289.0)
        self.assertEqual(offer.typical_eur, 360.0)
        self.assertEqual(offer.vs_typical, "below")
        self.assertEqual(offer.vs_typical_pct, -20)
        self.assertEqual(offer.typical_deal(), "below typical 360 € (−20%)")
        self.assertEqual(offer.cheapest_date, date(2026, 9, 15))
        self.assertEqual(offer.cheapest_eur, 300.0)
        payload = offer.to_dict()
        self.assertEqual(payload["cheapest_date"], "2026-09-15")
        self.assertEqual(payload["cheapest_eur"], 300.0)
        self.assertIsNone(source.calendar_calls[0][-1])

    def test_no_calendar_method_does_not_invent_a_typical(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        source = FakeSource(
            {("JFK", "LHR", "2026-09-15", 1): (card(airline="British Airways", price="412 €"),)}
        )
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.price_eur, 412.0)
        self.assertIsNone(offer.typical_eur)
        self.assertIsNone(offer.vs_typical)
        self.assertIsNone(offer.vs_typical_pct)
        self.assertIsNone(offer.typical_deal())
        self.assertIsNone(offer.cheapest_date)
        self.assertIsNone(offer.cheapest_eur)
        self.assertNotIn("cheapest_date", offer.to_dict())

    def test_thin_calendar_omits_the_comparison(self) -> None:
        query = FlightQuery("LAX", "NRT", date(2026, 10, 1), max_stops=1)
        source = FakeCalendarFlightSource(
            {("LAX", "NRT", "2026-10-01", 1): (card(price="612 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 1), 612.0),
                CompactCalendarDay(date(2026, 10, 2), None),
                CompactCalendarDay(date(2026, 10, 3), 588.0),
            ),
        )
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.price_eur, 612.0)
        self.assertIsNone(offer.typical_eur)
        self.assertIsNone(offer.vs_typical)
        self.assertIsNone(offer.vs_typical_pct)
        self.assertIsNone(offer.typical_deal())
        self.assertIsNone(offer.cheapest_date)
        self.assertIsNone(offer.cheapest_eur)
        self.assertNotIn("cheapest_date", offer.to_dict())

    def test_calendar_miss_does_not_fail_the_search_or_invent(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-09-15", 1): (card(price="412 €"),)},
            CompactParseMiss("no wrb.fr calendar payload"),
        )
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        assert isinstance(report.queries[0], QuerySuccess)
        self.assertIsNone(report.queries[0].offers[0].typical_eur)
        self.assertIsNone(report.queries[0].offers[0].vs_typical)
        self.assertIsNone(report.queries[0].offers[0].vs_typical_pct)
        self.assertIsNone(report.queries[0].offers[0].cheapest_date)
        self.assertNotIn("cheapest_date", report.queries[0].offers[0].to_dict())

    def test_same_route_reuses_one_calendar_fetch(self) -> None:
        first = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        second = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1, adults=1)
        days = (
            CompactCalendarDay(date(2026, 9, 15), 300.0),
            CompactCalendarDay(date(2026, 9, 16), 340.0),
            CompactCalendarDay(date(2026, 9, 17), 360.0),
        )
        source = FakeCalendarFlightSource(
            {
                ("JFK", "LHR", "2026-09-15", 1): (card(price="289 €"),),
            },
            days,
        )
        _run_search(
            (first, second),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(len(source.calendar_calls), 1)

    def test_round_trip_uses_same_stay_calendar_median(self) -> None:
        trip = RoundTrip("JFK", "LHR", date(2026, 10, 9), date(2026, 10, 20), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-10-09", 1): (card(airline="British Airways", price="620 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 9), 580.0),
                CompactCalendarDay(date(2026, 10, 10), 620.0),
                CompactCalendarDay(date(2026, 10, 11), 660.0),
            ),
        )
        report = _run_search(
            (trip,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(len(source.calendar_calls), 1)
        origin, dest, start, end, _stops, _adults, _cabin, nights = source.calendar_calls[0]
        self.assertEqual(origin, "JFK")
        self.assertEqual(dest, "LHR")
        self.assertEqual(start, date(2026, 10, 9))
        self.assertEqual(end, date(2026, 11, 8))
        self.assertEqual(nights, 11)
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.price_eur, 620.0)
        self.assertEqual(offer.typical_eur, 620.0)
        self.assertEqual(offer.vs_typical, "near")
        self.assertEqual(offer.vs_typical_pct, 0)
        self.assertEqual(offer.typical_deal(), "near typical 620 € (0%)")
        self.assertEqual(offer.cheapest_date, date(2026, 10, 9))
        self.assertEqual(offer.cheapest_eur, 580.0)

    def test_round_trip_does_not_reuse_a_one_way_calendar(self) -> None:
        one_way = FlightQuery("JFK", "LHR", date(2026, 10, 9), max_stops=1)
        packaged = RoundTrip("JFK", "LHR", date(2026, 10, 9), date(2026, 10, 20), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-10-09", 1): (card(price="620 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 9), 300.0),
                CompactCalendarDay(date(2026, 10, 10), 310.0),
                CompactCalendarDay(date(2026, 10, 11), 320.0),
            ),
        )
        report = _run_search(
            (one_way, packaged),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(len(source.calendar_calls), 2)
        self.assertIsNone(source.calendar_calls[0][-1])
        self.assertEqual(source.calendar_calls[1][-1], 11)
        assert isinstance(report.queries[0], QuerySuccess)
        assert isinstance(report.queries[1], QuerySuccess)
        self.assertEqual(report.queries[0].offers[0].typical_eur, 310.0)
        self.assertEqual(report.queries[1].offers[0].typical_eur, 310.0)
        self.assertEqual(report.queries[0].offers[0].cheapest_date, date(2026, 10, 9))
        self.assertEqual(report.queries[0].offers[0].cheapest_eur, 300.0)
        self.assertEqual(report.queries[1].offers[0].cheapest_date, date(2026, 10, 9))
        self.assertEqual(report.queries[1].offers[0].cheapest_eur, 300.0)

    def test_round_trip_thin_calendar_omits_the_comparison(self) -> None:
        trip = RoundTrip("YYZ", "LIS", date(2026, 10, 9), date(2026, 10, 16), max_stops=1)
        source = FakeCalendarFlightSource(
            {("YYZ", "LIS", "2026-10-09", 1): (card(price="410 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 9), 410.0),
                CompactCalendarDay(date(2026, 10, 10), None),
                CompactCalendarDay(date(2026, 10, 11), 388.0),
            ),
        )
        report = _run_search(
            (trip,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.price_eur, 410.0)
        self.assertIsNone(offer.typical_eur)
        self.assertIsNone(offer.vs_typical)
        self.assertIsNone(offer.vs_typical_pct)
        self.assertIsNone(offer.typical_deal())
        self.assertIsNone(offer.cheapest_date)
        self.assertNotIn("cheapest_date", offer.to_dict())

    def test_same_packaged_stay_reuses_one_calendar_fetch(self) -> None:
        first = RoundTrip("JFK", "LHR", date(2026, 10, 9), date(2026, 10, 20), max_stops=1)
        second = RoundTrip("JFK", "LHR", date(2026, 10, 9), date(2026, 10, 20), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-10-09", 1): (card(price="620 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 9), 580.0),
                CompactCalendarDay(date(2026, 10, 10), 620.0),
                CompactCalendarDay(date(2026, 10, 11), 660.0),
            ),
        )
        _run_search(
            (first, second),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(len(source.calendar_calls), 1)

    def test_multi_city_omits_typical(self) -> None:
        trip = MultiCity(
            (
                FlightLeg("JFK", "LHR", date(2026, 10, 9), max_stops=1),
                FlightLeg("LHR", "CDG", date(2026, 10, 12), max_stops=1),
            )
        )
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-10-09", 1): (card(price="410 €"),)},
            (
                CompactCalendarDay(date(2026, 10, 9), 300.0),
                CompactCalendarDay(date(2026, 10, 10), 310.0),
                CompactCalendarDay(date(2026, 10, 11), 320.0),
            ),
        )
        report = _run_search(
            (trip,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(source.calendar_calls, [])
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertIsNone(offer.typical_eur)
        self.assertIsNone(offer.vs_typical)
        self.assertIsNone(offer.vs_typical_pct)
        self.assertIsNone(offer.cheapest_date)
        self.assertNotIn("cheapest_date", offer.to_dict())

    def test_failed_query_does_not_fetch_a_typical(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-09-15", 1): NoFlightsFound()},
            (CompactCalendarDay(date(2026, 9, 15), 300.0),),
        )
        _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(source.calendar_calls, [])

    def test_fetch_with_calendar_stamps_typical_without_a_second_fetch(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)

        class PairSource(FakeCalendarFlightSource):
            def __init__(self) -> None:
                super().__init__(
                    {
                        ("JFK", "LHR", "2026-09-15", 1): (
                            card(airline="Norse Atlantic", price="289 €"),
                        )
                    },
                    (
                        CompactCalendarDay(date(2026, 9, 15), 300.0),
                        CompactCalendarDay(date(2026, 9, 16), 340.0),
                        CompactCalendarDay(date(2026, 9, 17), 360.0),
                    ),
                )
                self.pair_calls = 0

            def fetch(self, query):  # type: ignore[no-untyped-def]
                raise AssertionError("search should use fetch_with_calendar")

            def fetch_with_calendar(self, query: FlightQuery, start: date, end: date):
                self.pair_calls += 1
                cards = FakeSource.fetch(self, query)
                days = FakeCalendarFlightSource.fetch_calendar(self, query, start, end)
                return cards, days

        source = PairSource()
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(source.pair_calls, 1)
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(len(source.calendar_calls), 1)
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.typical_eur, 340.0)
        self.assertEqual(offer.vs_typical, "below")
        self.assertEqual(offer.vs_typical_pct, -15)
        self.assertEqual(offer.typical_deal(), "below typical 340 € (−15%)")
        self.assertEqual(offer.cheapest_date, date(2026, 9, 15))
        self.assertEqual(offer.cheapest_eur, 300.0)

    def test_cheapest_day_outside_the_window_is_not_used(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 15), max_stops=1)
        source = FakeCalendarFlightSource(
            {("JFK", "LHR", "2026-09-15", 1): (card(price="289 €"),)},
            (
                CompactCalendarDay(date(2026, 9, 14), 10.0),
                CompactCalendarDay(date(2026, 9, 15), 300.0),
                CompactCalendarDay(date(2026, 9, 16), 340.0),
                CompactCalendarDay(date(2026, 9, 17), 360.0),
            ),
        )
        report = _run_search(
            (query,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.typical_eur, 340.0)
        self.assertEqual(offer.cheapest_date, date(2026, 9, 15))
        self.assertEqual(offer.cheapest_eur, 300.0)

    def test_round_trip_fetch_with_calendar_stamps_same_stay_typical(self) -> None:
        trip = RoundTrip("JFK", "LHR", date(2026, 10, 9), date(2026, 10, 20), max_stops=1)

        class PairSource(FakeCalendarFlightSource):
            def __init__(self) -> None:
                super().__init__(
                    {("JFK", "LHR", "2026-10-09", 1): (card(price="620 €"),)},
                    (
                        CompactCalendarDay(date(2026, 10, 9), 580.0),
                        CompactCalendarDay(date(2026, 10, 10), 620.0),
                        CompactCalendarDay(date(2026, 10, 11), 660.0),
                    ),
                )
                self.pair_calls = 0

            def fetch(self, query):  # type: ignore[no-untyped-def]
                raise AssertionError("search should use fetch_with_calendar")

            def fetch_with_calendar(self, query, start: date, end: date):
                self.pair_calls += 1
                cards = FakeSource.fetch(self, query)
                days = FakeCalendarFlightSource.fetch_calendar(self, query, start, end)
                return cards, days

        source = PairSource()
        report = _run_search(
            (trip,),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 20),
        )
        self.assertEqual(source.pair_calls, 1)
        self.assertEqual(source.calendar_calls[0][-1], 11)
        assert isinstance(report.queries[0], QuerySuccess)
        offer = report.queries[0].offers[0]
        self.assertEqual(offer.typical_eur, 620.0)
        self.assertEqual(offer.vs_typical, "near")
        self.assertEqual(offer.vs_typical_pct, 0)
        self.assertEqual(offer.cheapest_date, date(2026, 10, 9))
        self.assertEqual(offer.cheapest_eur, 580.0)


class TypicalModelTests(unittest.TestCase):
    def test_typical_fields_must_be_paired(self) -> None:
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price="€100",
                price_eur=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
                typical_eur=120.0,
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price="€100",
                price_eur=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
                vs_typical="below",
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price="€100",
                price_eur=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
                typical_eur=120.0,
                vs_typical="below",
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price="€100",
                price_eur=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
                cheapest_date=date(2026, 9, 15),
                cheapest_eur=80.0,
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price="€100",
                price_eur=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
                typical_eur=120.0,
                vs_typical="near",
                vs_typical_pct=0,
                cheapest_date=date(2026, 9, 15),
            )


if __name__ == "__main__":
    unittest.main()
