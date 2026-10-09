from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import _isolate  # noqa: F401
from viajante import mcp_handlers, skiplagged
from viajante.control import SearchCancelled, SearchControl, active
from viajante.flights import classify_failure
from viajante.google_flights import (
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    ChromeSweepClient,
    GoogleFlightsBlocked,
    SweepHttpResponse,
    SweepPost,
    _raise_if_blocked,
    note_rate_limited,
    rate_limit_status,
)
from viajante.google_flights_public import PublicGoogleFlightsHttpSource
from viajante.google_hotels import GoogleHotelsSource
from viajante.google_hotels_rpc import HotelsBlocked
from viajante.models import FlightQuery, HotelQuery
from viajante.ratelimit import GOOGLE_RATE_LIMIT_FILE, rate_limit_advice
from viajante.runtime import package_version
from viajante.storage import default_state_dir, write_json_atomic

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

    def test_an_absurd_retry_after_is_capped_not_recorded_whole(self) -> None:
        state = note_rate_limited(retry_after=10**12, now=T0)
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_MAX_COOLDOWN_SECONDS)
        state = note_rate_limited(retry_after=float("inf"), now=T0 + 10_000)
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_COOLDOWN_SECONDS)

    def test_a_persisted_record_out_of_range_is_unreadable(self) -> None:
        write_json_atomic(
            {
                "at": T0,
                "until": T0 * 1000,
                "cooldown_s": 1.0,
                "basis": "unknown",
                "cause": "unknown",
            },
            default_state_dir() / GOOGLE_RATE_LIMIT_FILE,
        )
        self.assertIsNone(rate_limit_status(now=T0 + 1))

    def test_a_stamp_far_from_now_or_past_float_range_is_unreadable(self) -> None:
        century = 100 * 365 * 86400
        cases = {
            "past float range": (10**400, 10**400 + 60),
            "stamped a century ahead": (T0 + century, T0 + century + 60),
        }
        path = default_state_dir() / GOOGLE_RATE_LIMIT_FILE
        for label, (at, until) in cases.items():
            with self.subTest(label):
                write_json_atomic({"at": at, "until": until, "cooldown_s": 60.0}, path)
                self.assertIsNone(rate_limit_status(now=T0 + 1))
        write_json_atomic({"at": T0, "until": T0 + 60, "cooldown_s": 60.0}, path)
        self.assertIsNotNone(rate_limit_status(now=T0 + 1))

    def test_named_retry_after_wins(self) -> None:
        state = note_rate_limited(retry_after=42, now=T0)
        self.assertEqual(state["cooldown_s"], 42)
        self.assertEqual(state["basis"], "provider_retry_after")
        self.assertEqual(state["cause"], "http_429")

    def test_heuristic_http_429_is_the_backward_compatible_default(self) -> None:
        state = note_rate_limited(now=T0)
        self.assertEqual(state["basis"], "heuristic_http_429")
        self.assertEqual(state["cause"], "http_429")

    def test_rpc_13_basis_and_cause_are_persisted(self) -> None:
        state = note_rate_limited(now=T0, basis="heuristic_rpc_13", cause="rpc_13")
        self.assertEqual(state["basis"], "heuristic_rpc_13")
        self.assertEqual(state["cause"], "rpc_13")
        path = os.path.join(os.environ["VIAJANTE_STATE_DIR"], "google-rate-limit.json")
        with open(path, encoding="utf-8") as stream:
            persisted = json.load(stream)
        self.assertEqual(persisted["basis"], "heuristic_rpc_13")
        self.assertEqual(persisted["cause"], "rpc_13")

    def test_old_state_reads_with_unknown_provenance(self) -> None:
        path = os.path.join(os.environ["VIAJANTE_STATE_DIR"], "google-rate-limit.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"at": T0, "until": T0 + 60, "cooldown_s": 60}, stream)
        state = rate_limit_status(now=T0 + 1)
        self.assertEqual(state["basis"], "unknown")
        self.assertEqual(state["cause"], "unknown")

    def test_a_non_numeric_cooldown_length_in_an_expired_record_is_not_a_crash(self) -> None:
        write_json_atomic(
            {
                "at": T0 - 100,
                "until": T0 - 10,
                "cooldown_s": "x",
                "basis": "unknown",
                "cause": "unknown",
            },
            default_state_dir() / GOOGLE_RATE_LIMIT_FILE,
        )
        state = note_rate_limited(now=T0)
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_COOLDOWN_SECONDS)


