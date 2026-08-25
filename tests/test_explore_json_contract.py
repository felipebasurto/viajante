from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from viajante.models import (
    ExploreDestination,
    ExploreReport,
    SearchError,
    SearchErrorCode,
    StopsCompare,
    StopsCompareSide,
)

REPORT_KEYS = {
    "schema_version",
    "searched_at",
    "currency",
    "locale",
    "origin",
    "from",
    "days",
    "fetch_backend",
    "fetch_ms",
    "destinations",
}
REPORT_ERROR_KEYS = REPORT_KEYS | {"error"}
DESTINATION_KEYS = {"iata", "city", "country", "price_eur"}
DESTINATION_TYPICAL_KEYS = {"typical_eur", "vs_typical", "vs_typical_pct", "typical_deal"}
ERROR_KEYS = {"code", "message"}
EXPLORE_FETCH_BACKENDS = {"explore"}
FORBIDDEN_KEYS = {"co2", "co2_kg", "emissions", "carbon"}


def _report(*, with_error: bool = False) -> ExploreReport:
    return ExploreReport(
        searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
        origin="MAD",
        start_date=date(2026, 9, 1),
        days=7,
        destinations=(
            ExploreDestination(iata="OPO", city="Porto", country="Portugal", price_eur=42.0),
        ),
        fetch_backend="explore",
        fetch_ms=800,
        error=(
            SearchError(
                code=SearchErrorCode.FETCH_FAILED,
                message="Explore catalog fetch failed.",
            )
            if with_error
            else None
        ),
    )


class ExploreJsonContractTests(unittest.TestCase):
    """A renamed or dropped key is a breaking change for anyone reading --save output."""

    def setUp(self) -> None:
        self.data = _report().to_dict()

    def test_report_keys_are_exactly_the_documented_set(self) -> None:
        self.assertEqual(set(self.data), REPORT_KEYS)

    def test_error_report_adds_only_the_error_key(self) -> None:
        data = _report(with_error=True).to_dict()
        self.assertEqual(set(data), REPORT_ERROR_KEYS)
        self.assertEqual(set(data["error"]), ERROR_KEYS)

    def test_destination_shape_is_exact(self) -> None:
        self.assertEqual(set(self.data["destinations"][0]), DESTINATION_KEYS)

    def test_declared_constants_are_stable(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["currency"], "EUR")
        self.assertEqual(self.data["locale"], "en")
        self.assertEqual(self.data["origin"], "MAD")
        self.assertEqual(self.data["from"], "2026-09-01")
        self.assertEqual(self.data["days"], 7)
        self.assertEqual(self.data["fetch_backend"], "explore")
        self.assertEqual(self.data["fetch_ms"], 800)

    def test_schema_version_stays_1(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)

    def test_fetch_backend_is_in_the_closed_set(self) -> None:
        self.assertIn(self.data["fetch_backend"], EXPLORE_FETCH_BACKENDS)

    def test_forbidden_keys_are_absent(self) -> None:
        blob = json.dumps(self.data)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_timestamp_is_utc_iso_with_a_trailing_z(self) -> None:
        self.assertEqual(self.data["searched_at"], "2026-08-11T10:32:00Z")

    def test_the_whole_report_is_json_serialisable(self) -> None:
        json.loads(json.dumps(self.data, ensure_ascii=False))

    def test_stops_compare_is_an_extra_dest_key_from_shop(self) -> None:
        dest = ExploreDestination(
            iata="OPO",
            city="Porto",
            country="Portugal",
            price_eur=42.0,
            stops_compare=StopsCompare(
                nonstop=StopsCompareSide(
                    airline="Ryanair",
                    price="€42",
                    price_eur=42.0,
                    duration="1 hr",
                    duration_hours=1.0,
                    stops="Nonstop",
                    stops_count=0,
                    departure="07:00",
                    arrival="07:50",
                )
            ),
        )
        data = dest.to_dict()
        self.assertEqual(set(data), DESTINATION_KEYS | {"stops_compare"})
        self.assertEqual(set(data["stops_compare"]), {"nonstop"})
        self.assertNotIn("one_stop", data["stops_compare"])
        catalog = ExploreDestination(iata="LIS", city="Lisbon", country="Portugal", price_eur=61.0)
        self.assertEqual(set(catalog.to_dict()), DESTINATION_KEYS)

    def test_google_flights_url_is_an_extra_key_when_present(self) -> None:
        dest = ExploreDestination(
            iata="OPO",
            city="Porto",
            country="Portugal",
            price_eur=42.0,
            google_flights_url="https://www.google.com/travel/flights?tfs=opo",
        )
        data = dest.to_dict()
        self.assertEqual(set(data), DESTINATION_KEYS | {"google_flights_url"})
        self.assertNotIn("booking_token", data)
        report = ExploreReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
            origin="MAD",
            start_date=date(2026, 9, 1),
            days=7,
            destinations=(dest,),
            google_flights_url="https://www.google.com/travel/flights?tfs=report",
        ).to_dict()
        self.assertEqual(set(report), REPORT_KEYS | {"google_flights_url"})
        catalog = ExploreDestination(iata="LIS", city="Lisbon", country="Portugal", price_eur=61.0)
        self.assertEqual(set(catalog.to_dict()), DESTINATION_KEYS)
        self.assertNotIn("booking_token", catalog.to_dict())

    def test_typical_is_an_extra_dest_key_when_stamped(self) -> None:
        dest = ExploreDestination(
            iata="OPO",
            city="Porto",
            country="Portugal",
            price_eur=80.0,
            typical_eur=100.0,
            vs_typical="below",
            vs_typical_pct=-20,
        )
        data = dest.to_dict()
        self.assertEqual(set(data), DESTINATION_KEYS | DESTINATION_TYPICAL_KEYS)
        self.assertEqual(data["typical_eur"], 100.0)
        self.assertEqual(data["vs_typical"], "below")
        self.assertEqual(data["vs_typical_pct"], -20)
        self.assertEqual(data["typical_deal"], "below typical 100 € (−20%)")
        catalog = ExploreDestination(iata="LIS", city="Lisbon", country="Portugal", price_eur=61.0)
        self.assertEqual(set(catalog.to_dict()), DESTINATION_KEYS)
        self.assertNotIn("typical_eur", catalog.to_dict())
        self.assertNotIn("vs_typical", catalog.to_dict())
        self.assertNotIn("vs_typical_pct", catalog.to_dict())
        self.assertNotIn("typical_deal", catalog.to_dict())


if __name__ == "__main__":
    unittest.main()
