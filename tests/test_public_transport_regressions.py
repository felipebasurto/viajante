"""Fail-closed public transport, cooperative control, and package identity regressions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import _isolate  # noqa: F401
import test_google_flights as flight_fixtures
from test_explore import _explore_catalog_body, _explore_post, _explore_priced
from test_google_flights import _multi_board_html, _MultiPage
from test_google_flights_public import _context
from viajante.carriers import _airline_filter_hit, _passes_airline_filters
from viajante.control import SearchCancelled, SearchControl, SearchDeadline, active
from viajante.envelope import stamp_search
from viajante.explore import search_explore
from viajante.google_flights import (
    ChromeSweepClient,
    GoogleFlightsBlocked,
    NoFlightsFound,
    SweepHttpResponse,
    SweepPost,
    _multi_row_index,
    parse_flight_cards,
)
from viajante.google_flights_public import (
    GoogleFlightsMarkupError,
    PublicGoogleFlightsHttpSource,
    _package_card,
)
from viajante.google_flights_rpc import RawFlightCard, _wrb_chunk_objects, raw_rpc_error_status
from viajante.models import FlightLeg, FlightQuery, MultiCity, RawJourneyLeg
from viajante.ratelimit import rate_limit_status
from viajante.runtime import get_runtime_info

DAY = date(2099, 11, 14)
CATALOG_URL = "https://www.google.com/service/GetExploreDestinations?private=query"


def _mixed_rpc_body(*statuses: int) -> str:
    rows = [["wrb.fr", None, json.dumps([None])]]
    rows.extend(["wrb.fr", None, None, None, None, [status]] for status in statuses)
    return ")]}'\n\n" + json.dumps(rows)


class WrbChunkTests(unittest.TestCase):
    def test_a_bare_chunk_does_not_hide_the_length_prefixed_chunks_after_it(self) -> None:
        body = '[["a"]]\n7\n[["b"]]'
        self.assertEqual(list(_wrb_chunk_objects(body)), [[["a"]], [["b"]]])


class RoundTripPackageCarrierTests(unittest.TestCase):
    def _card(self, airline: str, codes: tuple[str, ...] | None, price: str) -> RawFlightCard:
        return RawFlightCard(
            airline=airline,
            airline_codes=codes,
            departure="08:00",
            arrival="10:00",
            price=price,
            duration="2 h",
            stops="Nonstop",
            legs=(RawJourneyLeg(departure="08:00", arrival="10:00"),),
        )

    def test_the_return_carrier_is_checked_against_the_exclusion(self) -> None:
        outbound = self._card("Iberia", ("IB",), "100 €")
        returned = self._card("Air Europa", ("UX",), "200 €")
        package = _package_card(outbound, returned)
        self.assertEqual(package.airline_codes, ("IB", "UX"))
        self.assertEqual(package.airline, "Iberia, Air Europa")
        self.assertFalse(_passes_airline_filters(package, airlines=None, exclude_airlines=("UX",)))

    def test_a_return_with_unknown_carriers_cannot_prove_an_exclusion(self) -> None:
        outbound = self._card("Iberia", ("IB",), "100 €")
        package = _package_card(outbound, self._card("", None, "200 €"))
        self.assertIsNone(package.airline_codes)
        self.assertFalse(_passes_airline_filters(package, airlines=None, exclude_airlines=("UX",)))

    def test_the_package_fare_and_bags_come_from_the_return_card(self) -> None:
        outbound = replace(self._card("Iberia", ("IB",), "100 €"), checked_bags=2)
        returned = replace(self._card("Iberia", ("IB",), "260 €"), checked_bags=0)
        package = _package_card(outbound, returned)
        self.assertEqual(package.price, "260 €")
        self.assertEqual(package.checked_bags, 0)


class _ExplorePage:
    url = "https://www.google.com/travel/explore"

    def __init__(self, body: str, *, status: int = 200, navigate=None, wait=None) -> None:
        self.response = SimpleNamespace(
            url=CATALOG_URL,
            status=status,
            headers={"retry-after": "120"},
            request=SimpleNamespace(post_data=_explore_post(day=DAY.isoformat())),
            text=lambda: body,
        )
        self.navigate = navigate
        self.wait = wait
        self.waits = 0
        self.closed = False

    def on(self, _event, callback):
        self.callback = callback

    def goto(self, _url, **_kwargs):
        if self.navigate is not None:
            return self.navigate(self)
        self.callback(self.response)
        return None

    def wait_for_timeout(self, milliseconds):
        self.waits += 1
        if self.wait is not None:
            self.wait(self, milliseconds)

    def close(self):
        self.closed = True


class PublicTransportRegressions(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="viajante-tests-")
        self.addCleanup(directory.cleanup)
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": directory.name})
        env.start()
        self.addCleanup(env.stop)

    def _source(self, page, *, proxy=None):
        source = PublicGoogleFlightsHttpSource(currency="USD", client=object(), proxy=proxy)
        source._explore_session = lambda: SimpleNamespace(new_page=lambda: page)
        return source

    def test_explore_http_429_stops_before_settle_and_stamps_actual_endpoint(self):
        for body in (
            "Too many requests",
            _explore_catalog_body(_explore_priced("/m/0pmp2", "YQB")),
        ):
            with self.subTest(body=body[:20]):
                page = _ExplorePage(body, status=429)
                with self.assertRaises(GoogleFlightsBlocked) as caught:
                    self._source(page).fetch_explore("JFK", DAY)
                error = caught.exception
                self.assertEqual(error.status, 429)
                self.assertEqual(error.diagnostics["http_status"], 429)
                self.assertEqual(
                    error.diagnostics["endpoint"], "www.google.com/service/GetExploreDestinations"
                )
                self.assertEqual(rate_limit_status()["basis"], "provider_retry_after")
                self.assertEqual(rate_limit_status()["cooldown_s"], 120)
                self.assertEqual(
                    rate_limit_status()["endpoint"], "www.google.com/service/GetExploreDestinations"
                )
                self.assertEqual(page.waits, 0)
                self.assertTrue(page.closed)
                # Reset only the test's temporary cooldown before the next subcase.
                from viajante.ratelimit import GOOGLE_RATE_LIMIT_FILE
                from viajante.storage import default_state_dir

                (default_state_dir() / GOOGLE_RATE_LIMIT_FILE).unlink()

    def test_proxied_explore_http_429_does_not_record_direct_cooldown(self):
        page = _ExplorePage("Too many requests", status=429)
        result = search_explore(
            "JFK", DAY, source=self._source(page, proxy="http://127.0.0.1:8080")
        )
        envelope = stamp_search(result.to_dict())
        self.assertEqual(envelope["status"], "rate_limited")
        self.assertEqual(envelope["empty_reason"], "not_loaded")
        self.assertIsNone(envelope["retry_after"])
        self.assertIsNone(rate_limit_status())

    def test_explore_navigation_429_also_records_the_cooldown(self):
        page = _ExplorePage("Too many requests")
        page.navigate = lambda _page: page.response
        page.response.status = 429
        with self.assertRaises(GoogleFlightsBlocked):
            self._source(page).fetch_explore("JFK", DAY)
        self.assertIsNotNone(rate_limit_status())
        self.assertTrue(page.closed)

    def test_proxied_block_stops_later_explore_and_flight_jobs_without_cooldown(self):
        for body, status in (("Too many requests", 429), (_mixed_rpc_body(13), 200)):
            with self.subTest(status=status):
                page = _ExplorePage(body, status=status)
                source = self._source(page, proxy="http://127.0.0.1:8080")
                with self.assertRaises(GoogleFlightsBlocked):
                    source.fetch_explore("JFK", DAY)
                source._explore_session = lambda: self.fail("pending jobs must not open a page")
                with self.assertRaises(GoogleFlightsBlocked) as caught:
                    source.fetch_explore("EWR", DAY)
                self.assertFalse(caught.exception.diagnostics["request_sent"])
                self.assertEqual(caught.exception.diagnostics["attempts"], 0)
                with self.assertRaises(GoogleFlightsBlocked):
                    source.fetch(FlightQuery("JFK", "LHR", DAY))
                self.assertIsNone(rate_limit_status())

    def test_status_13_wins_over_data_and_other_errors_in_rows_and_chunks(self):
        first = json.dumps([["wrb.fr", None, None, None, None, [3]]])
        later = json.dumps([["wrb.fr", None, None, None, None, [13]]])
        bodies = (
            _mixed_rpc_body(13),
            _mixed_rpc_body(3, 13),
            f")]}}'\n\n{len(first)}\n{first}\n{len(later)}\n{later}",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(raw_rpc_error_status(body), 13)
        page = _ExplorePage(bodies[1])
        result = search_explore("JFK", DAY, source=self._source(page))
        envelope = stamp_search(result.to_dict())
        self.assertEqual(envelope["status"], "rate_limited")
        self.assertEqual(envelope["empty_reason"], "not_loaded")
        self.assertEqual(result.error.diagnostics["rpc_status"], 13)
        self.assertEqual(rate_limit_status()["basis"], "heuristic_rpc_13")

    def test_sweep_exchange_and_queue_stop_on_status_13_in_a_later_row(self):
        client = object.__new__(ChromeSweepClient)
        client._proxied = False
        client._asyncio = asyncio
        client._streams = 1
        body = _mixed_rpc_body(13)

        async def response():
            return SimpleNamespace(status_code=200, text=body, url=CATALOG_URL, headers={})

        exchanged = asyncio.run(client._exchange(response, 1))
        self.assertIsNotNone(exchanged.rate_limit)
        self.assertEqual(rate_limit_status()["basis"], "heuristic_rpc_13")
        sent = []

        async def get(url, *_args):
            sent.append(url)
            return exchanged

        client._aget = get
        jobs = [SweepPost(f"https://www.google.com/{index}", "", {}) for index in range(3)]
        result = asyncio.run(client._apost_many(jobs, 1, [None] * 3, get=True))
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(row.stopped and not row.request_sent for row in result[1:]))

    def test_cancelled_explore_response_cannot_write_cooldown(self):
        for status, body in ((429, "Too many requests"), (200, _mixed_rpc_body(13))):
            with self.subTest(status=status):
                cancel = threading.Event()

                def navigate(page, cancel=cancel):
                    cancel.set()
                    page.callback(page.response)

                page = _ExplorePage(body, status=status, navigate=navigate)
                with self.assertRaises(SearchCancelled):
                    search_explore("JFK", DAY, source=self._source(page), cancel=cancel)
                self.assertIsNone(rate_limit_status())
                self.assertTrue(page.closed)

    def test_explore_poll_observes_deadline_and_never_claims_provider_empty(self):
        clock = SimpleNamespace(now=0.0)

        def wait(_page, milliseconds):
            clock.now += milliseconds / 1000

        page = _ExplorePage(_explore_catalog_body(), navigate=lambda _page: None, wait=wait)
        control = SearchControl(deadline_seconds=0.1, clock=lambda: clock.now)
        with (
            active(control),
            patch("viajante.google_flights_public.time.monotonic", lambda: clock.now),
        ):
            result = search_explore("JFK", DAY, source=self._source(page))
        self.assertTrue(control.cut)
        self.assertEqual(result.error.code.value, "deadline")
        self.assertEqual(result.coverage.stopping_reason, "deadline")
        envelope = stamp_search(result.to_dict())
        self.assertEqual(envelope["empty_reason"], "not_loaded")
        self.assertNotEqual(envelope["completeness"], "complete")
        self.assertEqual(page.waits, 1)
        self.assertTrue(page.closed)

    def test_explore_settle_is_cancellable(self):
        cancel = threading.Event()
        body = _explore_catalog_body(_explore_priced("/m/0pmp2", "YQB"))
        page = _ExplorePage(body, wait=lambda _page, _milliseconds: cancel.set())
        with self.assertRaises(SearchCancelled):
            search_explore("JFK", DAY, source=self._source(page), cancel=cancel)
        self.assertEqual(page.waits, 1)
        self.assertTrue(page.closed)
        self.assertIsNone(rate_limit_status())

    def test_explore_capture_does_not_swallow_search_deadline(self):
        def cut():
            raise SearchDeadline()

        page = _ExplorePage("")
        page.response.text = cut
        with self.assertRaises(SearchDeadline):
            self._source(page).fetch_explore("JFK", DAY)
        self.assertTrue(page.closed)

    def test_airline_codes_cannot_match_substrings_or_override_owned_codes(self):
        delta = RawFlightCard(
            "Delta", "08:00", "10:00", "2 hr", "Nonstop", "$100", airline_codes=("DL",)
        )
        for card in (delta, replace(delta, airline_codes=None)):
            with self.subTest(codes=card.airline_codes):
                self.assertFalse(_airline_filter_hit(card, "TA"))
                self.assertFalse(
                    _passes_airline_filters(card, airlines=("TA",), exclude_airlines=None)
                )
                self.assertTrue(
                    _passes_airline_filters(card, airlines=None, exclude_airlines=("TA",))
                )
        iberia_express = replace(delta, airline="Iberia Express", airline_codes=("I2",))
        self.assertFalse(_airline_filter_hit(iberia_express, "IB"))
        self.assertTrue(
            _airline_filter_hit(replace(delta, airline="British Airways", airline_codes=None), "BA")
        )
        self.assertTrue(_airline_filter_hit(replace(delta, airline_codes=("AA", "BA")), "BA"))
        source = PublicGoogleFlightsHttpSource(currency="USD", client=object())
        with self.assertRaises(GoogleFlightsMarkupError):
            source._verify_carrier_filters(
                "", (delta,), FlightQuery("JFK", "LHR", DAY, airlines=("TA",))
            )

    def test_multi_city_reselection_uses_current_arrival_and_package_total(self):
        first = _multi_board_html([("LHR", "JFK", "AA", "103", "20991114", "10:15 AM", "£500")])
        last = _multi_board_html([("JFK", "LAX", "AA", "201", "20991118", "9:00 AM", "£1,200")])

        class ReloadPage(_MultiPage):
            reads = 0

            def evaluate(self, script):
                html = super().evaluate(script)
                if self.step == 0 and "innerHTML" in script:
                    self.reads += 1
                    arrival = "3:00 PM" if self.reads == 1 else "5:00 PM"
                    return html.replace("<div>arr</div>", f"<div>{arrival}</div>")
                return html

        trip = MultiCity(
            (FlightLeg("LHR", "JFK", DAY), FlightLeg("JFK", "LAX", date(2099, 11, 18)))
        )
        page = ReloadPage([first, last])
        cards = flight_fixtures.MultiCityDetailTests._source(page).fetch(trip)
        self.assertEqual(cards[0].legs[0].arrival, "5:00 PM")
        self.assertEqual(cards[0].arrival, "5:00 PM")
        self.assertEqual(cards[0].price, "£1,200")

    def test_multi_city_reselection_rejects_ambiguous_or_incomplete_identity(self):
        card = parse_flight_cards(
            _multi_board_html([("LHR", "JFK", "AA", "103", "20991114", "10:15 AM", "£500")])
        )[0]
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "ambiguous"):
            _multi_row_index([(0, card), (1, replace(card, price="£600"))], card)
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "incomplete"):
            _multi_row_index([(0, card)], replace(card, departure=None))
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "incomplete"):
            _multi_row_index([(0, card)], replace(card, legs=()))
        changed_route = replace(card.legs[0].segments[0], destination="LAX")
        changed = replace(card, legs=(replace(card.legs[0], segments=(changed_route,)),))
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "no longer appears"):
            _multi_row_index([(0, changed)], card)

    def test_multi_city_click_preserves_search_deadline(self):
        class Page(_MultiPage):
            def wait_for_function(self, *_args, **_kwargs):
                raise SearchDeadline()

        source = flight_fixtures.MultiCityDetailTests._source(Page(["", ""]))
        with self.assertRaises(SearchDeadline):
            source._multi_click(source._session.new_page(), 0)

    def test_empty_page_requires_alliance_echo_before_provider_empty(self):
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=object())
        query = FlightQuery("JFK", "LHR", DAY, adults=2, alliances=("star",))
        empty = [None, None, [[], None, False], [[], 0, False], None, None, None]

        def html(data):
            return (
                _context(airlines_chip="Airlines, Selected")
                + "<script class='ds:1'>AF_initDataCallback({key:'ds:1',data:"
                + json.dumps(data)
                + "});</script>"
            )

        for data in (empty, empty + [[None, [[], [["ONEWORLD", "Oneworld"]]]]]):
            with self.subTest(data=data), self.assertRaises(GoogleFlightsMarkupError):
                source._parse_response(SweepHttpResponse(200, html(data)), CATALOG_URL, query)
        proven = empty + [[None, [[], [["STAR_ALLIANCE", "Star Alliance"]]]]]
        with self.assertRaises(NoFlightsFound):
            source._parse_response(SweepHttpResponse(200, html(proven)), CATALOG_URL, query)

    def test_runtime_capability_flags_remain_boolean_with_additive_limits(self):
        capabilities = get_runtime_info()["flight_capabilities"]
        for key in ("carrier_filters", "multi_city", "explore_catalog"):
            self.assertIs(type(capabilities[key]), bool)
            self.assertTrue(capabilities[key])
        self.assertFalse(capabilities["exclude_alliances"])
        self.assertEqual(capabilities["multi_city_fetch"], "detail_only")
        self.assertEqual(capabilities["explore_catalog_scope"], "bounded")


if __name__ == "__main__":
    unittest.main()
