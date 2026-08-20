from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from viajante.models import (
    DatePriceRow,
    FlexSearchReport,
    FlightOffer,
    SearchError,
    SearchErrorCode,
)

REPORT_KEYS = {
    "schema_version",
    "searched_at",
    "currency",
    "locale",
    "origin",
    "destination",
    "around",
    "flex_days",
    "from",
    "to",
    "trip",
    "chosen_date",
    "typical_eur",
    "vs_typical",
    "fetch_backend",
    "fetch_ms",
    "days",
    "offers",
}
RT_REPORT_KEYS = REPORT_KEYS | {"nights", "return_date"}
FLEX_FETCH_BACKENDS = {"calendar", "calendar_then_sweep"}
FORBIDDEN_KEYS = {"co2", "co2_kg", "emissions", "carbon"}


def _offer() -> FlightOffer:
    return FlightOffer(
        airline="British Airways",
        departure="18:00",
        arrival="06:00",
        price="€350",
        price_eur=350.0,
        duration="7 hr",
        duration_hours=7.0,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer_eur=0,
        needs_bag_verify=False,
        typical_eur=440.0,
        vs_typical="below",
    )


def _report() -> FlexSearchReport:
    return FlexSearchReport(
        searched_at=datetime(2026, 8, 20, 16, 0, 0, tzinfo=timezone.utc),
        origin="BOS",
        destination="LHR",
        around=date(2026, 9, 12),
        flex_days=3,
        start_date=date(2026, 9, 9),
        end_date=date(2026, 9, 15),
        trip="rt",
        nights=7,
        chosen_date=date(2026, 9, 10),
        return_date=date(2026, 9, 17),
        typical_eur=440.0,
        vs_typical="below",
        days=(
            DatePriceRow(
                departure_date=date(2026, 9, 10),
                return_date=date(2026, 9, 17),
                price_eur=388.0,
            ),
        ),
        offers=(_offer(),),
        fetch_backend="calendar_then_sweep",
        fetch_ms=800,
    )


class FlexJsonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = _report().to_dict()

    def test_round_trip_keys_are_exact(self) -> None:
        self.assertEqual(set(self.data), RT_REPORT_KEYS)

    def test_one_way_omits_nights_and_return_date(self) -> None:
        report = FlexSearchReport(
            searched_at=datetime(2026, 8, 20, 16, 0, 0),
            origin="JFK",
            destination="LHR",
            around=date(2026, 9, 15),
            flex_days=3,
            start_date=date(2026, 9, 12),
            end_date=date(2026, 9, 18),
            days=(),
        )
        data = report.to_dict()
        self.assertEqual(set(data), REPORT_KEYS)
        self.assertIsNone(data["chosen_date"])
        self.assertEqual(data["offers"], [])
        self.assertIsNone(data["typical_eur"])
        self.assertIsNone(data["vs_typical"])

    def test_declared_constants_are_stable(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["currency"], "EUR")
        self.assertEqual(self.data["locale"], "en")
        self.assertEqual(self.data["origin"], "BOS")
        self.assertEqual(self.data["destination"], "LHR")
        self.assertEqual(self.data["around"], "2026-09-12")
        self.assertEqual(self.data["flex_days"], 3)
        self.assertEqual(self.data["from"], "2026-09-09")
        self.assertEqual(self.data["to"], "2026-09-15")
        self.assertEqual(self.data["chosen_date"], "2026-09-10")
        self.assertEqual(self.data["return_date"], "2026-09-17")
        self.assertEqual(self.data["nights"], 7)
        self.assertEqual(self.data["trip"], "rt")
        self.assertEqual(self.data["typical_eur"], 440.0)
        self.assertEqual(self.data["vs_typical"], "below")
        self.assertEqual(self.data["fetch_backend"], "calendar_then_sweep")
        self.assertIn(self.data["fetch_backend"], FLEX_FETCH_BACKENDS)

    def test_miss_keeps_null_fare_fields(self) -> None:
        report = FlexSearchReport(
            searched_at=datetime(2026, 8, 20, 16, 0, 0),
            origin="BOS",
            destination="LHR",
            around=date(2026, 9, 12),
            flex_days=3,
            start_date=date(2026, 9, 9),
            end_date=date(2026, 9, 15),
            days=(),
            fetch_backend="calendar",
        )
        data = report.to_dict()
        self.assertIsNone(data["chosen_date"])
        self.assertEqual(data["offers"], [])
        self.assertIsNone(data["typical_eur"])
        self.assertIsNone(data["vs_typical"])
        self.assertEqual(data["fetch_backend"], "calendar")

    def test_error_key_is_added_when_present(self) -> None:
        report = FlexSearchReport(
            searched_at=datetime(2026, 8, 20, 16, 0, 0),
            origin="BOS",
            destination="LHR",
            around=date(2026, 9, 12),
            flex_days=3,
            start_date=date(2026, 9, 9),
            end_date=date(2026, 9, 15),
            days=(),
            error=SearchError(code=SearchErrorCode.REJECTED, message="rejected"),
        )
        data = report.to_dict()
        self.assertEqual(set(data["error"]), {"code", "message"})

    def test_forbidden_keys_are_absent(self) -> None:
        blob = json.dumps(self.data)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_the_whole_report_is_json_serialisable(self) -> None:
        json.loads(json.dumps(self.data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
