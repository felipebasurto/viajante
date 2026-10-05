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
    SelfTransferReport,
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
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertEqual(pairing.connection_minutes, 115)
        self.assertEqual(pairing.status, "ok")
        self.assertIs(pairing.same_airport, True)
        self.assertIs(pairing.protected, False)

    def test_margin_uses_utc_instants_across_zones(self) -> None:
        first = _into_lhr(DAY, "10:00", zone=LONDON)
        second = _out_of_lhr(DAY, "12:00", zone="Europe/Paris")
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertEqual(pairing.connection_minutes, 60)

    def test_margin_counts_the_dst_fall_back_hour(self) -> None:
        change = date(2026, 10, 25)
        first = _into_lhr(change, "00:30")
        second = _out_of_lhr(change, "03:00")
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertEqual(pairing.connection_minutes, 210)

    def test_ambiguous_dst_clock_is_unknown(self) -> None:
        change = date(2026, 10, 25)
        first = _into_lhr(change, "01:30")
        second = _out_of_lhr(change, "06:00")
        (pairing,) = join_self_transfer("LHR", [first], [second], min_connection_hours=0)
        self.assertIsNone(pairing.connection_minutes)
        self.assertEqual(pairing.status, "unknown")

    def test_missing_timezone_is_unknown_never_ok(self) -> None:
        first = _into_lhr(DAY, "10:00", zone=None)
        second = _out_of_lhr(DAY, "14:00")
        for bounds in ({}, {"min_connection_hours": 1, "max_connection_hours": 8}):
            (pairing,) = join_self_transfer("LHR", [first], [second], **bounds)
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
        (pairing,) = join_self_transfer("LHR", [bare], [_out_of_lhr(DAY, "14:00")])
        self.assertIsNone(pairing.same_airport)
        self.assertEqual(pairing.status, "unknown")

    def test_airport_change_is_not_same_airport(self) -> None:
        first = _into_lhr(DAY, "10:00", destination="LGW")
        second = _out_of_lhr(DAY, "14:00")
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertIs(pairing.same_airport, False)
        self.assertEqual(pairing.connection_minutes, 240)

    def test_named_bounds_label_short_and_long(self) -> None:
        first = _into_lhr(DAY, "10:00")
        short = _out_of_lhr(DAY, "11:00", price=50.0)
        fits = _out_of_lhr(DAY, "13:00", price=60.0)
        long = _out_of_lhr(DAY, "20:00", price=70.0)
        pairings = join_self_transfer(
            "LHR", [first], [short, fits, long], min_connection_hours=2, max_connection_hours=6
        )
        by_minutes = {p.connection_minutes: p.status for p in pairings}
        self.assertEqual(by_minutes, {60: "too_short", 180: "ok", 600: "too_long"})

    def test_departure_before_arrival_is_too_short_without_bounds(self) -> None:
        first = _into_lhr(DAY, "10:00")
        second = _out_of_lhr(DAY, "09:30")
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertEqual(pairing.connection_minutes, -30)
        self.assertEqual(pairing.status, "too_short")

    def test_in_bounds_pairs_lead_then_price_then_margin(self) -> None:
        first = _into_lhr(DAY, "10:00", price=100.0)
        cheap_short = _out_of_lhr(DAY, "10:30", price=10.0)
        late = _out_of_lhr(DAY, "16:00", price=50.0)
        early = _out_of_lhr(DAY, "13:00", price=50.0)
        pairings = join_self_transfer(
            "LHR", [first], [cheap_short, late, early], min_connection_hours=2
        )
        self.assertEqual([p.connection_minutes for p in pairings], [180, 360, 30])

    def test_total_price_only_when_currencies_match(self) -> None:
        first = _into_lhr(DAY, "10:00", price=120.0)
        same = _out_of_lhr(DAY, "13:00", price=80.0)
        other = _out_of_lhr(DAY, "14:00", price=80.0, currency="GBP")
        unproven = _out_of_lhr(DAY, "15:00", price=80.0, currency=None)
        pairings = {
            p.second.departure: p
            for p in join_self_transfer("LHR", [first], [same, other, unproven])
        }
        self.assertEqual(pairings["13:00"].total_price, 200.0)
        self.assertEqual(pairings["13:00"].currency, "EUR")
        for clock in ("14:00", "15:00"):
            self.assertIsNone(pairings[clock].total_price)
            self.assertIsNone(pairings[clock].currency)

    def test_bag_verify_surfaces_from_either_ticket(self) -> None:
        first = _into_lhr(DAY, "10:00")
        second = _out_of_lhr(DAY, "13:00", needs_bag_verify=True)
        (pairing,) = join_self_transfer("LHR", [first], [second])
        self.assertIs(pairing.to_dict("EUR")["needs_bag_verify"], True)

    def test_rejects_inverted_or_negative_bounds(self) -> None:
        for bounds in (
            {"min_connection_hours": 5, "max_connection_hours": 2},
            {"min_connection_hours": -1},
            {"max_connection_hours": float("nan")},
        ):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                join_self_transfer("LHR", [], [], **bounds)


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
            report = search_self_transfer("mad", ["lhr"], "jfk", DAY, min_connection_hours=2)
        queries = search.call_args.args[0]
        self.assertEqual(
            [(q.origin, q.destination, q.departure_date) for q in queries],
            [("MAD", "LHR", DAY), ("LHR", "JFK", DAY)],
        )
        self.assertEqual(search.call_args.kwargs["fetch"], "sweep")
        payload = report.to_dict()
        self.assertEqual(payload["vias"], ["LHR"])
        self.assertEqual(payload["failed_vias"], [])
        self.assertIs(payload["protected"], False)
        self.assertEqual(payload["ticketing"], "separate_tickets")
        (pairing,) = payload["pairings"]
        self.assertEqual(
            (pairing["via"], pairing["status"], pairing["connection_minutes"]),
            ("LHR", "ok", 180),
        )
        self.assertEqual(pairing["total_price"], 300.0)
        (legs,) = payload["legs"]
        self.assertEqual(legs["first"]["coverage"]["scope"]["destination"], "LHR")
        self.assertEqual(legs["second"]["coverage"]["succeeded"], 1)

    def test_failed_leg_is_error_evidence_with_no_pairings(self) -> None:
        first_q, second_q = self._queries()
        blocked = SearchError(SearchErrorCode.BLOCKED, "challenge page", rate_limited=True)
        fake = _report(
            QuerySuccess(first_q, 1, 1, (_into_lhr(DAY, "10:00"),)),
            QueryFailure(second_q, blocked),
        )
        with patch("viajante.self_transfer.search_flights", return_value=fake):
            payload = search_self_transfer("MAD", ("LHR",), "JFK", DAY).to_dict()
        self.assertEqual(payload["pairings"], [])
        self.assertEqual(payload["eligible_pairings"], 0)
        self.assertEqual(payload["failed_vias"], ["LHR"])
        (legs,) = payload["legs"]
        self.assertEqual(legs["second"]["coverage"]["failed"], 1)
        self.assertEqual(legs["second"]["coverage"]["empty"], 0)
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
            report = search_self_transfer("MAD", ("LHR",), "JFK", DAY, top=2)
        self.assertEqual(report.eligible_pairings, 6)
        self.assertEqual([p.total_price for p in report.pairings], [310.0, 311.0])

    def test_second_date_is_passed_and_cannot_precede_the_first(self) -> None:
        later = DAY + timedelta(days=2)
        first_q, second_q = self._queries(later)
        fake = _report(QuerySuccess(first_q, 0, 0, ()), QuerySuccess(second_q, 0, 0, ()))
        with patch("viajante.self_transfer.search_flights", return_value=fake) as search:
            search_self_transfer("MAD", ("LHR",), "JFK", DAY, second_date=later)
        self.assertEqual(search.call_args.args[0][1].departure_date, later)
        with patch("viajante.self_transfer.search_flights") as search:
            with self.assertRaises(ValueError):
                search_self_transfer(
                    "MAD", ("LHR",), "JFK", DAY, second_date=DAY - timedelta(days=1)
                )
        search.assert_not_called()

    def test_rejects_via_equal_to_an_endpoint_and_unknown_codes(self) -> None:
        with patch("viajante.self_transfer.search_flights") as search:
            for vias in (["MAD"], ["JFK"], ["ZZQ"], ["LHR", "JFK"], []):
                with self.subTest(vias=vias), self.assertRaises(ValueError):
                    search_self_transfer("MAD", vias, "JFK", DAY)
        search.assert_not_called()


