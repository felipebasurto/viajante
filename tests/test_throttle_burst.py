"""Repeated status-13 and missing-payload signals record the shared cooldown."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import _isolate  # noqa: F401
from test_explore import _explore_post
from test_google_flights_public import _card, _context, _ds1_error_frame
from viajante.dates import search_dates
from viajante.envelope import stamp_search
from viajante.google_flights import (
    ChromeSweepClient,
    GoogleFlightsBlocked,
    SweepHttpResponse,
    SweepPost,
)
from viajante.google_flights_page import parse_shopping_page
from viajante.google_flights_public import (
    GoogleFlightsMarkupError,
    PublicGoogleFlightsHttpSource,
    classify_failure,
)
from viajante.models import FlightQuery
from viajante.ratelimit import (
    RATE_LIMIT_COOLDOWN_SECONDS,
    THROTTLE_REPEAT_THRESHOLD,
    THROTTLE_REPEAT_WINDOW_SECONDS,
    note_repeated_throttle,
    rate_limit_status,
)

T0 = 1_800_000_000.0
DAY = date(2026, 12, 28)
FLIGHTS = "https://www.google.com/travel/flights?tfs=secret"


class _StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)


def _signal(now: float) -> dict | None:
    return note_repeated_throttle(now=now, endpoint=FLIGHTS)


class BurstCounterTests(_StateDir):
    def test_the_third_signal_records_the_guess_and_a_later_one_does_not_extend_it(self) -> None:
        self.assertIsNone(_signal(T0))
        self.assertIsNone(_signal(T0 + 1))
        self.assertIsNone(rate_limit_status(now=T0 + 1))
        state = _signal(T0 + 2)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["basis"], "heuristic_rpc_13")
        self.assertEqual(state["cause"], "rpc_13")
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(state["until"], T0 + 2 + RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(state["endpoint"], "www.google.com/travel/flights")
        later = _signal(T0 + 50)
        assert later is not None
        self.assertEqual(later["until"], state["until"])
        self.assertEqual(rate_limit_status(now=T0 + 50)["until"], state["until"])

    def test_hits_older_than_the_window_do_not_count(self) -> None:
        self.assertEqual(THROTTLE_REPEAT_THRESHOLD, 3)
        self.assertEqual(THROTTLE_REPEAT_WINDOW_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertIsNone(_signal(T0))
        self.assertIsNone(_signal(T0 + 1))
        self.assertIsNone(_signal(T0 + THROTTLE_REPEAT_WINDOW_SECONDS + 2))
        self.assertIsNone(rate_limit_status(now=T0 + THROTTLE_REPEAT_WINDOW_SECONDS + 2))
        self.assertIsNone(_signal(T0 + THROTTLE_REPEAT_WINDOW_SECONDS + 3))
        recorded = _signal(T0 + THROTTLE_REPEAT_WINDOW_SECONDS + 4)
        self.assertIsNotNone(recorded)


class _FakeHttp:
    def __init__(self, url: str, text: str) -> None:
        self.url = url
        self.text = text
        self.status_code = 200
        self.headers: dict[str, str] = {}


class _FrameSweep(ChromeSweepClient):
    """One stream, so a burst can stop jobs still waiting on the semaphore."""

    def __init__(self, html: str) -> None:
        self._asyncio = asyncio
        self._streams = 1
        self._proxied = False
        self._session = self
        self._html = html
        self.started: list[str] = []

    async def get(self, url, timeout, allow_redirects=True):
        self.started.append(url)
        return _FakeHttp(url, self._html)


class ErrorFrameBurstTests(_StateDir):
    def test_queued_gets_stop_once_the_third_status_13_frame_returns(self) -> None:
        client = _FrameSweep(_context(currency="GBP") + _ds1_error_frame(13))
        jobs = [SweepPost(f"https://www.google.com/travel/flights/{i}", "", {}) for i in range(5)]
        out: list[SweepHttpResponse | None] = [None] * len(jobs)
        responses = asyncio.run(client._apost_many(jobs, 1.0, out, get=True))
        self.assertEqual(len(client.started), THROTTLE_REPEAT_THRESHOLD)
        self.assertFalse(responses[0].rate_limited)
        self.assertTrue(responses[0].throttle_observed)
        self.assertTrue(responses[2].rate_limited)
        self.assertEqual(responses[2].cooldown_basis, "heuristic_rpc_13")
        self.assertIn("likely blocking this shared IP", responses[2].rate_limit or "")
        self.assertIn("npx -y @viajante/mcp", responses[2].rate_limit or "")
        self.assertIn("pip install viajante", responses[2].rate_limit or "")
        self.assertRegex(responses[2].rate_limit or "", r"~\d+ min")
        self.assertIsNotNone(responses[2].retry_until)
        for response in responses[3:]:
            self.assertFalse(response.request_sent)
            self.assertEqual(response.attempts, 0)
            self.assertTrue(response.stopped)
            self.assertIn("Not sent.", response.text)
        self.assertEqual(rate_limit_status()["basis"], "heuristic_rpc_13")

    def test_a_status_other_than_13_and_a_proxied_frame_do_not_count(self) -> None:
        other = _context() + _ds1_error_frame(3)
        proxied = ChromeSweepClient.__new__(ChromeSweepClient)
        proxied._proxied = True

        async def send(text: str) -> _FakeHttp:
            return _FakeHttp("https://www.google.com/travel/flights", text)

        for _ in range(THROTTLE_REPEAT_THRESHOLD):
            page = asyncio.run(proxied._exchange(lambda: send(other), 1.0))
            self.assertFalse(page.rate_limited)
            direct = ChromeSweepClient.__new__(ChromeSweepClient)
            direct._proxied = False
            page = asyncio.run(
                direct._exchange(lambda: send(_context() + _ds1_error_frame(3)), 1.0)
            )
            self.assertFalse(page.throttle_observed)
        proxied_source = PublicGoogleFlightsHttpSource(
            currency="GBP", client=object(), proxy="http://proxy.example:8080"
        )
        frame = SweepHttpResponse(200, _context() + _ds1_error_frame(13), FLIGHTS)
        for _ in range(THROTTLE_REPEAT_THRESHOLD):
            with self.assertRaises(GoogleFlightsBlocked) as caught:
                proxied_source._parse_response(frame, frame.url)
            self.assertIsNone(caught.exception.status)
        self.assertIsNone(rate_limit_status())

    def test_partial_calendar_carries_retry_after_and_the_next_search_sends_nothing(self) -> None:
        card = _card("LHR", "VNO", DAY, "08:00", "BA101", "£120")
        html_ok = _context(currency="GBP")
        html_frame = html_ok + _ds1_error_frame(13)

        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def get_many(self, urls, *, timeout):
                self.calls += 1
                return [
                    SweepHttpResponse(200, html_ok if index == 0 else html_frame, url)
                    for index, url in enumerate(urls)
                ]

        def parse(html, *, currency):
            if "ErrorResponse" in html:
                return parse_shopping_page(html, currency=currency)
            return (card,)

        client = Client()
        source = PublicGoogleFlightsHttpSource(currency="GBP", client=client)
        with patch("viajante.google_flights_public.parse_shopping_page", side_effect=parse):
            report = search_dates(
                "LHR",
                "VNO",
                DAY,
                DAY + timedelta(days=3),
                adults=2,
                max_stops=0,
                currency="GBP",
                source=source,
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual([row.status for row in report.days], ["ok", "error", "error", "error"])
        errors = [row.error for row in report.days[1:]]
        self.assertTrue(
            all(error is not None and error.code.value == "blocked" for error in errors)
        )
        self.assertFalse(errors[0].rate_limited)
        self.assertFalse(errors[1].rate_limited)
        self.assertTrue(errors[2].rate_limited)
        self.assertIsNotNone(errors[2].retry_until)
        payload = stamp_search(report.to_dict())
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["completeness"], "partial")
        self.assertIsNone(payload["empty_reason"])
        self.assertIsNotNone(payload["retry_after"])
        self.assertGreater(payload["retry_after_seconds"], 0)
        self.assertEqual(payload["retry_after"], errors[2].to_dict()["retry_after"])

        with patch("viajante.google_flights.shared_chrome_sweep_client") as network:
            follow = PublicGoogleFlightsHttpSource(currency="GBP")
            with self.assertRaises(GoogleFlightsBlocked) as caught:
                follow.fetch(FlightQuery("LTN", "VNO", DAY, adults=2))
        network.assert_not_called()
        error = classify_failure(caught.exception)
        self.assertIn("likely blocking this shared IP", error.message)
        self.assertRegex(error.message, r"~\d+ min")
        self.assertTrue(error.rate_limited)
        self.assertFalse(error.diagnostics["request_sent"])
        self.assertEqual(error.diagnostics["attempts"], 0)
        self.assertIsNotNone(error.retry_until)


class MissingPayloadBurstTests(_StateDir):
    def test_three_catalogs_without_a_payload_record_the_cooldown(self) -> None:
        shell = "<!doctype html><html><body>empty catalog</body></html>"
        source = PublicGoogleFlightsHttpSource(currency="GBP", client=object())
        source._explore_page = lambda url: [(_explore_post("LHR", DAY.isoformat()), shell)]
        for _ in range(THROTTLE_REPEAT_THRESHOLD - 1):
            with self.assertRaises(GoogleFlightsMarkupError):
                source.fetch_explore("LHR", DAY)
            self.assertIsNone(rate_limit_status())
        with self.assertRaises(GoogleFlightsBlocked) as caught:
            source.fetch_explore("LHR", DAY)
        self.assertEqual(caught.exception.status, 429)
        self.assertIsNotNone(caught.exception.retry_until)
        self.assertEqual(rate_limit_status()["basis"], "heuristic_rpc_13")
        self.assertIn("likely blocking this shared IP", str(caught.exception))
        self.assertRegex(str(caught.exception), r"~\d+ min")

    def test_a_proxied_missing_payload_does_not_pause_direct_searches(self) -> None:
        shell = "<!doctype html><html><body>empty catalog</body></html>"
        source = PublicGoogleFlightsHttpSource(
            currency="GBP", client=object(), proxy="http://proxy.example:8080"
        )
        source._explore_page = lambda url: [(_explore_post("LHR", DAY.isoformat()), shell)]
        for _ in range(THROTTLE_REPEAT_THRESHOLD):
            with self.assertRaises(GoogleFlightsMarkupError):
                source.fetch_explore("LHR", DAY)
        self.assertIsNone(rate_limit_status())
