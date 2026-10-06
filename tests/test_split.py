from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import patch

from viajante import mcp_handlers
from viajante.cli import main
from viajante.evidence import clear, verify_answer
from viajante.mcp_handlers import search_split_tickets_tool
from viajante.models import (
    FlightOffer,
    FlightQuery,
    OfferEvidence,
    QueryFailure,
    QuerySuccess,
    RawJourneyLeg,
    RawSegment,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
)
from viajante.split import (
    MAX_SPLIT_HUBS,
    layover_hubs,
    search_split_tickets,
)

DAY = date.today() + timedelta(days=30)
NEXT = DAY + timedelta(days=1)
BACK = DAY + timedelta(days=10)
NOW = datetime(2026, 1, 1, 12, 0)
ROUTE = f"JFK-NRT:{DAY.isoformat()}"
RT_ROUTE = f"JFK-NRT:{DAY.isoformat()}:{BACK.isoformat()}"


def _segment(
    origin: str,
    destination: str,
    dep: str,
    arr: str,
    *,
    on: date = DAY,
    lands: Optional[date] = None,
) -> RawSegment:
    return RawSegment(
        origin=origin,
        destination=destination,
        departure=dep,
        arrival=arr,
        departure_date=on,
        arrival_date=lands if lands is not None else on,
    )


def _offer(
    price: float,
    segments: Sequence[RawSegment],
    *,
    airline: str = "Air",
    currency: Optional[str] = None,
) -> FlightOffer:
    leg = RawJourneyLeg(
        departure=segments[0].departure,
        arrival=segments[-1].arrival,
        segments=tuple(segments),
    )
    evidence = None
    if currency is not None:
        evidence = OfferEvidence(
            evidence_id=f"gf_{price}_{airline}",
            query={"trip": "one-way"},
            currency=currency,
            retrieved_at=NOW,
            fetch_backend="sweep",
            query_url=None,
            offer_url=None,
            url_kind="none",
        )
    return FlightOffer(
        airline=airline,
        departure=leg.departure,
        arrival=leg.arrival,
        price_text=f"{price:.0f}",
        price=price,
        duration="5 hr",
        duration_hours=5.0,
        stops="Nonstop" if len(segments) == 1 else f"{len(segments) - 1} stop",
        stops_count=len(segments) - 1,
        baggage_buffer=0,
        needs_bag_verify=False,
        google_flights_url=f"https://example.test/{airline}/{price:.0f}",
        legs=(leg,),
        evidence=evidence,
    )


def _ok(query, offers: Sequence[FlightOffer]) -> QuerySuccess:
    return QuerySuccess(
        query=query,
        raw_count=len(offers),
        eligible_count=len(offers),
        offers=tuple(offers),
        google_flights_url="https://example.test/query",
    )


def _report(results, currency: str = "USD") -> SearchReport:
    return SearchReport(searched_at=NOW, queries=tuple(results), currency=currency)


def _rate_limited_failure(query) -> QueryFailure:
    return QueryFailure(
        query=query,
        error=SearchError(SearchErrorCode.BLOCKED, "Not sent. Google is rate-limiting", True),
    )


class FakeSearch:
    """Stands in for search_flights: answers from a table keyed by (origin, dest, date)."""

    def __init__(self, table, currency: str = "USD") -> None:
        self.table = table
        self.currency = currency
        self.calls: list[list[tuple[str, str, date]]] = []

    def __call__(self, trips, **kwargs) -> SearchReport:
        self.calls.append([(t.origin, t.destination, t.departure_date) for t in trips])
        results = []
        for trip in trips:
            key = (trip.origin, trip.destination, trip.departure_date)
            row = self.table.get(key)
            if isinstance(row, QueryFailure):
                results.append(QueryFailure(query=trip, error=row.error))
            elif row is None:
                results.append(
                    QueryFailure(
                        query=trip,
                        error=SearchError(SearchErrorCode.NO_RESULTS, "No flights"),
                    )
                )
            else:
                results.append(_ok(trip, row))
        return _report(results, self.currency)


def _packaged_via(hub: str, price: float = 900.0, currency: str = "USD") -> SearchReport:
    query = FlightQuery("JFK", "NRT", DAY)
    offer = _offer(
        price,
        (_segment("JFK", hub, "08:00", "11:00"), _segment(hub, "NRT", "13:00", "20:00")),
        currency=currency,
    )
    return _report([_ok(query, [offer])], currency)