class CooldownGateTests(_StateDir):
    def test_flights_and_hotels_send_nothing_during_a_cooldown(self) -> None:
        note_rate_limited()
        departure = date.today() + timedelta(days=30)
        with (
            patch("viajante.google_flights.shared_chrome_sweep_client") as flights_client,
            patch("viajante.google_hotels.shared_chrome_sweep_client") as hotels_client,
        ):
            with self.assertRaises(GoogleFlightsBlocked) as flights:
                PublicGoogleFlightsHttpSource(currency="USD").fetch(
                    FlightQuery("JFK", "LHR", departure)
                )
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

    def test_proxied_search_is_not_paused(self) -> None:
        note_rate_limited()
        with patch("viajante.google_flights.shared_chrome_sweep_client") as client:
            PublicGoogleFlightsHttpSource(
                proxy="http://proxy.example:8080", currency="EUR"
            )._ensure_client()
        client.assert_called_once()


STATUS_13_BODY = ")]}'\n\n" + json.dumps(
    [["wrb.fr", "GetCalendarGrid", None, None, None, [13], "generic"]]
)
CALENDAR_URL = "https://www.google.com/_/FlightsFrontendUi/data/GetCalendarGrid"


class _FakeHttpResponse:
    url = CALENDAR_URL

    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {} if headers is None else headers


def _exchange(
    text: str,
    *,
    proxied: bool,
    status_code: int = 200,
    headers: dict | None = None,
) -> SweepHttpResponse:
    client = ChromeSweepClient.__new__(ChromeSweepClient)
    client._proxied = proxied

    async def send() -> _FakeHttpResponse:
        return _FakeHttpResponse(text, status_code, headers)

    return asyncio.run(client._exchange(send, 1.0))


class StatusThirteenTests(_StateDir):
    def test_direct_status_13_records_a_cooldown_and_says_why(self) -> None:
        out = _exchange(STATUS_13_BODY, proxied=False)
        self.assertEqual(out.status, 200)
        self.assertTrue(out.request_sent)
        self.assertEqual(out.attempts, 1)
        self.assertFalse(out.stopped)
        self.assertIn("RPC status 13", out.rate_limit or "")
        self.assertEqual(out.cooldown_basis, "heuristic_rpc_13")
        state = rate_limit_status()
        self.assertIsNotNone(state)
        self.assertEqual(state["basis"], "heuristic_rpc_13")
        self.assertEqual(state["cause"], "rpc_13")

    def test_proxied_status_13_does_not_pause_direct_searches(self) -> None:
        out = _exchange(STATUS_13_BODY, proxied=True)
        self.assertTrue(out.request_sent)
        self.assertEqual(out.attempts, 1)
        self.assertIsNone(out.rate_limit)
        self.assertIsNone(out.cooldown_basis)
        self.assertIsNone(rate_limit_status())

    def test_direct_http_429_without_retry_after_has_heuristic_basis(self) -> None:
        out = _exchange("slow down", proxied=False, status_code=429)
        self.assertEqual(out.cooldown_basis, "heuristic_http_429")
        state = rate_limit_status()
        self.assertIsNotNone(state)
        self.assertEqual(state["basis"], "heuristic_http_429")
        self.assertEqual(state["cause"], "http_429")

    def test_direct_http_429_with_retry_after_reports_provider_basis(self) -> None:
        out = _exchange("slow down", proxied=False, status_code=429, headers={"retry-after": "42"})
        self.assertEqual(out.cooldown_basis, "provider_retry_after")
        state = rate_limit_status()
        self.assertIsNotNone(state)
        self.assertEqual(state["cooldown_s"], 42)
        self.assertEqual(state["basis"], "provider_retry_after")
        self.assertEqual(state["cause"], "http_429")

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


