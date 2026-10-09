from __future__ import annotations

import asyncio
import io
import json
import os
import random
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import _isolate  # noqa: F401
from viajante import evidence, mcp_handlers, skiplagged
from viajante.explore import search_explore
from viajante.flight_filters import NO_OFFER_FILTERS, OfferFilters
from viajante.flight_offers import _normalize_offer
from viajante.flights import _run_search, search_flights
from viajante.google_flights import GoogleFlightsBlocked, GoogleFlightsSource
from viajante.google_flights_rpc import CompactExplorePlace, RawFlightCard
from viajante.models import (
    FlightLeg,
    FlightQuery,
    HotelQuery,
    MultiCity,
    RawJourneyLeg,
    RawLayover,
    RawSegment,
    RoundTrip,
    SearchReport,
)
from viajante.ratelimit import note_rate_limited, rate_limit_status
from viajante.storage import write_text_atomic
from viajante.trip import _owned_flight_fare
from viajante.validate import validate_itinerary

FUTURE = date.today() + timedelta(days=30)


def _card(**kwargs) -> RawFlightCard:
    fields = dict(
        airline="Example Air",
        departure="10:00",
        arrival="12:00",
        duration="2 hr",
        stops="Nonstop",
        price="$100",
    )
    fields.update(kwargs)
    return RawFlightCard(**fields)


def _direct(origin="JFK", destination="LHR", *, on=FUTURE, number="EA100"):
    return RawJourneyLeg(
        departure="10:00",
        arrival="12:00",
        duration="2 hr",
        stops="Nonstop",
        segments=(
            RawSegment(origin, destination, "10:00", "12:00", "Example Air", number, on, "EA"),
        ),
    )


def _connecting_return(on):
    return RawJourneyLeg(
        departure="20:00",
        arrival="12:00",
        duration="16 hr",
        stops="1 stop",
        segments=(
            RawSegment("LHR", "BOS", "20:00", "23:00", "Example Air", "EA200", on, "EA"),
            RawSegment(
                "BOS", "JFK", "09:00", "12:00", "Example Air", "EA300", on + timedelta(days=1), "EA"
            ),
        ),
        layovers=(RawLayover("BOS", 10),),
    )


class _Source:
    config = SimpleNamespace(html_lang="en", currency="USD", country=None)

    def __init__(self, cards):
        self.cards = cards

    def fetch(self, _query):
        return self.cards

    def reset(self):
        pass

    def close(self):
        pass


def _search(query, source, *, filters=NO_OFFER_FILTERS, top=1):
    return _run_search(
        (query,),
        top=top,
        source=source,
        sleep=lambda _: None,
        random_gen=random.Random(0),
        now=lambda: datetime.now(timezone.utc),
        currency="USD",
        fetch_backend="sweep",
        filters=filters,
    )


def _selected(report):
    result = report.queries[0]
    return {
        "status": "ok",
        "query": result.query.to_dict(),
        "offer": result.offers[0].to_dict("USD"),
    }


