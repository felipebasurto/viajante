"""Transition tests for the long-lived and per-call state machines in viajante.

One class per machine. Each test drives one transition through the real code and
asserts what the caller can observe: the returned value, the requests sent, the
record written, the driver stopped. Nothing here reads a private field.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from datetime import date
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante import browser, google_flights, google_flights_explore_browser, mcp_server
from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.dates import _sweep_per_day, search_flex
from viajante.flight_filters import OfferFilters
from viajante.google_flights import GoogleFlightsBlocked, RawFlightCard, SweepTransportError
from viajante.google_flights_explore_browser import capture_catalog
from viajante.models import FlightQuery, SearchErrorCode
from viajante.ratelimit import (
    GOOGLE_RATE_LIMIT_FILE,
    note_rate_limited,
    rate_limit_status,
)
from viajante.skiplagged import SkiplaggedError, _call_mcp

T0 = 1_800_000_000.0
ENDPOINT = "www.google.com/travel/flights"


class _IsolatedStateDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = tmp.name
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self.state_dir})
        env.start()
        self.addCleanup(env.stop)


# --- 1. Provider cooldown -------------------------------------------------------------


class CooldownTransitionTests(_IsolatedStateDir):
    def test_named_retry_after_extends_a_running_heuristic_cooldown(self) -> None:
        # A bare 429 starts a 2-minute guess. A later, named Retry-After that ends
        # later must replace it: docs/architecture.md says the named value takes precedence.
        note_rate_limited(None, now=T0, endpoint=ENDPOINT)
        note_rate_limited(1500.0, now=T0 + 5, endpoint=ENDPOINT)

        state = rate_limit_status(now=T0 + 6)

        assert state is not None
        self.assertEqual(state["basis"], "provider_retry_after")
        self.assertEqual(state["until"], T0 + 5 + 1500.0)

    def test_a_shorter_later_limit_never_shortens_a_running_cooldown(self) -> None:
        note_rate_limited(1500.0, now=T0, endpoint=ENDPOINT)
        note_rate_limited(None, now=T0 + 5, endpoint=ENDPOINT)

        state = rate_limit_status(now=T0 + 6)

        assert state is not None
        self.assertEqual(state["until"], T0 + 1500.0)

    def test_expired_cooldown_reads_as_clear_and_a_new_one_starts_fresh(self) -> None:
        note_rate_limited(60.0, now=T0, endpoint=ENDPOINT)
        self.assertIsNone(rate_limit_status(now=T0 + 61))

        note_rate_limited(None, now=T0 + 100, endpoint=ENDPOINT)

        state = rate_limit_status(now=T0 + 101)
        assert state is not None
        self.assertEqual(state["basis"], "heuristic_http_429")

    def test_corrupt_record_reads_as_clear_by_design(self) -> None:
        # Fail-open is the rule: an unreadable record is no record. The next request
        # gets Google's own answer, and a 429 writes a fresh record.
        path = os.path.join(self.state_dir, GOOGLE_RATE_LIMIT_FILE)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")

        self.assertIsNone(rate_limit_status(now=T0))


# --- 2. Shared sweep client -----------------------------------------------------------


class _FreshClient:
    def __init__(self, proxy=None) -> None:
        self.proxy = proxy

    def close(self) -> None:
        return None


class _ClientThatResetsOnClose:
    """Stands in for the client being replaced. Its close() is where another thread
    could reset the shared client, inside the unlocked window of the swap."""

    def close(self) -> None:
        google_flights.reset_shared_chrome_sweep_client()


class SharedSweepClientTransitionTests(unittest.TestCase):
    def test_a_reset_during_a_swap_does_not_return_none(self) -> None:
        with (
            patch.object(google_flights, "ChromeSweepClient", _FreshClient),
            patch.object(google_flights, "_SHARED_CLIENT", _ClientThatResetsOnClose()),
            patch.object(google_flights, "_SHARED_CLIENT_PROXY", None),
            patch.object(google_flights, "_SHARED_CLIENT_MODE", "standard"),
        ):
            client = google_flights.shared_chrome_sweep_client(proxy="http://proxy.test:8080")

        self.assertIsInstance(client, _FreshClient)
        self.assertEqual(getattr(client, "proxy", None), "http://proxy.test:8080")

    def test_same_proxy_and_mode_reuses_the_live_client(self) -> None:
        live = _FreshClient()
        with (
            patch.object(google_flights, "ChromeSweepClient", _FreshClient),
            patch.object(google_flights, "_SHARED_CLIENT", live),
            patch.object(google_flights, "_SHARED_CLIENT_PROXY", None),
            patch.object(google_flights, "_SHARED_CLIENT_MODE", "standard"),
        ):
            self.assertIs(google_flights.shared_chrome_sweep_client(), live)


# --- 3. Chromium session startup ------------------------------------------------------


class _RaisingChromium:
    def launch(self, **options):
        raise RuntimeError("Executable doesn't exist at /opt/chromium")


class _Driver:
    def __init__(self, chromium) -> None:
        self.chromium = chromium
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Starter:
    """Hands out a new driver on each start(), like sync_playwright().start()."""

    def __init__(self, make_chromium) -> None:
        self._make = make_chromium
        self.drivers: list[_Driver] = []

    def start(self):
        driver = _Driver(self._make())
        self.drivers.append(driver)
        return driver


class ChromiumStartupTransitionTests(_IsolatedStateDir):
    def _session(self) -> ChromiumSession:
        config = BrowserSessionConfig(
            state_filename="state.json", locale="en", html_lang="en", currency="USD"
        )
        return ChromiumSession(self.state_dir_path(), config)

    def state_dir_path(self):
        from pathlib import Path

        return Path(self.state_dir)

    def test_failed_launch_stops_the_driver_it_started(self) -> None:
        starter = _Starter(_RaisingChromium)
        session = self._session()
        with patch.object(browser, "_sync_playwright", return_value=starter):
            with self.assertRaises(RuntimeError):
                session.new_page()

        self.assertEqual(len(starter.drivers), 1)
        self.assertTrue(starter.drivers[0].stopped)

    def test_every_failed_start_leaves_no_driver_running(self) -> None:
        starter = _Starter(_RaisingChromium)
        session = self._session()
        with patch.object(browser, "_sync_playwright", return_value=starter):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    session.new_page()

        self.assertEqual(len(starter.drivers), 2)
        self.assertTrue(all(driver.stopped for driver in starter.drivers))


# --- 4. Explore catalog capture -------------------------------------------------------


class _Body:
    def __init__(self, text: str, *, error: BaseException | None = None) -> None:
        self._text = text
        self._error = error

    def text(self) -> str:
        if self._error is not None:
            raise self._error
        return self._text


class _Request:
    def __init__(self, post_data: str | None) -> None:
        self.post_data = post_data


class _Response:
    def __init__(self, url: str, status: int, body: _Body, post: str | None = "f.req=1"):
        self.url = url
        self.status = status
        self.headers: dict[str, str] = {}
        self._body = body
        self.request = _Request(post)

    def text(self) -> str:
        return self._body.text()


class _Navigation:
    def __init__(self, url: str) -> None:
        self.url = url
        self.status = 200

    def text(self) -> str:
        return ""


class _ExplorePage:
    """Plays a scripted list of responses to the listener the way Playwright does
    while the page loads. The catalog service URL is the one capture_catalog watches."""

    def __init__(self, scripted: list[_Response]) -> None:
        self.url = "https://www.google.com/travel/explore"
        self._scripted = scripted
        self._listeners = []

    def on(self, event: str, callback) -> None:
        self._listeners.append(callback)

    def goto(self, url: str, **options):
        for response in self._scripted:
            for callback in self._listeners:
                callback(response)
        return _Navigation(url)

    def wait_for_timeout(self, milliseconds: float) -> None:
        return None

    def close(self) -> None:
        return None


class _SessionStub:
    def __init__(self, page: _ExplorePage) -> None:
        self._page = page

    def new_page(self):
        return self._page


def _as_session(page: _ExplorePage) -> ChromiumSession:
    """capture_catalog only calls new_page() on the session; the stub supplies that."""
    return cast(ChromiumSession, _SessionStub(page))


CATALOG = "https://www.google.com/_/TravelFrontendUi/data/" + "GetExploreDestinations"


def _check_ok(body: str, *, response=None, retry_after_seconds=None) -> None:
    return None


class ExploreCaptureTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(google_flights_explore_browser, "_EXPLORE_CATALOG_WAIT_MS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_captured_catalog_is_returned_with_its_request_body(self) -> None:
        page = _ExplorePage([_Response(CATALOG, 200, _Body("catalog-body"), post="req-a")])

        pairs = capture_catalog(
            _as_session(page), "https://www.google.com/travel/explore", _check_ok
        )

        self.assertEqual(pairs, [("req-a", "catalog-body")])

    def test_a_capture_wins_over_a_transport_error_in_the_same_load(self) -> None:
        # A read error on one response must not hide a catalog that did arrive.
        page = _ExplorePage(
            [
                _Response(CATALOG, 200, _Body("", error=RuntimeError("net::ERR_ABORTED"))),
                _Response(CATALOG, 200, _Body("catalog-body")),
            ]
        )

        pairs = capture_catalog(
            _as_session(page), "https://www.google.com/travel/explore", _check_ok
        )

        self.assertEqual(pairs[-1][1], "catalog-body")

    def test_only_a_transport_error_raises_it(self) -> None:
        page = _ExplorePage(
            [_Response(CATALOG, 200, _Body("", error=RuntimeError("net::ERR_CONNECTION_RESET")))]
        )

        with self.assertRaises(SweepTransportError):
            capture_catalog(_as_session(page), "https://www.google.com/travel/explore", _check_ok)

    def test_no_catalog_request_is_a_timeout_transport_error(self) -> None:
        page = _ExplorePage([])

        with self.assertRaises(SweepTransportError) as raised:
            capture_catalog(_as_session(page), "https://www.google.com/travel/explore", _check_ok)

        self.assertTrue(raised.exception.timeout)

    def test_a_captured_429_is_raised_through_the_check(self) -> None:
        def check(body, *, response=None, retry_after_seconds=None):
            if response is not None and response.status == 429:
                raise GoogleFlightsBlocked("Google Explore returned HTTP 429.", status=429)

        page = _ExplorePage([_Response(CATALOG, 429, _Body("throttled"))])

        with self.assertRaises(GoogleFlightsBlocked):
            capture_catalog(_as_session(page), "https://www.google.com/travel/explore", check)


# --- 5. Sequential day loop: stop sending after a provider block ----------------------


class _BlockingDaySource:
    """No latch of its own, so the loop alone must stop. Blocks on the second call."""

    transport = "other"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, trip):
        self.calls += 1
        if self.calls == 2:
            raise GoogleFlightsBlocked("Google Flights HTTP 429", status=429)
        return ()

    def close(self) -> None:
        return None


class DayLoopBlockTransitionTests(unittest.TestCase):
    def test_no_request_is_sent_for_days_after_a_block(self) -> None:
        source = _BlockingDaySource()
        seed = FlightQuery("JFK", "LHR", date(2027, 3, 1), max_stops=1, adults=1)

        rows = _sweep_per_day(
            source,
            seed,
            date(2027, 3, 1),
            date(2027, 3, 6),
            None,
            lambda _message: None,
            OfferFilters(),
            0,
        )

        self.assertEqual(source.calls, 2)
        self.assertEqual(len(rows), 6)
        self.assertEqual(_error_of(rows[1]).code, SearchErrorCode.BLOCKED)
        for row in rows[2:]:
            error = _error_of(row)
            self.assertEqual(error.code, SearchErrorCode.BLOCKED)
            self.assertTrue(error.message.startswith("Not sent"), error.message)


def _error_of(row):
    """The row's error. A row the transition should have failed must not pass as ``None``."""
    error = row.error
    if error is None:
        raise AssertionError("expected a failed row")
    return error


