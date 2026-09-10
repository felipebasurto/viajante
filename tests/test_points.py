from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from viajante.cli import main
from viajante.models import AwardOffer, PointsBalance
from viajante.points import (
    TRANSFER_LAST_VERIFIED,
    award_offer_from_mapping,
    cents_per_point,
    compare_award,
    load_award_offer,
    parse_balances,
    transfer_paths,
)


class CentsPerPointTests(unittest.TestCase):
    def test_cpp_uses_cash_minus_taxes(self) -> None:
        self.assertEqual(cents_per_point(1200, 70000, taxes=186), 1.45)

    def test_rejects_invented_zero_points(self) -> None:
        with self.assertRaises(ValueError):
            cents_per_point(100, 0)


class AwardOfferTests(unittest.TestCase):
    def test_estimated_stays_estimated(self) -> None:
        offer = AwardOffer(
            origin="JFK",
            destination="LHR",
            departure_date=date(2026, 11, 15),
            program="aeroplan",
            points=70000,
            evidence="estimated",
            taxes=186,
            currency="USD",
        )
        self.assertEqual(offer.evidence, "estimated")
        self.assertNotEqual(offer.evidence, "confirmed")
        payload = offer.to_dict()
        self.assertEqual(payload["evidence"], "estimated")
        self.assertNotIn("source", payload)

    def test_confirmed_needs_a_named_source(self) -> None:
        with self.assertRaises(ValueError):
            AwardOffer(
                origin="NRT",
                destination="SIN",
                departure_date=date(2026, 11, 3),
                program="aeroplan",
                points=75000,
                evidence="confirmed",
            )

    def test_load_offer_defaults_to_user_supplied(self) -> None:
        offer = award_offer_from_mapping(
            {
                "origin": "GRU",
                "destination": "EZE",
                "departure_date": "2026-11-20",
                "program": "Smiles",
                "points": 40000,
            }
        )
        self.assertEqual(offer.evidence, "user_supplied")
        self.assertEqual(offer.program, "smiles")


class TransferPathTests(unittest.TestCase):
    def test_funded_path_covers(self) -> None:
        paths = transfer_paths(
            "aeroplan",
            70000,
            (PointsBalance("MR", 80000), PointsBalance("CHASE", 10000)),
        )
        self.assertTrue(paths)
        self.assertEqual(paths[0].last_verified, TRANSFER_LAST_VERIFIED)
        funded = [path for path in paths if path.covers]
        self.assertEqual(funded[0].currency, "MR")
        self.assertEqual(funded[0].effective_points, 80000)

    def test_unknown_program_has_no_invented_partner(self) -> None:
        self.assertEqual(transfer_paths("not-a-program", 50000), ())


class CompareAwardTests(unittest.TestCase):
    def test_omits_cpp_without_cash(self) -> None:
        award = AwardOffer(
            origin="JFK",
            destination="LHR",
            departure_date=date(2026, 11, 15),
            program="aeroplan",
            points=70000,
            evidence="user_supplied",
        )
        report = compare_award(award, balances=(PointsBalance("MR", 90000),))
        self.assertIsNone(report.cpp_cents)
        self.assertIsNone(report.cash_price)
        payload = report.to_dict()
        self.assertNotIn("cpp_cents", payload)
        self.assertTrue(any(step["kind"] == "warning" for step in payload["playbook"]))
        self.assertTrue(payload["transfer_paths"][0]["covers"])

    def test_stamps_cpp_when_cash_is_named(self) -> None:
        award = AwardOffer(
            origin="LHR",
            destination="JFK",
            departure_date=date(2026, 11, 15),
            program="avios",
            points=50000,
            evidence="user_supplied",
            taxes=80,
            currency="GBP",
        )
        report = compare_award(award, cash_price=900, currency="GBP")
        self.assertEqual(report.cpp_cents, cents_per_point(900, 50000, taxes=80))
        self.assertEqual(report.currency, "GBP")

    def test_file_round_trip(self) -> None:
        payload = {
            "origin": "SIN",
            "destination": "NRT",
            "departure_date": "2026-12-01",
            "program": "aeroplan",
            "points": 75000,
            "evidence": "user_supplied",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "award.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            offer = load_award_offer(path)
        self.assertEqual(offer.origin, "SIN")

    def test_parse_balances_object_or_list(self) -> None:
        rows = parse_balances({"balances": [{"program": "mr", "balance": 10}]})
        self.assertEqual(rows[0].program, "MR")
        self.assertEqual(parse_balances([{"program": "CHASE", "balance": 1}])[0].program, "CHASE")


class PointsCliTests(unittest.TestCase):
    def test_points_cpp(self) -> None:
        argv = [
            "points",
            "--cash",
            "1200",
            "--points",
            "70000",
            "--taxes",
            "186",
            "--currency",
            "USD",
        ]
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(argv)
        self.assertEqual(code, 0)
        self.assertIn("cpp", buffer.getvalue())

    def test_awards_help_does_not_invent_live_seats(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["awards", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("no live seats", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
