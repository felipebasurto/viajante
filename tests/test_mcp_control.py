"""Progress, cancellation, deadline, and compact JSON for the MCP server. Offline."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_audit_regressions import _card as _rt_card
from test_audit_regressions import _direct
from test_google_flights import _compact_body, _itinerary, _MuxFakeSweepClient
from viajante import evidence, mcp_handlers, mcp_server
from viajante.control import (
    SearchCancelled,
    SearchControl,
    SearchDeadline,
    active,
    checkpoint,
    controlled,
    interruptible_sleep,
    validate_deadline_seconds,
)
from viajante.dates import search_dates, search_flex
from viajante.explore import search_explore
from viajante.flights import search_flights
from viajante.google_flights import (
    ChromeSweepClient,
    GoogleFlightsHttpSource,
    RawFlightCard,
    SweepHttpResponse,
    SweepPost,
)
from viajante.google_flights_rpc import CompactCalendarDay, CompactExplorePlace
from viajante.hotels import search_hotels
from viajante.models import FlightQuery, HotelQuery, QueryFailure
from viajante.trip import search_trip

FUTURE = date.today() + timedelta(days=30)
ROUTES = [f"JFK-LHR:{FUTURE}", f"JFK-CDG:{FUTURE}", f"JFK-FRA:{FUTURE}"]


def _card() -> RawFlightCard:
    return RawFlightCard(
        airline="Fake Air",
        departure="08:00",
        arrival="16:00",
        duration="7 hr",
        stops="Nonstop",
        price="$480",
    )


class FakeFlights:
    """A sweep source that takes ``delay`` seconds per query and counts its calls."""

    def __init__(self, delay: float = 0.0) -> None:
        self.config = SimpleNamespace(html_lang="en", currency="USD", country=None)
        self.delay = delay
        self.calls = 0
        self.started = threading.Event()

    def fetch(self, trip) -> list[RawFlightCard]:
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        return [_card()]

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeCalendar(FakeFlights):
    def fetch_calendar(self, seed, start, end):
        self.calls += 1
        return [CompactCalendarDay(departure_date=start, price=480.0)]

    def fetch_explore(self, origin, start, **kwargs):
        self.calls += 1
        return [CompactExplorePlace(iata="LHR", city="London", country="GB")]


class McpControlCase(unittest.TestCase):
    """Isolates the state dir, the replay cache, the evidence ledger, and Playwright."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        no_browser = patch("viajante.flights.playwright_available", return_value=False)
        no_browser.start()
        self.addCleanup(no_browser.stop)
        mcp_handlers._CACHE.clear()
        evidence.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)
        self.addCleanup(evidence.clear)

    def use_source(self, source: FakeFlights):
        patcher = patch("viajante.flights.GoogleFlightsHttpSource", return_value=source)
        patcher.start()
        self.addCleanup(patcher.stop)
        return source

    def assert_untouched(self) -> None:
        self.assertEqual(mcp_handlers._CACHE, {})
        self.assertEqual(list(self.state.iterdir()), [])


def _args(routes=ROUTES, **extra: object) -> dict:
    return {"routes": list(routes), "fetch": "sweep", "currency": "USD", **extra}


def _session(server):
    from mcp.shared.memory import create_connected_server_and_client_session

    return create_connected_server_and_client_session(server._mcp_server)


