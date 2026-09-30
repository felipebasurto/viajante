from __future__ import annotations

import unittest

from viajante import evidence

URL = "https://www.google.com/travel/flights?hl=en&curr=USD&booking_token=abc"
FLIGHTS = {
    "currency": "USD",
    "queries": [
        {
            "status": "ok",
            "query": {"origin": "JFK", "destination": "LHR", "departure_date": "2026-10-27"},
            "offers": [
                {"airline": "Icelandair", "price": 291.0, "google_flights_url": URL},
                {"airline": "British Airways", "price": 295.0},
            ],
        },
        {"status": "error", "query": {"origin": "JFK"}, "error": {"code": "blocked"}},
    ],
}


class VerifyAnswerTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence.clear()

    def test_nothing_is_owned_before_a_search(self) -> None:
        result = evidence.verify_answer("JFK-LHR is 291 USD")
        self.assertFalse(result["ok"])
        self.assertEqual(result["searches"], 0)

    def test_owned_claims_pass_and_rounding_is_tolerated(self) -> None:
        evidence.record(FLIGHTS)
        result = evidence.verify_answer(
            f"JFK to LHR on 2026-10-27: USD 291 on Icelandair, or ~$295. Book: {URL}."
        )
        self.assertEqual(result["unowned"], [])
        self.assertTrue(result["ok"])

    def test_invented_amount_currency_airport_date_and_link_are_flagged(self) -> None:
        evidence.record(FLIGHTS)
        result = evidence.verify_answer(
            "LGW on 2026-11-02 is 120 GBP, total 586 USD: https://example.com/deal"
        )
        kinds = {(row["kind"], row["text"]) for row in result["unowned"]}
        self.assertEqual(
            kinds,
            {
                ("url", "https://example.com/deal"),
                ("date", "2026-11-02"),
                ("amount", "120 GBP"),
                ("currency", "120 GBP"),
                ("amount", "586 USD"),
                ("airport", "LGW"),
            },
        )
        self.assertFalse(result["ok"])


class SummarizeTests(unittest.TestCase):
    def test_leads_with_cheapest_owned_row_link_and_failures(self) -> None:
        lines = evidence.summarize(FLIGHTS)
        self.assertEqual(lines[0], "cheapest owned: 291 USD, Icelandair, JFK-LHR, 2026-10-27")
        self.assertEqual(lines[1], f"link: {URL}")
        self.assertIn("failed: 1 (blocked)", lines)

    def test_no_priced_rows_says_so(self) -> None:
        lines = evidence.summarize({"currency": "JPY", "destinations": []})
        self.assertEqual(lines[0], "no priced rows; do not quote a fare or stay")


if __name__ == "__main__":
    unittest.main()
