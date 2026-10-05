from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from viajante import cli, evidence, mcp_handlers
from viajante.models import (
    FlightOffer,
    FlightQuery,
    OfferEvidence,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RawSegment,
    SearchError,
    SearchErrorCode,
    SearchReport,
)
from viajante.self_transfer import join_self_transfer, search_self_transfer

LONDON = "Europe/London"


def _offer(
    *,
    origin: str,
    destination: str,
    departure: tuple[date, str, Optional[str]],
    arrival: tuple[date, str, Optional[str]],
    price: float = 100.0,
    currency: Optional[str] = "EUR",
    needs_bag_verify: bool = False,
) -> FlightOffer:
    segment = RawSegment(
        origin=origin,
        destination=destination,
        departure=departure[1],
        arrival=arrival[1],
        departure_date=departure[0],
        arrival_date=arrival[0],
        departure_timezone=departure[2],
        arrival_timezone=arrival[2],
    )
    evidence = None
    if currency is not None:
        evidence = OfferEvidence(
            evidence_id=f"ev-{origin}-{destination}-{price}",
            query={},
            currency=currency,
            retrieved_at=datetime(2026, 10, 5, 12, 0),
            fetch_backend="sweep",
            query_url=None,
            offer_url=None,
            url_kind="none",
        )
    return FlightOffer(
        airline="Example Air",
        departure=departure[1],
        arrival=arrival[1],
        price_text=f"€{price:.0f}",
        price=price,
        duration=None,
        duration_hours=None,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer=0,
        needs_bag_verify=needs_bag_verify,
        legs=(RawJourneyLeg(departure[1], arrival[1], segments=(segment,)),),
        evidence=evidence,
    )


DAY = date(2026, 11, 10)


def _into_lhr(day: date, clock: str, zone: Optional[str] = LONDON, **kw) -> FlightOffer:
    return _offer(
        origin="MAD",
        destination=kw.pop("destination", "LHR"),
        departure=(day, "06:00", "Europe/Madrid"),
        arrival=(day, clock, zone),
        **kw,
    )


def _out_of_lhr(day: date, clock: str, zone: Optional[str] = LONDON, **kw) -> FlightOffer:
    return _offer(
        origin=kw.pop("origin", "LHR"),
        destination="JFK",
        departure=(day, clock, zone),
        arrival=(day, "23:00", "America/New_York"),
        **kw,
    )