class ProgressTests(McpControlCase):
    def test_indexed_progress_reaches_a_client_with_a_progress_token(self) -> None:
        self.use_source(FakeFlights())
        seen: list[tuple[float, float | None, str | None]] = []

        async def on_progress(progress: float, total: float | None, message: str | None) -> None:
            seen.append((progress, total, message))

        async def main():
            server = mcp_server.build_server()
            with patch.object(mcp_server, "_PROGRESS_INTERVAL_SECONDS", 0):
                async with _session(server) as session:
                    return await session.call_tool(
                        "search_flights", _args(), progress_callback=on_progress
                    )

        result = asyncio.run(main())
        self.assertFalse(result.isError, result)
        values = [row[0] for row in seen]
        self.assertEqual(values, sorted(set(values)), "progress must strictly increase")
        indexed = [(p, t) for p, t, m in seen if m and m.startswith("[")]
        self.assertEqual(indexed, [(1.0, 3.0), (2.0, 3.0), (3.0, 3.0)])
        self.assertTrue(any(m and m.startswith("fetch: sweep") for _p, _t, m in seen))
        self.assertEqual(len(json.loads(result.content[0].text)["queries"]), 3)

    def test_no_relay_without_a_progress_token(self) -> None:
        self.use_source(FakeFlights())

        async def main(**kwargs):
            server = mcp_server.build_server()
            async with _session(server) as session:
                return await session.call_tool("search_flights", _args(), **kwargs)

        with patch("viajante.mcp_server.ProgressRelay") as relay:
            asyncio.run(main())
            relay.assert_not_called()

            async def ignore(progress, total, message):
                return None

            asyncio.run(main(progress_callback=ignore))
            relay.assert_called_once()

    def test_messages_are_throttled_and_the_newest_is_flushed(self) -> None:
        sent: list[tuple[float, float | None, str]] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append((progress, total, message))

        async def main() -> None:
            loop = asyncio.get_running_loop()
            relay = mcp_server.ProgressRelay(Ctx(), loop, 0.1)

            def emit() -> None:
                for text in ("starting", "[1/3] a", "[2/3] b", "[3/3] c"):
                    relay(text)

            await loop.run_in_executor(None, emit)
            await asyncio.sleep(0.3)
            await relay.drain()

        asyncio.run(main())
        self.assertEqual([m for _p, _t, m in sent], ["starting", "[3/3] c"])
        self.assertEqual(sent[-1][:2], (3.0, 3.0))

    def test_message_only_lines_still_increase(self) -> None:
        sent: list[float] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append(progress)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            relay = mcp_server.ProgressRelay(Ctx(), loop, 0)
            await loop.run_in_executor(None, lambda: [relay(f"line {i}") for i in range(4)])
            await relay.drain()

        asyncio.run(main())
        self.assertEqual(len(sent), 4)
        self.assertEqual(sent, sorted(set(sent)))


class CancelTests(McpControlCase):
    def test_cancelled_request_stops_the_worker_and_frees_the_lock(self) -> None:
        from mcp import types

        source = self.use_source(FakeFlights(delay=0.3))
        timings: dict[str, float] = {}

        async def main():
            server = mcp_server.build_server()
            loop = asyncio.get_running_loop()
            async with _session(server) as session:
                request_id = session._request_id
                slow = asyncio.create_task(session.call_tool("search_flights", _args()))
                self.assertTrue(await loop.run_in_executor(None, source.started.wait, 5))
                await session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            method="notifications/cancelled",
                            params=types.CancelledNotificationParams(requestId=request_id),
                        )
                    )
                )
                await asyncio.sleep(0.05)
                timings["cancelled"] = time.monotonic()
                source.delay = 0.0
                calls_at_cancel = source.calls
                second = await session.call_tool("search_flights", _args(routes=ROUTES[:1]))
                timings["second"] = time.monotonic()
                slow.cancel()
                return calls_at_cancel, second

        calls_at_cancel, second = asyncio.run(main())
        self.assertFalse(second.isError, second)
        self.assertNotIn("already running", second.content[0].text)
        self.assertEqual(calls_at_cancel, 1)
        self.assertEqual(source.calls, 2, "cancelled queries 2 and 3 must never be sent")
        self.assertLess(timings["second"] - timings["cancelled"], 1.0)
        self.assertFalse(mcp_server._SEARCH_BUSY.locked())

    def test_cancelled_search_is_not_cached_recorded_or_cooled_down(self) -> None:
        source = self.use_source(FakeFlights(delay=0.2))

        async def main() -> None:
            task = asyncio.create_task(
                mcp_server.run_mcp_tool(mcp_handlers.search_flights_tool, **_tool_args())
            )
            loop = asyncio.get_running_loop()
            self.assertTrue(await loop.run_in_executor(None, source.started.wait, 5))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await loop.run_in_executor(None, mcp_server._SEARCH_BUSY.acquire)
            mcp_server._SEARCH_BUSY.release()

        asyncio.run(main())
        self.assertEqual(source.calls, 1)
        self.assert_untouched()
        self.assertEqual(evidence.verify_answer("x")["searches"], 0)

    def test_worker_waits_in_event_not_sleep(self) -> None:
        control = SearchControl()
        threading.Timer(0.05, control.cancel.set).start()
        started = time.monotonic()
        with active(control), self.assertRaises(SearchCancelled):
            interruptible_sleep(5.0)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_cancelled_backoff_does_not_retry(self) -> None:
        control = SearchControl()
        calls: list[int] = []

        class Flaky(FakeFlights):
            def fetch(self, trip):
                calls.append(1)
                control.cancel.set()
                raise RuntimeError("boom")

        trips = (FlightQuery("JFK", "LHR", FUTURE),)
        with (
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=Flaky()),
            patch("viajante.flights.playwright_available", return_value=False),
            self.assertRaises(SearchCancelled),
        ):
            search_flights(trips, fetch="sweep", currency="USD", cancel=control.cancel)
        self.assertEqual(calls, [1])

    def test_cancel_is_not_swallowed_by_exception_handlers(self) -> None:
        self.assertFalse(issubclass(SearchCancelled, Exception))
        self.assertTrue(issubclass(SearchDeadline, Exception))


