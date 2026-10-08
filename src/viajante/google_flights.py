"""Google Flights URL building, consent, card parsing, and page source."""

from __future__ import annotations

import contextlib
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlencode, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.control import (
    SearchCancelled,
    SearchDeadline,
    checkpoint,
    current_control,
    wait_for_future,
)
from viajante.google_flights_rpc import (
    RawFlightCard,
    raw_rpc_error_status,
    rpc_error_status,
)
from viajante.models import (
    FETCH_LANGUAGE,
    FETCH_LOCALE,
    FlightLeg,
    MultiCity,
    RawJourneyLeg,
    RawLayover,
    RawSegment,
    Trip,
)
from viajante.parsers import normalize_clock, parse_price
from viajante.ratelimit import (  # noqa: F401 - re-exported for callers and tests
    NOT_SENT,
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    note_rate_limited,
    rate_limit_advice,
    rate_limit_status,
)
from viajante.sweep_config import get_sweep_config
from viajante.tfs import encode_tfs

SEARCH_URL = "https://www.google.com/travel/flights"
STATE_FILENAME = "pw_state_google.json"

SCRAPE_LANGUAGE = FETCH_LANGUAGE
# Owned `tfu` blob that selects result tabs; not produced by encode_tfs.
RESULT_TABS = "EgQIABABIgA"

PAGE_TIMEOUT_MS = 60_000
CONSENT_CLICK_TIMEOUT_MS = 5_000
CONSENT_SETTLE_MS = 1_500
HTTP_TIMEOUT_SECONDS = 30
# One replay on empty/drift/5xx. Happy path does not sleep. Not an anti-bot pause.
SWEEP_RETRY_BACKOFF_SECONDS = 0.05
# Browser-like HTTP/2 stream cap. Dates fallback is at most 31 days.
_SWEEP_STREAMS = 8
HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
BLOCK_URL_MARKERS = ("consent.google", "/sorry/", "ipv4.google.com/sorry")
BLOCK_BODY_MARKERS = (
    "our systems have detected unusual traffic",
    "unusual traffic from your computer network",
)
# Tiny unknown shells are a block, not a results-page parse.
_SHORT_SHELL_CHARS = 200
_SHORT_SHELL_MARK = "short unknown shell"

RESULTS_SELECTOR = ".eQ35Ce"
EMPTY_STATE_SELECTOR = "div.QEk4oc.BgYkof"
READY_SELECTOR = f"{RESULTS_SELECTOR}, {EMPTY_STATE_SELECTOR}"
# Multi-city clicks through one board per extra journey; the budget keeps total
# selections on the round-trip scale (~8) regardless of leg count.
_MULTI_CLICK_BUDGET = 8
EMPTY_STATE_TEXT = "No options matching your search"

SECTION_SELECTOR = 'div[jsname="IWWDBc"], div[jsname="YdtKid"]'
CARD_SELECTOR = "ul.Rk10dc li"
AIRLINE_SELECTOR = "div.sSHqwe.tPgKwe.ogfYpf span"
TIME_SELECTOR = "span.mv1WYe div"
DURATION_SELECTOR = "div.Ak5kof div"
STOPS_SELECTOR = ".BbR8Ec .ogfYpf"
PRICE_SELECTOR = ".YMlIz.FpEdX"
# HTML fallback cards put the layover sentence on the link aria-label and the
# flown slices on the Travel Impact Model URL. Both are owned card text.
_ARIA_LAYOVER = re.compile(
    r"Layover \(\d+ of \d+\) is an? (?:(\d+) hr(?: (\d+) min)?|(\d+) min) "
    r"layover at .+? in ([^.]+)\.",
    re.IGNORECASE,
)
_IMPACT_SLICE = re.compile(r"([A-Z]{3})-([A-Z]{3})-([A-Z0-9]{2,3})-(\d{1,4}[A-Z]?)-(\d{8})")

CONSENT_SELECTORS = [
    'text="Accept all"',
    'text="Reject all"',
    'text="Aceptar todo"',
    'text="Rechazar todo"',
    'button:has-text("Accept")',
]


class NoFlightsFound(Exception):
    """Google rendered a results page with no priced offers."""

    def __init__(self, observed_text: str = "") -> None:
        self.observed_text = observed_text
        message = "Google Flights returned no flights for this route and date."
        if observed_text:
            message = f"{message} Observed: {observed_text}"
        super().__init__(message)


class GoogleFlightsMarkupError(RuntimeError):
    """Neither a results grid nor a recognized empty state was found."""


