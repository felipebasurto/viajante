from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone

from viajante.models import (
    FlightOffer,
    FlightQuery,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RawLayover,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
)

REPORT_KEYS = {
    "schema_version",
    "searched_at",
    "currency",
    "locale",
    "fetch_backend",
    "fetch_ms",
    "queries",
}
QUERY_KEYS = {
    "trip",
    "origin",
    "destination",
    "departure_date",
    "max_stops",
    "adults",
    "cabin",
}
SUCCESS_KEYS = {"status", "query", "raw_count", "eligible_count", "offers"}
FAILURE_KEYS = {"status", "query", "error"}
OFFER_KEYS = {
    "airline",
    "departure",
    "arrival",
    "price",
    "price_eur",
    "typical_eur",
    "vs_typical",
    "duration",
    "duration_hours",
    "stops",
    "stops_count",
    "layover_city",
    "layover_hours",
    "flight_numbers",
    "booking_token",
    "baggage_buffer_eur",
    "needs_bag_verify",
    "legs",
}
ERROR_KEYS = {"code", "message"}
FLIGHT_FETCH_BACKENDS = {"sweep", "detail", "sweep_then_detail"}
FORBIDDEN_KEYS = {"co2", "co2_kg", "emissions", "carbon"}


def _report() -> SearchReport:
    query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
    offer = FlightOffer(
        airline="Vueling",
        departure="07:15",
        arrival="08:40",
        price="€39",
        price_eur=39.0,
        duration="1 hr 25 min",
        duration_hours=1.4166666666666667,
        stops="Nonstop",
        stops_count=0,
        layover_city=None,
        layover_hours=None,
        baggage_buffer_eur=70,
        needs_bag_verify=True,
    )
    return SearchReport(
        searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
        fetch_backend="sweep",
        fetch_ms=2410,
        queries=(
            QuerySuccess(query=query, raw_count=24, eligible_count=1, offers=(offer,)),
            QueryFailure(
                query=query,
                error=SearchError(
                    code=SearchErrorCode.NO_RESULTS,
                    message="Google Flights returned no flights for this route and date.",
                ),
            ),
        ),
    )


class JsonContractTests(unittest.TestCase):
    """A renamed or dropped key is a breaking change for anyone reading --save output."""

    def setUp(self) -> None:
        self.data = _report().to_dict()

    def test_report_keys_are_exactly_the_documented_set(self) -> None:
        self.assertEqual(set(self.data), REPORT_KEYS)

    def test_success_and_failure_shapes_are_exact(self) -> None:
        success, failure = self.data["queries"]
        self.assertEqual(set(success), SUCCESS_KEYS)
        self.assertEqual(set(failure), FAILURE_KEYS)
        self.assertEqual(set(success["query"]), QUERY_KEYS)
        self.assertEqual(set(success["offers"][0]), OFFER_KEYS)
        self.assertEqual(set(failure["error"]), ERROR_KEYS)

    def test_declared_constants_are_stable(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)
        self.assertEqual(self.data["currency"], "EUR")
        self.assertEqual(self.data["locale"], "en")
        self.assertEqual(self.data["fetch_backend"], "sweep")
        self.assertEqual(self.data["fetch_ms"], 2410)

    def test_timestamp_is_utc_iso_with_a_trailing_z(self) -> None:
        self.assertEqual(self.data["searched_at"], "2026-08-11T10:32:00Z")

    def test_a_nonstop_offer_carries_a_real_stop_count(self) -> None:
        offer = self.data["queries"][0]["offers"][0]
        self.assertEqual(offer["stops_count"], 0)
        self.assertIsNotNone(offer["stops"])
        self.assertEqual(len(offer["legs"]), 1)
        self.assertEqual(offer["legs"][0]["departure"], offer["departure"])

    def test_missing_typical_baseline_is_null_not_invented(self) -> None:
        offer = self.data["queries"][0]["offers"][0]
        self.assertIsNone(offer["typical_eur"])
        self.assertIsNone(offer["vs_typical"])

    def test_owned_typical_serialises_with_a_coarse_label(self) -> None:
        offer = FlightOffer(
            airline="Norse Atlantic",
            departure="21:15",
            arrival="09:40",
            price="€289",
            price_eur=289.0,
            duration="7 hr 25 min",
            duration_hours=7.42,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=70,
            needs_bag_verify=True,
            typical_eur=340.0,
            vs_typical="below",
        )
        data = offer.to_dict()
        self.assertEqual(data["typical_eur"], 340.0)
        self.assertEqual(data["vs_typical"], "below")
        self.assertEqual(set(data), OFFER_KEYS)

    def test_two_stop_offer_hides_string_layover_city(self) -> None:
        offer = FlightOffer(
            airline="Iberia",
            departure="07:00",
            arrival="22:00",
            price="€199",
            price_eur=199.0,
            duration="15 hr",
            duration_hours=15.0,
            stops="2 stops",
            stops_count=2,
            layover_city=None,
            layover_hours=None,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
            legs=(
                RawJourneyLeg(
                    departure="07:00",
                    arrival="22:00",
                    duration="15 hr",
                    stops="2 stops",
                    layovers=(
                        RawLayover(city="LIS", hours=2.0),
                        RawLayover(city="GRU", hours=3.5),
                    ),
                ),
            ),
        )
        data = offer.to_dict()
        self.assertIsNone(data["layover_city"])
        self.assertEqual([row["city"] for row in data["legs"][0]["layovers"]], ["LIS", "GRU"])

    def test_error_codes_serialise_as_their_string_values(self) -> None:
        self.assertEqual(self.data["queries"][1]["error"]["code"], "no_results")

    def test_schema_version_stays_1(self) -> None:
        self.assertEqual(self.data["schema_version"], 1)

    def test_fetch_backend_is_in_the_closed_set(self) -> None:
        self.assertIn(self.data["fetch_backend"], FLIGHT_FETCH_BACKENDS)

    def test_forbidden_keys_are_absent(self) -> None:
        blob = json.dumps(self.data)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(f'"{key}"', blob)

    def test_the_whole_report_is_json_serialisable(self) -> None:
        json.loads(json.dumps(self.data, ensure_ascii=False))

    def test_round_trip_query_carries_return_date_and_trip_kind(self) -> None:
        query = RoundTrip("MAD", "PRG", date(2026, 12, 3), date(2026, 12, 9))
        offer = FlightOffer(
            airline="Iberia",
            departure="07:00",
            arrival="09:30",
            price="€209",
            price_eur=209.0,
            duration="2 hr 30 min",
            duration_hours=2.5,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
            legs=(
                RawJourneyLeg(departure="07:00", arrival="09:30", duration="2 hr 30 min"),
                RawJourneyLeg(departure="14:00", arrival="16:20", duration="2 hr 20 min"),
            ),
        )
        data = QuerySuccess(query=query, raw_count=1, eligible_count=1, offers=(offer,)).to_dict()
        self.assertEqual(data["query"]["trip"], "rt")
        self.assertEqual(data["query"]["return_date"], "2026-12-09")
        self.assertEqual(data["query"]["departure_date"], "2026-12-03")
        self.assertEqual(len(data["offers"][0]["legs"]), 2)
        self.assertEqual(data["offers"][0]["legs"][1]["departure"], "14:00")
        self.assertEqual(data["offers"][0]["legs"][1]["arrival"], "16:20")


if __name__ == "__main__":
    unittest.main()
