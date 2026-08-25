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
    EMPTY_DAY_MARK,
    MAX_DATE_WINDOW_DAYS,
    MAX_FLEX_DAYS,
    calendar_trip,
    cheapest_priced_day,
    flex_window,
    format_sparkline,
    format_summary_line,
    format_week_calendar,
    parse_route_pair,
    resolve_date_trip,
    search_dates,
    search_flex,
    validate_date_window,
)
from viajante.flights import _normalize_offer, parse_depart_window
from viajante.google_flights import GoogleFlightsRejected, RawFlightCard
from viajante.google_flights_rpc import (
    CompactCalendarDay,
    CompactParseMiss,
    build_calendar_inner,
    build_shopping_inner,
    parse_calendar_body,
)
from viajante.models import (
    MIN_PRICED_DAYS_FOR_SUMMARY,
    DateCalendarReport,
    DatePriceRow,
    FlexSearchReport,
    FlightQuery,
    RoundTrip,
    SearchErrorCode,
    owned_calendar_summary,
)
from viajante.typical import MIN_DAILY_PRICES, typical_eur_from_daily_prices


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
        self.fetched_queries: list[object] = []
        self.calendar_queries: list[object] = []
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch_calendar(self, query, start, end):
        self.calls += 1
        self.calendar_queries.append(query)
        if isinstance(self.days, Exception):
            raise self.days
        return self.days

    def fetch(self, query):
        self.fetch_calls += 1
        self.fetched_queries.append(query)
        return self.cards.get(query.departure_date, ())

    def close(self) -> None:
        self.closed = True


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


def _flex_shop_source(*cards: RawFlightCard) -> FakeCalendarSource:
    chosen = date(2026, 9, 12)
    return FakeCalendarSource(
        (
            CompactCalendarDay(date(2026, 9, 11), 180.0),
            CompactCalendarDay(chosen, 90.0),
            CompactCalendarDay(date(2026, 9, 13), 140.0),
        ),
        cards={chosen: cards},
    )


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
        self.assertEqual(trip.children, 0)
        self.assertEqual(trip.infants_in_seat, 0)
        self.assertEqual(trip.infants_on_lap, 0)

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


