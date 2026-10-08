"""Google Hotels sweep source: owned AtySUc RPC on a Chrome TLS session."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from viajante.control import SearchDeadline, note_cut
from viajante.google_flights import (
    COOLDOWN_UNCHECKED,
    NOT_SENT,
    SWEEP_TRANSPORT_STATUS,
    SweepHttpClient,
    SweepHttpResponse,
    SweepPost,
    SweepTransportError,
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
    parse_hotels_page,
)
from viajante.google_hotels_url import hotel_navigation_params
from viajante.models import (
    FETCH_LANGUAGE,
    AppliedHotelFilters,
    HotelPage,
    HotelQuery,
    SearchError,
    SearchErrorCode,
)

HTTP_TIMEOUT_SECONDS = 30


def build_applied_filters(
    query: HotelQuery,
    *,
    html_lang: str = FETCH_LANGUAGE,
    currency: str,
) -> AppliedHotelFilters:
    chips: list[str] = []
    if query.free_cancellation:
        chips.append("free_cancellation=1")
    if query.entire_home:
        chips.append("property_type=vacation_rentals")
    params = hotel_navigation_params(query, currency=currency, html_lang=html_lang)
    return AppliedHotelFilters(
        chips=tuple(chips), url=f"{HOTELS_SEARCH_URL}?{params}", url_context="stay"
    )


class GoogleHotelsSource:
    """Sweep source: compact AtySUc parse. No Chromium."""

    def __init__(
        self,
        *,
        html_lang: str = FETCH_LANGUAGE,
        currency: str,
        client: Optional[SweepHttpClient] = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._html_lang = html_lang
        self._currency = currency
        self._injected_client = client
        self._timeout = timeout
        self._cooldown = COOLDOWN_UNCHECKED

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
        lost = [i for i, r in enumerate(responses) if r.status == SWEEP_TRANSPORT_STATUS]
        if lost:
            # One replay on a fresh session; a second transport failure is final (the
            # hotel loop does not retry it).
            self.reset()
            client = self._ensure_client()
            for i, again in zip(
                lost,
                dispatch_posts(client, [posts[i] for i in lost], timeout=self._timeout),
                strict=True,
            ):
                responses[i] = again
        first = self._page(responses[0], posts[0].url)
        cards = list(first.cards[:limit])
        page_errors = []
        for post, response in zip(posts[1:], responses[1:], strict=True):
            try:
                cards.extend(self._page(response, post.url).cards[:limit])
            except SearchDeadline:
                # The price page already arrived; only the widening page was cut.
                note_cut()
                continue
            except SweepTransportError as exc:
                page_errors.append(
                    SearchError(
                        code=SearchErrorCode.FETCH_FAILED, message=str(exc), timeout=exc.timeout
                    )
                )
            except (HotelsBlocked, HotelsParseMiss, EmptyHotelResults, HotelsRejected):
                continue
        params = hotel_navigation_params(query, currency=self._currency, html_lang=self._html_lang)
        return HotelPage(
            cards=tuple(
                replace(card, link=f"{card.link}?{params}", link_context="stay")
                if card.link
                else card
                for card in cards
            ),
            page_errors=tuple(page_errors),
            resolved_place=first.resolved_place,
            place_bounds=first.place_bounds,
        )

    @staticmethod
    def _page(response: SweepHttpResponse, url: str) -> HotelPage:
        if response.deadline:
            raise SearchDeadline()
        advice = response.rate_limit
        if advice and response.status < 400:
            # A data-less RPC status 13 envelope: the cooldown is already recorded.
            raise HotelsBlocked(advice, rate_limited=True)
        if response.status == SWEEP_TRANSPORT_STATUS:
            raise SweepTransportError(
                f"Google Hotels request failed before any response: {response.text}",
                timeout="timeout" in response.text.partition(":")[0].casefold(),
            )
        if response.status == 429 and advice:
            message = advice if advice.startswith(NOT_SENT) else f"Google Hotels HTTP 429. {advice}"
            raise HotelsBlocked(message, rate_limited=True)
        if response.status in {403, 429, 503} or _looks_blocked(f"{response.text} {response.url}"):
            raise HotelsBlocked(f"Google Hotels HTTP {response.status} from {url}")
        if response.status >= 400:
            raise HotelsParseMiss(f"hotel HTTP {response.status}")
        return parse_hotels_page(response.text)

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