def _hub_table(second_dep: str = "15:00") -> dict:
    return {
        ("JFK", "LAX", DAY): [
            _offer(200.0, (_segment("JFK", "LAX", "08:00", "11:30"),), airline="A")
        ],
        ("LAX", "NRT", DAY): [
            _offer(500.0, (_segment("LAX", "NRT", second_dep, "23:00"),), airline="B")
        ],
    }


class HubSplitTests(unittest.TestCase):
    def test_hub_split_pairs_two_real_quotes_with_labels_and_links(self) -> None:
        fake = FakeSearch(_hub_table())
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            search=fake,
        )
        self.assertEqual(report.kind, "hub")
        self.assertEqual(report.hubs, ("LAX",))
        self.assertEqual(report.hubs_source, "packaged_layovers")
        self.assertEqual(len(report.itineraries), 1)
        row = report.itineraries[0].to_dict()
        self.assertIs(row["split_ticket"], True)
        self.assertIs(row["self_transfer"], True)
        self.assertIs(row["connection_protected"], False)
        self.assertEqual(row["tickets"], 2)
        self.assertEqual(row["hub"], "LAX")
        self.assertEqual(row["connection_minutes"], 210)
        self.assertEqual(row["total"], 700.0)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["vs_packaged"]["savings"], 200.0)
        self.assertIn("not protected", row["warning"])
        urls = [part["google_flights_url"] for part in row["parts"]]
        self.assertEqual(urls, ["https://example.test/A/200", "https://example.test/B/500"])
        self.assertEqual([p["price"] for p in row["parts"]], [200.0, 500.0])
        self.assertEqual(fake.calls, [[("JFK", "LAX", DAY), ("LAX", "NRT", DAY)]])
        self.assertEqual(report.extra_searches, 2)
        json.dumps(report.to_dict())

    def test_buffer_too_short_is_rejected_and_parameter_changes_it(self) -> None:
        table = _hub_table(second_dep="13:00")
        short = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            search=FakeSearch(table),
        )
        self.assertEqual(short.itineraries, ())
        self.assertEqual(short.rejected, {"connection_too_short": 1})
        relaxed = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            min_connection_hours=1.5,
            search=FakeSearch(table),
        )
        self.assertEqual(relaxed.itineraries[0].connection_minutes, 90)
        self.assertEqual(relaxed.min_connection_minutes, 90)

    def test_clock_without_owned_arrival_date_is_not_a_proven_connection(self) -> None:
        table = _hub_table()
        table[("JFK", "LAX", DAY)] = [
            _offer(
                200.0,
                (
                    RawSegment(
                        origin="JFK",
                        destination="LAX",
                        departure="08:00",
                        arrival="11:30",
                        departure_date=DAY,
                    ),
                ),
            )
        ]
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            search=FakeSearch(table),
        )
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.rejected, {"timing_unproven": 1})

    def test_currency_mismatch_keeps_parts_and_leaves_total_unknown(self) -> None:
        table = _hub_table()
        table[("LAX", "NRT", DAY)] = [
            _offer(500.0, (_segment("LAX", "NRT", "15:00", "23:00"),), currency="EUR")
        ]
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            search=FakeSearch(table),
        )
        row = report.itineraries[0].to_dict()
        self.assertIsNone(row["total"])
        self.assertIsNone(row["currency"])
        self.assertIn("nothing converts", row["total_note"])
        self.assertNotIn("vs_packaged", row)
        self.assertEqual([part["currency"] for part in row["parts"]], ["USD", "EUR"])
        self.assertEqual([part["price"] for part in row["parts"]], [200.0, 500.0])

    def test_packaged_in_another_currency_is_not_compared(self) -> None:
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX", currency="EUR"),
            search=FakeSearch(_hub_table()),
        )
        row = report.itineraries[0].to_dict()
        self.assertEqual(row["total"], 700.0)
        self.assertNotIn("vs_packaged", row)

    def test_overnight_adds_a_next_day_query_and_marks_the_stop(self) -> None:
        table = _hub_table()
        table[("JFK", "LAX", DAY)] = [
            _offer(200.0, (_segment("JFK", "LAX", "18:00", "21:30"),), airline="A")
        ]
        table[("LAX", "NRT", NEXT)] = [
            _offer(450.0, (_segment("LAX", "NRT", "11:00", "19:00", on=NEXT),), airline="C")
        ]
        base = FakeSearch(table)
        without = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY), packaged=_packaged_via("LAX"), search=base
        )
        self.assertEqual(without.itineraries, ())
        self.assertEqual(len(base.calls[0]), 2)
        fake = FakeSearch(table)
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            allow_overnight=True,
            search=fake,
        )
        self.assertEqual(len(fake.calls[0]), 3)
        self.assertEqual(report.extra_searches, 3)
        best = report.itineraries[0]
        self.assertTrue(best.overnight_at_hub)
        self.assertEqual(best.total, 650.0)
        self.assertEqual(best.connection_minutes, 810)

    def test_hub_candidates_come_from_packaged_layovers_not_origin_or_dest(self) -> None:
        query = FlightQuery("JFK", "NRT", DAY)
        one = _offer(
            900.0,
            (_segment("JFK", "LAX", "08:00", "11:00"), _segment("LAX", "NRT", "13:00", "20:00")),
        )
        two = _offer(
            950.0,
            (_segment("JFK", "SFO", "08:00", "11:00"), _segment("SFO", "NRT", "13:00", "20:00")),
        )
        three = _offer(
            990.0,
            (_segment("JFK", "LAX", "09:00", "12:00"), _segment("LAX", "NRT", "14:00", "21:00")),
        )
        self.assertEqual(
            layover_hubs(_report([_ok(query, [one, two, three])]), "JFK", "NRT"),
            ("LAX", "SFO"),
        )

    def test_user_hubs_are_capped_and_the_rest_are_reported_not_searched(self) -> None:
        fake = FakeSearch({})
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            hubs=["LAX", "SFO", "ORD", "SEA", "DEN"],
            max_hubs=2,
            search=fake,
        )
        self.assertEqual(report.hubs, ("LAX", "SFO"))
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(report.extra_searches, 4)
        self.assertEqual(report.max_extra_searches, 4)
        self.assertEqual(
            [row["hub"] for row in report.skipped_hubs if row["reason"] == "hub_cap"],
            ["ORD", "SEA", "DEN"],
        )
        self.assertEqual(report.coverage.stopping_reason, "hub_cap")
        self.assertFalse(report.coverage.complete)

    def test_cap_has_a_hard_ceiling(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_hubs"):
            search_split_tickets(
                FlightQuery("JFK", "NRT", DAY),
                packaged=_packaged_via("LAX"),
                max_hubs=MAX_SPLIT_HUBS + 1,
                search=FakeSearch({}),
            )

    def test_named_origin_and_dest_are_not_hubs(self) -> None:
        fake = FakeSearch({})
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            hubs=["JFK", "NRT"],
            search=fake,
        )
        self.assertEqual(fake.calls, [])
        self.assertEqual(report.coverage.stopping_reason, "no_hub_candidates")

    def test_recorded_cooldown_sends_nothing(self) -> None:
        fake = FakeSearch(_hub_table())
        state = {"at": 1.0, "until": 4_102_444_800.0, "cooldown_s": 120.0}
        with patch("viajante.split.rate_limit_status", return_value=state):
            report = search_split_tickets(
                FlightQuery("JFK", "NRT", DAY),
                packaged=_packaged_via("LAX"),
                search=fake,
            )
        self.assertEqual(fake.calls, [])
        self.assertTrue(report.error and report.error.rate_limited)
        self.assertIn("Not sent", report.error.message)
        self.assertEqual(report.skipped_hubs[0]["reason"], "rate_limited")
        self.assertEqual(report.coverage.stopping_reason, "rate_limited")

    def test_a_rate_limited_hub_stops_the_remaining_hubs(self) -> None:
        limited = _rate_limited_failure(FlightQuery("JFK", "LAX", DAY))
        fake = FakeSearch({("JFK", "LAX", DAY): limited})
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            hubs=["LAX", "SFO", "ORD"],
            search=fake,
        )
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([row["hub"] for row in report.skipped_hubs], ["SFO", "ORD"])
        self.assertTrue(report.error and report.error.rate_limited)

    def test_missing_packaged_is_searched_once_first(self) -> None:
        fake = FakeSearch({("JFK", "NRT", DAY): _packaged_via("LAX").queries[0].offers})
        fake.table.update(_hub_table())
        report = search_split_tickets(FlightQuery("JFK", "NRT", DAY), search=fake)
        self.assertTrue(report.packaged_searched)
        self.assertEqual(fake.calls[0], [("JFK", "NRT", DAY)])
        self.assertEqual(report.itineraries[0].savings, 200.0)

    def test_price_cap_drops_splits_that_cannot_prove_it(self) -> None:
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY, price_cap=650),
            packaged=_packaged_via("LAX"),
            search=FakeSearch(_hub_table()),
        )
        self.assertEqual(report.itineraries, ())