def _tool_args(**extra: object) -> dict:
    return {"routes": list(ROUTES), "fetch": "sweep", "currency": "USD", **extra}


class DeadlineTests(McpControlCase):
    def test_deadline_returns_finished_queries_and_labels_the_rest(self) -> None:
        source = self.use_source(FakeFlights(delay=0.2))

        async def main():
            server = mcp_server.build_server()
            async with _session(server) as session:
                return await session.call_tool("search_flights", _args(deadline_seconds=0.1))

        result = asyncio.run(main())
        self.assertFalse(result.isError, result)
        payload = json.loads(result.content[0].text)
        statuses = [q["status"] for q in payload["queries"]]
        self.assertEqual(statuses, ["ok", "error", "error"])
        for row in payload["queries"][1:]:
            self.assertEqual(row["error"]["code"], "deadline")
            self.assertNotIn("empty_reason", row)
            self.assertNotEqual(row["error"]["code"], "no_results")
        coverage = payload["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["stopping_reason"], "deadline")
        self.assertEqual(
            (coverage["attempted"], coverage["succeeded"], coverage["empty"], coverage["failed"]),
            (3, 1, 0, 2),
        )
        self.assertIn("not loaded", coverage["unsearched"])
        self.assertEqual(source.calls, 1)

    def test_partial_results_are_not_cached_or_cooled_down(self) -> None:
        source = self.use_source(FakeFlights(delay=0.2))
        first = mcp_handlers.search_flights_tool(
            ROUTES, fetch="sweep", currency="USD", deadline_seconds=0.1
        )
        self.assertEqual(first["coverage"]["stopping_reason"], "deadline")
        self.assert_untouched()
        source.delay = 0.0
        again = mcp_handlers.search_flights_tool(ROUTES, fetch="sweep", currency="USD")
        self.assertNotIn("cached", again)
        self.assertTrue(again["coverage"]["complete"])
        self.assertEqual(source.calls, 1 + 3)

    def test_complete_result_under_a_deadline_is_cached_for_any_deadline(self) -> None:
        source = self.use_source(FakeFlights())
        mcp_handlers.search_flights_tool(ROUTES, fetch="sweep", currency="USD", deadline_seconds=30)
        again = mcp_handlers.search_flights_tool(ROUTES, fetch="sweep", currency="USD")
        self.assertTrue(again["cached"])
        self.assertEqual(source.calls, 3)

    def test_no_deadline_keeps_todays_behavior(self) -> None:
        self.use_source(FakeFlights())
        payload = mcp_handlers.search_flights_tool(ROUTES, fetch="sweep", currency="USD")
        self.assertTrue(payload["coverage"]["complete"])
        self.assertEqual(payload["coverage"]["stopping_reason"], "completed_scope")

    def test_deadline_must_be_positive_and_finite(self) -> None:
        for bad in (0, -1, float("inf"), float("nan"), True, "3"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_deadline_seconds(bad)  # type: ignore[arg-type]
        self.assertEqual(validate_deadline_seconds(2), 2.0)
        self.assertIsNone(validate_deadline_seconds(None))

    def test_env_default_applies_only_when_the_call_names_none(self) -> None:
        seen: list[object] = []

        def tool(*, deadline_seconds=None):
            seen.append(deadline_seconds)
            return {}

        async def main() -> None:
            await mcp_server.run_mcp_tool(tool, deadline_seconds=None)
            await mcp_server.run_mcp_tool(tool, deadline_seconds=7)

        with patch.object(mcp_server, "_ENV_DEADLINE", 12.5):
            asyncio.run(main())
        self.assertEqual(seen, [12.5, 7])
        self.assertFalse(mcp_server._SEARCH_BUSY.locked())

    def test_env_deadline_is_validated_when_the_server_starts(self) -> None:
        with patch.dict(os.environ, {mcp_server.DEADLINE_ENV: "12.5"}):
            self.assertEqual(mcp_server.env_deadline_seconds(), 12.5)
            mcp_server.build_server()
            self.assertEqual(mcp_server._ENV_DEADLINE, 12.5)
        self.addCleanup(setattr, mcp_server, "_ENV_DEADLINE", None)
        for bad in ("soon", "0", "-3", "nan", "inf"):
            with patch.dict(os.environ, {mcp_server.DEADLINE_ENV: bad}):
                with (
                    self.subTest(bad=bad),
                    self.assertRaisesRegex(ValueError, mcp_server.DEADLINE_ENV),
                ):
                    mcp_server.build_server()
        with patch.dict(os.environ, {mcp_server.DEADLINE_ENV: "soon"}):
            with self.assertRaises(SystemExit) as raised:
                mcp_server.main([])
        self.assertIn(mcp_server.DEADLINE_ENV, str(raised.exception))
        self.assertIn("viajante-mcp", str(raised.exception))

    def test_a_string_or_boolean_deadline_is_rejected_by_the_tool_schema(self) -> None:
        source = self.use_source(FakeFlights())

        async def call(value):
            async with _session(mcp_server.build_server()) as session:
                return await session.call_tool("search_flights", _args(deadline_seconds=value))

        for bad in ("5", True):
            with self.subTest(bad=bad):
                result = asyncio.run(call(bad))
                self.assertTrue(result.isError, result)
        self.assertEqual(source.calls, 0)
        ok = asyncio.run(call(5))
        self.assertFalse(ok.isError, ok)
        self.assertEqual(asyncio.run(call(2.5)).isError, False)

    def test_expired_deadline_sends_no_query(self) -> None:
        source = FakeFlights()
        trips = tuple(FlightQuery("JFK", dest, FUTURE) for dest in ("LHR", "CDG"))
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights(trips, fetch="sweep", currency="USD", deadline_seconds=1e-9)
        self.assertEqual(source.calls, 0)
        self.assertTrue(all(isinstance(q, QueryFailure) for q in report.queries))
        self.assertEqual({q.error.code.value for q in report.queries}, {"deadline"})
        self.assertEqual(report.coverage.stopping_reason, "deadline")

    def test_dates_flex_and_explore_report_deadline_not_empty(self) -> None:
        source = FakeCalendar()
        end = FUTURE + timedelta(days=2)
        dates = search_dates(
            "JFK", "LHR", FUTURE, end, currency="USD", source=source, deadline_seconds=1e-9
        )
        flex = search_flex(
            "JFK",
            "LHR",
            FUTURE + timedelta(days=3),
            2,
            currency="USD",
            source=source,
            deadline_seconds=1e-9,
        )
        explore = search_explore(
            "JFK", FUTURE, currency="USD", source=source, deadline_seconds=1e-9
        )
        self.assertEqual(source.calls, 0)
        for name, report in (("dates", dates), ("flex", flex), ("explore", explore)):
            with self.subTest(report=name):
                coverage = report.coverage
                self.assertFalse(coverage.complete)
                self.assertEqual(coverage.stopping_reason, "deadline")
                self.assertEqual(coverage.empty, 0)
                self.assertIn("not loaded", coverage.unsearched)
        self.assertEqual({row.error.code.value for row in dates.days}, {"deadline"})
        self.assertEqual(flex.error.code.value, "deadline")
        self.assertEqual(explore.error.code.value, "deadline")

    def test_flex_counters_include_a_shop_cut_by_the_deadline(self) -> None:
        class CalendarThenCut(FakeCalendar):
            def fetch_calendar(self, seed, start, end):
                span = (end - start).days + 1
                return [
                    CompactCalendarDay(departure_date=start + timedelta(days=i), price=400.0 + i)
                    for i in range(span)
                ]

            def fetch(self, trip):
                raise SearchDeadline()

        flex = search_flex(
            "JFK",
            "LHR",
            FUTURE + timedelta(days=3),
            2,
            currency="USD",
            source=CalendarThenCut(),
        )
        coverage = flex.coverage
        self.assertEqual(flex.error.code.value, "deadline")
        self.assertEqual(
            (coverage.attempted, coverage.succeeded, coverage.failed, coverage.empty), (6, 5, 1, 0)
        )
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.stopping_reason, "deadline")
        self.assertIn("1 of 6 units", coverage.unsearched)

    def test_hotels_and_trip_report_deadline(self) -> None:
        class FakeHotels:
            config = SimpleNamespace(html_lang="en", currency="USD")
            calls = 0

            def fetch(self, *args):
                FakeHotels.calls += 1
                raise AssertionError("no query may start after the deadline")

            def reset(self) -> None:
                return None

            def close(self) -> None:
                return None

        query = HotelQuery("Lisbon", FUTURE, FUTURE + timedelta(days=2))
        flights = FakeFlights()
        with (
            patch("viajante.hotels.GoogleHotelsSource", return_value=FakeHotels()),
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=flights),
        ):
            hotels = search_hotels((query,), source="google", currency="USD", deadline_seconds=1e-9)
            trip = search_trip(
                (FlightQuery("JFK", "LIS", FUTURE),),
                query,
                currency="USD",
                fetch="sweep",
                deadline_seconds=1e-9,
            )
        self.assertEqual(FakeHotels.calls, 0)
        self.assertEqual(flights.calls, 0)
        payload = hotels.to_dict()
        self.assertEqual(payload["queries"][0]["error"]["code"], "deadline")
        self.assertEqual(payload["coverage"]["stopping_reason"], "deadline")
        self.assertFalse(payload["coverage"]["complete"])
        self.assertIsNone(trip.trip_total)
        self.assertEqual(trip.flights.coverage.stopping_reason, "deadline")
        self.assertEqual(trip.hotels.to_dict()["coverage"]["stopping_reason"], "deadline")

    def test_hotel_payload_has_no_coverage_without_a_deadline(self) -> None:
        class OkHotels:
            config = SimpleNamespace(html_lang="en", currency="USD")

            def fetch(self, *args):
                raise RuntimeError("provider failed")

            def reset(self) -> None:
                return None

            def close(self) -> None:
                return None

        query = HotelQuery("Lisbon", FUTURE, FUTURE + timedelta(days=2))
        with (
            patch("viajante.hotels.GoogleHotelsSource", return_value=OkHotels()),
            patch("viajante.hotels.interruptible_sleep"),
        ):
            report = search_hotels((query,), source="google", currency="USD")
        self.assertNotIn("coverage", report.to_dict())

    def test_deadline_cuts_a_sleep_without_raising(self) -> None:
        control = SearchControl(deadline_seconds=0.05)
        started = time.monotonic()
        with active(control):
            interruptible_sleep(5.0)
        self.assertLess(time.monotonic() - started, 1.0)
        with active(control), self.assertRaises(SearchDeadline):
            checkpoint()

    def test_nested_controls_inherit_and_tighten(self) -> None:
        seen: list[SearchControl | None] = []

        @controlled
        def inner(*, cancel=None, deadline_seconds=None, progress=None):
            from viajante.control import current_control

            seen.append(current_control())
            return progress

        outer = SearchControl(deadline_seconds=60, progress=lambda text: None)
        with active(outer):
            self.assertIs(inner(), outer.progress)
            self.assertIs(seen[-1], outer)
            inner(deadline_seconds=1)
            self.assertIsNot(seen[-1], outer)
            self.assertLess(seen[-1].deadline_at, outer.deadline_at)
        inner()
        self.assertIsNone(seen[-1])


