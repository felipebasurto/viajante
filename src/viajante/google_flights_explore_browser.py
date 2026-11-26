"""Chromium run for the public Google Explore catalog: the page's own catalog request."""

from __future__ import annotations

import contextlib
import time
from dataclasses import replace
from typing import Callable

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.control import SearchDeadline, bounded_ms, checkpoint
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


def _catalog_response_text(response) -> str:
    """Decoded GetExploreDestinations body.

    Playwright ``response.text()`` can return an empty string if it is read
    before the body is ready. Fall back to ``body()`` so a short RPC
    status-13 envelope is not mistaken for a missing catalog payload.
    """
    body = response.text()
    if body:
        return body
    raw_fn = getattr(response, "body", None)
    if not callable(raw_fn):
        return body or ""
    try:
        raw = raw_fn() or b""
    except SearchDeadline:
        raise
    except Exception:
        return body or ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (bytes, bytearray)) and raw:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return body or ""
    return body or ""


def capture_catalog(
    session: ChromiumSession, url: str, check: Check
) -> list[tuple[str | None, str]]:
    """Load the Explore page; return its catalog request/response pairs.

    ``check`` raises for a block, throttle or error response as soon as one arrives, so a
    provider stop ends the wait at once instead of after the full catalog timeout.
    """
    page = session.new_page()
    captured: list[tuple[str | None, SweepHttpResponse, float | None, object]] = []

    read_error: SweepTransportError | None = None

    def _refresh_empty() -> None:
        nonlocal read_error
        for i, (post, response, retry_after, raw) in enumerate(captured):
            if response.text:
                continue
            try:
                body = _catalog_response_text(raw)
            except SearchDeadline:
                raise
            except Exception as exc:
                transport = _network_failure(exc)
                if transport is not None:
                    read_error = transport
                continue
            if body:
                captured[i] = (post, replace(response, text=body), retry_after, raw)

    def _capture(response) -> None:
        nonlocal read_error
        if _EXPLORE_CATALOG_SERVICE not in response.url:
            return
        try:
            body = _catalog_response_text(response)
        except SearchDeadline:
            raise
        except Exception as exc:
            transport = _network_failure(exc)
            if transport is not None:
                read_error = transport
                return
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
                response,
            )
        )

    page.on("response", _capture)
    try:
        try:
            navigation = page.goto(
                url, wait_until="domcontentloaded", timeout=bounded_ms(PAGE_TIMEOUT_MS)
            )
        except SearchDeadline:
            raise
        except Exception as exc:
            checkpoint()
            transport = _network_failure(exc)
            if transport is not None:
                raise transport from exc
            raise
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
        while not captured and read_error is None and time.monotonic() < deadline:
            checkpoint()
            page.wait_for_timeout(250)
        checkpoint()
        _refresh_empty()
        for _post, response, retry_after, _raw in captured:
            check(response.text, response=response, retry_after_seconds=retry_after)
        for elapsed in range(0, CONSENT_SETTLE_MS, 250):
            checkpoint()
            page.wait_for_timeout(min(250, CONSENT_SETTLE_MS - elapsed))
            checkpoint()
            _refresh_empty()
            for _post, response, retry_after, _raw in captured:
                check(response.text, response=response, retry_after_seconds=retry_after)
    finally:
        with contextlib.suppress(Exception):
            page.close()
    if read_error is not None and not captured:
        raise read_error
    if not captured:
        raise SweepTransportError(
            "Google Explore issued no catalog request before the wait expired.",
            timeout=True,
        )
    return [(post, response.text) for post, response, _retry_after, _raw in captured]


def _network_failure(exc: BaseException) -> SweepTransportError | None:
    """A Chromium net error or timeout. Not a provider block.

    ``net::ERR_CONNECTION_CLOSED`` and the other ``net::ERR_*`` failures happen
    before any HTTP status. They are transport, retried once by the caller.
    """
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.casefold()
    if "net::err_" in lowered:
        return SweepTransportError(text)
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.casefold():
        return SweepTransportError(text, timeout=True)
    return None
