from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from viajante import mcp_handlers, skiplagged
from viajante.flights import _needs_detail_fallback, classify_failure
from viajante.google_flights import (
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    ChromeSweepClient,
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    SweepHttpResponse,
    _raise_if_blocked,
    note_rate_limited,
    rate_limit_status,
)
from viajante.google_hotels import GoogleHotelsSource
from viajante.google_hotels_rpc import HotelsBlocked
from viajante.models import FlightQuery, HotelQuery, QueryFailure

T0 = 1_800_000_000.0


class _StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)


class CooldownStateTests(_StateDir):
    def test_first_429_starts_the_base_cooldown_and_a_burst_does_not_extend_it(self) -> None:
        first = note_rate_limited(now=T0)
        self.assertEqual(first["until"], T0 + RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(note_rate_limited(now=T0 + 5), first)
        self.assertIsNotNone(rate_limit_status(now=T0 + 1))
        self.assertIsNone(rate_limit_status(now=first["until"] + 1))

    def test_repeat_429_soon_after_a_cooldown_doubles_up_to_the_cap(self) -> None:
        state = note_rate_limited(now=T0)
        for _ in range(8):
            state = note_rate_limited(now=state["until"] + 1)
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_MAX_COOLDOWN_SECONDS)

    def test_named_retry_after_wins(self) -> None:
        self.assertEqual(note_rate_limited(retry_after=42, now=T0)["cooldown_s"], 42)


class CooldownGateTests(_StateDir):
    def test_flights_and_hotels_send_nothing_during_a_cooldown(self) -> None:
        note_rate_limited()
        departure = date.today() + timedelta(days=30)
        with (
            patch("viajante.google_flights.shared_chrome_sweep_client") as flights_client,
            patch("viajante.google_hotels.shared_chrome_sweep_client") as hotels_client,
        ):
            with self.assertRaises(GoogleFlightsBlocked) as flights:
                GoogleFlightsHttpSource(currency="USD").fetch(FlightQuery("JFK", "LHR", departure))
            with self.assertRaises(HotelsBlocked) as hotels:
                GoogleHotelsSource(currency="USD").fetch(
                    HotelQuery("Tokyo", departure, departure + timedelta(days=2)), None, 5
                )
        flights_client.assert_not_called()
        hotels_client.assert_not_called()
        self.assertTrue(str(flights.exception).startswith("Not sent. Google is rate-limiting"))
        self.assertTrue(hotels.exception.rate_limited)
        error = classify_failure(flights.exception)
        self.assertTrue(error.rate_limited)
        self.assertIs(error.to_dict()["rate_limited"], True)
        failure = QueryFailure(query=FlightQuery("JFK", "LHR", departure), error=error)
        self.assertFalse(_needs_detail_fallback(failure))

    def test_proxied_search_is_not_paused(self) -> None:
        note_rate_limited()
        with patch("viajante.google_flights.shared_chrome_sweep_client") as client:
            GoogleFlightsHttpSource(
                proxy="http://proxy.example:8080", currency="EUR"
            )._ensure_client()
        client.assert_called_once()


STATUS_13_BODY = ")]}'\n\n" + json.dumps(
    [["wrb.fr", "GetCalendarGrid", None, None, None, [13], "generic"]]
)
CALENDAR_URL = "https://www.google.com/_/FlightsFrontendUi/data/GetCalendarGrid"


class _FakeHttpResponse:
    url = CALENDAR_URL
    headers: dict = {}

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _exchange(text: str, *, proxied: bool, status_code: int = 200) -> SweepHttpResponse:
    client = ChromeSweepClient.__new__(ChromeSweepClient)
    client._proxied = proxied

    async def send() -> _FakeHttpResponse:
        return _FakeHttpResponse(text, status_code)

    return asyncio.run(client._exchange(send, 1.0))


