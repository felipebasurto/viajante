"""Google Flights public-page transport, without unsigned shopping RPC calls."""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import replace
from datetime import date, timedelta
from typing import Callable, Sequence
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser

from viajante.airports import same_city_iata
from viajante.carriers import ALLIANCE_TFS_CODE, _airline_filter_hit
from viajante.control import SearchDeadline, checkpoint
from viajante.google_flights import (
    CONSENT_SETTLE_MS,
    PAGE_TIMEOUT_MS,
    RPC_THROTTLE_STATUS,
    SEARCH_URL,
    STATE_FILENAME,
    SWEEP_TRANSPORT_STATUS,
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    NoFlightsFound,
    SweepHttpResponse,
    SweepTransportError,
    _retry_after_seconds,
    build_search_params,
    looks_blocked,
    raise_for_sweep_response,
    response_diagnostics,
)
from viajante.google_flights_page import extract_ds1_data, parse_shopping_page
from viajante.google_flights_rpc import (
    CompactCalendarDay,
    CompactExplorePlace,
    CompactParseMiss,
    EmptyShoppingResults,
    RawFlightCard,
    explore_request_constraints,
    explore_request_echo,
    parse_explore_catalog,
    raw_rpc_error_status,
)
from viajante.models import FlightCabin, FlightQuery, MultiCity, RawJourneyLeg, RoundTrip, Trip
from viajante.parsers import parse_price
from viajante.ratelimit import note_rate_limited, rate_limit_advice, rate_limit_status
from viajante.storage import default_state_dir
from viajante.sweep_config import get_sweep_config
from viajante.tfs import CABIN_SEAT, encode_explore_tfs, encode_tfs_selected_outbound

MAX_PUBLIC_OUTBOUNDS = 8


def _ranked_outbounds(cards: Sequence[RawFlightCard]) -> tuple[RawFlightCard, ...]:
    ranked = sorted(cards, key=lambda card: parse_price(card.price) or float("inf"))
    return tuple(ranked[:MAX_PUBLIC_OUTBOUNDS])


def _cached_return_reader(
    outcomes: Sequence[object],
) -> Callable[[RoundTrip, RawFlightCard], object]:
    """Return pages already fetched, in the order ``_round_trip_packages`` asks for them."""
    pending = iter(outcomes)

    def read_return(_trip: RoundTrip, _outbound: RawFlightCard) -> object:
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return read_return


def _response_halts(response: SweepHttpResponse) -> bool:
    """A provider block: do not retry this batch or start another one.

    Status 599 is a transport failure, including pages the client never sent
    because an earlier request in the same batch raised. Those are replayed
    once. A 429, 403, or unsent page after a real block is not.
    """
    if response.deadline or response.status == SWEEP_TRANSPORT_STATUS:
        return False
    if response.stopped and not response.request_sent:
        return True
    if response.status in {403, 429}:
        return True
    if raw_rpc_error_status(response.text) == RPC_THROTTLE_STATUS:
        return True
    return looks_blocked(response.text, response.url)


def _response_retriable(response: SweepHttpResponse) -> bool:
    """Once-retry, matching ``_read_page_retry``: transport failures and HTTP 5xx only.

    A transport failure is retriable even when this page was not sent: the
    client stopped the rest of the batch after the first one raised.
    """
    if response.deadline:
        return False
    if response.status == SWEEP_TRANSPORT_STATUS:
        return True
    if response.stopped or not response.request_sent:
        return False
    return response.status >= 500


EXPLORE_URL = "https://www.google.com/travel/explore"
_EXPLORE_CATALOG_SERVICE = "GetExploreDestinations"
_EXPLORE_CATALOG_WAIT_MS = 45_000
_EXPLORE_OCCUPANCY = [1, 0, 0, 0]


class GoogleFlightsUnsupported(GoogleFlightsRejected):
    """A requested capability cannot be represented by this transport."""


def _carrier_catalog_tokens(data: object) -> set[str] | None:
    """Tokens listed in the page's airline filter catalog (ds:1 ``data[7][1][1]``).

    The public page injects each applied carrier filter token (airline code or
    alliance enum) as an extra ``[code, name]`` row in this list; that is the
    provider-owned echo proving the filter was registered.
    """
    if not isinstance(data, list) or len(data) <= 7:
        return None
    try:
        rows = data[7][1][1]
    except (TypeError, IndexError, KeyError):
        return None
    if not isinstance(rows, list):
        return None
    return {row[0] for row in rows if isinstance(row, list) and row and isinstance(row[0], str)}