# --- 6. Flex window: a block outranks an earlier row failure -------------------------


class _ScriptedDaySource:
    """Answers each day in call order: a tuple of cards, or an exception to raise."""

    transport = "other"

    def __init__(self, answers: list[object]) -> None:
        self._answers = list(answers)
        self.calls = 0
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(self, trip):
        answer = self._answers[self.calls]
        self.calls += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self) -> None:
        return None


def _priced(price: str) -> RawFlightCard:
    return RawFlightCard(
        airline="Iberia",
        departure="08:00",
        arrival="09:20",
        duration="1 hr 20 min",
        stops="Nonstop",
        price=price,
    )


class FlexBlockPrecedenceTransitionTests(unittest.TestCase):
    def test_a_block_outranks_an_earlier_ordinary_failure_in_the_window(self) -> None:
        source = _ScriptedDaySource(
            [
                RuntimeError("boom"),  # first day: an ordinary failure
                (_priced("€100"),),  # second day: priced
                GoogleFlightsBlocked("Google Flights HTTP 429", status=429),  # third: blocked
            ]
        )

        report = search_flex("JFK", "LHR", date(2027, 3, 3), 3, source=source)

        self.assertIsNone(report.chosen_date)
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.BLOCKED)
        self.assertEqual(source.calls, 3)