class MixedOneWayTests(unittest.TestCase):
    def _table(self) -> dict:
        return {
            ("JFK", "NRT", DAY): [
                _offer(300.0, (_segment("JFK", "NRT", "10:00", "14:00"),), airline="A"),
                _offer(280.0, (_segment("JFK", "NRT", "11:00", "15:00"),), airline="B"),
            ],
            ("NRT", "JFK", BACK): [
                _offer(350.0, (_segment("NRT", "JFK", "10:00", "09:00", on=BACK),), airline="C"),
                _offer(250.0, (_segment("NRT", "JFK", "12:00", "11:00", on=BACK),), airline="D"),
            ],
        }

    def _packaged(self, price: float = 700.0, currency: str = "USD") -> SearchReport:
        query = RoundTrip("JFK", "NRT", DAY, BACK)
        offer = _offer(price, (_segment("JFK", "NRT", "10:00", "14:00"),), currency=currency)
        return _report([_ok(query, [offer])], currency)

    def test_cheapest_outbound_plus_cheapest_return_compared_with_round_trip(self) -> None:
        fake = FakeSearch(self._table())
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=fake
        )
        self.assertEqual(report.kind, "mixed_one_ways")
        self.assertEqual(fake.calls, [[("JFK", "NRT", DAY), ("NRT", "JFK", BACK)]])
        row = report.itineraries[0].to_dict()
        self.assertEqual([part["role"] for part in row["parts"]], ["outbound", "return"])
        self.assertEqual([part["offer"]["airline"] for part in row["parts"]], ["B", "D"])
        self.assertEqual(row["total"], 530.0)
        self.assertEqual(row["vs_packaged"]["savings"], 170.0)
        self.assertEqual(row["vs_packaged"]["packaged"]["price"], 700.0)
        self.assertIs(row["split_ticket"], True)
        self.assertIs(row["self_transfer"], False)
        self.assertIs(row["connection_protected"], False)
        self.assertNotIn("hub", row)

    def test_split_dearer_than_round_trip_shows_negative_savings(self) -> None:
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK),
            packaged=self._packaged(price=500.0),
            search=FakeSearch(self._table()),
        )
        self.assertEqual(report.itineraries[0].savings, -30.0)

    def test_round_trip_in_another_currency_is_not_compared(self) -> None:
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK),
            packaged=self._packaged(currency="EUR"),
            search=FakeSearch(self._table()),
        )
        row = report.itineraries[0].to_dict()
        self.assertEqual(row["total"], 530.0)
        self.assertNotIn("vs_packaged", row)

    def test_a_missing_direction_gives_no_split_and_never_estimates(self) -> None:
        table = self._table()
        del table[("NRT", "JFK", BACK)]
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=FakeSearch(table)
        )
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.coverage.empty, 1)

    def test_hubs_are_rejected_for_round_trips(self) -> None:
        with self.assertRaisesRegex(ValueError, "one-way"):
            search_split_tickets(
                RoundTrip("JFK", "NRT", DAY, BACK),
                packaged=self._packaged(),
                hubs=["LAX"],
                search=FakeSearch({}),
            )

    def test_cooldown_sends_nothing(self) -> None:
        fake = FakeSearch(self._table())
        state = {"at": 1.0, "until": 4_102_444_800.0, "cooldown_s": 120.0}
        with patch("viajante.split.rate_limit_status", return_value=state):
            report = search_split_tickets(
                RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=fake
            )
        self.assertEqual(fake.calls, [])
        self.assertEqual(report.itineraries, ())
        self.assertTrue(report.error and report.error.rate_limited)