def _via_offer(
    origin: str, destination: str, depart: str, land: str, price: float, day: date = DAY
) -> FlightOffer:
    zones = {
        "MAD": "Europe/Madrid",
        "LHR": LONDON,
        "CDG": "Europe/Paris",
        "JFK": "America/New_York",
    }
    return _offer(
        origin=origin,
        destination=destination,
        departure=(day, depart, zones[origin]),
        arrival=(day, land, zones[destination]),
        price=price,
    )


def _two_via_report(cdg_second: object = None, day: date = DAY) -> SearchReport:
    """MAD-LHR-JFK and MAD-CDG-JFK legs; CDG pairs are cheaper than LHR pairs."""
    lhr_in, lhr_out = FlightQuery("MAD", "LHR", day), FlightQuery("LHR", "JFK", day)
    cdg_in, cdg_out = FlightQuery("MAD", "CDG", day), FlightQuery("CDG", "JFK", day)
    if cdg_second is None:
        cdg_second = QuerySuccess(
            cdg_out, 1, 1, (_via_offer("CDG", "JFK", "14:00", "17:00", 150.0, day),)
        )
    return SearchReport(
        searched_at=datetime(2026, 10, 5, 12, 0),
        queries=(
            QuerySuccess(lhr_in, 1, 1, (_via_offer("MAD", "LHR", "06:00", "08:00", 100.0, day),)),
            QuerySuccess(
                lhr_out,
                2,
                2,
                (
                    _via_offer("LHR", "JFK", "11:00", "14:00", 300.0, day),
                    _via_offer("LHR", "JFK", "12:00", "15:00", 320.0, day),
                ),
            ),
            QuerySuccess(cdg_in, 1, 1, (_via_offer("MAD", "CDG", "07:00", "09:00", 60.0, day),)),
            cdg_second,
        ),
        currency="EUR",
        fetch_backend="sweep",
    )


