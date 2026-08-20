from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone

from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightLeg,
    FlightOffer,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    MultiCity,
    PropertyTypeEvidence,
    QueryFailure,
    QuerySuccess,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
    StopsCompare,
    StopsCompareSide,
    normalize_country,
    normalize_currency,
)


class ModelTests(unittest.TestCase):
    def test_flight_query_validation(self) -> None:
        q = FlightQuery("mad", "bcn", date(2026, 9, 1), max_stops=1)
        self.assertEqual(q.origin, "MAD")
        self.assertEqual(q.destination, "BCN")
        self.assertEqual(q.adults, 1)
        self.assertEqual(q.cabin, "economy")
        self.assertEqual(FlightQuery("MAD", "NRT", date(2026, 9, 1), max_stops=2).max_stops, 2)
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=3)
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), adults=0)
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), cabin="space")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            FlightQuery("XXX", "BCN", date(2026, 9, 1))
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "XXX", date(2026, 9, 1))
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), children=-1)
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), infants_on_lap=2)

    def test_flight_query_occupancy_is_omitted_until_nonzero(self) -> None:
        data = FlightQuery(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            adults=2,
            children=1,
            infants_in_seat=0,
            infants_on_lap=1,
        ).to_dict()
        self.assertEqual(data["adults"], 2)
        self.assertEqual(data["children"], 1)
        self.assertEqual(data["infants_on_lap"], 1)
        self.assertNotIn("infants_in_seat", data)
        self.assertNotIn("children", FlightQuery("MAD", "BCN", date(2026, 9, 1)).to_dict())

    def test_currency_and_country_codes(self) -> None:
        self.assertEqual(normalize_currency("eur"), "EUR")
        self.assertEqual(normalize_country("us"), "US")
        self.assertIsNone(normalize_country(None))
        self.assertIsNone(normalize_country("  "))
        with self.assertRaises(ValueError):
            normalize_currency("euro")
        with self.assertRaises(ValueError):
            normalize_country("USA")

    def test_flight_query_legs_are_a_single_owned_leg(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=0)
        self.assertEqual(len(query.legs), 1)
        self.assertEqual(query.legs[0], FlightLeg("MAD", "BCN", date(2026, 9, 1), max_stops=0))

    def test_flight_query_to_dict_stays_one_way(self) -> None:
        data = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1).to_dict()
        self.assertEqual(
            set(data),
            {
                "trip",
                "origin",
                "destination",
                "departure_date",
                "max_stops",
                "adults",
                "cabin",
            },
        )
        self.assertEqual(data["trip"], "one-way")
        self.assertNotIn("return_date", data)
        self.assertNotIn("legs", data)
        self.assertNotIn("bags", data)
        self.assertNotIn("carry_on", data)

    def test_flight_query_bags_are_omitted_until_requested(self) -> None:
        data = FlightQuery("MAD", "BCN", date(2026, 9, 1), bags=1, carry_on=0).to_dict()
        self.assertEqual(data["bags"], 1)
        self.assertEqual(data["carry_on"], 0)
        with self.assertRaises(ValueError):
            FlightQuery("MAD", "BCN", date(2026, 9, 1), bags=-1)

    def test_flight_leg_accepts_two_stops(self) -> None:
        leg = FlightLeg("MAD", "NRT", date(2026, 10, 1), max_stops=2)
        self.assertEqual(leg.max_stops, 2)
        with self.assertRaises(ValueError):
            FlightLeg("MAD", "NRT", date(2026, 10, 1), max_stops=3)
        with self.assertRaises(ValueError):
            FlightLeg("MAD", "MAD", date(2026, 10, 1))

    def test_round_trip_mirrors_legs_and_rejects_open_jaw(self) -> None:
        trip = RoundTrip("MAD", "OPO", date(2026, 10, 9), date(2026, 10, 12), max_stops=1)
        self.assertEqual(trip.adults, 1)
        self.assertEqual(trip.cabin, "economy")
        self.assertEqual(
            trip.legs,
            (
                FlightLeg("MAD", "OPO", date(2026, 10, 9), max_stops=1),
                FlightLeg("OPO", "MAD", date(2026, 10, 12), max_stops=1),
            ),
        )
        with self.assertRaises(ValueError):
            RoundTrip("MAD", "OPO", date(2026, 10, 12), date(2026, 10, 9))
        with self.assertRaises(ValueError):
            RoundTrip("MAD", "MAD", date(2026, 10, 9), date(2026, 10, 12))
        data = trip.to_dict()
        self.assertEqual(data["trip"], "rt")
        self.assertEqual(data["return_date"], "2026-10-12")

    def test_multi_city_requires_two_legs_and_non_decreasing_dates(self) -> None:
        first = FlightLeg("MAD", "BCN", date(2026, 9, 1))
        second = FlightLeg("BCN", "FCO", date(2026, 9, 3))
        trip = MultiCity((first, second), adults=2, cabin="business")
        self.assertEqual(trip.legs, (first, second))
        self.assertEqual(trip.adults, 2)
        self.assertEqual(trip.cabin, "business")
        with self.assertRaises(ValueError):
            MultiCity((first,))
        with self.assertRaises(ValueError):
            MultiCity((second, first))
        same_day = MultiCity(
            (first, FlightLeg("BCN", "FCO", date(2026, 9, 1))),
        )
        self.assertEqual(same_day.legs[1].departure_date, date(2026, 9, 1))

    def test_flight_offer_requires_positive_price(self) -> None:
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="09:00",
                price="0 €",
                price_eur=0.0,
                duration="1 h",
                duration_hours=1.0,
                stops="Directo",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
            )

    def test_query_result_variants(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1))
        offer = FlightOffer(
            airline="Air",
            departure="08:00",
            arrival="09:00",
            price="100 €",
            price_eur=100.0,
            duration="1 h",
            duration_hours=1.0,
            stops="Directo",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
        )
        success = QuerySuccess(query=query, raw_count=1, eligible_count=1, offers=(offer,))
        failure = QueryFailure(
            query=query,
            error=SearchError(SearchErrorCode.FETCH_FAILED, "failed"),
        )
        self.assertEqual(success.status, "ok")
        self.assertEqual(failure.status, "error")
        self.assertEqual(success.eligible_count, 1)
        with self.assertRaises(ValueError):
            QuerySuccess(query=query, raw_count=0, eligible_count=1, offers=(offer,))
        with self.assertRaises(ValueError):
            QuerySuccess(query=query, raw_count=1, eligible_count=0, offers=(offer,))

    def test_stops_compare_rejects_empty_and_wrong_buckets(self) -> None:
        nonstop = StopsCompareSide.from_offer(
            FlightOffer(
                airline="Iberia",
                departure="08:00",
                arrival="09:00",
                price="100 €",
                price_eur=100.0,
                duration="1 h",
                duration_hours=1.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer_eur=0,
                needs_bag_verify=False,
            )
        )
        with self.assertRaises(ValueError):
            StopsCompare()
        with self.assertRaises(ValueError):
            StopsCompare(
                nonstop=StopsCompareSide(
                    airline="Air",
                    price="80 €",
                    price_eur=80.0,
                    duration="3 h",
                    duration_hours=3.0,
                    stops="1 stop",
                    stops_count=1,
                )
            )
        compare = StopsCompare(nonstop=nonstop)
        self.assertEqual(compare.to_dict()["nonstop"]["price_eur"], 100.0)
        self.assertNotIn("one_stop", compare.to_dict())

    def test_search_report_json(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=0)
        report = SearchReport(
            searched_at=datetime(2026, 8, 10, 9, 20, 0),
            queries=(
                QueryFailure(
                    query=query,
                    error=SearchError(
                        SearchErrorCode.FETCH_FAILED,
                        "Google Flights search failed after 3 attempts.",
                    ),
                ),
            ),
        )
        data = report.to_dict()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(data["locale"], "en")
        self.assertEqual(data["queries"][0]["status"], "error")
        self.assertEqual(data["queries"][0]["error"]["code"], "fetch_failed")
        json.dumps(data)


