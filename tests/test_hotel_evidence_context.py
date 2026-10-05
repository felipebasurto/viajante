"""Synthetic regressions for navigable links and evidence-bound stay shortlists."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from test_google_hotels import _hotel_record, _search_payload, _wrap_wrb
from test_hotels import FakeSource, _located, _two_stays, card, offer, query
from viajante.cli import main
from viajante.google_flights import SweepHttpResponse
from viajante.google_hotels import GoogleHotelsSource, build_applied_filters
from viajante.google_hotels_rpc import parse_hotels_body
from viajante.hotels import _is_eligible, _normalize_card, _raw_distance_km, search_hotels
from viajante.mcp_handlers import search_hotels_tool
from viajante.models import CancellationEvidence, HotelPage, LodgingKind, PropertyTypeEvidence
from viajante.runtime import get_runtime_info
from viajante.stays import plan_stay_blocks, split_stay_costs


class HotelLinkContextTests(unittest.TestCase):
    def test_click_tracker_is_never_a_navigation_fallback(self) -> None:
        for entity in (None, "", "Chbad/path", "https://other.example", "Chbad?x=1"):
            record = _hotel_record()
            record[20] = entity
            raw = parse_hotels_body(_wrap_wrb(_search_payload(record)))[0]
            self.assertIsNone(raw.link)
            self.assertEqual(raw.link_context, "none")

    def test_owned_entity_variants_are_property_navigation(self) -> None:
        for entity in ("ChSynthetic", "CgSynthetic", "CiSynthetic"):
            record = _hotel_record()
            record[20] = entity
            raw = parse_hotels_body(_wrap_wrb(_search_payload(record)))[0]
            self.assertEqual(raw.link, f"https://www.google.com/travel/hotels/entity/{entity}")
            self.assertEqual(raw.link_context, "property")

    def test_navigation_matches_provider_validated_nondefault_context(self) -> None:
        # Synthetic dates/place; wire encoding verified against Google's HTML
        # check-in/out inputs, adult control and AtySUc occupancy/rooms echo.
        q = query(
            location="Madrid",
            check_in=date(2026, 12, 14),
            check_out=date(2026, 12, 17),
            adults=3,
            rooms=2,
            free_cancellation=False,
        )
        applied = build_applied_filters(q, currency="EUR")
        params = parse_qs(urlparse(applied.url).query)
        self.assertEqual(
            params["ts"],
            [
                "CAESDgoCCAMKAggDCgIIAxACGiAKAhoAEhoSFAoHCOoPEAwYDhIHCOoPEAwYERgDMgIQACoJCgU6A0VVUhoA"
            ],
        )
        self.assertEqual(applied.url_context, "stay")
        self.assertEqual(params["curr"], ["EUR"])
        self.assertEqual(params["hl"], ["en"])
        variants = (
            replace(q, adults=1),
            replace(q, rooms=4),
            replace(q, check_out=date(2026, 12, 18)),
            replace(q, entire_home=True),
        )
        for variant in variants:
            self.assertNotEqual(build_applied_filters(variant, currency="EUR").url, applied.url)
        source = GoogleHotelsSource(currency="EUR", client=object())
        response = SweepHttpResponse(
            200, _wrap_wrb(_search_payload(_hotel_record())), "https://google.com"
        )
        with patch("viajante.google_hotels.dispatch_posts", return_value=[response]):
            page = source.fetch(q, applied, 10)
        self.assertEqual(page.cards[0].link_context, "stay")
        self.assertEqual(parse_qs(urlparse(page.cards[0].link).query), params)


class HotelEvidenceRegressions(unittest.TestCase):
    def test_google_property_description_cannot_prove_unit_or_policy(self) -> None:
        record = _hotel_record(title="Synthetic Apartment Hostel")
        record[11] = ["Private rooms and dorms. Free cancellation. Sleeps 10. 4 beds."]
        raw = parse_hotels_body(_wrap_wrb(_search_payload(record)))[0]
        normalized = _normalize_card(raw)
        self.assertIsNotNone(normalized)
        self.assertIn("Private rooms", normalized.details)
        self.assertEqual(normalized.lodging_kind, LodgingKind.UNKNOWN)
        self.assertEqual(normalized.cancellation_evidence, CancellationEvidence.UNKNOWN)
        self.assertEqual(normalized.property_type_evidence, PropertyTypeEvidence.UNKNOWN)
        self.assertIsNone(normalized.sleeps)
        self.assertIsNone(normalized.beds)

    def test_explicit_google_unit_chips_still_prove_capacity(self) -> None:
        for kind in ("Entire cottage", "Entire villa"):
            raw = replace(
                card(details="Property description"), unit_details=f"{kind}. Sleeps 3. 2 beds"
            )
            normalized = _normalize_card(raw)
            self.assertEqual(normalized.lodging_kind, LodgingKind.ENTIRE_HOME)
            self.assertEqual((normalized.sleeps, normalized.beds), (3, 2))

    def test_known_party_and_single_unit_capacity_contradictions_are_excluded(self) -> None:
        q = query(adults=4)
        self.assertFalse(_is_eligible(replace(offer(), priced_adults=2), q))
        self.assertTrue(_is_eligible(replace(offer(), priced_adults=None), q))
        self.assertFalse(_is_eligible(replace(offer(), sleeps=3), q))
        self.assertTrue(_is_eligible(replace(offer(), sleeps=4), q))
        # A per-unit capacity does not establish the total for multiple rooms.
        self.assertTrue(_is_eligible(replace(offer(), sleeps=3), replace(q, rooms=2)))


class HotelRadiusRegressions(unittest.TestCase):
    def test_named_radius_and_point_reach_cli_and_mcp_search(self) -> None:
        start = date.today() + timedelta(days=30)
        end = start + timedelta(days=2)
        report = MagicMock()
        report.queries = ()
        report.to_dict.return_value = {"queries": []}
        with (
            patch("viajante.cli.search_hotels", return_value=report) as search,
            patch("viajante.cli._print_hotel_report"),
        ):
            self.assertEqual(
                main(
                    [
                        "hotels",
                        "Synthetic CLI place",
                        start.isoformat(),
                        end.isoformat(),
                        "--currency",
                        "EUR",
                        "--near",
                        "0,0",
                        "--max-distance-km",
                        "4",
                    ]
                ),
                0,
            )
        self.assertEqual(search.call_args.kwargs["near"], (0, 0))
        self.assertEqual(search.call_args.kwargs["max_distance_km"], 4)
        with patch("viajante.mcp_handlers.search_hotels", return_value=report) as search:
            search_hotels_tool(
                "Synthetic MCP place",
                start.isoformat(),
                end.isoformat(),
                currency="EUR",
                near={"lat": 0, "lng": 0},
                max_distance_km=4,
            )
        self.assertEqual(search.call_args.kwargs["near"], (0, 0))
        self.assertEqual(search.call_args.kwargs["max_distance_km"], 4)

    def test_radius_filters_before_price_cut_and_excludes_unknown_coordinates(self) -> None:
        cards = (
            _located("Far cheap", "1 €", 0.1, 0),
            _located("Blind cheap", "2 €", None, None),
            _located("Inside", "30 €", 0.01, 0),
            _located("Outside rounding edge", "3 €", 0.00903, 0),
        )
        exact = _raw_distance_km((0, 0), 0.00903, 0)
        self.assertGreater(exact, 1)
        self.assertEqual(round(exact, 2), 1)
        bounded = _two_stays(cards, cards, near=(0, 0), max_distance_km=2)
        self.assertEqual(
            [o.title for o in bounded.queries[0].offers], ["Outside rounding edge", "Inside"]
        )
        edge = _two_stays(cards, cards, near=(0, 0), max_distance_km=1)
        self.assertEqual(edge.queries[0].offers, ())
        source = FakeSource([])
        with patch("viajante.hotels.GoogleHotelsSource", return_value=source):
            source.responses = [HotelPage(cards=cards)]
            top = search_hotels(
                (query(),), source="google", currency="EUR", top=1, near=(0, 0), max_distance_km=2
            )
        self.assertEqual(top.queries[0].offers[0].title, "Outside rounding edge")
        unbounded = _two_stays(cards, cards, near=(0, 0))
        self.assertEqual(unbounded.queries[0].offers[0].title, "Far cheap")
        self.assertEqual(bounded.to_dict()["max_distance_km"], 2)

    def test_invalid_radius_is_rejected_before_source_cli_and_mcp_calls(self) -> None:
        for radius in (0, -1, float("nan"), float("inf"), True):
            with patch("viajante.hotels.GoogleHotelsSource") as source:
                with self.assertRaises(ValueError):
                    search_hotels(
                        (query(),),
                        source="google",
                        currency="EUR",
                        near=(0, 0),
                        max_distance_km=radius,
                    )
                source.assert_not_called()
        with patch("viajante.hotels.GoogleHotelsSource") as source:
            with self.assertRaises(ValueError):
                search_hotels((query(),), source="google", currency="EUR", max_distance_km=2)
            source.assert_not_called()
        start = date.today() + timedelta(days=30)
        end = start + timedelta(days=2)
        with patch("viajante.cli.search_hotels") as search, redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "hotels",
                        "Example",
                        start.isoformat(),
                        end.isoformat(),
                        "--currency",
                        "EUR",
                        "--max-distance-km",
                        "2",
                    ]
                ),
                1,
            )
            search.assert_not_called()
        with patch("viajante.mcp_handlers.search_hotels") as search:
            with self.assertRaises(ValueError):
                search_hotels_tool(
                    "Example", start.isoformat(), end.isoformat(), currency="EUR", max_distance_km=2
                )
            search.assert_not_called()


class RuntimeAndRosterRegressions(unittest.TestCase):
    def test_cli_and_diagnostic_report_executing_version(self) -> None:
        with patch("viajante.runtime.package_version", return_value="1.4.0"):
            runtime = get_runtime_info()
        self.assertEqual(runtime["viajante_version"], "1.4.0")
        self.assertEqual(runtime["hotel_schema_version"], 2)
        self.assertTrue(runtime["python_version"])
        out = io.StringIO()
        with (
            patch("viajante.cli._PARSER", None),
            patch("viajante.cli.package_version", return_value="1.4.0"),
            redirect_stdout(out),
        ):
            self.assertEqual(main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), "viajante 1.4.0")

    def test_changed_departure_preserves_people_and_uncovered_nights(self) -> None:
        roster = {
            "2026-12-03": ["A", "B", "C", "D", "E"],
            "2026-12-04": ["A", "B", "C", "D", "E", "F"],
            "2026-12-05": ["A", "B", "C", "D", "E", "F", "G"],
            "2026-12-06": ["A", "F", "G"],
            "2026-12-07": ["A", "F", "G"],
            "2026-12-08": ["A", "F"],
        }
        blocks = plan_stay_blocks(roster)
        self.assertEqual(blocks.person_nights, 26)
        self.assertEqual([b.headcount for b in blocks.blocks], [5, 6, 7, 3, 2])
        split = split_stay_costs(
            [
                {
                    "name": "Synthetic stay",
                    "check_in": "2026-12-03",
                    "check_out": "2026-12-06",
                    "total": 180,
                }
            ],
            roster,
            currency="EUR",
        )
        self.assertTrue(split.to_dict()["unallocated_nights"])
