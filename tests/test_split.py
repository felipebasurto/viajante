from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Sequence
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante import mcp_handlers
from viajante.cli import main
from viajante.cli_report import _print_split_report
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
    SplitItinerary,
    SplitPart,
    _instant,
    _rank,
    layover_hubs,
    search_split_tickets,
)


def _calm(day: date) -> date:
    """Skip the weeks around DST changes so fixed-clock gaps in the fixtures stay exact."""
    while (3, 5) <= (day.month, day.day) <= (3, 31) or (10, 22) <= (day.month, day.day) <= (11, 8):
        day += timedelta(days=1)
    return day


DAY = _calm(date.today() + timedelta(days=30))
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
            via=["LAX", "SFO", "ORD", "SEA", "DEN"],
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
            via=["JFK", "NRT"],
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
            via=["LAX", "SFO", "ORD"],
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

    def test_tickets_must_meet_at_the_hub_airport(self) -> None:
        for arrives, departs in (("BUR", "LAX"), ("LAX", "SFO")):
            table = _hub_table()
            table[("JFK", "LAX", DAY)] = [
                _offer(200.0, (_segment("JFK", arrives, "08:00", "11:30"),), airline="A")
            ]
            table[("LAX", "NRT", DAY)] = [
                _offer(500.0, (_segment(departs, "NRT", "15:00", "23:00"),), airline="B")
            ]
            report = search_split_tickets(
                FlightQuery("JFK", "NRT", DAY),
                packaged=_packaged_via("LAX"),
                search=FakeSearch(table),
            )
            self.assertEqual(report.itineraries, (), (arrives, departs))
            self.assertEqual(report.rejected, {"airport_mismatch": 1})

    def test_airports_without_owned_segments_are_not_assumed_to_match(self) -> None:
        table = _hub_table()
        (bare,) = table[("LAX", "NRT", DAY)]
        table[("LAX", "NRT", DAY)] = [
            replace(bare, legs=(RawJourneyLeg(departure="15:00", arrival="23:00"),))
        ]
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY), packaged=_packaged_via("LAX"), search=FakeSearch(table)
        )
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.rejected, {"airport_unproven": 1})

    def test_ranking_never_compares_raw_totals_across_currencies(self) -> None:
        table = {
            ("JFK", "LAX", DAY): [
                _offer(100.0, (_segment("JFK", "LAX", "08:00", "11:30"),), currency="JPY")
            ],
            ("LAX", "NRT", DAY): [
                _offer(150.0, (_segment("LAX", "NRT", "15:00", "23:00"),), currency="JPY")
            ],
            ("JFK", "SFO", DAY): [
                _offer(200.0, (_segment("JFK", "SFO", "08:00", "11:30"),), currency="USD")
            ],
            ("SFO", "NRT", DAY): [
                _offer(300.0, (_segment("SFO", "NRT", "15:00", "23:00"),), currency="USD"),
                _offer(900.0, (_segment("SFO", "NRT", "16:00", "23:50"),), currency="USD"),
            ],
        }
        kwargs = dict(packaged=_packaged_via("LAX"), via=["LAX", "SFO"])
        ranked = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY), top=1, search=FakeSearch(table), **kwargs
        )
        self.assertEqual(
            [(row.hub, row.currency, row.total) for row in ranked.itineraries],
            [("SFO", "USD", 500.0), ("LAX", "JPY", 250.0)],
        )
        self.assertEqual(ranked.itineraries[0].savings, 400.0)
        self.assertIsNone(ranked.itineraries[1].savings)
        capped = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY, price_cap=600),
            search=FakeSearch(table),
            **kwargs,
        )
        self.assertEqual([row.hub for row in capped.itineraries], ["SFO"])

    def test_other_currency_rows_are_capped_and_the_cut_is_reported(self) -> None:
        table = {
            ("JFK", "LAX", DAY): [
                _offer(
                    100.0 + n,
                    (_segment("JFK", "LAX", "08:00", "11:30"),),
                    airline=f"A{n}",
                    currency="JPY",
                )
                for n in range(5)
            ],
            ("LAX", "NRT", DAY): [
                _offer(500.0, (_segment("LAX", "NRT", "15:00", "23:00"),), currency="JPY")
            ],
        }
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            top=10,
            search=FakeSearch(table),
        )
        self.assertEqual([row.total for row in report.itineraries], [600.0, 601.0, 602.0])
        self.assertEqual(report.omitted_other_currency, 2)
        payload = report.to_dict()
        self.assertEqual(payload["omitted_other_currency"], 2)
        self.assertEqual(payload["other_currency_row_cap"], 3)
        out = io.StringIO()
        with redirect_stdout(out):
            _print_split_report(report)
        self.assertIn("2 more row(s) in other currencies", out.getvalue())

    def test_packaged_baseline_ignores_cheaper_offers_in_other_currencies(self) -> None:
        query = FlightQuery("JFK", "NRT", DAY)
        seg = (_segment("JFK", "LAX", "08:00", "11:00"), _segment("LAX", "NRT", "13:00", "20:00"))
        packaged = _report(
            [_ok(query, [_offer(90000.0, seg, currency="JPY"), _offer(900.0, seg, currency="USD")])]
        )
        report = search_split_tickets(query, packaged=packaged, search=FakeSearch(_hub_table()))
        self.assertEqual(report.packaged.price, 900.0)
        self.assertEqual(report.packaged.currency, "USD")
        self.assertEqual(report.itineraries[0].savings, 200.0)

    def test_totals_are_rounded_to_the_minor_unit(self) -> None:
        table = _hub_table()
        table[("JFK", "LAX", DAY)] = [
            _offer(0.1, (_segment("JFK", "LAX", "08:00", "11:30"),), airline="A")
        ]
        table[("LAX", "NRT", DAY)] = [
            _offer(0.2, (_segment("LAX", "NRT", "15:00", "23:00"),), airline="B")
        ]
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX", 1.0),
            search=FakeSearch(table),
        )
        self.assertEqual(report.itineraries[0].total, 0.3)
        self.assertEqual(report.itineraries[0].savings, 0.7)


