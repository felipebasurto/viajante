from __future__ import annotations

import unittest
from datetime import date, datetime
from random import Random
from types import SimpleNamespace
from typing import Sequence
from unittest.mock import patch

from viajante.airports import get_airport
from viajante.flights import (
    _normalize_offer,
    _rank_offers,
    _run_search,
    classify_failure,
    compare_nonstop_vs_one_stop,
    expand_nearby_trips,
    is_low_cost,
    nearby_notes,
    normalize_trip_kind,
    parse_depart_window,
    parse_flight_plan,
    parse_route_specs,
    parse_via_airports,
    plan_unit_count,
    resolve_fetch_mode,
    search_flights,
    sweep_needs_fallback,
)
from viajante.google_flights import (
    GoogleFlightsBlocked,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    NoFlightsFound,
    RawFlightCard,
    google_flights_url,
)
from viajante.google_flights_rpc import build_shopping_inner
from viajante.models import (
    FlightOffer,
    FlightQuery,
    MultiCity,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RawLayover,
    RawSegment,
    RoundTrip,
    SearchErrorCode,
    SearchReport,
)
from viajante.orchestration import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_JITTER_SECONDS,
    MAX_ATTEMPTS,
    REQUEST_DELAY_SECONDS,
    REQUEST_JITTER_SECONDS,
    sweep_inter_query_delay_seconds,
)


def card(
    *,
    airline: str = "Air",
    departure: str = "08:00",
    arrival: str = "09:00",
    price: str = "99 €",
    duration: str = "1 h",
    stops: str = "Nonstop",
    layover_city: str | None = None,
    layover_hours: float | None = None,
    airline_codes: tuple[str, ...] | None = None,
    flight_numbers: tuple[str, ...] | None = None,
    checked_bags: int | None = None,
    carry_on: int | None = None,
    booking_token: str | None = None,
    legs: tuple[RawJourneyLeg, ...] = (),
) -> RawFlightCard:
    return RawFlightCard(
        airline=airline,
        departure=departure,
        arrival=arrival,
        price=price,
        duration=duration,
        stops=stops,
        layover_city=layover_city,
        layover_hours=layover_hours,
        airline_codes=airline_codes,
        flight_numbers=flight_numbers,
        checked_bags=checked_bags,
        carry_on=carry_on,
        booking_token=booking_token,
        legs=legs,
    )


class FakeSource:
    def __init__(self, responses: dict[tuple, object]) -> None:
        self.responses = responses
        self.fetch_calls = 0
        self.reset_calls = 0
        self.closed = False
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(self, query) -> Sequence[RawFlightCard]:
        self.fetch_calls += 1
        if isinstance(query, FlightQuery):
            key = (
                query.origin,
                query.destination,
                query.departure_date.isoformat(),
                query.max_stops,
            )
        else:
            first = query.legs[0]
            key = (
                first.origin,
                first.destination,
                first.departure_date.isoformat(),
                first.max_stops,
            )
        response = self.responses.get(key)
        if isinstance(response, Exception):
            raise response
        if response is None:
            raise RuntimeError("missing fake response")
        return response

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.closed = True


class LowCostClassificationTests(unittest.TestCase):
    def test_matching_ignores_case_and_punctuation(self) -> None:
        for variant in ("Ryanair", "ryanair", "RYANAIR", "EasyJet", "easyJet"):
            with self.subTest(variant=variant):
                self.assertTrue(is_low_cost(variant))
        self.assertTrue(is_low_cost("T'Way Air"))
        self.assertTrue(is_low_cost("Tway Air"))

    def test_european_carriers_on_the_documented_routes_are_covered(self) -> None:
        for airline in ("Vueling", "Wizz Air", "Transavia", "Volotea", "Norwegian"):
            with self.subTest(airline=airline):
                self.assertTrue(is_low_cost(airline))

    def test_full_service_carriers_are_not_penalised(self) -> None:
        for airline in ("Iberia", "Air Europa", "Lufthansa", "Iberia Express", ""):
            with self.subTest(airline=airline):
                self.assertFalse(is_low_cost(airline))

    def test_matching_respects_word_boundaries(self) -> None:
        self.assertFalse(is_low_cost("Peachtree Air"))


class FailureClassificationTests(unittest.TestCase):
    def test_empty_results_are_not_a_fetch_failure(self) -> None:
        error = classify_failure(NoFlightsFound("No options matching your search"))
        self.assertEqual(error.code, SearchErrorCode.NO_RESULTS)
        self.assertEqual(
            error.message,
            "Google Flights returned no flights for this route and date.",
        )

    def test_rejected_shopping_query_is_rejected(self) -> None:
        error = classify_failure(GoogleFlightsRejected("unknown airport"))
        self.assertEqual(error.code, SearchErrorCode.REJECTED)
        self.assertIn("rejected", error.message.casefold())

    def test_missing_chromium_is_reported_as_browser_unavailable(self) -> None:
        error = classify_failure(
            RuntimeError("Executable doesn't exist at /ms-playwright/chromium/headless")
        )
        self.assertEqual(error.code, SearchErrorCode.BROWSER_UNAVAILABLE)
        self.assertIn("playwright install chromium", error.message)

    def test_markup_errors_are_markup_drift(self) -> None:
        error = classify_failure(GoogleFlightsMarkupError("no results grid"))
        self.assertEqual(error.code, SearchErrorCode.MARKUP_DRIFT)
        self.assertIn("no results grid", error.message)

    def test_http_blocks_are_blocked(self) -> None:
        error = classify_failure(GoogleFlightsBlocked("consent wall"))
        self.assertEqual(error.code, SearchErrorCode.BLOCKED)
        self.assertIn("consent wall", error.message)

    def test_unrecognised_failures_keep_their_original_text(self) -> None:
        error = classify_failure(TimeoutError("Timeout 60000ms exceeded"))
        self.assertEqual(error.code, SearchErrorCode.FETCH_FAILED)
        self.assertIn("Timeout 60000ms exceeded", error.message)


