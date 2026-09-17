from __future__ import annotations

import json
import unittest
from datetime import datetime

from viajante.validate import validate_itinerary


def _segment(
    origin: str,
    destination: str,
    *,
    departure: str = "08:00",
    arrival: str = "10:00",
    airline: str = "Example Air",
    flight_number: str = "EA100",
) -> dict[str, object]:
    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "arrival": arrival,
        "airline": airline,
        "flight_number": flight_number,
    }


def _row(
    origin: str,
    destination: str,
    departure_date: str,
    *,
    evidence_id: str,
    price: float = 100.0,
    currency: str = "USD",
    segments: list[dict[str, object]] | None = None,
    checked_bags: int | None = 1,
    carry_on: int | None = 1,
    stops_count: int = 0,
    url_kind: str = "query",
) -> dict[str, object]:
    query = {
        "trip": "one-way",
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "max_stops": 2,
        "adults": 1,
        "cabin": "economy",
    }
    offer = {
        "price": price,
        "currency": currency,
        "baggage_buffer": 0,
        "needs_bag_verify": checked_bags is None,
        "checked_bags": checked_bags,
        "carry_on": carry_on,
        "stops_count": stops_count,
        "legs": [
            {
                "departure": "08:00",
                "arrival": "10:00",
                "segments": segments or [],
                "layovers": [],
            }
        ],
        "evidence": {
            "evidence_id": evidence_id,
            "source": "google_flights",
            "query": query,
            "retrieved_at": "2026-09-17T10:00:00Z",
            "fetch_backend": "sweep",
            "query_url": "https://example.test/search",
            "offer_url": "https://example.test/search",
            "url_kind": url_kind,
        },
    }
    return {"status": "ok", "query": query, "offer": offer}


def _status(report, name: str) -> str:
    return next(check.status for check in report.checks if check.constraint == name)


