"""Google Hotels sweep source: owned AtySUc RPC on a Chrome TLS session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode

from viajante.google_flights import (
    SweepHttpClient,
    reset_shared_chrome_sweep_client,
    shared_chrome_sweep_client,
)
from viajante.google_hotels_rpc import (
    HOTELS_POST_HEADERS,
    HOTELS_SEARCH_URL,
    HotelsBlocked,
    HotelsParseMiss,
    build_hotels_request,
    parse_hotels_body,
)
from viajante.models import (
    FETCH_LANGUAGE,
    AppliedHotelFilters,
    HotelPage,
    HotelQuery,
)

HTTP_TIMEOUT_SECONDS = 30


def build_applied_filters(
    query: HotelQuery,
    *,
    html_lang: str = FETCH_LANGUAGE,
    currency: str = "EUR",
) -> AppliedHotelFilters:
    chips: list[str] = []
    if query.free_cancellation:
        chips.append("free_cancellation=1")
    if query.entire_home:
        chips.append("property_type=vacation_rentals")
    params = {
        "q": f"{query.location} hotels",
        "hl": html_lang,
        "curr": currency,
    }
    return AppliedHotelFilters(chips=tuple(chips), url=f"{HOTELS_SEARCH_URL}?{urlencode(params)}")


class GoogleHotelsSource:
    """Sweep source: compact AtySUc parse. No Chromium."""

    def __init__(
        self,
        *,
        html_lang: str = FETCH_LANGUAGE,
        currency: str = "EUR",
        client: Optional[SweepHttpClient] = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._html_lang = html_lang
        self._currency = currency
        self._injected_client = client
        self._timeout = timeout
        self.config = SimpleNamespace(html_lang=html_lang, currency=currency)

    def fetch(
        self,
        query: HotelQuery,
        applied: AppliedHotelFilters,
        limit: int,
    ) -> HotelPage:
        del applied
        client = self._ensure_client()
        url, body = build_hotels_request(query, html_lang=self._html_lang, currency=self._currency)
        response = client.post(url, data=body, headers=HOTELS_POST_HEADERS, timeout=self._timeout)
        if response.status in {403, 429, 503} or _looks_blocked(response.text, response.url):
            raise HotelsBlocked(f"Google Hotels HTTP {response.status} from {url}")
        if response.status >= 400:
            raise HotelsParseMiss(f"hotel HTTP {response.status}")
        cards = parse_hotels_body(response.text)
        return HotelPage(cards=cards[:limit])

    def reset(self) -> None:
        if self._injected_client is None:
            reset_shared_chrome_sweep_client()

    def close(self) -> None:
        return None

    def _ensure_client(self) -> SweepHttpClient:
        if self._injected_client is not None:
            return self._injected_client
        return shared_chrome_sweep_client()


def _looks_blocked(body: str, final_url: str) -> bool:
    lowered = f"{body} {final_url}".casefold()
    return "/sorry/" in lowered or "unusual traffic" in lowered