class NonRetriableFailureTests(unittest.TestCase):
    def _run(self, exc: Exception) -> tuple:
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): exc})
        sleeps: list[float] = []
        report = _run_search(
            (FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
        )
        return report.queries[0], source, sleeps

    def test_no_results_is_not_retried(self) -> None:
        outcome, source, sleeps = self._run(NoFlightsFound())
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.reset_calls, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(outcome.error.code, SearchErrorCode.NO_RESULTS)

    def test_missing_browser_is_not_retried(self) -> None:
        outcome, source, sleeps = self._run(RuntimeError("Executable doesn't exist"))
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.reset_calls, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(outcome.error.code, SearchErrorCode.BROWSER_UNAVAILABLE)

    def test_markup_errors_are_not_retried(self) -> None:
        outcome, source, sleeps = self._run(GoogleFlightsMarkupError("selectors rotted"))
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.reset_calls, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(outcome.error.code, SearchErrorCode.MARKUP_DRIFT)

    def test_rejected_queries_do_not_reset_tls(self) -> None:
        outcome, source, sleeps = self._run(GoogleFlightsRejected("unknown airport"))
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.reset_calls, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(outcome.error.code, SearchErrorCode.REJECTED)

    def test_http_blocks_are_not_retried(self) -> None:
        outcome, source, sleeps = self._run(GoogleFlightsBlocked("consent wall"))
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(source.reset_calls, 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(outcome.error.code, SearchErrorCode.BLOCKED)

    def test_transient_failures_are_still_retried(self) -> None:
        outcome, source, sleeps = self._run(RuntimeError("network"))
        self.assertEqual(source.fetch_calls, MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), MAX_ATTEMPTS - 1)
        self.assertEqual(outcome.error.code, SearchErrorCode.FETCH_FAILED)


class ProgressTests(unittest.TestCase):
    def test_each_query_is_announced_before_it_runs(self) -> None:
        lines: list[str] = []
        queries = (
            FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),
            FlightQuery("MAD", "LHR", date(2026, 9, 2), max_stops=1),
        )
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (card(),),
                ("MAD", "LHR", "2026-09-02", 1): NoFlightsFound(),
            }
        )
        _run_search(
            queries,
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
            progress=lines.append,
        )
        self.assertIn("[1/2] MAD -> BCN 2026-09-01", lines)
        self.assertIn("[2/2] MAD -> LHR 2026-09-02", lines)
        self.assertTrue(any("no_results" in line for line in lines))


