import importlib.util
import unittest
from datetime import date
from pathlib import Path

import _isolate  # noqa: F401

_SPEC = importlib.util.spec_from_file_location(
    "canary", Path(__file__).resolve().parent.parent / "scripts" / "canary.py"
)
canary = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(canary)


class OutcomeTests(unittest.TestCase):
    def test_a_priced_success_is_ok(self) -> None:
        report = {"queries": [{"status": "ok", "offers": [{"price": 300}]}]}
        self.assertEqual(canary.outcome_of(report), ("ok", None))

    def test_a_query_error_names_its_code(self) -> None:
        for code in ("markup_drift", "blocked", "no_results"):
            with self.subTest(code=code):
                report = {"queries": [{"status": "error", "error": {"code": code}}]}
                self.assertEqual(canary.outcome_of(report), ("error", code))

    def test_a_success_with_no_offers_is_not_ok(self) -> None:
        report = {"queries": [{"status": "ok", "offers": []}]}
        self.assertEqual(canary.outcome_of(report), ("no_offers", None))

    def test_one_bad_query_among_good_ones_fails_the_case(self) -> None:
        report = {
            "queries": [
                {"status": "ok", "offers": [{"price": 1}]},
                {"status": "error", "error": {"code": "markup_drift"}},
            ]
        }
        self.assertEqual(canary.outcome_of(report), ("error", "markup_drift"))

    def test_a_missing_or_malformed_report_is_failed(self) -> None:
        for report in (None, {}, {"queries": []}, {"queries": ["x"]}, []):
            with self.subTest(report=report):
                self.assertEqual(canary.outcome_of(report), ("failed", None))


class CaseTests(unittest.TestCase):
    def test_dates_are_computed_from_today_and_always_ahead(self) -> None:
        today = date(2026, 11, 4)
        by_name = dict(canary.cases(today))
        self.assertEqual(by_name["flights-one-way"][1], "JFK-LHR:2026-12-04")
        self.assertEqual(list(by_name), ["flights-one-way", "hotels-google"])
        self.assertEqual(by_name["hotels-google"][2:4], ["2026-12-19", "2026-12-22"])

    def test_the_summary_lists_every_case(self) -> None:
        rows = [
            {"case": "a", "outcome": "ok", "error_code": None, "seconds": 1.0},
            {"case": "b", "outcome": "error", "error_code": "markup_drift", "seconds": 2.0},
        ]
        text = canary.markdown(rows, date(2026, 11, 4))
        self.assertIn("| b | error | markup_drift | 2.0 |", text)
        self.assertIn("| a | ok |  | 1.0 |", text)


if __name__ == "__main__":
    unittest.main()