class ItineraryEvidenceRegressions(unittest.TestCase):
    def test_unknown_stops_and_missing_layovers_never_pass_bounds(self):
        query = FlightQuery("JFK", "LHR", FUTURE)
        report = _search(query, _Source((_card(stops=None, duration=None),)))
        result = validate_itinerary(
            [_selected(report)], {"min_layover": 0.5, "max_layover": 1}, currency="USD"
        )
        self.assertIsNone(result.feasible)
        checks = {row.constraint: row.status for row in result.checks}
        self.assertEqual(checks["min_layover"], "unknown")
        self.assertEqual(checks["max_layover"], "unknown")

    def test_known_nonstop_can_pass_layover_bounds(self):
        report = _search(FlightQuery("JFK", "LHR", FUTURE), _Source((_card(),)))
        result = validate_itinerary([_selected(report)], {"max_layover": 1}, currency="USD")
        self.assertTrue(result.feasible)

    def test_missing_return_keeps_segments_and_completeness_unknown(self):
        query = RoundTrip("JFK", "LHR", FUTURE, FUTURE + timedelta(days=3))
        report = _search(query, _Source((_card(legs=(_direct(),)),)))
        offer = report.queries[0].offers[0]
        for field in (
            "segment_airports",
            "segment_operators",
            "flight_numbers",
            "segment_clocks",
            "layovers",
        ):
            self.assertEqual(getattr(offer.completeness, field), "unknown", field)
        result = validate_itinerary([_selected(report)], {"max_segments": 1}, currency="USD")
        self.assertIsNone(result.feasible)
        self.assertIsNone(result.segment_count)

    def test_travel_window_includes_return_and_every_multi_city_date(self):
        queries = (
            RoundTrip("JFK", "LHR", FUTURE, FUTURE + timedelta(days=3)),
            MultiCity(
                (
                    FlightLeg("JFK", "LHR", FUTURE),
                    FlightLeg("LHR", "CDG", FUTURE + timedelta(days=3)),
                )
            ),
        )
        for query in queries:
            with self.subTest(query=query):
                legs = tuple(
                    _direct(leg.origin, leg.destination, on=leg.departure_date)
                    for leg in query.legs
                )
                report = _search(query, _Source((_card(legs=legs),)))
                result = validate_itinerary(
                    [_selected(report)],
                    {
                        "travel_start": FUTURE.isoformat(),
                        "travel_end": (FUTURE + timedelta(days=1)).isoformat(),
                    },
                    currency="USD",
                )
                self.assertFalse(result.feasible)
                self.assertIn("travel_window", result.violations)
                self.assertEqual(result.trip_span_days, 3)

    def test_partial_multi_city_and_partial_layovers_are_unknown(self):
        query = MultiCity(
            (
                FlightLeg("JFK", "LHR", FUTURE),
                FlightLeg("LHR", "CDG", FUTURE + timedelta(days=3)),
                FlightLeg("CDG", "JFK", FUTURE + timedelta(days=6)),
            )
        )
        report = _search(query, _Source((_card(legs=(_direct(), _direct("LHR", "CDG"))),)))
        result = validate_itinerary([_selected(report)], {"max_segments": 2}, currency="USD")
        self.assertIsNone(result.feasible)
        self.assertIsNone(result.segment_count)
        two_stops = _card(
            stops="2 stops",
            legs=(RawJourneyLeg(None, None, stops="2 stops", layovers=(RawLayover("BOS", 1),)),),
        )
        report = _search(FlightQuery("JFK", "LHR", FUTURE, max_stops=2), _Source((two_stops,)))
        result = validate_itinerary([_selected(report)], {"max_layover": 2}, currency="USD")
        self.assertIsNone(result.feasible)


class PackagedFilterRegressions(unittest.TestCase):
    def test_required_via_and_overnight_can_be_owned_by_return(self):
        query = RoundTrip("JFK", "LHR", FUTURE, FUTURE + timedelta(days=3))
        package = _card(price="$500", legs=(_direct(), _connecting_return(query.return_date)))
        source = _Source((package,))
        report = _search(
            query, source, filters=OfferFilters(via=("BOS",), require_overnight=("BOS",))
        )
        self.assertEqual(len(report.queries[0].offers), 1)

    def test_unknown_and_partial_connections_cannot_prove_exclusion(self):
        for card in (
            _card(stops=None),
            _card(stops="1 stop"),
            _card(stops="2 stops", layover_city="BOS"),
            _card(
                stops="2 stops",
                legs=(
                    RawJourneyLeg(None, None, stops="2 stops", layovers=(RawLayover("BOS", 1),)),
                ),
            ),
        ):
            with self.subTest(card=card):
                self.assertIsNone(_normalize_offer(card, 2, exclude_via=("IST",)))
        self.assertIsNotNone(_normalize_offer(_card(), 2, exclude_via=("IST",)))
        complete = _card(
            stops="2 stops",
            legs=(
                RawJourneyLeg(
                    None,
                    None,
                    stops="2 stops",
                    segments=(
                        RawSegment("JFK", "BOS"),
                        RawSegment("BOS", "AMS"),
                        RawSegment("AMS", "LHR"),
                    ),
                ),
            ),
        )
        self.assertIsNotNone(_normalize_offer(complete, 2, exclude_via=("IST",)))