class MultiViaSelfTransferTests(unittest.TestCase):
    def test_each_via_shops_both_legs_in_one_sweep(self) -> None:
        with patch(
            "viajante.self_transfer.search_flights", return_value=_two_via_report()
        ) as search:
            search_self_transfer("MAD", ["LHR", "CDG"], "JFK", DAY)
        search.assert_called_once()
        self.assertEqual(
            [(q.origin, q.destination) for q in search.call_args.args[0]],
            [("MAD", "LHR"), ("LHR", "JFK"), ("MAD", "CDG"), ("CDG", "JFK")],
        )

    def test_pairings_from_all_vias_share_one_order_and_one_top_cap(self) -> None:
        with patch("viajante.self_transfer.search_flights", return_value=_two_via_report()):
            report = search_self_transfer("MAD", ["LHR", "CDG"], "JFK", DAY, top=2)
        self.assertEqual(report.eligible_pairings, 3)
        self.assertEqual(
            [(p.via, p.total_price) for p in report.pairings],
            [("CDG", 210.0), ("LHR", 400.0)],
        )
        self.assertEqual(report.to_dict()["vias"], ["LHR", "CDG"])

    def test_failed_via_keeps_its_error_and_never_pairs(self) -> None:
        cdg_out = FlightQuery("CDG", "JFK", DAY)
        failure = QueryFailure(cdg_out, SearchError(SearchErrorCode.MARKUP_DRIFT, "drift"))
        with patch("viajante.self_transfer.search_flights", return_value=_two_via_report(failure)):
            report = search_self_transfer("MAD", ["LHR", "CDG"], "JFK", DAY)
        payload = report.to_dict()
        self.assertEqual(payload["failed_vias"], ["CDG"])
        self.assertEqual({p["via"] for p in payload["pairings"]}, {"LHR"})
        self.assertEqual(payload["eligible_pairings"], 2)
        cdg = next(leg for leg in payload["legs"] if leg["via"] == "CDG")
        self.assertEqual(cdg["second"]["coverage"]["failed"], 1)
        self.assertEqual(payload["flights"]["queries"][3]["error"]["code"], "markup_drift")

    def test_repeated_via_is_shopped_and_counted_once(self) -> None:
        one_via = SearchReport(
            searched_at=datetime(2026, 10, 5, 12, 0),
            queries=_two_via_report().queries[:2],
            currency="EUR",
            fetch_backend="sweep",
        )
        with patch("viajante.self_transfer.search_flights", return_value=one_via) as search:
            report = search_self_transfer("MAD", ["LHR", "lhr", " LHR "], "JFK", DAY)
        self.assertEqual(len(search.call_args.args[0]), 2)
        self.assertEqual(report.vias, ("LHR",))
        self.assertEqual(report.eligible_pairings, 2)

    def test_via_cap_counts_distinct_codes_and_rejects_before_searching(self) -> None:
        five = ["LHR", "CDG", "AMS", "FRA", "ZRH"]
        with patch("viajante.self_transfer.search_flights") as search:
            with self.assertRaises(ValueError):
                search_self_transfer("MAD", [*five, "DUB"], "JFK", DAY)
            with self.assertRaises(TypeError):
                search_self_transfer("MAD", "LHR", "JFK", DAY)  # type: ignore[arg-type]
            search.return_value = SearchReport(
                searched_at=datetime(2026, 10, 5, 12, 0),
                queries=tuple(
                    QueryFailure(q, SearchError(SearchErrorCode.NO_RESULTS, "none"))
                    for via in five
                    for q in (FlightQuery("MAD", via, DAY), FlightQuery(via, "JFK", DAY))
                ),
                currency="EUR",
                fetch_backend="sweep",
            )
            report = search_self_transfer("MAD", [*five, "lhr"], "JFK", DAY)
        search.assert_called_once()
        self.assertEqual(report.vias, tuple(five))

    def test_report_rejects_a_pairing_through_an_unsearched_or_failed_via(self) -> None:
        flights = _two_via_report(
            QueryFailure(FlightQuery("CDG", "JFK", DAY), SearchError(SearchErrorCode.BLOCKED, "x"))
        )
        (cdg_pairing,) = join_self_transfer(
            "CDG", flights.queries[2].offers, [_via_offer("CDG", "JFK", "14:00", "17:00", 1.0)]
        )
        (ams_pairing,) = join_self_transfer(
            "AMS", flights.queries[0].offers, flights.queries[1].offers[:1]
        )
        for pairing in (cdg_pairing, ams_pairing):
            with self.subTest(via=pairing.via), self.assertRaises(ValueError):
                SelfTransferReport(
                    searched_at=flights.searched_at,
                    origin="MAD",
                    vias=("LHR", "CDG"),
                    destination="JFK",
                    flights=flights,
                    pairings=(pairing,),
                    eligible_pairings=1,
                    currency="EUR",
                )


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
            (f"MAD-LHR,CDG,AMS,FRA,ZRH,DUB-JFK:{future}",),
            (f"MAD-LHR,MAD-JFK:{future}",),
            (f"MAD-LHR,ZZQ-JFK:{future}",),
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

    def test_multi_via_route_prints_each_pairing_with_its_via(self) -> None:
        day = date.today() + timedelta(days=30)
        with patch(
            "viajante.self_transfer.search_flights", return_value=_two_via_report(day=day)
        ) as search:
            code, out, _ = self._run(f"MAD-LHR,CDG-JFK:{day.isoformat()}", "--top", "2")
        self.assertEqual(code, 0)
        self.assertEqual(
            [q.destination for q in search.call_args.args[0]], ["LHR", "JFK", "CDG", "JFK"]
        )
        self.assertIn("MAD -> LHR,CDG -> JFK", out)
        header, *rows = [line for line in out.splitlines() if "|" in line]
        self.assertIn("via", header)
        self.assertEqual([row.split()[2] for row in rows], ["CDG", "LHR"])
        self.assertIn("(2 of 3 pairings shown)", out)


class MultiViaToolTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_handlers._CACHE.clear()
        evidence.clear()

    def test_comma_list_via_reports_failed_vias_and_pairs_the_rest(self) -> None:
        day = date.today() + timedelta(days=30)
        failure = QueryFailure(
            FlightQuery("CDG", "JFK", day), SearchError(SearchErrorCode.BLOCKED, "challenge")
        )
        with patch(
            "viajante.self_transfer.search_flights",
            return_value=_two_via_report(failure, day=day),
        ) as search:
            payload = mcp_handlers.search_self_transfer_tool(
                "MAD", "LHR, cdg ,LHR", "JFK", day.isoformat()
            )
        self.assertEqual(len(search.call_args.args[0]), 4)
        self.assertEqual(payload["vias"], ["LHR", "CDG"])
        self.assertEqual(payload["failed_vias"], ["CDG"])
        self.assertEqual([p["via"] for p in payload["pairings"]], ["LHR", "LHR"])


if __name__ == "__main__":
    unittest.main()