# --- 7. Skiplagged MCP session -------------------------------------------------------


class _SkiplaggedServer:
    """A stateful MCP endpoint that knows only the session it issued last. A call with any
    other session id gets 404, as a server that restarted would answer."""

    def __init__(self) -> None:
        self.issued = 0
        self.current: str | None = None
        self.tool_session_ids: list[str | None] = []
        self.refuse_tools = False

    def restart(self) -> None:
        self.current = None

    def __call__(
        self, _url: str, payload: dict, headers: Mapping[str, str]
    ) -> tuple[int, dict, str]:
        method = payload.get("method")
        if method == "initialize":
            self.issued += 1
            self.current = f"s{self.issued}"
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
            return 200, {"mcp-session-id": self.current}, body
        if method == "notifications/initialized":
            return 202, {}, ""
        session = headers.get("Mcp-Session-Id")
        self.tool_session_ids.append(session)
        if self.refuse_tools or session is None or session != self.current:
            return 404, {}, ""
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})


class SkiplaggedSessionTransitionTests(unittest.TestCase):
    def test_a_stale_session_is_dropped_and_the_call_retried_on_a_new_one(self) -> None:
        server = _SkiplaggedServer()
        self.assertEqual(_call_mcp({"q": 1}, rpc=server), {"ok": True})

        server.restart()
        self.assertEqual(_call_mcp({"q": 2}, rpc=server), {"ok": True})

        self.assertEqual(server.issued, 2)  # one re-handshake, not one per call
        self.assertEqual(server.tool_session_ids, ["s1", "s1", "s2"])

    def test_a_second_stale_answer_raises_instead_of_looping(self) -> None:
        server = _SkiplaggedServer()
        _call_mcp({"q": 1}, rpc=server)
        server.refuse_tools = True
        before = len(server.tool_session_ids)

        with self.assertRaises(SkiplaggedError):
            _call_mcp({"q": 2}, rpc=server)

        self.assertEqual(len(server.tool_session_ids) - before, 2)  # the try, and one retry


class ProgressRelayCloseTests(unittest.TestCase):
    """ProgressRelay is final once closed: a held line never flushes, and no later call sends."""

    def test_close_drops_the_held_line_and_cancels_its_timer(self) -> None:
        sent: list[str] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append(message)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            relay = mcp_server.ProgressRelay(Ctx(), loop, 0.05)
            # The first line sends at once; the second is held behind the interval.
            await loop.run_in_executor(None, lambda: (relay("[1/3] first"), relay("[2/3] held")))
            relay.close()
            await asyncio.sleep(0.15)

        asyncio.run(main())
        self.assertEqual(sent, ["[1/3] first"])

    def test_calls_after_close_send_nothing(self) -> None:
        sent: list[str] = []

        class Ctx:
            async def report_progress(self, progress, total, message):
                sent.append(message)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            relay = mcp_server.ProgressRelay(Ctx(), loop, 0)
            relay.close()
            await loop.run_in_executor(None, lambda: relay("[1/2] late"))
            await relay.drain()

        asyncio.run(main())
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
