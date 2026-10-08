from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante import evidence, mcp_handlers
from viajante.cli import main
from viajante.flight_packages import _attach_missing_legs
from viajante.google_flights import note_rate_limited
from viajante.google_flights_rpc import RawFlightCard
from viajante.models import (
    FlightOffer,
    FlightQuery,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RawSegment,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
)
from viajante.recheck import CAVEAT, format_recheck, recheck_offer

FAR = date(2099, 1, 15)
CHECKED = datetime(2026, 10, 6, 15, 40, tzinfo=timezone.utc)


def _segment(
    origin: str,
    destination: str,
    departure: str,
    arrival: str,
    number: str | None = "BA178",
    *,
    day: date = FAR,
) -> RawSegment:
    return RawSegment(
        origin=origin,
        destination=destination,
        departure=departure,
        arrival=arrival,
        airline="British Airways",
        flight_number=number,
        departure_date=day,
        carrier="BA",
    )


def _offer(price: float, *journeys: tuple[RawSegment, ...]) -> FlightOffer:
    legs = tuple(
        RawJourneyLeg(
            departure=segments[0].departure,
            arrival=segments[-1].arrival,
            segments=segments,
        )
        for segments in journeys
    )
    return FlightOffer(
        airline="British Airways",
        departure=legs[0].departure,
        arrival=legs[0].arrival,
        price_text=f"${price:g}",
        price=price,
        duration="7 hr",
        duration_hours=7.0,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer=0,
        needs_bag_verify=False,
        legs=legs,
    )


def _query(day: date = FAR) -> FlightQuery:
    return FlightQuery("JFK", "LHR", day, max_stops=1, adults=1, cabin="economy")


def _report(
    *offers: FlightOffer,
    currency: str = "USD",
    query: FlightQuery | RoundTrip | None = None,
    raw: int | None = None,
    eligible: int | None = None,
) -> SearchReport:
    count = len(offers)
    return SearchReport(
        searched_at=CHECKED,
        queries=(
            QuerySuccess(
                query=query or _query(),
                raw_count=count if raw is None else raw,
                eligible_count=count if eligible is None else eligible,
                offers=offers,
            ),
        ),
        currency=currency,
        fetch_backend="sweep",
    )


def _failed(code: SearchErrorCode, *, rate_limited: bool = False) -> SearchReport:
    return SearchReport(
        searched_at=CHECKED,
        queries=(
            QueryFailure(
                query=_query(),
                error=SearchError(
                    code=code, message=f"{code.value} message", rate_limited=rate_limited
                ),
            ),
        ),
        currency="USD",
        fetch_backend="sweep",
    )


def _previous(
    *segments: RawSegment,
    price: float = 500.0,
    currency: str | None = "USD",
    query: dict | None = None,
    backend: str = "sweep",
    evidence_id: str | None = "gf_test",
) -> dict:
    row = dict(_offer(price, segments).to_dict("USD"))
    inferred_query = FlightQuery(
        segments[0].origin or "JFK",
        segments[-1].destination or "LHR",
        segments[0].departure_date or FAR,
        max_stops=1,
        adults=1,
        cabin="economy",
    )
    evidence_row: dict = {
        "source": "google_flights",
        "query": query if query is not None else dict(inferred_query.to_dict()),
        "fetch_backend": backend,
    }
    if currency:
        evidence_row["currency"] = currency
    if evidence_id:
        evidence_row["evidence_id"] = evidence_id
    row["evidence"] = evidence_row
    return row


class _Stub:
    def __init__(self, report: SearchReport) -> None:
        self.report = report
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: object, **kwargs: object) -> SearchReport:
        self.calls.append((args, kwargs))
        return self.report


OUTBOUND = _segment("JFK", "LHR", "19:30", "07:30", "BA178")
RETURN_DAY = FAR + timedelta(days=7)
BACK = _segment("LHR", "JFK", "10:00", "13:00", "BA177", day=RETURN_DAY)
RT_QUERY = RoundTrip("JFK", "LHR", FAR, RETURN_DAY, adults=1, cabin="economy")


def _round_trip_previous(price: float = 900.0) -> dict:
    row = dict(_offer(price, (OUTBOUND,), (BACK,)).to_dict("USD"))
    row["evidence"] = {
        "source": "google_flights",
        "query": dict(RT_QUERY.to_dict()),
        "currency": "USD",
        "fetch_backend": "sweep",
        "evidence_id": "gf_rt",
    }
    return row


def _return_card(price: str) -> RawFlightCard:
    leg = RawJourneyLeg(departure="10:00", arrival="13:00", segments=(BACK,))
    return RawFlightCard(None, "10:00", "13:00", None, None, price, legs=(leg,))


class _ReturnShop:
    """Source whose follow-up shop for the next journey fails or answers as told."""

    def __init__(self, answer: object) -> None:
        self.answer = answer

    def fetch_selected(self, trip: object, selections: list) -> list:
        if isinstance(self.answer, Exception):
            raise self.answer
        return [self.answer for _ in selections]


class OutcomeTests(unittest.TestCase):
    def test_same_price_needs_a_fresh_identical_itinerary(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["outcome"], "same_price")
        self.assertTrue(result["check_completed"])
        self.assertEqual(result["checked_at"], "2026-10-06T15:40:00Z")
        self.assertEqual(result["match_basis"], "flight_numbers")
        self.assertEqual(result["previous"]["price"], 500.0)
        self.assertEqual(result["current"]["price"], 500.0)
        self.assertEqual(result["current"]["currency"], "USD")
        self.assertEqual(result["caveat"], CAVEAT)
        ((args, kwargs),) = stub.calls
        self.assertEqual(len(args[0]), 1)
        self.assertEqual(kwargs["sort"], "fare")
        self.assertEqual(kwargs["fetch"], "sweep")
        self.assertEqual(kwargs["currency"], "USD")

    def test_price_changed_reports_old_and_new_in_one_currency(self) -> None:
        stub = _Stub(_report(_offer(540.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertEqual(result["previous"]["price"], 500.0)
        self.assertEqual(result["previous"]["currency"], "USD")
        self.assertNotIn("price_comparable", result)
        self.assertEqual(result["current"]["price"], 540.0)

    def test_other_flights_at_the_same_price_are_not_a_match(self) -> None:
        stub = _Stub(_report(_offer(500.0, (_segment("JFK", "LHR", "09:00", "21:00", "VS4"),))))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertNotEqual(result["outcome"], "same_price")
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "not_among_offers")
        self.assertIsNone(result["current"])

    def test_retimed_flight_is_substituted_and_names_what_differs(self) -> None:
        retimed = _segment("JFK", "LHR", "20:15", "08:15", "BA178")
        result = recheck_offer(
            _previous(OUTBOUND),
            allow_substitute=True,
            search=_Stub(_report(_offer(500.0, (retimed,)))),
        )
        self.assertEqual(result["outcome"], "substituted")
        fields = {row["field"]: row for row in result["differences"]}
        self.assertEqual(fields["departure"]["previous"], "19:30")
        self.assertEqual(fields["departure"]["current"], "20:15")
        self.assertNotIn("flight_numbers", fields)
        self.assertEqual(result["current"]["offer"]["legs"][0]["departure"], "20:15")

    def test_other_connection_near_the_same_time_is_substituted(self) -> None:
        via = (
            _segment("JFK", "BOS", "19:45", "21:00", "B6100"),
            _segment("BOS", "LHR", "22:00", "09:30", "BA212"),
        )
        result = recheck_offer(
            _previous(OUTBOUND), allow_substitute=True, search=_Stub(_report(_offer(450.0, via)))
        )
        self.assertEqual(result["outcome"], "substituted")
        fields = {row["field"] for row in result["differences"]}
        self.assertEqual(fields, {"flight_numbers", "route", "stops", "departure", "arrival"})
        self.assertEqual(result["current"]["price"], 450.0)

    def test_other_airline_near_the_same_time_is_not_a_substitute(self) -> None:
        other = _segment("JFK", "LHR", "19:55", "07:55", "VS4")
        other = RawSegment(**{**other.__dict__, "airline": "Virgin Atlantic", "carrier": "VS"})
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(_offer(450.0, (other,)))))
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "not_among_offers")
        self.assertTrue(result["check_completed"])

    def test_same_carrier_near_the_same_time_is_a_substitute(self) -> None:
        sibling = _segment("JFK", "LHR", "19:55", "07:55", "BA999")
        result = recheck_offer(
            _previous(OUTBOUND),
            allow_substitute=True,
            search=_Stub(_report(_offer(450.0, (sibling,)))),
        )
        self.assertEqual(result["outcome"], "substituted")
        self.assertEqual(
            {row["field"] for row in result["differences"]},
            {"flight_numbers", "departure", "arrival"},
        )

    def test_same_carrier_beyond_the_window_is_not_a_substitute(self) -> None:
        late = _segment("JFK", "LHR", "22:30", "10:30", "BA999")
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(_offer(450.0, (late,)))))
        self.assertEqual(result["outcome"], "not_found")

    def test_departures_either_side_of_midnight_are_close(self) -> None:
        late = _segment("JFK", "LHR", "23:50", "11:50", "BA178")
        early = _segment("JFK", "LHR", "00:10", "12:10", "BA555")
        other = RawSegment(**{**early.__dict__, "airline": "Virgin Atlantic", "carrier": "VS"})
        near = recheck_offer(
            _previous(late),
            allow_substitute=True,
            search=_Stub(_report(_offer(400.0, (early,)))),
        )
        far = recheck_offer(
            _previous(late),
            allow_substitute=True,
            search=_Stub(_report(_offer(400.0, (other,)))),
        )
        self.assertEqual(near["outcome"], "substituted")
        self.assertEqual(far["outcome"], "not_found")

    def test_a_retimed_second_segment_is_substituted_and_named(self) -> None:
        first = _segment("JFK", "BOS", "19:30", "20:45", "BA212")
        second = _segment("BOS", "LHR", "22:00", "09:30", "BA213")
        later = _segment("BOS", "LHR", "22:40", "09:30", "BA213")
        result = recheck_offer(
            _previous(first, second),
            allow_substitute=True,
            search=_Stub(_report(_offer(500.0, (first, later)))),
        )
        self.assertEqual(result["outcome"], "substituted")
        self.assertEqual(
            result["differences"],
            [
                {
                    "journey": 0,
                    "field": "connection_departures",
                    "previous": ["22:00"],
                    "current": ["22:40"],
                }
            ],
        )

    def test_provider_empty_is_a_completed_not_found(self) -> None:
        result = recheck_offer(
            _previous(OUTBOUND), search=_Stub(_failed(SearchErrorCode.NO_RESULTS))
        )
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "provider_empty")
        self.assertTrue(result["check_completed"])
        self.assertNotIn("error", result)

    def test_cards_without_eligible_offers_are_filtered_not_empty(self) -> None:
        stub = _Stub(_report(raw=7, eligible=0))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "filtered")
        self.assertTrue(result["check_completed"])

    def test_truncated_comparison_is_disclosed(self) -> None:
        stub = _Stub(
            _report(
                _offer(300.0, (_segment("JFK", "LHR", "06:00", "18:00", "VS4"),)),
                eligible=250,
                raw=250,
            )
        )
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["reason"], "not_among_offers")
        self.assertTrue(any("1 cheapest of 250" in note for note in result["notes"]))


