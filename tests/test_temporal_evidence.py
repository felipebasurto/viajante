from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta

from test_audit_regressions import FUTURE, _card, _search, _Source
from viajante.google_flights_rpc import _segments_from_flight
from viajante.models import FlightOffer, RawJourneyLeg, RawSegment, RoundTrip
from viajante.temporal import local_instant
from viajante.validate import validate_itinerary


def segment(origin="JFK", destination="LHR", **extra):
    return {
        "origin": origin,
        "destination": destination,
        "departure_date": "2099-07-01",
        "arrival_date": "2099-07-02",
        "departure": "22:00",
        "arrival": "10:00",
        "departure_timezone": "America/New_York",
        "arrival_timezone": "Europe/London",
        **extra,
    }


def row(segments, *, key="one", trip="one-way", journeys=None):
    query = {
        "origin": segments[0]["origin"],
        "destination": segments[-1]["destination"],
        "departure_date": segments[0].get("departure_date"),
        "trip": trip,
    }
    return {
        "status": "ok",
        "query": query,
        "offer": {
            "price": 100,
            "stops_count": 0,
            "evidence": {"evidence_id": key, "query": dict(query), "currency": "USD"},
            "legs": journeys or [{"segments": segments}],
        },
    }


def check(rows, constraint, value):
    report = validate_itinerary(rows, {constraint: value})
    return next(item.status for item in report.checks if item.constraint == constraint)


