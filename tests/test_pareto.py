from __future__ import annotations

import io
import random
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from viajante.cli import _build_parser, main
from viajante.flights import _rank_offers, _run_search, get_flights, search_flights
from viajante.google_flights import RawFlightCard
from viajante.models import (
    FlightOffer,
    FlightQuery,
    QuerySuccess,
    RawJourneyLeg,
    RoundTrip,
    SearchReport,
)
from viajante.selection import dimensions, pareto_select

QUERY = FlightQuery("JFK", "LHR", date(2099, 7, 1), max_stops=2)


def offer(price, hours, stops, *, bags=0, carry=1, name=None):
    return FlightOffer(
        name or str(price),
        "08:00",
        "10:00",
        f"${price}",
        price,
        None,
        hours,
        None,
        stops,
        0,
        False,
        checked_bags=bags,
        carry_on=carry,
    )


def select(offers, top=8, sort="ranked", trip=QUERY):
    return pareto_select(
        offers,
        trip,
        top=top,
        order=lambda rows: _rank_offers(rows, top=max(1, len(rows)), sort=sort, preserve=True),
    )


class ParetoTests(unittest.TestCase):
    def test_extremes_reserve_budget_in_order(self):
        cheap, fast, direct = offer(100, 12, 2), offer(180, 3, 1), offer(200, 5, 0)
        dominated = offer(250, 13, 2)
        for top, expected in [(1, (cheap,)), (2, (cheap, fast)), (4, (cheap, fast, direct))]:
            rows, metadata = select([dominated, cheap, fast, direct], top)
            self.assertEqual(rows, expected)
            self.assertEqual(metadata["frontier_count"], 3)
            self.assertEqual(metadata["truncated"], top < 3)

    def test_equal_dimensions_do_not_dominate(self):
        left, right = offer(100, 4, 0, name="A"), offer(100, 4, 0, name="B")
        self.assertEqual(len(select([left, right])[0]), 2)

    def test_baggage_unknown_or_different_cannot_be_dominated(self):
        best = offer(100, 4, 0)
        for other in [
            offer(200, 6, 1, bags=1),
            offer(200, 6, 1, bags=None),
            offer(200, 6, 1, carry=None),
        ]:
            self.assertEqual(len(select([best, other])[0]), 2)
        self.assertEqual(select([best, offer(200, 6, 1)])[0], (best,))

    def test_incomplete_dimensions_follow_frontier(self):
        unknown = offer(10, None, None)
        complete = offer(100, 4, 0)
        rows, meta = select([unknown, complete])
        self.assertEqual(rows, (complete, unknown))
        self.assertEqual(meta["incomplete_count"], 1)

    def test_package_dimensions_require_every_leg(self):
        trip = RoundTrip("JFK", "LHR", date(2099, 7, 1), date(2099, 7, 5))
        partial = replace(
            offer(100, 4, 0), legs=(RawJourneyLeg("08:00", "12:00", "4 hr", "Nonstop"),)
        )
        self.assertIsNone(dimensions(partial, trip))
        full = replace(
            partial, legs=partial.legs + (RawJourneyLeg("08:00", "14:00", "6 hr", "1 stop"),)
        )
        self.assertEqual(dimensions(full, trip), (100, 10, 1))
        self.assertEqual(select([partial, full], trip=trip)[0], (full, partial))

    def test_slow_heuristic_only_applies_to_default_selection(self):
        class Source:
            config = SimpleNamespace(html_lang="en", currency="USD")

            def fetch(self, trip):
                return [
                    RawFlightCard("Cheap", "08:00", "23:00", "15 hr", "1 stop", "$100"),
                    RawFlightCard("Fast", "08:00", "10:00", "2 hr", "Nonstop", "$200"),
                ]

            def close(self):
                pass

        kwargs = dict(
            source=Source(),
            top=8,
            sleep=lambda _: None,
            random_gen=random.Random(0),
            now=lambda: datetime(2099, 1, 1),
            currency="USD",
        )
        default = _run_search([QUERY], **kwargs)
        diverse = _run_search([QUERY], selection="pareto", **kwargs)
        self.assertEqual(len(default.queries[0].offers), 1)
        self.assertEqual(len(diverse.queries[0].offers), 2)
        self.assertIn("selection", diverse.to_dict()["queries"][0])
        self.assertNotIn("selection", default.to_dict()["queries"][0])

    def test_pareto_cli_forwards_selection(self):
        with (
            patch(
                "viajante.cli.search_flights",
                return_value=SearchReport(
                    datetime(2099, 1, 1),
                    (QuerySuccess(QUERY, 1, 1, (offer(100, 7, 0),)),),
                    currency="USD",
                ),
            ) as search,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["flights", "JFK-LHR:2099-07-01", "--selection", "pareto"]), 0)
        self.assertEqual(search.call_args.kwargs["selection"], "pareto")

    def test_cli_and_library_propagation_and_invalid_input(self):
        self.assertEqual(
            _build_parser().parse_args(["flights", "JFK-LHR:2099-07-01"]).selection, "top"
        )
        self.assertEqual(
            _build_parser()
            .parse_args(["flights", "JFK-LHR:2099-07-01", "--selection", "pareto"])
            .selection,
            "pareto",
        )
        with patch("viajante.flights.search_flights") as search:
            get_flights(QUERY, selection="pareto")
            self.assertEqual(search.call_args.kwargs["selection"], "pareto")
        with patch("viajante.flights.GoogleFlightsHttpSource") as source:
            with self.assertRaises(ValueError):
                search_flights([QUERY], selection="invented")
            source.assert_not_called()