class BlockedTests(unittest.TestCase):
    def test_rate_limited_check_is_not_completed_and_not_gone(self) -> None:
        report = _failed(SearchErrorCode.BLOCKED, rate_limited=True)
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(report))
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rate_limited")
        self.assertTrue(result["error"]["rate_limited"])
        self.assertIsNone(result["current"])
        self.assertTrue(any("could not be completed" in note for note in result["notes"]))
        self.assertNotIn("provider_empty", json.dumps(result))

    def test_every_provider_error_but_empty_is_check_failed(self) -> None:
        for code in SearchErrorCode:
            if code == SearchErrorCode.NO_RESULTS:
                continue
            with self.subTest(code=code):
                result = recheck_offer(_previous(OUTBOUND), search=_Stub(_failed(code)))
                self.assertEqual(result["outcome"], "check_failed")
                self.assertFalse(result["check_completed"])
                self.assertEqual(result["reason"], code.value)
                self.assertEqual(result["error"]["code"], code.value)
                self.assertIsNone(result["current"])

    def test_machine_cooldown_sends_nothing_and_does_not_report_the_offer_gone(self) -> None:
        day = date.today() + timedelta(days=30)
        previous = _previous(
            _segment("JFK", "LHR", "19:30", "07:30", "BA178", day=day),
            query=dict(_query(day).to_dict()),
        )
        with tempfile.TemporaryDirectory() as state:
            with patch.dict(os.environ, {"VIAJANTE_STATE_DIR": state}):
                note_rate_limited()
                with patch("viajante.google_flights.shared_chrome_sweep_client") as client:
                    result = recheck_offer(previous)
        client.assert_not_called()
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rate_limited")
        self.assertIsNone(result["checked_at"])
        self.assertTrue(format_recheck(result).startswith("check_failed  (no search sent)"))


class CurrencyTests(unittest.TestCase):
    def test_a_currency_other_than_the_offers_own_is_refused_before_any_search(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,)), currency="GBP"))
        for named in ("GBP", "eur"):
            with self.subTest(named), self.assertRaisesRegex(ValueError, "differs"):
                recheck_offer(_previous(OUTBOUND, price=500.0), currency=named, search=stub)
        self.assertEqual(stub.calls, [])

    def test_naming_the_offers_own_currency_is_fine(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND), currency="usd", search=stub)
        self.assertEqual(result["outcome"], "same_price")

    def test_identity_without_a_currency_is_refused_not_defaulted(self) -> None:
        with self.assertRaisesRegex(ValueError, "currency is required"):
            recheck_offer(_previous(OUTBOUND, currency=None), search=_Stub(_report()))

    def test_named_currency_completes_a_hand_built_identity(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND, currency=None), currency="USD", search=stub)
        self.assertEqual(result["outcome"], "same_price")