class StatusThirteenTests(_StateDir):
    def test_direct_status_13_records_a_cooldown_and_says_why(self) -> None:
        out = _exchange(STATUS_13_BODY, proxied=False)
        self.assertEqual(out.status, 200)
        self.assertIn("RPC status 13", out.rate_limit or "")
        self.assertIsNotNone(rate_limit_status())

    def test_proxied_status_13_does_not_pause_direct_searches(self) -> None:
        out = _exchange(STATUS_13_BODY, proxied=True)
        self.assertIsNone(out.rate_limit)
        self.assertIsNone(rate_limit_status())

    def test_ordinary_answers_leave_no_cooldown(self) -> None:
        out = _exchange(")]}'\n\n[]", proxied=False)
        self.assertIsNone(out.rate_limit)
        self.assertIsNone(rate_limit_status())

    def test_flights_treat_a_recorded_status_13_as_a_rate_limit(self) -> None:
        advice = _exchange(STATUS_13_BODY, proxied=False).rate_limit
        with self.assertRaises(GoogleFlightsBlocked) as ctx:
            _raise_if_blocked(200, STATUS_13_BODY, CALENDAR_URL, CALENDAR_URL, advice)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(str(ctx.exception), advice)
        self.assertTrue(classify_failure(ctx.exception).rate_limited)

    def test_status_13_without_advice_keeps_the_plain_blocked_message(self) -> None:
        with self.assertRaises(GoogleFlightsBlocked) as ctx:
            _raise_if_blocked(200, STATUS_13_BODY, CALENDAR_URL, CALENDAR_URL)
        self.assertIsNone(ctx.exception.status)
        self.assertIn("RPC error status 13", str(ctx.exception))

    def test_hotels_treat_a_recorded_status_13_as_a_rate_limit(self) -> None:
        advice = _exchange(STATUS_13_BODY, proxied=False).rate_limit
        response = SweepHttpResponse(200, STATUS_13_BODY, CALENDAR_URL, rate_limit=advice)
        with self.assertRaises(HotelsBlocked) as ctx:
            GoogleHotelsSource._page(response, CALENDAR_URL)
        self.assertTrue(ctx.exception.rate_limited)
        self.assertEqual(str(ctx.exception), advice)


class SkiplaggedCooldownTests(_StateDir):
    def _live_rpc(self, calls: list):
        def rpc(url, payload, headers):
            calls.append(payload.get("method"))
            return 429, {"retry-after": "90"}, ""

        return rpc

    def test_a_live_429_records_a_cooldown_and_the_next_call_sends_nothing(self) -> None:
        calls: list = []
        rpc = self._live_rpc(calls)
        with (
            patch("viajante.skiplagged._rpc_post", rpc),
            patch("viajante.skiplagged.MIN_CALL_INTERVAL_SECONDS", 0.0),
        ):
            with self.assertRaises(skiplagged.SkiplaggedRateLimited):
                skiplagged._call_mcp({}, rpc=rpc, tool="sk_hotels_search")
            state = rate_limit_status(file="skiplagged-rate-limit.json")
            self.assertEqual(state["cooldown_s"], 90.0)
            with self.assertRaises(skiplagged.SkiplaggedRateLimited) as again:
                skiplagged._call_mcp({}, rpc=rpc, tool="sk_hotels_search")
        self.assertEqual(calls, ["initialize"])
        self.assertTrue(str(again.exception).startswith("Not sent. Skiplagged is rate-limiting"))
        self.assertIsNone(rate_limit_status())

    def test_injected_transports_never_touch_the_cooldown_file(self) -> None:
        calls: list = []
        with self.assertRaises(skiplagged.SkiplaggedRateLimited):
            skiplagged._call_mcp({}, rpc=self._live_rpc(calls), tool="sk_hotels_search")
        self.assertIsNone(rate_limit_status(file="skiplagged-rate-limit.json"))

    def test_live_calls_are_paced(self) -> None:
        rpc = MagicMock()
        clock = MagicMock()
        clock.monotonic.side_effect = [100.0, 100.0, 100.2, 101.0]
        skiplagged._LAST_CALL[0] = 0.0
        sleep = MagicMock()
        with (
            patch("viajante.skiplagged._rpc_post", rpc),
            patch("viajante.skiplagged.time", clock),
            patch("viajante.skiplagged.interruptible_sleep", sleep),
        ):
            skiplagged._pace(rpc)
            skiplagged._pace(rpc)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.8)


class SearchCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_handlers._CACHE.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_identical_success_is_replayed_and_failures_are_not(self) -> None:
        calls: list[str] = []

        @mcp_handlers._cached
        def tool(route: str, *, fail: bool = False) -> dict:
            calls.append(route)
            error = {"code": "blocked"} if fail else None
            return {"searched_at": "t", "error": error}

        tool("JFK-LHR")
        again = tool("JFK-LHR")
        self.assertEqual(calls, ["JFK-LHR"])
        self.assertTrue(again["cached"])
        tool("JFK-LHR", fail=True)
        tool("JFK-LHR", fail=True)
        self.assertEqual(calls, ["JFK-LHR", "JFK-LHR", "JFK-LHR"])


if __name__ == "__main__":
    unittest.main()
