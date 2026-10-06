from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from viajante import evidence, mcp_handlers
from viajante.cli import main
from viajante.google_flights import note_rate_limited
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
from viajante.recheck import CAVEAT, recheck_offer

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
) -> dict:
    row = dict(_offer(price, segments).to_dict("USD"))
    evidence_row: dict = {
        "source": "google_flights",
        "query": query if query is not None else dict(_query().to_dict()),
        "fetch_backend": backend,
    }
    if currency:
        evidence_row["currency"] = currency
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
        self.assertTrue(result["price_comparable"])
        self.assertEqual(
            result["previous"], {**result["previous"], "price": 500.0, "currency": "USD"}
        )
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
            _previous(OUTBOUND), search=_Stub(_report(_offer(500.0, (retimed,))))
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
        result = recheck_offer(_previous(OUTBOUND), search=_Stub(_report(_offer(450.0, via))))
        self.assertEqual(result["outcome"], "substituted")
        fields = {row["field"] for row in result["differences"]}
        self.assertEqual(fields, {"flight_numbers", "route", "departure", "arrival"})
        self.assertEqual(result["current"]["price"], 450.0)

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
        self.assertEqual(result["outcome"], "not_found")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rate_limited")
        self.assertTrue(result["error"]["rate_limited"])
        self.assertIsNone(result["current"])
        self.assertTrue(any("could not be completed" in note for note in result["notes"]))
        self.assertNotIn("provider_empty", json.dumps(result))

    def test_blocked_and_drift_are_not_completed(self) -> None:
        for code in (
            SearchErrorCode.BLOCKED,
            SearchErrorCode.MARKUP_DRIFT,
            SearchErrorCode.REJECTED,
            SearchErrorCode.FETCH_FAILED,
        ):
            with self.subTest(code=code):
                result = recheck_offer(_previous(OUTBOUND), search=_Stub(_failed(code)))
                self.assertEqual(result["outcome"], "not_found")
                self.assertFalse(result["check_completed"])
                self.assertEqual(result["reason"], code.value)

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
        self.assertEqual(result["outcome"], "not_found")
        self.assertFalse(result["check_completed"])
        self.assertEqual(result["reason"], "rate_limited")


class CurrencyTests(unittest.TestCase):
    def test_other_currency_finds_the_itinerary_but_compares_no_amounts(self) -> None:
        stub = _Stub(_report(_offer(460.0, (OUTBOUND,)), currency="EUR"))
        result = recheck_offer(_previous(OUTBOUND, price=500.0), currency="EUR", search=stub)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertFalse(result["price_comparable"])
        self.assertEqual(result["previous"]["currency"], "USD")
        self.assertEqual(result["current"]["currency"], "EUR")
        self.assertEqual(stub.calls[0][1]["currency"], "EUR")
        self.assertNotIn("delta", result)
        self.assertTrue(any("not compared" in note for note in result["notes"]))

    def test_same_amount_in_another_currency_is_not_same_price(self) -> None:
        stub = _Stub(_report(_offer(500.0, (OUTBOUND,)), currency="GBP"))
        result = recheck_offer(_previous(OUTBOUND, price=500.0), currency="GBP", search=stub)
        self.assertEqual(result["outcome"], "price_changed")
        self.assertFalse(result["price_comparable"])

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
        result = recheck_offer(_previous(bare), search=stub)
        self.assertEqual(result["outcome"], "same_price")
        self.assertEqual(result["match_basis"], "carrier_times")
        self.assertTrue(any("Flight numbers are absent" in note for note in result["notes"]))

    def test_carrier_fallback_still_needs_matching_times(self) -> None:
        bare = _segment("JFK", "LHR", "19:30", "07:30", None)
        later = _segment("JFK", "LHR", "23:50", "11:50", None)
        result = recheck_offer(_previous(bare), search=_Stub(_report(_offer(500.0, (later,)))))
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

    def test_result_is_recorded_so_verify_answer_owns_both_amounts(self) -> None:
        stub = _Stub(_report(_offer(540.0, (OUTBOUND,))))
        with patch("viajante.recheck.search_flights", stub):
            mcp_handlers.recheck_offer_tool(_previous(OUTBOUND))
        verdict = evidence.verify_answer("It was USD 500 and is now USD 540.")
        self.assertTrue(verdict["ok"], verdict)

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
        self.assertIn("could not be completed", out)
        self.assertFalse(payload["check_completed"])

    def test_bad_input_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offer = Path(tmp) / "offer.json"
            offer.write_text("{}", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["recheck-offer", "--offer", str(offer)])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
