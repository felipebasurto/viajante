from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import sys
import tempfile
import time
import unittest
import warnings
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from viajante import evidence, mcp_handlers
from viajante.dates import search_dates, search_flex
from viajante.envelope import (
    COMPLETENESS,
    EMPTY_NOTES,
    EMPTY_REASONS,
    OBSERVED_BASES,
    STATUSES,
    EnvelopeShapeError,
    stamp_local,
    stamp_search,
    stamp_split,
)
from viajante.explore import search_explore
from viajante.flights import search_flights
from viajante.google_flights import (
    GoogleFlightsBlocked,
    GoogleFlightsMarkupError,
    NoFlightsFound,
    RawFlightCard,
)
from viajante.google_flights_rpc import CompactCalendarDay, CompactExplorePlace, CompactParseMiss
from viajante.google_hotels_rpc import EmptyHotelResults, HotelsBlocked
from viajante.hotels import _run_search
from viajante.models import (
    FlightQuery,
    HiddenCityOffer,
    HiddenCityReport,
    HotelPage,
    HotelQuery,
    HotelRoomRate,
    HotelRoomsReport,
    HotelSearchReport,
    RawHotelCard,
    SearchError,
    SearchErrorCode,
    TripSearchReport,
)
from viajante.orchestration import classify_failure
from viajante.ratelimit import (
    GOOGLE_RATE_LIMIT_FILE,
    NOT_SENT,
    SKIPLAGGED_RATE_LIMIT_FILE,
    note_rate_limited,
    rate_limit_advice,
)
from viajante.storage import reports_payload

ENVELOPE_KEYS = {
    "status",
    "completeness",
    "empty_reason",
    "empty_note",
    "error_code",
    "retry_after",
    "retry_after_seconds",
    "observed_at",
    "observed_at_basis",
}
ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000.0
TODAY = date.today()
DAY = TODAY + timedelta(days=30)


def _card(price: str = "$128", airline: str = "Delta") -> RawFlightCard:
    return RawFlightCard(
        airline=airline,
        departure="07:00",
        arrival="08:00",
        duration="1 hr",
        stops="Nonstop",
        price=price,
    )