class SplitCliTests(unittest.TestCase):
    def _run(self, argv, *, split_search=None):
        packaged = _packaged_via("LAX")
        real = search_split_tickets
        seen: dict[str, object] = {}

        def split(query, **kwargs):
            seen.update(kwargs)
            return real(query, search=split_search or FakeSearch(_hub_table()), **kwargs)

        out, err = io.StringIO(), io.StringIO()
        with (
            patch("viajante.cli.search_flights", return_value=packaged) as flights,
            patch("viajante.cli.search_split_tickets", side_effect=split),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(argv)
        return code, out.getvalue(), err.getvalue(), flights, seen

    def test_options_without_the_opt_in_are_rejected_before_any_search(self) -> None:
        code, _out, err, flights, _seen = self._run(["flights", ROUTE, "--split-hubs", "LAX"])
        self.assertEqual(code, 1)
        self.assertIn("--split-tickets", err)
        flights.assert_not_called()

    def test_split_needs_exactly_one_one_way_or_round_trip(self) -> None:
        for argv in (
            ["flights", ROUTE, f"LAX-JFK:{BACK.isoformat()}", "--split-tickets"],
            ["flights", "--trip", "rt", RT_ROUTE, "--split-tickets", "--split-hubs", "LAX"],
            ["flights", ROUTE, "--split-tickets", "--split-max-hubs", "9"],
        ):
            code, _out, _err, flights, _seen = self._run(argv)
            self.assertEqual(code, 1, argv)
            flights.assert_not_called()

    def test_prints_both_tickets_labels_links_and_savings(self) -> None:
        code, out, _err, _flights, seen = self._run(
            ["flights", ROUTE, "--split-tickets", "--split-min-connection", "3"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen["min_connection_hours"], 3.0)
        self.assertEqual(seen["fetch"], "sweep")
        self.assertIn("SPLIT TICKETS (self-transfer via a hub): JFK -> NRT", out)
        self.assertIn("not protected", out)
        self.assertIn("700 USD total  via LAX, 3h 30m connection", out)
        self.assertIn("saves 200 USD vs best packaged 900 USD", out)
        self.assertIn("ticket 1 (first leg)  JFK -> LAX", out)
        self.assertIn("ticket 2 (second leg)  LAX -> NRT", out)
        self.assertIn("https://example.test/A/200", out)
        self.assertIn("https://example.test/B/500", out)

    def test_save_adds_the_split_report_without_touching_existing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            code, *_ = self._run(["flights", ROUTE, "--split-tickets", "--save", str(target)])
            data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(data["schema_version"], 2)
        self.assertEqual(data["split_tickets"]["itineraries"][0]["total"], 700.0)
        self.assertIs(data["split_tickets"]["itineraries"][0]["connection_protected"], False)


class SplitMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_handlers._CACHE.clear()
        clear()

    def _call(self, route: str, fake: FakeSearch, **kwargs):
        real = search_split_tickets
        with patch(
            "viajante.mcp_handlers.search_split_tickets",
            side_effect=lambda query, **kw: real(
                query, packaged=_packaged_via("LAX"), search=fake, **kw
            ),
        ) as split:
            return search_split_tickets_tool(route, **kwargs), split

    def test_hub_split_over_the_tool_and_ledger_owns_the_savings(self) -> None:
        payload, split = self._call(ROUTE, FakeSearch(_hub_table()), hubs="lax", max_hubs=2)
        self.assertEqual(split.call_args.kwargs["hubs"], ("LAX",))
        self.assertEqual(split.call_args.kwargs["max_hubs"], 2)
        row = payload["itineraries"][0]
        self.assertIs(row["connection_protected"], False)
        self.assertEqual(row["vs_packaged"]["savings"], 200.0)
        self.assertTrue(verify_answer("The split saves 200 USD against the packaged fare.")["ok"])
        self.assertFalse(verify_answer("The split saves 321 USD.")["ok"])

    def test_round_trip_over_the_tool_is_mixed_one_ways(self) -> None:
        fake = FakeSearch(MixedOneWayTests()._table())
        payload, _split = self._call(RT_ROUTE, fake, trip="rt")
        self.assertEqual(payload["mode"], "mixed_one_ways")
        self.assertEqual(payload["itineraries"][0]["total"], 530.0)

    def test_busy_lock_and_bad_input_fail_before_any_search(self) -> None:
        fake = FakeSearch(_hub_table())
        with patch.object(mcp_handlers, "search_split_tickets") as split:
            past = (date.today() - timedelta(days=1)).isoformat()
            with self.assertRaisesRegex(ValueError, "past"):
                search_split_tickets_tool(f"JFK-NRT:{past}")
            with self.assertRaisesRegex(ValueError, "one-way"):
                search_split_tickets_tool(RT_ROUTE, trip="rt", hubs="LAX")
            with self.assertRaisesRegex(ValueError, "max_hubs"):
                search_split_tickets_tool(ROUTE, max_hubs=99)
            with mcp_handlers._SEARCH_LOCK:
                with self.assertRaisesRegex(ValueError, "already running"):
                    search_split_tickets_tool(ROUTE)
            split.assert_not_called()
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
