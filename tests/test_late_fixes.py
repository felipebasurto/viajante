from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

import _isolate  # noqa: F401
from viajante import evidence, history
from viajante.control import SearchCancelled
from viajante.google_flights import _retry_after_seconds

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def _response(value: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"Retry-After": value})


class RetryAfterTests(unittest.TestCase):
    def test_delta_seconds_stay_as_they_were(self) -> None:
        self.assertEqual(_retry_after_seconds(_response("90"), now=NOW), 90.0)

    def test_an_http_date_is_the_wait_until_that_time(self) -> None:
        when = "Mon, 16 Nov 2026 12:05:00 GMT"
        self.assertEqual(_retry_after_seconds(_response(when), now=NOW), 300.0)

    def test_a_past_date_or_junk_names_no_wait(self) -> None:
        self.assertIsNone(_retry_after_seconds(_response("Mon, 16 Nov 2026 11:00:00 GMT"), now=NOW))
        self.assertIsNone(_retry_after_seconds(_response("soon"), now=NOW))
        self.assertIsNone(_retry_after_seconds(_response("-5"), now=NOW))
        self.assertIsNone(_retry_after_seconds(SimpleNamespace(headers={}), now=NOW))


class HistoryLimitTests(unittest.TestCase):
    def test_a_limit_below_one_is_an_input_error_not_the_whole_series(self) -> None:
        for bad in (0, -3):
            with self.assertRaises(ValueError):
                history.price_history(limit=bad)


class LedgerCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence.clear()

    def tearDown(self) -> None:
        evidence.clear()

    def test_a_cancel_after_the_append_takes_the_entry_back_out(self) -> None:
        cancel = threading.Event()

        def cancelled() -> None:
            if cancel.is_set():
                raise SearchCancelled()

        kept = {"queries": [{"offers": [{"price": 111, "currency": "USD"}]}]}
        evidence.record(kept, cancelled=cancelled)
        gone = {"queries": [{"offers": [{"price": 222, "currency": "USD"}]}]}
        cancel.set()
        with self.assertRaises(SearchCancelled):
            evidence.record(gone, cancelled=cancelled)
        self.assertEqual(len(evidence._ledger), 1)
        self.assertEqual(evidence._ledger[0]["queries"][0]["offers"][0]["price"], 111)


if __name__ == "__main__":
    unittest.main()
