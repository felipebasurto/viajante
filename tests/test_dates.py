from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from viajante.cli import main
from viajante.dates import (
    MAX_DATE_WINDOW_DAYS,
    calendar_trip,
    parse_route_pair,
    resolve_date_trip,
    search_dates,
    validate_date_window,
)
from viajante.google_flights import GoogleFlightsRejected, RawFlightCard
from viajante.google_flights_rpc import (
    CompactCalendarDay,
    CompactParseMiss,
    build_calendar_inner,
    parse_calendar_body,
)
from viajante.models import FlightQuery, RoundTrip, SearchErrorCode
from viajante.typical import typical_eur_from_daily_prices


def _calendar_body(rows: list[list[object]]) -> str:
    data = [None, rows]
    wrb = [["wrb.fr", None, json.dumps(data, separators=(",", ":"))]]
    raw = json.dumps(wrb, separators=(",", ":"))
    return f")]}}'\n\n{len(raw)}\n{raw}"


def _priced(day: str, price: int) -> list[object]:
    return [day, None, [[None, price], "tok"], 1]


def _priced_rt(outbound: str, returning: str, price: int) -> list[object]:
    return [outbound, returning, [[None, price], "tok"], 1]


class FakeCalendarSource:
    def __init__(
        self,
        days: tuple[CompactCalendarDay, ...] | Exception,
        cards: dict[date, tuple[RawFlightCard, ...]] | None = None,
    ) -> None:
        self.days = days
        self.cards = cards or {}
        self.closed = False
        self.calls = 0
        self.fetch_calls = 0
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch_calendar(self, query, start, end):
        self.calls += 1
        if isinstance(self.days, Exception):
            raise self.days
        return self.days

    def fetch(self, query):
        self.fetch_calls += 1
        return self.cards.get(query.departure_date, ())

    def close(self) -> None:
        self.closed = True


class DateWindowTests(unittest.TestCase):
    def test_route_pair(self) -> None:
        self.assertEqual(parse_route_pair("mad-lhr"), ("mad", "lhr"))
        with self.assertRaises(ValueError):
            parse_route_pair("MAD")

    def test_window_cap(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_date_window(date(2026, 9, 1), date(2026, 10, 3), today=date(2026, 8, 14))
        self.assertIn(str(MAX_DATE_WINDOW_DAYS), str(ctx.exception))

    def test_past_start_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_date_window(date(2026, 8, 1), date(2026, 8, 7), today=date(2026, 8, 14))

    def test_nights_implies_round_trip(self) -> None:
        kind, nights = resolve_date_trip("one-way", 5)
        self.assertEqual(kind, "rt")
        self.assertEqual(nights, 5)
        trip = calendar_trip("BOS", "LHR", date(2026, 11, 1), nights=5)
        self.assertIsInstance(trip, RoundTrip)
        self.assertEqual(trip.return_date, date(2026, 11, 6))

    def test_round_trip_without_nights_is_invalid(self) -> None:
        with self.assertRaises(ValueError):
            resolve_date_trip("rt", None)
        with self.assertRaises(ValueError):
            resolve_date_trip("multi", 5)


class CalendarParseTests(unittest.TestCase):
    def test_cheapest_per_day_from_fixture(self) -> None:
        body = _calendar_body(
            [
                _priced("2026-09-01", 81),
                _priced("2026-09-02", 81),
                _priced("2026-09-03", 67),
                ["2026-09-04", None, None, 1],
            ]
        )
        days = parse_calendar_body(body)
        self.assertEqual(len(days), 4)
        self.assertEqual(days[0].departure_date, date(2026, 9, 1))
        self.assertEqual(days[0].price_eur, 81.0)
        self.assertEqual(days[2].price_eur, 67.0)
        self.assertIsNone(days[3].price_eur)
        self.assertEqual(
            typical_eur_from_daily_prices([row.price_eur for row in days]),
            81.0,
        )

    def test_round_trip_calendar_rows_keep_return_dates(self) -> None:
        body = _calendar_body(
            [
                _priced_rt("2026-11-01", "2026-11-06", 410),
                _priced_rt("2026-11-02", "2026-11-07", 388),
                ["2026-11-03", "2026-11-08", None, 1],
            ]
        )
        days = parse_calendar_body(body)
        self.assertEqual(days[0].departure_date, date(2026, 11, 1))
        self.assertEqual(days[0].return_date, date(2026, 11, 6))
        self.assertEqual(days[0].price_eur, 410.0)
        self.assertEqual(days[1].price_eur, 388.0)
        self.assertIsNone(days[2].price_eur)
        self.assertEqual(days[2].return_date, date(2026, 11, 8))

    def test_unreadable_calendar_is_a_miss(self) -> None:
        with self.assertRaises(CompactParseMiss):
            parse_calendar_body("not a calendar")

    def test_calendar_inner_keeps_owned_constraints_and_window(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1))
        inner = build_calendar_inner(query, date(2026, 9, 1), date(2026, 9, 14))
        self.assertEqual(inner[2], ["2026-09-01", "2026-09-14"])
        self.assertEqual(inner[1][13][0][0], [[["MAD", 0]]])
        self.assertEqual(inner[1][13][0][1], [[["BCN", 0]]])

    def test_round_trip_calendar_inner_uses_stay_length(self) -> None:
        trip = calendar_trip(
            "BOS", "LHR", date(2026, 11, 1), nights=5, cabin="business", max_stops=2
        )
        inner = build_calendar_inner(trip, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(inner[2], ["2026-11-01", "2026-11-30"])
        self.assertEqual(inner[1][2], 1)
        self.assertEqual(inner[1][5], 3)
        segments = inner[1][13]
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0][6], "2026-11-01")
        self.assertEqual(segments[1][6], "2026-11-06")
        self.assertEqual(segments[0][0], [[["BOS", 0]]])
        self.assertEqual(segments[0][1], [[["LHR", 0]]])
        self.assertEqual(segments[1][0], [[["LHR", 0]]])
        self.assertEqual(segments[1][1], [[["BOS", 0]]])


