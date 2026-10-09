"""Integration pins: public-page transport edges and callers that replay into it."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.control import SearchCancelled, SearchDeadline
from viajante.dates import search_dates, search_flex
from viajante.google_flights import (
    ChromeSweepClient,
    GoogleFlightsBlocked,
    SweepHttpResponse,
    SweepPost,
)
from viajante.google_flights_detail import (
    GoogleFlightsSource,
)
from viajante.google_flights_public import (
    GoogleFlightsMarkupError,
    GoogleFlightsUnsupported,
    PublicGoogleFlightsHttpSource,
    _validate_capabilities,
)
from viajante.google_flights_rpc import RawFlightCard
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    MultiCity,
    PropertyTypeEvidence,
    QueryFailure,
    RawJourneyLeg,
    RawSegment,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
)
from viajante.recheck import recheck_offer
from viajante.split import search_split_tickets
from viajante.tfs import encode_tfs_selected_outbound
from viajante.trip import search_trip

OUT = date(2099, 1, 14)
BACK = date(2099, 1, 25)
CHECKED = datetime(2098, 12, 1, 12, 0, tzinfo=timezone.utc)


def _leg(origin: str, dest: str, day: date, clock: str, flight: str) -> RawJourneyLeg:
    return RawJourneyLeg(
        clock,
        "20:00",
        "10 hr",
        "nonstop",
        (RawSegment(origin, dest, clock, "20:00", "Test Air", flight, day, "TA"),),
    )


def _card(origin: str, dest: str, day: date, clock: str, flight: str, price: str) -> RawFlightCard:
    return RawFlightCard(
        "Test Air",
        clock,
        "20:00",
        "10 hr",
        "nonstop",
        price,
        flight_numbers=(flight,),
        legs=(_leg(origin, dest, day, clock, flight),),
    )


def _connecting_leg(
    origin: str,
    hub: str,
    dest: str,
    day: date,
    flights: tuple[str, str] = ("TA101", "TA102"),
) -> RawJourneyLeg:
    return RawJourneyLeg(
        "08:00",
        "14:00",
        "6 hr",
        "1 stop",
        (
            RawSegment(origin, hub, "08:00", "10:00", "Test Air", flights[0], day, "TA"),
            RawSegment(hub, dest, "12:00", "14:00", "Test Air", flights[1], day, "TA"),
        ),
    )


def _context(*, adults: int = 2, currency: str = "EUR", cabin: str = "Economy") -> str:
    controls = {
        "adult passengers": adults,
        "children aged 2 to 11": 0,
        "infants in their own seat": 0,
        "infants on lap": 0,
    }
    return (
        f'<button aria-label="Currency {currency}"></button><div role="combobox">{cabin}</div>'
        + "".join(
            f'<div aria-label="Number of {label}"><span aria-live="polite">{count}</span></div>'
            for label, count in controls.items()
        )
    )


def _source() -> PublicGoogleFlightsHttpSource:
    return PublicGoogleFlightsHttpSource(currency="EUR", client=object())


def _echo(route: str = "HAN–SIN", clock: str = "8:00 AM") -> str:
    return f"<div>{route} {clock} Choose return round trip</div>"


class PublicRoundTripEdgeTests(unittest.TestCase):
    def test_connecting_outbound_selection_encodes_every_physical_segment(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=1)
        outbound_leg = _connecting_leg("HAN", "BKK", "SIN", OUT)
        tfs = base64.b64decode(encode_tfs_selected_outbound(query, outbound_leg))
        for marker in (b"BKK", b"TA", b"101", b"102"):
            self.assertIn(marker, tfs)
        outbound = RawFlightCard(
            "Test Air",
            "08:00",
            "14:00",
            "6 hr",
            "1 stop",
            "€100",
            flight_numbers=("TA101", "TA102"),
            legs=(outbound_leg,),
        )
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        reads: list[RawJourneyLeg | None] = []

        def read(trip, selected=None):
            reads.append(selected)
            return ((outbound,) if selected is None else (returned,)), _echo()

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(reads[1].segments), 2)
        self.assertEqual(reads[1].segments[0].destination, "BKK")
        self.assertEqual(len(offers), 1)
        self.assertEqual(len(offers[0].legs), 2)
        self.assertEqual(offers[0].flight_numbers, ("TA101", "TA102", "TA202"))
        self.assertEqual(offers[0].price, "€340")

    def test_unselectable_outbound_is_skipped_and_named_in_partial_metadata(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=1)
        broken = RawJourneyLeg(
            "08:00",
            "14:00",
            "6 hr",
            "1 stop",
            (
                RawSegment("HAN", "BKK", "08:00", "10:00", "Test Air", "TA101", OUT, "TA"),
                RawSegment("SGN", "SIN", "12:00", "14:00", "Test Air", "TA102", OUT, "TA"),
            ),
        )
        bad = RawFlightCard("Test Air", "08:00", "14:00", "6 hr", "1 stop", "€90", legs=(broken,))
        good = _card("HAN", "SIN", OUT, "10:00", "TA103", "€100")
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        reads: list[RawJourneyLeg | None] = []

        def read(trip, selected=None):
            if selected is not None:
                source._url(trip, selected)  # the request encode; bad segments raise
                reads.append(selected)
                return (returned,), _echo("HAN–SIN", "10:00 AM")
            reads.append(None)
            return (bad, good), "board"

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(offers), 1)
        partial_error, scope_bound = source.metadata_for(query)
        self.assertIsInstance(partial_error, ValueError)
        self.assertTrue(scope_bound)

    def test_unselectable_alone_is_unknown_not_a_package(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=1)
        broken = RawJourneyLeg(
            "08:00",
            "14:00",
            "6 hr",
            "1 stop",
            (
                RawSegment("HAN", "BKK", "08:00", "10:00", "Test Air", "TA101", OUT, "TA"),
                RawSegment("SGN", "SIN", "12:00", "14:00", "Test Air", "TA102", OUT, "TA"),
            ),
        )
        bad = RawFlightCard("Test Air", "08:00", "14:00", "6 hr", "1 stop", "€90", legs=(broken,))

        def read(trip, selected=None):
            if selected is not None:
                source._url(trip, selected)  # the request encode; bad segments raise
            return (bad,), "board"

        with patch.object(source, "_read_page", side_effect=read):
            with self.assertRaisesRegex(ValueError, "not contiguous"):
                source.fetch(query)

    def test_outbounds_are_selected_cheapest_first_and_ties_keep_board_order(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outs = (
            _card("HAN", "SIN", OUT, "10:00", "TA103", "€110"),
            _card("HAN", "SIN", OUT, "08:00", "TA101", "€100"),
            _card("HAN", "SIN", OUT, "12:00", "TA105", "€100"),
        )
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        echoes = {
            "08:00": _echo("HAN–SIN", "8:00 AM"),
            "10:00": _echo("HAN–SIN", "10:00 AM"),
            "12:00": _echo("HAN–SIN", "12:00 PM"),
        }
        reads: list[str] = []

        def read(trip, selected=None):
            if selected is None:
                return outs, "board"
            reads.append(selected.segments[0].departure)
            return (returned,), echoes[selected.segments[0].departure]

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(reads, ["08:00", "12:00", "10:00"])
        self.assertEqual(len(offers), 3)

    def test_429_mid_follow_up_keeps_completed_and_stops_pending(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outs = (
            _card("HAN", "SIN", OUT, "08:00", "TA101", "€100"),
            _card("HAN", "SIN", OUT, "10:00", "TA103", "€110"),
            _card("HAN", "SIN", OUT, "12:00", "TA105", "€120"),
        )
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        calls = 0

        def read(trip, selected=None):
            nonlocal calls
            calls += 1
            if selected is None:
                return outs, "board"
            if selected.departure == "10:00":
                raise GoogleFlightsBlocked("Google Flights HTTP 429", status=429)
            return (returned,), _echo("HAN–SIN", "8:00 AM")

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(calls, 3)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, "€340")
        partial_error, scope_bound = source.metadata_for(query)
        self.assertIsInstance(partial_error, GoogleFlightsBlocked)
        self.assertEqual(partial_error.status, 429)
        self.assertTrue(scope_bound)

    def test_rpc_status_13_body_mid_follow_up_is_a_block_not_a_replay(self) -> None:
        rpc_13 = ")]}'\n\n" + ('[["wrb.fr","GetShoppingResults",null,null,null,[13],"generic"]]')
        outs = (
            _card("HAN", "SIN", OUT, "08:00", "TA101", "€100"),
            _card("HAN", "SIN", OUT, "10:00", "TA103", "€110"),
            _card("HAN", "SIN", OUT, "12:00", "TA105", "€120"),
        )
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")

        class Client:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url, *, timeout):
                self.urls.append(url)
                body = rpc_13 if len(self.urls) == 3 else _context() + _echo()
                return SweepHttpResponse(200, body, url)

        client = Client()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client, sleep=lambda _: None)
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        pages = iter((outs, (returned,)))
        with patch(
            "viajante.google_flights_public.parse_shopping_page",
            side_effect=lambda *a, **k: next(pages),
        ):
            offers = source.fetch(query)
        self.assertEqual(len(client.urls), 3)
        self.assertEqual(len(offers), 1)
        partial_error, _ = source.metadata_for(query)
        self.assertIsInstance(partial_error, GoogleFlightsBlocked)
        self.assertIn("13", str(partial_error))
        self.assertIsNotNone(source._stopped_error)

    def test_markup_drift_on_one_follow_up_does_not_stop_the_rest(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outs = (
            _card("HAN", "SIN", OUT, "08:00", "TA101", "€100"),
            _card("HAN", "SIN", OUT, "10:00", "TA103", "€110"),
        )
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        reads = 0

        def read(trip, selected=None):
            nonlocal reads
            reads += 1
            if selected is None:
                return outs, "board"
            if selected.departure == "08:00":
                raise GoogleFlightsMarkupError("unreadable return page")
            return (returned,), _echo("HAN–SIN", "10:00 AM")

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(reads, 3)
        self.assertEqual(len(offers), 1)
        partial_error, _ = source.metadata_for(query)
        self.assertIsInstance(partial_error, GoogleFlightsMarkupError)

    def test_missing_or_mismatched_selected_echo_never_proves_a_package(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outbound = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        for bad_html in (
            "<div>HAN–SIN 8:00 AM departing flights</div>",
            "<div>HAN–SIN 9:00 AM Choose return round trip</div>",
            "<div>Choose return round trip</div>",
        ):
            pages = [((outbound,), "board"), ((returned,), bad_html)]
            with self.subTest(html=bad_html):
                with patch.object(source, "_read_page", side_effect=pages):
                    with self.assertRaisesRegex(Exception, "Selected outbound echo was not proven"):
                        source.fetch(query)

    def test_serial_gets_stops_after_a_blocked_page_body(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url, *, timeout):
                self.urls.append(url)
                return SweepHttpResponse(200, "our systems have detected unusual traffic", url)

        client = Client()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client)
        responses = source._serial_gets(
            client, [f"https://www.google.com/travel/flights?i={i}" for i in range(3)]
        )
        self.assertEqual(len(client.urls), 1)
        self.assertFalse(responses[1].request_sent)
        self.assertTrue(responses[1].stopped)
        self.assertEqual(responses[1].attempts, 0)


class _PartwayClient(ChromeSweepClient):
    """Batch client: job 0 completes, job ``in_flight`` is dispatched and never answers."""

    def __init__(self, in_flight: int) -> None:
        self._asyncio = asyncio
        self._streams = 1
        self._in_flight = in_flight
        self.started: list[int] = []

    async def _apost(self, url, data, headers, timeout, cancel_event=None):
        index = int(url.rsplit("/", 1)[-1])
        self.started.append(index)
        if index == self._in_flight:
            await self._asyncio.sleep(30)
        return SweepHttpResponse(200, "ok", url)

    async def _aget(self, url, timeout, cancel_event=None):
        return await self._apost(url, "", {}, timeout, cancel_event)

    def _submit(self, coro, *, timeout):
        async def _drive() -> None:
            task = asyncio.ensure_future(coro)
            await asyncio.sleep(0.05)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(_drive())
        raise SearchDeadline()


class SweepDiagnosticsTests(unittest.TestCase):
    def _assert_deadline_rows(
        self, responses: list[SweepHttpResponse], dispatched_index: int
    ) -> None:
        self.assertEqual(responses[0].status, 200)
        self.assertTrue(responses[0].request_sent)
        in_flight = responses[dispatched_index]
        self.assertTrue(in_flight.deadline)
        self.assertTrue(in_flight.request_sent)
        self.assertEqual(in_flight.attempts, 1)
        for index, response in enumerate(responses):
            if index in (0, dispatched_index):
                continue
            self.assertTrue(response.deadline)
            self.assertFalse(response.request_sent)
            self.assertEqual(response.attempts, 0)

    def test_post_many_deadline_distinguishes_dispatched_from_queued(self) -> None:
        client = _PartwayClient(in_flight=1)
        jobs = [SweepPost(f"https://example.test/{i}", "{}", {}) for i in range(4)]
        responses = client.post_many(jobs, timeout=1)
        self.assertEqual(client.started, [0, 1])
        self._assert_deadline_rows(responses, 1)

    def test_get_many_deadline_distinguishes_dispatched_from_queued(self) -> None:
        client = _PartwayClient(in_flight=2)
        urls = [f"https://example.test/{i}" for i in range(4)]
        responses = client.get_many(urls, timeout=1)
        self.assertEqual(client.started, [0, 1, 2])
        self.assertEqual(responses[0].status, 200)
        self.assertEqual(responses[1].status, 200)
        self.assertTrue(responses[2].deadline)
        self.assertTrue(responses[2].request_sent)
        self.assertEqual(responses[2].attempts, 1)
        self.assertTrue(responses[3].deadline)
        self.assertFalse(responses[3].request_sent)
        self.assertEqual(responses[3].attempts, 0)

    def test_a_mixed_one_way_page_gives_the_same_rows_through_fetch_and_fetch_many(self) -> None:
        # One board: a proven HAN-SIN card and a card from another origin. fetch keeps the
        # proven card; fetch_many must keep it too, not fail the whole day.
        good = _card("HAN", "SIN", OUT, "08:00", "VN100", "€100")
        stray = _card("SGN", "SIN", OUT, "09:00", "VN200", "€90")
        query = FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)

        class Client:
            def get(self, url, *, timeout):
                return SweepHttpResponse(200, "<html></html>", url)

            def get_many(self, urls, *, timeout):
                return [SweepHttpResponse(200, "<html></html>", url) for url in urls]

        source = PublicGoogleFlightsHttpSource(
            currency="EUR", client=Client(), sleep=lambda _: None
        )
        with patch.object(source, "_parse_response", return_value=(good, stray)):
            single = source.fetch(query)
            batched = source.fetch_many([query])[0]
        self.assertEqual([card.flight_numbers for card in single], [good.flight_numbers])
        self.assertEqual([card.flight_numbers for card in batched], [good.flight_numbers])

    def test_batch_replay_marks_the_retried_attempt(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def get_many(self, urls, *, timeout):
                self.calls += 1
                return [SweepHttpResponse(503, "unavailable", url) for url in urls]

        client = Client()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client, sleep=lambda _: None)
        results = source.fetch_many([FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)])
        self.assertEqual(client.calls, 2)
        self.assertIsInstance(results[0], GoogleFlightsBlocked)
        self.assertEqual(results[0].diagnostics["attempts"], 2)


class _DaySource:
    """Public-style per-day source answering fetch_many with scripted outcomes."""

    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.trips: list[object] = []
        self.closed = False

    def fetch_many(self, trips):
        self.trips.extend(trips)
        return list(self.results)

    def fetch(self, trip):
        raise AssertionError("per-day fetch is not used when fetch_many exists")

    def close(self) -> None:
        self.closed = True


class _DayShop:
    """Public-style day source: only the chosen day is priced, every query is recorded."""

    def __init__(self, chosen: date, cards) -> None:
        self.chosen = chosen
        self.cards = cards
        self.fetched: list[object] = []
        self.closed = False

    def fetch(self, query):
        self.fetched.append(query)
        return self.cards if query.departure_date == self.chosen else ()

    def close(self) -> None:
        self.closed = True


class DatesDeadlineCancelTests(unittest.TestCase):
    def test_deadline_mid_window_keeps_priced_days_and_stays_incomplete(self) -> None:
        card = RawFlightCard("Iberia", "08:00", "09:20", "1 hr 20 min", "Nonstop", "€41")
        source = _DaySource([(card,), SearchDeadline(), (card,)])
        report = search_dates(
            "JFK",
            "LHR",
            OUT,
            date(2099, 1, 16),
            source=source,
            currency="EUR",
            baggage_buffer=0,
        )
        self.assertEqual([row.status for row in report.days], ["ok", "error", "ok"])
        self.assertEqual(report.days[0].price, 41)
        self.assertEqual(report.days[1].error.code, SearchErrorCode.DEADLINE)
        self.assertFalse(report.coverage.complete)
        self.assertEqual(report.coverage.stopping_reason, "deadline")
        self.assertEqual(report.coverage.succeeded, 2)
        self.assertTrue(source.closed)

    def test_cancel_before_any_request_reaches_the_source(self) -> None:
        card = RawFlightCard("Iberia", "08:00", "09:20", "1 hr", "Nonstop", "€41")
        source = _DaySource([(card,)])
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(SearchCancelled):
            search_dates(
                "JFK",
                "LHR",
                OUT,
                OUT,
                source=source,
                currency="EUR",
                cancel=cancel,
            )
        self.assertEqual(source.trips, [])
        self.assertTrue(source.closed)

    def test_refused_constraint_marks_every_day_without_a_request(self) -> None:
        class NoGet:
            def get(self, *args, **kwargs):
                raise AssertionError("refused queries must not reach the client")

        source = PublicGoogleFlightsHttpSource(currency="EUR", client=NoGet())
        report = search_dates(
            "JFK",
            "LHR",
            OUT,
            date(2099, 1, 16),
            source=source,
            currency="EUR",
            bags=1,
            baggage_buffer=0,
        )
        self.assertTrue(all(row.status == "error" for row in report.days))
        self.assertTrue(all(row.error.code == SearchErrorCode.REJECTED for row in report.days))

    def test_round_trip_deadline_keeps_finished_days_and_marks_the_rest(self) -> None:
        card = RawFlightCard("Iberia", "08:00", "09:20", "1 hr 20 min", "Nonstop", "€41")
        fetched: list[date] = []
        progress_lines: list[str] = []

        class Transport:
            def fetch_round_trip_window(self, trips, on_day):
                for index, trip in enumerate(trips):
                    if index == 2:
                        raise SearchDeadline()
                    fetched.append(trip.departure_date)
                    on_day(index, (card,))

            def fetch(self, trip):
                raise AssertionError("a round-trip window is one bounded batch")

            def fetch_many(self, trips):
                raise AssertionError("a round-trip window does not use one-way fetch_many")

            def close(self) -> None:
                return None

        def progress(line: str) -> None:
            if line.startswith("["):
                progress_lines.append(line)

        report = search_dates(
            "JFK",
            "LHR",
            OUT,
            date(2099, 1, 17),
            nights=5,
            source=Transport(),
            currency="EUR",
            baggage_buffer=0,
            deadline_seconds=60,
            progress=progress,
        )
        self.assertEqual(fetched, [OUT, date(2099, 1, 15)])
        self.assertEqual([row.status for row in report.days], ["ok", "ok", "error", "error"])
        self.assertEqual(report.days[0].price, 41)
        self.assertEqual(report.days[1].price, 41)
        self.assertEqual(report.days[2].error.code, SearchErrorCode.DEADLINE)
        self.assertEqual(report.days[3].error.code, SearchErrorCode.DEADLINE)
        self.assertEqual(
            progress_lines,
            [
                f"[1/4] JFK -> LHR {OUT.isoformat()}",
                "[2/4] JFK -> LHR 2099-01-15",
            ],
        )
        self.assertFalse(report.coverage.complete)
        self.assertEqual(report.coverage.stopping_reason, "deadline")


class FlexShopReplayTests(unittest.TestCase):
    def _shop_source(self, *cards: RawFlightCard) -> _DayShop:
        return _DayShop(date(2099, 1, 15), cards)

    def test_flex_shop_replays_cabin_stops_stay_and_party(self) -> None:
        card = RawFlightCard("Iberia", "08:00", "09:20", "1 hr", "Nonstop", "€90")
        source = self._shop_source(card)
        report = search_flex(
            "JFK",
            "LHR",
            date(2099, 1, 15),
            1,
            trip="rt",
            nights=5,
            cabin="business",
            max_stops=2,
            adults=2,
            children=1,
            source=source,
            currency="EUR",
            baggage_buffer=0,
        )
        # The chosen day is priced from the sweep, so every request carries the same party,
        # cabin, stops and five-night stay; no separate shop request is sent.
        for query in source.fetched:
            self.assertEqual(query.cabin, "business")
            self.assertEqual(query.max_stops, 2)
            self.assertEqual(query.adults, 2)
            self.assertEqual(query.children, 1)
            self.assertEqual((query.return_date - query.departure_date).days, 5)
        self.assertEqual(report.trip, "rt")

    def test_flex_shop_applies_every_named_filter(self) -> None:
        keep = RawFlightCard("Iberia", "08:00", "09:20", "1 hr", "Nonstop", "€100")
        long_layover = RawFlightCard(
            "Iberia",
            "08:00",
            "20:00",
            "5 hr",
            "1 stop",
            "€60",
            layover_city="DXB",
            layover_hours=6.0,
        )
        too_long = RawFlightCard("Iberia", "08:00", "20:00", "20 hr", "Nonstop", "€50")
        source = self._shop_source(keep, long_layover, too_long)
        report = search_flex(
            "JFK",
            "LHR",
            date(2099, 1, 15),
            1,
            source=source,
            currency="EUR",
            baggage_buffer=0,
            max_layover_hours=3,
            max_duration_hours=8,
        )
        self.assertEqual([offer.price for offer in report.offers], [100.0])


def _previous_with(query: dict) -> dict:
    return {
        "price": 500.0,
        "currency": "USD",
        "legs": [
            {
                "departure": "19:30",
                "arrival": "07:30",
                "segments": [
                    {
                        "origin": "JFK",
                        "destination": "LHR",
                        "departure": "19:30",
                        "departure_date": OUT.isoformat(),
                        "flight_number": "BA178",
                        "carrier": "BA",
                    }
                ],
            }
        ],
        "evidence": {"source": "google_flights", "currency": "USD"},
    }


def _failed_report(code: SearchErrorCode) -> SearchReport:
    return SearchReport(
        searched_at=CHECKED,
        queries=(
            QueryFailure(
                query=FlightQuery("JFK", "LHR", OUT),
                error=SearchError(code=code, message=f"{code.value} message"),
            ),
        ),
        currency="USD",
        fetch_backend="sweep",
    )


class RecheckConstraintReplayTests(unittest.TestCase):
    def _constrained_query(self) -> dict:
        return {
            "trip": "one-way",
            "origin": "JFK",
            "destination": "LHR",
            "departure_date": OUT.isoformat(),
            "adults": 1,
            "cabin": "economy",
            "max_stops": 1,
            "bags": 1,
            "airlines": ["BA"],
        }

    def test_refused_constraints_still_ride_the_fresh_query(self) -> None:
        seen: list[object] = []

        def search(trips, **kwargs):
            seen.extend(trips)
            return _failed_report(SearchErrorCode.REJECTED)

        result = recheck_offer(
            _previous_with(self._constrained_query()),
            query=self._constrained_query(),
            search=search,
        )
        self.assertEqual(len(seen), 1)
        trip = seen[0]
        self.assertEqual(trip.bags, 1)
        self.assertEqual(trip.airlines, ("BA",))
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rejected")
        self.assertIn("bags", result["filters_replayed"])
        self.assertIn("airlines", result["filters_replayed"])

    def test_provider_empty_is_completed_not_found_under_the_same_constraints(self) -> None:
        def search(trips, **kwargs):
            return _failed_report(SearchErrorCode.NO_RESULTS)

        result = recheck_offer(
            _previous_with(self._constrained_query()),
            query=self._constrained_query(),
            search=search,
        )
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "provider_empty")

    def test_real_search_path_refuses_and_stays_check_failed(self) -> None:
        # The real public transport refuses bags before networking; no stub needed.
        result = recheck_offer(
            _previous_with(self._constrained_query()),
            query=self._constrained_query(),
        )
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rejected")
        self.assertEqual(result["fetch_backend"], "sweep")
        self.assertIn("bags", result["filters_replayed"])


class SplitConstraintReplayTests(unittest.TestCase):
    def test_leg_queries_carry_constraints_and_refusals_stay_visible(self) -> None:
        day = OUT
        query = FlightQuery("JFK", "NRT", day, max_stops=1, bags=1, airlines=("BA",))
        packaged = _failed_report(SearchErrorCode.NO_RESULTS)
        seen: list[object] = []

        def search(trips, **kwargs):
            seen.extend(trips)
            return SearchReport(
                searched_at=CHECKED,
                queries=tuple(
                    QueryFailure(
                        query=trip,
                        error=SearchError(code=SearchErrorCode.REJECTED, message="cannot verify"),
                    )
                    for trip in trips
                ),
                currency="USD",
                fetch_backend="sweep",
            )

        report = search_split_tickets(
            query, packaged=packaged, via=("HKG",), currency="USD", search=search
        )
        self.assertTrue(seen)
        self.assertTrue(all(trip.bags == 1 for trip in seen))
        self.assertTrue(all(trip.airlines == ("BA",) for trip in seen))
        self.assertEqual(report.itineraries, ())
        self.assertTrue(all(row["status"] == "error" for row in report.legs))
        self.assertTrue(all(row["error"]["code"] == "rejected" for row in report.legs))
        self.assertGreaterEqual(report.coverage.failed, 2)

    def test_real_search_path_legs_fail_visibly_not_silently(self) -> None:
        query = FlightQuery("JFK", "NRT", OUT, max_stops=1, bags=1)
        report = search_split_tickets(
            query,
            packaged=_failed_report(SearchErrorCode.NO_RESULTS),
            via=("HKG",),
            currency="USD",
        )
        self.assertEqual(report.itineraries, ())
        self.assertTrue(report.legs)
        self.assertTrue(all(row["status"] == "error" for row in report.legs))
        self.assertTrue(all(row["error"]["code"] == "rejected" for row in report.legs))


def _hotel_report() -> HotelSearchReport:
    offer = HotelOffer(
        title="Old Town Apartment",
        address="Melbourne",
        total_price_text="246 €",
        total_price=246.0,
        rating="8.9",
        rating_score=8.9,
        details="Free cancellation",
        cancellation_evidence=CancellationEvidence.FREE,
        property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
        lodging_kind=LodgingKind.ENTIRE_HOME,
        bedrooms=1,
        bathrooms=1,
        beds=2,
        link=None,
    )
    return HotelSearchReport(
        searched_at=CHECKED,
        queries=(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", OUT, BACK),
                applied=AppliedHotelFilters(chips=(), url="https://example.test"),
                raw_count=1,
                eligible_count=1,
                offers=(offer,),
            ),
        ),
        currency="EUR",
        fetch_backend="google",
        provider="google-hotels",
    )


class TripConstraintReplayTests(unittest.TestCase):
    def test_refused_flight_constraint_never_fabricates_a_trip_total(self) -> None:
        # Real flight path: the public transport refuses the overlaid bags
        # before any request; only the hotel side is stubbed.
        with patch("viajante.trip.search_hotels", return_value=_hotel_report()):
            report = search_trip(
                (RoundTrip("SIN", "MEL", OUT, BACK),),
                HotelQuery("Melbourne", OUT, BACK),
                bags=1,
                currency="EUR",
            )
        self.assertIsNone(report.trip_total)
        failure = report.flights.queries[0]
        self.assertIsInstance(failure, QueryFailure)
        self.assertEqual(failure.error.code, SearchErrorCode.REJECTED)
        self.assertEqual(failure.query.bags, 1)


class _NoBrowser:
    def new_page(self):
        raise AssertionError("a refused detail fetch must not open a page")

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class DetailCapabilityTests(unittest.TestCase):
    def test_detail_refuses_bag_and_carrier_filters_it_cannot_verify(self) -> None:
        source = GoogleFlightsSource(Path("unused"), session=_NoBrowser(), currency="EUR")
        for trip in (
            FlightQuery("HAN", "SIN", OUT, adults=2, bags=1),
            FlightQuery("HAN", "SIN", OUT, adults=2, carry_on=1),
            FlightQuery("HAN", "SIN", OUT, adults=2, airlines=("TA",)),
            FlightQuery("HAN", "SIN", OUT, adults=2, exclude_airlines=("TA",)),
            FlightQuery("HAN", "SIN", OUT, adults=2, alliances=("star",)),
        ):
            with self.subTest(trip=trip), self.assertRaises(GoogleFlightsUnsupported):
                source.fetch(trip)

    def test_sweep_refuses_multi_city_and_names_the_detail_path(self) -> None:
        trip = MultiCity(
            (
                FlightQuery("HAN", "SIN", OUT, adults=2).legs[0],
                FlightQuery("SIN", "HAN", BACK, adults=2).legs[0],
            ),
            adults=2,
        )
        with self.assertRaisesRegex(GoogleFlightsUnsupported, "--fetch detail"):
            _validate_capabilities(trip)


if __name__ == "__main__":
    unittest.main()
