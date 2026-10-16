"""Nearby-date grid on a dated flight search. One check for the calendar reuse."""

from __future__ import annotations

import io
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _isolate  # noqa: F401
from test_flights import card
from viajante.cli import main
from viajante.cli_report import _print_report
from viajante.flights import search_flights
from viajante.google_flights import GoogleFlightsBlocked
from viajante.models import FlightLeg, FlightQuery, MultiCity, QuerySuccess, RoundTrip


def _price(offset: int) -> int:
    if offset == -1:
        return 80
    if offset == 2:
        return 0
    return 100 + abs(offset) * 10


class _GridSource:
    def __init__(self, center: date) -> None:
        self.center = center
        self.config = SimpleNamespace(html_lang="en", currency="USD", country=None)
        self.fetches = 0
        self.batched: list[list[date]] = []
        self.windows: list[list[tuple[date, date]]] = []

    def fetch(self, trip):
        self.fetches += 1
        if getattr(self, "block", False):
            raise GoogleFlightsBlocked("blocked", status=429)
        return [card(price=f"${_price(0)}")]

    def fetch_many(self, trips):
        self.batched.append([trip.departure_date for trip in trips])
        rows = []
        for trip in trips:
            offset = (trip.departure_date - self.center).days
            amount = _price(offset)
            rows.append(() if amount == 0 else [card(price=f"${amount}")])
        return rows

    def fetch_round_trip_window(self, trips, on_day):
        self.windows.append([(trip.departure_date, trip.return_date) for trip in trips])
        for index, trip in enumerate(trips):
            offset = (trip.departure_date - self.center).days
            amount = 90 if offset == 1 else 100 + abs(offset) * 5
            on_day(index, [card(price=f"${amount}")])

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class NearbyDateGridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.center = date.today() + timedelta(days=40)

    def _search(self, query, **kwargs):
        source = _GridSource(self.center)
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights(
                (query,),
                top=1,
                fetch="sweep",
                currency="USD",
                nearby_days=kwargs.pop("nearby_days", 2),
                **kwargs,
            )
        return report, source

    def test_one_way_grid_deltas_cheapest_and_one_batch(self) -> None:
        query = FlightQuery("JFK", "LHR", self.center)
        report, source = self._search(query, less_emissions=True)
        self.assertEqual(source.fetches, 1)
        self.assertEqual(len(source.batched), 1)
        self.assertEqual(len(source.batched[0]), 5)
        result = report.queries[0]
        assert isinstance(result, QuerySuccess)
        grid = result.nearby_dates
        assert grid is not None
        self.assertEqual(grid.days, 2)
        self.assertIsNone(grid.nights)
        by_offset = {cell.offset_days: cell for cell in grid.prices}
        self.assertEqual(set(by_offset), {-2, -1, 0, 1, 2})
        self.assertEqual(by_offset[0].price, 100)
        self.assertEqual(by_offset[0].delta, 0)
        self.assertEqual(by_offset[-1].price, 80)
        self.assertEqual(by_offset[-1].delta, -20)
        self.assertTrue(by_offset[-1].cheapest)
        self.assertEqual(grid.cheapest_date, self.center - timedelta(days=1))
        self.assertEqual(sum(cell.cheapest for cell in grid.prices), 1)
        self.assertIsNone(by_offset[2].price)
        self.assertIsNone(by_offset[2].delta)
        self.assertEqual(by_offset[2].empty_reason, "provider_empty")
        payload = result.to_dict("USD")["nearby_dates"]
        self.assertEqual(payload["cheapest_date"], grid.cheapest_date.isoformat())
        text = io.StringIO()
        with patch("sys.stdout", text):
            _print_report(report)
        shown = text.getvalue()
        self.assertIn("cheapest", shown)
        self.assertIn("provider_empty", shown)
        self.assertIn("-20", shown)

    def test_day_queries_keep_the_page_filter(self) -> None:
        query = FlightQuery("JFK", "LHR", self.center)
        seen: list = []

        class _Recording(_GridSource):
            def fetch_many(self, trips):
                seen.extend(trips)
                return super().fetch_many(trips)

        source = _Recording(self.center)
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            search_flights(
                (query,),
                top=1,
                fetch="sweep",
                currency="USD",
                nearby_days=1,
                less_emissions=True,
            )
        self.assertEqual(len(seen), 3)
        self.assertTrue(
            all(trip.provider is not None and trip.provider.less_emissions for trip in seen)
        )

    def test_round_trip_shifts_both_dates_and_keeps_nights(self) -> None:
        back = self.center + timedelta(days=7)
        report, source = self._search(RoundTrip("JFK", "LHR", self.center, back))
        self.assertEqual(len(source.windows), 1)
        pairs = source.windows[0]
        self.assertEqual(len(pairs), 5)
        self.assertTrue(all((ret - out).days == 7 for out, ret in pairs))
        result = report.queries[0]
        assert isinstance(result, QuerySuccess)
        grid = result.nearby_dates
        assert grid is not None
        self.assertEqual(grid.nights, 7)
        winner = next(cell for cell in grid.prices if cell.cheapest)
        self.assertEqual(winner.offset_days, 1)
        self.assertEqual(winner.return_date, back + timedelta(days=1))
        self.assertEqual(winner.delta, -10)
        asked = next(cell for cell in grid.prices if cell.offset_days == 0)
        self.assertEqual(asked.delta, 0)
        self.assertEqual(asked.return_date, back)

    def test_zero_omits_the_grid_and_a_block_sends_no_calendar(self) -> None:
        query = FlightQuery("JFK", "LHR", self.center)
        report, source = self._search(query, nearby_days=0)
        self.assertIsNone(report.queries[0].nearby_dates)
        self.assertEqual(source.batched, [])
        source = _GridSource(self.center)
        source.block = True
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            blocked = search_flights((query,), top=1, fetch="sweep", currency="USD", nearby_days=2)
        self.assertEqual(source.batched, [])
        self.assertIsNone(blocked.queries[0].nearby_dates)

    def test_multi_city_and_bad_counts_do_not_fetch_a_grid(self) -> None:
        later = self.center + timedelta(days=3)
        trip = MultiCity(
            (
                FlightLeg("JFK", "LHR", self.center, 0),
                FlightLeg("LHR", "CDG", later, 0),
            )
        )
        _report, source = self._search(trip, nearby_days=2)
        self.assertIsNone(_report.queries[0].nearby_dates)
        self.assertEqual(source.batched, [])
        query = FlightQuery("JFK", "LHR", self.center)
        with patch("viajante.flights.GoogleFlightsHttpSource") as ctor:
            with self.assertRaisesRegex(ValueError, "nearby_days"):
                search_flights((query,), nearby_days=-1, currency="USD")
            with self.assertRaisesRegex(ValueError, "nearby_days"):
                search_flights((query,), nearby_days=True, currency="USD")  # type: ignore[arg-type]
        ctor.assert_not_called()

    def test_past_date_keeps_the_flight_result_and_skips_the_grid(self) -> None:
        query = FlightQuery("JFK", "LHR", date.today() - timedelta(days=1))
        source = _GridSource(query.departure_date)
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights((query,), top=1, fetch="sweep", currency="USD", nearby_days=1)
        self.assertEqual(source.fetches, 1)
        self.assertEqual(source.batched, [])
        self.assertIsNone(report.queries[0].nearby_dates)

    def test_cli_defaults_to_three_and_zero_disables(self) -> None:
        route = f"JFK-LHR:{self.center.isoformat()}"

        class _Stop(Exception):
            pass

        with patch("viajante.cli.search_flights", side_effect=_Stop) as search:
            with self.assertRaises(_Stop):
                main(["flights", route, "--currency", "USD"])
        self.assertEqual(search.call_args.kwargs["nearby_days"], 3)
        with patch("viajante.cli.search_flights", side_effect=_Stop) as search:
            with self.assertRaises(_Stop):
                main(["flights", route, "--currency", "USD", "--nearby-days", "0"])
        self.assertEqual(search.call_args.kwargs["nearby_days"], 0)