class DateSearchTests(unittest.TestCase):
    def test_fake_source_fills_a_window(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), None),
                CompactCalendarDay(date(2026, 9, 3), 52.0),
            )
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 3), source=source)
        self.assertEqual(len(report.days), 3)
        self.assertEqual(report.trip, "one-way")
        self.assertIsNone(report.nights)
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[1].status, "empty")
        self.assertEqual(report.days[2].price_eur, 52.0)
        self.assertTrue(source.closed)

    def test_rejected_calendar_marks_every_day(self) -> None:
        source = FakeCalendarSource(GoogleFlightsRejected("nope"))
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=source)
        self.assertEqual(report.days[0].status, "error")
        self.assertEqual(report.days[0].error.code, SearchErrorCode.REJECTED)
        self.assertEqual(report.days[1].error.code, SearchErrorCode.REJECTED)

    def test_calendar_miss_falls_back_to_per_day_sweep(self) -> None:
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (
                    RawFlightCard(
                        airline="Iberia",
                        departure="07:00",
                        arrival="08:20",
                        duration="1 hr 20 min",
                        stops="Nonstop",
                        price="€45",
                    ),
                ),
                date(2026, 9, 2): (
                    RawFlightCard(
                        airline="Vueling",
                        departure="09:00",
                        arrival="10:20",
                        duration="1 hr 20 min",
                        stops="Nonstop",
                        price="€38",
                    ),
                ),
            },
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=source)
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(source.fetch_calls, 2)
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[0].airline, "Iberia")
        self.assertEqual(report.days[0].stops_count, 0)
        self.assertEqual(report.days[1].price_eur, 38.0)
        self.assertTrue(source.closed)

    def test_calendar_miss_uses_fetch_many_when_the_source_has_it(self) -> None:
        class MuxSource(FakeCalendarSource):
            def __init__(self) -> None:
                super().__init__(
                    CompactParseMiss("no wrb.fr calendar payload"),
                    cards={
                        date(2026, 9, 1): (
                            RawFlightCard(
                                airline="Iberia",
                                departure="07:00",
                                arrival="08:20",
                                duration="1 hr 20 min",
                                stops="Nonstop",
                                price="€45",
                            ),
                        ),
                        date(2026, 9, 2): (
                            RawFlightCard(
                                airline="Vueling",
                                departure="09:00",
                                arrival="10:20",
                                duration="1 hr 20 min",
                                stops="Nonstop",
                                price="€38",
                            ),
                        ),
                    },
                )
                self.fetch_many_calls = 0

            def fetch_many(self, trips):
                self.fetch_many_calls += 1
                return [self.fetch(trip) for trip in trips]

        source = MuxSource()
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=source)
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(source.fetch_many_calls, 1)
        self.assertEqual(source.fetch_calls, 2)
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].price_eur, 38.0)

    def test_round_trip_calendar_fills_return_dates_and_does_not_invent_fares(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 11, 1), 410.0, date(2026, 11, 6)),
                CompactCalendarDay(date(2026, 11, 2), None, date(2026, 11, 7)),
            )
        )
        report = search_dates(
            "BOS",
            "LHR",
            date(2026, 11, 1),
            date(2026, 11, 2),
            trip="rt",
            nights=5,
            source=source,
        )
        self.assertEqual(report.trip, "rt")
        self.assertEqual(report.nights, 5)
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days[0].price_eur, 410.0)
        self.assertEqual(report.days[0].return_date, date(2026, 11, 6))
        self.assertEqual(report.days[1].status, "empty")
        self.assertIsNone(report.days[1].price_eur)
        self.assertEqual(report.days[1].return_date, date(2026, 11, 7))
        self.assertTrue(source.closed)

    def test_round_trip_calendar_miss_sweeps_packaged_stays(self) -> None:
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 11, 1): (
                    RawFlightCard(
                        airline="British Airways",
                        departure="18:00",
                        arrival="06:00",
                        duration="7 hr",
                        stops="Nonstop",
                        price="€410",
                    ),
                ),
            },
        )
        report = search_dates(
            "BOS",
            "LHR",
            date(2026, 11, 1),
            date(2026, 11, 2),
            nights=5,
            source=source,
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(report.trip, "rt")
        self.assertEqual(source.fetch_calls, 2)
        self.assertEqual(report.days[0].price_eur, 410.0)
        self.assertEqual(report.days[0].return_date, date(2026, 11, 6))
        self.assertEqual(report.days[1].status, "empty")
        self.assertIsNone(report.days[1].price_eur)


class DateCliTests(unittest.TestCase):
    def test_window_cap_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_dates") as search:
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-10-15",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_prints_compact_table(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 40.0),
                CompactCalendarDay(date(2026, 9, 2), 55.0),
            )
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "dates",
                        "MAD-BCN",
                        "--from",
                        "2026-09-01",
                        "--to",
                        "2026-09-02",
                        "--fetch",
                        "sweep",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("MAD -> BCN", output)
        self.assertIn("2026-09-01", output)
        self.assertIn("40 €", output)
        self.assertIn("55 €", output)

    def test_dates_help_mentions_the_cap(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["dates", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn(str(MAX_DATE_WINDOW_DAYS), help_text)
        self.assertIn("viajante dates LAX-NRT", help_text)
        self.assertIn("--nights", help_text)

    def test_trip_rt_without_nights_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_dates") as search:
            code = main(
                [
                    "dates",
                    "BOS-LHR",
                    "--from",
                    "2026-11-01",
                    "--to",
                    "2026-11-07",
                    "--trip",
                    "rt",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_prints_round_trip_stay_in_the_header(self) -> None:
        source = FakeCalendarSource(
            (CompactCalendarDay(date(2026, 11, 1), 410.0, date(2026, 11, 6)),)
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "dates",
                        "BOS-LHR",
                        "--from",
                        "2026-11-01",
                        "--to",
                        "2026-11-01",
                        "--nights",
                        "5",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("BOS -> LHR", output)
        self.assertIn("rt, 5 nights", output)
        self.assertIn("410 €", output)


if __name__ == "__main__":
    unittest.main()