class IdentityTests(unittest.TestCase):
    def test_absent_flight_numbers_fall_back_to_carrier_and_times_and_say_so(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        stub = _Stub(_report(_offer(500.0, (bare,))))
        result = recheck_offer(_previous(bare), allow_loose_match=True, search=stub)
        self.assertEqual(result["outcome"], "same_price")
        self.assertEqual(result["match_basis"], "carrier_times")
        self.assertTrue(result["loose_match"])
        self.assertTrue(any("Loose match" in note for note in result["notes"]))

    def test_carrier_fallback_still_needs_matching_times(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        later = _segment("JFK", "LHR", "23:50", "11:50", None)
        result = recheck_offer(
            _previous(bare),
            allow_loose_match=True,
            search=_Stub(_report(_offer(500.0, (later,)))),
        )
        self.assertEqual(result["match_basis"], "carrier_times")
        self.assertNotEqual(result["outcome"], "same_price")

    def test_flight_numbers_ignore_spacing_case_and_leading_zeros(self) -> None:
        spaced = _segment("JFK", "LHR", "19:30", "07:30", "ba 0178")
        result = recheck_offer(_previous(spaced), search=_Stub(_report(_offer(500.0, (OUTBOUND,)))))
        self.assertEqual(result["outcome"], "same_price")

    def test_a_different_segment_date_is_not_identical(self) -> None:
        next_day = _segment("JFK", "LHR", "19:30", "07:30", "BA178", day=FAR + timedelta(days=1))
        result = recheck_offer(
            _previous(OUTBOUND), search=_Stub(_report(_offer(500.0, (next_day,))))
        )
        self.assertNotEqual(result["outcome"], "same_price")

    def test_round_trip_matches_every_journey_and_never_splits_the_package_price(self) -> None:
        back = _segment("LHR", "JFK", "10:00", "13:00", "BA177", day=FAR + timedelta(days=7))
        query = RoundTrip("JFK", "LHR", FAR, FAR + timedelta(days=7), adults=1, cabin="economy")
        previous = dict(_offer(900.0, (OUTBOUND,), (back,)).to_dict("USD"))
        previous["evidence"] = {
            "source": "google_flights",
            "query": dict(query.to_dict()),
            "currency": "USD",
            "fetch_backend": "sweep",
        }
        other_return = _segment(
            "LHR", "JFK", "18:00", "21:00", "BA179", day=FAR + timedelta(days=7)
        )
        stub = _Stub(
            _report(
                _offer(880.0, (OUTBOUND,), (other_return,)),
                _offer(950.0, (OUTBOUND,), (back,)),
                query=query,
            )
        )
        result = recheck_offer(previous, search=stub)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertEqual(result["current"]["price"], 950.0)
        self.assertEqual(stub.calls[0][1]["fetch"], "sweep")
        self.assertLessEqual(stub.calls[0][1]["top"], 20)
        self.assertEqual(result["previous"]["price"], 900.0)

    def test_a_row_with_query_and_offer_is_accepted(self) -> None:
        previous = _previous(OUTBOUND)
        row = {"status": "ok", "query": previous["evidence"]["query"], "offer": previous}
        result = recheck_offer(row, search=_Stub(_report(_offer(500.0, (OUTBOUND,)))))
        self.assertEqual(result["outcome"], "same_price")

    def test_explicit_query_cannot_change_the_offer_route_or_any_journey_date(self) -> None:
        wrong_route = {**_query().to_dict(), "destination": "CDG"}
        wrong_outbound_date = dict(_query(FAR + timedelta(days=1)).to_dict())
        wrong_return_date = dict(
            RoundTrip(
                "JFK", "LHR", FAR, RETURN_DAY + timedelta(days=1), adults=1, cabin="economy"
            ).to_dict()
        )
        cases = (
            (_previous(OUTBOUND), wrong_route),
            (_previous(OUTBOUND), wrong_outbound_date),
            (_round_trip_previous(), wrong_return_date),
        )
        for previous, query in cases:
            stub = _Stub(_report())
            with self.subTest(query=query), self.assertRaisesRegex(ValueError, "query"):
                recheck_offer(previous, query=query, search=stub)
            self.assertEqual(stub.calls, [])

    def test_future_override_cannot_hide_a_past_owned_departure(self) -> None:
        past_day = date.today() - timedelta(days=1)
        past_segment = _segment("JFK", "LHR", "19:30", "07:30", "BA178", day=past_day)
        previous = _previous(past_segment, query=dict(_query(past_day).to_dict()))
        stub = _Stub(_report())
        with self.assertRaisesRegex(ValueError, "query"):
            recheck_offer(previous, query=dict(_query().to_dict()), search=stub)
        self.assertEqual(stub.calls, [])

    def test_external_offer_can_use_query_when_arrival_date_is_unknown(self) -> None:
        previous = _previous(OUTBOUND)
        previous.pop("evidence")
        row = {"query": dict(_query().to_dict()), "offer": previous}
        result = recheck_offer(
            row,
            currency="USD",
            search=_Stub(_report(_offer(500.0, (OUTBOUND,)))),
        )
        self.assertEqual(result["outcome"], "same_price")
        self.assertEqual(result["previous"]["source"], "caller_supplied")

    def test_airport_change_between_segments_does_not_break_route_binding(self) -> None:
        first = _segment("JFK", "LGA", "08:00", "09:00", "AA100")
        second = _segment("EWR", "LHR", "12:00", "19:00", "BA200")
        previous = _previous(first, second)
        result = recheck_offer(
            previous,
            search=_Stub(_report(_offer(500.0, (first, second)))),
        )
        self.assertEqual(result["outcome"], "same_price")

    def test_unsupported_input_is_refused_before_any_search(self) -> None:
        stub = _Stub(_report())
        past = _previous(OUTBOUND, query=dict(_query(date.today() - timedelta(days=1)).to_dict()))
        no_adults = {k: v for k, v in _query().to_dict().items() if k != "adults"}
        two_journeys = _previous(
            OUTBOUND,
            query=dict(
                RoundTrip(
                    "JFK", "LHR", FAR, FAR + timedelta(days=3), adults=1, cabin="economy"
                ).to_dict()
            ),
        )
        skiplagged = {**_previous(OUTBOUND), "evidence": {"source": "skiplagged", "query": {}}}
        for label, offer in {
            "past": past,
            "adults not named": _previous(OUTBOUND, query=no_adults),
            "missing journey": two_journeys,
            "other provider": skiplagged,
            "no price": {**_previous(OUTBOUND), "price": None},
            "no legs": {**_previous(OUTBOUND), "legs": []},
        }.items():
            with self.subTest(label), self.assertRaises(ValueError):
                recheck_offer(offer, search=stub)
        self.assertEqual(stub.calls, [])

    def test_fetch_follows_the_original_backend_unless_named(self) -> None:
        stub = _Stub(_report())
        recheck_offer(_previous(OUTBOUND, backend="detail"), search=stub)
        recheck_offer(_previous(OUTBOUND, backend="sweep_then_detail"), search=stub)
        recheck_offer(_previous(OUTBOUND), fetch="detail", search=stub)
        self.assertEqual([c[1]["fetch"] for c in stub.calls], ["detail", "auto", "detail"])


class StrictMatchingTests(unittest.TestCase):
    def test_fresh_missing_segment_identity_cannot_confirm_or_rule_out_original(self):
        for field in ("origin", "destination", "flight_number", "departure"):
            with self.subTest(field=field):
                fresh = replace(OUTBOUND, **{field: None})
                result = recheck_offer(
                    _previous(OUTBOUND), search=_Stub(_report(_offer(500, (fresh,))))
                )
                self.assertEqual(result["outcome"], "check_failed")
                self.assertFalse(result["check_completed"])
                self.assertEqual(result["reason"], "incomplete_offers")

    def test_fresh_missing_segments_cannot_rule_out_original(self):
        fresh = replace(_offer(500, (OUTBOUND,)), legs=())
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(fresh)))
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])

    def test_complete_exact_match_survives_other_incomplete_candidates(self):
        incomplete = _offer(450, (replace(OUTBOUND, origin=None),))
        exact = _offer(500, (OUTBOUND,))
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(incomplete, exact)))
        self.assertEqual(result["outcome"], "same_price")
        self.assertEqual(result["offers_compared"], 1)

    def test_loose_mode_still_requires_fresh_numbers_when_original_has_them(self):
        old = _previous(OUTBOUND)
        old["legs"][0]["segments"][0]["origin"] = None
        fresh = replace(OUTBOUND, flight_number=None)
        result = recheck_offer(
            old, allow_loose_match=True, search=_Stub(_report(_offer(500, (fresh,))))
        )
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])

    def test_loose_match_keeps_original_airport_identity(self):
        old = replace(OUTBOUND, flight_number=None)
        fresh = replace(old, destination=None)
        result = recheck_offer(
            _previous(old), allow_loose_match=True, search=_Stub(_report(_offer(500, (fresh,))))
        )
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])

    def test_two_identical_itineraries_are_multiple_matches_not_a_verdict(self) -> None:
        stub = _Stub(_report(_offer(510.0, (OUTBOUND,)), _offer(530.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["outcome"], "multiple_matches")
        self.assertEqual(result["reason"], "ambiguous_identity")
        self.assertTrue(result["check_completed"])
        self.assertIsNone(result["current"])
        self.assertEqual([c["price"] for c in result["candidates"]], [510.0, 530.0])
        self.assertEqual(result["candidates"][0]["journeys"][0]["departure"], "19:30")
        self.assertEqual(result["candidates"][0]["journeys"][0]["flight_numbers"], ["BA178"])
        self.assertTrue(any("2 fresh offers matched" in note for note in result["notes"]))
        self.assertNotIn("price_changed", json.dumps(result))

    def test_a_single_match_is_still_a_verdict(self) -> None:
        other = _segment("JFK", "LHR", "09:00", "21:00", "BA112")
        stub = _Stub(_report(_offer(510.0, (OUTBOUND,)), _offer(400.0, (other,))))
        self.assertEqual(
            recheck_offer(_previous(OUTBOUND), search=stub)["outcome"], "price_changed"
        )

    def test_an_offer_without_flight_numbers_is_incomplete_identity_and_sends_nothing(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        stub = _Stub(_report(_offer(500.0, (bare,))))
        result = recheck_offer(_previous(bare), search=stub)
        self.assertEqual(stub.calls, [])
        self.assertEqual(result["outcome"], "incomplete_identity")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["missing"], ["journey 0 segment 0: flight number"])
        self.assertIsNone(result["match_basis"])
        self.assertIsNone(result["current"])
        self.assertTrue(any("allow_loose_match" in note for note in result["notes"]))

    def test_every_missing_identity_field_is_named(self) -> None:
        old = _previous(OUTBOUND)
        segment = old["legs"][0]["segments"][0]
        for field in ("origin", "destination", "departure"):
            gutted = {**old, "legs": [{**old["legs"][0], "segments": [{**segment, field: None}]}]}
            with self.subTest(field):
                result = recheck_offer(gutted, search=_Stub(_report()))
                self.assertEqual(result["outcome"], "incomplete_identity")
                self.assertEqual(len(result["missing"]), 1)

    def test_a_leg_without_segments_is_incomplete_identity(self) -> None:
        old = _previous(OUTBOUND)
        flat = {**old, "legs": [{k: v for k, v in old["legs"][0].items() if k != "segments"}]}
        stub = _Stub(_report())
        result = recheck_offer(flat, search=stub)
        self.assertEqual(result["outcome"], "incomplete_identity")
        self.assertEqual(stub.calls, [])

    def test_loose_match_is_opt_in_and_labelled(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        stub = _Stub(_report(_offer(520.0, (bare,))))
        result = recheck_offer(_previous(bare), allow_loose_match=True, search=stub)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertEqual(result["match_basis"], "carrier_times")
        self.assertTrue(result["loose_match"])
        self.assertEqual(len(stub.calls), 1)

    def test_loose_match_cannot_rescue_an_offer_with_no_carrier_either(self) -> None:
        old = _previous(_segment("JFK", "LHR", "19:30", "07:30", None))
        segment = {**old["legs"][0]["segments"][0], "carrier": None, "airline": None}
        old = {**old, "airline": None, "legs": [{**old["legs"][0], "segments": [segment]}]}
        stub = _Stub(_report())
        result = recheck_offer(old, allow_loose_match=True, search=stub)
        self.assertEqual(result["outcome"], "incomplete_identity")
        self.assertIn("carrier or airline", result["missing"][0])
        self.assertEqual(stub.calls, [])

    def test_a_complete_identity_is_not_loose_even_when_loose_is_allowed(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        result = recheck_offer(_previous(OUTBOUND), allow_loose_match=True, search=stub)
        self.assertEqual(result["match_basis"], "flight_numbers")
        self.assertFalse(result["loose_match"])

    def test_a_different_flight_at_the_same_time_is_not_found_with_a_closest_candidate(
        self,
    ) -> None:
        sibling = _segment("JFK", "LHR", "19:30", "07:30", "BA999")
        stub = _Stub(_report(_offer(450.0, (sibling,))))
        result = recheck_offer(_previous(OUTBOUND), search=stub)
        self.assertEqual(result["outcome"], "not_found")
        self.assertEqual(result["reason"], "not_among_offers")
        self.assertTrue(result["check_completed"])
        self.assertIsNone(result["current"])
        self.assertNotIn("differences", result)
        closest = result["closest_candidate"]
        self.assertEqual(closest["price"], 450.0)
        self.assertEqual(closest["journeys"][0]["flight_numbers"], ["BA999"])
        self.assertEqual(closest["differences"][0]["field"], "flight_numbers")
        self.assertTrue(any("for information only" in note for note in result["notes"]))

    def test_substitution_is_reported_only_when_allowed(self) -> None:
        sibling = _segment("JFK", "LHR", "19:30", "07:30", "BA999")
        stub = _Stub(_report(_offer(450.0, (sibling,))))
        result = recheck_offer(_previous(OUTBOUND), allow_substitute=True, search=stub)
        self.assertEqual(result["outcome"], "substituted")
        self.assertEqual(result["current"]["price"], 450.0)
        self.assertNotIn("closest_candidate", result)

    def test_nothing_close_has_no_closest_candidate(self) -> None:
        far = _segment("JFK", "LHR", "06:00", "18:00", "BA001")
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(_offer(450.0, (far,)))))
        self.assertEqual(result["outcome"], "not_found")
        self.assertNotIn("closest_candidate", result)


class ReplayedFilterTests(unittest.TestCase):
    def _query(self, **extra: object) -> dict:
        return {**_query().to_dict(), **extra}

    def _check(self, price: float, segment: RawSegment = OUTBOUND, **extra: object) -> tuple:
        stub = _Stub(_report(_offer(price, (segment,))))
        previous = _previous(OUTBOUND, query=self._query(**extra))
        return recheck_offer(previous, search=stub), stub

    def test_a_price_cap_breach_is_reported_beside_the_verdict(self) -> None:
        result, stub = self._check(560.0, price_cap=520)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertEqual(result["filter_violations"], ["price_cap"])
        self.assertNotIn("price_cap", result["filters_replayed"])
        self.assertIn("price_cap", result["filters_checked"])
        self.assertIsNone(stub.calls[0][0][0][0].price_cap)

    def test_a_price_within_the_cap_has_no_violation(self) -> None:
        result, _ = self._check(510.0, price_cap=520)
        self.assertEqual(result["filter_violations"], [])

    def test_airline_filters_are_replayed_and_checked(self) -> None:
        wrong, stub = self._check(500.0, airlines=["VS"])
        self.assertEqual(wrong["filter_violations"], ["airlines"])
        self.assertEqual(stub.calls[0][0][0][0].airlines, ("VS",))
        barred, _ = self._check(500.0, exclude_airlines="BA")
        self.assertEqual(barred["filter_violations"], ["exclude_airlines"])
        fine, _ = self._check(500.0, airlines="BA")
        self.assertEqual(fine["filter_violations"], [])

    def test_the_search_carries_cabin_stops_bags_and_alliances(self) -> None:
        _, stub = self._check(
            500.0, cabin="business", max_stops=0, bags=1, carry_on=1, alliances="oneworld"
        )
        trip = stub.calls[0][0][0][0]
        self.assertEqual(
            (trip.cabin, trip.max_stops, trip.bags, trip.carry_on), ("business", 0, 1, 1)
        )
        self.assertEqual(len(trip.alliances), 1)

    def test_more_stops_than_the_replayed_limit_is_a_violation(self) -> None:
        first = _segment("JFK", "BOS", "19:30", "20:45", "BA212")
        second = _segment("BOS", "LHR", "22:00", "09:30", "BA213")
        stub = _Stub(_report(_offer(500.0, (first, second))))
        previous = _previous(first, second, query=self._query(max_stops=0))
        result = recheck_offer(previous, search=stub)
        self.assertEqual(result["filter_violations"], ["max_stops"])

    def test_substituted_and_candidates_carry_violations_too(self) -> None:
        sibling = _segment("JFK", "LHR", "19:30", "07:30", "BA999")
        stub = _Stub(_report(_offer(600.0, (sibling,))))
        previous = _previous(OUTBOUND, query=self._query(price_cap=520))
        result = recheck_offer(previous, allow_substitute=True, search=stub)
        self.assertEqual(result["filter_violations"], ["price_cap"])
        stub = _Stub(_report(_offer(600.0, (OUTBOUND,)), _offer(500.0, (OUTBOUND,))))
        many = recheck_offer(previous, search=stub)
        self.assertEqual([c["filter_violations"] for c in many["candidates"]], [[], ["price_cap"]])

    def test_a_bad_price_cap_is_refused(self) -> None:
        for bad in ("cheap", 0, -5):
            with self.subTest(bad), self.assertRaisesRegex(ValueError, "price_cap"):
                recheck_offer(
                    _previous(OUTBOUND, query=self._query(price_cap=bad)),
                    search=_Stub(_report()),
                )


class RoundTwoBugTests(unittest.TestCase):
    def _mixed(self) -> tuple:
        first = _segment("JFK", "BOS", "19:30", "20:45", "BA212")
        second = RawSegment(
            **{
                **_segment("BOS", "LHR", "22:00", "09:30", "AA6").__dict__,
                "carrier": "AA",
                "airline": "American",
            }
        )
        return first, second

    def _violations(self, **extra: object) -> list[str]:
        first, second = self._mixed()
        previous = _previous(first, second, query={**_query().to_dict(), **extra})
        stub = _Stub(_report(_offer(500.0, (first, second))))
        result = recheck_offer(previous, search=stub)
        self.assertEqual(result["outcome"], "same_price")
        return result["filter_violations"]  # type: ignore[return-value]

    def test_an_allow_list_is_satisfied_by_any_carrier_like_the_search(self) -> None:
        self.assertEqual(self._violations(airlines=["BA"]), [])
        self.assertEqual(self._violations(airlines=["AA"]), [])
        self.assertEqual(self._violations(airlines=["VS"]), ["airlines"])

    def test_an_exclude_list_is_hit_by_any_carrier(self) -> None:
        self.assertEqual(self._violations(exclude_airlines=["AA"]), ["exclude_airlines"])
        self.assertEqual(self._violations(exclude_airlines=["VS"]), [])

    def _leg_without_segments(self, stops: str | None, stops_count: object = None) -> dict:
        old = _previous(OUTBOUND)
        leg = {**old["legs"][0], "segments": [], "stops": stops}
        return {**old, "legs": [leg], "stops_count": stops_count}

    def test_loose_match_refuses_a_connecting_leg_without_segments(self) -> None:
        stub = _Stub(_report(_offer(650.0, (OUTBOUND,))))
        for stops, count in (("1 stop", None), (None, None), (None, 1), ("1 stop", 0)):
            with self.subTest(stops=stops, count=count):
                result = recheck_offer(
                    self._leg_without_segments(stops, count), allow_loose_match=True, search=stub
                )
                self.assertEqual(result["outcome"], "incomplete_identity")
                self.assertTrue(any("segments" in m for m in result["missing"]))
        self.assertEqual(stub.calls, [])

    def test_loose_match_accepts_a_leg_known_to_be_nonstop(self) -> None:
        for stops, count in (("Nonstop", None), (None, 0)):
            with self.subTest(stops=stops, count=count):
                stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
                result = recheck_offer(
                    self._leg_without_segments(stops, count), allow_loose_match=True, search=stub
                )
                self.assertEqual(result["outcome"], "same_price")
                self.assertEqual(len(stub.calls), 1)

    def test_a_close_alternative_with_nothing_visibly_different_is_not_reported(self) -> None:
        first = _segment("JFK", "BOS", "19:30", "20:45", "BA212")
        second = _segment("BOS", "LHR", "22:00", "09:30", "BA213")
        next_day = _segment("BOS", "LHR", "22:00", "09:30", "BA213", day=FAR + timedelta(days=1))
        stub = _Stub(_report(_offer(450.0, (first, next_day))))
        for options in ({}, {"allow_substitute": True}):
            with self.subTest(options):
                result = recheck_offer(_previous(first, second), search=stub, **options)
                self.assertEqual(result["outcome"], "not_found")
                self.assertNotIn("closest_candidate", result)

    def test_a_different_first_day_is_a_named_difference(self) -> None:
        next_day = _segment("JFK", "LHR", "19:30", "07:30", "BA178", day=FAR + timedelta(days=1))
        result = recheck_offer(
            _previous(OUTBOUND),
            allow_substitute=True,
            search=_Stub(_report(_offer(450.0, (next_day,)))),
        )
        self.assertEqual(result["outcome"], "substituted")
        self.assertEqual([r["field"] for r in result["differences"]], ["departure_date"])

    def test_flight_numbers_are_used_in_loose_mode_when_the_offer_has_them(self) -> None:
        old = _previous(OUTBOUND)
        segment = {**old["legs"][0]["segments"][0], "origin": None}
        old = {**old, "legs": [{**old["legs"][0], "segments": [segment]}]}
        other = _segment("JFK", "LHR", "19:30", "07:30", "BA999")
        wrong = recheck_offer(
            old, allow_loose_match=True, search=_Stub(_report(_offer(500.0, (other,))))
        )
        self.assertEqual(wrong["outcome"], "not_found")
        right = recheck_offer(
            old, allow_loose_match=True, search=_Stub(_report(_offer(500.0, (OUTBOUND,))))
        )
        self.assertEqual(right["outcome"], "same_price")
        self.assertTrue(right["loose_match"])

    def test_airports_are_compared_when_matching(self) -> None:
        tokyo = _segment("JFK", "NRT", "19:30", "22:30", "BA178")
        result = recheck_offer(
            _previous(OUTBOUND),
            allow_substitute=True,
            search=_Stub(_report(_offer(500.0, (tokyo,)))),
        )
        self.assertEqual(result["outcome"], "not_found")
        self.assertNotIn("closest_candidate", result)


class IncompleteJourneyTests(unittest.TestCase):
    """A fresh round trip without its return must never read as a finished 'gone'."""

    def _outbound_only(self, answer: object) -> SearchReport:
        offer = _offer(900.0, (OUTBOUND,))
        attached = _attach_missing_legs(RT_QUERY, (offer,), _ReturnShop(answer))
        self.assertEqual(len(attached[0].legs), 1)
        return _report(*attached, query=RT_QUERY)

    def _assert_failed(self, result: dict) -> None:
        self.assertEqual(result["outcome"], "check_failed")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "incomplete_offers")
        self.assertEqual(result["error"]["code"], "incomplete_offers")
        self.assertIsNone(result["current"])
        self.assertTrue(any("could not be completed" in note for note in result["notes"]))

    def test_a_failed_follow_up_shop_is_a_failed_check(self) -> None:
        report = self._outbound_only(RuntimeError("follow-up shop failed"))
        self._assert_failed(recheck_offer(_round_trip_previous(), search=_Stub(report)))

    def test_tied_returns_at_the_package_price_are_a_failed_check(self) -> None:
        tied = [_return_card("$900"), _return_card("$900")]
        report = self._outbound_only(tied)
        self._assert_failed(recheck_offer(_round_trip_previous(), search=_Stub(report)))

    def test_an_identical_match_elsewhere_still_wins(self) -> None:
        report = _report(
            _offer(900.0, (OUTBOUND,)),
            _offer(910.0, (OUTBOUND,), (BACK,)),
            query=RT_QUERY,
        )
        result = recheck_offer(_round_trip_previous(), search=_Stub(report))
        self.assertEqual(result["outcome"], "price_changed")
        self.assertEqual(result["current"]["price"], 910.0)

    def test_a_close_alternative_does_not_hide_an_incomplete_offer(self) -> None:
        sibling = _segment("LHR", "JFK", "10:20", "13:20", "BA999", day=RETURN_DAY)
        report = _report(
            _offer(900.0, (OUTBOUND,)),
            _offer(880.0, (OUTBOUND,), (sibling,)),
            query=RT_QUERY,
        )
        result = recheck_offer(_round_trip_previous(), search=_Stub(report))
        self.assertEqual(result["outcome"], "check_failed")

    def test_round_trips_carry_the_unique_return_caveat(self) -> None:
        report = _report(_offer(900.0, (OUTBOUND,), (BACK,)), query=RT_QUERY)
        result = recheck_offer(_round_trip_previous(), search=_Stub(report))
        self.assertEqual(result["outcome"], "same_price")
        self.assertTrue(any("unique at its cheapest" in note for note in result["notes"]))
        one_way = recheck_offer(
            _previous(OUTBOUND), search=_Stub(_report(_offer(500.0, (OUTBOUND,))))
        )
        self.assertFalse(any("unique at its cheapest" in note for note in one_way["notes"]))


class MalformedInputTests(unittest.TestCase):
    def _refused(self, offer: dict) -> None:
        stub = _Stub(_report())
        with self.assertRaises(ValueError):
            recheck_offer(offer, search=stub)
        self.assertEqual(stub.calls, [])

    def test_multi_city_leg_without_an_origin(self) -> None:
        query = {
            "trip": "multi",
            "adults": 1,
            "cabin": "economy",
            "max_stops": 1,
            "legs": [
                {"destination": "LHR", "departure_date": FAR.isoformat()},
                {"origin": "LHR", "destination": "JFK", "departure_date": RETURN_DAY.isoformat()},
            ],
        }
        self._refused(_previous(OUTBOUND, query=query))

    def test_other_malformed_shapes_are_value_errors_not_tracebacks(self) -> None:
        good = _previous(OUTBOUND)
        legs_missing = {k: v for k, v in good.items() if k != "legs"}
        for label, offer in {
            "no legs key": legs_missing,
            "legs is a number": {**good, "legs": 5},
            "segments is a number": {**good, "legs": [{"segments": 3}]},
            "query is a list": {**good, "evidence": {**good["evidence"], "query": []}},
            "adults is text": _previous(OUTBOUND, query={**_query().to_dict(), "adults": "two"}),
            "max_stops out of range": _previous(
                OUTBOUND, query={**_query().to_dict(), "max_stops": 7}
            ),
            "origin is a number": _previous(OUTBOUND, query={**_query().to_dict(), "origin": 5}),
        }.items():
            with self.subTest(label):
                self._refused(offer)

    def test_errors_name_the_field_and_never_leak_python(self) -> None:
        good = _previous(OUTBOUND)
        leg = {"destination": "LHR", "departure_date": FAR.isoformat()}
        multi = {
            "trip": "multi",
            "adults": 1,
            "cabin": "economy",
            "max_stops": 1,
            "legs": [leg, {**leg, "origin": "LHR"}],
        }
        cases = {
            "query.legs[0] is missing origin": _previous(OUTBOUND, query=multi),
            "query.airlines must be a list of IATA codes or a string": _previous(
                OUTBOUND, query={**_query().to_dict(), "airlines": 5}
            ),
            "query.alliances must be a list of alliance names": _previous(
                OUTBOUND, query={**_query().to_dict(), "alliances": 5}
            ),
            "query.exclude_airlines must be a list": _previous(
                OUTBOUND, query={**_query().to_dict(), "exclude_airlines": ["BA", 3]}
            ),
            "query.adults must be a whole number": _previous(
                OUTBOUND, query={**_query().to_dict(), "adults": "two"}
            ),
            "query is missing origin": _previous(
                OUTBOUND, query={**_query().to_dict(), "origin": None}
            ),
            "segments must be a list": {**good, "legs": [{"segments": 3}]},
        }
        for message, offer in cases.items():
            with self.subTest(message), self.assertRaises(ValueError) as caught:
                recheck_offer(offer, search=_Stub(_report()))
            self.assertIn(message, str(caught.exception))
            for leak in ("Error:", "NoneType", "object of type", "strip", "not iterable"):
                self.assertNotIn(leak, str(caught.exception))

    def test_segments_type_is_checked_before_truthiness(self) -> None:
        good = _previous(OUTBOUND)
        for bad in ({}, 0, "x", {"a": 1}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "must be a list"):
                recheck_offer({**good, "legs": [{"segments": bad}]}, search=_Stub(_report()))

    def test_wrong_typed_wrappers_name_their_own_field(self) -> None:
        good = _previous(OUTBOUND)
        for message, offer, kwargs in (
            ("currency must be an ISO 4217", good, {"currency": 5}),
            ("currency must be an ISO 4217", {**good, "currency": 5}, {}),
            ("offer.offer must be an object", {"offer": 5}, {}),
            ("offer.evidence must be an object", {**good, "evidence": 5}, {}),
        ):
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                recheck_offer(offer, search=_Stub(_report()), **kwargs)

    def test_a_plain_string_airline_list_is_parsed_not_split_into_letters(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        query = {**_query().to_dict(), "airlines": "BA,aa", "alliances": "star-alliance"}
        recheck_offer(_previous(OUTBOUND, query=query), search=stub)
        trip = stub.calls[0][0][0][0]
        self.assertEqual(trip.airlines, ("BA", "AA"))
        self.assertEqual(len(trip.alliances), 1)

    def test_a_departed_journey_is_refused_by_its_clock_in_the_origin_timezone(self) -> None:
        # JFK 19:30 on FAR is 00:30 UTC the next day (UTC-5).
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        before = datetime(2099, 1, 15, 20, 0, tzinfo=timezone.utc)
        after = datetime(2099, 1, 16, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(
            recheck_offer(_previous(OUTBOUND), search=stub, now=before)["outcome"], "same_price"
        )
        with self.assertRaisesRegex(ValueError, "in the past"):
            recheck_offer(_previous(OUTBOUND), search=stub, now=after)
        self.assertEqual(len(stub.calls), 1)

    def test_country_not_reapplied_is_noted(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        bare = recheck_offer(_previous(OUTBOUND), search=stub)
        named = recheck_offer(_previous(OUTBOUND), country="US", search=stub)
        self.assertTrue(any("country (gl)" in note for note in bare["notes"]))
        self.assertFalse(any("country (gl)" in note for note in named["notes"]))
        self.assertEqual(stub.calls[1][1]["country"], "US")


class EnvelopeTests(unittest.TestCase):
    """Every outcome leaves the MCP handler with a valid envelope, and the ledger rule holds."""

    def setUp(self) -> None:
        evidence.clear()
        self.addCleanup(evidence.clear)

    def _run(self, report: SearchReport, previous: dict | None = None, **options: bool) -> dict:
        with patch("viajante.recheck.search_flights", _Stub(report)):
            return mcp_handlers.recheck_offer_tool(previous or _previous(OUTBOUND), **options)

    def _assert_envelope(
        self, result: dict, status: str, completeness: str, empty: str | None, code: str | None
    ) -> None:
        self.assertEqual(
            (
                result["status"],
                result["completeness"],
                result["empty_reason"],
                result["error_code"],
            ),
            (status, completeness, empty, code),
        )
        self.assertEqual(
            result["empty_note"] is None, empty is None, "empty_note follows empty_reason"
        )
        self.assertEqual(result["observed_at_basis"] == "fetch", result["observed_at"] is not None)

    def _assert_observed(self, result: dict) -> None:
        self.assertEqual(result["observed_at"], result["checked_at"])
        self.assertEqual(result["observed_at_basis"], "fetch")

    def test_a_found_itinerary_is_ok_and_complete(self) -> None:
        same = self._run(_report(_offer(500.0, (OUTBOUND,))))
        changed = self._run(_report(_offer(540.0, (OUTBOUND,))))
        sibling = _segment("JFK", "LHR", "19:30", "07:30", "BA999")
        substituted = self._run(_report(_offer(600.0, (sibling,))), allow_substitute=True)
        many = self._run(_report(_offer(510.0, (OUTBOUND,)), _offer(530.0, (OUTBOUND,))))
        for result, outcome in (
            (same, "same_price"),
            (changed, "price_changed"),
            (substituted, "substituted"),
            (many, "multiple_matches"),
        ):
            with self.subTest(outcome):
                self.assertEqual(result["outcome"], outcome)
                self._assert_envelope(result, "ok", "complete", None, None)
                self._assert_observed(result)

    def test_not_found_maps_by_what_the_provider_answered(self) -> None:
        empty = self._run(_failed(SearchErrorCode.NO_RESULTS))
        self.assertEqual(empty["reason"], "provider_empty")
        self._assert_envelope(empty, "no_results", "complete", "provider_empty", None)
        self._assert_observed(empty)
        filtered = self._run(_report(raw=4, eligible=0))
        self.assertEqual(filtered["reason"], "filtered")
        self._assert_envelope(filtered, "no_results", "complete", "filtered_out", None)
        elsewhere = _segment("JFK", "LHR", "09:00", "21:00", "BA112")
        among = self._run(_report(_offer(700.0, (elsewhere,))))
        self.assertEqual(among["reason"], "not_among_offers")
        self._assert_envelope(among, "ok", "complete", None, None)
        truncated = self._run(_report(_offer(700.0, (elsewhere,)), raw=250, eligible=250))
        self.assertEqual(truncated["reason"], "not_among_offers")
        self._assert_envelope(truncated, "ok", "partial", None, None)

    def test_a_failed_check_is_not_loaded_with_its_failure_status(self) -> None:
        blocked = self._run(_failed(SearchErrorCode.BLOCKED))
        self._assert_envelope(blocked, "blocked", "blocked", "not_loaded", "blocked")
        self._assert_observed(blocked)
        for code in (SearchErrorCode.MARKUP_DRIFT, SearchErrorCode.FETCH_FAILED):
            with self.subTest(code):
                failed = self._run(_failed(code))
                self._assert_envelope(failed, "failed", "blocked", "not_loaded", code.value)

    def _assert_retry_fields_match_the_error(self, result: dict) -> None:
        self.assertIsNotNone(result["retry_after"])
        self.assertGreater(result["retry_after_seconds"], 0)
        self.assertEqual(result["retry_after"], result["error"]["retry_after"])
        self.assertEqual(result["retry_after_seconds"], result["error"]["retry_after_seconds"])

    def test_a_rate_limit_repeats_the_errors_own_retry_fields(self) -> None:
        error = SearchError(
            code=SearchErrorCode.BLOCKED,
            message="Google is rate-limiting this machine",
            rate_limited=True,
            retry_until=time.time() + 90.4,
        )
        report = SearchReport(
            searched_at=CHECKED,
            queries=(QueryFailure(query=_query(), error=error),),
            currency="USD",
            fetch_backend="sweep",
        )
        limited = self._run(report)
        self._assert_envelope(limited, "rate_limited", "blocked", "not_loaded", "blocked")
        self._assert_retry_fields_match_the_error(limited)
        self._assert_observed(limited)

    def test_a_rate_limit_carries_the_cooldown_and_observed_only_when_sent(self) -> None:
        day = date.today() + timedelta(days=30)
        previous = _previous(
            _segment("JFK", "LHR", "19:30", "07:30", "BA178", day=day),
            query=dict(_query(day).to_dict()),
        )
        with (
            tempfile.TemporaryDirectory() as state,
            patch.dict(os.environ, {"VIAJANTE_STATE_DIR": state}),
        ):
            note_rate_limited()
            with patch("viajante.google_flights.shared_chrome_sweep_client") as client:
                refused = mcp_handlers.recheck_offer_tool(previous)
        client.assert_not_called()
        self._assert_envelope(refused, "rate_limited", "blocked", "not_loaded", "blocked")
        self._assert_retry_fields_match_the_error(refused)
        self.assertIsNone(refused["checked_at"])
        self.assertIsNone(refused["observed_at"])
        self.assertIsNone(refused["observed_at_basis"])

    def test_a_proxied_rate_limit_without_a_cooldown_has_no_retry_fields(self) -> None:
        limited = self._run(_failed(SearchErrorCode.BLOCKED, rate_limited=True))
        self.assertEqual(limited["status"], "rate_limited")
        self.assertIsNone(limited["retry_after"])
        self.assertIsNone(limited["retry_after_seconds"])
        self.assertNotIn("retry_after", limited["error"])

    def test_incomplete_offers_is_failed_and_partial(self) -> None:
        offer = _offer(900.0, (OUTBOUND,))
        attached = _attach_missing_legs(RT_QUERY, (offer,), _ReturnShop(RuntimeError("down")))
        report = _report(*attached, query=RT_QUERY)
        result = self._run(report, _round_trip_previous())
        self.assertEqual(result["outcome"], "check_failed")
        self.assertEqual(result["reason"], "incomplete_offers")
        self._assert_envelope(result, "failed", "partial", "not_loaded", "incomplete_offers")

    def test_incomplete_identity_is_failed_blocked_with_nothing_observed(self) -> None:
        bare = _previous(OUTBOUND)
        bare["legs"][0]["segments"][0]["flight_number"] = None
        result = self._run(_report(), bare)
        self.assertEqual(result["outcome"], "incomplete_identity")
        self._assert_envelope(result, "failed", "blocked", None, "incomplete_identity")
        self.assertIsNone(result["observed_at"])
        self.assertTrue(result["missing"])
        self.assertTrue(any("allow_loose_match" in n for n in result["notes"]))

    def test_an_answered_not_found_is_recorded_without_caller_values(self) -> None:
        elsewhere = _segment("JFK", "LHR", "09:00", "21:00", "BA112")
        cases = {
            "provider_empty": _failed(SearchErrorCode.NO_RESULTS),
            "filtered": _report(raw=4, eligible=0),
            "not_among_offers": _report(_offer(700.0, (elsewhere,))),
        }
        for reason, report in cases.items():
            with self.subTest(reason):
                evidence.clear()
                evidence.record({"offers": [_previous(OUTBOUND, evidence_id="gf_other")]})
                before = evidence.verify_answer("x")["searches"]
                typed = _previous(OUTBOUND, price=480.0, evidence_id=None)
                result = self._run(report, typed)
                self.assertEqual(result["reason"], reason)
                after = evidence.verify_answer("It was USD 480 at BA178")
                self.assertEqual(after["searches"], before + 1)
                self.assertIn("USD 480", [row["text"] for row in after["unowned"]])

    def test_a_check_that_did_not_complete_is_not_recorded(self) -> None:
        evidence.record({"offers": [_previous(OUTBOUND, evidence_id="gf_other")]})
        before = evidence.verify_answer("x")["searches"]
        self._run(_failed(SearchErrorCode.BLOCKED))
        self.assertEqual(evidence.verify_answer("x")["searches"], before)


class McpToolTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence.clear()
        self.addCleanup(evidence.clear)

    def test_every_call_is_a_fresh_search_and_never_replayed(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,))))
        with patch("viajante.recheck.search_flights", stub):
            first = mcp_handlers.recheck_offer_tool(_previous(OUTBOUND))
            second = mcp_handlers.recheck_offer_tool(_previous(OUTBOUND))
        self.assertEqual(len(stub.calls), 2)
        self.assertNotIn("cached", first)
        self.assertNotIn("cached", second)

    def _recheck(self, previous: dict, current: float = 540.0, **options: bool) -> dict:
        stub = _Stub(_report(_offer(current, (OUTBOUND,))))
        with patch("viajante.recheck.search_flights", stub):
            return mcp_handlers.recheck_offer_tool(previous, **options)

    def _unowned(self) -> list[str]:
        verdict = evidence.verify_answer("It is now USD 540 and was USD 500.")
        return [row["text"] for row in verdict["unowned"]]

    def test_results_without_a_provider_answer_record_nothing(self) -> None:
        evidence.record({"offers": [_previous(OUTBOUND, evidence_id="gf_other")]})
        searches = evidence.verify_answer("x")["searches"]
        elsewhere = _segment("JFK", "NRT", "19:30", "22:30", "BA999")
        unnumbered = _previous(elsewhere, evidence_id=None)
        unnumbered["legs"][0]["segments"][0]["flight_number"] = None
        blocked = _Stub(_failed(SearchErrorCode.BLOCKED))
        with patch("viajante.recheck.search_flights", blocked):
            failed = mcp_handlers.recheck_offer_tool(_previous(elsewhere, evidence_id=None))
        incomplete = self._recheck(unnumbered)
        self.assertEqual(failed["outcome"], "check_failed")
        self.assertEqual(incomplete["outcome"], "incomplete_identity")
        verdict = evidence.verify_answer("JFK NRT on 2099-02-01")
        self.assertEqual(verdict["searches"], searches)
        self.assertIn("NRT", [row["text"] for row in verdict["unowned"]])

    def test_an_offer_this_process_returned_has_its_previous_price_owned(self) -> None:
        previous = _previous(OUTBOUND)
        evidence.record({"offers": [previous]})
        result = self._recheck(previous)
        self.assertEqual(result["previous"]["source"], "search_evidence")
        self.assertEqual(self._unowned(), [])

    def test_an_invented_evidence_id_is_caller_supplied_and_not_owned(self) -> None:
        result = self._recheck(_previous(OUTBOUND, evidence_id="gf_invented"))
        self.assertEqual(result["previous"]["source"], "caller_supplied")
        self.assertEqual(self._unowned(), ["USD 500"])

    def test_a_real_id_with_an_edited_amount_or_currency_is_caller_supplied(self) -> None:
        evidence.record({"offers": [_previous(OUTBOUND)]})
        edited = self._recheck(_previous(OUTBOUND, price=480.0))
        self.assertEqual(edited["previous"]["source"], "caller_supplied")
        evidence.clear()
        evidence.record({"offers": [_previous(OUTBOUND, currency="GBP")]})
        other = self._recheck(_previous(OUTBOUND))
        self.assertEqual(other["previous"]["source"], "caller_supplied")

    def test_a_real_id_on_another_itinerary_is_caller_supplied_and_not_owned(self) -> None:
        evidence.record({"offers": [_previous(OUTBOUND)]})
        elsewhere = _segment("JFK", "NRT", "19:30", "22:30", "BA999")
        result = self._recheck(_previous(elsewhere, price=700.0), current=500.0)
        self.assertEqual(result["previous"]["source"], "caller_supplied")
        flagged = evidence.verify_answer("The previous quote was USD 700")["unowned"]
        self.assertIn("USD 700", [row["text"] for row in flagged])

    def test_another_offers_id_at_the_same_price_does_not_own_this_itinerary(self) -> None:
        other = _segment("JFK", "LHR", "09:00", "21:00", "BA112")
        evidence.record({"offers": [_previous(other, evidence_id="gf_other")]})
        result = self._recheck(_previous(OUTBOUND, evidence_id="gf_other"))
        self.assertEqual(result["previous"]["source"], "caller_supplied")

    def _via_boston(self) -> dict:
        first = _segment("JFK", "BOS", "19:30", "20:45", "BA212")
        second = _segment("BOS", "LHR", "22:00", "09:30", "BA213")
        return _previous(first, second, evidence_id=None)

    def test_caller_supplied_values_in_differences_are_returned_but_not_recorded(self) -> None:
        result = self._recheck(self._via_boston(), current=500.0, allow_substitute=True)
        self.assertEqual(result["outcome"], "substituted")
        route = next(r for r in result["differences"] if r["field"] == "route")
        self.assertEqual(route["previous"], ["JFK", "BOS", "LHR"])
        flagged = [row["text"] for row in evidence.verify_answer("JFK BOS LHR")["unowned"]]
        self.assertIn("BOS", flagged)
        self.assertNotIn("LHR", flagged)

    def test_caller_values_in_a_closest_candidate_are_not_recorded_either(self) -> None:
        result = self._recheck(self._via_boston(), current=500.0)
        self.assertEqual(result["outcome"], "not_found")
        route = next(r for r in result["closest_candidate"]["differences"] if r["field"] == "route")
        self.assertEqual(route["previous"], ["JFK", "BOS", "LHR"])
        flagged = [row["text"] for row in evidence.verify_answer("JFK BOS LHR")["unowned"]]
        self.assertIn("BOS", flagged)
        self.assertNotIn("LHR", flagged)

    def test_previous_leg_times_come_from_the_ledger_offer_not_the_caller(self) -> None:
        recorded = _previous(OUTBOUND)
        evidence.record({"offers": [recorded]})
        edited = {**recorded, "legs": [{**recorded["legs"][0], "arrival": "23:59"}]}
        retimed = _segment("JFK", "LHR", "20:15", "08:15", "BA178")
        stub = _Stub(_report(_offer(500.0, (retimed,))))
        with patch("viajante.recheck.search_flights", stub):
            result = mcp_handlers.recheck_offer_tool(edited, allow_substitute=True)
        self.assertEqual(result["previous"]["source"], "search_evidence")
        arrival = next(r for r in result["differences"] if r["field"] == "arrival")
        self.assertEqual(arrival["previous"], "07:30")

    def test_owned_previous_keeps_its_differences_in_the_ledger(self) -> None:
        previous = _previous(OUTBOUND)
        evidence.record({"offers": [previous]})
        retimed = _segment("JFK", "LHR", "20:15", "08:15", "BA178")
        stub = _Stub(_report(_offer(500.0, (retimed,))))
        with patch("viajante.recheck.search_flights", stub):
            result = mcp_handlers.recheck_offer_tool(previous)
        self.assertEqual(result["previous"]["source"], "search_evidence")
        self.assertEqual(
            evidence.verify_answer("It left at 19:30 on 2099-01-15 for JFK LHR")["unowned"], []
        )

    def test_a_hand_typed_previous_price_is_not_recorded_as_owned_evidence(self) -> None:
        result = self._recheck(_previous(OUTBOUND, evidence_id=None))
        self.assertEqual(result["previous"]["source"], "caller_supplied")
        self.assertEqual(result["previous"]["price"], 500.0)
        self.assertEqual(self._unowned(), ["USD 500"])

    def test_the_cli_has_no_ledger_so_nothing_there_claims_search_evidence(self) -> None:
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report()))
        self.assertEqual(result["previous"]["source"], "caller_supplied")

    def test_the_one_search_lock_applies(self) -> None:
        stub = _Stub(_report())
        with patch("viajante.recheck.search_flights", stub):
            self.assertTrue(mcp_handlers._SEARCH_LOCK.acquire(blocking=False))
            try:
                with self.assertRaisesRegex(ValueError, "already running"):
                    mcp_handlers.recheck_offer_tool(_previous(OUTBOUND))
            finally:
                mcp_handlers._SEARCH_LOCK.release()
        self.assertEqual(stub.calls, [])


