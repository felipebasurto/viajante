from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import List, Sequence
from urllib.parse import parse_qs, urlparse

import viajante.booking as booking_module
from viajante.booking import (
    BOOKING_BLOCKED_RESOURCE_TYPES,
    CONSENT_SELECTORS,
    DISMISS_SELECTORS,
    EMPTY_STATE_SELECTORS,
    FAILURE_HTML_NAME,
    FAILURE_META_NAME,
    PROPERTY_CARD_SELECTOR,
    BookingHotelsSource,
    BookingResultsTimeout,
    HotelPage,
    build_applied_filters,
)
from viajante.models import HotelQuery


class FakeElement:
    def __init__(self, text: str, href: str | None = None) -> None:
        self.text = text
        self.href = href

    def inner_text(self) -> str:
        return self.text

    def get_attribute(self, name: str) -> str | None:
        return self.href if name == "href" else None


class FakeProviderCard:
    def __init__(
        self,
        elements: dict[str, FakeElement],
        details: str,
    ) -> None:
        self.elements = elements
        self.details = details

    def query_selector(self, selector: str) -> FakeElement | None:
        return self.elements.get(selector)

    def inner_text(self) -> str:
        return self.details


class FakeLocator:
    def __init__(
        self,
        *,
        count: int = 0,
        visible: bool = False,
        wait_error: Exception | None = None,
    ) -> None:
        self._count = count
        self._visible = visible
        self._wait_error = wait_error
        self.click_timeouts: List[int] = []

    @property
    def first(self) -> "FakeLocator":
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._visible

    def click(self, *, timeout: int) -> None:
        self.click_timeouts.append(timeout)

    def wait_for(self, *, timeout: int) -> None:
        if self._wait_error is not None:
            raise self._wait_error


class FakePage:
    def __init__(
        self,
        cards: Sequence[FakeProviderCard] = (),
        empty_selectors: Sequence[str] = (),
        locators: dict[str, FakeLocator] | None = None,
        wait_error: Exception | None = None,
    ) -> None:
        self.cards = list(cards)
        self.empty_selectors = set(empty_selectors)
        self.locators = locators or {}
        self.wait_error = wait_error
        self.closed = False
        self.wait_timeouts: List[int] = []
        self.url = ""
        self.html = "<html><body>challenge</body></html>"
        self.screenshot_paths: List[str] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.url = url

    def content(self) -> str:
        return self.html

    def screenshot(self, *, path: str) -> None:
        self.screenshot_paths.append(path)
        Path(path).write_bytes(b"png")

    def locator(self, selector: str) -> FakeLocator:
        if selector in self.locators:
            return self.locators[selector]
        result_selector = ", ".join((PROPERTY_CARD_SELECTOR,) + EMPTY_STATE_SELECTORS)
        if selector == result_selector:
            return FakeLocator(count=1, visible=True, wait_error=self.wait_error)
        visible = selector in self.empty_selectors
        return FakeLocator(count=int(visible), visible=visible)

    def query_selector_all(self, selector: str) -> List[FakeProviderCard]:
        return self.cards if selector == PROPERTY_CARD_SELECTOR else []

    def wait_for_timeout(self, timeout: int) -> None:
        self.wait_timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.reset_calls = 0
        self.close_calls = 0

    def new_page(self) -> FakePage:
        return self.page

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1


def query(**overrides: object) -> HotelQuery:
    values = {
        "location": "Lisboa",
        "check_in": date(2026, 12, 4),
        "check_out": date(2026, 12, 8),
        "adults": 2,
        "rooms": 1,
        "min_rating": None,
        "entire_home": False,
        "free_cancellation": True,
    }
    values.update(overrides)
    return HotelQuery(**values)


def provider_card(
    *,
    title: str | None = "Casa Azul",
    total_price: str | None = "400 €",
    rating: str | None = "8,7 Fabuloso\n1.234 comentarios",
    address: str | None = "Centro, Lisboa",
    link: str | None = "/hotel/pt/casa-azul.html?aid=123#reviews",
    details: str = "Free cancellation · Entire home",
) -> FakeProviderCard:
    elements = {}
    if title is not None:
        elements['[data-testid="title"]'] = FakeElement(title)
    if total_price is not None:
        elements['[data-testid="price-and-discounted-price"]'] = FakeElement(total_price)
    if rating is not None:
        elements['[data-testid="review-score"]'] = FakeElement(rating)
    if address is not None:
        elements['[data-testid="address-link"]'] = FakeElement(address)
    if link is not None:
        elements['a[data-testid="title-link"]'] = FakeElement("", href=link)
    return FakeProviderCard(elements, details)


class AppliedFiltersTests(unittest.TestCase):
    def test_default_url_and_free_cancellation_chip(self) -> None:
        applied = build_applied_filters(query())
        params = parse_qs(urlparse(applied.url).query)

        self.assertEqual(applied.chips, ("oos=1",))
        self.assertEqual(
            params,
            {
                "ss": ["Lisboa"],
                "checkin": ["2026-12-04"],
                "checkout": ["2026-12-08"],
                "group_adults": ["2"],
                "no_rooms": ["1"],
                "group_children": ["0"],
                "selected_currency": ["EUR"],
                "lang": ["en"],
                "order": ["price"],
                "nflt": ["oos=1"],
            },
        )

    def test_non_refundable_opt_in_has_no_filter_chip(self) -> None:
        applied = build_applied_filters(query(free_cancellation=False))

        self.assertEqual(applied.chips, ())
        params = parse_qs(urlparse(applied.url).query)
        self.assertNotIn("nflt", params)
        self.assertEqual(params["order"], ["price"])

    def test_entire_home_uses_exact_chips(self) -> None:
        applied = build_applied_filters(query(entire_home=True))

        self.assertEqual(
            applied.chips,
            ("oos=1", "privacy_type=3", "ht_id=201"),
        )
        self.assertEqual(
            parse_qs(urlparse(applied.url).query)["nflt"],
            ["oos=1;privacy_type=3;ht_id=201"],
        )

    def test_applied_filters_follow_lang_and_currency_args(self) -> None:
        applied = build_applied_filters(query(), html_lang="en", currency="USD")
        params = parse_qs(urlparse(applied.url).query)
        self.assertEqual(params["lang"], ["en"])
        self.assertEqual(params["selected_currency"], ["USD"])


