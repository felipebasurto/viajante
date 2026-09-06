from __future__ import annotations

import unittest
from random import Random

from viajante.models import SearchErrorCode
from viajante.orchestration import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_JITTER_SECONDS,
    BROWSER_UNAVAILABLE_MARKERS,
    MAX_ATTEMPTS,
    NON_RETRIABLE_CODES,
    REQUEST_DELAY_SECONDS,
    REQUEST_JITTER_SECONDS,
    SWEEP_DELAY_SECONDS,
    SWEEP_JITTER_SECONDS,
    classify_failure,
    inter_query_delay_seconds,
    retry_backoff_seconds,
    sweep_inter_query_delay_seconds,
)


class ClassifyFailureTests(unittest.TestCase):
    def test_browser_unavailable_markers(self) -> None:
        for marker in BROWSER_UNAVAILABLE_MARKERS:
            with self.subTest(marker=marker):
                error = classify_failure(RuntimeError(marker))
                self.assertEqual(error.code, SearchErrorCode.BROWSER_UNAVAILABLE)
                self.assertIn("playwright install chromium", error.message)

    def test_hotels_do_not_treat_empty_pages_as_no_results_by_default(self) -> None:
        error = classify_failure(
            RuntimeError("Booking results page has no recognized result state"),
        )
        self.assertEqual(error.code, SearchErrorCode.FETCH_FAILED)
        self.assertIn("RuntimeError", error.message)
        self.assertIn("no recognized result state", error.message)

    def test_fetch_failed_keeps_exception_type_and_message(self) -> None:
        error = classify_failure(
            RuntimeError("temporary upstream failure"),
        )
        self.assertEqual(error.code, SearchErrorCode.FETCH_FAILED)
        self.assertEqual(error.message, "RuntimeError: temporary upstream failure")

    def test_fetch_failed_message_is_capped(self) -> None:
        blob = "x" * 20_000
        error = classify_failure(RuntimeError(blob))
        self.assertEqual(error.code, SearchErrorCode.FETCH_FAILED)
        self.assertLessEqual(len(error.message), 500)
        self.assertTrue(error.message.endswith("..."))
        self.assertTrue(error.message.startswith("RuntimeError: "))

    def test_non_retriable_codes_cover_terminal_failures(self) -> None:
        self.assertEqual(
            NON_RETRIABLE_CODES,
            frozenset(
                {
                    SearchErrorCode.NO_RESULTS,
                    SearchErrorCode.REJECTED,
                    SearchErrorCode.BLOCKED,
                    SearchErrorCode.MARKUP_DRIFT,
                    SearchErrorCode.BROWSER_UNAVAILABLE,
                }
            ),
        )


class PacingHelperTests(unittest.TestCase):
    def test_retry_backoff_matches_existing_formula(self) -> None:
        self.assertEqual(MAX_ATTEMPTS, 3)
        expected = [
            BACKOFF_BASE_SECONDS * (2**attempt) + Random(7).uniform(0, BACKOFF_JITTER_SECONDS)
            for attempt in range(MAX_ATTEMPTS - 1)
        ]
        actual = [retry_backoff_seconds(attempt, Random(7)) for attempt in range(MAX_ATTEMPTS - 1)]
        self.assertEqual(actual, expected)

    def test_inter_query_delay_matches_existing_formula(self) -> None:
        expected = REQUEST_DELAY_SECONDS + Random(11).uniform(0, REQUEST_JITTER_SECONDS)
        self.assertEqual(inter_query_delay_seconds(Random(11)), expected)

    def test_sweep_delay_is_zero(self) -> None:
        self.assertEqual(SWEEP_DELAY_SECONDS, 0.0)
        self.assertEqual(SWEEP_JITTER_SECONDS, 0.0)
        self.assertEqual(sweep_inter_query_delay_seconds(Random(11)), 0.0)


if __name__ == "__main__":
    unittest.main()
