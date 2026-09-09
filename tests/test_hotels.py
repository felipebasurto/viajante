from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import List, Sequence, Tuple, Union
from unittest.mock import patch

import viajante.hotels as hotels_module
from viajante.booking import BookingResultsTimeout, HotelPage, RawHotelCard
from viajante.google_hotels_rpc import EmptyHotelResults, HotelsParseMiss
from viajante.hotels import (
    _is_eligible,
    _normalize_card,
    _rank_offers,
    _run_search,
    search_hotels,
    write_hotel_report_atomic,
)
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    SearchErrorCode,
)
from viajante.orchestration import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_JITTER_SECONDS,
    MAX_ATTEMPTS,
    REQUEST_DELAY_SECONDS,
    REQUEST_JITTER_SECONDS,
)

ScriptedResponse = Union[HotelPage, Exception]


class FakeSource:
    def __init__(self, responses: Sequence[ScriptedResponse]) -> None:
        self.responses = list(responses)
        self.fetch_calls: List[Tuple[HotelQuery, AppliedHotelFilters, int]] = []
        self.reset_calls = 0
        self.closed = False
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(
        self,
        query: HotelQuery,
        applied: AppliedHotelFilters,
        limit: int,
    ) -> HotelPage:
        self.fetch_calls.append((query, applied, limit))
        if not self.responses:
            raise RuntimeError("missing fake response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.closed = True


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


def card(
    *,
    title: str = "Casa Azul",
    address: str | None = "Centro, Lisboa",
    total_price: str = "400 €",
    rating: str | None = "Rating: 8.7",
    details: str = "Free cancellation · Entire home · 2 bedrooms · 1 bathroom · 3 beds",
    link: str | None = "https://www.booking.com/hotel/pt/casa-azul.html",
) -> RawHotelCard:
    return RawHotelCard(
        title=title,
        address=address,
        total_price=total_price,
        rating=rating,
        details=details,
        link=link,
    )


def offer(
    *,
    title: str = "Casa Azul",
    address: str | None = "Centro, Lisboa",
    total_price: float = 400.0,
    rating_score: float | None = 8.7,
    cancellation: CancellationEvidence = CancellationEvidence.FREE,
    property_type: PropertyTypeEvidence = PropertyTypeEvidence.ENTIRE_HOME,
    lodging_kind: LodgingKind | None = None,
) -> HotelOffer:
    if lodging_kind is None:
        lodging_kind = (
            LodgingKind.ENTIRE_HOME
            if property_type is PropertyTypeEvidence.ENTIRE_HOME
            else LodgingKind.UNKNOWN
        )
    return HotelOffer(
        title=title,
        address=address,
        total_price_text=f"{total_price:g} €",
        total_price=total_price,
        rating=None if rating_score is None else f"{rating_score:g}",
        rating_score=rating_score,
        details="details",
        cancellation_evidence=cancellation,
        property_type_evidence=property_type,
        lodging_kind=lodging_kind,
        bedrooms=None,
        bathrooms=None,
        beds=None,
        link=None,
    )


class HotelLoopPacingTests(unittest.TestCase):
    def test_provider_pacing_constants(self) -> None:
        self.assertEqual(
            (
                REQUEST_DELAY_SECONDS,
                REQUEST_JITTER_SECONDS,
                MAX_ATTEMPTS,
                BACKOFF_BASE_SECONDS,
                BACKOFF_JITTER_SECONDS,
            ),
            (4.5, 1.5, 3, 8.0, 3.0),
        )


class PureHotelLogicTests(unittest.TestCase):
    def test_normalize_card_preserves_raw_values_and_parses_details(self) -> None:
        normalized = _normalize_card(card())

        assert normalized is not None
        self.assertEqual(normalized.total_price_text, "400 €")
        self.assertEqual(normalized.total_price, 400.0)
        self.assertEqual(normalized.rating, "Rating: 8.7")
        self.assertEqual(normalized.rating_score, 8.7)
        self.assertEqual(normalized.details, card().details)
        self.assertEqual(normalized.cancellation_evidence, CancellationEvidence.FREE)
        self.assertEqual(
            normalized.property_type_evidence,
            PropertyTypeEvidence.ENTIRE_HOME,
        )
        self.assertEqual(normalized.lodging_kind, LodgingKind.ENTIRE_HOME)
        self.assertEqual(
            (normalized.bedrooms, normalized.bathrooms, normalized.beds),
            (2, 1, 3),
        )

    def test_rating_is_never_parsed_from_card_details(self) -> None:
        normalized = _normalize_card(card(rating=None, details="Rating: 9.9 · Free cancellation"))

        assert normalized is not None
        self.assertIsNone(normalized.rating_score)

    def test_normalize_card_parses_realistic_dedicated_rating_text(self) -> None:
        normalized = _normalize_card(
            card(
                rating="8,7 Fabulous",
                details="2 bedrooms · Neighborhood score: 4.1",
            )
        )

        assert normalized is not None
        self.assertEqual(normalized.rating_score, 8.7)

    def test_normalize_card_does_not_publish_review_count_as_rating(self) -> None:
        normalized = _normalize_card(card(rating="Fabulous 1.234 reviews"))

        assert normalized is not None
        self.assertIsNone(normalized.rating_score)

    def test_apartment_title_fills_silent_lodging_kind(self) -> None:
        normalized = _normalize_card(card(title="Chiado Apartment", details="Wifi · Centre"))
        assert normalized is not None
        self.assertEqual(normalized.lodging_kind, LodgingKind.ENTIRE_HOME)
        hotel = _normalize_card(card(title="Hotel Bruno", details="Wifi · Centro"))
        assert hotel is not None
        self.assertEqual(hotel.lodging_kind, LodgingKind.UNKNOWN)

    def test_invalid_prices_are_dropped(self) -> None:
        for price in ("", "consultar", "0 €", "-20 €"):
            with self.subTest(price=price):
                self.assertIsNone(_normalize_card(card(total_price=price)))

    def test_non_property_titles_are_dropped(self) -> None:
        self.assertIsNone(_normalize_card(card(title="closed", total_price="122 €")))
        self.assertIsNotNone(_normalize_card(card(title="Plus Prague Hostel", total_price="95 €")))

    def test_strict_minimum_rating_rejects_unknown_and_low_scores(self) -> None:
        strict_query = query(min_rating=8.0)

        self.assertFalse(_is_eligible(offer(rating_score=None), strict_query))
        self.assertFalse(_is_eligible(offer(rating_score=7.9), strict_query))
        self.assertTrue(_is_eligible(offer(rating_score=8.0), strict_query))

    def test_requested_evidence_rejects_only_explicit_contradictions(self) -> None:
        strict_query = query(entire_home=True, free_cancellation=True)

        self.assertFalse(
            _is_eligible(
                offer(cancellation=CancellationEvidence.NON_REFUNDABLE),
                strict_query,
            )
        )
        self.assertFalse(
            _is_eligible(
                offer(property_type=PropertyTypeEvidence.NOT_ENTIRE_HOME),
                strict_query,
            )
        )
        self.assertTrue(
            _is_eligible(
                offer(
                    cancellation=CancellationEvidence.UNKNOWN,
                    property_type=PropertyTypeEvidence.UNKNOWN,
                ),
                strict_query,
            )
        )
        self.assertTrue(
            _is_eligible(
                offer(cancellation=CancellationEvidence.NON_REFUNDABLE),
                query(free_cancellation=False),
            )
        )

    def test_rank_deduplicates_normalized_identity_and_sorts_ties(self) -> None:
        ranked = _rank_offers(
            (
                offer(title="Beta", total_price=200, rating_score=None),
                offer(title="alpha", total_price=200, rating_score=8.5),
                offer(
                    title=" ALPHA ",
                    address="  Centro,   Lisboa ",
                    total_price=200,
                    rating_score=9.0,
                ),
                offer(title="Cheap", total_price=150, rating_score=7.0),
            ),
            top=3,
        )

        self.assertEqual([row.title for row in ranked], ["Cheap", "ALPHA", "Beta"])
        with self.assertRaises(ValueError):
            _rank_offers((), top=0)


class EnglishHotelEvidenceSeamTests(unittest.TestCase):
    """English card text must survive normalize → evidence, not only parser unit tests."""

    def test_english_free_cancellation_and_entire_home_survive_normalize(self) -> None:
        normalized = _normalize_card(
            card(
                rating="Scored 8.7",
                details=("Free cancellation · Entire home · 2 bedrooms · 1 bathroom · 3 beds"),
            )
        )
        assert normalized is not None
        self.assertEqual(normalized.cancellation_evidence, CancellationEvidence.FREE)
        self.assertEqual(
            normalized.property_type_evidence,
            PropertyTypeEvidence.ENTIRE_HOME,
        )
        self.assertEqual(normalized.lodging_kind, LodgingKind.ENTIRE_HOME)
        self.assertEqual(
            (normalized.bedrooms, normalized.bathrooms, normalized.beds),
            (2, 1, 3),
        )
        self.assertTrue(_is_eligible(normalized, query(entire_home=True, free_cancellation=True)))

    def test_english_non_refundable_and_private_room_are_excluded_by_filters(self) -> None:
        normalized = _normalize_card(card(details="Non-refundable · Private room · Free WiFi"))
        assert normalized is not None
        self.assertEqual(
            normalized.cancellation_evidence,
            CancellationEvidence.NON_REFUNDABLE,
        )
        self.assertEqual(
            normalized.property_type_evidence,
            PropertyTypeEvidence.NOT_ENTIRE_HOME,
        )
        self.assertEqual(normalized.lodging_kind, LodgingKind.PRIVATE_ROOM)
        self.assertFalse(_is_eligible(normalized, query(entire_home=True, free_cancellation=True)))

    def test_english_unknown_evidence_stays_eligible_under_strict_filters(self) -> None:
        normalized = _normalize_card(
            card(title="City View Stay", details="Breakfast included · City view")
        )
        assert normalized is not None
        self.assertEqual(normalized.cancellation_evidence, CancellationEvidence.UNKNOWN)
        self.assertEqual(
            normalized.property_type_evidence,
            PropertyTypeEvidence.UNKNOWN,
        )
        self.assertEqual(normalized.lodging_kind, LodgingKind.UNKNOWN)
        self.assertTrue(_is_eligible(normalized, query(entire_home=True, free_cancellation=True)))


class HotelOrchestrationTests(unittest.TestCase):
    def test_failure_then_success_resets_once(self) -> None:
        source = FakeSource(
            [
                RuntimeError("temporary"),
                HotelPage(cards=(card(title="Recovered"),)),
            ]
        )
        sleeps: List[float] = []

        report = _run_search(
            (query(),),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(3),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        self.assertIsInstance(report.queries[0], HotelQuerySuccess)
        self.assertEqual(source.reset_calls, 1)
        self.assertEqual(len(source.fetch_calls), 2)
        self.assertEqual(len(sleeps), 1)

    def test_retries_same_applied_filters_then_returns_typed_failure(self) -> None:
        hotel_query = query(entire_home=True)
        source = FakeSource([RuntimeError("blocked")] * MAX_ATTEMPTS)
        sleeps: List[float] = []
        expected_random = Random(7)
        expected_backoffs = [
            BACKOFF_BASE_SECONDS * (2**attempt) + expected_random.uniform(0, BACKOFF_JITTER_SECONDS)
            for attempt in range(MAX_ATTEMPTS - 1)
        ]

        report = _run_search(
            (hotel_query,),
            top=5,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(7),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        self.assertEqual(len(source.fetch_calls), MAX_ATTEMPTS)
        self.assertEqual(source.reset_calls, MAX_ATTEMPTS)
        first_applied = source.fetch_calls[0][1]
        self.assertTrue(all(call[1] is first_applied for call in source.fetch_calls))
        self.assertEqual(
            first_applied.chips,
            ("oos=1", "privacy_type=3", "ht_id=201"),
        )
        self.assertTrue(all(call[2] == 24 for call in source.fetch_calls))
        self.assertEqual(sleeps, expected_backoffs)
        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.FETCH_FAILED)
        self.assertEqual(result.error.message, "RuntimeError: blocked")

    def test_results_timeout_is_not_retried(self) -> None:
        source = FakeSource([BookingResultsTimeout("cards never appeared")])
        sleeps: List[float] = []

        report = _run_search(
            (query(),),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.FETCH_FAILED)
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(source.reset_calls, 1)
        self.assertEqual(sleeps, [])

    def test_missing_chromium_fails_immediately_without_backoff(self) -> None:
        source = FakeSource(
            [RuntimeError("Executable doesn't exist at /ms-playwright/chromium/headless")]
        )
        sleeps: List[float] = []

        report = _run_search(
            (query(),),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.BROWSER_UNAVAILABLE)
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(source.reset_calls, 1)
        self.assertEqual(sleeps, [])

    def test_two_queries_sleep_once_between_queries(self) -> None:
        source = FakeSource([HotelPage(cards=()), HotelPage(cards=())])
        sleeps: List[float] = []
        expected = REQUEST_DELAY_SECONDS + Random(11).uniform(0, REQUEST_JITTER_SECONDS)

        _run_search(
            (query(), query(location="Porto")),
            top=8,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(11),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        self.assertEqual(sleeps, [expected])

    def test_progress_announces_each_query(self) -> None:
        source = FakeSource([HotelPage(cards=()), HotelPage(cards=())])
        lines: List[str] = []
        _run_search(
            (query(), query(location="Porto")),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            progress=lines.append,
        )
        self.assertEqual(
            lines,
            [
                "[1/2] Lisboa 2026-12-04 -> 2026-12-08",
                "[2/2] Porto 2026-12-04 -> 2026-12-08",
            ],
        )

    def test_explicit_empty_page_is_success(self) -> None:
        source = FakeSource([HotelPage(cards=())])

        report = _run_search(
            (query(),),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        result = report.queries[0]
        self.assertIsInstance(result, HotelQuerySuccess)
        assert isinstance(result, HotelQuerySuccess)
        self.assertEqual((result.raw_count, result.eligible_count), (0, 0))
        self.assertEqual(result.offers, ())
        self.assertEqual(source.reset_calls, 0)

    def test_counts_use_raw_cards_and_deduplicated_eligible_offers(self) -> None:
        source = FakeSource(
            [
                HotelPage(
                    cards=(
                        card(title="Cheap", total_price="100 €"),
                        card(title="Cheap", total_price="100 €"),
                        card(title="Second", total_price="200 €"),
                        card(title="Broken", total_price="consultar"),
                    )
                )
            ]
        )

        report = _run_search(
            (query(),),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        result = report.queries[0]
        assert isinstance(result, HotelQuerySuccess)
        self.assertEqual(result.raw_count, 4)
        self.assertEqual(result.eligible_count, 2)
        self.assertEqual([row.title for row in result.offers], ["Cheap"])
        self.assertEqual(source.fetch_calls[0][2], 24)

    def test_blank_price_normalizes_to_empty_success(self) -> None:
        source = FakeSource([HotelPage(cards=(card(total_price=""),))])

        report = _run_search(
            (query(),),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
        )

        result = report.queries[0]
        assert isinstance(result, HotelQuerySuccess)
        self.assertEqual((result.raw_count, result.eligible_count), (1, 0))
        self.assertEqual(result.offers, ())

    def test_run_search_uses_rank_offers_for_full_eligible_count(self) -> None:
        source = FakeSource(
            [
                HotelPage(
                    cards=(
                        card(title="One", total_price="100 €"),
                        card(title="One", total_price="100 €"),
                        card(title="Two", total_price="200 €"),
                    )
                )
            ]
        )

        with patch(
            "viajante.hotels._rank_offers",
            wraps=_rank_offers,
        ) as rank_offers:
            report = _run_search(
                (query(),),
                top=1,
                source=source,
                sleep=lambda _: None,
                random_gen=Random(0),
                now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            )

        result = report.queries[0]
        assert isinstance(result, HotelQuerySuccess)
        rank_offers.assert_called_once()
        self.assertEqual(result.eligible_count, 2)
        self.assertEqual([row.title for row in result.offers], ["One"])

    def test_run_search_validates_without_fetching(self) -> None:
        source = FakeSource([])
        kwargs = {
            "source": source,
            "sleep": lambda _: None,
            "random_gen": Random(0),
            "now": lambda: datetime(2026, 8, 10, 10, 0, 0),
        }

        with self.assertRaises(ValueError):
            _run_search((), top=8, **kwargs)
        with self.assertRaises(ValueError):
            _run_search((query(),), top=0, **kwargs)

        self.assertEqual(source.fetch_calls, [])

    def test_search_validates_before_source_construction(self) -> None:
        with (
            patch("viajante.hotels.BookingHotelsSource") as booking,
            patch("viajante.hotels.GoogleHotelsSource") as google,
        ):
            with self.assertRaises(ValueError):
                search_hotels(())
            with self.assertRaises(ValueError):
                search_hotels((query(),), top=0)
            with self.assertRaises(ValueError):
                search_hotels((query(),), source="bing")  # type: ignore[arg-type]

        booking.assert_not_called()
        google.assert_not_called()

    def test_google_source_sets_provider_and_skips_browser_delay(self) -> None:
        source = FakeSource([HotelPage(cards=()), HotelPage(cards=())])
        sleeps: List[float] = []
        report = _run_search(
            (query(), query()),
            top=1,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            html_lang="en",
            provider="google-hotels",
            applied_filters=lambda query, **_: AppliedHotelFilters(
                chips=("free_cancellation=1",),
                url="https://www.google.com/travel/search",
            ),
            delay_seconds=lambda _: 0.0,
        )
        self.assertEqual(report.provider, "google-hotels")
        self.assertEqual(report.locale, "en")
        self.assertEqual(sleeps, [0.0])
        self.assertNotEqual(report.provider, "booking.com")

    def test_google_parse_miss_is_not_retried(self) -> None:
        source = FakeSource([HotelsParseMiss("no wrb.fr hotel payload")])
        sleeps: List[float] = []
        report = _run_search(
            (query(),),
            top=1,
            source=source,
            sleep=sleeps.append,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            provider="google-hotels",
        )
        self.assertEqual(len(source.fetch_calls), 1)
        self.assertEqual(sleeps, [])
        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.MARKUP_DRIFT)

    def test_google_empty_is_not_retried(self) -> None:
        source = FakeSource([EmptyHotelResults()])
        report = _run_search(
            (query(),),
            top=1,
            source=source,
            sleep=lambda _: None,
            random_gen=Random(0),
            now=lambda: datetime(2026, 8, 10, 10, 0, 0),
            provider="google-hotels",
        )
        self.assertEqual(len(source.fetch_calls), 1)
        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.NO_RESULTS)

    def test_search_google_uses_google_source(self) -> None:
        source = FakeSource([HotelPage(cards=())])
        source.config = SimpleNamespace(html_lang="en", currency="EUR")
        with (
            patch("viajante.hotels.GoogleHotelsSource", return_value=source),
            patch("viajante.hotels.BookingHotelsSource") as booking,
            patch("viajante.hotels.time.sleep"),
        ):
            report = search_hotels((query(),), source="google", currency="EUR")
        booking.assert_not_called()
        self.assertTrue(source.closed)
        self.assertEqual(report.provider, "google-hotels")
        self.assertEqual(report.locale, "en")

    def test_search_always_closes_source(self) -> None:
        source = FakeSource([RuntimeError("blocked")] * MAX_ATTEMPTS)

        with (
            patch("viajante.hotels.BookingHotelsSource", return_value=source),
            patch("viajante.hotels.time.sleep"),
        ):
            report = search_hotels((query(),), currency="EUR")

        self.assertTrue(source.closed)
        self.assertIsInstance(report.queries[0], HotelQueryFailure)

    def test_report_writer_delegates_to_atomic_storage(self) -> None:
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 10, 0, 0),
            queries=(),
        )
        destination = Path("hotels.json")

        with patch("viajante.hotels.write_json_atomic") as writer:
            write_hotel_report_atomic(report, destination)

        writer.assert_called_once_with(report.to_dict(), destination)

    def test_report_writer_produces_json_atomically(self) -> None:
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 10, 0, 0),
            queries=(),
        )

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "nested" / "hotels.json"
            write_hotel_report_atomic(report, destination)

            self.assertTrue(destination.exists())
            self.assertFalse(destination.with_suffix(".json.tmp").exists())
            self.assertIn('"provider": "booking.com"', destination.read_text())

    def test_module_has_no_city_specific_or_private_policy(self) -> None:
        source = Path(hotels_module.__file__).read_text(encoding="utf-8").casefold()

        for forbidden in ("praga", "prague", "median", "suburb", "tram"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_booking_without_playwright_is_browser_unavailable(self) -> None:
        with (
            patch("viajante.hotels.playwright_available", return_value=False),
            patch("viajante.hotels.BookingHotelsSource") as source,
        ):
            report = search_hotels((query(),), currency="EUR", source="booking")
        source.assert_not_called()
        self.assertEqual(len(report.queries), 1)
        result = report.queries[0]
        self.assertIsInstance(result, HotelQueryFailure)
        assert isinstance(result, HotelQueryFailure)
        self.assertEqual(result.error.code, SearchErrorCode.BROWSER_UNAVAILABLE)
        self.assertIn("viajante[browser]", result.error.message)


if __name__ == "__main__":
    unittest.main()