class CancelledSweepCooldownTests(_StateDir):
    class CancellingSession:
        def __init__(self, cancel_event: threading.Event, status: int, text: str) -> None:
            self.cancel_event = cancel_event
            self.response = _FakeHttpResponse(text, status)

        async def post(self, *args, **kwargs):
            self.cancel_event.set()
            return self.response

    def test_cancelled_http_429_does_not_write_cooldown_across_loop_thread(self) -> None:
        self._assert_cancelled_rate_response(429, "slow down")

    def test_cancelled_status_13_does_not_write_cooldown_across_loop_thread(self) -> None:
        self._assert_cancelled_rate_response(200, STATUS_13_BODY)

    def test_cancelled_multiplexed_429_does_not_write_cooldown(self) -> None:
        self._assert_cancelled_rate_response(429, "slow down", multiplexed=True)

    def _assert_cancelled_rate_response(
        self, status: int, text: str, *, multiplexed: bool = False
    ) -> None:
        control = SearchControl()
        loop = asyncio.new_event_loop()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=run_loop, name="fake-sweep-loop", daemon=True)
        thread.start()
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._asyncio = asyncio
        client._loop = loop
        client._thread = thread
        client._proxied = False
        client._session = self.CancellingSession(control.cancel, status, text)
        try:
            with active(control), self.assertRaises(SearchCancelled):
                if multiplexed:
                    client.post_many(
                        [
                            SweepPost(CALENDAR_URL, "{}", {}),
                            SweepPost(CALENDAR_URL, "{}", {}),
                        ],
                        timeout=1.0,
                    )
                else:
                    client.post(CALENDAR_URL, data="{}", headers={}, timeout=1.0)
        finally:
            client.close()
        self.assertTrue(control.cancel.is_set())
        self.assertIsNone(rate_limit_status())


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

    def test_cancelled_live_429_does_not_write_cooldown(self) -> None:
        control = SearchControl()

        def cancel_then_429(url, payload, headers):
            control.cancel.set()
            return 429, {"retry-after": "90"}, ""

        with (
            active(control),
            patch("viajante.skiplagged._is_live", return_value=True),
            patch("viajante.skiplagged._pace"),
            self.assertRaises(SearchCancelled),
        ):
            skiplagged._call_mcp({}, rpc=cancel_then_429, tool="sk_hotels_search")
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


class CooldownProvenanceTests(_StateDir):
    def test_the_record_names_the_writing_version_and_endpoint_without_a_query(self) -> None:
        state = note_rate_limited(
            now=T0,
            endpoint="https://www.google.com/_/FlightsFrontendUi/data/batchexecute?rpcids=x&secret=1",
        )
        self.assertEqual(state["viajante_version"], package_version())
        self.assertEqual(state["endpoint"], "www.google.com/_/FlightsFrontendUi/data/batchexecute")
        self.assertNotIn("secret", json.dumps(state))

    def test_advice_names_the_writer_and_says_when_it_is_not_this_install(self) -> None:
        here = rate_limit_advice(
            note_rate_limited(now=T0, endpoint="www.google.com/travel"), sent=False
        )
        self.assertIn(f"recorded by viajante {package_version()} from www.google.com/travel", here)
        self.assertNotIn("may have caused it", here)
        stale = rate_limit_status(now=T0 + 1)
        stale["viajante_version"] = "1.4.0"
        advice = rate_limit_advice(stale)
        self.assertIn("recorded by viajante 1.4.0", advice)
        self.assertIn("another install on this machine may have caused it", advice)

    def test_an_old_record_without_provenance_is_named_as_such(self) -> None:
        path = os.path.join(os.environ["VIAJANTE_STATE_DIR"], "google-rate-limit.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"at": T0, "until": T0 + 600, "cooldown_s": 600}, stream)
        state = rate_limit_status(now=T0 + 1)
        self.assertIsNone(state["viajante_version"])
        self.assertIsNone(state["endpoint"])
        advice = rate_limit_advice(state)
        self.assertIn("recorded by a viajante that does not record its version", advice)
        self.assertIn(
            f"until {datetime.fromtimestamp(state['until'], timezone.utc):%H:%M} UTC", advice
        )


if __name__ == "__main__":
    unittest.main()
