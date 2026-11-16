from __future__ import annotations

import unittest

import _isolate  # noqa: F401
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

    def test_dollar_and_yen_glyphs_name_their_currencies(self) -> None:
        evidence.record(FLIGHTS)
        yen = evidence.verify_answer("¥295")
        self.assertFalse(yen["ok"])
        self.assertIn({"kind": "amount", "text": "¥295"}, yen["unowned"])
        evidence.clear()
        evidence.record({"currency": "JPY", "queries": [{"offers": [{"price": 295.0}]}]})
        self.assertTrue(evidence.verify_answer("¥295")["ok"])
        dollars = evidence.verify_answer("$295")
        self.assertFalse(dollars["ok"])
        self.assertIn({"kind": "amount", "text": "$295"}, dollars["unowned"])
        self.assertIn({"kind": "currency", "text": "$295"}, dollars["unowned"])

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

    def test_a_zero_amount_owns_only_zero(self) -> None:
        offer = {"airline": "Icelandair", "price": 291.0, "baggage_buffer": 0}
        evidence.record({**FLIGHTS, "queries": [{"status": "ok", "offers": [offer]}]})
        self.assertEqual(
            evidence.verify_answer("from 1 USD")["unowned"],
            [{"kind": "amount", "text": "1 USD"}],
        )
        self.assertTrue(evidence.verify_answer("bag buffer 0 USD")["ok"])

    def test_recommendation_scores_are_not_amounts(self) -> None:
        pick = {
            "score": 88.6,
            "breakdown": {"price": 0.227, "duration": 0.0, "stops": 0.0},
            "offer": {"price": 291.0},
        }
        recommendation = {"weights": {"price": 0.5}, "shortlist": [pick]}
        evidence.record({**FLIGHTS, "queries": [{"recommendation": recommendation}]})
        self.assertEqual(
            evidence.verify_answer("1 USD, then 291 USD")["unowned"],
            [{"kind": "amount", "text": "1 USD"}],
        )


if __name__ == "__main__":
    unittest.main()
