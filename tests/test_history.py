"""Offline tests for the opt-in local price history and the on-demand watch."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import _isolate  # noqa: F401
from test_recheck import OUTBOUND, _previous
from test_recheck import _offer as _recheck_offer
from test_recheck import _report as _recheck_report
from viajante import history, mcp_handlers, watch
from viajante.cli import main
from viajante.envelope import ENVELOPE_KEYS
from viajante.evidence import _Owned
from viajante.flights import DEFAULT_TOP, search_flights
from viajante.mcp_errors import structured_error
from viajante.mcp_guide import GUIDE, INSTRUCTIONS
from viajante.mcp_handlers import search_flights_tool
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightOffer,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    QueryFailure,
    QuerySuccess,
    SearchError,
    SearchErrorCode,
    SearchReport,
)

DEPART = date.today() + timedelta(days=60)
T0 = datetime(2026, 10, 6, 9, 0, 0, tzinfo=timezone.utc)


def _offer(price: float, airline: str = "Acme Air") -> FlightOffer:
    return FlightOffer(
        airline=airline,
        departure="08:00",
        arrival="20:00",
        price_text=f"{price:.0f} US$",
        price=price,
        duration="7 h",
        duration_hours=7.0,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer=0,
        needs_bag_verify=False,
    )


def _flight_report(
    *prices: float,
    currency: str = "USD",
    at: datetime = T0,
    adults: int = 1,
    origin: str = "JFK",
    destination: str = "LHR",
) -> SearchReport:
    query = FlightQuery(origin, destination, DEPART, adults=adults)
    if prices:
        result = QuerySuccess(
            query, len(prices), len(prices), tuple(_offer(price) for price in prices)
        )
    else:
        error = SearchError(SearchErrorCode.NO_RESULTS, "nothing")
        result = QueryFailure(query, error)
    return SearchReport(searched_at=at, queries=(result,), currency=currency)


def _hotel_report(total: float, *, currency: str = "GBP", at: datetime = T0) -> HotelSearchReport:
    query = HotelQuery(
        "Lisbon", date.today() + timedelta(days=60), date.today() + timedelta(60 + 3)
    )
    applied = AppliedHotelFilters(chips=(), url="https://example.test/h")
    offer = HotelOffer(
        title="Casa Azul",
        address=None,
        total_price_text=f"{total:.0f} GBP",
        total_price=total,
        rating=None,
        rating_score=None,
        details="",
        cancellation_evidence=CancellationEvidence.FREE,
        property_type_evidence=PropertyTypeEvidence.UNKNOWN,
        lodging_kind=LodgingKind.UNKNOWN,
        bedrooms=None,
        bathrooms=None,
        beds=None,
        link=None,
    )
    ok = HotelQuerySuccess(query, applied, 3, 2, (offer,))
    miss = HotelQueryFailure(query, applied, SearchError(SearchErrorCode.BLOCKED, "blocked"))
    return HotelSearchReport(
        searched_at=at, queries=(ok, miss), currency=currency, provider="google-hotels"
    )


class _State(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(history.ENV_RECORD, None)
        mcp_handlers._CACHE.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)

    def record_flights(self, report: SearchReport, **kwargs: object) -> SearchReport:
        def fake(queries, *, top=3, sort="ranked", baggage_buffer=None, via=None, **rest):
            return report

        return history.recorded_flights(fake)(report.queries, **kwargs)

    def enable(self) -> None:
        env = patch.dict(os.environ, {history.ENV_RECORD: "1"})
        env.start()
        self.addCleanup(env.stop)


class RecordingGateTests(_State):
    def test_off_by_default_writes_nothing(self) -> None:
        self.record_flights(_flight_report(500.0))
        self.assertFalse((self.state / history.HISTORY_FILE).exists())

    def test_zero_and_unset_do_not_enable(self) -> None:
        with patch.dict(os.environ, {history.ENV_RECORD: "0"}):
            self.record_flights(_flight_report(500.0))
        self.assertEqual(history.read_observations(), [])

    def test_env_records_only_real_priced_results(self) -> None:
        self.enable()
        report = self.record_flights(_flight_report(540.0, 500.0))
        self.assertIsInstance(report, SearchReport)
        (row,) = history.read_observations()
        self.assertEqual(
            (row["kind"], row["currency"], row["cheapest"], row["offers"], row["provider"]),
            ("flight", "USD", 500.0, 2, "google-flights"),
        )
        self.assertEqual(row["observed_at"], "2026-10-06T09:00:00Z")
        self.assertEqual(row["query"]["origin"], "JFK")
        self.assertEqual(row["query"]["adults"], 1)
        self.assertEqual(row["cheapest_text"], "500 US$")
        self.assertNotIn("proxy", json.dumps(row))

    def test_failures_and_empty_results_are_not_observations(self) -> None:
        self.enable()
        self.record_flights(_flight_report())
        self.assertEqual(history.read_observations(), [])

    def test_hotels_record_only_successful_queries(self) -> None:
        self.enable()

        def fake(queries, *, top=3, source="booking", near=None, max_distance_km=None):
            return _hotel_report(900.0)

        history.recorded_hotels(fake)((), source="google")
        (row,) = history.read_observations()
        self.assertEqual((row["kind"], row["currency"], row["cheapest"]), ("hotel", "GBP", 900.0))
        self.assertEqual(row["cheapest_label"], "Casa Azul")
        self.assertEqual(row["provider"], "google-hotels")
        self.assertEqual(row["filters"]["source"], "google")

    def test_recording_failure_never_loses_the_search(self) -> None:
        self.enable()
        report = _flight_report(500.0)
        err = io.StringIO()
        with (
            patch("viajante.history.append_observations", side_effect=OSError("disk full")),
            contextlib.redirect_stderr(err),
        ):
            self.assertIs(self.record_flights(report), report)
        self.assertIn("not recorded", err.getvalue())

    def test_real_search_flights_records_with_defaults_applied(self) -> None:
        self.enable()
        report = _flight_report(500.0)
        query = FlightQuery("JFK", "LHR", DEPART)
        with (
            patch("viajante.flights._run_search", return_value=report),
            patch("viajante.flights.GoogleFlightsHttpSource", MagicMock()),
            patch("viajante.flights.playwright_available", return_value=False),
        ):
            search_flights([query], fetch="sweep", currency="USD")
            search_flights([query], fetch="sweep", currency="USD", top=DEFAULT_TOP, sort="ranked")
            search_flights([query], fetch="sweep", currency="USD", top=DEFAULT_TOP + 2)
        rows = history.read_observations()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["query_key"], rows[1]["query_key"])
        self.assertNotEqual(rows[0]["query_key"], rows[2]["query_key"])


class ImmutabilityAndBoundsTests(_State):
    def test_entries_are_never_rewritten(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        before = (self.state / history.HISTORY_FILE).read_text().splitlines()
        history.read_observations()[0]["cheapest"] = 1.0
        self.record_flights(_flight_report(450.0, at=T0 + timedelta(days=1)))
        after = (self.state / history.HISTORY_FILE).read_text().splitlines()
        self.assertEqual(after[0], before[0])
        self.assertEqual(len(after), 2)
        self.assertEqual(history.read_observations()[0]["cheapest"], 500.0)

    def test_cap_drops_only_the_oldest(self) -> None:
        self.enable()
        with patch.object(history, "MAX_ENTRIES", 3):
            for index in range(5):
                self.record_flights(_flight_report(100.0 + index, at=T0 + timedelta(hours=index)))
        self.assertEqual(
            [r["cheapest"] for r in history.read_observations()], [102.0, 103.0, 104.0]
        )

    def test_unreadable_lines_are_skipped(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        path = self.state / history.HISTORY_FILE
        path.write_text("not json\n" + path.read_text(), encoding="utf-8")
        self.assertEqual(len(history.read_observations()), 1)

    def test_one_bad_byte_costs_only_its_own_line(self) -> None:
        self.enable()
        for hours in range(5):
            self.record_flights(_flight_report(500.0 + hours, at=T0 + timedelta(hours=hours)))
        path = self.state / history.HISTORY_FILE
        lines = path.read_bytes().split(b"\n")
        lines[2] = b"\xff" + lines[2]
        path.write_bytes(b"\n".join(lines))
        self.assertEqual(len(history.read_observations()), 4)

    def test_append_never_drops_lines_it_cannot_parse(self) -> None:
        self.enable()
        for hours in range(5):
            self.record_flights(_flight_report(500.0 + hours, at=T0 + timedelta(hours=hours)))
        path = self.state / history.HISTORY_FILE
        lines = path.read_bytes().split(b"\n")
        lines[1] = b"\xff\xfe broken"
        path.write_bytes(b"\n".join(lines))
        broken = lines[1]
        self.record_flights(_flight_report(400.0, at=T0 + timedelta(days=1)))
        after = path.read_bytes().split(b"\n")
        self.assertIn(broken, after)
        self.assertEqual(len(history.read_observations()), 5)
        self.assertEqual(after[:1], lines[:1])

    def test_cap_counts_only_valid_entries_and_keeps_unreadable_lines(self) -> None:
        self.enable()
        path = self.state / history.HISTORY_FILE
        with patch.object(history, "MAX_ENTRIES", 2):
            self.record_flights(_flight_report(100.0, at=T0))
            path.write_bytes(b"\xff garbage\n" + path.read_bytes())
            for hours in (1, 2, 3):
                self.record_flights(_flight_report(100.0 + hours, at=T0 + timedelta(hours=hours)))
        self.assertEqual([r["cheapest"] for r in history.read_observations()], [102.0, 103.0])
        self.assertIn(b"\xff garbage", path.read_bytes())

    def test_undecodable_file_reads_as_empty(self) -> None:
        (self.state / history.HISTORY_FILE).write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(history.read_observations(), [])

    def test_hand_edited_rows_cannot_crash_readers(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        good = history.read_observations()[0]
        bad = [
            {**good, "cheapest": "cheap"},
            {**good, "cheapest": 0},
            {**good, "cheapest": -5},
            {**good, "cheapest": True},
            {**good, "cheapest": float("nan")},
            {**good, "query": "JFK-LHR"},
            {**good, "filters": []},
            {**good, "currency": None},
            [1, 2],
        ]
        path = self.state / history.HISTORY_FILE
        lines = [json.dumps(row) for row in bad] + [path.read_text().strip()]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual([row["id"] for row in history.read_observations()], [good["id"]])
        self.assertEqual(len(history.price_history()["series"]), 1)

    def test_clear_removes_the_log_and_reports_the_count(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        self.record_flights(_flight_report(510.0, at=T0 + timedelta(days=1)))
        self.assertEqual(history.clear_history(), (2, 0))
        self.assertEqual(history.read_observations(), [])
        self.assertFalse((self.state / history.HISTORY_FILE).exists())
        self.assertEqual(history.clear_history(), (0, 0))


def _is_root() -> bool:
    return getattr(os, "geteuid", lambda: 0)() == 0


@unittest.skipIf(_is_root(), "chmod 000 does not stop root")
class UnreadableFileTests(_State):
    def setUp(self) -> None:
        super().setUp()
        self.enable()
        for hours in range(5):
            self.record_flights(_flight_report(500.0 + hours, at=T0 + timedelta(hours=hours)))
        self.path = self.state / history.HISTORY_FILE
        self.before = self.path.read_bytes()
        self.path.chmod(0)
        self.addCleanup(self.path.chmod, 0o600)

    def test_append_keeps_the_file_and_reports_the_read_error(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.record_flights(_flight_report(400.0, at=T0 + timedelta(days=1)))
        self.assertIn("not recorded", err.getvalue())
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_watch_says_accessing_not_writing(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            result = _watch_once(450.0)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["recording_error"], "permission denied accessing price history")
        self.assertEqual(result["read_error"], "permission denied accessing price history")
        (item,) = result["results"]
        self.assertIsNone(item["change"])
        self.assertFalse(item["recorded"])
        self.assertEqual(item["note"], "history could not be read; no comparison")
        self.assertNotIn("First observation", json.dumps(result))
        self.assertIn("recording failed: permission denied accessing price history", result["note"])
        self.assertNotIn(str(self.state), json.dumps(result))
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_reader_does_not_present_it_as_empty_history(self) -> None:
        payload = history.price_history()
        self.assertEqual(payload["read_error"], "permission denied accessing price history")
        self.assertIn("not an empty history", payload["note"])
        self.assertIsNone(payload["stored_entries"])
        with self.assertRaises(history.HistoryReadError):
            history.read_observations(strict=True)

    def test_clear_refuses_and_keeps_the_file(self) -> None:
        with self.assertRaises(history.HistoryReadError):
            history.clear_history()
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_cli_history_and_clear_fail_without_touching_the_file(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["history"]), 1)
            self.assertEqual(main(["history", "--clear"]), 1)
        self.assertIn("could not be read", err.getvalue())
        self.assertIn("could not clear the price history", err.getvalue())
        self.path.chmod(0o600)
        self.assertEqual(self.path.read_bytes(), self.before)


def _watch_once(price: float) -> dict:
    report = _flight_report(price, at=T0 + timedelta(days=2))
    stub = history.recorded_flights(
        lambda queries, *, top=3, sort="ranked", baggage_buffer=None, **rest: report
    )
    params = {"routes": [f"JFK-LHR:{DEPART.isoformat()}"], "currency": "USD"}
    with patch("viajante.mcp_handlers.search_flights", stub):
        return watch.watch_price_tool("nyc-lon", kind="flights", params=params)


class TrendTests(_State):
    def setUp(self) -> None:
        super().setUp()
        self.enable()

    def test_single_observation_says_so_and_has_no_change(self) -> None:
        self.record_flights(_flight_report(500.0))
        (item,) = history.price_history()["series"]
        trend = item["trend"]
        self.assertEqual(trend["observation_count"], 1)
        self.assertIsNone(trend["change_since_previous"])
        self.assertIn("Only one observation", trend["note"])
        self.assertEqual(trend["first_seen"], trend["last_seen"])

    def test_facts_for_one_comparable_series(self) -> None:
        for hours, price in enumerate((500.0, 430.0, 610.0, 580.0)):
            self.record_flights(_flight_report(price, at=T0 + timedelta(hours=hours)))
        (item,) = history.price_history()["series"]
        trend = item["trend"]
        self.assertEqual(trend["first_seen"]["cheapest"], 500.0)
        self.assertEqual(trend["last_seen"]["cheapest"], 580.0)
        self.assertEqual(trend["lowest"]["cheapest"], 430.0)
        self.assertEqual(trend["highest"]["cheapest"], 610.0)
        change = trend["change_since_previous"]
        self.assertEqual((change["price_change"], change["direction"]), (-30.0, "lower"))
        self.assertEqual(change["percent"], -4.9)
        self.assertEqual(change["previous"]["cheapest"], 610.0)
        self.assertNotIn("forecast_price", json.dumps(trend))

    def test_different_query_parameters_are_not_comparable(self) -> None:
        self.record_flights(_flight_report(500.0, adults=1))
        self.record_flights(_flight_report(900.0, adults=2, at=T0 + timedelta(hours=1)))
        self.record_flights(_flight_report(480.0, at=T0 + timedelta(hours=2)), via=("LIS",))
        series = history.price_history()["series"]
        self.assertEqual(len(series), 3)
        self.assertEqual({s["trend"]["observation_count"] for s in series}, {1})

    def test_currencies_never_share_a_series(self) -> None:
        self.record_flights(_flight_report(500.0, currency="USD"))
        self.record_flights(_flight_report(400.0, currency="GBP", at=T0 + timedelta(hours=1)))
        self.record_flights(_flight_report(450.0, currency="USD", at=T0 + timedelta(hours=2)))
        series = {s["currency"]: s for s in history.price_history()["series"]}
        self.assertEqual(set(series), {"USD", "GBP"})
        self.assertEqual(series["USD"]["trend"]["observation_count"], 2)
        self.assertEqual(series["USD"]["trend"]["lowest"]["cheapest"], 450.0)
        self.assertEqual(series["GBP"]["trend"]["observation_count"], 1)
        only = history.price_history(currency="gbp")["series"]
        self.assertEqual([s["currency"] for s in only], ["GBP"])

    def test_filters_select_route_date_location_and_kind(self) -> None:
        self.record_flights(_flight_report(500.0))
        self.record_flights(_flight_report(300.0, origin="NRT", destination="SIN"))
        history.append_observations(history.hotel_observations(_hotel_report(900.0), {}))
        self.assertEqual(len(history.price_history(route="jfk-lhr")["series"]), 1)
        self.assertEqual(len(history.price_history(date=DEPART.isoformat())["series"]), 3)
        self.assertEqual(len(history.price_history(kind="hotel", location="  lisbon")["series"]), 1)
        self.assertEqual(history.price_history(route="JFK-SIN")["series"], [])
        with self.assertRaises(ValueError):
            history.price_history(route="NYC")

    def test_empty_history_says_recording_is_opt_in(self) -> None:
        payload = history.price_history()
        self.assertEqual(payload["series"], [])
        self.assertIn(history.ENV_RECORD, payload["note"])

    def test_observation_limit_keeps_trend_over_all(self) -> None:
        for hours in range(5):
            self.record_flights(_flight_report(500.0 + hours, at=T0 + timedelta(hours=hours)))
        (item,) = history.price_history(limit=2)["series"]
        self.assertEqual(len(item["observations"]), 2)
        self.assertEqual(item["observations_omitted"], 3)
        self.assertEqual(item["trend"]["observation_count"], 5)


class CacheTests(_State):
    def test_cached_replay_is_not_a_new_observation(self) -> None:
        self.enable()
        calls = []

        def fake(queries, **kwargs):
            calls.append(1)
            return _flight_report(500.0)

        recorded = history.recorded_flights(
            lambda queries, *, top=3, sort="ranked", baggage_buffer=None, **kw: fake(queries)
        )
        with patch("viajante.mcp_handlers.search_flights", recorded):
            first = search_flights_tool([f"JFK-LHR:{DEPART.isoformat()}"], currency="USD")
            second = search_flights_tool([f"JFK-LHR:{DEPART.isoformat()}"], currency="USD")
        self.assertNotIn("cached", first)
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(history.read_observations()), 1)


class WatchTests(_State):
    def run_watch(self, *prices: float, name: str = "nyc-lon", **kw: object) -> dict:
        report = _flight_report(*prices, at=kw.pop("at", T0))  # type: ignore[arg-type]
        stub = history.recorded_flights(
            lambda queries, *, top=3, sort="ranked", baggage_buffer=None, **rest: report
        )
        with patch("viajante.mcp_handlers.search_flights", stub):
            return watch.watch_price_tool(name, **kw)  # type: ignore[arg-type]

    def params(self) -> dict:
        return {"routes": [f"JFK-LHR:{DEPART.isoformat()}"], "currency": "USD"}

    def test_save_run_records_even_when_global_recording_is_off(self) -> None:
        result = self.run_watch(500.0, kind="flights", params=self.params())
        self.assertFalse(history.recording_enabled())
        self.assertEqual(result["recorded"], 1)
        (item,) = result["results"]
        self.assertIsNone(item["change"])
        self.assertIn("First observation", item["note"])
        self.assertEqual(len(history.read_observations()), 1)

    def test_rerun_reports_change_against_the_last_observation(self) -> None:
        self.run_watch(500.0, kind="flights", params=self.params())
        mcp_handlers._CACHE.clear()
        result = self.run_watch(450.0, at=T0 + timedelta(days=1))
        (item,) = result["results"]
        self.assertEqual(item["change"]["price_change"], -50.0)
        self.assertEqual(item["change"]["previous"]["cheapest"], 500.0)
        self.assertEqual(len(history.read_observations()), 2)

    def test_history_becoming_unreadable_after_a_good_append(self) -> None:
        self.run_watch(500.0, kind="flights", params=self.params())
        mcp_handlers._CACHE.clear()
        denied = history.HistoryReadError(13, "Permission denied", "/secret/path/history")
        with patch("viajante.watch.read_observations", side_effect=denied):
            result = self.run_watch(450.0, at=T0 + timedelta(days=1))
        self.assertEqual(result["recorded"], 1)
        (item,) = result["results"]
        self.assertTrue(item["recorded"])
        self.assertIsNone(item["change"])
        self.assertEqual(item["note"], "history could not be read; no comparison")
        self.assertEqual(result["read_error"], "permission denied accessing price history")
        self.assertNotIn("recording_error", result)
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(history.read_observations()), 2)

    def test_other_currency_is_never_compared(self) -> None:
        self.run_watch(500.0, kind="flights", params=self.params())
        mcp_handlers._CACHE.clear()
        spec = {**self.params(), "currency": "GBP"}
        report = _flight_report(400.0, currency="GBP", at=T0 + timedelta(days=1))
        stub = history.recorded_flights(
            lambda queries, *, top=3, sort="ranked", baggage_buffer=None, **rest: report
        )
        with patch("viajante.mcp_handlers.search_flights", stub):
            result = watch.watch_price_tool("nyc-lon-gbp", kind="flights", params=spec)
        (item,) = result["results"]
        self.assertIsNone(item["change"])
        self.assertEqual(item["currency"], "GBP")

    def test_cached_run_records_nothing_and_says_so(self) -> None:
        self.run_watch(500.0, kind="flights", params=self.params())
        result = self.run_watch(450.0)
        self.assertTrue(result["cached"])
        self.assertEqual(result["recorded"], 0)
        self.assertIn("no observation", result["note"])
        self.assertEqual(len(history.read_observations()), 1)

    def test_failed_search_records_nothing_and_reports_the_error(self) -> None:
        result = self.run_watch(kind="flights", params=self.params())
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(result["errors"][0]["code"], "no_results")
        self.assertEqual(history.read_observations(), [])

    def test_recording_failure_is_reported_not_called_no_offer(self) -> None:
        err = io.StringIO()
        with (
            patch(
                "viajante.history.append_observations",
                side_effect=PermissionError(13, "Permission denied", str(self.state / "x.tmp")),
            ),
            contextlib.redirect_stderr(err),
        ):
            result = self.run_watch(500.0, kind="flights", params=self.params())
        self.assertEqual(result["recorded"], 0)
        (item,) = result["results"]
        self.assertEqual(item["current"]["cheapest"], 500.0)
        self.assertFalse(item["recorded"])
        self.assertIn("recording failed: permission denied writing price history", result["note"])
        self.assertEqual(result["recording_error"], "permission denied writing price history")
        self.assertNotIn("/", result["note"] + result["recording_error"])
        self.assertNotIn(str(self.state), json.dumps(result))
        self.assertNotIn("no priced offer", result["note"])
        self.assertEqual(history.read_observations(), [])

    def test_save_validation(self) -> None:
        with self.assertRaises(ValueError):
            watch.save_watch("x", "flights", {**self.params(), "proxy": "http://u:p@h:1"})
        with self.assertRaises(ValueError):
            watch.save_watch("x", "flights", {"routes": [], "bogus": 1})
        with self.assertRaises(ValueError):
            watch.save_watch("bad name!", "flights", self.params())
        with self.assertRaises(ValueError):
            watch.save_watch("x", "trains", self.params())
        with self.assertRaises(ValueError):
            watch.watch_price_tool("x", kind="flights")
        with self.assertRaises(ValueError):
            watch.watch_price_tool("missing")
        self.assertEqual(watch.list_watches(), [])
        self.assertFalse((self.state / "price-watches.json").exists())

    def test_list_and_remove(self) -> None:
        watch.save_watch("a", "flights", self.params())
        self.assertEqual([w["name"] for w in watch.watch_price_tool()["watches"]], ["a"])
        self.assertTrue(watch.remove_watch("a"))
        self.assertFalse(watch.remove_watch("a"))


class EnvelopeTests(_State):
    DENIED = history.HistoryReadError(13, "Permission denied", "/secret/path/history")

    def params(self) -> dict:
        return {"routes": [f"JFK-LHR:{DEPART.isoformat()}"], "currency": "USD"}

    def run_watch(self, *prices: float, **kw: object) -> dict:
        report = _flight_report(*prices, at=T0)
        stub = history.recorded_flights(
            lambda queries, *, top=3, sort="ranked", baggage_buffer=None, **rest: report
        )
        with patch("viajante.mcp_handlers.search_flights", stub):
            return watch.watch_price_tool("nyc-lon", **kw)  # type: ignore[arg-type]

    def test_price_history_is_a_local_complete_result(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        payload = watch.price_history_tool(route="JFK-LHR")
        self.assertTrue(set(ENVELOPE_KEYS) <= set(payload))
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "complete"))
        self.assertIsNone(payload["error_code"])
        self.assertEqual(len(payload["series"]), 1)

    def test_unreadable_history_is_failed_blocked_and_series_is_null(self) -> None:
        with patch("viajante.history.read_observations", side_effect=self.DENIED):
            payload = watch.price_history_tool(route="JFK-LHR")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["completeness"], "blocked")
        self.assertEqual(payload["error_code"], "history_unreadable")
        self.assertIsNone(payload["series"])
        self.assertIsNone(payload["stored_entries"])
        self.assertNotIn("secret", json.dumps(payload))

    def test_empty_history_is_an_empty_list_not_null(self) -> None:
        payload = watch.price_history_tool()
        self.assertEqual((payload["status"], payload["series"]), ("ok", []))

    def test_watch_list_mode_is_local(self) -> None:
        payload = watch.watch_price_tool()
        self.assertEqual(payload["watches"], [])
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "complete"))
        self.assertTrue(set(ENVELOPE_KEYS) <= set(payload))

    def test_watch_run_carries_the_inner_searchs_envelope(self) -> None:
        result = self.run_watch(500.0, kind="flights", params=self.params())
        self.assertTrue(set(ENVELOPE_KEYS) <= set(result))
        self.assertEqual((result["status"], result["completeness"]), ("ok", "complete"))
        self.assertEqual(result["observed_at"], "2026-10-06T09:00:00Z")
        self.assertEqual(result["observed_at_basis"], "fetch")

    def test_watch_run_is_partial_when_history_cannot_be_read(self) -> None:
        self.run_watch(500.0, kind="flights", params=self.params())
        mcp_handlers._CACHE.clear()
        with patch("viajante.watch.read_observations", side_effect=self.DENIED):
            result = self.run_watch(450.0)
        self.assertEqual((result["status"], result["completeness"]), ("ok", "partial"))
        self.assertEqual(result["read_error"], "permission denied accessing price history")

    def test_failed_inner_search_keeps_its_own_status(self) -> None:
        result = self.run_watch(kind="flights", params=self.params())
        self.assertEqual(result["status"], "no_results")
        self.assertEqual(result["empty_reason"], "provider_empty")

    def test_a_proxy_in_a_watch_names_params_as_the_field(self) -> None:
        with self.assertRaises(ValueError) as caught:
            watch.watch_price_tool("w", kind="flights", params={**self.params(), "proxy": "x"})
        body = json.loads(str(structured_error(caught.exception, {"name": "w", "params": {}})))
        self.assertEqual(body["error"]["field"], "params")
        self.assertEqual(body["error"]["code"], "invalid_parameter")


class RecheckRecordingTests(_State):
    def recheck(self) -> dict:
        fresh = _recheck_report(_recheck_offer(450.0, (OUTBOUND,)))
        with patch("viajante.flights._run_search", return_value=fresh):
            return mcp_handlers.recheck_offer_tool(_previous(OUTBOUND), fetch="sweep")  # type: ignore[return-value]

    def test_recheck_with_history_on_records_exactly_one_observation(self) -> None:
        self.enable()
        result = self.recheck()
        self.assertEqual(result["outcome"], "price_changed")
        (entry,) = history.read_observations()
        self.assertEqual((entry["cheapest"], entry["currency"]), (450.0, "USD"))

    def test_guide_documents_history_watch_and_recheck_recording(self) -> None:
        self.assertIn("## Price history and watches", GUIDE)
        section = GUIDE.split("## Price history and watches")[1].split("\n## ")[0]
        for phrase in ("recheck_offer", "series null", "history_unreadable", "not read-only"):
            self.assertIn(phrase, " ".join(section.split()))
        self.assertLessEqual(len(INSTRUCTIONS.encode()), 2200)
        self.assertNotIn("price_history", INSTRUCTIONS)

    def test_recheck_with_history_off_records_nothing(self) -> None:
        self.recheck()
        self.assertEqual(history.read_observations(), [])


class EvidenceTests(_State):
    def test_price_change_is_owned_money(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        self.record_flights(_flight_report(450.0, at=T0 + timedelta(hours=1)))
        owned = _Owned([watch.price_history_tool()])
        self.assertTrue(owned.owns_amount(50.0, "USD"))
        self.assertEqual(
            watch.price_history_tool()["series"][0]["trend"]["change_since_previous"][
                "price_change"
            ],
            -50.0,
        )
        self.assertTrue(owned.owns_amount(500.0, "USD"))
        self.assertFalse(owned.owns_amount(500.0, "GBP"))


class CliTests(_State):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        return code, out.getvalue()

    def test_history_prints_facts_and_clear(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        self.record_flights(_flight_report(450.0, at=T0 + timedelta(hours=1)))
        code, text = self.run_cli("history", "--route", "JFK-LHR")
        self.assertEqual(code, 0)
        self.assertIn("lowest 450.00 USD", text)
        self.assertIn("-50.00 USD (-10.0%, lower)", text)
        self.assertIn("not a forecast", text)
        code, text = self.run_cli("history", "--clear")
        self.assertIn("Cleared 2", text)
        code, text = self.run_cli("history")
        self.assertIn("No recorded observation", text)

    def test_history_single_observation_wording(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        _, text = self.run_cli("history")
        self.assertIn("Only one observation recorded", text)

    def test_clear_reports_unreadable_lines_it_removes(self) -> None:
        self.enable()
        self.record_flights(_flight_report(500.0))
        path = self.state / history.HISTORY_FILE
        path.write_bytes(path.read_bytes() + b"\xff broken\n")
        _, text = self.run_cli("history", "--clear")
        self.assertIn("Cleared 1 observation and 1 unreadable line.", text)

    def test_clear_failure_is_a_clean_error(self) -> None:
        err = io.StringIO()
        with (
            patch("viajante.history_cli.clear_history", side_effect=PermissionError("denied")),
            contextlib.redirect_stderr(err),
        ):
            code, _ = self.run_cli("history", "--clear")
        self.assertEqual(code, 1)
        self.assertIn("error: could not clear the price history: denied", err.getvalue())

    def test_history_rejects_bad_route(self) -> None:
        code, _ = self.run_cli("history", "--route", "NYC")
        self.assertEqual(code, 1)

    def test_watch_list_and_remove(self) -> None:
        _, text = self.run_cli("watch", "--list")
        self.assertIn("No saved watches", text)
        watch.save_watch("a", "flights", {"routes": [f"JFK-LHR:{DEPART.isoformat()}"]})
        _, text = self.run_cli("watch", "--list")
        self.assertIn("a  flights", text)
        _, text = self.run_cli("watch", "a", "--remove")
        self.assertIn("Removed a", text)


if __name__ == "__main__":
    unittest.main()