class FlightsOrchestrationTests(unittest.TestCase):
    def test_retry_reset_backoff_and_continue(self) -> None:
        q_ok = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        q_fail = FlightQuery("MAD", "LHR", date(2026, 9, 2), max_stops=1)
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (card(airline="Air One"),),
                ("MAD", "LHR", "2026-09-02", 1): RuntimeError("network"),
            }
        )
        sleeps: list[float] = []
        expected_rng = Random(0)
        inter_query = REQUEST_DELAY_SECONDS + expected_rng.uniform(0, REQUEST_JITTER_SECONDS)
        expected_backoffs = [
            BACKOFF_BASE_SECONDS * (2**attempt) + expected_rng.uniform(0, BACKOFF_JITTER_SECONDS)
            for attempt in range(MAX_ATTEMPTS - 1)
        ]

        report = _run_search(
            (q_ok, q_fail),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 9, 0, 0),
        )

        self.assertEqual(source.reset_calls, MAX_ATTEMPTS)
        self.assertEqual(source.fetch_calls, 1 + MAX_ATTEMPTS)
        self.assertIsInstance(report.queries[0], QuerySuccess)
        self.assertIsInstance(report.queries[1], QueryFailure)
        self.assertEqual(report.queries[1].error.code.value, "fetch_failed")
        self.assertEqual(len(sleeps), len(expected_backoffs) + 1)
        self.assertAlmostEqual(sleeps[0], inter_query)
        for got, want in zip(sleeps[1:], expected_backoffs, strict=True):
            self.assertAlmostEqual(got, want)

    def test_zero_retry_backoff_skips_sleep_on_transient_failure(self) -> None:
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): RuntimeError("network")})
        sleeps: list[float] = []
        report = _run_search(
            (FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 9, 0, 0),
            retry_backoff=lambda _attempt, _rng: 0.0,
        )
        self.assertEqual(source.fetch_calls, MAX_ATTEMPTS)
        self.assertEqual(source.reset_calls, MAX_ATTEMPTS)
        self.assertEqual(sleeps, [])
        self.assertIsInstance(report.queries[0], QueryFailure)
        self.assertEqual(report.queries[0].error.code.value, "fetch_failed")

    def test_many_one_ways_use_one_calendar_batch_when_source_offers_it(self) -> None:
        q1 = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        q2 = FlightQuery("MAD", "LHR", date(2026, 9, 2), max_stops=1)

        class BatchSource(FakeSource):
            def __init__(self) -> None:
                super().__init__(
                    {
                        ("MAD", "BCN", "2026-09-01", 1): (card(airline="Iberia"),),
                        ("MAD", "LHR", "2026-09-02", 1): (card(airline="British Airways"),),
                    }
                )
                self.batch_calls = 0

            def fetch(self, query):  # type: ignore[no-untyped-def]
                raise AssertionError("batched one-ways should not call fetch")

            def fetch_many_with_calendar(self, jobs):
                self.batch_calls += 1
                rows = []
                for query, _start, _end in jobs:
                    cards = FakeSource.fetch(self, query)
                    rows.append((cards, ()))
                return rows

        source = BatchSource()
        sleeps: list[float] = []
        report = _run_search(
            (q1, q2),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 9, 0, 0),
        )
        self.assertEqual(source.batch_calls, 1)
        self.assertEqual(source.reset_calls, 0)
        self.assertEqual(sleeps, [])
        self.assertIsInstance(report.queries[0], QuerySuccess)
        self.assertIsInstance(report.queries[1], QuerySuccess)
        self.assertEqual(report.queries[0].offers[0].airline, "Iberia")
        self.assertEqual(report.queries[1].offers[0].airline, "British Airways")

    def test_max_stops_zero_keeps_only_nonstop(self) -> None:
        nonstop = card(stops="Nonstop", price="100 €")
        one_stop = card(stops="1 stop", price="80 €", departure="10:00", arrival="13:00")
        self.assertIsNotNone(_normalize_offer(nonstop, max_stops=0))
        self.assertIsNone(_normalize_offer(one_stop, max_stops=0))
        self.assertIsNotNone(_normalize_offer(one_stop, max_stops=1))

    def test_bag_request_drops_only_offers_that_contradict_parsed_counts(self) -> None:
        missing = card(price="80 €")
        too_few = card(price="70 €", checked_bags=0, carry_on=1)
        enough = card(price="90 €", checked_bags=1, carry_on=1)
        self.assertIsNotNone(_normalize_offer(missing, max_stops=1, bags=1, carry_on=1))
        self.assertIsNone(_normalize_offer(too_few, max_stops=1, bags=1, carry_on=1))
        kept = _normalize_offer(enough, max_stops=1, bags=1, carry_on=1)
        assert kept is not None
        self.assertEqual(kept.checked_bags, 1)
        self.assertEqual(kept.carry_on, 1)

    def test_parsed_bag_counts_clear_the_lcc_guess(self) -> None:
        raw = card(airline="Ryanair", price="50 €", checked_bags=1, carry_on=0)
        offer = _normalize_offer(raw, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.baggage_buffer_eur, 0)
        self.assertFalse(offer.needs_bag_verify)

    def test_requested_bags_without_parsed_counts_do_not_invent_a_fee(self) -> None:
        raw = card(airline="Ryanair", price="50 €")
        offer = _normalize_offer(raw, max_stops=1, bags=1)
        assert offer is not None
        self.assertEqual(offer.baggage_buffer_eur, 0)
        self.assertTrue(offer.needs_bag_verify)
        self.assertNotIn("checked_bags", offer.to_dict())

    def test_parse_flight_plan_keeps_bags_unset_by_default(self) -> None:
        plan = parse_flight_plan(["MAD-BCN:2026-09-01"], max_stops=1, bags=1, carry_on=1)
        self.assertEqual(plan[0].bags, 1)
        self.assertEqual(plan[0].carry_on, 1)
        default = parse_flight_plan(["MAD-BCN:2026-09-01"], max_stops=1)
        self.assertIsNone(default[0].bags)
        self.assertIsNone(default[0].carry_on)
        self.assertIsNone(default[0].price_cap_eur)

    def test_named_price_cap_drops_owned_fares_above_the_cap(self) -> None:
        under = card(price="199 €")
        at_cap = card(price="200 €")
        over = card(price="201 €")
        self.assertIsNotNone(_normalize_offer(under, 1, price_cap_eur=200))
        self.assertIsNotNone(_normalize_offer(at_cap, 1, price_cap_eur=200))
        self.assertIsNone(_normalize_offer(over, 1, price_cap_eur=200))
        self.assertIsNotNone(_normalize_offer(over, 1))
        four_hundred = card(price="400 €")
        over_four = card(price="401 €")
        self.assertIsNotNone(_normalize_offer(four_hundred, 1, price_cap_eur=400))
        self.assertIsNone(_normalize_offer(over_four, 1, price_cap_eur=400))

    def test_parse_flight_plan_named_price_cap_stays_off_index_7(self) -> None:
        plan = parse_flight_plan(["MAD-BCN:2026-09-01"], max_stops=1, price_cap_eur=200)
        self.assertEqual(plan[0].price_cap_eur, 200)
        self.assertIsNone(build_shopping_inner(plan[0])[1][7])
        unnamed = parse_flight_plan(["MAD-BCN:2026-09-01"], max_stops=1)
        self.assertIsNone(unnamed[0].price_cap_eur)
        self.assertIsNone(build_shopping_inner(unnamed[0])[1][7])

    def test_unlabelled_stops_are_rejected_when_only_direct_flights_are_wanted(self) -> None:
        unknown = card(stops="Unknown", price="90 €", departure="14:00", arrival="15:00")
        self.assertIsNone(_normalize_offer(unknown, max_stops=0))
        self.assertIsNotNone(_normalize_offer(unknown, max_stops=1))

    def test_eligible_count_is_zero_when_all_offers_fail_normalize(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=0)
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 0): (
                    card(stops="Unknown", price="90 €", departure="14:00", arrival="15:00"),
                    card(stops="1 stop", price="80 €", departure="10:00", arrival="13:00"),
                ),
            }
        )
        report = _run_search(
            (query,),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
        )
        outcome = report.queries[0]
        self.assertIsInstance(outcome, QuerySuccess)
        self.assertEqual(outcome.raw_count, 2)
        self.assertEqual(outcome.eligible_count, 0)
        self.assertEqual(outcome.offers, ())
        self.assertIsNone(outcome.stops_compare)
        self.assertNotIn("stops_compare", outcome.to_dict())

    def test_rank_dedupe_and_baggage(self) -> None:
        offers = (
            FlightOffer(
                airline="Ryanair",
                departure="07:00",
                arrival="09:00",
                price="50 €",
                price_eur=50.0,
                duration="2 h",
                duration_hours=2.0,
                stops="Directo",
                stops_count=0,
                baggage_buffer_eur=70,
                needs_bag_verify=True,
            ),
            FlightOffer(
                airline="Legacy",
                departure="08:00",
                arrival="10:00",
                price="100 €",
                price_eur=100.0,
                duration="2 h",
                duration_hours=2.0,
                stops="Directo",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
            ),
            FlightOffer(
                airline="Ryanair",
                departure="07:00",
                arrival="09:00",
                price="50 €",
                price_eur=50.0,
                duration="2 h",
                duration_hours=2.0,
                stops="Directo",
                stops_count=0,
                baggage_buffer_eur=70,
                needs_bag_verify=True,
            ),
        )
        ranked = _rank_offers(offers, top=5)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].airline, "Legacy")
        self.assertEqual(ranked[1].airline, "Ryanair")
        by_fare = _rank_offers(offers, top=5, sort="fare")
        self.assertEqual(by_fare[0].airline, "Ryanair")
        self.assertEqual(by_fare[1].airline, "Legacy")

    def test_raw_normalized_pairing(self) -> None:
        raw = card(
            airline="Air",
            departure="08:40",
            arrival="11:30",
            price="129 €",
            duration="2 h 50 min",
            stops="Nonstop",
        )
        offer = _normalize_offer(raw, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.price, "129 €")
        self.assertEqual(offer.price_eur, 129.0)
        self.assertEqual(offer.duration, "2 h 50 min")
        self.assertAlmostEqual(offer.duration_hours or 0, 2 + 50 / 60)
        self.assertEqual(offer.stops, "Nonstop")
        self.assertEqual(offer.stops_count, 0)

    def test_dedupe_keeps_flights_that_differ_only_in_stops(self) -> None:
        def offer(stops_count: int, hours: float) -> FlightOffer:
            return FlightOffer(
                airline="Iberia",
                departure="08:00",
                arrival="09:00",
                price="100 €",
                price_eur=100.0,
                duration=f"{hours} h",
                duration_hours=hours,
                stops="Nonstop" if stops_count == 0 else "1 stop",
                stops_count=stops_count,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
            )

        ranked = _rank_offers((offer(0, 1.0), offer(1, 2.5)), top=5)
        self.assertEqual(len(ranked), 2)

    def test_parse_route_specs(self) -> None:
        queries = parse_route_specs(["MAD-BCN:2026-09-01,2026-09-02"], max_stops=0)
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0].max_stops, 0)

    def test_parse_round_trip_sugar(self) -> None:
        queries = parse_route_specs(["MAD-OPO:2026-10-09:2026-10-12"], max_stops=1)
        legs = [
            (query.origin, query.destination, query.departure_date.isoformat()) for query in queries
        ]
        self.assertEqual(legs, [("MAD", "OPO", "2026-10-09"), ("OPO", "MAD", "2026-10-12")])

    def test_parse_rejects_mixed_rt_and_comma_dates(self) -> None:
        with self.assertRaises(ValueError):
            parse_route_specs(["MAD-OPO:2026-10-09:2026-10-12,2026-10-13"], max_stops=1)

    def test_parse_rejects_return_on_or_before_outbound(self) -> None:
        with self.assertRaises(ValueError):
            parse_route_specs(["MAD-OPO:2026-10-12:2026-10-09"], max_stops=1)

    def test_parse_flight_plan_one_way_keeps_sugar(self) -> None:
        plan = parse_flight_plan(
            ["MAD-OPO:2026-10-09:2026-10-12"],
            trip="one-way",
            max_stops=1,
        )
        self.assertIsInstance(plan, tuple)
        self.assertEqual(plan_unit_count(plan), 2)

    def test_parse_flight_plan_rt(self) -> None:
        plan = parse_flight_plan(
            ["MAD-OPO:2026-10-09:2026-10-12"],
            trip="rt",
            max_stops=1,
            adults=2,
        )
        self.assertIsInstance(plan, RoundTrip)
        assert isinstance(plan, RoundTrip)
        self.assertEqual(plan.origin, "MAD")
        self.assertEqual(plan.destination, "OPO")
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan_unit_count(plan), 1)
        with self.assertRaises(ValueError):
            parse_flight_plan(["MAD-BCN:2026-09-01"], trip="rt", max_stops=1)
        with self.assertRaises(ValueError):
            parse_flight_plan(
                ["MAD-OPO:2026-10-09:2026-10-12", "LIS-MAD:2026-10-15"],
                trip="rt",
                max_stops=1,
            )
        with self.assertRaises(ValueError):
            parse_flight_plan(["MAD-BCN:2026-09-01,2026-09-02"], trip="rt", max_stops=1)
        alias = parse_flight_plan(
            ["MAD-OPO:2026-10-09:2026-10-12"],
            trip="round-trip",
            max_stops=1,
        )
        self.assertIsInstance(alias, RoundTrip)
        self.assertEqual(normalize_trip_kind("oneway"), "one-way")
        self.assertEqual(normalize_trip_kind("one_way"), "one-way")
        self.assertEqual(normalize_trip_kind("round_trip"), "rt")

    def test_parse_flight_plan_rt_open_jaw_is_packaged_multi(self) -> None:
        plan = parse_flight_plan(
            ["YVR-LHR:2026-10-09", "LGW-YVR:2026-10-13"],
            trip="rt",
            max_stops=1,
            adults=2,
            children=1,
        )
        self.assertIsInstance(plan, MultiCity)
        assert isinstance(plan, MultiCity)
        self.assertEqual(
            [(leg.origin, leg.destination, leg.departure_date) for leg in plan.legs],
            [
                ("YVR", "LHR", date(2026, 10, 9)),
                ("LGW", "YVR", date(2026, 10, 13)),
            ],
        )
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.children, 1)
        self.assertEqual(plan_unit_count(plan), 1)
        mirrored = parse_flight_plan(
            ["YVR-LHR:2026-10-09:2026-10-13"],
            trip="rt",
            max_stops=1,
        )
        self.assertIsInstance(mirrored, RoundTrip)
        assert isinstance(mirrored, RoundTrip)
        self.assertEqual(mirrored.destination, "LHR")
        self.assertEqual(mirrored.legs[1].origin, "LHR")

    def test_nearby_expands_one_way_and_skips_named_open_jaw(self) -> None:
        queries = parse_route_specs(["BOS-LHR:2026-09-18"], max_stops=1)
        off = expand_nearby_trips(queries, nearby=False)
        self.assertEqual([(item.origin, item.destination) for item in off], [("BOS", "LHR")])
        expanded = expand_nearby_trips(queries, nearby=True)
        pairs = [(item.origin, item.destination) for item in expanded]
        self.assertEqual(pairs[0], ("BOS", "LHR"))
        dests = {dest for _origin, dest in pairs}
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= dests)
        self.assertTrue(all(origin == "BOS" for origin, _dest in pairs))
        self.assertNotIn("BQH", dests)
        self.assertTrue(all(item.nearby_label for item in expanded))
        self.assertIn("nearby London LGW", [item.nearby_label for item in expanded])
        notes = nearby_notes(expanded)
        self.assertEqual(len(notes), 1)
        self.assertIn("London", notes[0])
        self.assertIn("LHR", notes[0])
        self.assertIn("LGW", notes[0])

        tokyo = expand_nearby_trips(
            parse_route_specs(["LAX-NRT:2026-11-03"], max_stops=1),
            nearby=True,
        )
        tokyo_dests = {item.destination for item in tokyo}
        self.assertEqual(tokyo[0].destination, "NRT")
        self.assertEqual(tokyo_dests, {"NRT", "HND"})

        mad = expand_nearby_trips(
            parse_route_specs(["MAD-BCN:2026-09-01"], max_stops=1),
            nearby=True,
        )
        self.assertEqual([(item.origin, item.destination) for item in mad], [("MAD", "BCN")])
        self.assertIsNone(mad[0].nearby_label)

        packaged = parse_flight_plan(
            ["BOS-LHR:2026-10-09:2026-10-12"],
            trip="rt",
            max_stops=1,
        )
        rt_alts = expand_nearby_trips((packaged,), nearby=True)
        self.assertGreater(len(rt_alts), 1)
        self.assertIsInstance(rt_alts[0], RoundTrip)
        self.assertEqual(
            (rt_alts[0].origin, rt_alts[0].destination),
            ("BOS", "LHR"),
        )
        self.assertTrue({"LHR", "LGW"} <= {item.destination for item in rt_alts})
        self.assertTrue(all(item.origin == "BOS" for item in rt_alts))

        open_jaw = parse_flight_plan(
            ["YVR-LHR:2026-10-09", "LGW-YVR:2026-10-13"],
            trip="rt",
            max_stops=1,
        )
        kept = expand_nearby_trips((open_jaw,), nearby=True)
        self.assertEqual(len(kept), 1)
        self.assertIsInstance(kept[0], MultiCity)
        self.assertEqual(
            [(leg.origin, leg.destination) for leg in kept[0].legs],
            [("YVR", "LHR"), ("LGW", "YVR")],
        )

    def test_parse_flight_plan_occupancy(self) -> None:
        plan = parse_flight_plan(
            ["MAD-OPO:2026-10-09:2026-10-12"],
            trip="rt",
            max_stops=1,
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        self.assertIsInstance(plan, RoundTrip)
        assert isinstance(plan, RoundTrip)
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.children, 1)
        self.assertEqual(plan.infants_in_seat, 1)
        self.assertEqual(plan.infants_on_lap, 1)

    def test_parse_flight_plan_multi(self) -> None:
        plan = parse_flight_plan(
            ["MAD-BCN:2026-09-01", "BCN-FCO:2026-09-04"],
            trip="multi",
            max_stops=0,
        )
        self.assertIsInstance(plan, MultiCity)
        assert isinstance(plan, MultiCity)
        self.assertEqual(len(plan.legs), 2)
        self.assertEqual(plan.legs[1].origin, "BCN")
        self.assertEqual(plan_unit_count(plan), 1)
        with self.assertRaises(ValueError):
            parse_flight_plan(["MAD-BCN:2026-09-01"], trip="multi", max_stops=1)
        with self.assertRaises(ValueError):
            parse_flight_plan(
                ["MAD-OPO:2026-10-09:2026-10-12", "OPO-LIS:2026-10-15"],
                trip="multi",
                max_stops=1,
            )
        with self.assertRaises(ValueError):
            parse_flight_plan(
                ["MAD-BCN:2026-09-01,2026-09-02", "BCN-FCO:2026-09-04"],
                trip="multi",
                max_stops=1,
            )

    def test_round_trip_search_fetches_once(self) -> None:
        trip = RoundTrip("MAD", "OPO", date(2026, 10, 9), date(2026, 10, 12), max_stops=1)
        source = FakeSource(
            {("MAD", "OPO", "2026-10-09", 1): (card(airline="TAP", price="120 €"),)}
        )
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights((trip,), top=3, fetch="sweep")
        self.assertEqual(source.fetch_calls, 1)
        self.assertEqual(len(report.queries), 1)
        query = report.queries[0].query
        self.assertEqual(query.origin, "MAD")
        self.assertEqual(query.destination, "OPO")
        self.assertEqual(query.to_dict()["trip"], "rt")
        self.assertEqual(query.to_dict()["return_date"], "2026-10-12")
        assert isinstance(report.queries[0], QuerySuccess)
        self.assertEqual(report.queries[0].offers[0].price_eur, 120.0)

    def test_search_stamps_google_flights_urls(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource(
            {("MAD", "BCN", "2026-09-01", 1): (card(airline="Iberia", booking_token="tok"),)}
        )
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights((query,), top=1, fetch="sweep")
        result = report.queries[0]
        assert isinstance(result, QuerySuccess)
        expected_query = google_flights_url(query, currency="EUR")
        expected_offer = google_flights_url(query, currency="EUR", booking_token="tok")
        self.assertEqual(result.google_flights_url, expected_query)
        self.assertEqual(result.offers[0].google_flights_url, expected_offer)
        self.assertIn("booking_token=tok", result.offers[0].google_flights_url or "")
        payload = report.to_dict()
        self.assertEqual(payload["queries"][0]["query"]["google_flights_url"], expected_query)
        self.assertEqual(payload["queries"][0]["offers"][0]["google_flights_url"], expected_offer)

    def test_search_closes_source(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): (card(airline="Air One"),)})
        with patch("viajante.flights.GoogleFlightsSource", return_value=source):
            search_flights((query,), top=1)
        self.assertTrue(source.closed)

    def test_sweep_skips_the_browser_inter_query_delay(self) -> None:
        queries = (
            FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),
            FlightQuery("MAD", "OPO", date(2026, 10, 9), max_stops=1),
        )
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (card(),),
                ("MAD", "OPO", "2026-10-09", 1): (card(airline="Ryanair"),),
            }
        )
        sleeps: list[float] = []
        _run_search(
            queries,
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
            inter_query_delay=sweep_inter_query_delay_seconds,
        )
        self.assertEqual(sleeps, [0.0])

    def test_search_sweep_does_not_construct_chromium(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): (card(airline="Iberia"),)})
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            with patch("viajante.flights.GoogleFlightsSource") as detail:
                report = search_flights((query,), top=1, fetch="sweep")
        detail.assert_not_called()
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertIsInstance(report.fetch_ms, int)
        self.assertGreaterEqual(report.fetch_ms or 0, 0)
        self.assertTrue(source.closed)

    def test_auto_uses_sweep_for_three_queries(self) -> None:
        queries = (
            FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),
            FlightQuery("MAD", "OPO", date(2026, 10, 9), max_stops=1),
            FlightQuery("OPO", "MAD", date(2026, 10, 12), max_stops=1),
        )
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (card(),),
                ("MAD", "OPO", "2026-10-09", 1): (card(),),
                ("OPO", "MAD", "2026-10-12", 1): (card(),),
            }
        )
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            with patch("viajante.flights.GoogleFlightsSource") as detail:
                report = search_flights(queries, top=1)
        detail.assert_not_called()
        self.assertEqual(report.fetch_backend, "sweep")

    def test_sweep_fallback_reruns_the_whole_report_on_detail(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        sweep = FakeSource({("MAD", "BCN", "2026-09-01", 1): NoFlightsFound()})
        detail = FakeSource({("MAD", "BCN", "2026-09-01", 1): (card(airline="Iberia"),)})
        lines: list[str] = []
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=sweep):
            with patch("viajante.flights.GoogleFlightsSource", return_value=detail):
                report = search_flights((query,), top=1, fetch="sweep", progress=lines.append)
        self.assertEqual(report.fetch_backend, "sweep_then_detail")
        self.assertIsInstance(report.queries[0], QuerySuccess)
        self.assertEqual(report.queries[0].offers[0].airline, "Iberia")
        self.assertTrue(any("falling back to detail" in line for line in lines))
        self.assertTrue(sweep.closed)
        self.assertTrue(detail.closed)

    def test_sweep_fallback_does_not_rerun_successful_legs(self) -> None:
        ok = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        empty = FlightQuery("MAD", "ICN", date(2026, 9, 22), max_stops=1)
        sweep = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (card(airline="Vueling"),),
                ("MAD", "ICN", "2026-09-22", 1): NoFlightsFound(),
            }
        )
        detail = FakeSource({("MAD", "ICN", "2026-09-22", 1): (card(airline="Korean Air"),)})
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=sweep):
            with patch("viajante.flights.GoogleFlightsSource", return_value=detail) as detail_ctor:
                report = search_flights((ok, empty), top=1, fetch="sweep")
        self.assertEqual(report.fetch_backend, "sweep_then_detail")
        self.assertEqual(report.queries[0].offers[0].airline, "Vueling")
        self.assertEqual(report.queries[1].offers[0].airline, "Korean Air")
        self.assertEqual(detail.fetch_calls, 1)
        detail_ctor.assert_called_once()

    def test_rejected_sweep_query_does_not_open_chromium(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        sweep = FakeSource(
            {("MAD", "BCN", "2026-09-01", 1): GoogleFlightsRejected("unknown airport")}
        )
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=sweep):
            with patch("viajante.flights.GoogleFlightsSource") as detail:
                report = search_flights((query,), top=1, fetch="sweep")
        detail.assert_not_called()
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertIsInstance(report.queries[0], QueryFailure)
        self.assertEqual(report.queries[0].error.code, SearchErrorCode.REJECTED)

    def test_markup_miss_after_sweep_does_not_open_chromium(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        sweep = FakeSource(
            {("MAD", "BCN", "2026-09-01", 1): GoogleFlightsMarkupError("no results grid")}
        )
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=sweep):
            with patch("viajante.flights.GoogleFlightsSource") as detail:
                report = search_flights((query,), top=1, fetch="sweep")
        detail.assert_not_called()
        self.assertEqual(report.fetch_backend, "sweep")
        self.assertEqual(report.queries[0].error.code, SearchErrorCode.MARKUP_DRIFT)

    def test_twelve_and_twenty_four_hour_clocks_dedupe(self) -> None:
        ampm = _normalize_offer(
            card(
                airline="Iberia",
                departure="1:40 PM",
                arrival="9:00 AM",
                price="74 €",
                duration="20 hr 20 min",
                stops="1 stop",
            ),
            max_stops=1,
        )
        hhmm = _normalize_offer(
            card(
                airline="Iberia",
                departure="13:40",
                arrival="09:00",
                price="74 €",
                duration="20 hr 20 min",
                stops="1 stop",
            ),
            max_stops=1,
        )
        assert ampm is not None and hhmm is not None
        ranked = _rank_offers((ampm, hhmm), top=5)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0].departure, "13:40")
        self.assertEqual(ranked[0].arrival, "09:00")

    def test_max_layover_drops_overnight_connections(self) -> None:
        overnight = card(
            airline="Tap Air Portugal",
            departure="13:40",
            arrival="09:00",
            price="74 €",
            duration="20 hr 20 min",
            stops="1 stop",
            layover_city="Lisbon",
            layover_hours=18.0,
        )
        self.assertIsNone(_normalize_offer(overnight, max_stops=1, max_layover_hours=10))
        short = card(airline="Air Europa", departure="10:35", arrival="10:50", price="58 €")
        self.assertIsNotNone(_normalize_offer(short, max_stops=1, max_layover_hours=10))


