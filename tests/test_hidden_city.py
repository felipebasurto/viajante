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

    def test_missing_card_currency_is_skipped(self) -> None:
        offers = parse_skiplagged_offers(
            [{"price": 89, "origin": "LHR", "destination": "JFK"}],
            origin="LHR",
            destination="JFK",
            departure_date=FUTURE,
            currency="GBP",
        )
        self.assertEqual(offers, ())

    def test_dollar_glyph_is_not_origin_cash(self) -> None:
        offers = parse_skiplagged_offers(
            [{"price": "$412", "airline": "Delta"}],
            origin="LHR",
            destination="JFK",
            departure_date=FUTURE,
        )
        self.assertEqual(offers, ())

    def test_brand_token_is_not_hidden_city(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 350,
                    "currency": "USD",
                    "attributes": ["skiplagged", "nonstop"],
                    "skiplagged": True,
                }
            ],
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertFalse(offers[0].hidden_city)

    def test_trip_hash_tilde_is_hidden_city(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 234,
                    "currency": "USD",
                    "airline": "American Airlines",
                    "url": "https://skiplagged.com/flights/JFK/MIA/2026-11-15#trip=AA475~",
                }
            ],
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)
        self.assertIsNone(offers[0].ticketed_destination)
        self.assertIsNone(offers[0].layover_city)

    def test_round_trip_tilde_overrides_false_flag(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 480,
                    "currency": "USD",
                    "airline": "Swiss",
                    "hidden_city": False,
                    "url": (
                        "https://skiplagged.com/flights/LHR/JFK/2026-11-18/2026-11-25"
                        "#trip=LX339-LX16,BA116~"
                    ),
                }
            ],
            origin="LHR",
            destination="JFK",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)

    def test_nested_legs_stamp_ticketed_and_layover(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 89,
                    "currency": "USD",
                    "origin": "JFK",
                    "destination": "BOS",
                    "legs": [
                        {"origin": "JFK", "destination": "BOS"},
                        {"origin": "BOS", "destination": "LHR"},
                    ],
                }
            ],
            origin="JFK",
            destination="BOS",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)
        self.assertEqual(offers[0].ticketed_destination, "LHR")
        self.assertEqual(offers[0].layover_city, "BOS")

    def test_hidden_city_airport_key(self) -> None:
        offers = parse_skiplagged_offers(
            [
                {
                    "price": 120,
                    "currency": "USD",
                    "destination": "MIA",
                    "hiddenCityAirport": "PTY",
                }
            ],
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertTrue(offers[0].hidden_city)
        self.assertEqual(offers[0].ticketed_destination, "PTY")
        self.assertEqual(offers[0].layover_city, "MIA")

    def test_one_bad_row_does_not_drop_priced_neighbors(self) -> None:
        class Boom(dict):
            def get(self, key, default=None):  # noqa: ANN001
                if key == "airline":
                    raise TypeError("boom")
                return super().get(key, default)

        offers = parse_skiplagged_offers(
            [
                Boom({"price": 80, "currency": "USD"}),
                {"price": 120, "currency": "USD", "airline": "Delta"},
            ],
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
        )
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, 120)


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
        self.assertIsNone(report.currency)
        self.assertNotIn("currency", report.to_dict())

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
        self.assertIsNone(report.currency)

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
        self.assertEqual(report.currency, "USD")

    def test_does_not_stamp_origin_cash_on_skiplagged_amounts(self) -> None:
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
                                        "price": {"amount": 154, "currency": "USD"},
                                        "airline": "ANA",
                                    }
                                ]
                            }
                        },
                    }
                ),
            )

        report = search_hidden_city("NRT", "SIN", FUTURE, rpc=rpc)
        self.assertEqual(len(report.offers), 1)
        self.assertEqual(report.offers[0].currency, "USD")
        self.assertEqual(report.currency, "USD")
        self.assertNotEqual(report.currency, "JPY")

    def test_top_is_a_fare_cut(self) -> None:
        captured: list[dict] = []

        def rpc(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
            if payload.get("method") == "initialize":
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
                return 200, {"mcp-session-id": "s1"}, body
            if payload.get("method") == "tools/call":
                captured.append(payload["params"]["arguments"])
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
                                    {"price": 500, "currency": "USD"},
                                    {"price": 80, "currency": "USD"},
                                    {"price": 120, "currency": "USD"},
                                ]
                            }
                        },
                    }
                ),
            )

        report = search_hidden_city("JFK", "MIA", FUTURE, top=2, rpc=rpc)
        self.assertEqual([offer.price for offer in report.offers], [80, 120])
        self.assertEqual(captured[0]["limit"], 2)
        self.assertEqual(captured[0]["sort"], "price")

    def test_reuses_mcp_session(self) -> None:
        methods: list[str] = []

        def rpc(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
            methods.append(str(payload.get("method")))
            if payload.get("method") == "initialize":
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
                return 200, {"mcp-session-id": "s1"}, body
            if payload.get("method") == "notifications/initialized":
                return 202, {}, ""
            return (
                200,
                {},
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "structuredContent": {"flights": [{"price": 90, "currency": "USD"}]}
                        },
                    }
                ),
            )

        search_hidden_city("JFK", "MIA", FUTURE, rpc=rpc)
        search_hidden_city("JFK", "MIA", FUTURE, rpc=rpc)
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods.count("notifications/initialized"), 1)
        self.assertEqual(methods.count("tools/call"), 2)

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

    def test_cli_prints_ticketed_and_layover(self) -> None:
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="BOS",
            departure_date=FUTURE,
            currency="USD",
            offers=(
                HiddenCityOffer(
                    origin="JFK",
                    destination="BOS",
                    departure_date=FUTURE,
                    price=89,
                    currency="USD",
                    evidence="confirmed",
                    airline="JetBlue",
                    layover_city="BOS",
                    ticketed_destination="LHR",
                    hidden_city=True,
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
            code = main(["hidden-city", f"JFK-BOS:{FUTURE.isoformat()}"])
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("ticketed LHR", printed)
        self.assertIn("via BOS", printed)
        self.assertIn("yes", printed)

    def test_out_back_sugar_is_a_round_trip(self) -> None:
        back = FUTURE + timedelta(days=5)
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
            currency="USD",
            offers=(),
            return_date=back,
        )
        with patch("viajante.cli.search_hidden_city", return_value=report) as search:
            code = main(["hidden-city", f"JFK-MIA:{FUTURE.isoformat()}:{back.isoformat()}"])
        self.assertEqual(code, 0)
        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["return_date"], back)

    def test_comma_dates_are_rejected(self) -> None:
        later = FUTURE + timedelta(days=1)
        err = io.StringIO()
        with (
            patch("viajante.cli.search_hidden_city") as search,
            patch("sys.stderr", err),
        ):
            code = main(["hidden-city", f"JFK-LHR:{FUTURE.isoformat()},{later.isoformat()}"])
        self.assertEqual(code, 1)
        search.assert_not_called()
        self.assertIn("OUT:BACK", err.getvalue())


if __name__ == "__main__":
    unittest.main()
