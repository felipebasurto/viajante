"""Google Flights URL building, consent, card parsing, and page source."""

from __future__ import annotations

import contextlib
import gzip
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Optional, Protocol
from urllib.parse import urlencode

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
from viajante.models import FlightCabin, FlightQuery, Trip
from viajante.tfs import encode_tfs

SEARCH_URL = "https://www.google.com/travel/flights"
STATE_FILENAME = "pw_state_google.json"

SCRAPE_LANGUAGE = "en"
SCRAPE_CURRENCY = "EUR"
# Owned `tfu` blob that selects result tabs; not produced by encode_tfs.
RESULT_TABS = "EgQIABABIgA"

PAGE_TIMEOUT_MS = 60_000
CONSENT_CLICK_TIMEOUT_MS = 5_000
CONSENT_SETTLE_MS = 1_500
HTTP_TIMEOUT_SECONDS = 30
# Current Linux Chrome; do not spoof a stale Chrome/macOS UA.
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


class GoogleFlightsRejected(RuntimeError):
    """Shopping RPC rejected the query (unknown airport or invalid request)."""


def build_search_params(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str = SCRAPE_CURRENCY,
) -> dict[str, str]:
    return {
        "tfs": encode_tfs(trip),
        "hl": html_lang,
        "tfu": RESULT_TABS,
        "curr": currency,
    }


def build_search_url(
    trip: Trip,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str = SCRAPE_CURRENCY,
) -> str:
    params = build_search_params(trip, html_lang=html_lang, currency=currency)
    return f"{SEARCH_URL}?{urlencode(params)}"


def build_itinerary_url(
    booking_token: str,
    *,
    html_lang: str = SCRAPE_LANGUAGE,
    currency: str = SCRAPE_CURRENCY,
) -> str:
    token = booking_token.strip()
    if not token:
        raise ValueError("booking_token must not be blank")
    return f"{SEARCH_URL}?" + urlencode({"hl": html_lang, "curr": currency, "booking_token": token})


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


def _decode_http_body(raw: bytes, content_encoding: str) -> str:
    encoding = content_encoding.casefold()
    if "gzip" in encoding or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        raw = gzip.decompress(raw, wbits=-15)
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


class ChromeSweepClient:
    """One curl_cffi session: Chrome TLS, HTTP/2, keep-alive."""

    def __init__(self) -> None:
        self._session = _curl_requests().Session(impersonate="chrome")

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        response = self._session.get(url, timeout=timeout, allow_redirects=True)
        return SweepHttpResponse(
            status=int(response.status_code),
            text=response.text,
            url=str(response.url),
        )

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> SweepHttpResponse:
        response = self._session.post(
            url,
            data=data,
            headers=dict(headers),
            timeout=timeout,
            allow_redirects=True,
        )
        return SweepHttpResponse(
            status=int(response.status_code),
            text=response.text,
            url=str(response.url),
        )

    def close(self) -> None:
        self._session.close()


class _OpenerSweepClient:
    """Test hook: urllib opener for HTML GET. POST is treated as a compact miss."""

    def __init__(self, opener: urllib.request.OpenerDirector) -> None:
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
        raise GoogleFlightsBlocked(f"Google Flights HTTP {status} from {fallback_url}")
    if status >= 400:
        raise GoogleFlightsBlocked(f"Google Flights HTTP {status} from {final_url or fallback_url}")
    if looks_blocked(body, final_url):
        raise GoogleFlightsBlocked(
            f"Google Flights blocked the sweep at {final_url or fallback_url}"
        )


def _opener_get(
    url: str,
    *,
    opener: urllib.request.OpenerDirector,
    timeout: float,
) -> tuple[str, str, int]:
    request = urllib.request.Request(url, headers=URLLIB_HEADERS)
    try:
        response = opener.open(request, timeout=timeout)
        with response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429, 503}:
            raise GoogleFlightsBlocked(f"Google Flights HTTP {exc.code} from {url}") from exc
        raise
    return _decode_http_body(raw, encoding), final_url, status


