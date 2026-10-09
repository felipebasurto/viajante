from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import _isolate  # noqa: F401
from viajante import mcp_handlers
from viajante.cli import main
from viajante.flight_routes import (
    drop_excluded_airport_trips,
    expand_nearby_trips,
    keep_included_dest_trips,
    nearby_notes,
    parse_flight_plan,
)
from viajante.flights import search_flights
from viajante.google_flights import RawFlightCard
from viajante.history import flight_observations
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    RoundTrip,
)
from viajante.trip import search_trip

DAY = date(2026, 11, 10)
BACK = date(2026, 11, 14)


def _pairs(trips) -> list[tuple[str, str]]:
    return [(trip.origin, trip.destination) for trip in trips]


class IncludeAirportsReturnTests(unittest.TestCase):
    def test_a_named_destination_keeps_its_return_leg_of_the_same_trip(self) -> None:
        trips = parse_flight_plan(["JFK-LHR:2026-12-03:2026-12-10"], max_stops=1)
        kept = keep_included_dest_trips(trips, ("LHR",))
        self.assertEqual(
            [(trip.origin, trip.destination) for trip in kept],
            [("JFK", "LHR"), ("LHR", "JFK")],
        )

    def test_a_round_trip_to_another_airport_is_dropped_on_both_legs(self) -> None:
        trips = parse_flight_plan(["JFK-LHR:2026-12-03:2026-12-10"], max_stops=1)
        self.assertEqual(keep_included_dest_trips(trips, ("LGW",)), ())


class MetroPlanTests(unittest.TestCase):
    def test_named_metro_expands_to_owned_members_with_a_metro_label(self) -> None:
        trips = parse_flight_plan([f"nyc-LHR:{DAY}"], max_stops=1)
        self.assertEqual(_pairs(trips), [("JFK", "LHR"), ("EWR", "LHR"), ("LGA", "LHR")])
        self.assertEqual({trip.nearby_label for trip in trips}, {"metro NYC"})
        self.assertEqual({trip.departure_date for trip in trips}, {DAY})

    def test_same_metro_routes_are_rejected(self) -> None:
        specs = (
            f"LON-LON:{DAY}",
            f"JFK-NYC:{DAY}",
            f"NYC-JFK:{DAY}",
            f"LHR-LGW:{DAY}",
            f"JFK-EWR:{DAY}",
            f"LON-LHR:{DAY}",
        )
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(ValueError) as caught:
                parse_flight_plan([spec], max_stops=1)
            message = str(caught.exception)
            origin, destination = spec.split(":", 1)[0].split("-")
            self.assertIn(f"origin {origin}", message)
            self.assertIn(f"destination {destination}", message)
            self.assertIn("same metro", message)
        with self.assertRaises(ValueError) as caught:
            parse_flight_plan([f"LON-LON:{DAY}:{BACK}"], trip="rt", max_stops=1)
        self.assertIn("origin LON and destination LON", str(caught.exception))

    def test_member_airport_is_not_expanded_to_its_metro(self) -> None:
        (trip,) = parse_flight_plan([f"JFK-LHR:{DAY}"], max_stops=1)
        self.assertEqual(_pairs([trip]), [("JFK", "LHR")])
        self.assertIsNone(trip.nearby_label)

    def test_both_sides_and_one_way_out_back_expand_together(self) -> None:
        trips = parse_flight_plan([f"PAR-TYO:{DAY}:{BACK}"], max_stops=1)
        self.assertEqual(len(trips), 8)
        self.assertEqual({trip.nearby_label for trip in trips}, {"metro PAR; metro TYO"})
        self.assertEqual(
            {(t.origin, t.destination) for t in trips if t.departure_date == BACK},
            {(d, o) for o in ("CDG", "ORY") for d in ("NRT", "HND")},
        )

    def test_round_trip_expands_to_one_package_per_member_pair(self) -> None:
        plan = parse_flight_plan([f"MAD-NYC:{DAY}:{BACK}"], trip="rt", max_stops=1)
        self.assertTrue(all(isinstance(trip, RoundTrip) for trip in plan))
        self.assertEqual(_pairs(plan), [("MAD", "JFK"), ("MAD", "EWR"), ("MAD", "LGA")])
        self.assertEqual({trip.return_date for trip in plan}, {BACK})

    def test_open_jaw_and_multi_city_reject_metro_codes(self) -> None:
        cases = (
            ([f"MAD-NYC:{DAY}", f"NYC-LIS:{BACK}"], "rt"),
            ([f"MAD-LHR:{DAY}", f"LON-LIS:{BACK}"], "multi"),
        )
        for specs, kind in cases:
            with self.subTest(kind=kind), self.assertRaises(ValueError) as caught:
                parse_flight_plan(specs, trip=kind, max_stops=1)
            self.assertIn("metro", str(caught.exception))

    def test_nearby_with_a_metro_is_an_error_not_a_silent_partial_expand(self) -> None:
        for spec in (f"NYC-MAD:{DAY}", f"NYC-LHR:{DAY}", f"NYC-LON:{DAY}"):
            trips = parse_flight_plan([spec], max_stops=1)
            with self.subTest(spec=spec), self.assertRaises(ValueError) as caught:
                expand_nearby_trips(trips, nearby=True)
            self.assertIn("--nearby", str(caught.exception))
            self.assertIn("metro", str(caught.exception))
        trips = parse_flight_plan([f"NYC-MAD:{DAY}"], max_stops=1)
        self.assertEqual(expand_nearby_trips(trips, nearby=False), trips)

    def test_exclude_drops_members_and_the_legend_follows(self) -> None:
        trips = parse_flight_plan([f"NYC-MAD:{DAY}"], max_stops=1)
        self.assertEqual(nearby_notes(trips), ("metro NYC: EWR, JFK, LGA",))
        kept = drop_excluded_airport_trips(trips, ("EWR",))
        self.assertEqual(_pairs(kept), [("JFK", "MAD"), ("LGA", "MAD")])
        self.assertEqual(nearby_notes(kept, exclude_airports=("EWR",)), ("metro NYC: JFK, LGA",))