class MoneyEvidenceRegressions(unittest.TestCase):
    def setUp(self):
        evidence.clear()
        mcp_handlers._CACHE.clear()
        self.addCleanup(evidence.clear)
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_amount_and_currency_must_be_owned_together(self):
        evidence.record(
            {"currency": "USD", "offers": [{"price": 100}, {"price": 300, "currency": "GBP"}]}
        )
        evidence.record({"currency": "EUR", "offers": [{"price": 900}]})
        for answer in ("EUR 100", "USD 900", "USD 300", "GBP 100"):
            with self.subTest(answer=answer):
                self.assertFalse(evidence.verify_answer(answer)["ok"])
        for answer in ("USD 100", "EUR 900", "GBP 300"):
            self.assertTrue(evidence.verify_answer(answer)["ok"], answer)

    def test_cached_search_replays_are_recorded_after_ledger_eviction(self):
        report = _search(FlightQuery("JFK", "LHR", FUTURE), _Source((_card(price="$12345"),)))
        with patch("viajante.mcp_handlers.search_flights", return_value=report) as search:
            route = [f"JFK-LHR:{FUTURE.isoformat()}"]
            mcp_handlers.search_flights_tool(route, currency="USD")
            for i in range(evidence.LEDGER_SIZE):
                evidence.record({"currency": "USD", "price": i})
            self.assertFalse(evidence.verify_answer("USD 12345")["ok"])
            cached = mcp_handlers.search_flights_tool(route, currency="USD")
            self.assertTrue(cached["cached"])
            self.assertTrue(evidence.verify_answer("USD 12345")["ok"])
            search.assert_called_once()

    def test_repeated_dated_routes_are_required_fares_not_alternatives(self):
        queries = (
            FlightQuery("JFK", "LHR", FUTURE),
            FlightQuery("LHR", "JFK", FUTURE + timedelta(days=2)),
            FlightQuery("JFK", "LHR", FUTURE + timedelta(days=4)),
        )
        results = tuple(
            _search(query, _Source((_card(price=f"${price}"),))).queries[0]
            for query, price in zip(queries, (100, 200, 300), strict=True)
        )
        report = SearchReport(
            searched_at=datetime.now(timezone.utc), queries=results, currency="USD"
        )
        self.assertEqual(
            _owned_flight_fare(report, [HotelQuery("London", FUTURE, FUTURE + timedelta(days=6))]),
            600,
        )

    def test_multi_city_routes_with_different_middle_legs_do_not_collapse(self):
        queries = tuple(
            MultiCity(
                (FlightLeg("JFK", via, FUTURE), FlightLeg(via, "LHR", FUTURE + timedelta(days=2)))
            )
            for via in ("BOS", "CDG")
        )
        results = tuple(
            _search(query, _Source((_card(price=f"${price}"),))).queries[0]
            for query, price in zip(queries, (100, 200), strict=True)
        )
        report = SearchReport(
            searched_at=datetime.now(timezone.utc), queries=results, currency="USD"
        )
        self.assertEqual(
            _owned_flight_fare(report, [HotelQuery("London", FUTURE, FUTURE + timedelta(days=6))]),
            300,
        )


