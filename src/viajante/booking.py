"""Booking.com URL, consent, and property-card scrape."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.models import (
    FETCH_LANGUAGE,
    FETCH_LOCALE,
    AppliedHotelFilters,
    HotelPage,
    HotelQuery,
    RawHotelCard,
)
from viajante.storage import write_text_atomic

BOOKING_SEARCH_URL = "https://www.booking.com/searchresults.html"
PROPERTY_CARD_SELECTOR = '[data-testid="property-card"]'
EMPTY_STATE_SELECTORS = (
    '[data-testid="searchresults-empty"]',
    '[data-testid="empty-state"]',
    '[data-testid="no-results"]',
)
CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    'button:has-text("Accept")',
    'button:has-text("Aceptar")',
    'button:has-text("Accept all")',
    'button:has-text("Aceptar todas")',
    '[id*="accept"]',
)
DISMISS_SELECTORS = (
    '[aria-label="Dismiss sign-in info."]',
    '[aria-label="Cerrar información de inicio de sesión."]',
    'button[aria-label*="Dismiss sign-in"]',
    'button[aria-label*="Cerrar información"]',
)
BOOKING_STATE_FILENAME = "pw_state_booking.json"
BOOKING_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media"})
PAGE_TIMEOUT_MS = 60_000
RESULTS_TIMEOUT_MS = 60_000
OVERLAY_CLICK_TIMEOUT_MS = 3_000
OVERLAY_SETTLE_MS = 800
FAILURE_HTML_NAME = "booking-last-failure.html"
FAILURE_META_NAME = "booking-last-failure.txt"


class BookingResultsTimeout(TimeoutError):
    """Cards or empty state never appeared; likely consent, challenge, or markup drift."""


def build_applied_filters(
    query: HotelQuery,
    *,
    html_lang: str = FETCH_LANGUAGE,
    currency: str = "EUR",
) -> AppliedHotelFilters:
    chips: list[str] = []
    if query.free_cancellation:
        chips.append("oos=1")
    if query.entire_home:
        chips.extend(("privacy_type=3", "ht_id=201"))

    params = {
        "ss": query.location,
        "checkin": query.check_in.isoformat(),
        "checkout": query.check_out.isoformat(),
        "group_adults": str(query.adults),
        "no_rooms": str(query.rooms),
        "group_children": "0",
        "selected_currency": currency,
        "lang": html_lang,
        "order": "price",
    }
    if chips:
        params["nflt"] = ";".join(chips)
    return AppliedHotelFilters(
        chips=tuple(chips),
        url=f"{BOOKING_SEARCH_URL}?{urlencode(params)}",
    )


class BookingHotelsSource:
    def __init__(
        self,
        state_dir: Path,
        session: Optional[ChromiumSession] = None,
        config: Optional[BrowserSessionConfig] = None,
        *,
        currency: str = "EUR",
    ) -> None:
        self._state_dir = state_dir
        self._config = config or BrowserSessionConfig(
            state_filename=BOOKING_STATE_FILENAME,
            locale=FETCH_LOCALE,
            html_lang=FETCH_LANGUAGE,
            currency=currency,
            viewport={"width": 1280, "height": 900},
            blocked_resource_types=BOOKING_BLOCKED_RESOURCE_TYPES,
        )
        self._session = session or ChromiumSession(state_dir, self._config)

    @property
    def config(self) -> BrowserSessionConfig:
        return self._config

    @staticmethod
    def _click_first_visible(page, selectors: Tuple[str, ...]) -> bool:
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.count() > 0 and button.is_visible():
                    button.click(timeout=OVERLAY_CLICK_TIMEOUT_MS)
                    page.wait_for_timeout(OVERLAY_SETTLE_MS)
                    return True
            except Exception:
                continue
        return False

    @classmethod
    def _dismiss_overlays(cls, page) -> None:
        cls._click_first_visible(page, CONSENT_SELECTORS)
        cls._click_first_visible(page, DISMISS_SELECTORS)

    def _record_failure(self, page) -> None:
        url = ""
        html = ""
        with contextlib.suppress(Exception):
            url = str(getattr(page, "url", "") or "")
        with contextlib.suppress(Exception):
            html = page.content()
        write_text_atomic(f"url: {url}\n", self._state_dir / FAILURE_META_NAME)
        if html:
            write_text_atomic(html, self._state_dir / FAILURE_HTML_NAME)
        with contextlib.suppress(Exception):
            page.screenshot(path=str(self._state_dir / "booking-last-failure.png"))

    @staticmethod
    def _clean_link(link: Optional[str]) -> Optional[str]:
        if not link:
            return None
        absolute = urljoin("https://www.booking.com/", link)
        parsed = urlsplit(absolute)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    @classmethod
    def _extract_card(cls, card) -> Optional[RawHotelCard]:
        title_element = card.query_selector('[data-testid="title"]')
        price_element = card.query_selector('[data-testid="price-and-discounted-price"]')
        if title_element is None:
            return None

        title = title_element.inner_text().strip()
        if not title:
            return None
        total_price = price_element.inner_text().strip() if price_element is not None else ""

        rating_element = card.query_selector('[data-testid="review-score"]')
        address_element = card.query_selector('[data-testid="address-link"]')
        if address_element is None:
            address_element = card.query_selector('[data-testid="address"]')
        link_element = card.query_selector('a[data-testid="title-link"]')

        rating = None
        if rating_element is not None:
            rating_text = rating_element.inner_text()
            rating = rating_text.splitlines()[0].strip() if rating_text else None
        address = address_element.inner_text().strip() if address_element is not None else None
        link = (
            cls._clean_link(link_element.get_attribute("href"))
            if link_element is not None
            else None
        )
        return RawHotelCard(
            title=title,
            address=address or None,
            total_price=total_price,
            rating=rating or None,
            details=card.inner_text(),
            link=link,
        )

    @staticmethod
    def _has_empty_state(page) -> bool:
        for selector in EMPTY_STATE_SELECTORS:
            try:
                if page.locator(selector).first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def fetch(
        self,
        query: HotelQuery,
        applied: AppliedHotelFilters,
        limit: int,
    ) -> HotelPage:
        if limit <= 0:
            raise ValueError("limit must be positive")
        page = self._session.new_page()
        try:
            page.goto(
                applied.url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )
            self._dismiss_overlays(page)
            result_selector = ", ".join((PROPERTY_CARD_SELECTOR,) + EMPTY_STATE_SELECTORS)
            try:
                page.locator(result_selector).first.wait_for(timeout=RESULTS_TIMEOUT_MS)
            except Exception as exc:
                self._record_failure(page)
                message = str(exc)
                if "timeout" in type(exc).__name__.casefold() or "timeout" in message.casefold():
                    raise BookingResultsTimeout(message) from exc
                raise
            provider_cards = page.query_selector_all(PROPERTY_CARD_SELECTOR)
            if not provider_cards:
                if self._has_empty_state(page):
                    return HotelPage(cards=())
                self._record_failure(page)
                raise RuntimeError("Booking results page has no recognized result state")

            cards: list[RawHotelCard] = []
            for provider_card in provider_cards[:limit]:
                try:
                    extracted = self._extract_card(provider_card)
                    if extracted is not None:
                        cards.append(extracted)
                except Exception:
                    continue
            if not cards:
                self._record_failure(page)
                raise RuntimeError("Booking property cards could not be parsed")
            return HotelPage(cards=tuple(cards))
        finally:
            with contextlib.suppress(Exception):
                page.close()

    def reset(self) -> None:
        self._session.reset()

    def close(self) -> None:
        self._session.close()