class CliTests(unittest.TestCase):
    def _run(self, report: SearchReport, *extra: str) -> tuple[int, str, str, dict | None]:
        with tempfile.TemporaryDirectory() as tmp:
            offer = Path(tmp) / "offer.json"
            offer.write_text(json.dumps(_previous(OUTBOUND)), encoding="utf-8")
            saved = Path(tmp) / "out.json"
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("viajante.recheck.search_flights", _Stub(report)),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                code = main(["recheck-offer", "--offer", str(offer), "--save", str(saved), *extra])
            payload = json.loads(saved.read_text()) if saved.exists() else None
        return code, out.getvalue(), err.getvalue(), payload

    def test_completed_check_prints_and_saves_the_outcome(self) -> None:
        code, out, _, payload = self._run(_report(_offer(540.0, (OUTBOUND,))))
        self.assertEqual(code, 0)
        self.assertIn("price_changed", out)
        self.assertIn("previous 500 USD", out)
        self.assertIn("current  540 USD", out)
        self.assertEqual(payload["outcome"], "price_changed")

    def test_blocked_check_exits_2(self) -> None:
        code, out, _, payload = self._run(_failed(SearchErrorCode.BLOCKED, rate_limited=True))
        self.assertEqual(code, 2)
        self.assertEqual(payload["outcome"], "check_failed")
        self.assertIn("could not be completed", out)
        self.assertFalse(payload["check_completed"])

    def test_multiple_matches_prints_candidates_and_exits_0(self) -> None:
        report = _report(_offer(510.0, (OUTBOUND,)), _offer(530.0, (OUTBOUND,)))
        code, out, _, payload = self._run(report)
        self.assertEqual(code, 0)
        self.assertEqual(payload["outcome"], "multiple_matches")
        self.assertIn("510 USD", out)
        self.assertIn("530 USD", out)
        self.assertIn("BA178", out)

    def test_incomplete_identity_exits_2_and_loose_flag_is_wired(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        with tempfile.TemporaryDirectory() as tmp:
            offer = Path(tmp) / "offer.json"
            offer.write_text(json.dumps(_previous(bare)), encoding="utf-8")
            stub = _Stub(_report(_offer(500.0, (bare,))))
            shown = io.StringIO()
            with patch("viajante.recheck.search_flights", stub), redirect_stdout(shown):
                strict = main(["recheck-offer", "--offer", str(offer)])
                text = shown.getvalue()
                loose = main(["recheck-offer", "--offer", str(offer), "--allow-loose-match"])
            shown.truncate(0)
            shown.write(text)
        self.assertEqual((strict, loose), (2, 0))
        self.assertIn("no search sent", shown.getvalue())
        self.assertNotIn("None", shown.getvalue().splitlines()[0])
        self.assertEqual(len(stub.calls), 1)

    def test_bad_input_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offer = Path(tmp) / "offer.json"
            offer.write_text("{}", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["recheck-offer", "--offer", str(offer)])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())


class CliInputTests(unittest.TestCase):
    def _error(self, name: str, text: str | None) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            if text is not None:
                path.write_text(text, encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(["recheck-offer", "--offer", str(path)]), 1)
        return err.getvalue()

    def test_bad_json_and_missing_file_get_plain_messages(self) -> None:
        bad = self._error("offer.json", "{oops")
        self.assertIn("offer file is not valid JSON (line 1, column 2)", bad)
        self.assertNotIn("Expecting", bad)
        missing = self._error("nope.json", None)
        self.assertIn("offer file not found:", missing)
        self.assertNotIn("Errno", missing)


if __name__ == "__main__":
    unittest.main()