class _FaresByOrigin:
    """Flight source whose single card price depends on the trip's origin airport."""

    def __init__(self, fares: dict[str, str]) -> None:
        self.fares = fares
        self.fetched: list[str] = []
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(self, trip: object) -> tuple[RawFlightCard, ...]:
        origin = trip.origin  # type: ignore[attr-defined]
        self.fetched.append(origin)
        card = RawFlightCard(
            airline="Example Air",
            departure="08:00",
            arrival="20:00",
            duration="7 hr",
            stops="Nonstop",
            price=self.fares[origin],
        )
        return (card,)

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


class MetroTripTotalTests(unittest.TestCase):
    def test_metro_members_take_the_cheapest_fare_not_a_sum(self) -> None:
        plan = parse_flight_plan([f"NYC-LHR:{DAY}:{BACK}"], trip="rt", max_stops=1)
        source = _FaresByOrigin({"JFK": "€300", "EWR": "€120", "LGA": "€200"})
        stay = HotelQuery("London", DAY, BACK)
        offer = HotelOffer(
            title="Example Hotel",
            address="London",
            total_price_text="50 €",
            total_price=50.0,
            rating=None,
            rating_score=None,
            details="Free cancellation",
            cancellation_evidence=CancellationEvidence.FREE,
            property_type_evidence=PropertyTypeEvidence.UNKNOWN,
            lodging_kind=LodgingKind.UNKNOWN,
            bedrooms=None,
            bathrooms=None,
            beds=None,
            link=None,
        )
        hotels = HotelSearchReport(
            searched_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc),
            queries=(
                HotelQuerySuccess(
                    query=stay,
                    applied=AppliedHotelFilters(chips=(), url="https://example.test"),
                    raw_count=1,
                    eligible_count=1,
                    offers=(offer,),
                ),
            ),
            currency="EUR",
            fetch_backend="google",
            provider="google-hotels",
        )
        with (
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=source),
            patch("viajante.trip.search_hotels", return_value=hotels),
        ):
            report = search_trip(plan, stay, fetch="sweep", baggage_buffer=0, currency="EUR")
        self.assertEqual(sorted(source.fetched), ["EWR", "JFK", "LGA"])
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare, 120)
        self.assertEqual(report.trip_total.total, 170)