class CalendarPresentationTests(unittest.TestCase):
    def test_summary_floor_matches_typical(self) -> None:
        self.assertEqual(MIN_PRICED_DAYS_FOR_SUMMARY, MIN_DAILY_PRICES)
        self.assertEqual(MIN_PRICED_DAYS_FOR_SUMMARY, 3)

    def test_summary_uses_only_priced_days_and_skips_empty(self) -> None:
        summary = owned_calendar_summary(
            (
                (date(2026, 9, 1), 81.0),
                (date(2026, 9, 2), None),
                (date(2026, 9, 3), 67.0),
                (date(2026, 9, 4), 120.0),
                (date(2026, 9, 5), 0.0),
            )
        )
        assert summary is not None
        self.assertEqual(summary.min_eur, 67.0)
        self.assertEqual(summary.median_eur, 81.0)
        self.assertEqual(summary.max_eur, 120.0)
        self.assertEqual(summary.cheapest_date, date(2026, 9, 3))
        self.assertEqual(summary.n_priced, 3)

    def test_summary_omitted_when_fewer_than_three_priced_days(self) -> None:
        self.assertIsNone(
            owned_calendar_summary(
                (
                    (date(2026, 9, 1), 40.0),
                    (date(2026, 9, 2), None),
                    (date(2026, 9, 3), 55.0),
                )
            )
        )

    def test_tied_min_picks_the_earliest_owned_day(self) -> None:
        summary = owned_calendar_summary(
            (
                (date(2026, 9, 2), 50.0),
                (date(2026, 9, 1), 50.0),
                (date(2026, 9, 3), 80.0),
            )
        )
        assert summary is not None
        self.assertEqual(summary.cheapest_date, date(2026, 9, 1))
        self.assertEqual(summary.min_eur, 50.0)

    def test_search_dates_attaches_summary_from_owned_rows(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 81.0),
                CompactCalendarDay(date(2026, 9, 2), None),
                CompactCalendarDay(date(2026, 9, 3), 67.0),
                CompactCalendarDay(date(2026, 9, 4), 90.0),
            )
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 4), source=source)
        assert report.summary is not None
        self.assertEqual(report.summary.min_eur, 67.0)
        self.assertEqual(report.summary.median_eur, 81.0)
        self.assertEqual(report.summary.max_eur, 90.0)
        self.assertEqual(report.summary.cheapest_date, date(2026, 9, 3))
        self.assertEqual(report.summary.n_priced, 3)
        self.assertEqual(report.to_dict()["summary"]["cheapest_date"], "2026-09-03")

    def test_search_dates_omits_summary_when_the_grid_is_thin(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), None),
                CompactCalendarDay(date(2026, 9, 3), 52.0),
            )
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 3), source=source)
        self.assertIsNone(report.summary)
        self.assertNotIn("summary", report.to_dict())

    def test_week_calendar_marks_empty_days_and_does_not_invent(self) -> None:
        lines = format_week_calendar(
            (
                DatePriceRow(departure_date=date(2026, 9, 1), price_eur=40.0),
                DatePriceRow(departure_date=date(2026, 9, 2), status="empty"),
                DatePriceRow(departure_date=date(2026, 9, 3), price_eur=55.0),
            )
        )
        self.assertGreaterEqual(len(lines), 2)
        self.assertIn("Mon", lines[0])
        body = "\n".join(lines[1:])
        self.assertIn("40", body)
        self.assertIn("55", body)
        self.assertIn(EMPTY_DAY_MARK, body)
        self.assertNotIn("47", body)
        self.assertIn("1-3 Sep", body)

    def test_sparkline_marks_empty_and_skips_when_nothing_is_priced(self) -> None:
        spark = format_sparkline(
            (
                DatePriceRow(departure_date=date(2026, 9, 1), price_eur=40.0),
                DatePriceRow(departure_date=date(2026, 9, 2), status="empty"),
                DatePriceRow(departure_date=date(2026, 9, 3), price_eur=80.0),
            )
        )
        self.assertEqual(len(spark), 3)
        self.assertEqual(spark[1], EMPTY_DAY_MARK)
        self.assertEqual(spark[0], "▁")
        self.assertEqual(spark[2], "█")
        self.assertEqual(
            format_sparkline((DatePriceRow(departure_date=date(2026, 9, 1), status="empty"),)),
            "",
        )

    def test_summary_line_is_english(self) -> None:
        summary = owned_calendar_summary(
            (
                (date(2026, 9, 1), 40.0),
                (date(2026, 9, 2), 55.0),
                (date(2026, 9, 3), 90.0),
            )
        )
        assert summary is not None
        line = format_summary_line(summary)
        self.assertIn("min 40 €", line)
        self.assertIn("median 55 €", line)
        self.assertIn("max 90 €", line)
        self.assertIn("cheapest 2026-09-01", line)
        self.assertIn("3 priced", line)


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
        self.assertIn("Mon", output)
        self.assertNotIn("min ", output)
        self.assertNotIn("median ", output)
        self.assertNotIn("Cheapest nonstop:", output)

    def test_prints_week_calendar_summary_and_marks_empty(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 40.0),
                CompactCalendarDay(date(2026, 9, 2), None),
                CompactCalendarDay(date(2026, 9, 3), 90.0),
                CompactCalendarDay(date(2026, 9, 4), 55.0),
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
                        "2026-09-04",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Mon", output)
        self.assertIn(EMPTY_DAY_MARK, output)
        self.assertIn("min 40 €", output)
        self.assertIn("median 55 €", output)
        self.assertIn("max 90 €", output)
        self.assertIn("cheapest 2026-09-01", output)
        self.assertIn("3 priced", output)
        self.assertNotIn("65 €", output)

    def test_dates_help_mentions_the_cap(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["dates", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn(str(MAX_DATE_WINDOW_DAYS), help_text)
        self.assertIn("viajante dates LAX-NRT", help_text)
        self.assertIn("--nights", help_text)
        self.assertIn("--bags", help_text)
        self.assertIn("--carry-on", help_text)
        self.assertIn("--via", help_text)
        self.assertIn("--exclude-via", help_text)
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
        self.assertIn("--max-layover", help_text)
        self.assertIn("--min-layover", help_text)
        self.assertIn("--max-duration", help_text)

    def test_dates_forwards_owned_shop_filters(self) -> None:
        with (
            patch("viajante.cli.search_dates") as search,
            patch("viajante.cli._print_dates_report"),
            patch("viajante.cli._dates_exit_code", return_value=0),
        ):
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
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
        self.assertEqual(kwargs["max_layover_hours"], 3)
        self.assertEqual(kwargs["min_layover_hours"], 1)
        self.assertEqual(kwargs["max_duration_hours"], 8)

    def test_dates_unnamed_shop_filters_stay_unset(self) -> None:
        with (
            patch("viajante.cli.search_dates") as search,
            patch("viajante.cli._print_dates_report"),
            patch("viajante.cli._dates_exit_code", return_value=0),
        ):
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
                ]
            )
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
        self.assertIsNone(kwargs["max_layover_hours"])
        self.assertIsNone(kwargs["min_layover_hours"])
        self.assertIsNone(kwargs["max_duration_hours"])
        self.assertFalse(kwargs["nearby"])
        self.assertEqual(kwargs["children"], 0)
        self.assertEqual(kwargs["infants_in_seat"], 0)
        self.assertEqual(kwargs["infants_on_lap"], 0)
        self.assertEqual(kwargs["currency"], "EUR")
        self.assertIsNone(kwargs["country"])

    def test_dates_forwards_named_occupancy(self) -> None:
        with (
            patch("viajante.cli.search_dates") as search,
            patch("viajante.cli._print_dates_report"),
            patch("viajante.cli._dates_exit_code", return_value=0),
        ):
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
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

    def test_dates_forwards_named_currency_country(self) -> None:
        with (
            patch("viajante.cli.search_dates") as search,
            patch("viajante.cli._print_dates_report"),
            patch("viajante.cli._dates_exit_code", return_value=0),
        ):
            code = main(
                [
                    "dates",
                    "JFK-LHR",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
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

    def test_dates_invalid_currency_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_dates") as search:
            code = main(
                [
                    "dates",
                    "JFK-LHR",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
                    "--currency",
                    "euro",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_dates_lap_infants_over_adults_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_dates") as search:
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
                    "--adults",
                    "1",
                    "--infants-on-lap",
                    "2",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_dates_negative_max_layover_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_dates") as search:
            code = main(
                [
                    "dates",
                    "MAD-BCN",
                    "--from",
                    "2026-09-01",
                    "--to",
                    "2026-09-02",
                    "--max-layover",
                    "-1",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_dates_nearby_flag_forwards(self) -> None:
        with (
            patch("viajante.cli.search_dates") as search,
            patch("viajante.cli._print_dates_report"),
            patch("viajante.cli._dates_exit_code", return_value=0),
        ):
            err = io.StringIO()
            with patch("sys.stderr", err):
                code = main(
                    [
                        "dates",
                        "BOS-LHR",
                        "--from",
                        "2026-09-01",
                        "--to",
                        "2026-09-02",
                        "--nearby",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertTrue(search.call_args.kwargs["nearby"])
        self.assertIn("nearby London", err.getvalue())

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


class FlexWindowTests(unittest.TestCase):
    def test_around_plus_minus_three(self) -> None:
        start, end = flex_window(date(2026, 9, 12), 3, today=date(2026, 8, 20))
        self.assertEqual(start, date(2026, 9, 9))
        self.assertEqual(end, date(2026, 9, 15))
        self.assertEqual((end - start).days + 1, 7)
        self.assertLessEqual((end - start).days + 1, MAX_DATE_WINDOW_DAYS)

    def test_past_around_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            flex_window(date(2026, 8, 1), 3, today=date(2026, 8, 20))

    def test_flex_zero_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            flex_window(date(2026, 9, 12), 0, today=date(2026, 8, 20))

    def test_flex_over_cap_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            flex_window(date(2026, 9, 12), MAX_FLEX_DAYS + 1, today=date(2026, 8, 20))

    def test_start_clamps_to_today(self) -> None:
        start, end = flex_window(date(2026, 8, 21), 3, today=date(2026, 8, 20))
        self.assertEqual(start, date(2026, 8, 20))
        self.assertEqual(end, date(2026, 8, 24))

    def test_cheapest_day_breaks_ties_toward_the_anchor(self) -> None:
        rows = (
            DatePriceRow(departure_date=date(2026, 9, 9), price_eur=100.0, status="ok"),
            DatePriceRow(departure_date=date(2026, 9, 13), price_eur=100.0, status="ok"),
            DatePriceRow(departure_date=date(2026, 9, 11), price_eur=120.0, status="ok"),
        )
        winner = cheapest_priced_day(rows, date(2026, 9, 12))
        assert winner is not None
        self.assertEqual(winner.departure_date, date(2026, 9, 13))


class FlexSearchTests(unittest.TestCase):
    def test_calendar_picks_cheapest_and_shops_once(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 9), 520.0, date(2026, 9, 16)),
                CompactCalendarDay(date(2026, 9, 10), 388.0, date(2026, 9, 17)),
                CompactCalendarDay(date(2026, 9, 11), 410.0, date(2026, 9, 18)),
                CompactCalendarDay(date(2026, 9, 12), 450.0, date(2026, 9, 19)),
                CompactCalendarDay(date(2026, 9, 13), 430.0, date(2026, 9, 20)),
                CompactCalendarDay(date(2026, 9, 14), None, date(2026, 9, 21)),
                CompactCalendarDay(date(2026, 9, 15), 500.0, date(2026, 9, 22)),
            ),
            cards={
                date(2026, 9, 10): (
                    RawFlightCard(
                        airline="British Airways",
                        departure="18:00",
                        arrival="06:00",
                        duration="7 hr",
                        stops="Nonstop",
                        price="€350",
                    ),
                ),
            },
        )
        report = search_flex(
            "BOS",
            "LHR",
            date(2026, 9, 12),
            3,
            nights=7,
            source=source,
            buffer_eur=0,
        )
        self.assertEqual(report.chosen_date, date(2026, 9, 10))
        self.assertEqual(report.return_date, date(2026, 9, 17))
        self.assertEqual(report.trip, "rt")
        self.assertEqual(report.nights, 7)
        self.assertEqual(report.locale, "en")
        self.assertEqual(source.calls, 1)
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(report.fetch_backend, "calendar_then_sweep")
        self.assertEqual(report.offers[0].price_eur, 350.0)
        self.assertEqual(report.typical_eur, 440.0)
        self.assertEqual(report.vs_typical, "below")
        self.assertEqual(report.offers[0].typical_eur, 440.0)
        self.assertEqual(report.offers[0].vs_typical, "below")
        self.assertTrue(source.closed)

    def test_calendar_miss_does_not_shop_or_invent(self) -> None:
        source = FakeCalendarSource(CompactParseMiss("no wrb.fr calendar payload"))
        report = search_flex("BOS", "LHR", date(2026, 9, 12), 3, nights=7, source=source)
        self.assertIsNone(report.chosen_date)
        self.assertEqual(report.offers, ())
        self.assertIsNone(report.typical_eur)
        self.assertIsNone(report.vs_typical)
        self.assertEqual(source.fetch_calls, 0)
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days, ())
        self.assertTrue(source.closed)

    def test_empty_window_does_not_invent(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 9), None),
                CompactCalendarDay(date(2026, 9, 12), None),
                CompactCalendarDay(date(2026, 9, 15), None),
            )
        )
        report = search_flex("JFK", "LHR", date(2026, 9, 12), 3, source=source)
        self.assertIsNone(report.chosen_date)
        self.assertEqual(report.offers, ())
        self.assertIsNone(report.typical_eur)
        self.assertEqual(source.fetch_calls, 0)
        self.assertEqual(len(report.days), 7)
        self.assertTrue(all(row.status == "empty" for row in report.days))

    def test_thin_grid_omits_typical(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 12), 410.0),
                CompactCalendarDay(date(2026, 9, 13), 388.0),
            ),
            cards={
                date(2026, 9, 13): (
                    RawFlightCard(
                        airline="Norse Atlantic",
                        departure="21:00",
                        arrival="08:00",
                        duration="7 hr",
                        stops="Nonstop",
                        price="€388",
                    ),
                ),
            },
        )
        report = search_flex("JFK", "LHR", date(2026, 9, 12), 3, source=source, buffer_eur=0)
        self.assertEqual(report.chosen_date, date(2026, 9, 13))
        self.assertEqual(report.offers[0].price_eur, 388.0)
        self.assertIsNone(report.typical_eur)
        self.assertIsNone(report.vs_typical)
        self.assertIsNone(report.offers[0].typical_eur)