class PublicGoogleFlightsHttpSource(GoogleFlightsHttpSource):
    """Read server-rendered ds:1 data; complete RTs with owned outbound selections."""

    automatic_typical = False
    transport = "public_page"

    def __init__(self, **kwargs) -> None:
        get_sweep_config()  # Validate even with an injected client, before network.
        super().__init__(**kwargs)
        self.partial_error: BaseException | None = None
        self.scope_bound = False
        self._query_meta: dict[Trip, tuple[BaseException | None, bool]] = {}
        self._stopped_error: GoogleFlightsBlocked | None = None
        self._explore_browser = None

    def _validate_capabilities(self, trip: Trip, *, allow_multi_city: bool = False) -> None:
        unsupported = [
            key
            for key in (
                "bags",
                "exclude_alliances",
            )
            if getattr(trip, key, None) is not None
        ]
        # A positive carry-on rides the tfs BaggageFilter and is verified by the
        # page's Bags chip echo. Checked bags produce no provider echo on this
        # transport, and a named 0 is the unselected state (no echo either);
        # both stay refused like an unverifiable filter.
        if trip.carry_on == 0:
            unsupported.append("carry_on")
        if isinstance(trip, MultiCity) and not allow_multi_city:
            unsupported.append("multi_city")
        if unsupported:
            reasons = []
            if "bags" in unsupported or "carry_on" in unsupported:
                reasons.append("baggage-inclusive fares remain unknown")
            if "exclude_alliances" in unsupported:
                reasons.append(
                    "alliance exclusion is not verifiable: the public page "
                    "registers the filter but does not remove every "
                    "alliance-marketed itinerary"
                )
            if "multi_city" in unsupported:
                reasons.append(
                    "the public page returns no multi-city results; request an explicit "
                    "browser detail search (--fetch detail) for multi-city packages"
                )
            message = (
                "Not sent. Google Flights public-page transport cannot verify "
                "these requested capabilities: "
            ) + ", ".join(unsupported)
            message += ". Remove them only for an explicitly separate scenario"
            if reasons:
                message += "; " + "; ".join(reasons)
            raise GoogleFlightsUnsupported(message + ".")

    def _url(self, trip: Trip, outbound: RawJourneyLeg | None = None) -> str:
        params = build_search_params(
            trip, html_lang=self._html_lang, currency=self._currency, country=self._country
        )
        if outbound is not None:
            if not isinstance(trip, RoundTrip):
                raise GoogleFlightsUnsupported("Selected outbound requires a round-trip query.")
            params["tfs"] = encode_tfs_selected_outbound(trip, outbound)
        return SEARCH_URL + "?" + urlencode(params)

    def _check_stopped(self, endpoint: str) -> None:
        if self._stopped_error is not None:
            error = self._stopped_error
            raise GoogleFlightsBlocked(
                "Not sent. Remaining search stopped after a provider block.",
                status=error.status,
                diagnostics={
                    "http_status": None,
                    "rpc_status": None,
                    "request_sent": False,
                    "attempts": 0,
                    "endpoint": endpoint,
                    "cooldown_basis": (error.diagnostics or {}).get("cooldown_basis"),
                },
            )

    def _ensure_client(self):
        self._check_stopped("www.google.com/travel/flights")
        return super()._ensure_client()

    def _parse_response(
        self, response: SweepHttpResponse, url: str, trip: Trip | None = None
    ) -> tuple[RawFlightCard, ...]:
        try:
            raise_for_sweep_response(response, url)
        except GoogleFlightsBlocked as exc:
            if exc.status in {None, 403, 429}:
                self._stopped_error = exc
            raise
        if trip is not None:
            self._verify_context(response.text, trip)
        try:
            cards = parse_shopping_page(response.text, currency=self._currency)
        except EmptyShoppingResults as exc:
            if trip is not None:
                self._verify_carrier_filters(response.text, (), trip)
            raise NoFlightsFound() from exc
        except CompactParseMiss as exc:
            # Missing bootstrap data cannot prove the provider returned no flights.
            raise GoogleFlightsMarkupError(str(exc)) from exc
        if trip is not None:
            self._verify_carrier_filters(response.text, cards, trip)
        return cards

    def _verify_context(self, html: str, trip: Trip) -> None:
        root = LexborHTMLParser(html)
        labels = {node.attributes.get("aria-label") for node in root.css("button[aria-label]")}
        if f"Currency {self._currency}" not in labels:
            raise GoogleFlightsMarkupError("Public page did not echo the requested quote currency.")
        occupancy = {
            "Number of adult passengers": trip.adults,
            "Number of children aged 2 to 11": trip.children,
            "Number of infants in their own seat": trip.infants_in_seat,
            "Number of infants on lap": trip.infants_on_lap,
        }
        for label, expected in occupancy.items():
            node = root.css_first(f'[aria-label="{label}"] [aria-live="polite"]')
            if node is None or node.text(strip=True) != str(expected):
                raise GoogleFlightsMarkupError(
                    "Public page did not echo the requested passenger occupancy."
                )
        # Some markets label the default economy cabin "Economy (include Basic)";
        # "exclude Basic" is a fare filter nobody asked for, so it does not match.
        cabins = {
            "economy": {"Economy", "Economy (include Basic)"},
            "premium-economy": {"Premium economy"},
            "business": {"Business"},
            "first": {"First"},
        }[trip.cabin]
        if not cabins & {node.text(strip=True) for node in root.css('[role="combobox"]')}:
            raise GoogleFlightsMarkupError("Public page did not echo the requested cabin.")
        for count, kind in ((trip.carry_on, "carry-on"), (trip.bags, "checked")):
            if not count:
                continue
            chip = rf"(?<!\d){count} {kind} bags?"
            if not any(
                label and re.search(chip, label) and "Bags, Selected" in label for label in labels
            ):
                raise GoogleFlightsMarkupError(
                    "Public page did not echo the requested baggage filter."
                )
        if (
            trip.airlines or trip.exclude_airlines or trip.alliances or trip.exclude_alliances
        ) and "Airlines, Selected" not in labels:
            raise GoogleFlightsMarkupError("Public page did not echo the requested airline filter.")

    def _verify_carrier_filters(
        self, html: str, cards: tuple[RawFlightCard, ...], trip: Trip
    ) -> None:
        # A unioned include (airlines + alliances) can qualify through members
        # we cannot check; a pure airline include must hold on every card.
        if trip.airlines and not trip.alliances:
            for card in cards:
                if not any(_airline_filter_hit(card, token) for token in trip.airlines):
                    raise GoogleFlightsMarkupError(
                        "Public page results do not satisfy the requested airline filter."
                    )
        # The page echoes each selected alliance as an extra row in the airline
        # filter catalog; a missing row means the filter was not registered.
        if trip.alliances:
            try:
                data = extract_ds1_data(html)
            except CompactParseMiss:
                data = None
            catalog = _carrier_catalog_tokens(data)
            tokens = {
                ALLIANCE_TFS_CODE[name] for name in trip.alliances if name in ALLIANCE_TFS_CODE
            }
            if catalog is None or not tokens <= catalog:
                raise GoogleFlightsMarkupError(
                    "Public page did not echo the requested alliance filter."
                )

    @staticmethod
    def _matches_leg(card: RawFlightCard, query) -> bool:
        if len(card.legs) != 1 or not card.legs[0].segments:
            return False
        segments = card.legs[0].segments
        return (
            segments[0].origin == query.origin
            and segments[-1].destination == query.destination
            and segments[0].departure_date == query.departure_date
            and len(segments) - 1 <= query.max_stops
        )

    def _read_page(self, trip: Trip, outbound: RawJourneyLeg | None = None):
        checkpoint()
        url = self._url(trip, outbound)
        try:
            response = self._ensure_client().get(url, timeout=self._timeout)
        except (GoogleFlightsBlocked, SearchDeadline):
            raise
        except Exception as exc:
            raise SweepTransportError(
                f"{type(exc).__name__}: public-page transport failed before a response",
                timeout=isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.casefold(),
            ) from exc
        cards = self._parse_response(response, url, trip)
        return cards, response.text

    @staticmethod
    def _selected_echo(html: str, outbound: RawJourneyLeg) -> bool:
        """Require the visible selected journey, not just return rows with a price."""
        root = LexborHTMLParser(html)
        if root.body is None:
            return False
        text = " ".join(root.body.text(separator=" ", strip=True).split())
        chosen = text.find("Choose return")
        if chosen < 0:
            return False
        selected_header = text[max(0, chosen - 700) : chosen]
        segment = outbound.segments[0]
        # Google's selected header carries route and outbound local clock.
        clock = outbound.departure or segment.departure
        if not clock:
            return False
        hour, minute = map(int, clock.split(":"))
        english_clock = f"{hour % 12 or 12}:{minute:02d} {'AM' if hour < 12 else 'PM'}"
        route = f"{segment.origin}–{outbound.segments[-1].destination}"
        return (
            route in selected_header
            and english_clock in selected_header
            and "Choose return" in text
            and "round trip" in text
        )

    def fetch(self, trip: Trip) -> tuple[RawFlightCard, ...]:
        self._validate_capabilities(trip)
        self.partial_error = None
        self.scope_bound = False
        cards, _html = self._read_page_retry(trip)
        matched = tuple(card for card in cards if self._matches_leg(card, trip.legs[0]))
        if not matched:
            raise GoogleFlightsMarkupError(
                "Public-page rows did not prove the requested route, date and stops."
            )
        if not isinstance(trip, RoundTrip):
            return matched
        client = self._round_trip_batch_client()
        if client is None:
            return self._round_trip_packages(trip, matched, self._read_selected_return)
        slots, _halt, _client = self._return_slots(client, trip, matched)
        return self._round_trip_packages(trip, matched, _cached_return_reader(slots))

    def _read_selected_return(
        self, trip: RoundTrip, outbound: RawFlightCard
    ) -> tuple[tuple[RawFlightCard, ...], str]:
        checkpoint()
        return self._read_page_retry(trip, outbound.legs[0])

    def _round_trip_packages(
        self,
        trip: RoundTrip,
        outbound_cards: Sequence[RawFlightCard],
        read_return: Callable[[RoundTrip, RawFlightCard], object],
    ) -> tuple[RawFlightCard, ...]:
        """Join each selected outbound with its return page. Outbound amounts are not fares."""
        self.partial_error = None
        self.scope_bound = True  # A bounded outbound board does not cover all packages.
        complete: list[RawFlightCard] = []
        for outbound in _ranked_outbounds(outbound_cards):
            try:
                parsed = read_return(trip, outbound)
                returns, html = parsed
                if not self._selected_echo(html, outbound.legs[0]):
                    raise GoogleFlightsMarkupError(
                        "Selected outbound echo was not proven on the return page."
                    )
                for returned in returns:
                    if self._matches_leg(returned, trip.legs[1]):
                        complete.append(
                            replace(
                                outbound,
                                price=returned.price,
                                booking_token=returned.booking_token,
                                legs=(outbound.legs[0], returned.legs[0]),
                                flight_numbers=tuple(
                                    (outbound.flight_numbers or ())
                                    + (returned.flight_numbers or ())
                                ),
                            )
                        )
            except SearchDeadline:
                if not complete:
                    raise
                self.partial_error = SearchDeadline()
                break
            except Exception as exc:
                self.partial_error = exc
                if isinstance(exc, GoogleFlightsBlocked):
                    break
        if not complete:
            if self.partial_error is not None and not isinstance(
                self.partial_error, NoFlightsFound
            ):
                raise self.partial_error
            raise GoogleFlightsMarkupError(
                "No complete round-trip package was proved by the public pages."
            )
        self._query_meta[trip] = (self.partial_error, self.scope_bound)
        return tuple(complete)

    def metadata_for(self, trip: Trip):
        return self._query_meta.get(trip, (None, False))

    def _read_page_retry(self, trip: Trip, outbound: RawJourneyLeg | None = None):
        try:
            return self._read_page(trip, outbound)
        except SweepTransportError:
            self.reset()
        except GoogleFlightsBlocked as exc:
            if exc.status is None or exc.status < 500:
                raise
        self._sleep(0.05)
        try:
            return self._read_page(trip, outbound)
        except Exception as exc:
            diagnostics = getattr(exc, "diagnostics", None)
            if isinstance(diagnostics, dict):
                exc.diagnostics = {**diagnostics, "attempts": 2}
            raise

    def fetch_with_calendar(self, query, start, end):
        return self.fetch(query), ()  # No hidden 31-day fanout for an optional typical.

    def fetch_many_with_calendar(self, jobs):
        return [(result, ()) for result in self.fetch_many([query for query, _, _ in jobs])]

    def _round_trip_batch_client(self):
        """Sweep client when it can multiplex GETs. Otherwise the serial loop stays."""
        try:
            client = self._ensure_client()
        except GoogleFlightsBlocked:
            return None
        if not callable(getattr(client, "get_many", None)):
            return None
        return client

    def _get_pages(self, client, urls: Sequence[str]):
        """One multiplexed GET, plus the same once-retry ``_read_page_retry`` uses.

        The client is returned because a transport retry may have replaced it.
        """
        if not urls:
            return [], client
        checkpoint()
        getter = client.get_many
        first = list(getter(urls, timeout=self._timeout))
        if len(first) != len(urls):
            raise GoogleFlightsMarkupError(
                "public-page batch returned the wrong number of responses"
            )
        if any(response.deadline or _response_halts(response) for response in first):
            return first, client
        retry = [index for index, response in enumerate(first) if _response_retriable(response)]
        if not retry:
            return first, client
        if any(first[index].status == SWEEP_TRANSPORT_STATUS for index in retry):
            self.reset()
            client = self._ensure_client()
            getter = getattr(client, "get_many", None)
            if not callable(getter):
                return first, client
        try:
            self._sleep(0.05)
            checkpoint()
            retried = list(getter([urls[index] for index in retry], timeout=self._timeout))
        except SearchDeadline:
            return first, client
        if len(retried) != len(retry):
            raise GoogleFlightsMarkupError(
                "public-page batch returned the wrong number of responses"
            )
        out = list(first)
        for slot, response in zip(retry, retried, strict=True):
            # An unsent sibling's replay is its first send. Only a page that
            # was sent, failed, and was sent again is attempt 2.
            already_sent = first[slot].request_sent and response.request_sent
            out[slot] = replace(response, attempts=2) if already_sent else response
        return out, client

    @staticmethod
    def _not_sent_block(halt: GoogleFlightsBlocked) -> GoogleFlightsBlocked:
        return GoogleFlightsBlocked(
            "Not sent. Remaining search stopped after a provider block.",
            status=halt.status,
            diagnostics={
                "http_status": None,
                "rpc_status": None,
                "request_sent": False,
                "attempts": 0,
                "endpoint": "www.google.com/travel/flights",
                "cooldown_basis": (halt.diagnostics or {}).get("cooldown_basis"),
            },
        )

    @staticmethod
    def _stopped_after_outbound(halt: GoogleFlightsBlocked) -> GoogleFlightsBlocked:
        """The outbound page was sent; its return pages were not."""
        diagnostics = halt.diagnostics or {}
        return GoogleFlightsBlocked(
            "Outbound page arrived. Return pages were not sent after a provider block.",
            status=halt.status,
            diagnostics={
                "http_status": halt.status,
                "rpc_status": diagnostics.get("rpc_status"),
                "request_sent": True,
                "attempts": 1,
                "endpoint": "www.google.com/travel/flights",
                "cooldown_basis": diagnostics.get("cooldown_basis"),
            },
        )

    def _fetch_many_round_trips(self, client, trips: Sequence[RoundTrip]):
        results: list = [None] * len(trips)
        try:
            self._fill_round_trip_batch(client, trips, results, None)
        except SearchDeadline as exc:
            for index, item in enumerate(results):
                if item is None:
                    results[index] = exc
        return results

    def fetch_round_trip_window(self, trips: Sequence[RoundTrip], on_day) -> None:
        """Bounded round-trip date window. ``on_day`` runs once a day has a result.

        A deadline raised before a day starts leaves that day and the rest to the caller.
        """
        client = self._round_trip_batch_client()
        if client is None:
            self._serial_round_trip_window(trips, on_day)
            return
        self._fill_round_trip_batch(client, trips, [None] * len(trips), on_day)

    def _serial_round_trip_window(self, trips: Sequence[RoundTrip], on_day) -> None:
        for index, trip in enumerate(trips):
            checkpoint()
            try:
                result: object = self.fetch(trip)
            except SearchDeadline:
                raise
            except Exception as exc:
                result = exc
            on_day(index, result)

    def _fill_round_trip_batch(self, client, trips: Sequence[RoundTrip], results: list, on_day):
        # ponytail: chunks of sweep-concurrency days — one outbound get_many, then
        # one get_many per day for that day's returns (at most 8). A 31-day window
        # at 8 streams is ceil(31/8) + 31 = 35 waves. A short day does not share a
        # return wave, and a 429 keeps only chunks that already finished.
        # Upgrade: pack a short day's returns into a wave that still has room.
        chunk_size = get_sweep_config().concurrency
        for index, trip in enumerate(trips):
            try:
                self._validate_capabilities(trip)
            except Exception as exc:
                results[index] = exc
        halt: GoogleFlightsBlocked | None = None
        for start in range(0, len(trips), chunk_size):
            chunk = list(range(start, min(len(trips), start + chunk_size)))
            if halt is not None:
                for index in chunk:
                    if results[index] is None:
                        results[index] = self._not_sent_block(halt)
                    if on_day is not None:
                        on_day(index, results[index])
                continue
            boards: dict[int, tuple[RawFlightCard, ...]] = {}
            outbound = [index for index in chunk if results[index] is None]
            urls = [self._url(trips[index]) for index in outbound]
            responses, client = self._get_pages(client, urls)
            for index, url, response in zip(outbound, urls, responses, strict=True):
                if halt is not None:
                    results[index] = (
                        self._stopped_after_outbound(halt)
                        if response.request_sent
                        else self._not_sent_block(halt)
                    )
                    continue
                try:
                    cards = self._parse_response(response, url, trips[index])
                    matched = tuple(
                        card for card in cards if self._matches_leg(card, trips[index].legs[0])
                    )
                    if not matched:
                        raise GoogleFlightsMarkupError(
                            "Public-page rows did not prove the requested route, date and stops."
                        )
                    boards[index] = matched
                except SearchDeadline as exc:
                    results[index] = exc
                except Exception as exc:
                    results[index] = exc
                    if isinstance(exc, GoogleFlightsBlocked) and exc.status in {None, 403, 429}:
                        halt = exc
            if halt is not None:
                for index in chunk:
                    if results[index] is None:
                        arrived = index in boards
                        results[index] = (
                            self._stopped_after_outbound(halt)
                            if arrived
                            else self._not_sent_block(halt)
                        )
                    if on_day is not None:
                        on_day(index, results[index])
                continue
            for index in chunk:
                if results[index] is not None:
                    if on_day is not None:
                        on_day(index, results[index])
                    continue
                if halt is not None:
                    results[index] = self._stopped_after_outbound(halt)
                    if on_day is not None:
                        on_day(index, results[index])
                    continue
                matched = boards[index]
                slots, day_halt, client = self._return_slots(client, trips[index], matched)
                try:
                    results[index] = self._round_trip_packages(
                        trips[index], matched, _cached_return_reader(slots)
                    )
                except SearchDeadline as exc:
                    results[index] = exc
                except Exception as exc:
                    results[index] = exc
                if on_day is not None:
                    on_day(index, results[index])
                if day_halt is not None:
                    halt = day_halt

    def _return_slots(self, client, trip: RoundTrip, matched: Sequence[RawFlightCard]):
        slots: list = []
        jobs: list[tuple[int, str]] = []
        for slot_index, card in enumerate(_ranked_outbounds(matched)):
            try:
                url = self._url(trip, card.legs[0])
            except Exception as exc:
                slots.append(exc)
            else:
                slots.append(None)
                jobs.append((slot_index, url))
        halt: GoogleFlightsBlocked | None = None
        if not jobs:
            return slots, halt, client
        returned, client = self._get_pages(client, [url for _slot, url in jobs])
        for (slot_index, url), response in zip(jobs, returned, strict=True):
            try:
                cards = self._parse_response(response, url, trip)
            except Exception as exc:
                slots[slot_index] = exc
                if (
                    halt is None
                    and isinstance(exc, GoogleFlightsBlocked)
                    and exc.status in {None, 403, 429}
                ):
                    halt = exc
            else:
                slots[slot_index] = (cards, response.text)
        return slots, halt, client

    def fetch_many(self, trips: Sequence[Trip]):
        # The HTTP client multiplexes GETs at the configured concurrency for OWs.
        if not trips:
            return []
        if all(isinstance(trip, RoundTrip) for trip in trips):
            client = self._round_trip_batch_client()
            if client is not None:
                return self._fetch_many_round_trips(client, trips)
        if any(not isinstance(trip, FlightQuery) for trip in trips):
            results = []
            for index, trip in enumerate(trips):
                try:
                    results.append(self.fetch(trip))
                except SearchDeadline as exc:
                    results.extend([exc] * (len(trips) - index))
                    break
                except Exception as exc:
                    results.append(exc)
            return results
        results: list[object] = [None] * len(trips)
        pending = []
        for index, trip in enumerate(trips):
            try:
                self._validate_capabilities(trip)
                pending.append(index)
            except Exception as exc:
                results[index] = exc
        if not pending:
            return results
        try:
            client = self._ensure_client()
        except Exception as exc:
            for index in pending:
                results[index] = exc
            return results
        urls = [self._url(trips[index]) for index in pending]
        getter = getattr(client, "get_many", None)
        responses = (
            getter(urls, timeout=self._timeout)
            if callable(getter)
            else self._serial_gets(client, urls)
        )
        for index, url, response in zip(pending, urls, responses, strict=True):
            try:
                cards = self._parse_response(response, url, trips[index])
                if not all(self._matches_leg(card, trips[index].legs[0]) for card in cards):
                    raise GoogleFlightsMarkupError(
                        "Public-page rows did not prove the requested route, date and stops."
                    )
                results[index] = cards
            except SearchDeadline as exc:
                results[index] = exc
            except Exception as exc:
                results[index] = exc
        if not any(
            isinstance(item, GoogleFlightsBlocked) and (item.status in {None, 403, 429})
            for item in results
        ):
            client, replay = self._plan_replay(client, results)
            if replay:
                retry_urls = [self._url(trips[index]) for index in replay]
                getter = getattr(client, "get_many", None)
                retried = (
                    getter(retry_urls, timeout=self._timeout)
                    if callable(getter)
                    else self._serial_gets(client, retry_urls)
                )
                for index, url, response in zip(replay, retry_urls, retried, strict=True):
                    try:
                        cards = self._parse_response(
                            replace(response, attempts=2), url, trips[index]
                        )
                        if not all(self._matches_leg(card, trips[index].legs[0]) for card in cards):
                            raise GoogleFlightsMarkupError(
                                "Public-page rows did not prove the requested "
                                "route, date and stops."
                            )
                        results[index] = cards
                    except SearchDeadline as exc:
                        results[index] = exc
                    except Exception as exc:
                        results[index] = exc
        return results

    def _serial_gets(self, client, urls):
        responses = []
        stopped = False
        for url in urls:
            checkpoint()
            if stopped:
                responses.append(
                    SweepHttpResponse(
                        0,
                        "Not sent. Remaining batch stopped after a provider block.",
                        url,
                        request_sent=False,
                        attempts=0,
                        stopped=True,
                    )
                )
                continue
            try:
                response = client.get(url, timeout=self._timeout)
            except SearchDeadline:
                raise
            except Exception as exc:
                response = SweepHttpResponse(
                    SWEEP_TRANSPORT_STATUS,
                    f"{type(exc).__name__}: transport failed before a response",
                    url,
                )
            responses.append(response)
            stopped = (
                response.status in {403, 429}
                or response.stopped
                or raw_rpc_error_status(response.text) == 13
                or looks_blocked(response.text, response.url)
            )
        return responses

    def fetch_calendar(self, trip: Trip, start: date, end: date):
        self._validate_capabilities(trip)
        if end < start or (end - start).days > 30:
            raise ValueError("public calendar requires an ordered window of at most 31 days")
        queries = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            changes = {"departure_date": day}
            if isinstance(trip, RoundTrip):
                changes["return_date"] = day + (trip.return_date - trip.departure_date)
            queries.append(replace(trip, **changes))
        outcomes = self.fetch_many(queries)
        days = []
        for query, outcome in zip(queries, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                raise outcome  # Unknown is never an unpriced provider-empty cell.
            prices = [parse_price(card.price) for card in outcome]
            owned = [price for price in prices if price is not None]
            days.append(
                CompactCalendarDay(
                    query.departure_date,
                    min(owned) if owned else None,
                    query.return_date if isinstance(query, RoundTrip) else None,
                )
            )
        return tuple(days)

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
        """Catalog via the public Explore page's own browser-issued request.

        The page applies its fixed one-adult economy state; a non-default party
        or cabin cannot be proven applied and is refused before any request.
        """
        occupancy = [adults, children, infants_in_seat, infants_on_lap]
        if occupancy != _EXPLORE_OCCUPANCY or cabin != "economy":
            raise GoogleFlightsUnsupported(
                "Not sent. The public Google Explore catalog prices under the page's "
                "one-adult economy state; requested "
                f"adults={adults}, children={children}, "
                f"infants_in_seat={infants_in_seat}, "
                f"infants_on_lap={infants_on_lap}, cabin={cabin!r} "
                "cannot be proven applied."
            )
        params = {
            "tfs": encode_explore_tfs(origin, departure_date),
            "hl": self._html_lang,
            "curr": self._currency,
        }
        if self._country:
            params["gl"] = self._country
        pairs = self._explore_page(f"{EXPLORE_URL}?{urlencode(params)}")
        checkpoint()
        for _post, body in pairs:
            self._raise_explore_failure(body)
        accepted = frozenset({origin, *same_city_iata(origin)})
        chosen = None
        for post, body in pairs:
            echo = explore_request_echo(explore_request_constraints(post))
            if (
                echo is not None
                and echo.origins
                and echo.origins <= accepted
                and departure_date.isoformat() in echo.dates
            ):
                chosen = (body, echo)
                break
        if chosen is None:
            raise GoogleFlightsMarkupError(
                "Google Explore did not issue a catalog request for the requested "
                "origin and date; the page state was not applied."
            )
        body, echo = chosen
        if echo.cabin != CABIN_SEAT["economy"] or echo.occupancy != _EXPLORE_OCCUPANCY:
            raise GoogleFlightsMarkupError(
                "Google Explore catalog request did not echo the default occupancy."
            )
        catalog = parse_explore_catalog(body, origins=accepted)
        if catalog.origin_echo is not None and catalog.origin_echo not in accepted:
            raise GoogleFlightsMarkupError(
                f"Google Explore catalog echoed origin {catalog.origin_echo}, "
                f"not requested {origin}."
            )
        if catalog.currencies and catalog.currencies != {self._currency}:
            raise GoogleFlightsMarkupError(
                f"Google Explore catalog was not priced in the requested currency {self._currency}."
            )
        if catalog.priced and not catalog.places:
            raise GoogleFlightsMarkupError(
                "Google Explore priced rows did not prove the requested origin."
            )
        return catalog.places

    def _explore_page(self, url: str) -> list[tuple[object, str]]:
        """Load the Explore page in Chromium; return its catalog request/response pairs."""
        checkpoint()
        self._check_stopped("www.google.com/travel/explore")
        state = rate_limit_status()
        if state is not None:
            raise GoogleFlightsBlocked(rate_limit_advice(state, sent=False), status=429)
        page = self._explore_session().new_page()
        captured: list[tuple[object, SweepHttpResponse, float | None]] = []

        def _capture(response) -> None:
            if _EXPLORE_CATALOG_SERVICE not in response.url:
                return
            try:
                body = response.text()
            except SearchDeadline:
                raise
            except Exception:
                body = ""
            post = None
            try:
                post = response.request.post_data
            except SearchDeadline:
                raise
            except Exception:
                post = None
            captured.append(
                (
                    post,
                    SweepHttpResponse(response.status, body, response.url),
                    _retry_after_seconds(response),
                )
            )

        page.on("response", _capture)
        try:
            navigation = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            checkpoint()
            if navigation is not None and navigation.status >= 400:
                self._raise_explore_failure(
                    navigation.text(),
                    response=SweepHttpResponse(navigation.status, "", navigation.url),
                    retry_after_seconds=_retry_after_seconds(navigation),
                )
            if "consent.google" in page.url:
                self._dismiss_consent(page)
            if looks_blocked("", page.url):
                raise GoogleFlightsBlocked(f"Google Flights blocked the browser at {page.url}")
            deadline = time.monotonic() + _EXPLORE_CATALOG_WAIT_MS / 1000
            while not captured and time.monotonic() < deadline:
                checkpoint()
                page.wait_for_timeout(250)
            checkpoint()
            for _post, response, retry_after in captured:
                self._raise_explore_failure(
                    response.text, response=response, retry_after_seconds=retry_after
                )
            # Keep Playwright pumping response events while checking control between sleeps.
            for elapsed in range(0, CONSENT_SETTLE_MS, 250):
                checkpoint()
                page.wait_for_timeout(min(250, CONSENT_SETTLE_MS - elapsed))
                checkpoint()
                for _post, response, retry_after in captured:
                    self._raise_explore_failure(
                        response.text, response=response, retry_after_seconds=retry_after
                    )
        finally:
            with contextlib.suppress(Exception):
                page.close()
        if not captured:
            raise SweepTransportError(
                "Google Explore issued no catalog request before the wait expired.",
                timeout=True,
            )
        return [(post, response.text) for post, response, _retry_after in captured]

    def _explore_session(self):
        if self._explore_browser is None:
            from viajante.browser import BrowserSessionConfig, ChromiumSession
            from viajante.models import FETCH_LOCALE

            self._explore_browser = ChromiumSession(
                default_state_dir(),
                BrowserSessionConfig(
                    state_filename=STATE_FILENAME,
                    locale=FETCH_LOCALE,
                    html_lang=self._html_lang,
                    currency=self._currency,
                    country=self._country,
                    proxy=self._proxy,
                ),
            )
        return self._explore_browser

    @staticmethod
    def _dismiss_consent(page) -> None:
        from viajante.google_flights import GoogleFlightsSource

        GoogleFlightsSource._dismiss_consent(page)

    def _raise_explore_failure(
        self,
        body: str,
        *,
        response: SweepHttpResponse | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        checkpoint()
        response = (
            replace(response, text=body)
            if response is not None
            else SweepHttpResponse(200, body, EXPLORE_URL)
        )
        rpc_status = raw_rpc_error_status(body)
        if response.status == 429 or rpc_status == RPC_THROTTLE_STATUS:
            cause = "HTTP 429" if response.status == 429 else f"RPC status {RPC_THROTTLE_STATUS}"
            reason = f"Google Explore catalog returned {cause}."
            basis = None
            if self._proxy is None:
                checkpoint()
                state = (
                    note_rate_limited(retry_after_seconds, endpoint=response.url)
                    if response.status == 429
                    else note_rate_limited(
                        basis="heuristic_rpc_13", cause="rpc_13", endpoint=response.url
                    )
                )
                reason = rate_limit_advice(state, reason=cause)
                basis = state.get("basis", "unknown")
            self._stopped_error = GoogleFlightsBlocked(
                reason,
                status=429,
                diagnostics=response_diagnostics(
                    replace(response, cooldown_basis=basis), response.url
                ),
            )
            raise self._stopped_error
        try:
            raise_for_sweep_response(response, response.url)
        except GoogleFlightsBlocked as exc:
            self._stopped_error = exc
            raise
        if "travel.frontend.flights.ErrorResponse" in body:
            raise GoogleFlightsRejected(
                "Google Explore rejected the catalog request without an owned cause."
            )

    def close(self) -> None:
        browser = self._explore_browser
        self._explore_browser = None
        if browser is not None:
            browser.close()
        super().close()
