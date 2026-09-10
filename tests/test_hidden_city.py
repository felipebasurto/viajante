from __future__ import annotations

import io
import json
import unittest
from datetime import date, datetime, timedelta
from inspect import getsource
from unittest.mock import patch

from viajante.cli import main
from viajante.models import (
    HIDDEN_CITY_WARNINGS,
    HiddenCityOffer,
    HiddenCityReport,
    SearchErrorCode,
)
from viajante.skiplagged import parse_skiplagged_offers, search_hidden_city

FUTURE = date.today() + timedelta(days=40)


class SkiplaggedParseTests(unittest.TestCase):
    def test_skips_rows_without_a_price(self) -> None:
        offers = parse_skiplagged_offers(
            {
                "structuredContent": {
                    "flights": [
                        {"airline": "Delta", "origin": "JFK", "destination": "LHR"},
                        {
                            "price": 412,
                            "currency": "USD",
                            "airline": "JetBlue",
                            "origin": "JFK",
                            "destination": "LHR",
                            "hidden_city": False,
                            "url": "https://skiplagged.com/example",
                        },
                    ]
                }
            },
            origin="JFK",
            destination="LHR",
            departure_date=FUTURE,
            currency="USD",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, 412)
        self.assertEqual(offers[0].evidence, "confirmed")
        self.assertFalse(offers[0].hidden_city)
        self.assertEqual(offers[0].booking_url, "https://skiplagged.com/example")

    def test_hidden_city_from_ticketed_destination(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 89,
                    "currency": "USD",
                    "origin": "JFK",
                    "destination": "BOS",
                    "ticketed_destination": "LHR",
                    "layover_city": "BOS",
                }
            ],
            origin="JFK",
            destination="BOS",
            departure_date=FUTURE,
            currency="USD",
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)
        self.assertEqual(offers[0].warnings, HIDDEN_CITY_WARNINGS)

    def test_json_text_content_payload(self) -> None:
        offers = parse_skiplagged_offers(
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "itineraries": [
                                    {
                                        "fare": 199.0,
                                        "curr": "USD",
                                        "airlineName": "United",
                                        "hiddenCity": True,
                                    }
                                ]
                            }
                        ),
                    }
                ]
            },
            origin="EWR",
            destination="ORD",
            departure_date=FUTURE,
            currency="USD",
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)

    def test_flight_card_nested_price_and_hidden_attribute(self) -> None:
        offers = parse_skiplagged_offers(
            {
                "structuredContent": {
                    "flights": [
                        {
                            "type": "FlightCard",
                            "airlines": "American Airlines",
                            "departure": {
                                "airport": "JFK",
                                "dateTime": "2026-10-20T05:55:00-04:00",
                            },
                            "arrival": {"airport": "MIA", "dateTime": "2026-10-20T09:00:00-04:00"},
                            "duration": "3h 5m",
                            "layovers": 0,
                            "price": {"amount": 154, "currency": "USD"},
                            "deepLink": (
                                "https://skiplagged.com/flights/JFK/MIA/2026-10-20#trip=AA2161~"
                            ),
                            "attributes": ["hidden-city", "nonstop"],
                        }
                    ]
                }
            },
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
            currency="USD",
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, 154)
        self.assertEqual(offers[0].airline, "American Airlines")
        self.assertTrue(offers[0].hidden_city)
        self.assertEqual(offers[0].stops_count, 0)
        self.assertIn("skiplagged.com", offers[0].booking_url or "")


class HiddenCitySearchTests(unittest.TestCase):
    def test_rpc_failure_does_not_invent_fares(self) -> None:
        def boom(_url: str, _payload: dict, _headers: dict) -> tuple[int, dict, str]:
            raise OSError("offline")

        report = search_hidden_city("JFK", "LHR", FUTURE, rpc=boom)
        self.assertEqual(report.offers, ())
        self.assertIsNotNone(report.error)
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.FETCH_FAILED)
        self.assertEqual(report.source, "skiplagged")
        self.assertEqual(report.warnings, HIDDEN_CITY_WARNINGS)

    def test_empty_priced_rows_are_no_results(self) -> None:
        def rpc(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
            method = payload.get("method")
            if method == "initialize":
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
                return 200, {"mcp-session-id": "s1"}, body
            empty = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"flights": []}}}
            return 200, {}, json.dumps(empty)

        report = search_hidden_city("NRT", "SIN", FUTURE, rpc=rpc)
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.NO_RESULTS)

    def test_success_parses_offers(self) -> None:
        def rpc(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
            if payload.get("method") == "initialize":
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
                return 200, {"mcp-session-id": "s1"}, body
            return (
                200,
                {},
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "structuredContent": {
                                "flights": [
                                    {
                                        "price": 350,
                                        "currency": "USD",
                                        "airline": "Delta",
                                        "hidden_city": False,
                                    }
                                ]
                            }
                        },
                    }
                ),
            )

        report = search_hidden_city("JFK", "LHR", FUTURE, rpc=rpc)
        self.assertEqual(len(report.offers), 1)
        self.assertIsNone(report.error)
        self.assertEqual(report.offers[0].price, 350)

    def test_past_departure_is_rejected_before_rpc(self) -> None:
        past = date.today() - timedelta(days=1)
        with self.assertRaises(ValueError):
            search_hidden_city("JFK", "LHR", past, rpc=lambda *_: (200, {}, "{}"))

    def test_search_does_not_import_google_flights(self) -> None:
        import viajante.skiplagged as module

        self.assertNotIn("google_flights", module.__name__)
        source = getsource(module)
        self.assertNotIn("viajante.google_flights", source)
        self.assertNotIn("viajante.flights", source)


class HiddenCityCliTests(unittest.TestCase):
    def test_hidden_city_help_lists_mixed_iata(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["hidden-city", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("JFK-LHR", help_text)
        self.assertIn("NRT-SIN", help_text)
        self.assertIn("GRU-EZE", help_text)

    def test_cli_prints_mocked_offers(self) -> None:
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="LHR",
            departure_date=FUTURE,
            currency="USD",
            offers=(
                HiddenCityOffer(
                    origin="JFK",
                    destination="LHR",
                    departure_date=FUTURE,
                    price=350,
                    currency="USD",
                    evidence="confirmed",
                    airline="Delta",
                ),
            ),
        )
        buffer = io.StringIO()
        err = io.StringIO()
        with (
            patch("viajante.cli.search_hidden_city", return_value=report),
            patch("sys.stdout", buffer),
            patch("sys.stderr", err),
        ):
            code = main(["hidden-city", f"JFK-LHR:{FUTURE.isoformat()}"])
        self.assertEqual(code, 0)
        self.assertIn("350", buffer.getvalue())
        self.assertIn("Hidden-city", err.getvalue())


if __name__ == "__main__":
    unittest.main()