def fetch_search_html(
    url: str,
    *,
    opener: Optional[urllib.request.OpenerDirector] = None,
    client: Optional[SweepHttpClient] = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    if client is not None:
        response = client.get(url, timeout=timeout)
        _raise_if_blocked(response.status, response.text, response.url, url)
        return response.text, response.url
    request = urllib.request.Request(url, headers=URLLIB_HEADERS)
    try:
        if opener is None:
            response = urllib.request.urlopen(
                request, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            response = opener.open(request, timeout=timeout)
        with response:
            raw = response.read()
            encoding = response.headers.get("Content-Encoding", "")
            final_url = response.geturl()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if exc.code in {403, 429, 503}:
            raise GoogleFlightsBlocked(f"Google Flights HTTP {exc.code} from {url}") from exc
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
    # Unknown shells without either signal are markup drift.
    if _has_empty_state(parser) or parser.css_first("ul.Rk10dc") is not None:
        observed = EMPTY_STATE_TEXT if _has_empty_state(parser) else ""
        raise NoFlightsFound(observed)
    raise GoogleFlightsMarkupError(
        f"no results grid and no empty state in {len(html)} chars of main HTML"
    )


class GoogleFlightsHttpSource:
    """Sweep source: compact shopping RPC, HTML card parse as fallback. No Chromium."""

    def __init__(
        self,
        *,
        html_lang: str = SCRAPE_LANGUAGE,
        currency: str = SCRAPE_CURRENCY,
        opener: Optional[urllib.request.OpenerDirector] = None,
        client: Optional[SweepHttpClient] = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._html_lang = html_lang
        self._currency = currency
        self._injected_client = client
        self._opener = opener
        self._owned_client: Optional[ChromeSweepClient] = None
        self._timeout = timeout
        self.config = SimpleNamespace(html_lang=html_lang, currency=currency)

    def fetch(self, trip: Trip) -> tuple[RawFlightCard, ...]:
        client = self._ensure_client()
        try:
            return self._fetch_compact(client, trip)
        except (GoogleFlightsBlocked, NoFlightsFound, GoogleFlightsRejected):
            raise
        except CompactParseMiss:
            pass
        url = build_search_url(trip, html_lang=self._html_lang, currency=self._currency)
        html, _final_url = fetch_search_html(url, client=client, timeout=self._timeout)
        return parse_http_flight_cards(html)

    def reset(self) -> None:
        self._close_owned_client()

    def close(self) -> None:
        self._close_owned_client()

    def _ensure_client(self) -> SweepHttpClient:
        if self._injected_client is not None:
            return self._injected_client
        if self._opener is not None:
            return _OpenerSweepClient(self._opener)
        if self._owned_client is None:
            self._owned_client = ChromeSweepClient()
        return self._owned_client

    def _close_owned_client(self) -> None:
        if self._owned_client is not None:
            self._owned_client.close()
            self._owned_client = None

    def _fetch_compact(self, client: SweepHttpClient, trip: Trip) -> tuple[RawFlightCard, ...]:
        url, body = build_shopping_request(trip, html_lang=self._html_lang, currency=self._currency)
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
        try:
            return parse_shopping_body(response.text)
        except EmptyShoppingResults as exc:
            raise NoFlightsFound() from exc
        except ShoppingRejected as exc:
            raise GoogleFlightsRejected(str(exc)) from exc

    def fetch_calendar(
        self,
        query: FlightQuery,
        start: date,
        end: date,
    ) -> tuple[CompactCalendarDay, ...]:
        client = self._ensure_client()
        url, body = build_calendar_request(
            query,
            start,
            end,
            html_lang=self._html_lang,
            currency=self._currency,
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
    ) -> tuple[CompactExplorePlace, ...]:
        client = self._ensure_client()
        url, body = build_explore_request(
            origin,
            departure_date,
            adults=adults,
            cabin=cabin,
            html_lang=self._html_lang,
            currency=self._currency,
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
    ) -> None:
        self._config = config or BrowserSessionConfig(
            state_filename=STATE_FILENAME,
            locale="en-US",
            html_lang=SCRAPE_LANGUAGE,
            currency=SCRAPE_CURRENCY,
        )
        self._session = session or ChromiumSession(state_dir, self._config)

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
                )
            )
        )

    def reset(self) -> None:
        self._session.reset()

    def close(self) -> None:
        self._session.close()

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