class BookingHotelsSourceTests(unittest.TestCase):
    def source_for(self, page: FakePage, state_dir: Path) -> BookingHotelsSource:
        return BookingHotelsSource(state_dir, session=FakeSession(page))

    def test_default_session_uses_booking_browser_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = BookingHotelsSource(Path(tmp))
            config = source.config
            self.assertEqual(config.state_filename, "pw_state_booking.json")
            self.assertEqual(config.locale, "en-US")
            self.assertEqual(config.html_lang, "en")
            self.assertEqual(config.currency, "EUR")
            self.assertEqual(config.viewport, {"width": 1280, "height": 900})
            self.assertIsNone(config.user_agent)
            self.assertEqual(config.blocked_types(), BOOKING_BLOCKED_RESOURCE_TYPES)
            self.assertNotIn("font", config.blocked_types())
            self.assertIs(source._session._config, config)

    def test_genius_signin_popup_is_dismissed(self) -> None:
        button = FakeLocator(count=1, visible=True)
        page = FakePage(locators={DISMISS_SELECTORS[0]: button})

        BookingHotelsSource._dismiss_overlays(page)

        self.assertEqual(button.click_timeouts, [3_000])
        self.assertEqual(page.wait_timeouts, [800])

    def test_visible_consent_button_is_clicked(self) -> None:
        button = FakeLocator(count=1, visible=True)
        page = FakePage(locators={CONSENT_SELECTORS[0]: button})

        BookingHotelsSource._dismiss_overlays(page)

        self.assertEqual(button.click_timeouts, [3_000])
        self.assertEqual(page.wait_timeouts, [800])

    def test_selector_timeout_closes_page_and_propagates(self) -> None:
        page = FakePage(wait_error=RuntimeError("selector timeout"))

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            source = self.source_for(page, state_dir)
            with self.assertRaises(BookingResultsTimeout):
                source.fetch(query(), build_applied_filters(query()), 24)
            self.assertTrue((state_dir / FAILURE_HTML_NAME).exists())
            meta = (state_dir / FAILURE_META_NAME).read_text(encoding="utf-8")
            self.assertIn("url:", meta)

        self.assertTrue(page.closed)

    def test_recognized_empty_state_returns_empty_page_and_closes_page(self) -> None:
        page = FakePage(empty_selectors=(EMPTY_STATE_SELECTORS[0],))

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            result = source.fetch(query(), build_applied_filters(query()), 24)

        self.assertEqual(result.cards, ())
        self.assertIsInstance(result, HotelPage)
        self.assertTrue(page.closed)

    def test_unrecognized_no_card_state_raises_and_closes_page(self) -> None:
        page = FakePage()

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            with self.assertRaises(RuntimeError):
                source.fetch(query(), build_applied_filters(query()), 24)
            self.assertTrue((Path(tmp) / FAILURE_HTML_NAME).exists())

        self.assertTrue(page.closed)

    def test_malformed_cards_are_skipped_while_good_cards_survive(self) -> None:
        page = FakePage(cards=(provider_card(title=None), provider_card(title="Good")))

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            result = source.fetch(query(), build_applied_filters(query()), 24)

        self.assertEqual([row.title for row in result.cards], ["Good"])
        self.assertTrue(page.closed)

    def test_fetch_honors_limit(self) -> None:
        page = FakePage(
            cards=(
                provider_card(title="One"),
                provider_card(title="Two"),
                provider_card(title="Three"),
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            result = source.fetch(query(), build_applied_filters(query()), 2)

        self.assertEqual([row.title for row in result.cards], ["One", "Two"])

    def test_fetch_cleans_relative_link_and_keeps_rating_first_line(self) -> None:
        page = FakePage(cards=(provider_card(),))

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            result = source.fetch(query(), build_applied_filters(query()), 24)

        extracted = result.cards[0]
        self.assertEqual(extracted.rating, "8,7 Fabuloso")
        self.assertEqual(
            extracted.link,
            "https://www.booking.com/hotel/pt/casa-azul.html",
        )
        self.assertTrue(page.closed)

    def test_blank_price_crosses_the_card_boundary(self) -> None:
        page = FakePage(cards=(provider_card(total_price=None),))

        with tempfile.TemporaryDirectory() as tmp:
            source = self.source_for(page, Path(tmp))
            result = source.fetch(query(), build_applied_filters(query()), 24)

        self.assertEqual(len(result.cards), 1)
        self.assertEqual(result.cards[0].total_price, "")
        self.assertTrue(page.closed)

    def test_close_and_reset_delegate_to_session(self) -> None:
        session = FakeSession(FakePage())
        with tempfile.TemporaryDirectory() as tmp:
            source = BookingHotelsSource(Path(tmp), session=session)
            source.reset()
            source.close()
        self.assertEqual(session.reset_calls, 1)
        self.assertEqual(session.close_calls, 1)

    def test_module_has_no_city_specific_or_private_policy(self) -> None:
        source = Path(booking_module.__file__).read_text(encoding="utf-8").casefold()

        for forbidden in ("praga", "prague", "median", "suburb", "tram"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
