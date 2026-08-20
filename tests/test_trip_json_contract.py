from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightOffer,
    HotelOffer,
    HotelQuery,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    QuerySuccess,
    RoundTrip,
    SearchReport,
    TripSearchReport,
    TripTotal,
)

REPORT_KEYS = {
    "schema_version",
    "searched_at",
    "currency",
    "locale",
    "fetch_ms",
    "flights",
    "hotels",
    "trip_total",
}
REPORT_KEYS_WITHOUT_TOTAL = REPORT_KEYS - {"trip_total"}
TOTAL_KEYS = {
    "flight_fare_eur",
    "hotel_stay_eur",
    "total_eur",
    "hotel_price_basis",
    "nights",
}
FORBIDDEN_KEYS = {"co2", "co2_kg", "emissions", "carbon"}


def _offer() -> FlightOffer:
    return FlightOffer(
        airline="Example Air",
        departure="08:40",
        arrival="11:30",
        price="€412",
        price_eur=412.0,
        duration="2 h 50 min",
        duration_hours=2.8,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer_eur=0,
        needs_bag_verify=False,
    )


def _stay() -> HotelOffer:
    return HotelOffer(
        title="Old Town Apartment",
        address="Melbourne",
        total_price="246 €",
        total_price_eur=246.0,
        rating="8.9",
        rating_score=8.9,
        details="Free cancellation",
        cancellation_evidence=CancellationEvidence.FREE,
        property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
        lodging_kind=LodgingKind.ENTIRE_HOME,
        bedrooms=1,
        bathrooms=1,
        beds=2,
        link=None,
    )


def _report(*, with_total: bool = True) -> TripSearchReport:
    flights = SearchReport(
        searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
        fetch_backend="sweep",
        fetch_ms=100,
        queries=(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10), adults=2),
                raw_count=4,
                eligible_count=1,
                offers=(_offer(),),
            ),
        ),
    )
    hotels = HotelSearchReport(
        searched_at=datetime(2026, 8, 11, 10, 33, 0, tzinfo=timezone.utc),
        fetch_backend="google",
        fetch_ms=80,
        provider="google-hotels",
        queries=(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10), adults=2),
                applied=AppliedHotelFilters(
                    chips=("free_cancellation=1",),
                    url="https://example.test",
                ),
                raw_count=8,
                eligible_count=1,
                offers=(_stay(),),
            ),
        ),
    )
    total = (
        TripTotal(flight_fare_eur=412.0, hotel_stay_eur=246.0, total_eur=658.0, nights=4)
        if with_total
        else None
    )
    return TripSearchReport(
        searched_at=datetime(2026, 8, 11, 10, 33, 0, tzinfo=timezone.utc),
        flights=flights,
        hotels=hotels,
        trip_total=total,
        fetch_ms=180,
    )


class TripJsonContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = _report().to_dict()

    def test_report_keys_include_nested_reports_and_total(self) -> None:
        self.assertEqual(set(self.data), REPORT_KEYS)

    def test_trip_total_keys_are_exact(self) -> None:
        total = self.data["trip_total"]
        self.assertEqual(set(total), TOTAL_KEYS)
        self.assertEqual(total["hotel_price_basis"], "total_stay")
        self.assertEqual(total["flight_fare_eur"], 412.0)
        self.assertEqual(total["hotel_stay_eur"], 246.0)
        self.assertEqual(total["total_eur"], 658.0)
        self.assertEqual(total["nights"], 4)

    def test_nested_hotel_price_basis_stays_total_stay(self) -> None:
        self.assertEqual(self.data["hotels"]["price_basis"], "total_stay")
        self.assertNotIn("price_basis", self.data)

    def test_omitted_total_drops_the_key(self) -> None:
        data = _report(with_total=False).to_dict()
        self.assertEqual(set(data), REPORT_KEYS_WITHOUT_TOTAL)
        self.assertNotIn("trip_total", data)

    def test_schema_version_stays_1(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["locale"], "en")
        self.assertEqual(self.data["currency"], "EUR")

    def test_forbidden_keys_are_absent(self) -> None:
        blob = json.dumps(self.data)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_the_whole_report_is_json_serialisable(self) -> None:
        json.loads(json.dumps(self.data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