class ValidateItineraryTests(unittest.TestCase):
    def test_segment_count_counts_segments_not_rows(self) -> None:
        first = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="one",
            segments=[_segment("JFK", "BOS"), _segment("BOS", "LHR")],
            stops_count=1,
        )
        second = _row(
            "LHR",
            "CDG",
            "2026-10-04",
            evidence_id="two",
            segments=[_segment("LHR", "AMS"), _segment("AMS", "CDG")],
            stops_count=1,
        )
        report = validate_itinerary(
            [first, second],
            {"max_segments": 4},
            currency="USD",
            now=datetime(2026, 9, 17, 10, 0, 0),
        )
        self.assertEqual(report.offer_row_count, 2)
        self.assertEqual(report.journey_leg_count, 2)
        self.assertEqual(report.segment_count, 4)
        self.assertEqual(_status(report, "max_segments"), "pass")

    def test_consecutive_same_operator_fails(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="same",
            segments=[
                _segment("JFK", "BOS", airline="Same Air"),
                _segment("BOS", "LHR", airline="Same Air"),
            ],
            stops_count=1,
        )
        report = validate_itinerary([row], {"no_consecutive_same_operator": True}, currency="USD")
        self.assertFalse(report.feasible)
        self.assertEqual(_status(report, "no_consecutive_same_operator"), "fail")

    def test_travel_window_rejects_proxy_date(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-06-01",
            evidence_id="proxy",
            segments=[_segment("JFK", "LHR")],
        )
        report = validate_itinerary(
            [row],
            {"travel_start": "2026-09-01", "travel_end": "2026-10-15"},
            currency="USD",
        )
        self.assertFalse(report.feasible)
        self.assertEqual(_status(report, "travel_window"), "fail")

    def test_segment_clock_bounds_fail_early_and_late(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="clock",
            segments=[_segment("JFK", "LHR", departure="03:40", arrival="23:45")],
        )
        report = validate_itinerary(
            [row],
            {"depart_after": "07:00", "arrive_before": "23:30"},
            currency="USD",
        )
        self.assertEqual(_status(report, "depart_after"), "fail")
        self.assertEqual(_status(report, "arrive_before"), "fail")

    def test_fare_total_mismatch_fails(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="fare",
            price=88.0,
            segments=[_segment("JFK", "LHR")],
        )
        report = validate_itinerary([row], {"claimed_fare_total": 96.0}, currency="USD")
        self.assertEqual(report.fare_total, 88.0)
        self.assertEqual(_status(report, "fare_total"), "fail")

    def test_missing_segments_yields_unknown_segment_checks(self) -> None:
        row = _row("JFK", "LHR", "2026-10-01", evidence_id="aggregate")
        report = validate_itinerary(
            [row],
            {
                "max_segments": 4,
                "no_consecutive_same_operator": True,
                "depart_after": "07:00",
            },
            currency="USD",
        )
        self.assertIsNone(report.segment_count)
        self.assertIsNone(report.feasible)
        self.assertEqual(_status(report, "max_segments"), "unknown")
        self.assertEqual(_status(report, "no_consecutive_same_operator"), "unknown")
        self.assertEqual(_status(report, "depart_after"), "unknown")

    def test_error_leg_fails_evidence(self) -> None:
        row = {
            "status": "error",
            "query": {"origin": "JFK", "destination": "LHR", "departure_date": "2026-10-01"},
            "error": {"code": "blocked", "message": "blocked"},
        }
        report = validate_itinerary([row], {}, currency="USD")
        self.assertFalse(report.feasible)
        self.assertEqual(_status(report, "evidence"), "fail")

    def test_query_mutation_fails_binding(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="bound",
            segments=[_segment("JFK", "LHR")],
        )
        row["query"]["departure_date"] = "2026-10-02"
        report = validate_itinerary([row], {}, currency="USD")
        self.assertEqual(_status(report, "query_binding"), "fail")

    def test_mixed_currency_omits_total(self) -> None:
        first = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="usd",
            currency="USD",
            segments=[_segment("JFK", "LHR")],
        )
        second = _row(
            "LHR",
            "CDG",
            "2026-10-04",
            evidence_id="gbp",
            currency="GBP",
            segments=[_segment("LHR", "CDG")],
        )
        report = validate_itinerary([first, second], {})
        self.assertIsNone(report.currency)
        self.assertIsNone(report.fare_total)
        self.assertEqual(_status(report, "currency"), "fail")

    def test_needs_bag_verify_is_unknown_for_bags(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="bags",
            checked_bags=None,
            segments=[_segment("JFK", "LHR")],
        )
        report = validate_itinerary([row], {"bags": 1}, currency="USD")
        self.assertEqual(_status(report, "bags"), "unknown")
        self.assertIsNone(report.feasible)

    def test_partial_itinerary_keeps_segment_cap_unknown(self) -> None:
        first = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="partial-one",
            segments=[_segment("JFK", "LHR")],
        )
        second = _row(
            "LHR",
            "CDG",
            "2026-10-04",
            evidence_id="partial-two",
            segments=[_segment("LHR", "CDG")],
        )
        report = validate_itinerary(
            [first, second],
            {"required_leg_count": 3, "max_segments": 18},
            currency="USD",
        )
        self.assertEqual(_status(report, "required_leg_count"), "fail")
        self.assertEqual(_status(report, "max_segments"), "unknown")

    def test_comparative_savings_without_evidence_is_unknown(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="comparison",
            segments=[_segment("JFK", "LHR")],
        )
        report = validate_itinerary([row], {"minimum_savings": 200}, currency="USD")
        self.assertEqual(_status(report, "minimum_savings"), "unknown")

    def test_route_break_fails(self) -> None:
        first = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="route-one",
            segments=[_segment("JFK", "LHR")],
        )
        second = _row(
            "CDG",
            "NRT",
            "2026-10-04",
            evidence_id="route-two",
            segments=[_segment("CDG", "NRT")],
        )
        report = validate_itinerary([first, second], {}, currency="USD")
        self.assertEqual(_status(report, "route_continuity"), "fail")

    def test_relaxations_are_separate_from_scenario(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="relaxed",
            segments=[_segment("JFK", "LHR")],
        )
        report = validate_itinerary(
            [row],
            {
                "travel_start": "2026-10-01",
                "travel_end": "2026-10-02",
                "relaxations": [{"constraint": "travel_start", "from": "2026-09-01"}],
            },
            currency="USD",
        )
        self.assertNotIn("relaxations", report.scenario)
        self.assertEqual(report.relaxations[0]["constraint"], "travel_start")

    def test_schema_v2_shape_is_json_serialisable(self) -> None:
        row = _row(
            "JFK",
            "LHR",
            "2026-10-01",
            evidence_id="json",
            segments=[_segment("JFK", "LHR")],
        )
        data = validate_itinerary([row], {}, currency="USD").to_dict()
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(
            set(data),
            {
                "schema_version",
                "validated_at",
                "scenario",
                "relaxations",
                "feasible",
                "currency",
                "fare_total",
                "ranked_total",
                "offer_row_count",
                "journey_leg_count",
                "segment_count",
                "trip_span_days",
                "violations",
                "unknown",
                "checks",
                "legs",
            },
        )
        self.assertNotIn("price_eur", json.dumps(data))

    def test_unknown_constraint_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported itinerary constraints"):
            validate_itinerary([], {"optimal": True}, currency="USD")


if __name__ == "__main__":
    unittest.main()
