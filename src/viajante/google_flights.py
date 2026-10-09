"""Google Flights URL building, consent, card parsing, and page source."""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlencode, urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

from viajante.consent import is_reject_form, restore_consent_cookies, save_consent_cookies
from viajante.control import (
    SearchCancelled,
    SearchDeadline,
    checkpoint,
    current_control,
    wait_for_future,
)
from viajante.google_flights_rpc import (  # noqa: F401 - RawFlightCard is re-exported for callers
    RawFlightCard,
    raw_rpc_error_status,
    rpc_error_status,
)
from viajante.models import (
    FETCH_LANGUAGE,
    Trip,
)
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

SCRAPE_LANGUAGE = FETCH_LANGUAGE
# Owned `tfu` blob that selects result tabs; not produced by encode_tfs.
RESULT_TABS = "EgQIABABIgA"

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
        value = float(headers.get("retry-after") or headers.get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _settle_batch(
    out: Sequence[SweepHttpResponse | None],
    sent: Sequence[bool],
    jobs: Sequence[SweepPost],
    *,
    deadline: bool,
) -> list[SweepHttpResponse]:
    """Answered jobs stay as they are; the rest are stamped as not loaded."""
    settled: list[SweepHttpResponse] = []
    for index, (item, job) in enumerate(zip(out, jobs, strict=True)):
        if item is not None:
            settled.append(item)
        elif deadline:
            settled.append(
                SweepHttpResponse(
                    0,
                    "",
                    job.url,
                    deadline=True,
                    request_sent=sent[index],
                    attempts=1 if sent[index] else 0,
                )
            )
        else:
            settled.append(
                SweepHttpResponse(
                    SWEEP_TRANSPORT_STATUS,
                    "TimeoutError: batch wait exceeded",
                    job.url,
                    request_sent=sent[index],
                    attempts=1 if sent[index] else 0,
                )
            )
    return settled


class _CooldownClient:
    """Answers every request with a local 429 while a recorded cooldown runs. No network."""

    def __init__(self, state: Mapping[str, float]) -> None:
        self._advice = rate_limit_advice(state, sent=False)
        self._basis = str(state.get("basis", "unknown"))

    def _answer(self, url: str) -> SweepHttpResponse:
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

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        return self._answer(url)

    def post(
        self, url: str, *, data: str, headers: Mapping[str, str], timeout: float
    ) -> SweepHttpResponse:
        return self._answer(url)

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
                restore_consent_cookies(self._session)
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
        try:
            return wait_for_future(future, max(timeout + 5.0, 10.0))
        except FutureTimeout:
            # Stop the requests still queued on the loop; nothing should run unawaited.
            future.cancel()
            raise

    def _batch_timeout(self, count: int, timeout: float) -> float:
        """A batch runs in waves of ``streams`` requests, so its wait grows with the waves."""
        streams = getattr(self, "_streams", _SWEEP_STREAMS)
        return timeout * math.ceil(count / streams)

    async def _dismiss_consent(
        self,
        response: Any,
        timeout: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        async with self._consent_lock:
            _raise_if_cancelled(cancel_event)
            parsed = _consent_reject_form(response.text, str(response.url))
            if parsed is None:
                return False
            action, fields = parsed
            save = await self._session.post(
                action, data=fields, timeout=timeout, allow_redirects=True
            )
            _raise_if_cancelled(cancel_event)
            dismissed = not _is_consent_interstitial(str(save.url))
            # Only a declined consent is kept, so the next process skips this round trip.
            if dismissed and is_reject_form(fields):
                save_consent_cookies(self._session)
            return dismissed

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
            state = note_rate_limited(_retry_after_seconds(response), endpoint=str(response.url))
            out = replace(
                out,
                rate_limit=rate_limit_advice(state),
                cooldown_basis=state.get("basis", "unknown"),
            )
        elif out.status == 200 and raw_rpc_error_status(out.text) == RPC_THROTTLE_STATUS:
            state = note_rate_limited(
                basis="heuristic_rpc_13", cause="rpc_13", endpoint=str(response.url)
            )
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
                    timeout=self._batch_timeout(len(jobs), timeout),
                )
            )
        except SearchDeadline:
            # Responses that already arrived are real; only the rest were not loaded.
            return _settle_batch(out, sent, jobs, deadline=True)
        except FutureTimeout:
            return _settle_batch(out, sent, jobs, deadline=False)

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
                    timeout=self._batch_timeout(len(jobs), timeout),
                )
            )
        except SearchDeadline:
            return _settle_batch(out, sent, jobs, deadline=True)
        except FutureTimeout:
            return _settle_batch(out, sent, jobs, deadline=False)

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
    """Once-retry for empty results, markup drift, and HTTP 5xx. Any other block never replays.

    Other blocks are status None (consent walls, short shells) and 403 or 429.
    """
    if isinstance(exc, (NoFlightsFound, GoogleFlightsMarkupError)):
        return True
    return (
        isinstance(exc, GoogleFlightsBlocked) and isinstance(exc.status, int) and exc.status >= 500
    )


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
