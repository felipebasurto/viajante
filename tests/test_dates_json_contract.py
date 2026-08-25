from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from viajante.models import (
    DateCalendarReport,
    DatePriceRow,
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
    "destination",
    "from",
    "to",
    "trip",
    "fetch_backend",
    "fetch_ms",
    "days",
}
RT_REPORT_KEYS = REPORT_KEYS | {"nights"}
DAY_KEYS = {"date", "price_eur", "airline", "stops_count", "status"}
DAY_RETURN_KEYS = DAY_KEYS | {"return_date"}
DAY_ERROR_KEYS = DAY_KEYS | {"error"}
DAY_TYPICAL_KEYS = {"typical_eur", "vs_typical", "vs_typical_pct", "typical_deal"}
ERROR_KEYS = {"code", "message"}
DATE_FETCH_BACKENDS = {"calendar", "sweep"}
FORBIDDEN_KEYS = {"co2", "co2_kg", "emissions", "carbon"}


def _report() -> DateCalendarReport:
    return DateCalendarReport(
        searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
        origin="MAD",
        destination="BCN",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 2),
        days=(
            DatePriceRow(
                departure_date=date(2026, 9, 1),
                price_eur=39.0,
                airline="Vueling",
                stops_count=0,
            ),
            DatePriceRow(
                departure_date=date(2026, 9, 2),
                status="error",
                error=SearchError(
                    code=SearchErrorCode.FETCH_FAILED,
                    message="Google Flights search failed after 3 attempts.",
                ),
            ),
        ),
        fetch_backend="calendar",
        fetch_ms=1200,
    )


