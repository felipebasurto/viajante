"""Browser detail search and the CSS card parser: the Playwright path behind fetch=detail."""

from __future__ import annotations

import contextlib
import re
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Optional, Sequence

from selectolax.lexbor import LexborHTMLParser

from viajante.browser import BrowserSessionConfig, ChromiumSession
from viajante.control import (
    SearchDeadline,
    checkpoint,
)
from viajante.google_flights import (
    SCRAPE_LANGUAGE,
    GoogleFlightsBlocked,
    GoogleFlightsMarkupError,
    NoFlightsFound,
    build_search_url,
    looks_blocked,
)
from viajante.google_flights_rpc import (
    RawFlightCard,
)
from viajante.models import (
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

STATE_FILENAME = "pw_state_google.json"


PAGE_TIMEOUT_MS = 60_000


CONSENT_CLICK_TIMEOUT_MS = 5_000


CONSENT_SETTLE_MS = 1_500


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


# Multi-city boards render their rows without the results container, so a board
# is ready when either the container, the empty state, or a row is attached.
BOARD_READY_SELECTOR = f"{READY_SELECTOR}, {CARD_SELECTOR}"


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


_DETAIL_UNPROVEN = (
    "bags",
    "carry_on",
    "airlines",
    "exclude_airlines",
    "alliances",
    "exclude_alliances",
)


class GoogleFlightsSource:
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
        # Inline: google_flights_public imports this module, so a top-level import would cycle.
        from viajante.google_flights_public import GoogleFlightsUnsupported, _validate_capabilities

        # Detail reads no page echo, so a bag or carrier filter it sends stays unproven.
        unproven = [key for key in _DETAIL_UNPROVEN if getattr(trip, key, None) is not None]
        if unproven:
            raise GoogleFlightsUnsupported(
                "Not sent. Browser detail cannot verify these requested capabilities: "
                + ", ".join(unproven)
                + ". The public-page sweep verifies carry-on and airline or alliance includes."
            )
        # The public page bootstraps no multi-city results; the browser UI renders them.
        _validate_capabilities(trip, allow_multi_city=True)
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
        # Inline import, same cycle as in fetch().
        from viajante.google_flights_public import _matches_leg

        page.locator(BOARD_READY_SELECTOR).first.wait_for(timeout=PAGE_TIMEOUT_MS)
        checkpoint()
        html = page.evaluate("() => document.querySelector('[role=\"main\"]')?.innerHTML || ''")
        matched: list[tuple[int, RawFlightCard]] = []
        parser = LexborHTMLParser(html)
        for position, item in enumerate(parser.css(CARD_SELECTOR)):
            card = _extract_card(item)
            if card is not None and _matches_leg(card, leg):
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
            page.locator(BOARD_READY_SELECTOR).first.wait_for(timeout=PAGE_TIMEOUT_MS)
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
