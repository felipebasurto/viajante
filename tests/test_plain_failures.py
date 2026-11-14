"""Plain next-step sentences for failures, partial calendars, metros, and bags."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from io import StringIO
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.cli import _cli_parser, _owned_shop_filters_from_args, main
from viajante.cli_report import _print_dates_report
from viajante.dates import format_week_calendar, search_dates
from viajante.envelope import EMPTY_NOTES, stamp_search
from viajante.flight_routes import metro_fanout_warning, parse_flight_plan
from viajante.google_capabilities import (
    CHECKED_BAGS_REFUSAL,
    GoogleFlightsUnsupported,
    validate_capabilities,
)
from viajante.google_flights import GoogleFlightsRejected
from viajante.google_flights_public import classify_failure
from viajante.mcp_server import _tool_error_text
from viajante.models import FlightQuery
from viajante.models_common import SearchError, SearchErrorCode
from viajante.models_dates_explore import DateCalendarReport, DatePriceRow
from viajante.orchestration import classify_failure as classify_provider_failure
from viajante.quote import cash_currency_for_origin
from viajante.ratelimit import note_repeated_throttle, rate_limit_status

DAY = date(2027, 6, 7)


class CalendarNoteTests(unittest.TestCase):
    def _report(self) -> DateCalendarReport:
        paused = SearchError(
            code=SearchErrorCode.BLOCKED,
            message="Google Flights paused this request.",
            diagnostics={"rpc_status": 13},
        )
        days = (
            DatePriceRow(date(2027, 6, 7), price=100),
            DatePriceRow(date(2027, 6, 8), price=110),
            DatePriceRow(date(2027, 6, 9), price=120),
            DatePriceRow(date(2027, 6, 10), status="error", error=paused),
            DatePriceRow(date(2027, 6, 11), status="empty", empty_reason="not_loaded"),
        )
        return DateCalendarReport(
            searched_at=datetime(2027, 6, 1, 12, 0, 0),
            origin="JFK",
            destination="LHR",
            start_date=date(2027, 6, 7),
            end_date=date(2027, 6, 11),
            days=days,
            currency="USD",
        )

    def test_a_calendar_with_a_failed_day_is_not_a_plain_ok(self) -> None:
        payload = stamp_search(self._report().to_dict())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["completeness"], "partial")
        self.assertIsNone(payload["empty_reason"])
        note = payload["completeness_note"]
        self.assertIn("1 of 5 dates did not load", note)
        self.assertIn("paused the request", note)
        self.assertIn("unknown, not unsold", note)
        self.assertIn("15-30", note)
        self.assertNotIn("status 13", note.casefold())

    def test_the_grid_marks_a_failed_day_and_prints_the_sentence_above_the_summary(self) -> None:
        report = self._report()
        grid = "\n".join(format_week_calendar(report.days))
        self.assertIn("!", grid)
        self.assertIn("·", grid)
        out = StringIO()
        with redirect_stdout(out):
            _print_dates_report(report)
        text = out.getvalue()
        self.assertLess(text.index("did not load"), text.index("  min "))
        self.assertIn("—  The search did not complete", text)
        self.assertNotIn("not_loaded", text)


class PauseAndMetroTests(unittest.TestCase):
    def setUp(self) -> None:
        self._state = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self._state.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._state.cleanup)

    def test_hits_persist_after_the_process_memory_is_cleared(self) -> None:
        self.assertIsNone(note_repeated_throttle())
        self.assertIsNone(note_repeated_throttle())
        self.assertIsNone(rate_limit_status())
        from viajante import ratelimit

        ratelimit._throttle_hits.clear()
        state = note_repeated_throttle()
        self.assertIsNotNone(state)
        self.assertEqual(rate_limit_status()["basis"], "heuristic_rpc_13")

    def test_a_city_code_on_a_calendar_names_the_airports(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            search_dates("LON", "JFK", DAY, DAY)
        message = str(ctx.exception)
        self.assertIn("LON is a city code covering LHR, LGW, STN, LTN, LCY, SEN", message)
        self.assertIn("Name one airport for a calendar, or use search_flights.", message)
        self.assertNotIn("unknown origin", message)

    def test_currency_for_a_city_code_uses_that_citys_country(self) -> None:
        self.assertEqual(cash_currency_for_origin("LON"), "GBP")
        self.assertEqual(cash_currency_for_origin("NYC"), "USD")

    def test_a_flight_metro_warning_counts_the_google_requests(self) -> None:
        trips = parse_flight_plan(["JFK-LON:2027-06-07"], max_stops=1)
        warning = metro_fanout_warning(trips)
        self.assertIn("LON is a city code covering LHR, LGW, STN, LTN, LCY, SEN", warning)
        self.assertIn("6 Google requests", warning)
        self.assertIn("LHR or LGW", warning)


class PlainSentenceTests(unittest.TestCase):
    def test_empty_notes_name_a_next_step(self) -> None:
        self.assertIn("Try other dates or airports", EMPTY_NOTES["provider_empty"])
        self.assertIn("fewer filters", EMPTY_NOTES["filtered_out"])
        self.assertIn("Don't search again until then", EMPTY_NOTES["not_loaded"])

    def test_checked_bags_are_refused_in_plain_words(self) -> None:
        query = FlightQuery("JFK", "LHR", DAY, bags=2)
        with self.assertRaises(GoogleFlightsUnsupported) as ctx:
            validate_capabilities(query)
        self.assertEqual(str(ctx.exception), CHECKED_BAGS_REFUSAL)
        self.assertIn("carry_on=2", CHECKED_BAGS_REFUSAL)

    def test_carry_on_count_and_the_bare_flag(self) -> None:
        parser = _cli_parser()

        def carry(argv: list[str]) -> object:
            return _owned_shop_filters_from_args(parser.parse_args(argv))["carry_on"]

        route = ["flights", "JFK-LHR:2027-06-07"]
        self.assertEqual(carry(route + ["--carry-on", "2"]), 2)
        self.assertEqual(carry(route + ["--carry-on"]), 1)
        self.assertEqual(carry(route + ["--carry-on", "0"]), 0)
        self.assertIsNone(carry(route))

    def test_timeout_connect_and_rejected_name_a_next_step(self) -> None:
        timeout = classify_provider_failure(TimeoutError("Timeout 60000ms exceeded"))
        self.assertIn("Wait a minute", timeout.message)
        self.assertNotIn("TimeoutError", timeout.message)

        class ConnectError(OSError):
            pass

        connect = classify_provider_failure(ConnectError("connection reset by peer"))
        self.assertIn("Could not reach the provider", connect.message)
        rejected = classify_failure(GoogleFlightsRejected("unknown airport"))
        self.assertIn("try one airport", rejected.message)
        self.assertNotIn("unknown airport", rejected.message)

    def test_an_unexpected_mcp_error_is_a_json_body(self) -> None:
        text = _tool_error_text("search_flights", RuntimeError("boom"))
        body = json.loads(text.removeprefix("Error executing tool search_flights: "))
        self.assertEqual(body["error"]["code"], "internal")
        self.assertNotIn("RuntimeError", body["error"]["message"])
        self.assertIn("Wait a minute", body["error"]["message"])

    def test_the_cli_hides_a_traceback_unless_debug_is_set(self) -> None:
        def explode(_args: object) -> int:
            raise RuntimeError("secret marker")

        err = StringIO()
        with patch("viajante.cli._run_flights", explode), redirect_stderr(err):
            code = main(["flights", "JFK-LHR:2027-06-07"])
        self.assertEqual(code, 1)
        self.assertIn("unexpected error", err.getvalue())
        self.assertNotIn("secret marker", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

        err = StringIO()
        with patch("viajante.cli._run_flights", explode), redirect_stderr(err):
            code = main(["--debug", "flights", "JFK-LHR:2027-06-07"])
        self.assertEqual(code, 1)
        self.assertIn("Traceback", err.getvalue())
        self.assertIn("secret marker", err.getvalue())