class DatesJsonContractTests(unittest.TestCase):
    """A renamed or dropped key is a breaking change for anyone reading --save output."""

    def setUp(self) -> None:
        self.data = _report().to_dict()

    def test_report_keys_are_exactly_the_documented_set(self) -> None:
        self.assertEqual(set(self.data), REPORT_KEYS)

    def test_day_shapes_are_exact(self) -> None:
        priced, failed = self.data["days"]
        self.assertEqual(set(priced), DAY_KEYS)
        self.assertEqual(set(failed), DAY_ERROR_KEYS)
        self.assertEqual(set(failed["error"]), ERROR_KEYS)

    def test_declared_constants_are_stable(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["currency"], "EUR")
        self.assertEqual(self.data["locale"], "en")
        self.assertEqual(self.data["origin"], "MAD")
        self.assertEqual(self.data["destination"], "BCN")
        self.assertEqual(self.data["from"], "2026-09-01")
        self.assertEqual(self.data["to"], "2026-09-02")
        self.assertEqual(self.data["trip"], "one-way")
        self.assertNotIn("nights", self.data)
        self.assertEqual(self.data["fetch_backend"], "calendar")
        self.assertEqual(self.data["fetch_ms"], 1200)

    def test_schema_version_stays_1(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)

    def test_fetch_backend_is_in_the_closed_set(self) -> None:
        self.assertIn(self.data["fetch_backend"], DATE_FETCH_BACKENDS)
        sweep = DateCalendarReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0),
            origin="MAD",
            destination="BCN",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            days=(),
            fetch_backend="sweep",
        )
        self.assertIn(sweep.to_dict()["fetch_backend"], DATE_FETCH_BACKENDS)

    def test_forbidden_keys_are_absent(self) -> None:
        blob = json.dumps(self.data)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_timestamp_is_utc_iso_with_a_trailing_z(self) -> None:
        self.assertEqual(self.data["searched_at"], "2026-08-11T10:32:00Z")

    def test_the_whole_report_is_json_serialisable(self) -> None:
        json.loads(json.dumps(self.data, ensure_ascii=False))

    def test_round_trip_report_adds_nights_and_return_dates(self) -> None:
        report = DateCalendarReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
            origin="BOS",
            destination="LHR",
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 2),
            trip="rt",
            nights=5,
            days=(
                DatePriceRow(
                    departure_date=date(2026, 11, 1),
                    return_date=date(2026, 11, 6),
                    price_eur=410.0,
                ),
                DatePriceRow(
                    departure_date=date(2026, 11, 2),
                    return_date=date(2026, 11, 7),
                    status="empty",
                ),
            ),
        )
        data = report.to_dict()
        self.assertEqual(set(data), RT_REPORT_KEYS)
        self.assertEqual(data["trip"], "rt")
        self.assertEqual(data["nights"], 5)
        self.assertEqual(set(data["days"][0]), DAY_RETURN_KEYS)
        self.assertEqual(data["days"][0]["return_date"], "2026-11-06")
        self.assertEqual(data["days"][1]["status"], "empty")
        self.assertIsNone(data["days"][1]["price_eur"])

    def test_summary_is_omitted_when_fewer_than_three_priced_days(self) -> None:
        self.assertNotIn("summary", self.data)

    def test_summary_block_uses_only_priced_days(self) -> None:
        report = DateCalendarReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
            origin="MAD",
            destination="BCN",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 4),
            days=(
                DatePriceRow(departure_date=date(2026, 9, 1), price_eur=81.0),
                DatePriceRow(departure_date=date(2026, 9, 2), status="empty"),
                DatePriceRow(departure_date=date(2026, 9, 3), price_eur=67.0),
                DatePriceRow(departure_date=date(2026, 9, 4), price_eur=120.0),
            ),
        )
        data = report.to_dict()
        self.assertEqual(
            set(data),
            REPORT_KEYS | {"summary"},
        )
        self.assertEqual(
            set(data["summary"]),
            {"min_eur", "median_eur", "max_eur", "cheapest_date", "n_priced"},
        )
        self.assertEqual(data["summary"]["min_eur"], 67.0)
        self.assertEqual(data["summary"]["median_eur"], 81.0)
        self.assertEqual(data["summary"]["max_eur"], 120.0)
        self.assertEqual(data["summary"]["cheapest_date"], "2026-09-03")
        self.assertEqual(data["summary"]["n_priced"], 3)
        self.assertIsNone(data["days"][1]["price_eur"])
        self.assertNotIn("stops_compare", data["days"][0])
        self.assertNotIn("stops_compare", data)
        self.assertEqual(set(data["days"][0]), DAY_KEYS | DAY_TYPICAL_KEYS)
        self.assertEqual(data["days"][0]["typical_eur"], 81.0)
        self.assertEqual(data["days"][0]["vs_typical"], "near")
        self.assertEqual(data["days"][0]["vs_typical_pct"], 0)
        self.assertEqual(data["days"][0]["typical_deal"], "near typical 81 € (0%)")
        self.assertEqual(set(data["days"][1]), DAY_KEYS)
        self.assertNotIn("typical_eur", data["days"][1])
        self.assertEqual(data["days"][2]["vs_typical"], "below")
        self.assertEqual(data["days"][2]["vs_typical_pct"], -17)
        self.assertEqual(data["days"][3]["vs_typical"], "above")
        self.assertEqual(data["days"][3]["typical_eur"], 81.0)

    def test_typical_is_an_extra_day_key_when_stamped(self) -> None:
        row = DatePriceRow(
            departure_date=date(2026, 9, 1),
            price_eur=67.0,
            typical_eur=81.0,
            vs_typical="below",
            vs_typical_pct=-17,
        )
        data = row.to_dict()
        self.assertEqual(set(data), DAY_KEYS | DAY_TYPICAL_KEYS)
        self.assertEqual(data["typical_deal"], "below typical 81 € (−17%)")
        omitted = DatePriceRow(departure_date=date(2026, 9, 1), price_eur=67.0).to_dict()
        self.assertEqual(set(omitted), DAY_KEYS)
        self.assertNotIn("typical_eur", omitted)
        self.assertNotIn("vs_typical", omitted)
        self.assertNotIn("vs_typical_pct", omitted)
        self.assertNotIn("typical_deal", omitted)

    def test_stops_compare_is_an_extra_day_key_from_sweep_shop(self) -> None:
        side = StopsCompareSide(
            airline="Iberia",
            price="€88",
            price_eur=88.0,
            duration="1 hr 20 min",
            duration_hours=1.33,
            stops="Nonstop",
            stops_count=0,
            departure="08:00",
            arrival="09:20",
        )
        report = DateCalendarReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0),
            origin="MAD",
            destination="BCN",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            fetch_backend="sweep",
            days=(
                DatePriceRow(
                    departure_date=date(2026, 9, 1),
                    price_eur=88.0,
                    airline="Iberia",
                    stops_count=0,
                    stops_compare=StopsCompare(nonstop=side),
                ),
            ),
        )
        data = report.to_dict()
        self.assertEqual(set(data), REPORT_KEYS)
        self.assertEqual(set(data["days"][0]), DAY_KEYS | {"stops_compare"})
        self.assertEqual(set(data["days"][0]["stops_compare"]), {"nonstop"})
        self.assertEqual(data["days"][0]["stops_compare"]["nonstop"]["price_eur"], 88.0)
        self.assertNotIn("one_stop", data["days"][0]["stops_compare"])

    def test_google_flights_url_is_an_extra_key_when_present(self) -> None:
        report = DateCalendarReport(
            searched_at=datetime(2026, 8, 11, 10, 32, 0),
            origin="MAD",
            destination="BCN",
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 1),
            google_flights_url="https://www.google.com/travel/flights?tfs=abc",
            days=(
                DatePriceRow(
                    departure_date=date(2026, 9, 1),
                    price_eur=45.0,
                    google_flights_url="https://www.google.com/travel/flights?tfs=day",
                ),
            ),
        )
        data = report.to_dict()
        self.assertEqual(set(data), REPORT_KEYS | {"google_flights_url"})
        self.assertEqual(
            data["google_flights_url"], "https://www.google.com/travel/flights?tfs=abc"
        )
        self.assertEqual(set(data["days"][0]), DAY_KEYS | {"google_flights_url"})
        self.assertNotIn("booking_token", data["days"][0])
        omitted = DatePriceRow(departure_date=date(2026, 9, 1), price_eur=45.0).to_dict()
        self.assertEqual(set(omitted), DAY_KEYS)
        self.assertNotIn("google_flights_url", omitted)
        self.assertNotIn("booking_token", omitted)


if __name__ == "__main__":
    unittest.main()
