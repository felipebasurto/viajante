"""Errors name what failed: the value given, the query asked, the request that answered."""

from __future__ import annotations

import _isolate  # noqa: F401
import json
import unittest
from datetime import date
from unittest.mock import patch

from viajante import evidence
from viajante.carriers import parse_airline_codes, parse_alliances
from viajante.control import validate_deadline_seconds
from viajante.dates import validate_date_window
from viajante.flight_filters import parse_depart_window, parse_named_clock
from viajante.google_flights import (
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    NoFlightsFound,
    SweepHttpResponse,
)
from viajante.google_flights_public import (
    PublicGoogleFlightsHttpSource,
    classify_failure,
    trip_label,
)
from viajante.models import FlightQuery, RoundTrip

OUT = date(2027, 3, 14)
BACK = date(2027, 3, 21)
URL = "https://www.google.com/travel/flights?tfs=secret&hl=en"


def _source() -> PublicGoogleFlightsHttpSource:
    return PublicGoogleFlightsHttpSource(currency="EUR", client=object())


class GoogleFailureContextTests(unittest.TestCase):
    def test_trip_label_names_every_leg_without_a_price_or_a_token(self) -> None:
        self.assertEqual(trip_label(FlightQuery("JFK", "LHR", OUT)), "JFK-LHR 2027-03-14")
        self.assertEqual(
            trip_label(RoundTrip("JFK", "LHR", OUT, BACK)),
            "JFK-LHR 2027-03-14, LHR-JFK 2027-03-21",
        )
        self.assertEqual(trip_label(None), "")

    def test_an_unreadable_page_carries_the_request_diagnostics_and_the_query(self) -> None:
        trip = FlightQuery("JFK", "LHR", OUT)
        response = SweepHttpResponse(200, "<html><body>nothing</body></html>", URL)
        with self.assertRaises(GoogleFlightsMarkupError) as caught:
            _source()._parse_response(response, URL, trip)
        error = classify_failure(caught.exception)
        self.assertEqual(error.code.value, "markup_drift")
        diagnostics = error.diagnostics or {}
        self.assertEqual(diagnostics["endpoint"], "www.google.com/travel/flights")
        self.assertIs(diagnostics["request_sent"], True)
        self.assertEqual(diagnostics["http_status"], 200)
        self.assertNotIn("secret", json.dumps(error.to_dict()))

    def test_markup_drift_without_its_own_detail_names_the_query(self) -> None:
        exc = GoogleFlightsMarkupError("")
        exc.trip_label = "JFK-LHR 2027-03-14"
        error = classify_failure(exc)
        self.assertIn("a page for JFK-LHR 2027-03-14 that could not be read", error.message)
        self.assertIn("Wait and retry this one search once", error.message)

    def test_no_flights_and_rejected_name_the_route_and_date(self) -> None:
        trip = FlightQuery("JFK", "LHR", OUT)
        response = SweepHttpResponse(200, "<html></html>", URL)
        for raised, code, phrase in (
            (NoFlightsFound(), "no_results", "no flights for JFK-LHR 2027-03-14"),
            (GoogleFlightsRejected(), "rejected", "rejected the query for JFK-LHR 2027-03-14"),
        ):
            with self.subTest(code=code):
                with patch.object(_source().__class__, "_parse_page", side_effect=raised):
                    with self.assertRaises(type(raised)) as caught:
                        _source()._parse_response(response, URL, trip)
                error = classify_failure(caught.exception)
                self.assertEqual(error.code.value, code)
                self.assertIn(phrase, error.message)
                self.assertEqual(
                    (error.diagnostics or {})["endpoint"], "www.google.com/travel/flights"
                )

    def test_without_a_trip_the_generic_sentences_stay(self) -> None:
        self.assertEqual(
            classify_failure(NoFlightsFound()).message,
            "Google Flights returned no flights for this route and date.",
        )
        self.assertIsNone(classify_failure(GoogleFlightsRejected()).diagnostics)


class InputErrorsEchoTheValueTests(unittest.TestCase):
    def assertRaisesValue(self, call, *needles: str) -> None:
        with self.assertRaises(ValueError) as caught:
            call()
        for needle in needles:
            self.assertIn(needle, str(caught.exception))

    def test_depart_window(self) -> None:
        self.assertRaisesValue(
            lambda: parse_depart_window("late"), "'late'", "must look like 6-20 or 06:00-20:00"
        )
        self.assertRaisesValue(lambda: parse_depart_window("20-6"), "'20-6'", "start must be")
        self.assertRaisesValue(lambda: parse_depart_window("  "), "''")

    def test_named_clock(self) -> None:
        self.assertRaisesValue(
            lambda: parse_named_clock("noon", role="depart_after"), "depart_after 'noon'", "06:30"
        )

    def test_empty_carrier_lists_show_what_was_given_and_an_example(self) -> None:
        self.assertRaisesValue(lambda: parse_airline_codes(" , "), "' , '", "LH,UA")
        self.assertRaisesValue(lambda: parse_alliances(","), "','", "oneworld, skyteam or star")

    def test_date_window_shows_both_dates(self) -> None:
        self.assertRaisesValue(
            lambda: validate_date_window(date(2027, 5, 9), date(2027, 5, 2)),
            "end date 2027-05-02",
            "start date 2027-05-09",
        )

    def test_deadline_shows_the_bad_value(self) -> None:
        self.assertRaisesValue(lambda: validate_deadline_seconds("soon"), "'soon'")
        self.assertRaisesValue(lambda: validate_deadline_seconds(-3), "-3")


class SelectionErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence._selections.clear()
        self.addCleanup(evidence._selections.clear)

    def test_an_unknown_id_says_why_and_what_to_do(self) -> None:
        with self.assertRaises(ValueError) as caught:
            evidence.selected_reference("sel_x", "hotel")
        message = str(caught.exception)
        self.assertIn("'sel_x' is not known to this process", message)
        self.assertIn(f"last {evidence.LEDGER_SIZE} searches", message)
        self.assertIn("Run the hotel search again", message)

    def test_an_id_for_another_kind_is_not_called_unknown(self) -> None:
        evidence._selections["sel_f"] = ("flight", 0, 0)
        with self.assertRaises(ValueError) as caught:
            evidence.selected_reference("sel_f", "hotel")
        message = str(caught.exception)
        self.assertIn("belongs to a flight result", message)
        self.assertNotIn("not known", message)


if __name__ == "__main__":
    unittest.main()