class CompactJsonTests(McpControlCase):
    def test_tool_text_is_compact_and_equal_to_the_indented_payload(self) -> None:
        self.use_source(FakeFlights())

        async def main():
            server = mcp_server.build_server()
            async with _session(server) as session:
                return await session.call_tool("search_flights", _args())

        result = asyncio.run(main())
        text = result.content[0].text
        payload = json.loads(text)
        self.assertNotIn("\n", text)
        self.assertEqual(text, json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        indented = json.dumps(payload, indent=2, ensure_ascii=False)
        self.assertLess(len(text.encode()), len(indented.encode()) * 0.8)
        mcp_handlers._CACHE.clear()
        direct = mcp_handlers.search_flights_tool(ROUTES, fetch="sweep", currency="USD")
        self.assertEqual(set(payload), set(direct) - {"cached"})

    def test_non_json_text_and_other_blocks_pass_through(self) -> None:
        self.assertEqual(mcp_server._compact_json("not json"), "not json")
        self.assertEqual(mcp_server._compact_json('{\n  "a": [1, 2]\n}'), '{"a":[1,2]}')
        self.assertEqual(mcp_server._compact_blocks({"a": 1}), {"a": 1})

    def test_unicode_is_kept(self) -> None:
        self.assertEqual(mcp_server._compact_json('{"city": "São Paulo"}'), '{"city":"São Paulo"}')


class ReturnLegDeadlineTests(McpControlCase):
    """A deadline inside a follow-up provider call is a deadline, never a quiet downgrade."""

    def _round_trip_source(self, slow: list[bool]) -> FakeFlights:
        source = FakeFlights()
        source.fetch = lambda trip: [_rt_card(legs=(_direct(),))]  # type: ignore[method-assign]
        source.selected_calls = 0

        def fetch_selected(_trip, selections):
            source.selected_calls += 1
            if slow[0]:
                interruptible_sleep(0.6)
                checkpoint()
            return [
                (_rt_card(legs=(_direct("LHR", "JFK", on=FUTURE + timedelta(days=3)),)),)
                for _ in selections
            ]

        source.fetch_selected = fetch_selected  # type: ignore[attr-defined]
        return source

    def _call(self, **extra: object) -> dict:
        route = f"JFK-LHR:{FUTURE}:{FUTURE + timedelta(days=3)}"

        async def main():
            async with _session(mcp_server.build_server()) as session:
                args = _args(routes=[route], trip="round-trip", **extra)
                return await session.call_tool("search_flights", args)

        result = asyncio.run(main())
        self.assertFalse(result.isError, result)
        return json.loads(result.content[0].text)

    def test_deadline_in_the_return_leg_fetch_is_partial_and_never_cached(self) -> None:
        slow = [True]
        source = self._round_trip_source(slow)
        self.use_source(source)
        first = self._call(deadline_seconds=0.2)
        row = first["queries"][0]
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"]["code"], "deadline")
        self.assertFalse(first["coverage"]["complete"])
        self.assertEqual(first["coverage"]["stopping_reason"], "deadline")
        self.assertEqual(mcp_handlers._CACHE, {})

        slow[0] = False
        self.assertEqual(source.selected_calls, 1)
        second = self._call()
        self.assertNotIn("cached", second)
        self.assertEqual(source.selected_calls, 2, "the second call must hit the provider")
        self.assertEqual(len(second["queries"][0]["offers"][0]["legs"]), 2)
        self.assertTrue(second["coverage"]["complete"])

    def test_deadline_in_the_typical_calendar_is_not_swallowed(self) -> None:
        source = FakeCalendar()

        def fetch_calendar(_seed, _start, _end):
            raise SearchDeadline()

        source.fetch_calendar = fetch_calendar  # type: ignore[method-assign]
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights(
                (FlightQuery("JFK", "LHR", FUTURE),), fetch="sweep", currency="USD"
            )
        self.assertEqual(report.queries[0].error.code.value, "deadline")

    def test_a_result_that_used_up_its_deadline_is_never_cached(self) -> None:
        runs: list[int] = []

        @mcp_handlers._cached
        def tool(*, deadline_seconds=None):
            runs.append(1)
            time.sleep(0.05)
            return {"queries": []}

        tool(deadline_seconds=0.01)
        tool(deadline_seconds=0.01)
        self.assertEqual(len(runs), 2)
        tool(deadline_seconds=30)
        tool(deadline_seconds=None)
        self.assertEqual(len(runs), 3, "a complete result under its deadline is replayed")


class ProgressRobustnessTests(unittest.TestCase):
    def _relay(self, ctx, interval: float):
        return mcp_server.ProgressRelay(ctx, asyncio.get_running_loop(), interval)

    def test_older_sdk_signature_gets_progress_and_total_only(self) -> None:
        sent: list[tuple] = []

        class OldCtx:
            async def report_progress(self, progress, total=None):
                sent.append((progress, total))

        async def main() -> None:
            relay = self._relay(OldCtx(), 0)
            await asyncio.get_running_loop().run_in_executor(None, relay, "[1/3] a")
            await relay.drain()

        asyncio.run(main())
        self.assertEqual(sent, [(1.0, 3.0)])

    def test_a_failing_notification_is_logged_once_and_never_raised(self) -> None:
        class BrokenCtx:
            async def report_progress(self, progress, total, message):
                raise RuntimeError("transport closed")

        async def main() -> None:
            loop = asyncio.get_running_loop()
            relay = self._relay(BrokenCtx(), 0)
            await loop.run_in_executor(None, lambda: [relay(f"[{i}/3] x") for i in (1, 2, 3)])
            await relay.drain()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            asyncio.run(main())
        self.assertEqual(err.getvalue().count("progress notification failed"), 1)
        self.assertIn("transport closed", err.getvalue())

    def test_the_last_held_line_is_flushed_when_the_search_finishes(self) -> None:
        sent: list[tuple] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append((progress, total, message))

        async def main() -> None:
            relay = self._relay(Ctx(), 60)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: [relay(f"[{i}/3] q{i}") for i in (1, 2, 3)]
            )
            await relay.drain()

        asyncio.run(main())
        self.assertEqual([row[:2] for row in sent], [(1.0, 3.0), (3.0, 3.0)])
        self.assertEqual(sent[-1][2], "[3/3] q3")

    def test_progress_never_exceeds_the_total(self) -> None:
        sent: list[tuple] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append((progress, total))

        async def main() -> None:
            relay = self._relay(Ctx(), 0)
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: [relay(line) for line in ("[2/3] a", "[3/3] b", "wrap up", "[3/3] b")],
            )
            await relay.drain()

        asyncio.run(main())
        self.assertEqual(sent, [(2.0, 3.0), (3.0, 3.0)])


