from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import _isolate  # noqa: F401
from viajante.control import SearchControl, SearchDeadline, active
from viajante.google_flights import GoogleFlightsMarkupError
from viajante.google_flights_detail import (
    CONSENT_SELECTORS,
    PAGE_TIMEOUT_MS,
    READY_PROBE_MS,
    GoogleFlightsSource,
)
from viajante.google_flights_public import classify_failure
from viajante.models import SearchErrorCode

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
except ImportError:  # pragma: no cover - the browser extra is optional
    PlaywrightTimeout = None


class _ConsentLocator:
    def __init__(self, page: "_ConsentPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_ConsentLocator":
        return self

    def count(self) -> int:
        return 1 if self._selector in self._page.present else 0

    def click(self, timeout: int) -> None:
        self._page.clicks.append(self._selector)


class _ConsentPage:
    def __init__(self, present: set[str]) -> None:
        self.present = present
        self.clicks: list[str] = []
        self.waits: list[int] = []

    def locator(self, selector: str) -> _ConsentLocator:
        return _ConsentLocator(self, selector)

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


class ConsentIsRejectOnly(unittest.TestCase):
    def test_no_accept_selector_is_ever_configured(self) -> None:
        self.assertFalse(any("accept" in selector.lower() for selector in CONSENT_SELECTORS))

    def test_accept_and_reject_both_present_clicks_only_reject(self) -> None:
        page = _ConsentPage({'text="Accept all"', 'text="Reject all"'})
        GoogleFlightsSource._dismiss_consent(page)
        self.assertEqual(page.clicks, ['text="Reject all"'])

    def test_accept_only_present_clicks_nothing(self) -> None:
        page = _ConsentPage({'text="Accept all"'})
        GoogleFlightsSource._dismiss_consent(page)
        self.assertEqual(page.clicks, [])

    def test_a_spent_deadline_is_not_swallowed_before_consent_settles(self) -> None:
        clock = {"now": 0.0}
        control = SearchControl(deadline_seconds=1, clock=lambda: clock["now"])
        clock["now"] = 5.0
        page = _ConsentPage({'text="Reject all"'})
        with active(control), self.assertRaises(SearchDeadline):
            GoogleFlightsSource._dismiss_consent(page)
        self.assertEqual(page.clicks, [])
        self.assertEqual(page.waits, [])

    def test_consent_settle_is_cut_to_the_time_left(self) -> None:
        clock = {"now": 0.0}
        control = SearchControl(deadline_seconds=0.4, clock=lambda: clock["now"])
        page = _ConsentPage(set())
        with active(control):
            GoogleFlightsSource._dismiss_consent(page)
        self.assertEqual(page.waits, [400])


class _ReadyLocator:
    def __init__(self, page: "_DriftPage") -> None:
        self._page = page

    @property
    def first(self) -> "_ReadyLocator":
        return self

    def wait_for(self, *, timeout: int) -> None:
        self._page.waits.append(timeout)
        if self._page.on_wait is not None:
            self._page.on_wait()
        appears_after = self._page.results_appear_after
        if appears_after is None or len(self._page.waits) < appears_after:
            raise self._page.timeout_error()


class _DriftPage:
    url = "https://www.google.com/travel/flights"

    def __init__(
        self,
        *,
        ready_state: str,
        results_appear_after: int | None = None,
        on_wait=None,
    ) -> None:
        self.ready_state = ready_state
        self.results_appear_after = results_appear_after
        self.on_wait = on_wait
        self.waits: list[int] = []
        self.closed = False

    def timeout_error(self):
        return PlaywrightTimeout("Timeout waiting for results")

    def goto(self, _url, **_kwargs) -> None:
        return None

    def locator(self, _selector: str) -> _ReadyLocator:
        return _ReadyLocator(self)

    def evaluate(self, script: str):
        if "readyState" in script:
            return self.ready_state
        return "<main>fixture</main>"

    def close(self) -> None:
        self.closed = True


class DeadlineCutWaits(unittest.TestCase):
    def test_board_reload_after_the_deadline_is_not_markup_drift(self) -> None:
        clock = {"now": 0.0}

        class Page:
            def goto(self, _url, **_kwargs):
                clock["now"] = 30.0
                raise TimeoutError("navigation timed out")

        source = GoogleFlightsSource(Path("unused"), session=MagicMock(), currency="USD")
        control = SearchControl(deadline_seconds=5, clock=lambda: clock["now"])
        with active(control), self.assertRaises(SearchDeadline):
            source._multi_reset(Page(), "https://www.google.com/travel/flights")


@unittest.skipIf(PlaywrightTimeout is None, "the browser extra is not installed")
class DriftIsNotSlowness(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="viajante-tests-")
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)

    def _fetch(self, page: _DriftPage) -> str:
        session = MagicMock()
        session.new_page.return_value = page
        source = GoogleFlightsSource(
            Path(os.environ["VIAJANTE_STATE_DIR"]), session=session, currency="USD"
        )
        return source._fetch_html(page.url)

    def test_detail_config_passes_proxy_to_chromium_session(self) -> None:
        with patch("viajante.google_flights_detail.ChromiumSession") as session:
            GoogleFlightsSource(Path("unused"), currency="USD", proxy="http://proxy.example:8080")
        self.assertEqual(session.call_args.args[1].proxy, "http://proxy.example:8080")

    def test_loaded_page_without_markers_is_markup_drift_after_probe_only(self) -> None:
        page = _DriftPage(ready_state="complete")
        with self.assertRaises(GoogleFlightsMarkupError) as raised:
            self._fetch(page)
        self.assertEqual(page.waits, [READY_PROBE_MS])
        self.assertEqual(classify_failure(raised.exception).code, SearchErrorCode.MARKUP_DRIFT)

    def test_page_still_loading_keeps_the_full_wait_and_succeeds(self) -> None:
        page = _DriftPage(ready_state="loading", results_appear_after=2)
        self.assertEqual(self._fetch(page), "<main>fixture</main>")
        self.assertEqual(page.waits, [READY_PROBE_MS, PAGE_TIMEOUT_MS])

    def test_deadline_shortened_probe_on_a_complete_page_is_not_markup_drift(self) -> None:
        clock = {"now": 0.0}
        control = SearchControl(deadline_seconds=5, clock=lambda: clock["now"])

        def expire() -> None:
            clock["now"] = 10.0

        page = _DriftPage(ready_state="complete", on_wait=expire)
        with active(control), self.assertRaises(SearchDeadline):
            self._fetch(page)
        self.assertEqual(page.waits, [5_000])

    def test_deadline_during_the_loading_wait_is_not_a_transport_timeout(self) -> None:
        clock = {"now": 0.0}
        control = SearchControl(deadline_seconds=30, clock=lambda: clock["now"])
        page = _DriftPage(ready_state="loading", results_appear_after=99)

        def expire_on_the_second_wait() -> None:
            if len(page.waits) >= 2:
                clock["now"] = 100.0

        page.on_wait = expire_on_the_second_wait
        with active(control), self.assertRaises(SearchDeadline):
            self._fetch(page)
        self.assertEqual(page.waits, [READY_PROBE_MS, 30_000])