class MetroSurfaceTests(unittest.TestCase):
    FUTURE = date.today() + timedelta(days=30)

    def test_flights_cli_expands_a_named_metro_and_prints_its_members(self) -> None:
        err = io.StringIO()
        with (
            patch("viajante.cli.search_flights") as search,
            patch("viajante.cli._print_report"),
            patch("viajante.cli._exit_code", return_value=0),
            redirect_stderr(err),
        ):
            code = main(["flights", f"MAD-LON:{self.FUTURE}", "--exclude-airports", "SEN"])
        self.assertEqual(code, 0)
        self.assertEqual(
            [q.destination for q in search.call_args.args[0]],
            ["LHR", "LGW", "STN", "LTN", "LCY", "SEN"],
        )
        self.assertIn("metro LON: LCY, LGW, LHR, LTN, STN\n", err.getvalue())

    def test_mcp_search_flights_expands_a_named_metro(self) -> None:
        mcp_handlers._CACHE.clear()
        fake = MagicMock()
        fake.to_dict.return_value = {"schema_version": 2, "queries": []}
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            mcp_handlers.search_flights_tool([f"WAS-CHI:{self.FUTURE}"], currency="USD")
        trips = search.call_args.args[0]
        self.assertEqual(len(trips), 6)
        self.assertEqual({t.nearby_label for t in trips}, {"metro WAS; metro CHI"})

    def test_metro_fan_out_over_18_sends_nothing(self) -> None:
        day = self.FUTURE.isoformat()
        later = (self.FUTURE + timedelta(days=3)).isoformat()
        routes = [f"LON-NYC:{day}", f"LON-SAO:{day}"]
        message = "metro expansion would send 36 provider queries; the limit is 18 per call"
        mcp_handlers._CACHE.clear()
        with (
            patch("viajante.flights.GoogleFlightsHttpSource") as flights,
            patch("viajante.mcp_handlers._with_search_lock") as locked,
        ):
            with self.assertRaisesRegex(ValueError, message):
                mcp_handlers.search_flights_tool(routes, currency="GBP", fetch="sweep")
            with self.assertRaisesRegex(ValueError, message):
                mcp_handlers.search_trip_tool(
                    routes, "Paris", check_in=day, check_out=later, currency="GBP"
                )
            flights.assert_not_called()
            locked.assert_not_called()

    def test_metro_label_reaches_query_json_and_not_the_history_key(self) -> None:
        day = self.FUTURE
        back = day + timedelta(days=4)
        expanded = parse_flight_plan([f"LON-MAD:{day.isoformat()}"], max_stops=1)
        self.assertEqual(expanded[0].to_dict()["nearby_label"], "metro LON")
        direct = parse_flight_plan([f"LHR-MAD:{day.isoformat()}"], max_stops=1)
        self.assertNotIn("nearby_label", direct[0].to_dict())
        blank = FlightQuery("LHR", "MAD", day, nearby_label="   ")
        self.assertIsNone(blank.nearby_label)
        self.assertNotIn("nearby_label", blank.to_dict())
        packaged = parse_flight_plan(
            [f"MAD-NYC:{day.isoformat()}:{back.isoformat()}"], trip="rt", max_stops=1
        )
        self.assertIsInstance(packaged[0], RoundTrip)
        self.assertEqual(packaged[0].to_dict()["nearby_label"], "metro NYC")

        codes = ("LHR", "LGW", "STN", "LTN", "LCY", "SEN")
        source = _FaresByOrigin({code: "€100" for code in codes})
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            report = search_flights(expanded, fetch="sweep", currency="EUR", baggage_buffer=0)
        data = report.to_dict()
        self.assertEqual(
            {row["query"]["nearby_label"] for row in data["queries"]},
            {"metro LON"},
        )
        row = data["queries"][0]
        evidence_query = row["offers"][0]["evidence"]["query"]
        self.assertEqual(evidence_query["nearby_label"], "metro LON")
        shown = {key: value for key, value in row["query"].items() if key != "google_flights_url"}
        self.assertEqual(shown, evidence_query)

        labeled = flight_observations(report, {})
        direct_source = _FaresByOrigin({"LHR": "€100"})
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=direct_source):
            direct_report = search_flights(direct, fetch="sweep", currency="EUR", baggage_buffer=0)
        plain = flight_observations(direct_report, {})
        metro_lhr = next(item for item in labeled if item["query"]["origin"] == "LHR")
        self.assertEqual(metro_lhr["query"]["nearby_label"], "metro LON")
        self.assertNotIn("nearby_label", plain[0]["query"])
        self.assertEqual(metro_lhr["query_key"], plain[0]["query_key"])

    def test_lon_nyc_alone_sends_18_provider_queries(self) -> None:
        day = self.FUTURE.isoformat()
        trips = parse_flight_plan([f"LON-NYC:{day}"], max_stops=1)
        self.assertEqual(len(trips), 18)
        source = _FaresByOrigin(
            {code: "£100" for code in ("LHR", "LGW", "STN", "LTN", "LCY", "SEN")}
        )
        mcp_handlers._CACHE.clear()
        with patch("viajante.flights.GoogleFlightsHttpSource", return_value=source):
            mcp_handlers.search_flights_tool([f"LON-NYC:{day}"], currency="GBP", fetch="sweep")
        self.assertEqual(len(source.fetched), 18)


if __name__ == "__main__":
    unittest.main()