class GoogleFlightsBlocked(RuntimeError):
    """HTTP sweep hit a consent wall, captcha, or traffic block."""

    def __init__(
        self,
        message: str = "",
        *,
        status: Optional[int] = None,
        diagnostics: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.diagnostics = diagnostics


class SweepTransportError(RuntimeError):
    """The request never produced an HTTP response (connection reset, timeout, TLS)."""

    def __init__(self, message: str = "", *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


class GoogleFlightsRejected(RuntimeError):
    """Shopping RPC rejected the query without an owned cause."""


def build_search_params(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str,
    country: Optional[str] = None,
) -> dict[str, str]:
    params = {
        "tfs": encode_tfs(trip),
        "hl": html_lang,
        "tfu": RESULT_TABS,
        "curr": currency,
    }
    if country:
        params["gl"] = country
    return params


def build_search_url(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str,
    country: Optional[str] = None,
) -> str:
    params = build_search_params(trip, html_lang=html_lang, currency=currency, country=country)
    return f"{SEARCH_URL}?{urlencode(params)}"


def build_itinerary_url(
    booking_token: str,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str,
    country: Optional[str] = None,
) -> str:
    token = booking_token.strip()
    if not token:
        raise ValueError("booking_token must not be blank")
    params = {"hl": html_lang, "curr": currency, "booking_token": token}
    if country:
        params["gl"] = country
    return f"{SEARCH_URL}?" + urlencode(params)


def google_flights_url(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str,
    country: Optional[str] = None,
    booking_token: Optional[str] = None,
) -> Optional[str]:
    """Owned Google Flights link for a trip, or a deeper itinerary when token is owned.

    If the trip cannot be turned into a search URL and there is no booking
    token, the field is omitted. No invented tfs or token.
    """
    token = (booking_token or "").strip()
    if token:
        return build_itinerary_url(token, html_lang=html_lang, currency=currency, country=country)
    try:
        return build_search_url(trip, html_lang=html_lang, currency=currency, country=country)
    except ValueError:
        return None


def _text_or_none(node) -> Optional[str]:
    if node is None:
        return None
    text = node.text(strip=True)
    return text or None


def _aria_layover(label: str) -> tuple[Optional[str], Optional[float]]:
    best_city: Optional[str] = None
    best_hours: Optional[float] = None
    for match in _ARIA_LAYOVER.finditer(label):
        hours_text, mins_text, only_mins, city = match.groups()
        if only_mins:
            hours = int(only_mins) / 60.0
        elif hours_text:
            hours = int(hours_text) + (int(mins_text) / 60.0 if mins_text else 0.0)
        else:
            continue
        if best_hours is None or hours > best_hours:
            best_hours = hours
            best_city = city.strip() or None
    return best_city, best_hours


def _impact_date(text: str) -> Optional[date]:
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _impact_segments(item) -> tuple[RawSegment, ...]:
    node = item.css_first("[data-travelimpactmodelwebsiteurl]")
    if node is None:
        return ()
    url = node.attributes.get("data-travelimpactmodelwebsiteurl") or ""
    segments: list[RawSegment] = []
    for origin, dest, code, number, day in _IMPACT_SLICE.findall(url):
        if not any(char.isalpha() for char in code):
            continue
        on = _impact_date(day)
        if on is None:
            continue
        segments.append(
            RawSegment(
                origin=origin,
                destination=dest,
                flight_number=f"{code}{number}",
                departure_date=on,
                carrier=code,
            )
        )
    return tuple(segments)


def _extract_card(item) -> Optional[RawFlightCard]:
    price = _text_or_none(item.css_first(PRICE_SELECTOR))
    if price is None:
        return None
    times = item.css(TIME_SELECTOR)
    departure = _text_or_none(times[0]) if len(times) > 0 else None
    arrival = _text_or_none(times[1]) if len(times) > 1 else None
    duration = _text_or_none(item.css_first(DURATION_SELECTOR))
    stops = _text_or_none(item.css_first(STOPS_SELECTOR))
    label_node = item.css_first("[aria-label]")
    label = ""
    if label_node is not None:
        label = label_node.attributes.get("aria-label") or ""
    layover_city, layover_hours = _aria_layover(label)
    segments = _impact_segments(item)
    flight_numbers = (
        tuple(segment.flight_number for segment in segments if segment.flight_number) or None
    )
    legs: tuple[RawJourneyLeg, ...] = ()
    if segments or layover_city is not None or layover_hours is not None:
        layovers: tuple[RawLayover, ...] = ()
        if layover_city is not None or layover_hours is not None:
            layovers = (RawLayover(city=layover_city, hours=layover_hours),)
        legs = (
            RawJourneyLeg(
                departure=departure,
                arrival=arrival,
                duration=duration,
                stops=stops,
                segments=segments,
                layovers=layovers,
            ),
        )
    return RawFlightCard(
        airline=_text_or_none(item.css_first(AIRLINE_SELECTOR)),
        departure=departure,
        arrival=arrival,
        duration=duration,
        stops=stops,
        price=price,
        layover_city=layover_city,
        layover_hours=layover_hours,
        flight_numbers=flight_numbers,
        legs=legs,
    )


def _has_empty_state(parser: LexborHTMLParser) -> bool:
    if parser.css_first(EMPTY_STATE_SELECTOR) is not None:
        return True
    body = parser.body
    if body is None:
        return False
    return EMPTY_STATE_TEXT.casefold() in body.text(separator=" ").casefold()


def extract_main_html(html: str) -> str:
    parser = LexborHTMLParser(html)
    main = parser.css_first('[role="main"]')
    if main is None:
        return html
    return main.html or html


def looks_blocked(html: str, final_url: str = "") -> bool:
    lowered_url = final_url.casefold()
    if any(marker in lowered_url for marker in BLOCK_URL_MARKERS):
        return True
    lowered = html.casefold()
    return any(marker in lowered for marker in BLOCK_BODY_MARKERS) or (
        rpc_error_status(html) is not None
    )


def _is_consent_interstitial(url: str) -> bool:
    lowered = url.casefold()
    return "consent.google" in lowered and "/sorry/" not in lowered


def _consent_reject_form(html: str, final_url: str) -> Optional[tuple[str, dict[str, str]]]:
    """Reject-all (or Accept-all) fields from a consent.google interstitial."""
    if not _is_consent_interstitial(final_url):
        return None
    reject: Optional[tuple[str, dict[str, str]]] = None
    accept: Optional[tuple[str, dict[str, str]]] = None
    for form in LexborHTMLParser(html).css("form"):
        action = urljoin(final_url, form.attributes.get("action") or "")
        if not action:
            continue
        data: dict[str, str] = {}
        for inp in form.css("input"):
            name = inp.attributes.get("name")
            if name:
                data[name] = inp.attributes.get("value") or ""
        if not data:
            continue
        pair = (action, data)
        if data.get("set_eom") == "true" and data.get("set_sc") != "true":
            reject = pair
        elif data.get("set_sc") == "true":
            accept = pair
    return reject or accept


@dataclass(frozen=True)
class SweepHttpResponse:
    status: int
    text: str
    url: str = ""
    rate_limit: Optional[str] = None
    deadline: bool = False
    request_sent: bool = True
    attempts: int = 1
    stopped: bool = False
    cooldown_basis: Optional[str] = None


# Not an HTTP status: marks a multiplexed job whose request raised before any response.
SWEEP_TRANSPORT_STATUS = 599


# ponytail: Google answers a throttled IP with a data-less wrb.fr envelope, status 13.
RPC_THROTTLE_STATUS = 13


def _retry_after_seconds(response: Any) -> Optional[float]:
    headers = getattr(response, "headers", None) or {}
    try:
        return float(headers.get("retry-after") or headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None


class _CooldownClient:
    """Answers every request with a local 429 while a recorded cooldown runs. No network."""

    def __init__(self, state: Mapping[str, float]) -> None:
        self._advice = rate_limit_advice(state, sent=False)
        self._basis = str(state.get("basis", "unknown"))

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        return SweepHttpResponse(
            0,
            "",
            url,
            rate_limit=self._advice,
            request_sent=False,
            attempts=0,
            stopped=True,
            cooldown_basis=self._basis,
        )

    def post(
        self, url: str, *, data: str, headers: Mapping[str, str], timeout: float
    ) -> SweepHttpResponse:
        return SweepHttpResponse(
            0,
            "",
            url,
            rate_limit=self._advice,
            request_sent=False,
            attempts=0,
            stopped=True,
            cooldown_basis=self._basis,
        )

    def close(self) -> None:
        return None


COOLDOWN_UNCHECKED: Any = object()


def cooldown_client(snapshot: Any) -> tuple[Any, Optional[_CooldownClient]]:
    """Read the cooldown once per search; live responses have their own stop policy."""
    state = rate_limit_status() if snapshot is COOLDOWN_UNCHECKED else snapshot
    return state, (_CooldownClient(state) if state is not None else None)


class SweepHttpClient(Protocol):
    def get(self, url: str, *, timeout: float) -> SweepHttpResponse: ...

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> SweepHttpResponse: ...

    def close(self) -> None: ...


def _curl_requests():
    # Sweep TLS only: card parse and unittest import must not pay for curl_cffi.
    from curl_cffi import requests as curl_requests

    return curl_requests


def _as_sweep_response(response: Any) -> SweepHttpResponse:
    return SweepHttpResponse(
        status=int(response.status_code),
        text=response.text,
        url=str(response.url),
    )


def _raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    """Carry the caller's cancellation across the sweep client's event-loop thread."""
    if cancel_event is not None and cancel_event.is_set():
        raise SearchCancelled()


@dataclass(frozen=True)
class SweepPost:
    url: str
    data: str
    headers: Mapping[str, str]


def _normalize_proxy(proxy: Optional[str]) -> Optional[str]:
    if proxy is None:
        return None
    text = proxy.strip()
    return text or None


class ChromeSweepClient:
    """Process-wide curl_cffi AsyncSession: Chrome TLS, HTTP/2 multiplex, keep-alive."""

    def __init__(self, *, proxy: Optional[str] = None) -> None:
        import asyncio

        from curl_cffi import CurlHttpVersion

        config = get_sweep_config()
        self._streams = config.concurrency
        self.sweep_mode = config.mode
        curl_requests = _curl_requests()
        self._asyncio = asyncio
        self._proxied = _normalize_proxy(proxy) is not None
        self._loop = asyncio.new_event_loop()
        self._session: Any = None
        self._error: Optional[BaseException] = None
        self._consent_ok = False
        self._consent_lock: Any = None
        ready = threading.Event()
        session_kw: dict[str, Any] = {
            "impersonate": "chrome",
            "max_clients": self._streams,
            "timeout": HTTP_TIMEOUT_SECONDS,
            "allow_redirects": True,
            "loop": self._loop,
            "http_version": CurlHttpVersion.V2TLS,
        }
        proxy = _normalize_proxy(proxy)
        if proxy is not None:
            session_kw["proxy"] = proxy

        def _run() -> None:
            asyncio.set_event_loop(self._loop)
            try:
                self._session = curl_requests.AsyncSession(**session_kw)
                self._consent_lock = asyncio.Lock()
            except BaseException as exc:
                self._error = exc
                ready.set()
                return
            ready.set()
            self._loop.run_forever()
            closer = getattr(self._session, "close", None)
            if closer is not None:
                try:
                    self._loop.run_until_complete(closer())
                except Exception:
                    pass
            self._loop.close()

        self._thread = threading.Thread(target=_run, name="viajante-sweep", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=5):
            raise RuntimeError("sweep HTTP/2 session failed to start")
        if self._error is not None:
            raise self._error

    def _submit(self, coro: Any, *, timeout: float) -> Any:
        try:
            checkpoint()
        except BaseException:
            coro.close()
            raise
        future = self._asyncio.run_coroutine_threadsafe(coro, self._loop)
        return wait_for_future(future, max(timeout + 5.0, 10.0))

    async def _dismiss_consent(
        self,
        response: Any,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        async with self._consent_lock:
            _raise_if_cancelled(cancel_event)
            if self._consent_ok:
                return True
            parsed = _consent_reject_form(response.text, str(response.url))
            if parsed is None:
                return False
            action, fields = parsed
            save = await self._session.post(
                action, data=fields, timeout=timeout, allow_redirects=True
            )
            _raise_if_cancelled(cancel_event)
            # ponytail: SOCS is process-lifetime; 429 reset builds a new client.
            self._consent_ok = not _is_consent_interstitial(str(save.url))
            return self._consent_ok

    async def _exchange(
        self,
        send: Any,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> SweepHttpResponse:
        _raise_if_cancelled(cancel_event)
        response = await send()
        _raise_if_cancelled(cancel_event)
        if _is_consent_interstitial(str(response.url)) and await self._dismiss_consent(
            response, timeout, cancel_event
        ):
            _raise_if_cancelled(cancel_event)
            response = await send()
            _raise_if_cancelled(cancel_event)
        out = _as_sweep_response(response)
        # ponytail: a proxy is another egress IP, so its limit does not pause direct searches.
        if self._proxied:
            return out
        _raise_if_cancelled(cancel_event)
        if out.status == 429:
            state = note_rate_limited(_retry_after_seconds(response))
            out = replace(
                out,
                rate_limit=rate_limit_advice(state),
                cooldown_basis=state.get("basis", "unknown"),
            )
        elif out.status == 200 and raw_rpc_error_status(out.text) == RPC_THROTTLE_STATUS:
            state = note_rate_limited(basis="heuristic_rpc_13", cause="rpc_13")
            out = replace(
                out,
                rate_limit=rate_limit_advice(state, reason=f"RPC status {RPC_THROTTLE_STATUS}"),
                cooldown_basis=state.get("basis", "unknown"),
            )
        return out

    async def _aget(
        self,
        url: str,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> SweepHttpResponse:
        return await self._exchange(
            lambda: self._session.get(url, timeout=timeout, allow_redirects=True),
            timeout,
            cancel_event,
        )

    async def _apost(
        self,
        url: str,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> SweepHttpResponse:
        return await self._exchange(
            lambda: self._session.post(
                url,
                data=data,
                headers=dict(headers),
                timeout=timeout,
                allow_redirects=True,
            ),
            timeout,
            cancel_event,
        )

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        control = current_control()
        cancel_event = control.cancel if control is not None else None
        return self._submit(self._aget(url, timeout, cancel_event), timeout=timeout)

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> SweepHttpResponse:
        control = current_control()
        cancel_event = control.cancel if control is not None else None
        return self._submit(self._apost(url, data, headers, timeout, cancel_event), timeout=timeout)

    def post_many(
        self,
        jobs: Sequence[SweepPost],
        *,
        timeout: float,
    ) -> list[SweepHttpResponse]:
        if not jobs:
            return []
        if len(jobs) == 1:
            job = jobs[0]
            return [self.post(job.url, data=job.data, headers=job.headers, timeout=timeout)]
        out: list[SweepHttpResponse | None] = [None] * len(jobs)
        sent = [False] * len(jobs)
        control = current_control()
        cancel_event = control.cancel if control is not None else None
        try:
            return list(
                self._submit(
                    self._apost_many(jobs, timeout, out, cancel_event, sent=sent),
                    timeout=timeout,
                )
            )
        except SearchDeadline:
            # Responses that already arrived are real; only the rest were not loaded.
            return [
                item
                if item is not None
                else SweepHttpResponse(
                    0,
                    "",
                    job.url,
                    deadline=True,
                    request_sent=sent[index],
                    attempts=1 if sent[index] else 0,
                )
                for index, (item, job) in enumerate(zip(out, jobs, strict=True))
            ]

    def get_many(self, urls: Sequence[str], *, timeout: float) -> list[SweepHttpResponse]:
        if not urls:
            return []
        jobs = [SweepPost(url, "", {}) for url in urls]
        out: list[SweepHttpResponse | None] = [None] * len(jobs)
        sent = [False] * len(jobs)
        control = current_control()
        cancel_event = control.cancel if control is not None else None
        try:
            return list(
                self._submit(
                    self._apost_many(jobs, timeout, out, cancel_event, get=True, sent=sent),
                    timeout=timeout,
                )
            )
        except SearchDeadline:
            return [
                item
                if item is not None
                else SweepHttpResponse(
                    0,
                    "",
                    job.url,
                    deadline=True,
                    request_sent=sent[index],
                    attempts=1 if sent[index] else 0,
                )
                for index, (item, job) in enumerate(zip(out, jobs, strict=True))
            ]

    async def _apost_many(
        self,
        jobs: Sequence[SweepPost],
        timeout: float,
        out: list[SweepHttpResponse | None],
        cancel_event: Optional[threading.Event] = None,
        *,
        get: bool = False,
        sent: Optional[list[bool]] = None,
    ) -> list[SweepHttpResponse]:
        # Keep HTTP/2 multiplex on the happy path. After HTTP 429 or a transport
        # failure, stop feeding this TLS session. Unsent blocking jobs have no HTTP
        # status; only transport failures may be continued on a fresh session.
        semaphore = self._asyncio.Semaphore(getattr(self, "_streams", _SWEEP_STREAMS))
        stop = self._asyncio.Event()
        stop_status = 0
        dispatched = sent if sent is not None else [False] * len(jobs)

        async def _one(index: int, job: SweepPost) -> None:
            nonlocal stop_status
            if stop.is_set():
                return
            async with semaphore:
                if stop.is_set():
                    return
                dispatched[index] = True
                try:
                    response = (
                        await self._aget(job.url, timeout, cancel_event)
                        if get
                        else await self._apost(
                            job.url, job.data, job.headers, timeout, cancel_event
                        )
                    )
                except SearchDeadline:
                    raise
                except Exception as exc:
                    if not stop.is_set():
                        stop_status = SWEEP_TRANSPORT_STATUS
                    stop.set()
                    out[index] = SweepHttpResponse(
                        SWEEP_TRANSPORT_STATUS, f"{type(exc).__name__}: {exc}", job.url
                    )
                    return
                out[index] = response
                if (
                    response.status == 429
                    or raw_rpc_error_status(response.text) == RPC_THROTTLE_STATUS
                ):
                    stop.set()

        await self._asyncio.gather(*[_one(index, job) for index, job in enumerate(jobs)])
        return [
            item
            if item is not None
            else SweepHttpResponse(
                stop_status,
                "not sent after an earlier transport failure"
                if stop_status == SWEEP_TRANSPORT_STATUS
                else "Not sent. Remaining batch stopped after a provider block.",
                job.url,
                request_sent=dispatched[index],
                attempts=1 if dispatched[index] else 0,
                stopped=True,
            )
            for index, (item, job) in enumerate(zip(out, jobs, strict=True))
        ]

    def close(self) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(loop.stop)
        thread = getattr(self, "_thread", None)
        if thread is not None:
            thread.join(timeout=2)


_SHARED_CLIENT_LOCK = threading.Lock()
_SHARED_CLIENT: Optional[ChromeSweepClient] = None
_SHARED_CLIENT_PROXY: Optional[str] = None
_SHARED_CLIENT_MODE: Optional[str] = None


def shared_chrome_sweep_client(*, proxy: Optional[str] = None) -> ChromeSweepClient:
    global _SHARED_CLIENT, _SHARED_CLIENT_PROXY, _SHARED_CLIENT_MODE
    wanted = _normalize_proxy(proxy)
    mode = get_sweep_config().mode
    with _SHARED_CLIENT_LOCK:
        if (
            _SHARED_CLIENT is not None
            and _SHARED_CLIENT_PROXY == wanted
            and _SHARED_CLIENT_MODE == mode
        ):
            return _SHARED_CLIENT
        old = _SHARED_CLIENT
        _SHARED_CLIENT = ChromeSweepClient(proxy=wanted)
        _SHARED_CLIENT_PROXY = wanted
        _SHARED_CLIENT_MODE = mode
    if old is not None:
        old.close()
    return _SHARED_CLIENT


def reset_shared_chrome_sweep_client() -> None:
    global _SHARED_CLIENT, _SHARED_CLIENT_PROXY, _SHARED_CLIENT_MODE
    with _SHARED_CLIENT_LOCK:
        client = _SHARED_CLIENT
        _SHARED_CLIENT = None
        _SHARED_CLIENT_PROXY = None
        _SHARED_CLIENT_MODE = None
    if client is not None:
        client.close()


def dispatch_posts(
    client: SweepHttpClient,
    jobs: Sequence[SweepPost],
    *,
    timeout: float,
) -> list[SweepHttpResponse]:
    poster = getattr(client, "post_many", None)
    if callable(poster) and len(jobs) > 1:
        return list(poster(jobs, timeout=timeout))
    return [
        client.post(job.url, data=job.data, headers=job.headers, timeout=timeout) for job in jobs
    ]


def _raise_if_blocked(
    status: int, body: str, final_url: str, fallback_url: str, advice: Optional[str] = None
) -> None:
    if status == SWEEP_TRANSPORT_STATUS:
        raise SweepTransportError(
            f"Google Flights request failed before any response: {body}",
            timeout="timeout" in body.partition(":")[0].casefold(),
        )
    if status in {403, 429, 503}:
        message = f"Google Flights HTTP {status} from {fallback_url}"
        if status == 429 and advice:
            message = advice if advice.startswith(NOT_SENT) else f"{message}. {advice}"
        raise GoogleFlightsBlocked(message, status=status)
    if status >= 400:
        raise GoogleFlightsBlocked(
            f"Google Flights HTTP {status} from {final_url or fallback_url}",
            status=status,
        )
    rpc_status = rpc_error_status(body)
    if rpc_status is not None:
        if advice and rpc_status == RPC_THROTTLE_STATUS:
            # status=429 so the caller treats it as a rate limit and does not replay or fall back.
            raise GoogleFlightsBlocked(advice, status=429)
        # Seen alongside google.com/sorry for the same IP while shopping still answers.
        raise GoogleFlightsBlocked(
            f"Google Flights answered with RPC error status {rpc_status} and no data "
            f"from {urlsplit(fallback_url).netloc}{urlsplit(fallback_url).path}. "
            "The cause is unknown; this status alone does not prove an IP block."
        )
    if looks_blocked(body, final_url):
        raise GoogleFlightsBlocked(
            f"Google Flights blocked the sweep at {final_url or fallback_url}"
        )


def response_diagnostics(response: SweepHttpResponse, url: str) -> dict[str, object]:
    endpoint = urlsplit(url)
    return {
        "http_status": response.status
        if response.request_sent and response.status != SWEEP_TRANSPORT_STATUS
        else None,
        "rpc_status": raw_rpc_error_status(response.text) if response.request_sent else None,
        "request_sent": response.request_sent,
        "attempts": response.attempts,
        "endpoint": endpoint.netloc + endpoint.path,
        "cooldown_basis": response.cooldown_basis,
    }


def raise_for_sweep_response(response: SweepHttpResponse, url: str) -> None:
    if response.deadline:
        raise SearchDeadline()
    diagnostics = response_diagnostics(response, url)
    if response.stopped and not response.request_sent and response.status != SWEEP_TRANSPORT_STATUS:
        raise GoogleFlightsBlocked(
            response.rate_limit or response.text,
            status=429 if response.rate_limit else None,
            diagnostics=diagnostics,
        )
    raw_status = raw_rpc_error_status(response.text)
    if raw_status == RPC_THROTTLE_STATUS:
        message = response.rate_limit or (
            "Google Flights returned RPC status 13. The cause is unknown; "
            "availability is not disproved."
        )
        raise GoogleFlightsBlocked(
            message, status=429 if response.rate_limit else None, diagnostics=diagnostics
        )
    try:
        _raise_if_blocked(response.status, response.text, response.url, url, response.rate_limit)
    except GoogleFlightsBlocked as exc:
        exc.diagnostics = diagnostics
        raise


def _is_retriable_sweep_failure(exc: BaseException) -> bool:
    if isinstance(exc, (NoFlightsFound, GoogleFlightsMarkupError)):
        return True
    if isinstance(exc, GoogleFlightsBlocked):
        status = exc.status
        # Short unknown shells: once-retry like former markup drift. Consent
        # walls are also Blocked with status None and must not retry.
        if status is None:
            return _SHORT_SHELL_MARK in str(exc)
        return isinstance(status, int) and status >= 500
    return False


def parse_http_flight_cards(html: str) -> tuple[RawFlightCard, ...]:
    return parse_flight_cards(extract_main_html(html))


def _multi_identity(card: RawFlightCard) -> tuple[object, ...]:
    """Owned segment routes/numbers/dates and the board's departure clock."""
    departure = normalize_clock(card.departure)
    if departure is None or len(card.legs) != 1 or not card.legs[0].segments:
        raise GoogleFlightsMarkupError("The selected journey has an incomplete identity.")
    segments = card.legs[0].segments
    identity = tuple(
        (s.origin, s.destination, s.departure_date, s.carrier, s.flight_number) for s in segments
    )
    if any(value is None or value == "" for row in identity for value in row):
        raise GoogleFlightsMarkupError("The selected journey has an incomplete identity.")
    return departure, identity


def _multi_row_index(board: Sequence[tuple[int, RawFlightCard]], wanted: RawFlightCard) -> int:
    """DOM index of the unique board row matching the complete owned identity."""
    identity = _multi_identity(wanted)
    matches = []
    for index, card in board:
        try:
            same = _multi_identity(card) == identity
        except GoogleFlightsMarkupError:
            continue
        if same:
            matches.append(index)
    if len(matches) > 1:
        raise GoogleFlightsMarkupError("The selected journey is ambiguous on the reloaded board.")
    if not matches:
        raise GoogleFlightsMarkupError(
            "The selected journey no longer appears on the reloaded board."
        )
    return matches[0]


def parse_flight_cards(html: str) -> tuple[RawFlightCard, ...]:
    parser = LexborHTMLParser(html)
    cards: list[RawFlightCard] = []
    for section in parser.css(SECTION_SELECTOR):
        for item in section.css(CARD_SELECTOR):
            card = _extract_card(item)
            if card is not None:
                cards.append(card)
    if cards:
        return tuple(cards)
    # Empty result lists and grounded empty-state copy both mean no flights.
    # Unknown shells without either signal are markup drift, except a tiny
    # shell which is a block, not a parse of a results page.
    empty_state = _has_empty_state(parser)
    if empty_state or parser.css_first("ul.Rk10dc") is not None:
        raise NoFlightsFound(EMPTY_STATE_TEXT if empty_state else "")
    n = len(html)
    if n < _SHORT_SHELL_CHARS:
        raise GoogleFlightsBlocked(
            f"Google Flights returned a {_SHORT_SHELL_MARK} ({n} chars of main HTML)"
        )
    raise GoogleFlightsMarkupError(f"no results grid and no empty state in {n} chars of main HTML")


class GoogleFlightsHttpSource:
    """Sweep transport shared by the public-page source: client, cooldown, one replay."""

    def __init__(
        self,
        *,
        html_lang: str = SCRAPE_LANGUAGE,
        currency: str,
        country: Optional[str] = None,
        client: Optional[SweepHttpClient] = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        sleep: Optional[Callable[[float], None]] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self._html_lang = html_lang
        self._currency = currency
        self._country = country
        self._injected_client = client
        self._timeout = timeout
        self._sleep = time.sleep if sleep is None else sleep
        self._proxy = _normalize_proxy(proxy)
        self._cooldown = COOLDOWN_UNCHECKED
        self.config = SimpleNamespace(html_lang=html_lang, currency=currency, country=country)

    def reset(self) -> None:
        if self._injected_client is None:
            reset_shared_chrome_sweep_client()

    def _plan_replay(
        self, client: SweepHttpClient, outcomes: Sequence[object]
    ) -> tuple[SweepHttpClient, list[int]]:
        """One replay of retriable failures; a provider block prevents every replay.

        Transport failures are replayed only here, as a batch, never again per query.
        """
        failures = [(i, o) for i, o in enumerate(outcomes) if isinstance(o, BaseException)]
        if any(
            isinstance(exc, GoogleFlightsBlocked)
            and (exc.status in {403, 429} or (exc.diagnostics or {}).get("rpc_status") == 13)
            for _, exc in failures
        ):
            return client, []
        rate = [i for i, exc in failures if isinstance(exc, SweepTransportError)]
        if rate:
            retry = [i for i, exc in failures if i not in rate and _is_retriable_sweep_failure(exc)]
            return self._client_after_rate_limit(), sorted(rate + retry)
        retry = [i for i, exc in failures if _is_retriable_sweep_failure(exc)]
        if not retry:
            return client, []
        if SWEEP_RETRY_BACKOFF_SECONDS > 0:
            self._sleep(SWEEP_RETRY_BACKOFF_SECONDS)
        return client, retry

    def _client_after_rate_limit(self) -> SweepHttpClient:
        self.reset()
        if SWEEP_RETRY_BACKOFF_SECONDS > 0:
            self._sleep(SWEEP_RETRY_BACKOFF_SECONDS)
        return self._ensure_client()

    def close(self) -> None:
        # Keep the process TLS session warm for the next MCP/CLI search.
        return None

    def _ensure_client(self) -> SweepHttpClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._proxy is None:
            self._cooldown, paused = cooldown_client(self._cooldown)
            if paused is not None:
                return paused
        return shared_chrome_sweep_client(proxy=self._proxy)


_DETAIL_UNPROVEN = (
    "bags",
    "carry_on",
    "airlines",
    "exclude_airlines",
    "alliances",
    "exclude_alliances",
)


class GoogleFlightsSource:
    automatic_typical = False

    def __init__(
        self,
        state_dir: Path,
        session: Optional[ChromiumSession] = None,
        config: Optional[BrowserSessionConfig] = None,
        *,
        currency: str,
        country: Optional[str] = None,
    ) -> None:
        self._config = config or BrowserSessionConfig(
            state_filename=STATE_FILENAME,
            locale=FETCH_LOCALE,
            html_lang=SCRAPE_LANGUAGE,
            currency=currency,
            country=country,
        )
        self._session = session or ChromiumSession(state_dir, self._config)
        self._started = False
        self.partial_error: Optional[BaseException] = None
        self.scope_bound = False
        self._query_meta: dict[Trip, tuple[BaseException | None, bool]] = {}

    @property
    def config(self) -> BrowserSessionConfig:
        return self._config

    def fetch(self, trip: Trip) -> tuple[RawFlightCard, ...]:
        from viajante.google_flights_public import (
            GoogleFlightsUnsupported,
            PublicGoogleFlightsHttpSource,
        )

        # Detail reads no page echo, so a bag or carrier filter it sends stays unproven.
        unproven = [key for key in _DETAIL_UNPROVEN if getattr(trip, key, None) is not None]
        if unproven:
            raise GoogleFlightsUnsupported(
                "Not sent. Browser detail cannot verify these requested capabilities: "
                + ", ".join(unproven)
                + ". The public-page sweep verifies carry-on and airline or alliance includes."
            )
        # The public page bootstraps no multi-city results; the browser UI renders them.
        PublicGoogleFlightsHttpSource._validate_capabilities(self, trip, allow_multi_city=True)
        if isinstance(trip, MultiCity):
            return self._fetch_multi(trip)
        return parse_flight_cards(
            self._fetch_html(
                build_search_url(
                    trip,
                    html_lang=self._config.html_lang,
                    currency=self._config.currency,
                    country=self._config.country,
                )
            )
        )

    def _fetch_multi(self, trip: MultiCity) -> tuple[RawFlightCard, ...]:
        """Complete multi-city packages by clicking through the real board UI.

        The public results page returns a shell for multi-city searches; the
        browser renders each journey's board after the previous selection. Each
        emitted card is a complete provider package: every journey leg comes
        from an owned board row echoed against the request, priced by the
        provider's own package total on the last board (never a sum of one-way
        fares). Coverage is bounded to a few first-journey candidates; middle
        journeys take their cheapest displayed continuation. Completed packages
        survive a failed follow-up.
        """
        if not self._started:
            state = rate_limit_status()
            if state is not None:
                raise GoogleFlightsBlocked(rate_limit_advice(state, sent=False), status=429)
            self._started = True
        self.partial_error = None
        self.scope_bound = True  # Bounded first-journey coverage, like the RT cap.
        url = build_search_url(
            trip,
            html_lang=self._config.html_lang,
            currency=self._config.currency,
            country=self._config.country,
        )
        page = self._session.new_page()
        complete: list[RawFlightCard] = []
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if "consent.google" in page.url:
                self._dismiss_consent(page)
            if looks_blocked("", page.url):
                raise GoogleFlightsBlocked(f"Google Flights blocked the browser at {page.url}")
            leaders = sorted(
                self._multi_board(page, trip.legs[0]),
                key=lambda ic: parse_price(ic[1].price) or float("inf"),
            )
            budget = max(2, _MULTI_CLICK_BUDGET // (len(trip.legs) - 1))
            for index, (_first_index, leader) in enumerate(leaders[:budget]):
                checkpoint()
                try:
                    if index:
                        self._multi_reset(page, url)
                    # Re-locate the candidate on the (re)loaded board by owned
                    # identity; row order is not guaranteed across reloads.
                    board = self._multi_board(page, trip.legs[0])
                    dom_index = _multi_row_index(board, leader)
                    selected_leader = next(card for row, card in board if row == dom_index)
                    path = [selected_leader.legs[0]]
                    self._multi_click(page, dom_index)
                    for leg in trip.legs[1:-1]:
                        checkpoint()
                        continuations = sorted(
                            self._multi_board(page, leg),
                            key=lambda ic: parse_price(ic[1].price) or float("inf"),
                        )
                        next_index, next_card = continuations[0]
                        path.append(next_card.legs[0])
                        self._multi_click(page, next_index)
                    finals = self._multi_board(page, trip.legs[-1])
                except SearchDeadline:
                    if not complete:
                        raise
                    self.partial_error = SearchDeadline()
                    break
                except Exception as exc:
                    self.partial_error = exc
                    if isinstance(exc, GoogleFlightsBlocked):
                        break
                else:
                    for _last_index, last in finals:
                        selected = tuple(path) + (last.legs[0],)
                        complete.append(
                            replace(
                                selected_leader,
                                price=last.price,
                                booking_token=last.booking_token,
                                legs=selected,
                                flight_numbers=tuple(
                                    segment.flight_number
                                    for journey in selected
                                    for segment in journey.segments
                                    if segment.flight_number
                                ),
                            )
                        )
            if not complete:
                if self.partial_error is not None and not isinstance(
                    self.partial_error, NoFlightsFound
                ):
                    raise self.partial_error
                raise GoogleFlightsMarkupError(
                    "No complete multi-city package was proved by the browser pages."
                )
        finally:
            with contextlib.suppress(Exception):
                page.close()
        self._query_meta[trip] = (self.partial_error, self.scope_bound)
        return tuple(complete)

    def _multi_board(self, page: object, leg: FlightLeg) -> list[tuple[int, RawFlightCard]]:
        """(DOM row index, card) pairs echoing the requested journey leg.

        The index counts every ``ul.Rk10dc li`` in document order so the row can
        be clicked back with ``locator(CARD_SELECTOR).nth(index)``.
        """
        from viajante.google_flights_public import PublicGoogleFlightsHttpSource

        page.locator(READY_SELECTOR).first.wait_for(timeout=PAGE_TIMEOUT_MS)
        checkpoint()
        html = page.evaluate("() => document.querySelector('[role=\"main\"]')?.innerHTML || ''")
        matches_leg = PublicGoogleFlightsHttpSource._matches_leg
        matched: list[tuple[int, RawFlightCard]] = []
        parser = LexborHTMLParser(html)
        for position, item in enumerate(parser.css(CARD_SELECTOR)):
            card = _extract_card(item)
            if card is not None and matches_leg(card, leg):
                matched.append((position, card))
        if not matched:
            raise GoogleFlightsMarkupError(
                "Browser board rows did not prove the requested journey."
            )
        return matched

    def metadata_for(self, trip: Trip):
        return self._query_meta.get(trip, (None, False))

    @staticmethod
    def _multi_sig(page: object) -> str:
        return page.evaluate(
            "() => {"
            "const m = document.querySelector('[role=\"main\"]');"
            "if (!m) return '';"
            "const h = m.querySelector('h3');"
            "const r = m.querySelector('ul.Rk10dc li');"
            "return (h ? h.textContent : '') + '|' + (r ? r.textContent : '');}"
        )

    def _multi_click(self, page: object, dom_index: int) -> None:
        """Click one board row and wait for the board to advance."""
        prev_sig = self._multi_sig(page)
        page.locator(CARD_SELECTOR).nth(dom_index).click(timeout=CONSENT_CLICK_TIMEOUT_MS)
        try:
            page.wait_for_function(
                "(prev) => {"
                "const m = document.querySelector('[role=\"main\"]');"
                "if (!m) return false;"
                "const h = m.querySelector('h3');"
                "const r = m.querySelector('ul.Rk10dc li');"
                "const sig = (h ? h.textContent : '') + '|' + (r ? r.textContent : '');"
                "return r !== null && sig !== prev;}",
                arg=prev_sig,
                timeout=PAGE_TIMEOUT_MS,
            )
        except SearchDeadline:
            raise
        except Exception as exc:
            raise GoogleFlightsMarkupError(
                "The clicked board did not advance to the next journey."
            ) from exc

    def _multi_reset(self, page: object, url: str) -> None:
        """Return to the first journey's board for the next candidate."""
        checkpoint()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if looks_blocked("", page.url):
                raise GoogleFlightsBlocked(f"Google Flights blocked the browser at {page.url}")
            page.locator(READY_SELECTOR).first.wait_for(timeout=PAGE_TIMEOUT_MS)
        except (SearchDeadline, GoogleFlightsBlocked):
            raise
        except Exception as exc:
            raise GoogleFlightsMarkupError("Could not reload the first journey board.") from exc

    def reset(self) -> None:
        self._session.reset()

    def close(self) -> None:
        self._session.close()

    def _fetch_html(self, url: str) -> str:
        if not self._started:
            state = rate_limit_status()
            if state is not None:
                raise GoogleFlightsBlocked(rate_limit_advice(state, sent=False), status=429)
            self._started = True
        page = self._session.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if "consent.google" in page.url:
                self._dismiss_consent(page)
            if looks_blocked("", page.url):
                raise GoogleFlightsBlocked(f"Google Flights blocked the browser at {page.url}")
            page.locator(READY_SELECTOR).first.wait_for(timeout=PAGE_TIMEOUT_MS)
            return page.evaluate("() => document.querySelector('[role=\"main\"]')?.innerHTML || ''")
        finally:
            with contextlib.suppress(Exception):
                page.close()

    @staticmethod
    def _dismiss_consent(page) -> None:
        for selector in CONSENT_SELECTORS:
            try:
                button = page.locator(selector).first
                if button.count() > 0:
                    button.click(timeout=CONSENT_CLICK_TIMEOUT_MS)
                    break
            except Exception:
                continue
        page.wait_for_timeout(CONSENT_SETTLE_MS)