class _FlightSource:
    """Scripted per-destination responses; an Exception value is raised."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.config = SimpleNamespace(html_lang="en", currency="USD")

    def fetch(self, query):
        response = self.responses[query.destination]
        if isinstance(response, Exception):
            raise response
        return response

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def _flights(
    responses: dict[str, object],
    *,
    packaged: bool = False,
    **kwargs: object,
) -> dict:
    queries = tuple(FlightQuery("JFK", dest, DAY, max_stops=1) for dest in responses)
    with (
        patch("viajante.flights.GoogleFlightsHttpSource", return_value=_FlightSource(responses)),
        patch("viajante.flights.chromium_installed", return_value=False),
    ):
        report = search_flights(queries, top=3, fetch="sweep", currency="USD", **kwargs)
    return stamp_search(reports_payload(report), now=NOW)


def _hotel_page(*cards: RawHotelCard) -> HotelPage:
    return HotelPage(cards=cards)


def _hotel_card(rating: str = "Rating: 8.7") -> RawHotelCard:
    return RawHotelCard(
        title="Casa Azul",
        address="Centro, Lisboa",
        total_price="$400",
        rating=rating,
        details="Free cancellation · Entire home · 2 bedrooms · 1 bathroom · 3 beds",
        link="https://www.booking.com/hotel/pt/casa-azul.html",
    )


class _HotelSource:
    def __init__(self, response: object) -> None:
        self.response = response
        self.config = SimpleNamespace(html_lang="en", currency="USD")

    def fetch(self, query, applied, limit):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def _hotels(response: object, **query_kwargs: object) -> HotelSearchReport:
    query = HotelQuery("Lisboa", DAY, DAY + timedelta(days=3), **query_kwargs)
    return _run_search(
        (query,),
        top=5,
        source=_HotelSource(response),
        sleep=lambda _seconds: None,
        random_gen=Random(0),
        now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        currency="USD",
    )


def _hotel_env(response: object, **query_kwargs: object) -> dict:
    return stamp_search(reports_payload(_hotels(response, **query_kwargs)), now=NOW)


class _StateDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory()
        patcher = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self.state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.state.cleanup)


class EnvelopeShapeTests(unittest.TestCase):
    def assertEnvelope(self, payload: dict, **expected: object) -> None:
        self.assertTrue(ENVELOPE_KEYS <= set(payload), ENVELOPE_KEYS - set(payload))
        self.assertIn(payload["status"], STATUSES)
        self.assertIn(payload["completeness"], COMPLETENESS)
        if payload["empty_reason"] is not None:
            self.assertIn(payload["empty_reason"], EMPTY_REASONS)
            self.assertEqual(payload["empty_note"], EMPTY_NOTES[payload["empty_reason"]])
        else:
            self.assertIsNone(payload["empty_note"])
        if payload["observed_at_basis"] is not None:
            self.assertIn(payload["observed_at_basis"], OBSERVED_BASES)
        for key, value in expected.items():
            self.assertEqual(payload[key], value, key)

    def test_rows_with_offers_are_ok_and_complete(self) -> None:
        payload = _flights({"LHR": (_card(),)})
        self.assertEnvelope(
            payload,
            status="ok",
            completeness="complete",
            empty_reason=None,
            error_code=None,
            retry_after=None,
            observed_at_basis="fetch",
        )
        self.assertEqual(payload["observed_at"], payload["searched_at"])
        self.assertNotIn("empty_reason", payload["queries"][0])
        self.assertEqual(payload["queries"][0]["status"], "ok")

    def test_existing_keys_survive_the_stamp(self) -> None:
        payload = _flights({"LHR": (_card(),)})
        existing = {
            "schema_version",
            "searched_at",
            "currency",
            "locale",
            "fetch_backend",
            "fetch_ms",
            "coverage",
            "queries",
        }
        self.assertEqual(set(payload) - ENVELOPE_KEYS, existing)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["queries"][0]["status"], "ok")


class FlightEmptyReasonTests(_StateDirCase):
    def test_provider_empty_when_the_provider_answered_nothing(self) -> None:
        payload = _flights({"LHR": NoFlightsFound()})
        self.assertEqual(payload["queries"][0]["error"]["code"], "no_results")
        self.assertEqual(payload["queries"][0]["empty_reason"], "provider_empty")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("no_results", "complete", "provider_empty"),
        )
        self.assertEqual(payload["error_code"], "no_results")

    def test_provider_empty_for_a_success_with_zero_raw_cards(self) -> None:
        payload = _flights({"LHR": ()})
        self.assertEqual(payload["queries"][0]["raw_count"], 0)
        self.assertEqual(payload["empty_reason"], "provider_empty")
        self.assertEqual(payload["status"], "no_results")

    def test_filtered_out_when_local_filters_remove_every_provider_row(self) -> None:
        payload = _flights({"LHR": (_card(),)}, max_duration_hours=0.5)
        row = payload["queries"][0]
        self.assertEqual((row["status"], row["raw_count"], row["eligible_count"]), ("ok", 1, 0))
        self.assertEqual(row["empty_reason"], "filtered_out")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("no_results", "complete", "filtered_out"),
        )

    def test_filtered_out_when_named_exclusion_leaves_nothing_to_search(self) -> None:
        payload = _flights({"LHR": (_card(),)}, exclude_airports=("LHR",))
        self.assertEqual(payload["queries"][0]["raw_count"], 0)
        self.assertEqual(payload["queries"][0]["empty_reason"], "filtered_out")
        self.assertEqual(payload["empty_reason"], "filtered_out")

    def test_not_loaded_when_the_provider_blocks(self) -> None:
        payload = _flights({"LHR": GoogleFlightsBlocked("consent wall")})
        self.assertEqual(payload["queries"][0]["empty_reason"], "not_loaded")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "blocked", "not_loaded"),
        )
        self.assertEqual(payload["error_code"], "blocked")
        self.assertIsNone(payload["retry_after"])

    def test_not_loaded_when_markup_drifts_or_the_provider_rejects(self) -> None:
        payload = _flights({"LHR": GoogleFlightsMarkupError("no grid")})
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("failed", "not_loaded", "markup_drift"),
        )

    def test_timeout_has_its_own_status(self) -> None:
        payload = _flights({"LHR": TimeoutError("read timed out")})
        self.assertTrue(payload["queries"][0]["error"]["timeout"])
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("timeout", "blocked", "not_loaded"),
        )
        self.assertEqual(payload["error_code"], "fetch_failed")

    def test_a_429_is_rate_limited_and_names_the_known_cooldown(self) -> None:
        start = time.time()
        advice = rate_limit_advice(note_rate_limited(300.0, start, file=GOOGLE_RATE_LIMIT_FILE))
        responses = {"LHR": GoogleFlightsBlocked(advice, status=429)}
        queries = (FlightQuery("JFK", "LHR", DAY, max_stops=1),)
        with (
            patch(
                "viajante.flights.GoogleFlightsHttpSource", return_value=_FlightSource(responses)
            ),
            patch("viajante.flights.chromium_installed", return_value=False),
        ):
            report = search_flights(queries, top=3, fetch="sweep", currency="USD")
        payload = stamp_search(reports_payload(report), now=start + 60)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("rate_limited", "blocked", "not_loaded"),
        )
        error = payload["queries"][0]["error"]
        self.assertIn(payload["retry_after_seconds"], (300, 301))
        self.assertEqual(payload["retry_after_seconds"], error["retry_after_seconds"])
        retry_after = datetime.fromtimestamp(math.ceil(start + 300), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.assertEqual(payload["retry_after"], retry_after)
        self.assertEqual(payload["queries"][0]["error"]["retry_after"], retry_after)

    def test_a_proxied_429_during_a_direct_cooldown_gets_no_top_level_retry_after(self) -> None:
        note_rate_limited(300.0, time.time(), file=GOOGLE_RATE_LIMIT_FILE)
        payload = _flights({"LHR": GoogleFlightsBlocked("HTTP 429", status=429)})
        error = payload["queries"][0]["error"]
        self.assertTrue(error["rate_limited"])
        self.assertNotIn("retry_after", error)
        self.assertEqual(payload["status"], "rate_limited")
        self.assertIsNone(payload["retry_after"])
        self.assertIsNone(payload["retry_after_seconds"])

    def test_a_429_without_a_recorded_cooldown_invents_no_retry_after(self) -> None:
        payload = _flights({"LHR": GoogleFlightsBlocked("HTTP 429", status=429)})
        self.assertEqual(payload["status"], "rate_limited")
        self.assertIsNone(payload["retry_after"])
        self.assertIsNone(payload["retry_after_seconds"])

    def test_partial_when_some_queries_fail_and_others_return_rows(self) -> None:
        payload = _flights({"LHR": (_card(),), "CDG": GoogleFlightsBlocked("wall")})
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "partial", None),
        )
        self.assertEqual(payload["error_code"], "blocked")
        self.assertNotIn("empty_reason", payload["queries"][0])
        self.assertEqual(payload["queries"][1]["empty_reason"], "not_loaded")

    def test_a_failed_query_beside_an_empty_one_never_reads_as_no_flights(self) -> None:
        payload = _flights({"LHR": NoFlightsFound(), "CDG": GoogleFlightsBlocked("wall")})
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "partial", "not_loaded"),
        )

    def test_filtered_beside_provider_empty_is_reported_as_filtered(self) -> None:
        payload = _flights(
            {"LHR": NoFlightsFound(), "CDG": (_card(),)},
            max_duration_hours=0.5,
        )
        self.assertEqual(payload["empty_reason"], "filtered_out")
        self.assertIsNone(payload["error_code"])

    def test_a_cooldown_replay_sent_nothing_so_nothing_was_observed(self) -> None:
        blocked = GoogleFlightsBlocked(f"{NOT_SENT}Google is rate limiting.", status=429)
        payload = _flights({"LHR": blocked})
        self.assertEqual(payload["status"], "rate_limited")
        self.assertIsNone(payload["observed_at"])
        self.assertIsNone(payload["observed_at_basis"])

    def test_a_request_that_was_sent_is_observed_on_the_fetch_clock(self) -> None:
        payload = _flights({"LHR": GoogleFlightsBlocked("HTTP 429", status=429)})
        self.assertEqual(payload["observed_at_basis"], "fetch")
        self.assertIsNotNone(payload["observed_at"])

    def test_envelope_agrees_with_the_search_coverage_counters(self) -> None:
        payload = _flights(
            {"LHR": (_card(),), "CDG": NoFlightsFound(), "AMS": GoogleFlightsBlocked("wall")}
        )
        coverage = payload["coverage"]
        self.assertEqual((coverage["succeeded"], coverage["empty"], coverage["failed"]), (1, 1, 1))
        self.assertEqual(payload["completeness"] == "partial", coverage["failed"] > 0)

    def test_nearby_fan_out_payload_is_stamped_once_at_the_top(self) -> None:
        one = _flights({"LHR": (_card(),)})
        two = _flights({"CDG": NoFlightsFound()})
        payload = stamp_search({"queries": [one, two]}, now=NOW)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "complete", None),
        )
        self.assertEqual(payload["observed_at"], max(one["searched_at"], two["searched_at"]))


class HotelEmptyReasonTests(_StateDirCase):
    def test_provider_empty(self) -> None:
        payload = _hotel_env(EmptyHotelResults())
        self.assertEqual(payload["queries"][0]["empty_reason"], "provider_empty")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("no_results", "complete", "provider_empty"),
        )

    def test_filtered_out_when_the_rating_filter_removes_every_row(self) -> None:
        payload = _hotel_env(_hotel_page(_hotel_card()), min_rating=9.5)
        row = payload["queries"][0]
        self.assertEqual((row["raw_count"], row["eligible_count"]), (1, 0))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("no_results", "complete", "filtered_out"),
        )

    def test_rows_are_ok(self) -> None:
        payload = _hotel_env(_hotel_page(_hotel_card()))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "complete", None),
        )

    def test_not_loaded_on_a_hotel_rate_limit(self) -> None:
        start = time.time()
        advice = rate_limit_advice(note_rate_limited(120.0, start, file=GOOGLE_RATE_LIMIT_FILE))
        report = _hotels(HotelsBlocked(advice, rate_limited=True))
        payload = stamp_search(reports_payload(report), now=start)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("rate_limited", "blocked", "not_loaded"),
        )
        self.assertIn(payload["retry_after_seconds"], (120, 121))
        self.assertEqual(
            payload["retry_after_seconds"], payload["queries"][0]["error"]["retry_after_seconds"]
        )

    def test_not_loaded_on_a_booking_timeout(self) -> None:
        from viajante.booking import BookingResultsTimeout

        payload = _hotel_env(BookingResultsTimeout("no cards"))
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("timeout", "not_loaded", "fetch_failed"),
        )


class DatesFlexExploreTests(_StateDirCase):
    class _Source:
        def __init__(self, calendar: object, cards: dict | None = None) -> None:
            self.calendar = calendar
            self.cards = cards or {}
            self.config = SimpleNamespace(html_lang="en", currency="USD")

        def fetch_calendar(self, query, start, end):
            if isinstance(self.calendar, Exception):
                raise self.calendar
            return self.calendar

        def fetch(self, query):
            response = self.cards.get(query.departure_date, ())
            if isinstance(response, Exception):
                raise response
            return response

        def close(self) -> None:
            pass

    def _dates(self, source, **kwargs: object) -> dict:
        report = search_dates(
            "JFK", "LHR", DAY, DAY + timedelta(days=1), source=source, currency="USD", **kwargs
        )
        return stamp_search(reports_payload(report), now=NOW)

    def test_calendar_cells_without_a_price_are_never_provider_empty(self) -> None:
        # An unpriced or missing calendar cell does not prove the provider has no flights.
        payload = self._dates(
            self._Source((CompactCalendarDay(DAY, None),))  # the second day is missing entirely
        )
        self.assertEqual({row["empty_reason"] for row in payload["days"]}, {"not_loaded"})
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("no_results", "partial", "not_loaded"),
        )
        self.assertNotIn("provider_empty", str(payload["days"]))

    def test_priced_calendar_is_ok(self) -> None:
        payload = self._dates(
            self._Source(
                (CompactCalendarDay(DAY, 150.0), CompactCalendarDay(DAY + timedelta(1), None))
            )
        )
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "partial", None),
        )

    def test_sweep_days_filtered_locally_are_filtered_out_not_empty(self) -> None:
        source = self._Source(
            CompactParseMiss("drift"),
            cards={DAY: (_card(),), DAY + timedelta(1): ()},
        )
        payload = self._dates(source, max_duration_hours=0.5)
        reasons = [row["empty_reason"] for row in payload["days"]]
        self.assertEqual(reasons, ["filtered_out", "provider_empty"])
        self.assertEqual(payload["empty_reason"], "filtered_out")
        self.assertEqual(payload["status"], "no_results")

    def test_a_blocked_calendar_is_not_loaded_and_blocked(self) -> None:
        payload = self._dates(self._Source(GoogleFlightsBlocked("wall")))
        self.assertTrue(all(row["status"] == "error" for row in payload["days"]))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "blocked", "not_loaded"),
        )

    def test_a_window_with_missing_days_is_partial(self) -> None:
        payload = self._dates(self._Source((CompactCalendarDay(DAY, 150.0),)))
        payload["coverage"] = {**payload["coverage"], "complete": False}
        restamped = stamp_search(payload, now=NOW)
        self.assertEqual((restamped["status"], restamped["completeness"]), ("ok", "partial"))

    def test_flex_calendar_miss_is_failed_not_no_results(self) -> None:
        source = self._Source(CompactParseMiss("drift"))
        report = search_flex("JFK", "LHR", DAY, 1, source=source, currency="USD")
        payload = stamp_search(reports_payload(report), now=NOW)
        self.assertEqual(payload["error"]["code"], "markup_drift")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("failed", "blocked", "not_loaded"),
        )

    def test_flex_window_with_no_priced_day_is_not_loaded_not_no_flights(self) -> None:
        source = self._Source(())
        report = search_flex("JFK", "LHR", DAY, 1, source=source, currency="USD")
        payload = stamp_search(reports_payload(report), now=NOW)
        self.assertEqual(payload["empty_reason"], "not_loaded")
        self.assertEqual((payload["status"], payload["completeness"]), ("no_results", "partial"))

    class _ExploreSource:
        def __init__(self, places: object, prices: dict | None = None) -> None:
            self.places = places
            self.prices = prices or {}
            self.config = SimpleNamespace(html_lang="en", currency="USD")

        def fetch_explore(self, origin, departure_date, **kwargs: object):
            if isinstance(self.places, Exception):
                raise self.places
            return self.places

        def fetch(self, query):
            response = self.prices.get(query.destination, ())
            if isinstance(response, Exception):
                raise response
            return response

        def close(self) -> None:
            pass

    def _explore(self, source, **kwargs: object) -> dict:
        report = search_explore("JFK", DAY, days=7, top=2, source=source, **kwargs)
        return stamp_search(reports_payload(report), now=NOW)

    def test_explore_empty_catalog_is_provider_empty(self) -> None:
        payload = self._explore(self._ExploreSource(()))
        self.assertEqual(payload["empty_reason"], "provider_empty")
        self.assertEqual(payload["status"], "no_results")

    def test_explore_catalog_removed_by_named_exclusion_is_filtered_out(self) -> None:
        source = self._ExploreSource((CompactExplorePlace("LHR", "London", "United Kingdom"),))
        payload = self._explore(source, exclude_airports=("LHR",))
        self.assertEqual(payload["empty_reason"], "filtered_out")

    def test_explore_origin_excluded_is_filtered_out(self) -> None:
        payload = self._explore(self._ExploreSource(()), exclude_airports=("JFK",))
        self.assertEqual(payload["empty_reason"], "filtered_out")

    def test_explore_price_cap_removing_every_priced_dest_is_filtered_out(self) -> None:
        source = self._ExploreSource(
            (CompactExplorePlace("LHR", "London", "United Kingdom"),),
            prices={"LHR": (_card(price="$900"),)},
        )
        payload = self._explore(source, price_cap=100)
        self.assertEqual(payload["destinations"], [])
        self.assertEqual(payload["empty_reason"], "filtered_out")

    def test_explore_dest_with_no_provider_cards_is_provider_empty(self) -> None:
        source = self._ExploreSource((CompactExplorePlace("LHR", "London", "United Kingdom"),))
        payload = self._explore(source, price_cap=100)
        self.assertEqual(payload["empty_reason"], "provider_empty")

    def test_explore_with_some_shops_answered_and_some_failed_is_partial(self) -> None:
        source = self._ExploreSource(
            (
                CompactExplorePlace("LHR", "London", "United Kingdom"),
                CompactExplorePlace("CDG", "Paris", "France"),
            ),
            prices={"CDG": GoogleFlightsBlocked("wall")},
        )
        payload = self._explore(source, price_cap=100)
        self.assertEqual(payload["destinations"], [])
        self.assertEqual(payload["coverage"]["empty"], 1)
        self.assertEqual(payload["coverage"]["failed"], 1)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "partial", "not_loaded"),
        )

    def test_explore_unpriced_catalog_dest_after_a_failed_shop_is_not_ok(self) -> None:
        source = self._ExploreSource(
            (CompactExplorePlace("LHR", "London", "United Kingdom"),),
            prices={"LHR": GoogleFlightsBlocked("wall")},
        )
        payload = self._explore(source)
        self.assertEqual(
            [(d["iata"], d["price"]) for d in payload["destinations"]], [("LHR", None)]
        )
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "blocked", "not_loaded"),
        )

    def test_explore_priced_dest_beside_an_unpriced_one_is_ok_and_partial(self) -> None:
        source = self._ExploreSource(
            (
                CompactExplorePlace("LHR", "London", "United Kingdom"),
                CompactExplorePlace("CDG", "Paris", "France"),
            ),
            prices={"LHR": (_card(),), "CDG": GoogleFlightsBlocked("wall")},
        )
        payload = self._explore(source)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "partial", None),
        )
        # An ok/partial result keeps the worst failure's code while empty_reason stays null.
        self.assertEqual(payload["error_code"], "blocked")

    def test_explore_catalog_failure_is_not_loaded(self) -> None:
        payload = self._explore(self._ExploreSource(GoogleFlightsBlocked("wall")))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "blocked", "not_loaded"),
        )

    def test_explore_rows_are_ok_and_the_shortlist_scope_is_not_a_failure(self) -> None:
        source = self._ExploreSource(
            (CompactExplorePlace("LHR", "London", "United Kingdom"),),
            prices={"LHR": (_card(),)},
        )
        payload = self._explore(source)
        self.assertFalse(payload["coverage"]["complete"])
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "complete", None),
        )


class SkiplaggedAndTripTests(_StateDirCase):
    def _hidden(self, now: float = NOW, **kwargs: object) -> dict:
        report = HiddenCityReport(
            searched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            origin="JFK",
            destination="MIA",
            departure_date=DAY,
            **kwargs,
        )
        return stamp_search(reports_payload(report), now=now)

    def test_hidden_city_no_results_is_provider_empty(self) -> None:
        payload = self._hidden(error=SearchError(SearchErrorCode.NO_RESULTS, "none"))
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("no_results", "provider_empty", "no_results"),
        )

    def test_hidden_city_currency_mismatch_is_filtered_out_with_its_code(self) -> None:
        payload = self._hidden(
            currency="USD", error=SearchError(SearchErrorCode.CURRENCY_MISMATCH, "keep GBP")
        )
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("no_results", "filtered_out", "currency_mismatch"),
        )

    def test_hidden_city_rate_limit_reads_the_skiplagged_cooldown_only(self) -> None:
        start = float(math.floor(time.time()))
        note_rate_limited(60.0, start, file=SKIPLAGGED_RATE_LIMIT_FILE)
        error = SearchError(
            SearchErrorCode.BLOCKED, "429", rate_limited=True, retry_until=start + 60
        )
        payload = self._hidden(error=error, now=start)
        self.assertEqual((payload["status"], payload["retry_after_seconds"]), ("rate_limited", 60))

    def test_hidden_city_rows_are_ok(self) -> None:
        offer = HiddenCityOffer(
            "JFK", "MIA", DAY, 99.0, currency="USD", evidence="confirmed", airline="Delta"
        )
        payload = self._hidden(offers=(offer,), currency="USD")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "complete", None),
        )

    def _rooms(self, **kwargs: object) -> dict:
        report = HotelRoomsReport(
            searched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            hotel_id=kwargs.pop("hotel_id", "7"),
            check_in=DAY,
            check_out=DAY + timedelta(days=2),
            adults=2,
            rooms=1,
            currency="USD",
            **kwargs,
        )
        return stamp_search(reports_payload(report), now=NOW)

    def test_rooms_with_rates_are_ok(self) -> None:
        rate = HotelRoomRate("Double", 300.0, 150.0, None, 2, True, True, (), None)
        payload = self._rooms(rates=(rate,))
        self.assertEqual((payload["status"], payload["empty_reason"]), ("ok", None))

    def test_rooms_by_id_with_no_match_is_provider_empty(self) -> None:
        payload = self._rooms(error=SearchError(SearchErrorCode.NO_RESULTS, "none"))
        self.assertEqual(payload["empty_reason"], "provider_empty")

    def test_rooms_name_that_resolves_no_hotel_is_never_called_no_hotels(self) -> None:
        payload = self._rooms(
            hotel_id=None,
            requested_name="Casa Azul",
            error=SearchError(SearchErrorCode.NO_RESULTS, "no exact match"),
        )
        self.assertEqual(payload["empty_reason"], "filtered_out")
        self.assertIsNone(payload["error_code"])

    def test_rooms_failure_is_not_loaded(self) -> None:
        payload = self._rooms(error=SearchError(SearchErrorCode.MARKUP_DRIFT, "drift"))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("failed", "blocked", "not_loaded"),
        )

    def _trip(self, flights: dict, hotels: HotelSearchReport) -> dict:
        report = TripSearchReport(
            searched_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            flights=SimpleNamespace(to_dict=lambda: flights),
            hotels=hotels,
            currency="USD",
        )
        return stamp_search(reports_payload(report), now=NOW)

    def test_trip_with_a_hotel_row_and_filtered_flights_is_ok(self) -> None:
        flights = _flights({"LHR": (_card(),)}, max_duration_hours=0.5)
        payload = self._trip(flights, _hotels(_hotel_page(_hotel_card())))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("ok", "complete", None),
        )

    def test_trip_failed_hotel_beside_an_empty_flight_is_not_loaded(self) -> None:
        flights = _flights({"LHR": NoFlightsFound()})
        payload = self._trip(flights, _hotels(HotelsBlocked("wall")))
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("blocked", "partial", "not_loaded"),
        )


class LocalToolTests(unittest.TestCase):
    def test_local_payloads_run_ok_with_no_provider_fields(self) -> None:
        payload = stamp_local({"x": 1})
        self.assertEqual(set(payload) - {"x"}, ENVELOPE_KEYS)
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "complete"))
        for key in ENVELOPE_KEYS - {"status", "completeness"}:
            self.assertIsNone(payload[key])

    def test_split_stay_costs_with_unallocated_nights_is_partial(self) -> None:
        roster = {"2027-01-01": ["ana"], "2027-01-02": ["ana"]}
        stays = [{"name": "A", "check_in": "2027-01-01", "check_out": "2027-01-02", "total": 100}]
        payload = mcp_handlers.split_stay_costs_tool(stays, roster, "USD")
        self.assertEqual(payload["unallocated_nights"], ["2027-01-02"])
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "partial"))

    def test_split_stay_costs_fully_covered_is_complete(self) -> None:
        roster = {"2027-01-01": ["ana"]}
        stays = [{"name": "A", "check_in": "2027-01-01", "check_out": "2027-01-02", "total": 100}]
        payload = mcp_handlers.split_stay_costs_tool(stays, roster, "USD")
        self.assertEqual(payload["completeness"], "complete")

    def test_validation_with_unknown_evidence_is_partial(self) -> None:
        report = MagicMock()
        report.to_dict.return_value = {"feasible": None, "checks": []}
        with patch("viajante.mcp_handlers.validate_itinerary", return_value=report):
            payload = mcp_handlers.validate_itinerary_tool([], {})
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "partial"))
        report.to_dict.return_value = {"feasible": False, "checks": []}
        with patch("viajante.mcp_handlers.validate_itinerary", return_value=report):
            payload = mcp_handlers.validate_itinerary_tool([], {})
        self.assertEqual(payload["completeness"], "complete")

    def test_every_offline_handler_carries_the_envelope(self) -> None:
        payloads = [
            mcp_handlers.plan_stay_blocks_tool({"2027-01-01": ["ana"]}),
            mcp_handlers.lookup_transfers_tool("aeroplan", 1000),
            mcp_handlers.compare_awards_tool(
                {
                    "origin": "JFK",
                    "destination": "LHR",
                    "departure_date": DAY.isoformat(),
                    "program": "aeroplan",
                    "points": 70000,
                    "evidence": "user_supplied",
                }
            ),
        ]
        for payload in payloads:
            with self.subTest(keys=sorted(payload)[:3]):
                self.assertTrue(ENVELOPE_KEYS <= set(payload))
                self.assertEqual(payload["status"], "ok")


class UnknownShapeTests(unittest.TestCase):
    def test_an_unrecognised_payload_raises_instead_of_reading_as_no_results(self) -> None:
        for payload in (
            {"outcome": "same_price", "price": 291},
            {"offers": [], "chosen_date": DAY.isoformat()},
            {},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    stamp_search(dict(payload))

    def test_a_payload_with_only_a_typed_error_is_recognised(self) -> None:
        payload = stamp_search({"error": {"code": "blocked", "message": "wall"}}, now=NOW)
        self.assertEqual((payload["status"], payload["empty_reason"]), ("blocked", "not_loaded"))


class VerifyAnswerEnvelopeTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence._ledger.clear()
        self.addCleanup(evidence._ledger.clear)

    def test_nothing_recorded_is_a_failed_verification_not_status_ok_beside_ok_false(self) -> None:
        payload = mcp_handlers.verify_answer_tool("JFK-LHR is 291 USD")
        self.assertIs(payload["ok"], False)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["error_code"]),
            ("failed", "blocked", "no_search_recorded"),
        )
        self.assertIsNone(payload["empty_reason"])

    def test_unowned_claims_fail_with_a_complete_check(self) -> None:
        evidence.record({"queries": [{"query": {"origin": "JFK"}, "offers": [{"price": 291}]}]})
        payload = mcp_handlers.verify_answer_tool("Costs USD 99999")
        self.assertIs(payload["ok"], False)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["error_code"]),
            ("failed", "complete", "unowned_claims"),
        )

    def test_a_verified_answer_is_ok_and_complete(self) -> None:
        evidence.record({"queries": [{"query": {"origin": "JFK"}, "offers": [{"price": 291}]}]})
        payload = mcp_handlers.verify_answer_tool("nothing to check")
        self.assertIs(payload["ok"], True)
        self.assertEqual((payload["status"], payload["completeness"]), ("ok", "complete"))


class HandlerTests(_StateDirCase):
    def setUp(self) -> None:
        super().setUp()
        mcp_handlers._CACHE.clear()

    def test_search_handlers_stamp_before_the_ledger_and_the_cache(self) -> None:
        report = MagicMock()
        report.to_dict.return_value = {
            "schema_version": 2,
            "searched_at": "2026-08-10T10:00:00Z",
            "currency": "USD",
            "queries": [],
        }
        with patch("viajante.mcp_handlers.search_flights", return_value=report) as search:
            first = mcp_handlers.search_flights_tool([f"JFK-LHR:{DAY.isoformat()}"])
            second = mcp_handlers.search_flights_tool([f"JFK-LHR:{DAY.isoformat()}"])
        self.assertEqual(search.call_count, 1)
        self.assertTrue(ENVELOPE_KEYS <= set(first))
        self.assertTrue(second["cached"])
        self.assertEqual(
            {k: second[k] for k in ENVELOPE_KEYS}, {k: first[k] for k in ENVELOPE_KEYS}
        )
        self.assertEqual(first["observed_at"], "2026-08-10T10:00:00Z")

    def test_a_blocked_search_is_not_cached(self) -> None:
        payload = _flights({"LHR": GoogleFlightsBlocked("wall")})
        report = MagicMock()
        report.to_dict.return_value = {
            key: v for key, v in payload.items() if key not in ENVELOPE_KEYS
        }
        with patch("viajante.mcp_handlers.search_flights", return_value=report) as search:
            mcp_handlers.search_flights_tool([f"JFK-LHR:{DAY.isoformat()}"])
            mcp_handlers.search_flights_tool([f"JFK-LHR:{DAY.isoformat()}"])
        self.assertEqual(search.call_count, 2)


SPLIT_AT = "2026-08-10T10:00:00Z"


def _split_error(code: SearchErrorCode, message: str = "x", **kwargs: object) -> dict:
    return dict(SearchError(code, message, **kwargs).to_dict())


def _leg(error: dict | None = None, offers: int = 3, hub: str = "LAX") -> dict:
    row = {
        "origin": "JFK",
        "destination": hub,
        "departure_date": "2026-09-01",
        "status": "ok" if error is None else "error",
    }
    if error is None:
        row["offers"] = offers
    else:
        row["error"] = error
    return row


def _split_payload(legs: list, itineraries: list | None = None, **extra: object) -> dict:
    return {
        "schema_version": 2,
        "searched_at": SPLIT_AT,
        "itineraries": itineraries or [],
        "legs": legs,
        **extra,
    }


class SplitEnvelopeTests(unittest.TestCase):
    assertEnvelope = EnvelopeShapeTests.assertEnvelope
    ITINERARY = {"split_ticket": True, "total": 700.0}
    EMPTY = _split_error(SearchErrorCode.NO_RESULTS, "No flights")

    def test_an_itinerary_is_ok_and_complete(self) -> None:
        payload = stamp_split(_split_payload([_leg(), _leg()], [self.ITINERARY]))
        self.assertEnvelope(
            payload,
            status="ok",
            completeness="complete",
            empty_reason=None,
            error_code=None,
            retry_after=None,
            retry_after_seconds=None,
            observed_at=SPLIT_AT,
            observed_at_basis="fetch",
        )
        self.assertEqual(payload["itineraries"], [self.ITINERARY])

    def test_an_itinerary_beside_a_failed_leg_is_ok_but_partial(self) -> None:
        timeout = _split_error(SearchErrorCode.FETCH_FAILED, "slow", timeout=True)
        payload = stamp_split(_split_payload([_leg(), _leg(timeout)], [self.ITINERARY]))
        self.assertEnvelope(
            payload,
            status="ok",
            completeness="partial",
            empty_reason=None,
            error_code="fetch_failed",
            observed_at=SPLIT_AT,
            observed_at_basis="fetch",
        )

    def test_a_failed_leg_with_no_itinerary_names_that_failure(self) -> None:
        cases = {
            "blocked": _split_error(SearchErrorCode.BLOCKED, "wall"),
            "timeout": _split_error(SearchErrorCode.FETCH_FAILED, "slow", timeout=True),
            "failed": _split_error(SearchErrorCode.FETCH_FAILED, "boom"),
            "rate_limited": _split_error(SearchErrorCode.BLOCKED, "429", rate_limited=True),
        }
        for status, error in cases.items():
            with self.subTest(status=status):
                payload = stamp_split(_split_payload([_leg(error)]))
                self.assertEnvelope(
                    payload,
                    status=status,
                    completeness="blocked",
                    empty_reason="not_loaded",
                    error_code=error["code"],
                    observed_at=SPLIT_AT,
                    observed_at_basis="fetch",
                )

    def test_a_failure_beside_an_answered_leg_is_partial_not_blocked(self) -> None:
        error = _split_error(SearchErrorCode.BLOCKED, "wall")
        payload = stamp_split(_split_payload([_leg(), _leg(error)]))
        self.assertEnvelope(
            payload, status="blocked", completeness="partial", empty_reason="not_loaded"
        )
        empty_leg = stamp_split(_split_payload([_leg(self.EMPTY), _leg(error)]))
        self.assertEnvelope(empty_leg, completeness="partial", empty_reason="not_loaded")

    def test_the_worst_failure_wins_and_a_repeated_one_counts_once(self) -> None:
        legs = [
            _leg(_split_error(SearchErrorCode.FETCH_FAILED, "boom")),
            _leg(_split_error(SearchErrorCode.BLOCKED, "429", rate_limited=True)),
            _leg(_split_error(SearchErrorCode.BLOCKED, "wall")),
        ]
        payload = stamp_split(_split_payload(legs, error=legs[1]["error"]))
        self.assertEnvelope(payload, status="rate_limited", error_code="blocked")

    def test_a_cooldown_carries_the_errors_own_retry_fields(self) -> None:
        error = self._cooldown()
        self.assertIn("retry_after", error)
        packaged = {"queries": [{"query": {}, "status": "error", "error": error}]}
        for payload in (
            _split_payload([_leg(error)], error=error),
            _split_payload([], error=error),
            _split_payload([], error=dict(error), packaged_report=packaged),
        ):
            with self.subTest(legs=len(payload["legs"])):
                stamped = stamp_split(payload)
                self.assertEnvelope(
                    stamped,
                    status="rate_limited",
                    completeness="blocked",
                    empty_reason="not_loaded",
                    error_code="blocked",
                    retry_after=error["retry_after"],
                    retry_after_seconds=error["retry_after_seconds"],
                    observed_at=None,
                    observed_at_basis=None,
                )

    def test_a_cooldown_beside_an_answered_packaged_search_was_observed(self) -> None:
        error = self._cooldown()
        offers = {"query": {}, "status": "ok", "offers": [{}], "raw_count": 1}
        payload = stamp_split(
            _split_payload([], error=error, packaged_report={"queries": [offers]})
        )
        self.assertEnvelope(
            payload,
            status="rate_limited",
            completeness="partial",
            observed_at=SPLIT_AT,
            observed_at_basis="fetch",
        )

    def test_a_real_429_is_observed_but_a_recorded_cooldown_is_not(self) -> None:
        real = _split_error(SearchErrorCode.BLOCKED, "HTTP 429", rate_limited=True)
        payload = stamp_split(_split_payload([_leg(real)], error=real))
        self.assertEnvelope(payload, status="rate_limited", observed_at=SPLIT_AT)

    @staticmethod
    def _cooldown() -> dict:
        return _split_error(
            SearchErrorCode.BLOCKED,
            f"{NOT_SENT}Google is paused",
            rate_limited=True,
            retry_until=time.time() + 300,
        )

    def test_a_cooldown_after_an_answered_leg_is_partial(self) -> None:
        error = _split_error(
            SearchErrorCode.BLOCKED, "429", rate_limited=True, retry_until=time.time() + 300
        )
        payload = stamp_split(_split_payload([_leg(), _leg(error)], error=error))
        self.assertEnvelope(
            payload,
            status="rate_limited",
            completeness="partial",
            retry_after=error["retry_after"],
            retry_after_seconds=error["retry_after_seconds"],
        )

    def test_a_failed_packaged_search_is_a_failure_too(self) -> None:
        error = _split_error(SearchErrorCode.BLOCKED, "wall")
        packaged = {"queries": [{"query": {}, "status": "error", "error": error}]}
        payload = stamp_split(_split_payload([], packaged_report=packaged))
        self.assertEnvelope(
            payload, status="blocked", completeness="blocked", empty_reason="not_loaded"
        )

    def test_answered_legs_whose_pairings_were_all_rejected_are_filtered_out(self) -> None:
        payload = stamp_split(
            _split_payload([_leg(), _leg()], rejected={"connection_too_short": 2})
        )
        self.assertEnvelope(
            payload,
            status="no_results",
            completeness="complete",
            empty_reason="filtered_out",
            error_code=None,
            observed_at=SPLIT_AT,
            observed_at_basis="fetch",
        )
        mixed = stamp_split(_split_payload([_leg(self.EMPTY), _leg()]))
        self.assertEnvelope(mixed, status="no_results", empty_reason="filtered_out")

    def test_only_provider_empty_legs_are_provider_empty(self) -> None:
        payload = stamp_split(_split_payload([_leg(self.EMPTY), _leg(self.EMPTY)]))
        self.assertEnvelope(
            payload,
            status="no_results",
            completeness="complete",
            empty_reason="provider_empty",
            error_code="no_results",
            observed_at=SPLIT_AT,
            observed_at_basis="fetch",
        )

    def test_legs_decide_provider_empty_not_the_packaged_baseline(self) -> None:
        offers = {"query": {}, "status": "ok", "offers": [{}], "raw_count": 1}
        packaged = {"queries": [offers]}
        empty = stamp_split(
            _split_payload([_leg(self.EMPTY), _leg(self.EMPTY)], packaged_report=packaged)
        )
        self.assertEnvelope(
            empty,
            status="no_results",
            completeness="complete",
            empty_reason="provider_empty",
            error_code="no_results",
        )
        one_answered = stamp_split(
            _split_payload([_leg(), _leg(self.EMPTY)], packaged_report=packaged)
        )
        self.assertEnvelope(one_answered, empty_reason="filtered_out", error_code=None)

    def test_a_leg_with_no_raw_cards_is_provider_empty_but_unknown_counts_are_not(self) -> None:
        empty_leg = {**_leg(offers=0), "raw_count": 0}
        payload = stamp_split(_split_payload([empty_leg, _leg(self.EMPTY)]))
        self.assertEnvelope(payload, empty_reason="provider_empty", error_code="no_results")
        unknown = stamp_split(_split_payload([_leg(offers=0), _leg(self.EMPTY)]))
        self.assertEnvelope(unknown, empty_reason="filtered_out")
        removed = stamp_split(_split_payload([{**_leg(offers=0), "raw_count": 4}]))
        self.assertEnvelope(removed, empty_reason="filtered_out")

    def test_an_unreadable_shape_is_refused(self) -> None:
        with self.assertRaises(EnvelopeShapeError):
            stamp_split({"searched_at": SPLIT_AT})
        with self.assertRaises(EnvelopeShapeError):
            stamp_search(_split_payload([_leg()]))


class ErrorTaxonomyTests(unittest.TestCase):
    def test_timeout_flag_is_typed_at_classification(self) -> None:
        class ReadTimeout(Exception):
            pass

        for exc in (TimeoutError("t"), ReadTimeout("t")):
            self.assertTrue(classify_failure(exc).timeout)
        error = classify_failure(RuntimeError("boom"))
        self.assertFalse(error.timeout)
        self.assertNotIn("timeout", error.to_dict())


class ContractTextTests(unittest.TestCase):
    def test_only_provider_empty_may_say_nothing_was_found(self) -> None:
        self.assertIn("returned no results", EMPTY_NOTES["provider_empty"])
        for reason in ("filtered_out", "not_loaded"):
            note = EMPTY_NOTES[reason].casefold()
            for phrase in ("no results", "no flights", "no hotels", "none found", "nothing found"):
                self.assertNotIn(phrase, note)
        self.assertIn("filters removed", EMPTY_NOTES["filtered_out"])
        self.assertIn("did not complete", EMPTY_NOTES["not_loaded"])
        self.assertEqual(set(EMPTY_NOTES), set(EMPTY_REASONS))

    def test_agent_docs_state_the_rule(self) -> None:
        for path in ("AGENTS.md", ".cursor/skills/viajante/SKILL.md", "docs/architecture.md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                for token in ("provider_empty", "filtered_out", "not_loaded", "completeness"):
                    self.assertIn(token, text)

    def test_mcp_instructions_state_the_rule(self) -> None:
        from viajante.mcp_server import _HELP

        for token in ("provider_empty", "filtered_out", "not_loaded", "Read the envelope first"):
            self.assertIn(token, _HELP)


HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipIf(not HAS_MCP, "mcp extra not installed")
class OutputSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        from viajante.mcp_server import build_server

        # test_mcp asserts the SDK is not imported until build_server runs.
        loaded = set(sys.modules)
        self.addCleanup(
            lambda: [
                sys.modules.pop(name)
                for name in set(sys.modules) - loaded
                if name == "mcp" or name.startswith("mcp.")
            ]
        )
        with warnings.catch_warnings():
            # The SDK's own settings model leaves `lifespan` unresolved; plain FastMCP("x")
            # emits this too. It is not ours to fix.
            warnings.filterwarnings("ignore", message="Field 'lifespan'")
            self.server = build_server()
        mcp_handlers._CACHE.clear()

    def test_every_tool_but_the_bare_list_advertises_the_envelope_schema(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}
        self.assertEqual(len(tools), 19)
        for name, tool in tools.items():
            if name == "lookup_airports":
                self.assertIsNone(tool.outputSchema)
                continue
            schema = tool.outputSchema
            with self.subTest(tool=name):
                self.assertEqual(set(schema["required"]), ENVELOPE_KEYS)
                self.assertTrue(schema["additionalProperties"])
                self.assertEqual(schema["properties"]["status"]["enum"], list(STATUSES))
                self.assertEqual(schema["properties"]["completeness"]["enum"], list(COMPLETENESS))
                reasons = schema["properties"]["empty_reason"]["anyOf"][0]["enum"]
                self.assertEqual(reasons, list(EMPTY_REASONS))

    def test_structured_content_keeps_every_payload_key(self) -> None:
        _content, structured = asyncio.run(self.server.call_tool("get_runtime_info", {}))
        self.assertTrue(ENVELOPE_KEYS <= set(structured))
        self.assertIn("viajante_version", structured)
        content, structured = asyncio.run(
            self.server.call_tool("plan_stay_blocks", {"roster": {"2027-01-01": ["ana"]}})
        )
        self.assertIn("blocks", structured)
        self.assertEqual(json.loads(content[0].text)["status"], "ok")

    def test_search_result_round_trips_with_payload_and_envelope(self) -> None:
        report = MagicMock()
        report.to_dict.return_value = {
            "schema_version": 2,
            "searched_at": "2026-08-10T10:00:00Z",
            "currency": "USD",
            "queries": [],
        }
        with patch("viajante.mcp_handlers.search_flights", return_value=report):
            _content, structured = asyncio.run(
                self.server.call_tool("search_flights", {"routes": [f"JFK-LHR:{DAY.isoformat()}"]})
            )
        self.assertEqual(structured["schema_version"], 2)
        self.assertEqual(structured["observed_at"], "2026-08-10T10:00:00Z")
        self.assertEqual(structured["queries"], [])


if __name__ == "__main__":
    unittest.main()