class ShopFilterTests(unittest.TestCase):
    def test_unnamed_filters_do_not_invent_constraints(self) -> None:
        trip = calendar_trip("MAD", "BCN", date(2026, 9, 12))
        self.assertIsNone(trip.bags)
        self.assertIsNone(trip.carry_on)
        self.assertIsNone(trip.price_cap_eur)
        self.assertIsNone(trip.airlines)
        self.assertIsNone(trip.exclude_airlines)
        self.assertIsNone(trip.alliances)
        self.assertIsNone(trip.exclude_alliances)
        self.assertEqual(trip.children, 0)
        self.assertEqual(trip.infants_in_seat, 0)
        self.assertEqual(trip.infants_on_lap, 0)
        self.assertEqual(
            build_calendar_inner(trip, date(2026, 9, 12), date(2026, 9, 13))[1][6],
            [1, 0, 0, 0],
        )
        too_few = _card(checked_bags=0, carry_on=0, price="€40")
        over_cap = _card(airline="Ryanair", airline_codes=("FR",), price="€401")
        other_via = _card(stops="1 stop", layover_city="DXB", price="€80")
        source = _flex_shop_source(too_few, over_cap, other_via)
        report = search_flex("MAD", "BCN", date(2026, 9, 12), 1, source=source, buffer_eur=0)
        prices = [offer.price_eur for offer in report.offers]
        self.assertEqual(prices, [40.0, 80.0, 401.0])

    def test_flex_shop_bags_drop_the_same_contradictions_as_search_flights(self) -> None:
        silent = _card(price="€80")
        too_few = _card(checked_bags=0, carry_on=1, price="€70")
        enough = _card(checked_bags=1, carry_on=1, price="€90")
        self.assertIsNotNone(_normalize_offer(silent, 1, bags=1, carry_on=1))
        self.assertIsNone(_normalize_offer(too_few, 1, bags=1, carry_on=1))
        self.assertIsNotNone(_normalize_offer(enough, 1, bags=1, carry_on=1))
        source = _flex_shop_source(silent, too_few, enough)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            bags=1,
            carry_on=1,
        )
        self.assertEqual([offer.price_eur for offer in report.offers], [80.0, 90.0])
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.fetched_queries[0].bags, 1)
        self.assertEqual(source.fetched_queries[0].carry_on, 1)

    def test_flex_shop_via_airlines_and_price_cap_match_normalize(self) -> None:
        via_lis = _card(stops="1 stop", layover_city="LIS", airline_codes=("IB",), price="€120")
        via_dxb = _card(stops="1 stop", layover_city="DXB", airline_codes=("IB",), price="€90")
        nonstop = _card(stops="Nonstop", airline_codes=("IB",), price="€110")
        ryanair = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="LIS",
            price="€60",
        )
        over_cap = _card(
            airline="Iberia",
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€250",
        )
        cards = (via_lis, via_dxb, nonstop, ryanair, over_cap)
        filters = dict(via=("LIS",), airlines=("IB",), price_cap_eur=200)
        expected = [_normalize_offer(card, 1, buffer_eur=0, **filters) for card in cards]
        kept = [offer.price_eur for offer in expected if offer is not None]
        source = _flex_shop_source(*cards)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            **filters,
        )
        self.assertEqual([offer.price_eur for offer in report.offers], kept)
        self.assertEqual(kept, [120.0])

    def test_flex_shop_exclude_via_and_exclude_airlines_match_normalize(self) -> None:
        keep = _card(stops="1 stop", layover_city="OPO", airline_codes=("IB",), price="€100")
        drop_via = _card(stops="1 stop", layover_city="LIS", airline_codes=("IB",), price="€80")
        drop_airline = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="OPO",
            price="€70",
        )
        cards = (keep, drop_via, drop_airline)
        filters = dict(exclude_via=("LIS",), exclude_airlines=("FR",))
        expected = [_normalize_offer(card, 1, buffer_eur=0, **filters) for card in cards]
        kept = [offer.price_eur for offer in expected if offer is not None]
        source = _flex_shop_source(*cards)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            **filters,
        )
        self.assertEqual([offer.price_eur for offer in report.offers], kept)
        self.assertEqual(kept, [100.0])

    def test_dates_sweep_applies_the_same_shop_filters(self) -> None:
        too_few = _card(checked_bags=0, price="€38")
        enough = _card(
            checked_bags=1,
            airline_codes=("IB",),
            layover_city="LIS",
            stops="1 stop",
            price="€45",
        )
        over_cap = _card(airline_codes=("IB",), price="€300")
        iberia = _card(airline_codes=("IB",), layover_city="LIS", stops="1 stop", price="€90")
        ryanair = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            layover_city="LIS",
            stops="1 stop",
            price="€70",
        )
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (too_few, enough),
                date(2026, 9, 2): (over_cap, iberia, ryanair),
            },
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            bags=1,
            via=("LIS",),
            airlines=("IB",),
            price_cap_eur=200,
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].status, "ok")
        self.assertEqual(report.days[1].price_eur, 90.0)

    def test_dates_sweep_unnamed_keeps_contradicting_cards(self) -> None:
        cheap_contradiction = _card(checked_bags=0, airline_codes=("FR",), price="€30")
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={date(2026, 9, 1): (cheap_contradiction,)},
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 1), source=source)
        self.assertEqual(report.days[0].price_eur, 30.0)

    def test_flex_shop_depart_window_drops_off_clock_offers(self) -> None:
        morning = _card(departure="08:00", price="€90")
        evening = _card(departure="21:00", price="€40")
        silent = _card(departure=None, price="€70")
        window = parse_depart_window("7-12")
        self.assertIsNotNone(_normalize_offer(morning, 1, depart_window=window))
        self.assertIsNone(_normalize_offer(evening, 1, depart_window=window))
        self.assertIsNone(_normalize_offer(silent, 1, depart_window=window))
        source = _flex_shop_source(morning, evening, silent)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            depart_window=window,
        )
        self.assertEqual([offer.price_eur for offer in report.offers], [90.0])
        unnamed = search_flex(
            "MAD", "BCN", date(2026, 9, 12), 1, source=_flex_shop_source(morning, evening, silent)
        )
        self.assertEqual([offer.price_eur for offer in unnamed.offers], [40.0, 70.0, 90.0])

    def test_dates_sweep_depart_window_unprices_off_window_days(self) -> None:
        morning = _card(departure="08:00", price="€45")
        evening = _card(departure="21:00", price="€30")
        silent = _card(departure=None, price="€20")
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (morning, evening),
                date(2026, 9, 2): (evening, silent),
            },
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            depart_window=parse_depart_window("7-12"),
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].status, "empty")
        self.assertIsNone(report.days[1].price_eur)

    def test_dates_compact_calendar_does_not_invent_a_clock(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            depart_window=parse_depart_window("7-12"),
        )
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].price_eur, 30.0)
        self.assertEqual(source.fetch_calls, 0)

    def test_flex_shop_layover_and_duration_drop_contradicting_offers(self) -> None:
        nonstop = _card(stops="Nonstop", duration="1 hr 20 min", price="€90")
        short_hop = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=2.0,
            duration="4 hr",
            price="€70",
        )
        long_hop = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=8.0,
            duration="10 hr",
            price="€40",
        )
        silent = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=None,
            duration="4 hr",
            price="€55",
        )
        long_elapsed = _card(stops="Nonstop", duration="12 hr", price="€35")
        filters = dict(max_layover_hours=3.0, min_layover_hours=1.0, max_duration_hours=5.0)
        self.assertIsNotNone(_normalize_offer(nonstop, 1, **filters))
        self.assertIsNotNone(_normalize_offer(short_hop, 1, **filters))
        self.assertIsNone(_normalize_offer(long_hop, 1, **filters))
        self.assertIsNotNone(_normalize_offer(silent, 1, **filters))
        self.assertIsNone(_normalize_offer(long_elapsed, 1, **filters))
        source = _flex_shop_source(nonstop, short_hop, long_hop, silent, long_elapsed)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            sort="fare",
            **filters,
        )
        self.assertEqual([offer.price_eur for offer in report.offers], [55.0, 70.0, 90.0])
        unnamed = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=_flex_shop_source(nonstop, short_hop, long_hop, silent, long_elapsed),
            buffer_eur=0,
            sort="fare",
        )
        self.assertEqual(
            [offer.price_eur for offer in unnamed.offers],
            [35.0, 40.0, 55.0, 70.0, 90.0],
        )

    def test_flex_layover_filter_does_not_invent_another_calendar_day(self) -> None:
        overnight = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=18.0,
            duration="20 hr",
            price="€40",
        )
        source = _flex_shop_source(overnight)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            max_layover_hours=3.0,
        )
        self.assertEqual(report.chosen_date, date(2026, 9, 12))
        self.assertEqual(report.offers, ())
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(report.days[1].price_eur, 90.0)

    def test_dates_sweep_layover_unprices_days_without_an_eligible_shop(self) -> None:
        short = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=2.0,
            duration="4 hr",
            price="€45",
        )
        overnight = _card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=18.0,
            duration="20 hr",
            price="€30",
        )
        silent = _card(stops="1 stop", layover_hours=None, duration="6 hr", price="€20")
        nonstop = _card(stops="Nonstop", duration="1 hr 20 min", price="€80")
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (short, overnight),
                date(2026, 9, 2): (overnight,),
                date(2026, 9, 3): (silent,),
            },
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 3),
            source=source,
            max_layover_hours=3.0,
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].status, "empty")
        self.assertIsNone(report.days[1].price_eur)
        self.assertEqual(report.days[2].status, "ok")
        self.assertEqual(report.days[2].price_eur, 20.0)
        unnamed_source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (short, overnight),
                date(2026, 9, 2): (overnight,),
                date(2026, 9, 3): (silent,),
            },
        )
        unnamed = search_dates(
            "MAD", "BCN", date(2026, 9, 1), date(2026, 9, 3), source=unnamed_source
        )
        self.assertEqual(unnamed.days[0].price_eur, 30.0)
        self.assertEqual(unnamed.days[1].price_eur, 30.0)
        self.assertEqual(unnamed.days[2].price_eur, 20.0)
        keep_nonstop = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={date(2026, 9, 1): (overnight, nonstop)},
        )
        kept = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 1),
            source=keep_nonstop,
            max_layover_hours=3.0,
        )
        self.assertEqual(kept.days[0].price_eur, 80.0)

    def test_dates_compact_calendar_does_not_invent_a_layover_clock(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            max_layover_hours=3.0,
            min_layover_hours=1.0,
            max_duration_hours=4.0,
        )
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].price_eur, 30.0)
        self.assertEqual(source.fetch_calls, 0)

    def test_dates_compact_calendar_does_not_invent_alliance_members(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            alliances=("star",),
            exclude_alliances=("oneworld",),
        )
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertEqual(report.days[1].price_eur, 30.0)
        self.assertEqual(source.fetch_calls, 0)
        self.assertEqual(source.calendar_queries[0].alliances, ("star",))
        self.assertEqual(source.calendar_queries[0].exclude_alliances, ("oneworld",))

    def test_dates_compact_calendar_occupancy_rides_calendar_trip(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 2),
            source=source,
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertEqual(report.fetch_backend, "calendar")
        seed = source.calendar_queries[0]
        self.assertEqual(seed.adults, 2)
        self.assertEqual(seed.children, 1)
        self.assertEqual(seed.infants_in_seat, 1)
        self.assertEqual(seed.infants_on_lap, 1)
        self.assertEqual(
            build_calendar_inner(seed, date(2026, 9, 1), date(2026, 9, 2))[1][6],
            [2, 1, 1, 1],
        )
        unnamed_source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        unnamed = search_dates(
            "MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=unnamed_source
        )
        unnamed_seed = unnamed_source.calendar_queries[0]
        self.assertEqual(unnamed.fetch_backend, "calendar")
        self.assertEqual(unnamed_seed.adults, 1)
        self.assertEqual(unnamed_seed.children, 0)
        self.assertEqual(unnamed_seed.infants_in_seat, 0)
        self.assertEqual(unnamed_seed.infants_on_lap, 0)
        self.assertEqual(
            build_calendar_inner(unnamed_seed, date(2026, 9, 1), date(2026, 9, 2))[1][6],
            [1, 0, 0, 0],
        )

    def test_dates_named_currency_country_reach_http_source(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_dates(
                "JFK",
                "LHR",
                date(2026, 9, 1),
                date(2026, 9, 2),
                currency="usd",
                country="us",
            )
        ctor.assert_called_once_with(currency="USD", country="US")
        self.assertEqual(report.currency, "USD")

    def test_dates_unnamed_currency_stays_eur_country_omitted(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 30.0),
            )
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_dates("JFK", "LHR", date(2026, 9, 1), date(2026, 9, 2))
        ctor.assert_called_once_with(currency="EUR", country=None)
        self.assertEqual(report.currency, "EUR")

    def test_dates_invalid_currency_or_country_is_rejected_before_fetch(self) -> None:
        with patch("viajante.dates.GoogleFlightsHttpSource") as ctor:
            with self.assertRaises(ValueError):
                search_dates(
                    "JFK",
                    "LHR",
                    date(2026, 9, 1),
                    date(2026, 9, 2),
                    currency="euro",
                )
        ctor.assert_not_called()
        with patch("viajante.dates.GoogleFlightsHttpSource") as ctor:
            with self.assertRaises(ValueError):
                search_dates(
                    "JFK",
                    "LHR",
                    date(2026, 9, 1),
                    date(2026, 9, 2),
                    country="USA",
                )
        ctor.assert_not_called()

    def test_dates_sweep_alliance_rides_the_shopping_request(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€45")
        ryanair = _card(airline="Ryanair", airline_codes=("FR",), price="€30")
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={date(2026, 9, 1): (iberia, ryanair)},
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 1),
            source=source,
            alliances=("star",),
            exclude_alliances=("oneworld",),
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(source.fetch_calls, 1)
        shop = source.fetched_queries[0]
        self.assertEqual(shop.alliances, ("star",))
        self.assertEqual(shop.exclude_alliances, ("oneworld",))
        self.assertEqual(
            build_shopping_inner(shop)[1][13][0][7],
            [None, [["*A"]], [["*O"]]],
        )
        self.assertEqual(report.days[0].price_eur, 30.0)

    def test_dates_sweep_occupancy_rides_the_shopping_request(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€45")
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={date(2026, 9, 1): (iberia,)},
        )
        report = search_dates(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            date(2026, 9, 1),
            source=source,
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(source.fetch_calls, 1)
        shop = source.fetched_queries[0]
        self.assertEqual(shop.adults, 2)
        self.assertEqual(shop.children, 1)
        self.assertEqual(shop.infants_in_seat, 1)
        self.assertEqual(shop.infants_on_lap, 1)
        self.assertEqual(build_shopping_inner(shop)[1][6], [2, 1, 1, 1])
        unnamed_source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={date(2026, 9, 1): (iberia,)},
        )
        search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 1), source=unnamed_source)
        unnamed_shop = unnamed_source.fetched_queries[0]
        self.assertEqual(unnamed_shop.children, 0)
        self.assertEqual(unnamed_shop.infants_in_seat, 0)
        self.assertEqual(unnamed_shop.infants_on_lap, 0)
        self.assertEqual(build_shopping_inner(unnamed_shop)[1][6], [1, 0, 0, 0])

    def test_flex_shop_alliance_rides_the_shopping_request(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€90")
        ryanair = _card(airline="Ryanair", airline_codes=("FR",), price="€40")
        source = _flex_shop_source(iberia, ryanair)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            alliances=("star",),
            exclude_alliances=("oneworld",),
        )
        self.assertEqual(source.fetch_calls, 1)
        shop = source.fetched_queries[0]
        self.assertEqual(shop.alliances, ("star",))
        self.assertEqual(shop.exclude_alliances, ("oneworld",))
        self.assertEqual(
            build_shopping_inner(shop)[1][13][0][7],
            [None, [["*A"]], [["*O"]]],
        )
        self.assertEqual([offer.price_eur for offer in report.offers], [40.0, 90.0])
        self.assertEqual(
            [row.price_eur for row in report.days],
            [180.0, 90.0, 140.0],
        )
        unnamed_source = _flex_shop_source(iberia, ryanair)
        unnamed = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=unnamed_source,
            buffer_eur=0,
        )
        self.assertEqual([offer.price_eur for offer in unnamed.offers], [40.0, 90.0])
        self.assertIsNone(unnamed_source.fetched_queries[0].alliances)
        self.assertIsNone(unnamed_source.fetched_queries[0].exclude_alliances)

    def test_flex_shop_occupancy_rides_the_shopping_request(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€90")
        source = _flex_shop_source(iberia)
        report = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=source,
            buffer_eur=0,
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertEqual(source.fetch_calls, 1)
        seed = source.calendar_queries[0]
        shop = source.fetched_queries[0]
        self.assertEqual(seed.children, 1)
        self.assertEqual(seed.infants_in_seat, 1)
        self.assertEqual(seed.infants_on_lap, 1)
        self.assertEqual(shop.adults, 2)
        self.assertEqual(shop.children, 1)
        self.assertEqual(shop.infants_in_seat, 1)
        self.assertEqual(shop.infants_on_lap, 1)
        self.assertEqual(build_shopping_inner(shop)[1][6], [2, 1, 1, 1])
        self.assertEqual(
            build_calendar_inner(seed, date(2026, 9, 11), date(2026, 9, 13))[1][6],
            [2, 1, 1, 1],
        )
        self.assertEqual([offer.price_eur for offer in report.offers], [90.0])
        unnamed_source = _flex_shop_source(iberia)
        search_flex("MAD", "BCN", date(2026, 9, 12), 1, source=unnamed_source, buffer_eur=0)
        unnamed_shop = unnamed_source.fetched_queries[0]
        self.assertEqual(unnamed_shop.children, 0)
        self.assertEqual(unnamed_shop.infants_in_seat, 0)
        self.assertEqual(unnamed_shop.infants_on_lap, 0)
        self.assertEqual(build_shopping_inner(unnamed_shop)[1][6], [1, 0, 0, 0])

    def test_flex_named_currency_country_reach_http_source(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€90")
        source = _flex_shop_source(iberia)
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_flex(
                "JFK",
                "LHR",
                date(2026, 9, 12),
                1,
                currency="usd",
                country="gb",
                buffer_eur=0,
            )
        ctor.assert_called_once_with(currency="USD", country="GB")
        self.assertEqual(report.currency, "USD")

    def test_flex_unnamed_currency_stays_eur_country_omitted(self) -> None:
        iberia = _card(airline="Iberia", airline_codes=("IB",), price="€90")
        source = _flex_shop_source(iberia)
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source) as ctor:
            report = search_flex("JFK", "LHR", date(2026, 9, 12), 1, buffer_eur=0)
        ctor.assert_called_once_with(currency="EUR", country=None)
        self.assertEqual(report.currency, "EUR")


class FlexCliTests(unittest.TestCase):
    def test_flex_help_mentions_the_window(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["flex", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("--around", help_text)
        self.assertIn("--flex", help_text)
        self.assertIn("--nights", help_text)
        self.assertIn("viajante flex BOS-LHR", help_text)
        self.assertIn(str(MAX_FLEX_DAYS), help_text)
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
        self.assertIn("--max-layover", help_text)
        self.assertIn("--min-layover", help_text)
        self.assertIn("--max-duration", help_text)

    def test_flex_forwards_owned_shop_filters(self) -> None:
        with (
            patch("viajante.cli.search_flex") as search,
            patch("viajante.cli._print_flex_report"),
            patch("viajante.cli._flex_exit_code", return_value=0),
        ):
            code = main(
                [
                    "flex",
                    "MAD-BCN",
                    "--around",
                    "2026-09-12",
                    "--flex",
                    "1",
                    "--bags",
                    "1",
                    "--via",
                    "LIS",
                    "--airlines",
                    "IB",
                    "--alliance",
                    "star",
                    "--exclude-alliance",
                    "oneworld",
                    "--price-cap",
                    "200",
                    "--depart-window",
                    "06:00-20:00",
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
        self.assertEqual(kwargs["via"], ("LIS",))
        self.assertEqual(kwargs["airlines"], ("IB",))
        self.assertEqual(kwargs["alliances"], ("star",))
        self.assertEqual(kwargs["exclude_alliances"], ("oneworld",))
        self.assertEqual(kwargs["price_cap_eur"], 200)
        self.assertEqual(kwargs["depart_window"], (6 * 60, 20 * 60))
        self.assertEqual(kwargs["max_layover_hours"], 3)
        self.assertEqual(kwargs["min_layover_hours"], 1)
        self.assertEqual(kwargs["max_duration_hours"], 8)
        self.assertIsNone(kwargs["carry_on"])
        self.assertIsNone(kwargs["exclude_via"])

    def test_flex_unnamed_shop_filters_stay_unset(self) -> None:
        with (
            patch("viajante.cli.search_flex") as search,
            patch("viajante.cli._print_flex_report"),
            patch("viajante.cli._flex_exit_code", return_value=0),
        ):
            code = main(
                [
                    "flex",
                    "MAD-BCN",
                    "--around",
                    "2026-09-12",
                    "--flex",
                    "1",
                ]
            )
        self.assertEqual(code, 0)
        kwargs = search.call_args.kwargs
        self.assertIsNone(kwargs["bags"])
        self.assertIsNone(kwargs["via"])
        self.assertIsNone(kwargs["airlines"])
        self.assertIsNone(kwargs["alliances"])
        self.assertIsNone(kwargs["exclude_alliances"])
        self.assertIsNone(kwargs["price_cap_eur"])
        self.assertIsNone(kwargs["depart_window"])
        self.assertIsNone(kwargs["max_layover_hours"])
        self.assertIsNone(kwargs["min_layover_hours"])
        self.assertIsNone(kwargs["max_duration_hours"])
        self.assertFalse(kwargs["nearby"])
        self.assertEqual(kwargs["children"], 0)
        self.assertEqual(kwargs["infants_in_seat"], 0)
        self.assertEqual(kwargs["infants_on_lap"], 0)
        self.assertEqual(kwargs["currency"], "EUR")
        self.assertIsNone(kwargs["country"])

    def test_flex_forwards_named_occupancy(self) -> None:
        with (
            patch("viajante.cli.search_flex") as search,
            patch("viajante.cli._print_flex_report"),
            patch("viajante.cli._flex_exit_code", return_value=0),
        ):
            code = main(
                [
                    "flex",
                    "MAD-BCN",
                    "--around",
                    "2026-09-12",
                    "--flex",
                    "1",
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

    def test_flex_forwards_named_currency_country(self) -> None:
        with (
            patch("viajante.cli.search_flex") as search,
            patch("viajante.cli._print_flex_report"),
            patch("viajante.cli._flex_exit_code", return_value=0),
        ):
            code = main(
                [
                    "flex",
                    "JFK-LHR",
                    "--around",
                    "2026-09-12",
                    "--flex",
                    "1",
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

    def test_past_around_is_rejected_before_search(self) -> None:
        with patch("viajante.cli.search_flex") as search:
            code = main(
                [
                    "flex",
                    "BOS-LHR",
                    "--around",
                    "2020-01-01",
                    "--flex",
                    "3",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_prints_chosen_day_and_fare(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 9), 520.0, date(2026, 9, 16)),
                CompactCalendarDay(date(2026, 9, 10), 388.0, date(2026, 9, 17)),
                CompactCalendarDay(date(2026, 9, 11), 410.0, date(2026, 9, 18)),
            ),
            cards={
                date(2026, 9, 10): (
                    RawFlightCard(
                        airline="British Airways",
                        departure="18:00",
                        arrival="06:00",
                        duration="7 hr",
                        stops="Nonstop",
                        price="€350",
                    ),
                ),
            },
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "flex",
                        "BOS-LHR",
                        "--around",
                        "2026-09-12",
                        "--flex",
                        "3",
                        "--nights",
                        "7",
                        "--baggage-buffer",
                        "0",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("BOS -> LHR", output)
        self.assertIn("around 2026-09-12", output)
        self.assertIn("chosen 2026-09-10", output)
        self.assertIn("350 €", output)
        self.assertIn("rt, 7 nights", output)


class NearbyDateFlexTests(unittest.TestCase):
    def test_dates_nearby_expands_london_and_default_keeps_heathrow(self) -> None:
        priced = (CompactCalendarDay(date(2026, 9, 1), 80.0),)
        off_source = FakeCalendarSource(priced)
        off = search_dates("BOS", "LHR", date(2026, 9, 1), date(2026, 9, 1), source=off_source)
        self.assertIsInstance(off, DateCalendarReport)
        self.assertEqual((off.origin, off.destination), ("BOS", "LHR"))
        self.assertIsNone(off.nearby_label)
        self.assertEqual(off_source.calls, 1)
        self.assertEqual(off_source.calendar_queries[0].destination, "LHR")

        on_source = FakeCalendarSource(priced)
        reports = search_dates(
            "BOS", "LHR", date(2026, 9, 1), date(2026, 9, 1), nearby=True, source=on_source
        )
        self.assertIsInstance(reports, tuple)
        pairs = [(row.origin, row.destination) for row in reports]
        self.assertEqual(pairs[0], ("BOS", "LHR"))
        dests = {dest for _origin, dest in pairs}
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= dests)
        self.assertNotIn("BQH", dests)
        self.assertTrue(all(row.origin == "BOS" for row in reports))
        self.assertTrue(all(row.nearby_label for row in reports))
        self.assertNotIn("nearby_label", reports[0].to_dict())
        self.assertEqual({query.destination for query in on_source.calendar_queries}, dests)

    def test_dates_nearby_unknown_city_does_not_invent_codes(self) -> None:
        source = FakeCalendarSource((CompactCalendarDay(date(2026, 9, 1), 40.0),))
        report = search_dates(
            "MAD", "BCN", date(2026, 9, 1), date(2026, 9, 1), nearby=True, source=source
        )
        self.assertIsInstance(report, DateCalendarReport)
        self.assertEqual((report.origin, report.destination), ("MAD", "BCN"))
        self.assertIsNone(report.nearby_label)
        self.assertEqual(source.calls, 1)

    def test_flex_nearby_expands_london_and_default_keeps_heathrow(self) -> None:
        off_source = _flex_shop_source(_card(price="€90"))
        off = search_flex("BOS", "LHR", date(2026, 9, 12), 3, source=off_source, buffer_eur=0)
        self.assertIsInstance(off, FlexSearchReport)
        self.assertEqual((off.origin, off.destination), ("BOS", "LHR"))
        self.assertIsNone(off.nearby_label)
        self.assertEqual(off_source.calls, 1)
        self.assertEqual(off_source.fetch_calls, 1)

        on_source = _flex_shop_source(_card(price="€90"))
        reports = search_flex(
            "BOS", "LHR", date(2026, 9, 12), 3, nearby=True, source=on_source, buffer_eur=0
        )
        self.assertIsInstance(reports, tuple)
        dests = {row.destination for row in reports}
        self.assertEqual(reports[0].destination, "LHR")
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= dests)
        self.assertNotIn("BQH", dests)
        self.assertTrue(all(row.origin == "BOS" for row in reports))
        self.assertTrue(all(row.nearby_label for row in reports))
        self.assertNotIn("nearby_label", reports[0].to_dict())
        self.assertEqual(on_source.calls, len(reports))
        self.assertEqual(on_source.fetch_calls, len(reports))
        self.assertEqual({query.destination for query in on_source.calendar_queries}, dests)

    def test_flex_nearby_unknown_city_does_not_invent_codes(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 11), 80.0),
                CompactCalendarDay(date(2026, 9, 12), 70.0),
                CompactCalendarDay(date(2026, 9, 13), 90.0),
            ),
            cards={date(2026, 9, 12): (_card(price="€70"),)},
        )
        report = search_flex(
            "MAD", "BCN", date(2026, 9, 12), 1, nearby=True, source=source, buffer_eur=0
        )
        self.assertIsInstance(report, FlexSearchReport)
        self.assertEqual((report.origin, report.destination), ("MAD", "BCN"))
        self.assertIsNone(report.nearby_label)
        self.assertEqual(source.calls, 1)


class StopsCompareShopParityTests(unittest.TestCase):
    def test_flex_shop_stamps_compare_from_eligible_offers(self) -> None:
        source = _flex_shop_source(
            _card(airline="Iberia", price="€88", stops="Nonstop"),
            _card(
                airline="Ryanair",
                price="€49",
                duration="5 hr",
                stops="1 stop",
                layover_city="OPO",
                layover_hours=2.0,
            ),
            _card(airline="Vueling", price="€120", stops="Nonstop"),
        )
        report = search_flex("MAD", "BCN", date(2026, 9, 12), 1, source=source, buffer_eur=0)
        compare = report.stops_compare
        assert compare is not None
        assert compare.nonstop is not None
        assert compare.one_stop is not None
        self.assertEqual(compare.nonstop.price_eur, 88.0)
        self.assertEqual(compare.nonstop.airline, "Iberia")
        self.assertEqual(compare.one_stop.price_eur, 49.0)
        self.assertEqual(compare.one_stop.airline, "Ryanair")
        self.assertEqual(compare.one_stop.layover_city, "OPO")
        payload = report.to_dict()["stops_compare"]
        self.assertEqual(payload["nonstop"]["price_eur"], 88.0)
        self.assertEqual(payload["one_stop"]["price_eur"], 49.0)
        self.assertNotIn("stops_compare", report.days[0].to_dict())

    def test_flex_calendar_miss_and_empty_window_omit_compare(self) -> None:
        miss = search_flex(
            "BOS",
            "LHR",
            date(2026, 9, 12),
            3,
            source=FakeCalendarSource(CompactParseMiss("no wrb.fr calendar payload")),
        )
        self.assertEqual(miss.offers, ())
        self.assertIsNone(miss.stops_compare)
        self.assertNotIn("stops_compare", miss.to_dict())
        empty = search_flex(
            "JFK",
            "LHR",
            date(2026, 9, 12),
            3,
            source=FakeCalendarSource(
                (
                    CompactCalendarDay(date(2026, 9, 9), None),
                    CompactCalendarDay(date(2026, 9, 12), None),
                    CompactCalendarDay(date(2026, 9, 15), None),
                )
            ),
        )
        self.assertEqual(empty.offers, ())
        self.assertIsNone(empty.stops_compare)
        self.assertNotIn("stops_compare", empty.to_dict())

    def test_flex_omits_empty_side_and_block(self) -> None:
        only_nonstop = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=_flex_shop_source(_card(airline="Iberia", price="€88", stops="Nonstop")),
            buffer_eur=0,
        )
        assert only_nonstop.stops_compare is not None
        self.assertEqual(only_nonstop.stops_compare.nonstop.price_eur, 88.0)
        self.assertIsNone(only_nonstop.stops_compare.one_stop)
        self.assertEqual(set(only_nonstop.to_dict()["stops_compare"]), {"nonstop"})
        only_one = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            source=_flex_shop_source(
                _card(
                    airline="Ryanair",
                    price="€49",
                    duration="5 hr",
                    stops="1 stop",
                    layover_city="OPO",
                )
            ),
            buffer_eur=0,
        )
        assert only_one.stops_compare is not None
        self.assertIsNone(only_one.stops_compare.nonstop)
        self.assertEqual(only_one.stops_compare.one_stop.price_eur, 49.0)
        self.assertEqual(set(only_one.to_dict()["stops_compare"]), {"one_stop"})
        two_stop_only = search_flex(
            "MAD",
            "BCN",
            date(2026, 9, 12),
            1,
            max_stops=2,
            source=_flex_shop_source(
                _card(airline="China Southern", price="€314", duration="21 hr", stops="2 stops")
            ),
            buffer_eur=0,
        )
        self.assertEqual(two_stop_only.offers[0].price_eur, 314.0)
        self.assertIsNone(two_stop_only.stops_compare)
        self.assertNotIn("stops_compare", two_stop_only.to_dict())

    def test_flex_ranked_top_hides_slow_one_stop_that_compare_keeps(self) -> None:
        source = _flex_shop_source(
            _card(
                airline="Iberia",
                price="€88",
                duration="1 hr 20 min",
                stops="Nonstop",
                departure="09:30",
                arrival="10:50",
            ),
            _card(
                airline="Air Europa",
                price="€69",
                duration="21 hr",
                stops="1 stop",
                layover_city="Palma",
                layover_hours=18.0,
                departure="21:00",
                arrival="18:00",
            ),
        )
        report = search_flex("MAD", "BCN", date(2026, 9, 12), 1, source=source, buffer_eur=0, top=8)
        self.assertEqual([offer.airline for offer in report.offers], ["Iberia"])
        compare = report.stops_compare
        assert compare is not None
        assert compare.nonstop is not None
        assert compare.one_stop is not None
        self.assertEqual(compare.one_stop.airline, "Air Europa")
        self.assertEqual(compare.one_stop.price_eur, 69.0)

    def test_dates_compact_calendar_omits_compare(self) -> None:
        source = FakeCalendarSource(
            (
                CompactCalendarDay(date(2026, 9, 1), 45.0),
                CompactCalendarDay(date(2026, 9, 2), 38.0),
            )
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=source)
        self.assertEqual(report.fetch_backend, "calendar")
        self.assertEqual(report.days[0].price_eur, 45.0)
        self.assertIsNone(report.days[0].stops_compare)
        self.assertIsNone(report.days[1].stops_compare)
        self.assertNotIn("stops_compare", report.days[0].to_dict())
        self.assertNotIn("stops_compare", report.to_dict())
        cell = DatePriceRow(departure_date=date(2026, 9, 1), price_eur=45.0, stops_count=0)
        self.assertIsNone(cell.stops_compare)
        self.assertNotIn("stops_compare", cell.to_dict())

    def test_dates_sweep_stamps_compare_from_that_day_shop(self) -> None:
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (
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
                date(2026, 9, 2): (_card(airline="Iberia", price="€38", stops="Nonstop"),),
            },
        )
        report = search_dates("MAD", "BCN", date(2026, 9, 1), date(2026, 9, 2), source=source)
        self.assertEqual(report.fetch_backend, "sweep")
        first = report.days[0].stops_compare
        assert first is not None
        assert first.nonstop is not None
        assert first.one_stop is not None
        self.assertEqual(first.nonstop.price_eur, 88.0)
        self.assertEqual(first.one_stop.price_eur, 49.0)
        self.assertEqual(report.days[0].price_eur, 49.0)
        second = report.days[1].stops_compare
        assert second is not None
        self.assertEqual(second.nonstop.price_eur, 38.0)
        self.assertIsNone(second.one_stop)
        self.assertEqual(set(report.days[1].to_dict()["stops_compare"]), {"nonstop"})
        self.assertNotIn("stops_compare", report.to_dict())

    def test_dates_sweep_omits_block_when_both_buckets_empty(self) -> None:
        source = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (
                    _card(
                        airline="China Southern",
                        price="€314",
                        duration="21 hr",
                        stops="2 stops",
                    ),
                ),
            },
        )
        report = search_dates(
            "MAD", "BCN", date(2026, 9, 1), date(2026, 9, 1), max_stops=2, source=source
        )
        self.assertEqual(report.days[0].price_eur, 314.0)
        self.assertEqual(report.days[0].stops_count, 2)
        self.assertIsNone(report.days[0].stops_compare)
        self.assertNotIn("stops_compare", report.days[0].to_dict())

    def test_flex_cli_prints_compare(self) -> None:
        source = _flex_shop_source(
            _card(airline="Iberia", price="€88", stops="Nonstop"),
            _card(airline="Ryanair", price="€49", duration="5 hr", stops="1 stop"),
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=source):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "flex",
                        "MAD-BCN",
                        "--around",
                        "2026-09-12",
                        "--flex",
                        "1",
                        "--baggage-buffer",
                        "0",
                    ]
                )
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Cheapest nonstop:", output)
        self.assertIn("88 €", output)
        self.assertIn("Cheapest 1-stop:", output)
        self.assertIn("49 €", output)

    def test_dates_cli_omits_compare_on_compact_and_prints_it_on_sweep(self) -> None:
        compact = FakeCalendarSource((CompactCalendarDay(date(2026, 9, 1), 45.0),))
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=compact):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["dates", "MAD-BCN", "--from", "2026-09-01", "--to", "2026-09-01"])
        self.assertEqual(code, 0)
        self.assertNotIn("Cheapest nonstop:", buffer.getvalue())
        sweep = FakeCalendarSource(
            CompactParseMiss("no wrb.fr calendar payload"),
            cards={
                date(2026, 9, 1): (
                    _card(airline="Iberia", price="€88", stops="Nonstop"),
                    _card(airline="Ryanair", price="€49", duration="5 hr", stops="1 stop"),
                ),
            },
        )
        with patch("viajante.dates.GoogleFlightsHttpSource", return_value=sweep):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["dates", "MAD-BCN", "--from", "2026-09-01", "--to", "2026-09-01"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("Cheapest nonstop:", output)
        self.assertIn("Cheapest 1-stop:", output)
        self.assertIn("49 €", output)


if __name__ == "__main__":
    unittest.main()