class ReportTimestampTests(unittest.TestCase):
    def test_offset_aware_timestamps_are_converted_to_real_utc(self) -> None:
        madrid = datetime(2026, 8, 10, 9, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        report = SearchReport(searched_at=madrid, queries=())
        self.assertEqual(report.to_dict()["searched_at"], "2026-08-10T07:00:00Z")

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        report = SearchReport(searched_at=datetime(2026, 8, 10, 9, 0, 0), queries=())
        self.assertEqual(report.to_dict()["searched_at"], "2026-08-10T09:00:00Z")


class BaggageInvariantTests(unittest.TestCase):
    def _offer(self, *, buffer_eur: int, needs_verify: bool) -> FlightOffer:
        return FlightOffer(
            airline="Ryanair",
            departure="08:00",
            arrival="09:00",
            price="€50",
            price_eur=50.0,
            duration="1 hr",
            duration_hours=1.0,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=buffer_eur,
            needs_bag_verify=needs_verify,
        )

    def test_a_buffer_cannot_be_applied_without_flagging_the_carrier(self) -> None:
        with self.assertRaises(ValueError):
            self._offer(buffer_eur=70, needs_verify=False)

    def test_a_flagged_carrier_may_carry_no_buffer(self) -> None:
        self.assertEqual(self._offer(buffer_eur=0, needs_verify=True).baggage_buffer_eur, 0)

    def test_parsed_bag_counts_serialise_only_when_present(self) -> None:
        known = FlightOffer(
            airline="Ryanair",
            departure="08:00",
            arrival="09:00",
            price="€50",
            price_eur=50.0,
            duration="1 hr",
            duration_hours=1.0,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer_eur=0,
            needs_bag_verify=False,
            checked_bags=1,
            carry_on=1,
        )
        data = known.to_dict()
        self.assertEqual(data["checked_bags"], 1)
        self.assertEqual(data["carry_on"], 1)
        omitted = self._offer(buffer_eur=0, needs_verify=True).to_dict()
        self.assertNotIn("checked_bags", omitted)
        self.assertNotIn("carry_on", omitted)

    def test_negative_buffers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._offer(buffer_eur=-1, needs_verify=True)


class HotelModelTests(unittest.TestCase):
    def test_hotel_query_validation_and_nights(self) -> None:
        q = HotelQuery("  Praga  ", date(2026, 12, 4), date(2026, 12, 8))
        self.assertEqual(q.location, "Praga")
        q2 = HotelQuery("New   York", date(2026, 12, 4), date(2026, 12, 8))
        self.assertEqual(q2.location, "New York")
        self.assertEqual(q.nights, 4)
        self.assertTrue(q.free_cancellation)
        with self.assertRaises(ValueError):
            HotelQuery("   ", date(2026, 12, 4), date(2026, 12, 8))
        with self.assertRaises(ValueError):
            HotelQuery("Praga", date(2026, 12, 8), date(2026, 12, 4))
        with self.assertRaises(ValueError):
            HotelQuery("Praga", date(2026, 12, 4), date(2026, 12, 8), adults=0)
        with self.assertRaises(ValueError):
            HotelQuery("Praga", date(2026, 12, 4), date(2026, 12, 8), rooms=0)
        with self.assertRaises(ValueError):
            HotelQuery(
                "Praga",
                date(2026, 12, 4),
                date(2026, 12, 8),
                min_rating=10.1,
            )

    def test_hotel_query_to_dict(self) -> None:
        q = HotelQuery(
            "Praga",
            date(2026, 12, 4),
            date(2026, 12, 8),
            adults=2,
            rooms=1,
            min_rating=8.0,
            entire_home=True,
            free_cancellation=False,
        )
        data = q.to_dict()
        self.assertEqual(data["location"], "Praga")
        self.assertEqual(data["check_in"], "2026-12-04")
        self.assertEqual(data["check_out"], "2026-12-08")
        self.assertEqual(data["nights"], 4)
        self.assertEqual(data["min_rating"], 8.0)
        self.assertTrue(data["entire_home"])
        self.assertFalse(data["free_cancellation"])

    def test_hotel_offer_validation(self) -> None:
        with self.assertRaises(ValueError):
            HotelOffer(
                title="   ",
                address=None,
                total_price="100 €",
                total_price_eur=100.0,
                rating=None,
                rating_score=None,
                details="",
                cancellation_evidence=CancellationEvidence.UNKNOWN,
                property_type_evidence=PropertyTypeEvidence.UNKNOWN,
                lodging_kind=LodgingKind.UNKNOWN,
                bedrooms=None,
                bathrooms=None,
                beds=None,
                link=None,
            )
        with self.assertRaises(ValueError):
            HotelOffer(
                title="Hotel",
                address=None,
                total_price="0 €",
                total_price_eur=0.0,
                rating=None,
                rating_score=None,
                details="",
                cancellation_evidence=CancellationEvidence.UNKNOWN,
                property_type_evidence=PropertyTypeEvidence.UNKNOWN,
                lodging_kind=LodgingKind.UNKNOWN,
                bedrooms=None,
                bathrooms=None,
                beds=None,
                link=None,
            )
        with self.assertRaises(ValueError):
            HotelOffer(
                title="Hotel",
                address=None,
                total_price="100 €",
                total_price_eur=100.0,
                rating="11",
                rating_score=11.0,
                details="",
                cancellation_evidence=CancellationEvidence.UNKNOWN,
                property_type_evidence=PropertyTypeEvidence.UNKNOWN,
                lodging_kind=LodgingKind.UNKNOWN,
                bedrooms=None,
                bathrooms=None,
                beds=None,
                link=None,
            )

    def test_hotel_query_success_count_invariant(self) -> None:
        query = HotelQuery("Praga", date(2026, 12, 4), date(2026, 12, 8))
        applied = AppliedHotelFilters(chips=("free_cancellation",), url="https://example.com")
        offer = HotelOffer(
            title="Hotel Test",
            address="Praga 1",
            total_price="200 €",
            total_price_eur=200.0,
            rating="Rating: 8.4",
            rating_score=8.4,
            details="2 bedrooms",
            cancellation_evidence=CancellationEvidence.FREE,
            property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
            lodging_kind=LodgingKind.ENTIRE_HOME,
            bedrooms=2,
            bathrooms=1,
            beds=3,
            link="https://booking.com/hotel",
        )
        success = HotelQuerySuccess(
            query=query,
            applied=applied,
            raw_count=10,
            eligible_count=5,
            offers=(offer,),
        )
        self.assertEqual(success.status, "ok")
        with self.assertRaises(ValueError):
            HotelQuerySuccess(
                query=query,
                applied=applied,
                raw_count=1,
                eligible_count=5,
                offers=(offer,),
            )
        offer_two = HotelOffer(
            title="Hotel Two",
            address="Praga 2",
            total_price="150 €",
            total_price_eur=150.0,
            rating=None,
            rating_score=None,
            details="",
            cancellation_evidence=CancellationEvidence.UNKNOWN,
            property_type_evidence=PropertyTypeEvidence.UNKNOWN,
            lodging_kind=LodgingKind.UNKNOWN,
            bedrooms=None,
            bathrooms=None,
            beds=None,
            link=None,
        )
        with self.assertRaises(ValueError):
            HotelQuerySuccess(
                query=query,
                applied=applied,
                raw_count=10,
                eligible_count=1,
                offers=(offer, offer_two),
            )

    def test_hotel_query_result_variants_json(self) -> None:
        query = HotelQuery("Praga", date(2026, 12, 4), date(2026, 12, 8))
        applied = AppliedHotelFilters(chips=("free_cancellation",), url="https://example.com")
        offer = HotelOffer(
            title="Hotel Test",
            address="Praga 1",
            total_price="200 €",
            total_price_eur=200.0,
            rating="8,4",
            rating_score=8.4,
            details="",
            cancellation_evidence=CancellationEvidence.FREE,
            property_type_evidence=PropertyTypeEvidence.UNKNOWN,
            lodging_kind=LodgingKind.UNKNOWN,
            bedrooms=None,
            bathrooms=None,
            beds=None,
            link=None,
        )
        success = HotelQuerySuccess(
            query=query,
            applied=applied,
            raw_count=3,
            eligible_count=2,
            offers=(offer,),
        )
        failure = HotelQueryFailure(
            query=query,
            applied=applied,
            error=SearchError(SearchErrorCode.FETCH_FAILED, "failed"),
        )
        success_data = success.to_dict()
        failure_data = failure.to_dict()
        self.assertEqual(success_data["status"], "ok")
        self.assertEqual(success_data["raw_count"], 3)
        self.assertEqual(success_data["eligible_count"], 2)
        self.assertEqual(
            success_data["offers"][0]["cancellation_evidence"],
            "free",
        )
        self.assertEqual(failure_data["status"], "error")
        self.assertEqual(failure_data["applied"]["url"], "https://example.com")
        json.dumps(success_data)
        json.dumps(failure_data)

    def test_hotel_search_report_json(self) -> None:
        query = HotelQuery("Praga", date(2026, 12, 4), date(2026, 12, 8))
        applied = AppliedHotelFilters(chips=(), url="https://example.com")
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 20, 0),
            queries=(
                HotelQueryFailure(
                    query=query,
                    applied=applied,
                    error=SearchError(
                        SearchErrorCode.FETCH_FAILED,
                        "Booking search failed.",
                    ),
                ),
            ),
        )
        data = report.to_dict()
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["provider"], "booking.com")
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(data["locale"], "en")
        self.assertEqual(data["price_basis"], "total_stay")
        self.assertEqual(data["searched_at"], "2026-08-10T09:20:00Z")
        self.assertEqual(data["queries"][0]["status"], "error")
        json.dumps(data)


if __name__ == "__main__":
    unittest.main()