class JoinSelfTransferTests(unittest.TestCase):
    def test_margin_crosses_midnight_on_provider_dates(self) -> None:
        first = _into_lhr(DAY, "23:10")
        second = _out_of_lhr(DAY + timedelta(days=1), "01:05")
        (pairing,) = join_self_transfer([first], [second])
        self.assertEqual(pairing.connection_minutes, 115)
        self.assertEqual(pairing.status, "ok")
        self.assertIs(pairing.same_airport, True)
        self.assertIs(pairing.protected, False)

    def test_margin_uses_utc_instants_across_zones(self) -> None:
        first = _into_lhr(DAY, "10:00", zone=LONDON)
        second = _out_of_lhr(DAY, "12:00", zone="Europe/Paris")
        (pairing,) = join_self_transfer([first], [second])
        self.assertEqual(pairing.connection_minutes, 60)

    def test_margin_counts_the_dst_fall_back_hour(self) -> None:
        change = date(2026, 10, 25)
        first = _into_lhr(change, "00:30")
        second = _out_of_lhr(change, "03:00")
        (pairing,) = join_self_transfer([first], [second])
        self.assertEqual(pairing.connection_minutes, 210)

    def test_ambiguous_dst_clock_is_unknown(self) -> None:
        change = date(2026, 10, 25)
        first = _into_lhr(change, "01:30")
        second = _out_of_lhr(change, "06:00")
        (pairing,) = join_self_transfer([first], [second], min_connection_hours=0)
        self.assertIsNone(pairing.connection_minutes)
        self.assertEqual(pairing.status, "unknown")

    def test_missing_timezone_is_unknown_never_ok(self) -> None:
        first = _into_lhr(DAY, "10:00", zone=None)
        second = _out_of_lhr(DAY, "14:00")
        for bounds in ({}, {"min_connection_hours": 1, "max_connection_hours": 8}):
            (pairing,) = join_self_transfer([first], [second], **bounds)
            self.assertIsNone(pairing.connection_minutes)
            self.assertEqual(pairing.status, "unknown")

    def test_empty_segments_leave_airport_and_margin_unknown(self) -> None:
        bare = FlightOffer(
            airline=None,
            departure="06:00",
            arrival="09:00",
            price_text="€80",
            price=80.0,
            duration=None,
            duration_hours=None,
            stops=None,
            stops_count=None,
            baggage_buffer=0,
            needs_bag_verify=False,
        )
        (pairing,) = join_self_transfer([bare], [_out_of_lhr(DAY, "14:00")])
        self.assertIsNone(pairing.same_airport)
        self.assertEqual(pairing.status, "unknown")

    def test_airport_change_is_not_same_airport(self) -> None:
        first = _into_lhr(DAY, "10:00", destination="LGW")
        second = _out_of_lhr(DAY, "14:00")
        (pairing,) = join_self_transfer([first], [second])
        self.assertIs(pairing.same_airport, False)
        self.assertEqual(pairing.connection_minutes, 240)

    def test_named_bounds_label_short_and_long(self) -> None:
        first = _into_lhr(DAY, "10:00")
        short = _out_of_lhr(DAY, "11:00", price=50.0)
        fits = _out_of_lhr(DAY, "13:00", price=60.0)
        long = _out_of_lhr(DAY, "20:00", price=70.0)
        pairings = join_self_transfer(
            [first], [short, fits, long], min_connection_hours=2, max_connection_hours=6
        )
        by_minutes = {p.connection_minutes: p.status for p in pairings}
        self.assertEqual(by_minutes, {60: "too_short", 180: "ok", 600: "too_long"})

    def test_departure_before_arrival_is_too_short_without_bounds(self) -> None:
        first = _into_lhr(DAY, "10:00")
        second = _out_of_lhr(DAY, "09:30")
        (pairing,) = join_self_transfer([first], [second])
        self.assertEqual(pairing.connection_minutes, -30)
        self.assertEqual(pairing.status, "too_short")

    def test_in_bounds_pairs_lead_then_price_then_margin(self) -> None:
        first = _into_lhr(DAY, "10:00", price=100.0)
        cheap_short = _out_of_lhr(DAY, "10:30", price=10.0)
        late = _out_of_lhr(DAY, "16:00", price=50.0)
        early = _out_of_lhr(DAY, "13:00", price=50.0)
        pairings = join_self_transfer([first], [cheap_short, late, early], min_connection_hours=2)
        self.assertEqual([p.connection_minutes for p in pairings], [180, 360, 30])

    def test_total_price_only_when_currencies_match(self) -> None:
        first = _into_lhr(DAY, "10:00", price=120.0)
        same = _out_of_lhr(DAY, "13:00", price=80.0)
        other = _out_of_lhr(DAY, "14:00", price=80.0, currency="GBP")
        unproven = _out_of_lhr(DAY, "15:00", price=80.0, currency=None)
        pairings = {
            p.second.departure: p for p in join_self_transfer([first], [same, other, unproven])
        }
        self.assertEqual(pairings["13:00"].total_price, 200.0)
        self.assertEqual(pairings["13:00"].currency, "EUR")
        for clock in ("14:00", "15:00"):
            self.assertIsNone(pairings[clock].total_price)
            self.assertIsNone(pairings[clock].currency)

    def test_bag_verify_surfaces_from_either_ticket(self) -> None:
        first = _into_lhr(DAY, "10:00")
        second = _out_of_lhr(DAY, "13:00", needs_bag_verify=True)
        (pairing,) = join_self_transfer([first], [second])
        self.assertIs(pairing.to_dict("EUR")["needs_bag_verify"], True)

    def test_rejects_inverted_or_negative_bounds(self) -> None:
        for bounds in (
            {"min_connection_hours": 5, "max_connection_hours": 2},
            {"min_connection_hours": -1},
            {"max_connection_hours": float("nan")},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                join_self_transfer([], [], **bounds)


def _report(first, second, currency: str = "EUR") -> SearchReport:
    return SearchReport(
        searched_at=datetime(2026, 10, 5, 12, 0),
        queries=(first, second),
        currency=currency,
        fetch_backend="sweep",
    )


class SearchSelfTransferTests(unittest.TestCase):
    def _queries(self, second_day: date = DAY) -> tuple[FlightQuery, FlightQuery]:
        return FlightQuery("MAD", "LHR", DAY), FlightQuery("LHR", "JFK", second_day)

    def test_shops_two_one_way_legs_in_one_sweep_and_joins(self) -> None:
        first_q, second_q = self._queries()
        fake = _report(
            QuerySuccess(first_q, 1, 1, (_into_lhr(DAY, "10:00", price=90.0),)),
            QuerySuccess(second_q, 1, 1, (_out_of_lhr(DAY, "13:00", price=210.0),)),
        )
        with patch("viajante.self_transfer.search_flights", return_value=fake) as search:
            report = search_self_transfer("mad", "lhr", "jfk", DAY, min_connection_hours=2)
        queries = search.call_args.args[0]
        self.assertEqual(
            [(q.origin, q.destination, q.departure_date) for q in queries],
            [("MAD", "LHR", DAY), ("LHR", "JFK", DAY)],
        )
        self.assertEqual(search.call_args.kwargs["fetch"], "sweep")
        payload = report.to_dict()
        self.assertEqual(payload["via"], "LHR")
        self.assertIs(payload["protected"], False)
        self.assertEqual(payload["ticketing"], "separate_tickets")
        (pairing,) = payload["pairings"]
        self.assertEqual(
            (pairing["status"], pairing["connection_minutes"], pairing["total_price"]),
            ("ok", 180, 300.0),
        )
        self.assertEqual(payload["first_leg"]["coverage"]["scope"]["destination"], "LHR")
        self.assertEqual(payload["second_leg"]["coverage"]["succeeded"], 1)

    def test_failed_leg_is_error_evidence_with_no_pairings(self) -> None:
        first_q, second_q = self._queries()
        blocked = SearchError(SearchErrorCode.BLOCKED, "challenge page", rate_limited=True)
        fake = _report(
            QuerySuccess(first_q, 1, 1, (_into_lhr(DAY, "10:00"),)),
            QueryFailure(second_q, blocked),
        )
        with patch("viajante.self_transfer.search_flights", return_value=fake):
            payload = search_self_transfer("MAD", "LHR", "JFK", DAY).to_dict()
        self.assertEqual(payload["pairings"], [])
        self.assertEqual(payload["eligible_pairings"], 0)
        self.assertEqual(payload["second_leg"]["coverage"]["failed"], 1)
        self.assertEqual(payload["second_leg"]["coverage"]["empty"], 0)
        second = payload["flights"]["queries"][1]
        self.assertEqual(
            second["error"], {"code": "blocked", "message": "challenge page", "rate_limited": True}
        )

    def test_top_caps_pairings_and_reports_eligible(self) -> None:
        first_q, second_q = self._queries()
        firsts = tuple(_into_lhr(DAY, f"0{h}:00", price=100.0 + h) for h in (7, 8, 9))
        seconds = tuple(_out_of_lhr(DAY, f"1{h}:00", price=200.0 + h) for h in (3, 4))
        fake = _report(QuerySuccess(first_q, 3, 3, firsts), QuerySuccess(second_q, 2, 2, seconds))
        with patch("viajante.self_transfer.search_flights", return_value=fake):
            report = search_self_transfer("MAD", "LHR", "JFK", DAY, top=2)
        self.assertEqual(report.eligible_pairings, 6)
        self.assertEqual([p.total_price for p in report.pairings], [310.0, 311.0])

    def test_second_date_is_passed_and_cannot_precede_the_first(self) -> None:
        later = DAY + timedelta(days=2)
        first_q, second_q = self._queries(later)
        fake = _report(QuerySuccess(first_q, 0, 0, ()), QuerySuccess(second_q, 0, 0, ()))
        with patch("viajante.self_transfer.search_flights", return_value=fake) as search:
            search_self_transfer("MAD", "LHR", "JFK", DAY, second_date=later)
        self.assertEqual(search.call_args.args[0][1].departure_date, later)
        with patch("viajante.self_transfer.search_flights") as search:
            with self.assertRaises(ValueError):
                search_self_transfer("MAD", "LHR", "JFK", DAY, second_date=DAY - timedelta(days=1))
        search.assert_not_called()

    def test_rejects_via_equal_to_an_endpoint_and_unknown_codes(self) -> None:
        with patch("viajante.self_transfer.search_flights") as search:
            for route in (("MAD", "MAD", "JFK"), ("MAD", "JFK", "JFK"), ("MAD", "ZZQ", "JFK")):
                with self.subTest(route=route), self.assertRaises(ValueError):
                    search_self_transfer(*route, DAY)
        search.assert_not_called()


class SelfTransferToolTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_handlers._CACHE.clear()
        evidence.clear()

    def test_tool_records_leg_offers_for_details_and_owns_the_total(self) -> None:
        day = date.today() + timedelta(days=30)
        first_q, second_q = FlightQuery("MAD", "LHR", day), FlightQuery("LHR", "JFK", day)
        fake = _report(
            QuerySuccess(first_q, 1, 1, (_into_lhr(day, "10:00", price=95.0),)),
            QuerySuccess(second_q, 1, 1, (_out_of_lhr(day, "13:00", price=240.0),)),
        )
        with patch("viajante.self_transfer.search_flights", return_value=fake):
            payload = mcp_handlers.search_self_transfer_tool(
                "MAD", "LHR", "JFK", day.isoformat(), min_connection_hours=2
            )
        (pairing,) = payload["pairings"]
        self.assertEqual((pairing["status"], pairing["total_price"]), ("ok", 335.0))
        leg_offer = payload["flights"]["queries"][1]["offers"][0]
        detail = mcp_handlers.get_flight_details_tool(leg_offer["selection_id"])
        self.assertEqual(detail["original_quote"]["query"]["origin"], "LHR")
        self.assertTrue(evidence.verify_answer("Both tickets total EUR 335 via LHR.")["ok"])

    def test_tool_rejects_a_past_second_date_before_searching(self) -> None:
        future = (date.today() + timedelta(days=30)).isoformat()
        past = (date.today() - timedelta(days=1)).isoformat()
        with patch("viajante.self_transfer.search_flights") as search:
            with self.assertRaises(ValueError):
                mcp_handlers.search_self_transfer_tool(
                    "MAD", "LHR", "JFK", future, second_date=past
                )
        search.assert_not_called()


class SelfTransferCliTests(unittest.TestCase):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["self-transfer", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_invalid_input_exits_before_searching(self) -> None:
        future = (date.today() + timedelta(days=30)).isoformat()
        past = (date.today() - timedelta(days=1)).isoformat()
        cases = (
            (f"MAD-LHR-JFK:{past}",),
            (f"MAD-MAD-JFK:{future}",),
            (f"MAD-JFK:{future}",),
            (f"MAD-LHR-JFK:{future}", "--min-connection", "6", "--max-connection", "2"),
        )
        with patch("viajante.self_transfer.search_flights") as search:
            for argv in cases:
                with self.subTest(argv=argv):
                    code, _, err = self._run(*argv)
                    self.assertEqual(code, 1)
                    self.assertIn("error:", err)
        search.assert_not_called()

    def test_prints_pairings_and_saves_json(self) -> None:
        day = date.today() + timedelta(days=30)
        first_q, second_q = FlightQuery("MAD", "LHR", day), FlightQuery("LHR", "JFK", day)
        fake = _report(
            QuerySuccess(first_q, 1, 1, (_into_lhr(day, "10:00", price=95.0),)),
            QuerySuccess(second_q, 1, 1, (_out_of_lhr(day, "12:05", price=240.0),)),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pairs.json"
            with patch("viajante.self_transfer.search_flights", return_value=fake):
                code, out, err = self._run(
                    f"MAD-LHR-JFK:{day.isoformat()}",
                    "--currency",
                    "EUR",
                    "--min-connection",
                    "3",
                    "--save",
                    str(target),
                )
            saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertIn("too_short", out)
        self.assertIn("2h05", out)
        self.assertIn("separate tickets", out)
        self.assertIn("Two separate tickets", err)
        self.assertEqual(saved["pairings"][0]["connection_minutes"], 125)
        self.assertIs(saved["protected"], False)


if __name__ == "__main__":
    unittest.main()
