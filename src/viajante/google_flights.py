"""Google Flights URL building, consent, card parsing, and page source."""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlencode, urljoin

from selectolax.lexbor import LexborHTMLParser

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.google_flights_rpc import (
    SHOPPING_POST_HEADERS,
    CompactCalendarDay,
    CompactExplorePlace,
    CompactParseMiss,
    EmptyShoppingResults,
    RawFlightCard,
    ShoppingRejected,
    build_calendar_request,
    build_explore_request,
    build_shopping_request,
    parse_calendar_body,
    parse_explore_body,
    parse_shopping_body,
)
from viajante.models import FETCH_LANGUAGE, FETCH_LOCALE, FlightCabin, Trip
from viajante.tfs import encode_tfs

SEARCH_URL = "https://www.google.com/travel/flights"
STATE_FILENAME = "pw_state_google.json"

SCRAPE_LANGUAGE = FETCH_LANGUAGE
SCRAPE_CURRENCY = "EUR"
# Owned `tfu` blob that selects result tabs; not produced by encode_tfs.
RESULT_TABS = "EgQIABABIgA"

PAGE_TIMEOUT_MS = 60_000
CONSENT_CLICK_TIMEOUT_MS = 5_000
CONSENT_SETTLE_MS = 1_500
HTTP_TIMEOUT_SECONDS = 30
# One replay on empty/drift/5xx. Happy path does not sleep. Not an anti-bot pause.
SWEEP_RETRY_LIMIT = 1
SWEEP_RETRY_BACKOFF_SECONDS = 0.05
# Browser-like HTTP/2 stream cap. Dates fallback is at most 31 days.
_SWEEP_STREAMS = 8
# urllib / tests only. Production sweep uses impersonate="chrome", not this string.
HTTP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HTTP_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
# urllib fallback / tests only. Production sweep uses a Chrome TLS session.
URLLIB_HEADERS = {
    **HTTP_HEADERS,
    "User-Agent": HTTP_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
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
EMPTY_STATE_TEXT = "No options matching your search"

SECTION_SELECTOR = 'div[jsname="IWWDBc"], div[jsname="YdtKid"]'
CARD_SELECTOR = "ul.Rk10dc li"
AIRLINE_SELECTOR = "div.sSHqwe.tPgKwe.ogfYpf span"
TIME_SELECTOR = "span.mv1WYe div"
DURATION_SELECTOR = "div.Ak5kof div"
STOPS_SELECTOR = ".BbR8Ec .ogfYpf"
PRICE_SELECTOR = ".YMlIz.FpEdX"

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

    def __init__(self, message: str = "", *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class GoogleFlightsRejected(RuntimeError):
    """Shopping RPC rejected the query (unknown airport or invalid request)."""


def build_search_params(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str = SCRAPE_CURRENCY,
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
    currency: str = SCRAPE_CURRENCY,
    country: Optional[str] = None,
) -> str:
    params = build_search_params(trip, html_lang=html_lang, currency=currency, country=country)
    return f"{SEARCH_URL}?{urlencode(params)}"


def build_itinerary_url(
    booking_token: str,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str = SCRAPE_CURRENCY,
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
    currency: str = SCRAPE_CURRENCY,
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


def _extract_card(item) -> Optional[RawFlightCard]:
    price = _text_or_none(item.css_first(PRICE_SELECTOR))
    if price is None:
        return None
    times = item.css(TIME_SELECTOR)
    departure = _text_or_none(times[0]) if len(times) > 0 else None
    arrival = _text_or_none(times[1]) if len(times) > 1 else None
    return RawFlightCard(
        airline=_text_or_none(item.css_first(AIRLINE_SELECTOR)),
        departure=departure,
        arrival=arrival,
        duration=_text_or_none(item.css_first(DURATION_SELECTOR)),
        stops=_text_or_none(item.css_first(STOPS_SELECTOR)),
        price=price,
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
    return any(marker in lowered for marker in BLOCK_BODY_MARKERS)


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


def _gzip():
    # Live/deflate bodies only: unittest HTML fixtures are already decoded.
    import gzip

    return gzip


def _stdlib_urllib():
    # urllib path for fetch_search_html without a sweep client. Tests inject opener=
    # or client=. Production sweep uses curl_cffi impersonate="chrome".
    import ssl
    import urllib.error
    import urllib.request

    return urllib.request, urllib.error, ssl


def _decode_http_body(raw: bytes, content_encoding: str) -> str:
    encoding = content_encoding.casefold()
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        raw = _gzip().decompress(raw)
    elif "deflate" in encoding:
        raw = _gzip().decompress(raw, wbits=-15)
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class SweepHttpResponse:
    status: int
    text: str
    url: str = ""


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

        curl_requests = _curl_requests()
        self._asyncio = asyncio
        self._loop = asyncio.new_event_loop()
        self._session: Any = None
        self._error: Optional[BaseException] = None
        self._consent_ok = False
        self._consent_lock: Any = None
        ready = threading.Event()
        session_kw: dict[str, Any] = {
            "impersonate": "chrome",
            "max_clients": _SWEEP_STREAMS,
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
        future = self._asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=max(timeout + 5.0, 10.0))

    async def _dismiss_consent(self, response: Any, timeout: float) -> bool:
        async with self._consent_lock:
            if self._consent_ok:
                return True
            parsed = _consent_reject_form(response.text, str(response.url))
            if parsed is None:
                return False
            action, fields = parsed
            save = await self._session.post(
                action, data=fields, timeout=timeout, allow_redirects=True
            )
            # ponytail: SOCS is process-lifetime; 429 reset builds a new client.
            self._consent_ok = not _is_consent_interstitial(str(save.url))
            return self._consent_ok

    async def _exchange(self, send: Any, timeout: float) -> SweepHttpResponse:
        response = await send()
        if _is_consent_interstitial(str(response.url)) and await self._dismiss_consent(
            response, timeout
        ):
            response = await send()
        return _as_sweep_response(response)

    async def _aget(self, url: str, timeout: float) -> SweepHttpResponse:
        return await self._exchange(
            lambda: self._session.get(url, timeout=timeout, allow_redirects=True),
            timeout,
        )

    async def _apost(
        self,
        url: str,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
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
        )

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        return self._submit(self._aget(url, timeout), timeout=timeout)

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> SweepHttpResponse:
        return self._submit(self._apost(url, data, headers, timeout), timeout=timeout)

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
        return list(self._submit(self._apost_many(jobs, timeout), timeout=timeout))

    async def _apost_many(
        self,
        jobs: Sequence[SweepPost],
        timeout: float,
    ) -> list[SweepHttpResponse]:
        # Keep HTTP/2 multiplex on the happy path. After HTTP 429, stop
        # feeding this TLS session; unsent jobs are marked 429 so the
        # caller can continue them on a fresh session.
        semaphore = self._asyncio.Semaphore(_SWEEP_STREAMS)
        stop = self._asyncio.Event()
        out: list[SweepHttpResponse | None] = [None] * len(jobs)

        async def _one(index: int, job: SweepPost) -> None:
            if stop.is_set():
                return
            async with semaphore:
                if stop.is_set():
                    return
                try:
                    response = await self._apost(job.url, job.data, job.headers, timeout)
                except Exception:
                    stop.set()
                    out[index] = SweepHttpResponse(429, "", job.url)
                    return
                out[index] = response
                if response.status == 429:
                    stop.set()

        await self._asyncio.gather(*[_one(index, job) for index, job in enumerate(jobs)])
        return [
            item if item is not None else SweepHttpResponse(429, "", job.url)
            for item, job in zip(out, jobs, strict=True)
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


def shared_chrome_sweep_client(*, proxy: Optional[str] = None) -> ChromeSweepClient:
    global _SHARED_CLIENT, _SHARED_CLIENT_PROXY
    wanted = _normalize_proxy(proxy)
    with _SHARED_CLIENT_LOCK:
        if _SHARED_CLIENT is not None and _SHARED_CLIENT_PROXY == wanted:
            return _SHARED_CLIENT
        old = _SHARED_CLIENT
        _SHARED_CLIENT = ChromeSweepClient(proxy=wanted)
        _SHARED_CLIENT_PROXY = wanted
    if old is not None:
        old.close()
    return _SHARED_CLIENT


def reset_shared_chrome_sweep_client() -> None:
    global _SHARED_CLIENT, _SHARED_CLIENT_PROXY
    with _SHARED_CLIENT_LOCK:
        client = _SHARED_CLIENT
        _SHARED_CLIENT = None
        _SHARED_CLIENT_PROXY = None
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


@dataclass(frozen=True)
class _OpenerRequest:
    """Stand-in for urllib.request.Request so opener tests skip urllib.request."""

    full_url: str
    headers: Mapping[str, str]


class _OpenerSweepClient:
    """Test hook: urllib opener for HTML GET. POST is treated as a compact miss."""

    def __init__(self, opener: Any) -> None:
        self._opener = opener

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        html, final_url, status = _opener_get(url, opener=self._opener, timeout=timeout)
        return SweepHttpResponse(status=status, text=html, url=final_url)

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> SweepHttpResponse:
        raise CompactParseMiss("opener client has no shopping POST")

    def close(self) -> None:
        return None


def _raise_if_blocked(status: int, body: str, final_url: str, fallback_url: str) -> None:
    if status in {403, 429, 503}:
        raise GoogleFlightsBlocked(
            f"Google Flights HTTP {status} from {fallback_url}",
            status=status,
        )
    if status >= 400:
        raise GoogleFlightsBlocked(
            f"Google Flights HTTP {status} from {final_url or fallback_url}",
            status=status,
        )
    if looks_blocked(body, final_url):
        raise GoogleFlightsBlocked(
            f"Google Flights blocked the sweep at {final_url or fallback_url}"
        )


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


def _is_rate_limited_sweep_failure(exc: BaseException) -> bool:
    return isinstance(exc, GoogleFlightsBlocked) and exc.status == 429


def _http_error_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _opener_get(
    url: str,
    *,
    opener: Any,
    timeout: float,
) -> tuple[str, str, int]:
    request = _OpenerRequest(url, URLLIB_HEADERS)
    try:
        response = opener.open(request, timeout=timeout)
        with response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except Exception as exc:
        code = _http_error_code(exc)
        if code in {403, 429, 503}:
            raise GoogleFlightsBlocked(
                f"Google Flights HTTP {code} from {url}",
                status=code,
            ) from exc
        raise
    return _decode_http_body(raw, encoding), final_url, status


def fetch_search_html(
    url: str,
    *,
    opener: Optional[Any] = None,
    client: Optional[SweepHttpClient] = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    if client is not None:
        response = client.get(url, timeout=timeout)
        _raise_if_blocked(response.status, response.text, response.url, url)
        return response.text, response.url
    if opener is not None:
        html, final_url, status = _opener_get(url, opener=opener, timeout=timeout)
        _raise_if_blocked(status, html, final_url, url)
        return html, final_url
    urllib_request, urllib_error, ssl_mod = _stdlib_urllib()
    request = urllib_request.Request(url, headers=URLLIB_HEADERS)
    try:
        response = urllib_request.urlopen(
            request, timeout=timeout, context=ssl_mod.create_default_context()
        )
        with response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except urllib_error.HTTPError as exc:
        if exc.code in {403, 429, 503}:
            raise GoogleFlightsBlocked(
                f"Google Flights HTTP {exc.code} from {url}",
                status=exc.code,
            ) from exc
        raise
    html = _decode_http_body(raw, encoding)
    _raise_if_blocked(status, html, final_url, url)
    return html, final_url


def parse_http_flight_cards(html: str) -> tuple[RawFlightCard, ...]:
    return parse_flight_cards(extract_main_html(html))


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
    if _has_empty_state(parser) or parser.css_first("ul.Rk10dc") is not None:
        observed = EMPTY_STATE_TEXT if _has_empty_state(parser) else ""
        raise NoFlightsFound(observed)
    n = len(html)
    if n < _SHORT_SHELL_CHARS:
        raise GoogleFlightsBlocked(
            f"Google Flights returned a {_SHORT_SHELL_MARK} ({n} chars of main HTML)"
        )
    raise GoogleFlightsMarkupError(f"no results grid and no empty state in {n} chars of main HTML")


class GoogleFlightsHttpSource:
    """Sweep source: compact shopping RPC, HTML card parse as fallback. No Chromium."""

    def __init__(
        self,
        *,
        html_lang: str = SCRAPE_LANGUAGE,
        currency: str = SCRAPE_CURRENCY,
        country: Optional[str] = None,
        opener: Optional[Any] = None,
        client: Optional[SweepHttpClient] = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
        sleep: Optional[Callable[[float], None]] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self._html_lang = html_lang
        self._currency = currency
        self._country = country
        self._injected_client = client
        self._opener = opener
        self._timeout = timeout
        self._sleep = time.sleep if sleep is None else sleep
        self._proxy = _normalize_proxy(proxy)
        self.config = SimpleNamespace(html_lang=html_lang, currency=currency, country=country)

    def fetch(self, trip: Trip) -> tuple[RawFlightCard, ...]:
        return self._retry_sweep(lambda: self._fetch_once(trip))

    def fetch_with_calendar(
        self,
        query: Trip,
        start: date,
        end: date,
    ) -> tuple[tuple[RawFlightCard, ...], tuple[CompactCalendarDay, ...]]:
        """Shopping + typical calendar on one multiplexed round-trip."""
        return self._retry_sweep(lambda: self._fetch_with_calendar_once(query, start, end))

    def fetch_many(self, trips: Sequence[Trip]) -> list[tuple[RawFlightCard, ...] | BaseException]:
        """Multiplexed shopping POSTs for calendar-day fanout. Isolates per-trip errors."""
        if not trips:
            return []
        client = self._ensure_client()
        jobs = [self._shopping_post(trip) for trip in trips]
        responses = dispatch_posts(client, jobs, timeout=self._timeout)
        results: list[tuple[RawFlightCard, ...] | BaseException] = []
        retry_indexes: list[int] = []
        rate_indexes: list[int] = []
        for index, (trip, response) in enumerate(zip(trips, responses, strict=True)):
            try:
                results.append(self._cards_from_shopping_response(client, trip, response))
            except BaseException as exc:
                results.append(exc)
                if _is_rate_limited_sweep_failure(exc):
                    rate_indexes.append(index)
                elif _is_retriable_sweep_failure(exc):
                    retry_indexes.append(index)
        replay = sorted(set(rate_indexes) | set(retry_indexes)) if rate_indexes else retry_indexes
        if replay and SWEEP_RETRY_LIMIT >= 1:
            if rate_indexes:
                client = self._client_after_rate_limit()
            elif SWEEP_RETRY_BACKOFF_SECONDS > 0:
                self._sleep(SWEEP_RETRY_BACKOFF_SECONDS)
            retry_jobs = [jobs[index] for index in replay]
            retried = dispatch_posts(client, retry_jobs, timeout=self._timeout)
            for index, response in zip(replay, retried, strict=True):
                try:
                    results[index] = self._cards_from_shopping_response(
                        client, trips[index], response
                    )
                except BaseException as exc:
                    results[index] = exc
        return results

    def fetch_many_with_calendar(
        self,
        jobs: Sequence[tuple[Trip, date, date]],
    ) -> list[tuple[tuple[RawFlightCard, ...] | BaseException, tuple[CompactCalendarDay, ...]]]:
        """Shopping + typical calendar for many stays on one multiplexed round-trip."""
        if not jobs:
            return []
        if len(jobs) == 1:
            query, start, end = jobs[0]
            try:
                cards, days = self.fetch_with_calendar(query, start, end)
            except BaseException as exc:
                return [(exc, ())]
            return [(cards, days)]
        client = self._ensure_client()
        posts: list[SweepPost] = []
        for query, start, end in jobs:
            posts.append(self._shopping_post(query))
            posts.append(self._calendar_post(query, start, end))
        responses = dispatch_posts(client, posts, timeout=self._timeout)
        results: list[
            tuple[tuple[RawFlightCard, ...] | BaseException, tuple[CompactCalendarDay, ...]]
        ] = []
        retry_indexes: list[int] = []
        rate_indexes: list[int] = []
        for index, (query, _start, _end) in enumerate(jobs):
            shop_resp = responses[2 * index]
            cal_resp = responses[2 * index + 1]
            try:
                cards: tuple[RawFlightCard, ...] | BaseException = (
                    self._cards_from_shopping_response(client, query, shop_resp)
                )
            except BaseException as exc:
                cards = exc
                if _is_rate_limited_sweep_failure(exc):
                    rate_indexes.append(index)
                elif _is_retriable_sweep_failure(exc):
                    retry_indexes.append(index)
            days = self._days_from_calendar_response(cal_resp, posts[2 * index + 1].url)
            results.append((cards, days))
        replay = sorted(set(rate_indexes) | set(retry_indexes)) if rate_indexes else retry_indexes
        if replay and SWEEP_RETRY_LIMIT >= 1:
            if rate_indexes:
                client = self._client_after_rate_limit()
            elif SWEEP_RETRY_BACKOFF_SECONDS > 0:
                self._sleep(SWEEP_RETRY_BACKOFF_SECONDS)
            retry_posts: list[SweepPost] = []
            for index in replay:
                query, start, end = jobs[index]
                retry_posts.append(self._shopping_post(query))
                retry_posts.append(self._calendar_post(query, start, end))
            retried = dispatch_posts(client, retry_posts, timeout=self._timeout)
            for offset, index in enumerate(replay):
                query, _start, _end = jobs[index]
                shop_resp = retried[2 * offset]
                cal_resp = retried[2 * offset + 1]
                try:
                    cards = self._cards_from_shopping_response(client, query, shop_resp)
                except BaseException as exc:
                    cards = exc
                days = self._days_from_calendar_response(cal_resp, retry_posts[2 * offset + 1].url)
                results[index] = (cards, days)
        return results

    def reset(self) -> None:
        if self._injected_client is None and self._opener is None:
            reset_shared_chrome_sweep_client()

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
        if self._opener is not None:
            return _OpenerSweepClient(self._opener)
        return shared_chrome_sweep_client(proxy=self._proxy)

    def _retry_sweep(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            if SWEEP_RETRY_LIMIT < 1 or not _is_retriable_sweep_failure(exc):
                raise
            if SWEEP_RETRY_BACKOFF_SECONDS > 0:
                self._sleep(SWEEP_RETRY_BACKOFF_SECONDS)
            return fn()

    def _shopping_post(self, trip: Trip) -> SweepPost:
        url, body = build_shopping_request(
            trip, html_lang=self._html_lang, currency=self._currency, country=self._country
        )
        return SweepPost(url, body, SHOPPING_POST_HEADERS)

    def _calendar_post(self, trip: Trip, start: date, end: date) -> SweepPost:
        url, body = build_calendar_request(
            trip,
            start,
            end,
            html_lang=self._html_lang,
            currency=self._currency,
            country=self._country,
        )
        return SweepPost(url, body, SHOPPING_POST_HEADERS)

    def _fetch_once(self, trip: Trip) -> tuple[RawFlightCard, ...]:
        client = self._ensure_client()
        try:
            return self._fetch_compact(client, trip)
        except (GoogleFlightsBlocked, NoFlightsFound, GoogleFlightsRejected):
            raise
        except CompactParseMiss:
            pass
        url = build_search_url(
            trip,
            html_lang=self._html_lang,
            currency=self._currency,
            country=self._country,
        )
        html, _final_url = fetch_search_html(url, client=client, timeout=self._timeout)
        return parse_http_flight_cards(html)

    def _fetch_with_calendar_once(
        self,
        query: Trip,
        start: date,
        end: date,
    ) -> tuple[tuple[RawFlightCard, ...], tuple[CompactCalendarDay, ...]]:
        client = self._ensure_client()
        posts = (self._shopping_post(query), self._calendar_post(query, start, end))
        shop_resp, cal_resp = dispatch_posts(client, posts, timeout=self._timeout)
        try:
            cards = self._cards_from_shopping_response(client, query, shop_resp)
        except CompactParseMiss:
            cards = self._html_cards(client, query)
        days = self._days_from_calendar_response(cal_resp, posts[1].url)
        return cards, days

    def _fetch_compact(self, client: SweepHttpClient, trip: Trip) -> tuple[RawFlightCard, ...]:
        url, body = build_shopping_request(
            trip,
            html_lang=self._html_lang,
            currency=self._currency,
            country=self._country,
        )
        try:
            response = client.post(
                url, data=body, headers=SHOPPING_POST_HEADERS, timeout=self._timeout
            )
        except CompactParseMiss:
            raise
        except Exception as exc:
            raise CompactParseMiss(f"shopping POST failed: {exc}") from exc
        return self._cards_from_shopping_response(client, trip, response)

    def _cards_from_shopping_response(
        self,
        client: SweepHttpClient,
        trip: Trip,
        response: SweepHttpResponse,
    ) -> tuple[RawFlightCard, ...]:
        url, _body = build_shopping_request(
            trip, html_lang=self._html_lang, currency=self._currency, country=self._country
        )
        if (
            response.status in {403, 429}
            or response.status >= 500
            or looks_blocked(response.text, response.url)
        ):
            _raise_if_blocked(response.status, response.text, response.url, url)
        if response.status >= 400:
            raise CompactParseMiss(f"shopping HTTP {response.status}")
        try:
            return parse_shopping_body(response.text, currency=self._currency)
        except EmptyShoppingResults as exc:
            raise NoFlightsFound() from exc
        except ShoppingRejected as exc:
            raise GoogleFlightsRejected(str(exc)) from exc
        except CompactParseMiss:
            return self._html_cards(client, trip)

    def _html_cards(self, client: SweepHttpClient, trip: Trip) -> tuple[RawFlightCard, ...]:
        url = build_search_url(
            trip, html_lang=self._html_lang, currency=self._currency, country=self._country
        )
        html, _final_url = fetch_search_html(url, client=client, timeout=self._timeout)
        return parse_http_flight_cards(html)

    def _days_from_calendar_response(
        self,
        response: SweepHttpResponse,
        url: str,
    ) -> tuple[CompactCalendarDay, ...]:
        try:
            if (
                response.status in {403, 429, 503}
                or looks_blocked(response.text, response.url)
                or response.status >= 400
            ):
                if response.status in {403, 429, 503} or looks_blocked(response.text, response.url):
                    _raise_if_blocked(response.status, response.text, response.url, url)
                return ()
            return parse_calendar_body(response.text)
        except Exception:
            return ()

    def fetch_calendar(
        self,
        trip: Trip,
        start: date,
        end: date,
    ) -> tuple[CompactCalendarDay, ...]:
        client = self._ensure_client()
        url, body = build_calendar_request(
            trip,
            start,
            end,
            html_lang=self._html_lang,
            currency=self._currency,
            country=self._country,
        )
        response = self._post_rpc(client, url, body)
        try:
            return parse_calendar_body(response.text)
        except ShoppingRejected as exc:
            raise GoogleFlightsRejected(str(exc)) from exc

    def fetch_explore(
        self,
        origin: str,
        departure_date: date,
        *,
        adults: int = 1,
        cabin: FlightCabin = "economy",
        children: int = 0,
        infants_in_seat: int = 0,
        infants_on_lap: int = 0,
    ) -> tuple[CompactExplorePlace, ...]:
        client = self._ensure_client()
        url, body = build_explore_request(
            origin,
            departure_date,
            adults=adults,
            cabin=cabin,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            html_lang=self._html_lang,
            currency=self._currency,
            country=self._country,
        )
        response = self._post_rpc(client, url, body)
        try:
            return parse_explore_body(response.text)
        except ShoppingRejected as exc:
            raise GoogleFlightsRejected(str(exc)) from exc

    def _post_rpc(self, client: SweepHttpClient, url: str, body: str) -> SweepHttpResponse:
        try:
            response = client.post(
                url, data=body, headers=SHOPPING_POST_HEADERS, timeout=self._timeout
            )
        except CompactParseMiss:
            raise
        except Exception as exc:
            raise CompactParseMiss(f"shopping POST failed: {exc}") from exc
        if response.status in {403, 429, 503} or looks_blocked(response.text, response.url):
            _raise_if_blocked(response.status, response.text, response.url, url)
        if response.status >= 400:
            raise CompactParseMiss(f"shopping HTTP {response.status}")
        return response


class GoogleFlightsSource:
    def __init__(
        self,
        state_dir: Path,
        session: Optional[ChromiumSession] = None,
        config: Optional[BrowserSessionConfig] = None,
        *,
        currency: str = SCRAPE_CURRENCY,
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
        self._http: Optional[GoogleFlightsHttpSource] = None

    @property
    def config(self) -> BrowserSessionConfig:
        return self._config

    def fetch(self, trip: Trip) -> tuple[RawFlightCard, ...]:
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

    def fetch_calendar(
        self,
        trip: Trip,
        start: date,
        end: date,
    ) -> tuple[CompactCalendarDay, ...]:
        if self._http is None:
            self._http = GoogleFlightsHttpSource(
                html_lang=self._config.html_lang,
                currency=self._config.currency,
                country=self._config.country,
            )
        return self._http.fetch_calendar(trip, start, end)

    def reset(self) -> None:
        self._session.reset()

    def close(self) -> None:
        self._session.close()
        if self._http is not None:
            self._http.close()
            self._http = None

    def _fetch_html(self, url: str) -> str:
        page = self._session.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            if "consent.google" in page.url:
                self._dismiss_consent(page)
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