class FetchModeTests(unittest.TestCase):
    def test_auto_is_detail_for_one_or_two_queries(self) -> None:
        self.assertEqual(resolve_fetch_mode("auto", 1), "detail")
        self.assertEqual(resolve_fetch_mode("auto", 2), "detail")

    def test_auto_is_sweep_for_three_or_more(self) -> None:
        self.assertEqual(resolve_fetch_mode("auto", 3), "sweep")
        self.assertEqual(resolve_fetch_mode("auto", 10), "sweep")

    def test_explicit_modes_win(self) -> None:
        self.assertEqual(resolve_fetch_mode("sweep", 1), "sweep")
        self.assertEqual(resolve_fetch_mode("detail", 8), "detail")

    def test_fallback_on_empty_or_failure_not_on_ok(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        ok = SearchReport(
            searched_at=datetime(2026, 8, 10),
            queries=(QuerySuccess(query=query, raw_count=2, eligible_count=1, offers=()),),
        )
        empty = SearchReport(
            searched_at=datetime(2026, 8, 10),
            queries=(
                QueryFailure(
                    query=query,
                    error=classify_failure(NoFlightsFound()),
                ),
            ),
        )
        self.assertFalse(sweep_needs_fallback(ok))
        self.assertTrue(sweep_needs_fallback(empty))


class CarrierShoppingOverlayTests(unittest.TestCase):
    def test_search_stamps_airline_filters_on_the_fetched_trip(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): (card(airline="Iberia"),)})
        seen: list[object] = []
        original = source.fetch

        def fetch(trip):
            seen.append(trip)
            return original(trip)

        source.fetch = fetch  # type: ignore[method-assign]
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            search_flights((query,), top=1, fetch="sweep", airlines=("BA", "KL"))
        self.assertEqual(seen[0].airlines, ("BA", "KL"))  # type: ignore[attr-defined]

    def test_auto_uses_sweep_when_carrier_filters_are_set(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource({("MAD", "BCN", "2026-09-01", 1): (card(),)})
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            with patch("viajante.flights.GoogleFlightsSource") as detail:
                report = search_flights((query,), top=1, airlines=("BA",))
        detail.assert_not_called()
        self.assertEqual(report.fetch_backend, "sweep")


class OfferFilterTests(unittest.TestCase):
    def test_include_airlines_keeps_matching_codes(self) -> None:
        iberia = card(airline="Iberia", airline_codes=("IB",), price="100 €")
        ryanair = card(airline="Ryanair", airline_codes=("FR",), price="40 €", departure="09:00")
        self.assertIsNotNone(_normalize_offer(iberia, 1, airlines=("IB", "I2")))
        self.assertIsNone(_normalize_offer(ryanair, 1, airlines=("IB", "I2")))

    def test_exclude_airlines_drops_matching_names(self) -> None:
        air_europa = card(airline="Air Europa", airline_codes=("UX",), price="90 €")
        iberia = card(airline="Iberia", airline_codes=("IB",), price="100 €")
        self.assertIsNone(_normalize_offer(air_europa, 1, exclude_airlines=("UX",)))
        self.assertIsNotNone(_normalize_offer(iberia, 1, exclude_airlines=("UX",)))

    def test_depart_window_keeps_local_hours(self) -> None:
        early = card(departure="06:45", price="80 €")
        mid = card(departure="09:10", price="90 €")
        late = card(departure="13:00", price="70 €")
        window = parse_depart_window("7-12")
        self.assertEqual(window, (7 * 60, 12 * 60 + 59))
        self.assertIsNone(_normalize_offer(early, 1, depart_window=window))
        self.assertIsNotNone(_normalize_offer(mid, 1, depart_window=window))
        self.assertIsNone(_normalize_offer(late, 1, depart_window=window))

    def test_depart_window_clocks_are_inclusive_minutes(self) -> None:
        window = parse_depart_window("06:00-20:00")
        self.assertEqual(window, (6 * 60, 20 * 60))
        self.assertIsNone(
            _normalize_offer(card(departure="05:59", price="80 €"), 1, depart_window=window)
        )
        self.assertIsNotNone(
            _normalize_offer(card(departure="06:00", price="80 €"), 1, depart_window=window)
        )
        self.assertIsNotNone(
            _normalize_offer(card(departure="20:00", price="80 €"), 1, depart_window=window)
        )
        self.assertIsNone(
            _normalize_offer(card(departure="20:01", price="80 €"), 1, depart_window=window)
        )
        hour_window = parse_depart_window("6-20")
        self.assertIsNotNone(
            _normalize_offer(card(departure="20:45", price="80 €"), 1, depart_window=hour_window)
        )

    def test_parse_depart_window_rejects_backwards_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_depart_window("20:00-06:00")
        with self.assertRaises(ValueError):
            parse_depart_window("20-6")

    def test_max_duration_drops_long_elapsed_time(self) -> None:
        short = card(duration="1 h 20 min", price="90 €")
        long = card(duration="6 h", price="40 €")
        self.assertIsNotNone(_normalize_offer(short, 1, max_duration_hours=4))
        self.assertIsNone(_normalize_offer(long, 1, max_duration_hours=4))

    def test_min_layover_keeps_nonstops_and_drops_short_connections(self) -> None:
        nonstop = card(stops="Nonstop", price="80 €")
        short_hop = card(
            stops="1 stop",
            layover_hours=0.5,
            layover_city="LIS",
            price="70 €",
        )
        long_hop = card(
            stops="1 stop",
            layover_hours=3.0,
            layover_city="LIS",
            price="75 €",
        )
        self.assertIsNotNone(_normalize_offer(nonstop, 1, min_layover_hours=1))
        self.assertIsNone(_normalize_offer(short_hop, 1, min_layover_hours=1))
        self.assertIsNotNone(_normalize_offer(long_hop, 1, min_layover_hours=1))

    def test_via_keeps_parsed_layover_city_or_iata(self) -> None:
        lis = get_airport("LIS")
        assert lis is not None
        city = card(
            stops="1 stop",
            layover_city=lis.city,
            layover_hours=18.0,
            price="74 €",
        )
        iata = card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=3.0,
            price="70 €",
        )
        other = card(
            stops="1 stop",
            layover_city="DXB",
            layover_hours=4.0,
            price="80 €",
        )
        nonstop = card(stops="Nonstop", price="90 €")
        silent = card(stops="1 stop", price="60 €")
        self.assertIsNotNone(_normalize_offer(city, 1, via=("LIS",)))
        self.assertIsNotNone(_normalize_offer(iata, 1, via=("LIS",)))
        self.assertIsNone(_normalize_offer(other, 1, via=("LIS",)))
        self.assertIsNone(_normalize_offer(nonstop, 1, via=("LIS",)))
        self.assertIsNone(_normalize_offer(silent, 1, via=("LIS",)))

    def test_exclude_via_drops_known_layover_and_keeps_unknown(self) -> None:
        dxb = get_airport("DXB")
        assert dxb is not None
        city = card(
            stops="1 stop",
            layover_city=dxb.city,
            layover_hours=5.0,
            price="80 €",
        )
        code = card(
            stops="1 stop",
            layover_city="DXB",
            layover_hours=5.0,
            price="82 €",
        )
        other = card(
            stops="1 stop",
            layover_city="LIS",
            layover_hours=3.0,
            price="70 €",
        )
        silent = card(stops="1 stop", price="60 €")
        nonstop = card(stops="Nonstop", price="90 €")
        self.assertIsNone(_normalize_offer(city, 1, exclude_via=("DXB",)))
        self.assertIsNone(_normalize_offer(code, 1, exclude_via=("DXB",)))
        self.assertIsNotNone(_normalize_offer(other, 1, exclude_via=("DXB",)))
        self.assertIsNotNone(_normalize_offer(silent, 1, exclude_via=("DXB",)))
        self.assertIsNotNone(_normalize_offer(nonstop, 1, exclude_via=("DXB",)))

    def test_via_and_exclude_via_use_leg_layovers(self) -> None:
        silent = card(stops="2 stops", price="120 €")
        ist = get_airport("IST")
        doh = get_airport("DOH")
        assert ist is not None and doh is not None
        with_legs = card(
            stops="2 stops",
            price="120 €",
            legs=(
                RawJourneyLeg(
                    departure="08:00",
                    arrival="22:00",
                    duration="14 h",
                    stops="2 stops",
                    segments=(
                        RawSegment(origin="JFK", destination="IST"),
                        RawSegment(origin="IST", destination="DOH"),
                        RawSegment(origin="DOH", destination="SIN"),
                    ),
                    layovers=(
                        RawLayover(city=ist.city, hours=2.0),
                        RawLayover(city=doh.city, hours=1.5),
                    ),
                ),
            ),
        )
        self.assertIsNotNone(_normalize_offer(with_legs, 2, via=("IST",)))
        self.assertIsNone(_normalize_offer(with_legs, 2, exclude_via=("IST",)))
        self.assertIsNone(_normalize_offer(with_legs, 2, via=("DXB",)))
        self.assertIsNotNone(_normalize_offer(silent, 2, exclude_via=("IST",)))

    def test_parse_via_airports_rejects_unknown_and_overlap(self) -> None:
        self.assertEqual(parse_via_airports("IST,DXB"), ("IST", "DXB"))
        with self.assertRaises(ValueError):
            parse_via_airports("ZZZ")
        with self.assertRaises(ValueError):
            parse_via_airports("")
        query = FlightQuery("JFK", "SIN", date(2026, 11, 3), max_stops=1)
        with self.assertRaises(ValueError):
            search_flights((query,), top=1, via=("IST",), exclude_via=("IST",))

    def test_duration_sort_orders_by_hours_then_fare(self) -> None:
        slow = FlightOffer(
            airline="Slow",
            departure="08:00",
            arrival="14:00",
            price="80 €",
            price_eur=80.0,
            duration="6 h",
            duration_hours=6.0,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        fast = FlightOffer(
            airline="Fast",
            departure="09:00",
            arrival="10:20",
            price="120 €",
            price_eur=120.0,
            duration="1 h 20 min",
            duration_hours=1 + 20 / 60,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        ranked = _rank_offers((slow, fast), top=5, sort="duration")
        self.assertEqual(ranked[0].airline, "Fast")
        self.assertEqual(ranked[1].airline, "Slow")

    def test_price_sort_orders_by_cabin_fare(self) -> None:
        cheap = FlightOffer(
            airline="Cheap",
            departure="21:00",
            arrival="22:20",
            price="40 €",
            price_eur=40.0,
            duration="1 h 20 min",
            duration_hours=1 + 20 / 60,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=70,
            needs_bag_verify=True,
        )
        dear = FlightOffer(
            airline="Dear",
            departure="09:00",
            arrival="10:20",
            price="90 €",
            price_eur=90.0,
            duration="1 h 20 min",
            duration_hours=1 + 20 / 60,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        by_price = _rank_offers((dear, cheap), top=5, sort="price")
        by_fare = _rank_offers((dear, cheap), top=5, sort="fare")
        ranked = _rank_offers((dear, cheap), top=5, sort="ranked")
        self.assertEqual([offer.airline for offer in by_price], ["Cheap", "Dear"])
        self.assertEqual([offer.airline for offer in by_fare], ["Cheap", "Dear"])
        self.assertEqual([offer.airline for offer in ranked], ["Dear", "Cheap"])

    def test_departure_and_arrival_sort_use_clocks(self) -> None:
        late = FlightOffer(
            airline="Late",
            departure="19:40",
            arrival="21:00",
            price="80 €",
            price_eur=80.0,
            duration="1 h 20 min",
            duration_hours=1 + 20 / 60,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        early = FlightOffer(
            airline="Early",
            departure="06:15",
            arrival="22:10",
            price="120 €",
            price_eur=120.0,
            duration="15 h 55 min",
            duration_hours=15 + 55 / 60,
            stops="1 stop",
            stops_count=1,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        by_depart = _rank_offers((late, early), top=5, sort="departure")
        by_arrive = _rank_offers((late, early), top=5, sort="arrival")
        self.assertEqual([offer.airline for offer in by_depart], ["Early", "Late"])
        self.assertEqual([offer.airline for offer in by_arrive], ["Late", "Early"])

    def test_ranked_sort_hides_overnight_hops_on_short_haul(self) -> None:
        nonstop = FlightOffer(
            airline="Iberia",
            departure="09:30",
            arrival="10:50",
            price="€88",
            price_eur=88.0,
            duration="1 hr 20 min",
            duration_hours=1 + 20 / 60,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        overnight = FlightOffer(
            airline="Air Europa",
            departure="21:00",
            arrival="18:00",
            price="€69",
            price_eur=69.0,
            duration="21 hr",
            duration_hours=21.0,
            stops="1 stop",
            stops_count=1,
            layover_city="Palma",
            layover_hours=18.0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        ranked = _rank_offers((overnight, nonstop), top=5, sort="ranked")
        self.assertEqual([offer.airline for offer in ranked], ["Iberia"])
        fare = _rank_offers((overnight, nonstop), top=5, sort="fare")
        self.assertEqual(fare[0].airline, "Air Europa")

    def test_ranked_sort_keeps_long_haul_one_stops(self) -> None:
        one_stop = FlightOffer(
            airline="Korean Air",
            departure="10:00",
            arrival="16:00",
            price="€400",
            price_eur=400.0,
            duration="17 hr",
            duration_hours=17.0,
            stops="1 stop",
            stops_count=1,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        two_stop = FlightOffer(
            airline="China Southern",
            departure="21:00",
            arrival="21:50",
            price="€314",
            price_eur=314.0,
            duration="17 hr 50 min",
            duration_hours=17 + 50 / 60,
            stops="2 stops",
            stops_count=2,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        ranked = _rank_offers((one_stop, two_stop), top=5, sort="ranked")
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].airline, "China Southern")


def _fare_offer(
    *,
    airline: str,
    price_eur: float,
    stops_count: int,
    duration_hours: float = 2.0,
    duration: str = "2 h",
    layover_city: str | None = None,
    layover_hours: float | None = None,
    baggage_buffer_eur: int = 0,
    needs_bag_verify: bool = False,
    stops: str | None = None,
) -> FlightOffer:
    if stops is None:
        if stops_count == 0:
            stops = "Nonstop"
        elif stops_count == 1:
            stops = "1 stop"
        else:
            stops = f"{stops_count} stops"
    return FlightOffer(
        airline=airline,
        departure="08:00",
        arrival="10:00",
        price=f"{price_eur:.0f} €",
        price_eur=price_eur,
        duration=duration,
        duration_hours=duration_hours,
        stops=stops,
        stops_count=stops_count,
        layover_city=layover_city,
        layover_hours=layover_hours,
        baggage_buffer_eur=baggage_buffer_eur,
        needs_bag_verify=needs_bag_verify,
    )


class StopsCompareTests(unittest.TestCase):
    def test_picks_cheapest_cabin_fare_in_each_bucket(self) -> None:
        compare = compare_nonstop_vs_one_stop(
            (
                _fare_offer(airline="Iberia", price_eur=120.0, stops_count=0),
                _fare_offer(airline="Vueling", price_eur=90.0, stops_count=0),
                _fare_offer(airline="Ryanair", price_eur=55.0, stops_count=1),
                _fare_offer(airline="Air Europa", price_eur=80.0, stops_count=1),
            )
        )
        assert compare is not None
        assert compare.nonstop is not None
        assert compare.one_stop is not None
        self.assertEqual(compare.nonstop.airline, "Vueling")
        self.assertEqual(compare.nonstop.price_eur, 90.0)
        self.assertEqual(compare.one_stop.airline, "Ryanair")
        self.assertEqual(compare.one_stop.price_eur, 55.0)

    def test_uses_fare_not_ranked_buffer(self) -> None:
        compare = compare_nonstop_vs_one_stop(
            (
                _fare_offer(
                    airline="Ryanair",
                    price_eur=40.0,
                    stops_count=0,
                    baggage_buffer_eur=70,
                    needs_bag_verify=True,
                ),
                _fare_offer(airline="Iberia", price_eur=100.0, stops_count=0),
            )
        )
        assert compare is not None
        assert compare.nonstop is not None
        self.assertEqual(compare.nonstop.airline, "Ryanair")
        self.assertEqual(compare.nonstop.price_eur, 40.0)
        self.assertIsNone(compare.one_stop)

    def test_omits_empty_one_stop_side(self) -> None:
        compare = compare_nonstop_vs_one_stop(
            (_fare_offer(airline="Iberia", price_eur=88.0, stops_count=0),)
        )
        assert compare is not None
        assert compare.nonstop is not None
        self.assertEqual(compare.nonstop.airline, "Iberia")
        self.assertIsNone(compare.one_stop)
        self.assertEqual(set(compare.to_dict()), {"nonstop"})

    def test_omits_empty_nonstop_side(self) -> None:
        compare = compare_nonstop_vs_one_stop(
            (_fare_offer(airline="Ryanair", price_eur=49.0, stops_count=1),)
        )
        assert compare is not None
        assert compare.one_stop is not None
        self.assertIsNone(compare.nonstop)
        self.assertEqual(compare.one_stop.airline, "Ryanair")
        self.assertEqual(set(compare.to_dict()), {"one_stop"})

    def test_omits_block_when_no_zero_or_one_stop(self) -> None:
        two_stop = _fare_offer(airline="China Southern", price_eur=314.0, stops_count=2)
        unknown = FlightOffer(
            airline="Mystery",
            departure="08:00",
            arrival="10:00",
            price="40 €",
            price_eur=40.0,
            duration="2 h",
            duration_hours=2.0,
            stops="Unknown",
            stops_count=None,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        self.assertIsNone(compare_nonstop_vs_one_stop((two_stop, unknown)))

    def test_tie_breaks_on_shorter_duration(self) -> None:
        compare = compare_nonstop_vs_one_stop(
            (
                _fare_offer(
                    airline="Slow",
                    price_eur=100.0,
                    stops_count=0,
                    duration_hours=3.0,
                    duration="3 h",
                ),
                _fare_offer(
                    airline="Fast",
                    price_eur=100.0,
                    stops_count=0,
                    duration_hours=1.5,
                    duration="1 h 30 min",
                ),
            )
        )
        assert compare is not None
        assert compare.nonstop is not None
        self.assertEqual(compare.nonstop.airline, "Fast")

    def test_search_keeps_hidden_ranked_one_stop_in_compare(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 1): (
                    card(
                        airline="Iberia",
                        price="88 €",
                        duration="1 hr 20 min",
                        stops="Nonstop",
                        departure="09:30",
                        arrival="10:50",
                    ),
                    card(
                        airline="Air Europa",
                        price="69 €",
                        duration="21 hr",
                        stops="1 stop",
                        layover_city="Palma",
                        layover_hours=18.0,
                        departure="21:00",
                        arrival="18:00",
                    ),
                ),
            }
        )
        report = _run_search(
            (query,),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
            buffer_eur=0,
        )
        outcome = report.queries[0]
        self.assertIsInstance(outcome, QuerySuccess)
        self.assertEqual([offer.airline for offer in outcome.offers], ["Iberia"])
        compare = outcome.stops_compare
        assert compare is not None
        assert compare.nonstop is not None
        assert compare.one_stop is not None
        self.assertEqual(compare.nonstop.airline, "Iberia")
        self.assertEqual(compare.nonstop.price_eur, 88.0)
        self.assertEqual(compare.one_stop.airline, "Air Europa")
        self.assertEqual(compare.one_stop.price_eur, 69.0)
        payload = outcome.to_dict()["stops_compare"]
        self.assertEqual(payload["nonstop"]["price_eur"], 88.0)
        self.assertEqual(payload["one_stop"]["price_eur"], 69.0)
        self.assertEqual(payload["one_stop"]["layover_city"], "Palma")

    def test_search_with_max_stops_zero_omits_one_stop_side(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=0)
        source = FakeSource(
            {
                ("MAD", "BCN", "2026-09-01", 0): (
                    card(airline="Iberia", price="88 €", stops="Nonstop"),
                    card(airline="Ryanair", price="49 €", stops="1 stop"),
                ),
            }
        )
        report = _run_search(
            (query,),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10),
            buffer_eur=0,
        )
        outcome = report.queries[0]
        self.assertIsInstance(outcome, QuerySuccess)
        assert outcome.stops_compare is not None
        assert outcome.stops_compare.nonstop is not None
        self.assertEqual(outcome.stops_compare.nonstop.price_eur, 88.0)
        self.assertIsNone(outcome.stops_compare.one_stop)
        self.assertNotIn("one_stop", outcome.to_dict()["stops_compare"])


if __name__ == "__main__":
    unittest.main()
