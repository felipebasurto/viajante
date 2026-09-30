"""Google Hotels sweep source: owned AtySUc RPC on a Chrome TLS session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode

from viajante.google_flights import (
    COOLDOWN_UNCHECKED,
    NOT_SENT,
    SweepHttpClient,
    SweepHttpResponse,
    SweepPost,
    cooldown_client,
    dispatch_posts,
    reset_shared_chrome_sweep_client,
    shared_chrome_sweep_client,
)
from viajante.google_hotels_rpc import (
    HOTELS_POST_HEADERS,
    HOTELS_SEARCH_URL,
    SORT_LOWEST_PRICE,
    SORT_RELEVANCE,
    EmptyHotelResults,
    HotelsBlocked,
    HotelsParseMiss,
    HotelsRejected,
    _looks_blocked,
    build_hotels_request,
    parse_hotels_body,
)
from viajante.models import (
    FETCH_LANGUAGE,
    AppliedHotelFilters,
    HotelPage,
    HotelQuery,
    RawHotelCard,
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
        self._cooldown = COOLDOWN_UNCHECKED
        self.config = SimpleNamespace(html_lang=html_lang, currency=currency)

    def fetch(
        self,
        query: HotelQuery,
        applied: AppliedHotelFilters,
        limit: int,
    ) -> HotelPage:
        del applied
        client = self._ensure_client()
        # A price-sorted page is mostly low-rated when min_rating is named, so the
        # relevance page rides the same multiplexed round-trip to widen the pool.
        sorts = [SORT_LOWEST_PRICE] + ([SORT_RELEVANCE] if query.min_rating is not None else [])
        posts = []
        for sort in sorts:
            url, body = build_hotels_request(
                query, html_lang=self._html_lang, currency=self._currency, sort=sort
            )
            posts.append(SweepPost(url, body, HOTELS_POST_HEADERS))
        responses = dispatch_posts(client, posts, timeout=self._timeout)
        cards = list(self._cards(responses[0], posts[0].url)[:limit])
        for post, response in zip(posts[1:], responses[1:], strict=True):
            try:
                cards.extend(self._cards(response, post.url)[:limit])
            except (HotelsBlocked, HotelsParseMiss, EmptyHotelResults, HotelsRejected):
                continue
        return HotelPage(cards=tuple(cards))

    @staticmethod
    def _cards(response: SweepHttpResponse, url: str) -> tuple[RawHotelCard, ...]:
        advice = response.rate_limit
        if response.status == 429 and advice:
            message = advice if advice.startswith(NOT_SENT) else f"Google Hotels HTTP 429. {advice}"
            raise HotelsBlocked(message, rate_limited=True)
        if response.status in {403, 429, 503} or _looks_blocked(f"{response.text} {response.url}"):
            raise HotelsBlocked(f"Google Hotels HTTP {response.status} from {url}")
        if response.status >= 400:
            raise HotelsParseMiss(f"hotel HTTP {response.status}")
        return parse_hotels_body(response.text)

    def reset(self) -> None:
        if self._injected_client is None:
            reset_shared_chrome_sweep_client()

    def close(self) -> None:
        return None

    def _ensure_client(self) -> SweepHttpClient:
        if self._injected_client is not None:
            return self._injected_client
        self._cooldown, paused = cooldown_client(self._cooldown)
        return paused or shared_chrome_sweep_client()