def _hub_search(day, hub, first_arr, second_dep, *, origin="JFK", dest="NRT", **kwargs):
    """One hub split on a fixed date; the first ticket lands on ``day`` at ``first_arr``."""
    table = {
        (origin, hub, day): [
            _offer(
                200.0,
                (_segment(origin, hub, "08:00", first_arr, on=day),),
                airline="A",
            )
        ],
        (hub, dest, day): [
            _offer(500.0, (_segment(hub, dest, second_dep, "23:00", on=day),), airline="B")
        ],
    }
    return search_split_tickets(
        FlightQuery(origin, dest, day),
        packaged=_packaged_via(hub),
        via=[hub],
        search=FakeSearch(table),
        **kwargs,
    )


class ConnectionTimezoneTests(unittest.TestCase):
    def test_dst_ambiguous_arrival_leaves_the_connection_unproven(self) -> None:
        # 01:30 on 2026-10-25 happens twice in London: the real gap is 210 or 270 minutes.
        report = _hub_search(date(2026, 10, 25), "LHR", "01:30", "05:00", min_connection_hours=1)
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.rejected, {"timing_unproven": 1})

    def test_nonexistent_local_departure_leaves_the_connection_unproven(self) -> None:
        # 01:30 on 2026-03-29 never happens in London (clocks jump 01:00 -> 02:00).
        report = _hub_search(date(2026, 3, 29), "LHR", "00:30", "01:30", min_connection_hours=0)
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.rejected, {"timing_unproven": 1})

    def test_gap_is_measured_in_utc_across_a_dst_change(self) -> None:
        # 00:30 BST (23:30Z) to 03:00 GMT (03:00Z) is 210 minutes; the clocks alone say 150.
        report = _hub_search(date(2026, 10, 25), "LHR", "00:30", "03:00")
        (row,) = report.itineraries
        self.assertEqual(row.connection_minutes, 210)
        self.assertEqual(report.rejected, {})

    def test_missing_timezone_leaves_the_connection_unproven(self) -> None:
        with patch("viajante.split.airport_geo", return_value=None):
            report = _hub_search(DAY, "LAX", "11:30", "15:00")
        self.assertEqual(report.itineraries, ())
        self.assertEqual(report.rejected, {"timing_unproven": 1})
        self.assertIsNone(_instant(DAY, "10:00", "ZZZ"))
        with patch("viajante.split.airport_geo", return_value=("Not/AZone", 0.0, 0.0)):
            self.assertIsNone(_instant(DAY, "10:00", "LAX"))

    def test_date_line_and_after_midnight_arrivals_stay_correct(self) -> None:
        day = date(2026, 11, 1)
        # AKL -> HNL lands the same calendar day it left (date line); HNL -> LAX leaves 2h later.
        report = _hub_search(
            day, "HNL", "08:00", "10:00", origin="AKL", dest="LAX", min_connection_hours=2
        )
        (row,) = report.itineraries
        self.assertEqual(row.connection_minutes, 120)
        self.assertFalse(row.overnight_at_hub)
        # Lands HNL at 00:30 the next day; the next ticket leaves at 02:30 that day.
        after = date(2026, 11, 2)
        table = {
            ("AKL", "HNL", day): [
                _offer(200.0, (_segment("AKL", "HNL", "20:00", "00:30", on=day, lands=after),))
            ],
            ("HNL", "LAX", after): [
                _offer(500.0, (_segment("HNL", "LAX", "02:30", "10:00", on=after),))
            ],
        }
        overnight = search_split_tickets(
            FlightQuery("AKL", "LAX", day),
            packaged=_packaged_via("HNL"),
            via=["HNL"],
            allow_overnight=True,
            min_connection_hours=2,
            search=FakeSearch(table),
        )
        (night,) = overnight.itineraries
        self.assertEqual(night.connection_minutes, 120)
        self.assertFalse(night.overnight_at_hub)

    def test_mixed_one_ways_compare_in_utc_and_unknown_timing_stays_unproven(self) -> None:
        day, back = date(2026, 10, 24), date(2026, 10, 25)
        table = {
            ("JFK", "LHR", day): [
                _offer(300.0, (_segment("JFK", "LHR", "20:00", "01:30", on=day, lands=back),))
            ],
            ("LHR", "JFK", back): [
                _offer(250.0, (_segment("LHR", "JFK", "09:00", "12:00", on=back),))
            ],
        }
        report = search_split_tickets(
            RoundTrip("JFK", "LHR", day, back),
            packaged=MixedOneWayTests()._packaged(),
            search=FakeSearch(table),
        )
        (row,) = report.itineraries
        self.assertIs(row.timing_proven, False)
        self.assertIn("not verified", row.to_dict()["timing_note"])

    def test_unknown_timing_never_sorts_above_proven_timing(self) -> None:
        query = FlightQuery("JFK", "NRT", DAY)

        def row(price: float, proven: bool) -> SplitItinerary:
            offer = _offer(price, (_segment("JFK", "NRT", "10:00", "14:00"),), currency="USD")
            part = SplitPart("outbound", query, offer, "USD")
            return SplitItinerary("mixed_one_ways", (part,), timing_proven=proven)

        ranked, _omitted = _rank([row(100.0, False), row(900.0, True)], "USD", 5)
        self.assertEqual([r.total for r in ranked], [900.0, 100.0])