class TemporalEvidenceTests(unittest.TestCase):
    def test_missing_return_does_not_prove_package_dates_or_timezones(self):
        raw = RawSegment(
            "JFK",
            "LHR",
            departure_date=FUTURE,
            arrival_date=FUTURE,
            departure_timezone="America/New_York",
            arrival_timezone="Europe/London",
        )
        query = RoundTrip("JFK", "LHR", FUTURE, FUTURE + timedelta(days=3))
        report = _search(
            query, _Source([_card(legs=(RawJourneyLeg(None, None, segments=(raw,)),))])
        )
        completeness = report.queries[0].offers[0].completeness
        self.assertEqual(completeness.segment_dates, "unknown")
        self.assertEqual(completeness.segment_timezones, "unknown")

    def test_compact_segment_keeps_owned_dates_and_catalogue_zones(self):
        leg = [None] * 23
        leg[3], leg[6], leg[8], leg[10] = "JFK", "LHR", [22, 0], [10, 0]
        leg[20], leg[21], leg[22] = [2099, 7, 1], [2099, 7, 2], ["BA", "100", None, "BA"]
        parsed = _segments_from_flight([None, None, [leg]])[0]
        self.assertEqual(parsed.arrival_date, date(2099, 7, 2))
        self.assertEqual(parsed.departure_timezone, "America/New_York")
        self.assertEqual(parsed.arrival_timezone, "Europe/London")
        self.assertEqual(parsed.to_dict()["arrival_date"], "2099-07-02")
        leg[21], leg[6] = None, "ZZZ"
        unknown = _segments_from_flight([None, None, [leg]])[0]
        self.assertIsNone(unknown.arrival_date)
        self.assertIsNone(unknown.arrival_timezone)

    def test_completeness_dates_and_zones_are_explicit(self):
        raw = RawSegment("JFK", "LHR", departure_date=date(2099, 7, 1))
        offer = FlightOffer(
            None,
            None,
            None,
            "$100",
            100,
            None,
            None,
            None,
            None,
            0,
            False,
            legs=(RawJourneyLeg(None, None, segments=(raw,)),),
        )
        self.assertEqual(offer.completeness.segment_dates, "unknown")
        self.assertEqual(offer.completeness.segment_timezones, "unknown")

    def test_next_day_deadline_and_equality(self):
        selected = [row([segment()])]
        for deadline, expected in [
            ("2099-07-01T23:00", "fail"),
            ("2099-07-02T10:00", "pass"),
            ("2099-07-02T09:59", "fail"),
        ]:
            self.assertEqual(check(selected, "arrival_deadline", deadline), expected)
        self.assertEqual(check(selected, "chronological", True), "pass")

    def test_aware_deadline_is_compared_in_utc(self):
        # 10:00 Europe/London on 2099-07-02 is 09:00Z. An explicit offset is that
        # instant, even when it is not the airport's civil clock.
        selected = [row([segment()])]
        self.assertEqual(check(selected, "arrival_deadline", "2099-07-02T09:00Z"), "pass")
        self.assertEqual(check(selected, "arrival_deadline", "2099-07-02T08:59Z"), "fail")
        self.assertEqual(check(selected, "arrival_deadline", "2099-07-02T05:00-04:00"), "pass")

    def test_date_line_uses_utc_not_civil_date_order(self):
        flight = segment(
            "NRT",
            "LAX",
            departure_date="2099-07-02",
            arrival_date="2099-07-01",
            departure="01:00",
            arrival="18:00",
            departure_timezone="Asia/Tokyo",
            arrival_timezone="America/Los_Angeles",
        )
        self.assertEqual(check([row([flight])], "chronological", True), "pass")

    def test_dst_ambiguous_nonexistent_and_unknown_zone(self):
        for civil in ["2026-11-01T01:30", "2026-03-08T02:30"]:
            self.assertIsNone(local_instant(datetime.fromisoformat(civil), "America/New_York"))
        self.assertIsNone(local_instant(datetime(2026, 1, 1), "Unowned/Zone"))
        self.assertIsNotNone(local_instant(datetime(2026, 11, 1, 3), "America/New_York"))

    def test_missing_and_dst_segment_time_is_unknown(self):
        for extras in [
            {"arrival_date": None},
            {"arrival_timezone": None},
            {
                "arrival_date": "2026-11-01",
                "arrival": "01:30",
                "arrival_timezone": "America/New_York",
            },
        ]:
            selected = [row([segment(**extras)])]
            self.assertEqual(check(selected, "chronological", True), "unknown")
            self.assertEqual(check(selected, "arrival_deadline", "2099-07-03T10:00"), "unknown")

    def test_overlap_and_negative_flight_are_failures(self):
        first = row([segment()])
        second = row(
            [
                segment(
                    "LHR",
                    "JFK",
                    departure_date="2099-07-02",
                    departure="09:00",
                    departure_timezone="Europe/London",
                    arrival_date="2099-07-02",
                    arrival="15:00",
                    arrival_timezone="America/New_York",
                )
            ],
            key="two",
        )
        self.assertEqual(check([first, second], "chronological", True), "fail")
        reversed_time = segment(arrival_date="2099-07-01", arrival="10:00")
        self.assertEqual(check([row([reversed_time])], "chronological", True), "fail")

    def test_stay_counts_local_dates_and_boundaries(self):
        first = row([segment()])
        second = row(
            [
                segment(
                    "LHR",
                    "JFK",
                    departure_date="2099-07-05",
                    departure="11:00",
                    departure_timezone="Europe/London",
                    arrival_date="2099-07-05",
                    arrival="15:00",
                    arrival_timezone="America/New_York",
                )
            ],
            key="two",
        )
        for constraint, bound, status in [
            ("min_stay_days", 3, "pass"),
            ("min_stay_days", 4, "fail"),
            ("max_stay_days", 3, "pass"),
            ("max_stay_days", 2, "fail"),
        ]:
            self.assertEqual(check([first, second], constraint, bound), status)
        self.assertEqual(check([first], "min_stay_days", 100), "unknown")
        self.assertEqual(check([first], "max_stay_days", 1), "unknown")
        report = validate_itinerary([first], {"min_stay_days": 1, "max_stay_days": 1})
        for constraint in ("min_stay_days", "max_stay_days"):
            item = next(check for check in report.checks if check.constraint == constraint)
            self.assertEqual(item.status, "unknown")
            self.assertEqual(item.detail, "no intervening stay between selected journeys")

    def test_packaged_stay_and_final_deadline_use_last_journey(self):
        inbound = segment()
        outbound = segment(
            "LHR",
            "JFK",
            departure_date="2099-07-05",
            departure="11:00",
            departure_timezone="Europe/London",
            arrival_date="2099-07-05",
            arrival="15:00",
            arrival_timezone="America/New_York",
        )
        selected = [
            row([inbound], trip="rt", journeys=[{"segments": [inbound]}, {"segments": [outbound]}])
        ]
        self.assertEqual(check(selected, "min_stay_days", 3), "pass")
        self.assertEqual(check(selected, "max_stay_days", 2), "fail")
        self.assertEqual(check(selected, "arrival_deadline", "2099-07-05T14:59"), "fail")
        self.assertEqual(check(selected, "chronological", True), "pass")

    def test_packaged_missing_last_journey_is_unknown(self):
        selected = [row([segment()], trip="rt")]
        for constraint, value in [
            ("chronological", True),
            ("min_stay_days", 2),
            ("arrival_deadline", "2099-07-03T10:00"),
        ]:
            self.assertEqual(check(selected, constraint, value), "unknown")

    def test_disconnected_or_missing_stay_is_unknown(self):
        first = row([segment()])
        second = row([segment("MAD", "JFK", departure_date="2099-07-05")], key="two")
        self.assertEqual(check([first, second], "min_stay_days", 1), "unknown")
        second["offer"]["legs"] = [{"segments": []}]
        self.assertEqual(check([first, second], "max_stay_days", 1), "unknown")

    def test_legacy_travel_window_stays_departure_date_based(self):
        report = validate_itinerary(
            [row([segment()])], {"travel_start": "2099-07-01", "travel_end": "2099-07-01"}
        )
        self.assertEqual(
            next(c.status for c in report.checks if c.constraint == "travel_window"), "pass"
        )