class PartialBatchTests(unittest.TestCase):
    def test_responses_that_arrived_before_the_deadline_are_kept(self) -> None:
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._asyncio = asyncio
        client._loop = asyncio.new_event_loop()
        thread = threading.Thread(target=client._loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(client._loop.call_soon_threadsafe, client._loop.stop)

        async def apost(url, data, headers, timeout):
            if url.endswith("slow"):
                await asyncio.sleep(30)
            return SweepHttpResponse(200, "arrived", url)

        client._apost = apost  # type: ignore[method-assign]
        jobs = [SweepPost("https://x/fast", "", {}), SweepPost("https://x/slow", "", {})]
        with active(SearchControl(deadline_seconds=0.3)):
            out = client.post_many(jobs, timeout=1.0)
        time.sleep(0.2)
        self.assertEqual([r.deadline for r in out], [False, True])
        self.assertEqual(out[0].text, "arrived")

    def test_arrived_routes_keep_their_cards_and_the_rest_are_deadline(self) -> None:
        shop = _compact_body(_itinerary(price=45, airline="Vueling"))

        class Client(_MuxFakeSweepClient):
            def post_many(self, jobs, *, timeout):
                return [
                    self._response(jobs[0].url),
                    SweepHttpResponse(0, "", jobs[1].url, deadline=True),
                ]

        source = GoogleFlightsHttpSource(client=Client(shop_text=shop), currency="USD")
        trips = tuple(FlightQuery("JFK", dest, FUTURE) for dest in ("LHR", "CDG"))
        first, second = source.fetch_many(trips)
        self.assertEqual(first[0].airline, "Vueling")
        self.assertIsInstance(second, SearchDeadline)


if __name__ == "__main__":
    unittest.main()