class ProviderFailureRegressions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": tmp.name})
        env.start()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(env.stop)
        mcp_handlers._CACHE.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_new_detail_search_does_not_open_browser_during_google_cooldown(self):
        note_rate_limited()
        session = MagicMock()
        source = GoogleFlightsSource(
            Path(os.environ["VIAJANTE_STATE_DIR"]), session=session, currency="USD"
        )
        with patch("viajante.flights.GoogleFlightsSource", return_value=source):
            report = search_flights(
                (FlightQuery("JFK", "LHR", FUTURE),), currency="USD", fetch="detail"
            )
        error = report.queries[0].error
        self.assertEqual(error.code.value, "blocked")
        self.assertTrue(error.rate_limited)
        session.new_page.assert_not_called()

    def test_detail_source_already_running_can_finish_after_cooldown_is_recorded(self):
        session = MagicMock()
        source = GoogleFlightsSource(
            Path(os.environ["VIAJANTE_STATE_DIR"]), session=session, currency="USD"
        )
        page = session.new_page.return_value
        page.url = "https://www.google.com/travel/flights"
        page.evaluate.return_value = "<main>fixture</main>"
        source._fetch_html(page.url)
        note_rate_limited()
        source._fetch_html(page.url)
        self.assertEqual(session.new_page.call_count, 2)

    def test_hidden_city_429_preserves_rate_limited_flag(self):
        report = skiplagged.search_hidden_city("JFK", "LHR", FUTURE, rpc=lambda *_: (429, {}, ""))
        self.assertEqual(report.error.code.value, "blocked")
        self.assertTrue(report.error.rate_limited)

    def test_recovered_skiplagged_429_records_cooldown_and_sends_no_retry(self):
        rpc = MagicMock(
            side_effect=[
                (404, {}, ""),
                (200, {"mcp-session-id": "new"}, json.dumps({"result": {}})),
                (202, {}, ""),
                (429, {"Retry-After": "75"}, ""),
            ]
        )
        url = "https://example.test/mcp"
        skiplagged._SESSION_IDS[(rpc, url)] = "old"
        self.addCleanup(skiplagged._SESSION_IDS.pop, (rpc, url), None)
        with (
            patch("viajante.skiplagged._rpc_post", rpc),
            patch("viajante.skiplagged.MIN_CALL_INTERVAL_SECONDS", 0),
        ):
            with self.assertRaises(skiplagged.SkiplaggedRateLimited):
                skiplagged._call_mcp({}, rpc=rpc, url=url)
            self.assertEqual(rate_limit_status(file="skiplagged-rate-limit.json")["cooldown_s"], 75)
            with self.assertRaises(skiplagged.SkiplaggedRateLimited):
                skiplagged._call_mcp({}, rpc=rpc, url=url)
        self.assertEqual(rpc.call_count, 4)

    def test_explore_counts_failed_empty_and_successful_pricing_and_does_not_cache_failure(self):
        class Explore(_Source):
            def fetch_explore(self, *_args, **_kwargs):
                return tuple(
                    CompactExplorePlace(code, code, None) for code in ("LHR", "CDG", "AMS")
                )

            def fetch(self, query):
                if query.destination == "LHR":
                    raise GoogleFlightsBlocked("HTTP 429", status=429)
                return () if query.destination == "CDG" else (_card(),)

        report = search_explore("JFK", FUTURE, currency="USD", source=Explore(()))
        coverage = report.coverage
        self.assertEqual(
            (coverage.attempted, coverage.succeeded, coverage.empty, coverage.failed), (3, 1, 1, 1)
        )
        error = report.to_dict()["pricing_errors"][0]
        self.assertEqual(error["query"]["destination"], "LHR")
        self.assertTrue(error["error"]["rate_limited"])
        with patch("viajante.mcp_handlers.search_explore", return_value=report) as search:
            for _ in range(2):
                payload = mcp_handlers.search_explore_tool(
                    "JFK", FUTURE.isoformat(), currency="USD"
                )
                self.assertNotIn("cached", payload)
        self.assertEqual(search.call_count, 2)

    def test_explore_filtered_out_pricing_failures_still_count_as_failed(self):
        class Explore(_Source):
            def fetch_explore(self, *_args, **_kwargs):
                return (CompactExplorePlace("LHR", "London", "United Kingdom"),)

            def fetch(self, _query):
                raise GoogleFlightsBlocked("HTTP 429", status=429)

        report = search_explore("JFK", FUTURE, currency="USD", source=Explore(()), via=("BOS",))
        self.assertEqual(report.destinations, ())
        self.assertEqual(
            (report.coverage.attempted, report.coverage.failed, report.coverage.empty), (1, 1, 0)
        )
        self.assertEqual(report.pricing_errors[0].query.destination, "LHR")

    def test_explore_cli_reports_pricing_failures_with_nonzero_exit(self):
        from viajante.cli import main
        from viajante.models import (
            ExploreDestination,
            ExploreReport,
            QueryFailure,
            SearchError,
            SearchErrorCode,
        )

        query = FlightQuery("JFK", "LHR", FUTURE)
        report = ExploreReport(
            searched_at=datetime.now(timezone.utc),
            origin="JFK",
            start_date=FUTURE,
            days=7,
            destinations=(),
            currency="USD",
            pricing_errors=(
                QueryFailure(
                    query=query,
                    error=SearchError(SearchErrorCode.BLOCKED, "HTTP 429", rate_limited=True),
                ),
            ),
        )
        for destinations, expected in (
            ((), 2),
            ((ExploreDestination("CDG", "Paris", "France", price=200),), 3),
        ):
            with self.subTest(expected=expected):
                value = replace(report, destinations=destinations, coverage=None)
                with (
                    patch("viajante.cli.search_explore", return_value=value),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    code = main(
                        ["explore", "JFK", "--from", FUTURE.isoformat(), "--currency", "USD"]
                    )
                self.assertEqual(code, expected)
                self.assertIn("ERROR pricing LHR: HTTP 429", output.getvalue())


class LifecycleAndInputRegressions(unittest.TestCase):
    def test_cancelled_mcp_search_keeps_busy_until_worker_finishes(self):
        from viajante.mcp_server import _SEARCH_BUSY, run_mcp_tool

        # This worker never checks its cancel event; a short grace keeps the test fast.
        grace = patch("viajante.mcp_server._CANCEL_GRACE_SECONDS", 0.05)
        grace.start()
        self.addCleanup(grace.stop)
        for fail in (False, True):
            with self.subTest(worker_raises=fail):
                started = threading.Event()
                release = threading.Event()

                def worker(started=started, release=release, fail=fail):
                    started.set()
                    if not release.wait(2):
                        raise TimeoutError("test worker was not released")
                    if fail:
                        raise RuntimeError("worker failure after cancellation")
                    return "done"

                async def main(worker=worker, started=started, release=release):
                    task = asyncio.create_task(run_mcp_tool(worker))
                    try:
                        for _ in range(100):
                            if started.is_set():
                                break
                            await asyncio.sleep(0.005)
                        self.assertTrue(started.is_set())
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        with self.assertRaisesRegex(ValueError, "already running"):
                            await run_mcp_tool(lambda: "should not run")
                    finally:
                        release.set()
                        for _ in range(100):
                            if not _SEARCH_BUSY.locked():
                                break
                            await asyncio.sleep(0.005)
                    self.assertFalse(_SEARCH_BUSY.locked())
                    self.assertEqual(await run_mcp_tool(lambda: "next"), "next")

                asyncio.run(main())

    def test_nested_stay_occupancy_is_rejected_without_fetching(self):
        mcp_handlers._CACHE.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)
        for key in ("adults", "rooms"):
            for value in (2.9, 1.0, True, "2"):
                with (
                    self.subTest(key=key, value=value),
                    patch("viajante.mcp_handlers.search_hotels") as search,
                ):
                    with self.assertRaisesRegex(ValueError, f"{key} must be an integer"):
                        mcp_handlers.search_hotels_tool(
                            stays=[
                                {
                                    "location": "London",
                                    "check_in": FUTURE.isoformat(),
                                    "check_out": (FUTURE + timedelta(days=2)).isoformat(),
                                    key: value,
                                }
                            ],
                            currency="USD",
                        )
                    search.assert_not_called()

    def test_atomic_writers_use_distinct_files_and_preserve_each_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "state.json"
            barrier = threading.Barrier(2)
            real_replace = os.replace
            errors, observed = [], []

            def synchronized_replace(source, target):
                barrier.wait(2)
                observed.append((str(source), Path(source).read_text(encoding="utf-8")))
                real_replace(source, target)

            def writer(payload):
                try:
                    write_text_atomic(payload, destination)
                except Exception as exc:
                    errors.append(exc)

            with patch("viajante.storage.os.replace", synchronized_replace):
                threads = [
                    threading.Thread(target=writer, args=(payload,))
                    for payload in ("first", "second")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(3)
            self.assertEqual(errors, [])
            self.assertEqual({payload for _, payload in observed}, {"first", "second"})
            self.assertEqual(len({source for source, _ in observed}), 2)
            self.assertIn(destination.read_text(), {"first", "second"})
            self.assertEqual(list(Path(tmp).iterdir()), [destination])

    def test_failed_atomic_replace_cleans_temporary_file_and_preserves_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "state.json"
            destination.write_text("old")
            with patch("viajante.storage.os.replace", side_effect=OSError("fixture failure")):
                with self.assertRaises(OSError):
                    write_text_atomic("new", destination)
            self.assertEqual(destination.read_text(), "old")
            self.assertEqual(list(Path(tmp).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
