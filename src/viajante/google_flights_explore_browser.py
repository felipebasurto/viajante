"""Chromium run for the public Google Explore catalog: the page's own catalog request."""

from __future__ import annotations

import contextlib
import time
from typing import Callable

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.control import SearchDeadline, checkpoint
from viajante.google_flights import (
    GoogleFlightsBlocked,
    SweepHttpResponse,
    SweepTransportError,
    _retry_after_seconds,
    looks_blocked,
)
from viajante.google_flights_detail import (
    CONSENT_SETTLE_MS,
    PAGE_TIMEOUT_MS,
    STATE_FILENAME,
    GoogleFlightsSource,
)
from viajante.models import FETCH_LOCALE
from viajante.storage import default_state_dir

_EXPLORE_CATALOG_SERVICE = "GetExploreDestinations"
_EXPLORE_CATALOG_WAIT_MS = 45_000

# check(body, *, response=None, retry_after_seconds=None) raises for a block, throttle or error.
Check = Callable[..., None]


def explore_session(
    *, html_lang: str, currency: str, country: str | None, proxy: str | None
) -> ChromiumSession:
    """The Chromium session the Explore page runs in. Creating it opens nothing."""
    return ChromiumSession(
        default_state_dir(),
        BrowserSessionConfig(
            state_filename=STATE_FILENAME,
            locale=FETCH_LOCALE,
            html_lang=html_lang,
            currency=currency,
            country=country,
            proxy=proxy,
        ),
    )


def capture_catalog(session: ChromiumSession, url: str, check: Check) -> list[tuple[object, str]]:
    """Load the Explore page; return its catalog request/response pairs.

    ``check`` raises for a block, throttle or error response as soon as one arrives, so a
    provider stop ends the wait at once instead of after the full catalog timeout.
    """
    page = session.new_page()
    captured: list[tuple[object, SweepHttpResponse, float | None]] = []

    def _capture(response) -> None:
        if _EXPLORE_CATALOG_SERVICE not in response.url:
            return
        try:
            body = response.text()
        except SearchDeadline:
            raise
        except Exception:
            body = ""
        post = None
        try:
            post = response.request.post_data
        except SearchDeadline:
            raise
        except Exception:
            post = None
        captured.append(
            (
                post,
                SweepHttpResponse(response.status, body, response.url),
                _retry_after_seconds(response),
            )
        )

    page.on("response", _capture)
    try:
        navigation = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        checkpoint()
        if navigation is not None and navigation.status >= 400:
            check(
                navigation.text(),
                response=SweepHttpResponse(navigation.status, "", navigation.url),
                retry_after_seconds=_retry_after_seconds(navigation),
            )
        if "consent.google" in page.url:
            GoogleFlightsSource._dismiss_consent(page)
        if looks_blocked("", page.url):
            raise GoogleFlightsBlocked(f"Google Flights blocked the browser at {page.url}")
        deadline = time.monotonic() + _EXPLORE_CATALOG_WAIT_MS / 1000
        while not captured and time.monotonic() < deadline:
            checkpoint()
            page.wait_for_timeout(250)
        checkpoint()
        for _post, response, retry_after in captured:
            check(response.text, response=response, retry_after_seconds=retry_after)
        # Keep Playwright pumping response events while checking control between sleeps.
        for elapsed in range(0, CONSENT_SETTLE_MS, 250):
            checkpoint()
            page.wait_for_timeout(min(250, CONSENT_SETTLE_MS - elapsed))
            checkpoint()
            for _post, response, retry_after in captured:
                check(response.text, response=response, retry_after_seconds=retry_after)
    finally:
        with contextlib.suppress(Exception):
            page.close()
    if not captured:
        raise SweepTransportError(
            "Google Explore issued no catalog request before the wait expired.",
            timeout=True,
        )
    return [(post, response.text) for post, response, _retry_after in captured]