class ViaTests(unittest.TestCase):
    def test_several_named_via_airports_are_all_tried(self) -> None:
        fake = FakeSearch(_hub_table())
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            via=["LAX", "SFO", "ORD", "SEA", "DEN"],
            search=fake,
        )
        self.assertEqual(report.hubs, ("LAX", "SFO", "ORD", "SEA", "DEN"))
        self.assertEqual(report.hubs_source, "user")
        self.assertEqual(report.max_hubs, 5)
        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(report.skipped_hubs, ())
        self.assertEqual(len(report.itineraries), 1)

    def test_via_errors_name_the_field(self) -> None:
        query = FlightQuery("JFK", "NRT", DAY)
        with self.assertRaisesRegex(ValueError, "unknown via IATA code: 'ZZZ'"):
            search_split_tickets(query, packaged=_packaged_via("LAX"), via=["LAX", "ZZZ"])
        with self.assertRaisesRegex(ValueError, "via accepts at most 5 airports \\(got 6\\)"):
            search_split_tickets(
                query,
                packaged=_packaged_via("LAX"),
                via=["LAX", "SFO", "ORD", "SEA", "DEN", "ATL"],
            )

    def test_cli_and_mcp_report_the_same_via_errors(self) -> None:
        for argv in (
            ["flights", ROUTE, "--split-tickets", "--split-via", "LAX,ZZZ"],
            ["flights", ROUTE, "--split-tickets", "--split-via", "LAX,SFO,ORD,SEA,DEN,ATL"],
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(main(argv), 1)
            self.assertIn("via", err.getvalue())
        with self.assertRaisesRegex(ValueError, "unknown via IATA code"):
            search_split_tickets_tool(ROUTE, via="LAX,ZZZ")
        with self.assertRaisesRegex(ValueError, "via accepts at most 5"):
            search_split_tickets_tool(ROUTE, via="LAX,SFO,ORD,SEA,DEN,ATL")

    def test_mismatched_airports_stay_rejected_with_several_via(self) -> None:
        table = _hub_table()
        table[("JFK", "SFO", DAY)] = [
            _offer(100.0, (_segment("JFK", "BUR", "08:00", "11:30"),), airline="C")
        ]
        table[("SFO", "NRT", DAY)] = [
            _offer(100.0, (_segment("SFO", "NRT", "15:00", "23:00"),), airline="D")
        ]
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            via=["SFO", "LAX"],
            search=FakeSearch(table),
        )
        self.assertEqual([row.hub for row in report.itineraries], ["LAX"])
        self.assertEqual(report.rejected, {"airport_mismatch": 1})


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
        row = report.itineraries[0]
        self.assertEqual(row.savings, -30.0)
        comparison = row.to_dict()["vs_packaged"]
        self.assertEqual(comparison["direction"], "costlier")
        self.assertEqual(comparison["extra_cost"], 30.0)
        self.assertNotIn("savings", comparison)

    def test_return_that_leaves_before_the_outbound_lands_is_never_paired(self) -> None:
        back = DAY + timedelta(days=1)
        table = {
            ("JFK", "NRT", DAY): [
                _offer(300.0, (_segment("JFK", "NRT", "10:00", "15:30", lands=back),)),
            ],
            ("NRT", "JFK", back): [
                _offer(100.0, (_segment("NRT", "JFK", "09:00", "08:00", on=back),), airline="E"),
                _offer(400.0, (_segment("NRT", "JFK", "18:00", "17:00", on=back),), airline="L"),
            ],
        }
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, back), packaged=self._packaged(), search=FakeSearch(table)
        )
        self.assertEqual(report.rejected, {"return_before_arrival": 1})
        row = report.itineraries[0]
        self.assertEqual([p.offer.airline for p in row.parts], ["Air", "L"])
        self.assertEqual(row.total, 700.0)
        self.assertIs(row.to_dict()["timing_proven"], True)

        table[("NRT", "JFK", back)] = table[("NRT", "JFK", back)][:1]
        none = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, back), packaged=self._packaged(), search=FakeSearch(table)
        )
        self.assertEqual(none.itineraries, ())
        self.assertEqual(none.rejected, {"return_before_arrival": 1})

    def test_unproven_timing_is_flagged_and_a_proven_pair_wins_over_it(self) -> None:
        bare = RawSegment(
            origin="JFK", destination="NRT", departure="10:00", arrival="14:00", departure_date=DAY
        )
        proven_out = _offer(320.0, (_segment("JFK", "NRT", "11:00", "15:00"),), airline="P")
        table = self._table()
        table[("JFK", "NRT", DAY)] = [_offer(100.0, (bare,), airline="U")]
        only_unproven = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=FakeSearch(table)
        )
        row = only_unproven.itineraries[0]
        self.assertIs(row.timing_proven, False)
        self.assertIs(row.to_dict()["timing_proven"], False)
        self.assertIn("not verified", row.to_dict()["timing_note"])
        self.assertEqual(only_unproven.rejected, {"timing_unproven": 1})
        self.assertEqual(only_unproven.coverage.scope["timing_unproven_kept"], 1)
        self.assertIn("timing_unproven_kept", only_unproven.to_dict()["coverage"]["scope"])
        table[("JFK", "NRT", DAY)].append(proven_out)
        both = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=FakeSearch(table)
        )
        self.assertEqual(both.itineraries[0].parts[0].offer.airline, "P")
        self.assertIs(both.itineraries[0].timing_proven, True)
        self.assertIsNone(both.itineraries[0].to_dict()["timing_note"])
        self.assertEqual(both.rejected, {"timing_unproven": 2})
        self.assertEqual(both.coverage.scope["timing_unproven_kept"], 0)

    def test_mixed_pairs_are_chosen_within_one_currency_never_across(self) -> None:
        def one_way(origin, dest, day, price, currency):
            return _offer(
                price, (_segment(origin, dest, "10:00", "14:00", on=day),), currency=currency
            )

        table = {
            ("JFK", "NRT", DAY): [
                one_way("JFK", "NRT", DAY, 300.0, "USD"),
                one_way("JFK", "NRT", DAY, 150.0, "JPY"),
            ],
            ("NRT", "JFK", BACK): [
                one_way("NRT", "JFK", BACK, 200.0, "USD"),
                one_way("NRT", "JFK", BACK, 100.0, "JPY"),
            ],
        }
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=FakeSearch(table)
        )
        self.assertEqual(
            [(row.currency, row.total) for row in report.itineraries],
            [("USD", 500.0), ("JPY", 250.0)],
        )
        usd, jpy = (row.to_dict() for row in report.itineraries)
        self.assertEqual(usd["vs_packaged"]["savings"], 200.0)
        self.assertNotIn("vs_packaged", jpy)
        only_jpy = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK),
            packaged=self._packaged(currency="EUR"),
            search=FakeSearch(
                {k: [o for o in v if o.evidence.currency == "JPY"] for k, v in table.items()}
            ),
        )
        self.assertEqual([row.currency for row in only_jpy.itineraries], ["JPY"])
        self.assertNotIn("vs_packaged", only_jpy.itineraries[0].to_dict())

    def test_each_direction_in_its_own_currency_leaves_the_total_unknown(self) -> None:
        table = self._table()
        table[("NRT", "JFK", BACK)] = [
            _offer(40000.0, (_segment("NRT", "JFK", "12:00", "11:00", on=BACK),), currency="JPY")
        ]
        report = search_split_tickets(
            RoundTrip("JFK", "NRT", DAY, BACK), packaged=self._packaged(), search=FakeSearch(table)
        )
        (row,) = report.itineraries
        self.assertIsNone(row.total)
        self.assertEqual([p.currency for p in row.parts], ["USD", "JPY"])
        self.assertEqual(row.parts[0].offer.price, 280.0)

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
                via=["LAX"],
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
    def test_split_accepts_the_baggage_buffer(self):
        code, _out, err, _flights, _seen = self._run(
            ["flights", ROUTE, "--split-tickets", "--split-via", "LAX", "--baggage-buffer", "10"]
        )
        self.assertNotIn("does not support", err)
        self.assertNotEqual(code, 1, err)

    def test_split_accepts_the_named_filters_and_applies_them(self):
        flags = (
            ("--arrive-before", "09:00"),
            ("--depart-after", "10:00"),
            ("--depart-window", "08:00-10:00"),
            ("--max-duration", "8"),
            ("--min-layover", "1"),
            ("--max-layover", "4"),
            ("--via", "LAX"),
            ("--exclude-via", "SFO"),
            ("--no-overnight", "LAX"),
            ("--require-overnight", "SFO"),
            ("--exclude-airports", "SFO"),
            ("--include-airports", "NRT"),
        )
        for flag, value in flags:
            with self.subTest(flag=flag):
                code, _out, err, _flights, _seen = self._run(
                    ["flights", ROUTE, "--split-tickets", "--split-via", "LAX", flag, value]
                )
                self.assertNotIn("does not support", err)
                self.assertNotEqual(code, 1, err)

    def test_top_caps_split_output_and_saved_pairings(self):
        table = _hub_table()
        second = table[("LAX", "NRT", DAY)][0]
        table[("LAX", "NRT", DAY)] = [
            replace(second, airline=f"B{i}", price=second.price + i) for i in range(4)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            code, out, _err, _flights, seen = self._run(
                [
                    "flights",
                    ROUTE,
                    "--split-tickets",
                    "--split-via",
                    "LAX",
                    "--top",
                    "1",
                    "--save",
                    str(target),
                ],
                split_search=FakeSearch(table),
            )
            payload = json.loads(target.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(seen["top"], 1)
        self.assertEqual(len(payload["split_tickets"]["itineraries"]), 1)
        self.assertNotIn("\n  2.", out)

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
        code, _out, err, flights, _seen = self._run(["flights", ROUTE, "--split-via", "LAX"])
        self.assertEqual(code, 1)
        self.assertIn("--split-tickets", err)
        flights.assert_not_called()

    def test_split_needs_exactly_one_one_way_or_round_trip(self) -> None:
        for argv in (
            ["flights", ROUTE, f"LAX-JFK:{BACK.isoformat()}", "--split-tickets"],
            ["flights", "--trip", "rt", RT_ROUTE, "--split-tickets", "--split-via", "LAX"],
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

    def test_a_split_that_stopped_on_the_cooldown_exits_non_zero(self) -> None:
        state = {"at": 1.0, "until": 4_102_444_800.0, "cooldown_s": 120.0}
        with patch("viajante.split.rate_limit_status", return_value=state):
            code, out, _err, _flights, _seen = self._run(["flights", ROUTE, "--split-tickets"])
        self.assertEqual(code, 3)
        self.assertIn("ERROR: Not sent", out)

    def test_zero_hubs_is_rejected_not_replaced_by_the_default(self) -> None:
        code, _out, err, flights, _seen = self._run(
            ["flights", ROUTE, "--split-tickets", "--split-max-hubs", "0"]
        )
        self.assertEqual(code, 1)
        self.assertIn("max_hubs", err)
        flights.assert_not_called()


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

    def test_named_filters_reach_the_split_search_over_mcp(self) -> None:
        payload, _split = self._call(
            ROUTE, FakeSearch(_hub_table()), via="LAX", depart_after="09:00"
        )
        self.assertEqual(payload["itineraries"], [])
        self.assertGreaterEqual(payload["rejected"].get("filter", 0), 1)
        payload, _split = self._call(
            ROUTE, FakeSearch(_hub_table()), via="LAX", depart_after="07:00"
        )
        self.assertEqual(len(payload["itineraries"]), 1)

    def test_a_recorded_cooldown_is_a_rate_limited_envelope_with_its_retry_fields(self) -> None:
        now = time.time()
        state = {"at": now, "until": now + 300, "cooldown_s": 300.0}
        fake = FakeSearch(_hub_table())
        with patch("viajante.split.rate_limit_status", return_value=state):
            payload, _split = self._call(ROUTE, fake, via="LAX")
        self.assertEqual(fake.calls, [])
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("rate_limited", "blocked", "not_loaded"),
        )
        self.assertEqual(payload["error"]["retry_after"], payload["retry_after"])
        self.assertEqual(payload["error"]["retry_after_seconds"], payload["retry_after_seconds"])
        self.assertIn(payload["retry_after_seconds"], (300, 301))
        self.assertEqual((payload["observed_at"], payload["observed_at_basis"]), (None, None))

    def test_a_failed_second_ticket_leg_is_a_partial_ok_or_the_failure(self) -> None:
        table = _hub_table()
        table[("LAX", "NRT", DAY)] = QueryFailure(
            query=FlightQuery("LAX", "NRT", DAY),
            error=SearchError(SearchErrorCode.FETCH_FAILED, "boom", timeout=True),
        )
        payload, _split = self._call(ROUTE, FakeSearch(table), via="LAX")
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["error_code"]),
            ("timeout", "partial", "fetch_failed"),
        )

    def test_every_pairing_rejected_is_filtered_out_and_empty_legs_are_provider_empty(self) -> None:
        payload, _split = self._call(ROUTE, FakeSearch(_hub_table("12:00")), via="LAX")
        self.assertEqual(payload["itineraries"], [])
        self.assertEqual(
            (payload["status"], payload["empty_reason"]), ("no_results", "filtered_out")
        )
        mcp_handlers._CACHE.clear()
        payload, _split = self._call(ROUTE, FakeSearch({}), via="LAX")
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("no_results", "provider_empty", "no_results"),
        )

    def _call_unseeded(self, fake: FakeSearch, **kwargs):
        """The handler path with no injected packaged report: the fake answers that query too."""
        real = search_split_tickets
        with patch(
            "viajante.mcp_handlers.search_split_tickets",
            side_effect=lambda query, **kw: real(query, search=fake, **kw),
        ):
            return search_split_tickets_tool(ROUTE, via="LAX", **kwargs)

    def _packaged_row(self) -> dict:
        return {("JFK", "NRT", DAY): _packaged_via("LAX").queries[0].offers}

    def test_the_packaged_fare_does_not_make_empty_legs_filtered_out(self) -> None:
        payload = self._call_unseeded(FakeSearch(self._packaged_row()))
        self.assertEqual(payload["itineraries"], [])
        self.assertEqual(
            (payload["status"], payload["empty_reason"], payload["error_code"]),
            ("no_results", "provider_empty", "no_results"),
        )
        self.assertEqual(payload["completeness"], "complete")
        self.assertEqual(payload["observed_at"], payload["searched_at"])

    def test_one_answered_leg_and_one_empty_leg_is_filtered_out(self) -> None:
        table = self._packaged_row()
        table[("JFK", "LAX", DAY)] = _hub_table()[("JFK", "LAX", DAY)]
        payload = self._call_unseeded(FakeSearch(table))
        self.assertEqual(
            (payload["status"], payload["empty_reason"]), ("no_results", "filtered_out")
        )

    def test_a_cooldown_only_search_observed_nothing(self) -> None:
        now = time.time()
        state = {"at": now, "until": now + 300, "cooldown_s": 300.0}
        cooled = {("JFK", "NRT", DAY): _rate_limited_failure(None)}
        fake = FakeSearch(cooled)
        with patch("viajante.split.rate_limit_status", return_value=state):
            payload = self._call_unseeded(fake)
        self.assertEqual(
            (payload["status"], payload["completeness"], payload["empty_reason"]),
            ("rate_limited", "blocked", "not_loaded"),
        )
        self.assertEqual((payload["observed_at"], payload["observed_at_basis"]), (None, None))
        self.assertEqual(fake.calls, [[("JFK", "NRT", DAY)]])

    def test_the_extra_search_cap_counts_only_named_via_airports_that_can_be_tried(self) -> None:
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            via=["LAX"],
            max_hubs=5,
            search=FakeSearch(_hub_table()),
        )
        self.assertEqual(report.max_extra_searches, 2)
        report = search_split_tickets(
            FlightQuery("JFK", "NRT", DAY),
            packaged=_packaged_via("LAX"),
            via=["LAX", "JFK", "NRT"],
            allow_overnight=True,
            search=FakeSearch(_hub_table()),
        )
        self.assertEqual(report.max_extra_searches, 3)

    def test_hub_split_over_the_tool_and_ledger_owns_the_savings(self) -> None:
        payload, split = self._call(ROUTE, FakeSearch(_hub_table()), via="lax", max_hubs=2)
        self.assertEqual(split.call_args.kwargs["via"], ("LAX",))
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

    def test_a_costlier_split_is_verifiable_as_a_positive_extra_cost(self) -> None:
        fake = FakeSearch(MixedOneWayTests()._table())
        real = search_split_tickets
        packaged = MixedOneWayTests()._packaged(price=500.0)
        with patch(
            "viajante.mcp_handlers.search_split_tickets",
            side_effect=lambda query, **kw: real(query, packaged=packaged, search=fake, **kw),
        ):
            payload = search_split_tickets_tool(RT_ROUTE, trip="rt")
        comparison = payload["itineraries"][0]["vs_packaged"]
        self.assertEqual(comparison["extra_cost"], 30.0)
        self.assertTrue(verify_answer("The split costs 30 USD more than the round-trip.")["ok"])
        self.assertFalse(verify_answer("The split costs 35 USD more.")["ok"])

    def test_busy_lock_and_bad_input_fail_before_any_search(self) -> None:
        fake = FakeSearch(_hub_table())
        with patch.object(mcp_handlers, "search_split_tickets") as split:
            past = (date.today() - timedelta(days=1)).isoformat()
            with self.assertRaisesRegex(ValueError, "past"):
                search_split_tickets_tool(f"JFK-NRT:{past}")
            with self.assertRaisesRegex(ValueError, "one-way"):
                search_split_tickets_tool(RT_ROUTE, trip="rt", via="LAX")
            with self.assertRaisesRegex(ValueError, "max_hubs"):
                search_split_tickets_tool(ROUTE, max_hubs=99)
            with mcp_handlers._SEARCH_LOCK:
                with self.assertRaisesRegex(ValueError, "already running"):
                    search_split_tickets_tool(ROUTE)
            split.assert_not_called()
        self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
