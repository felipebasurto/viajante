from __future__ import annotations

import io
import sys
import threading
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from viajante.explore import DEFAULT_EXPLORE_TOP
from viajante.mcp_handlers import (
    lookup_airports_tool,
    search_dates_tool,
    search_explore_tool,
    search_flights_tool,
    search_hotels_tool,
)

FUTURE = (date.today() + timedelta(days=30)).isoformat()
FUTURE_OUT = (date.today() + timedelta(days=33)).isoformat()
PAST = (date.today() - timedelta(days=1)).isoformat()


def _report(**payload: object) -> MagicMock:
    report = MagicMock()
    report.to_dict.return_value = {
        "schema_version": 1,
        **payload,
    }
    return report


class McpHandlerTests(unittest.TestCase):
    def test_handlers_do_not_import_the_sdk(self) -> None:
        text = Path("src/viajante/mcp_handlers.py").read_text(encoding="utf-8")
        self.assertNotIn("from mcp", text)
        self.assertNotIn("import mcp", text)

    def test_lookup_airports_returns_dicts(self) -> None:
        rows = lookup_airports_tool("MAD", limit=3)
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("iata", rows[0])
        self.assertNotIn("success", rows[0])

    def test_lookup_london_ranks_passenger_airports_first(self) -> None:
        rows = lookup_airports_tool("london", limit=8)
        codes = [row["iata"] for row in rows]
        self.assertLess(codes.index("LHR"), codes.index("BQH") if "BQH" in codes else len(codes))
        self.assertTrue({"LHR", "LGW", "STN"} <= set(codes[:5]))

    def test_search_flights_returns_report_to_dict(self) -> None:
        fake = _report(queries=[], currency="EUR")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            payload = search_flights_tool([f"MAD-BCN:{FUTURE}"], top=3)
        search.assert_called_once()
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("queries", payload)
        self.assertNotIn("success", payload)
        self.assertNotIn("flights", payload)

    def test_search_flights_accepts_cli_filters_and_round_trip_alias(self) -> None:
        fake = _report(queries=[], currency="EUR")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            search_flights_tool(
                [f"MAD-PRG:{FUTURE}:{FUTURE_OUT}"],
                trip="round-trip",
                airlines="IB,I2",
                exclude_airlines="FR",
                depart_window="7-12",
                max_duration=8,
                min_layover=1,
                max_layover=6,
                baggage_buffer=0,
                sort="duration",
            )
        kwargs = search.call_args.kwargs
        self.assertEqual(type(search.call_args.args[0][0]).__name__, "RoundTrip")
        self.assertEqual(kwargs["airlines"], ("IB", "I2"))
        self.assertEqual(kwargs["exclude_airlines"], ("FR",))
        self.assertEqual(kwargs["depart_window"], (7 * 60, 12 * 60 + 59))
        self.assertEqual(kwargs["max_duration_hours"], 8)
        self.assertEqual(kwargs["min_layover_hours"], 1)
        self.assertEqual(kwargs["max_layover_hours"], 6)
        self.assertEqual(kwargs["buffer_eur"], 0)
        self.assertEqual(kwargs["sort"], "duration")

    def test_search_flights_accepts_clock_window_and_price_sort(self) -> None:
        fake = _report(queries=[], currency="EUR")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            search_flights_tool(
                [f"MAD-BCN:{FUTURE}"],
                depart_window="06:00-20:00",
                sort="price",
            )
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["depart_window"], (6 * 60, 20 * 60))
        self.assertEqual(kwargs["sort"], "price")

    def test_search_flights_accepts_alliance_filters(self) -> None:
        fake = _report(queries=[], currency="EUR")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            search_flights_tool(
                [f"MAD-LHR:{FUTURE}"],
                alliance="oneworld",
                exclude_alliance="star",
            )
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["alliances"], ("oneworld",))
        self.assertEqual(kwargs["exclude_alliances"], ("star",))

    def test_search_flights_accepts_bags_and_carry_on(self) -> None:
        fake = _report(queries=[], currency="EUR")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            search_flights_tool([f"MAD-BCN:{FUTURE}"], bags=1, carry_on=1)
        trip = search.call_args.args[0][0]
        self.assertEqual(trip.bags, 1)
        self.assertEqual(trip.carry_on, 1)

    def test_search_flights_accepts_occupancy_and_locale_params(self) -> None:
        fake = _report(queries=[], currency="USD")
        with patch("viajante.mcp_handlers.search_flights", return_value=fake) as search:
            search_flights_tool(
                [f"MAD-BCN:{FUTURE}"],
                adults=2,
                children=1,
                infants_in_seat=1,
                infants_on_lap=1,
                currency="usd",
                country="us",
            )
        trip = search.call_args.args[0][0]
        self.assertEqual(trip.adults, 2)
        self.assertEqual(trip.children, 1)
        self.assertEqual(trip.infants_in_seat, 1)
        self.assertEqual(trip.infants_on_lap, 1)
        self.assertEqual(search.call_args.kwargs["currency"], "usd")
        self.assertEqual(search.call_args.kwargs["country"], "us")

    def test_past_flight_date_fails_before_search(self) -> None:
        with patch("viajante.mcp_handlers.search_flights") as search:
            with self.assertRaises(ValueError):
                search_flights_tool([f"MAD-BCN:{PAST}"])
        search.assert_not_called()

    def test_search_dates_does_not_pass_fetch(self) -> None:
        fake = _report(days=[])
        with patch("viajante.mcp_handlers.search_dates", return_value=fake) as search:
            payload = search_dates_tool("MAD-BCN", FUTURE, FUTURE_OUT)
        kwargs = search.call_args.kwargs
        self.assertNotIn("fetch", kwargs)
        self.assertEqual(kwargs["trip"], "one-way")
        self.assertIsNone(kwargs["nights"])
        self.assertEqual(payload["schema_version"], 1)

    def test_search_dates_accepts_nights_as_round_trip(self) -> None:
        fake = _report(days=[], trip="rt", nights=5)
        with patch("viajante.mcp_handlers.search_dates", return_value=fake) as search:
            payload = search_dates_tool("BOS-LHR", FUTURE, FUTURE_OUT, nights=5)
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["trip"], "rt")
        self.assertEqual(kwargs["nights"], 5)
        self.assertEqual(payload["trip"], "rt")
        self.assertEqual(payload["nights"], 5)

    def test_search_dates_round_trip_without_nights_fails_before_search(self) -> None:
        with patch("viajante.mcp_handlers.search_dates") as search:
            with self.assertRaises(ValueError):
                search_dates_tool("BOS-LHR", FUTURE, FUTURE_OUT, trip="rt")
        search.assert_not_called()

    def test_past_date_window_fails_before_search(self) -> None:
        with patch("viajante.mcp_handlers.search_dates") as search:
            with self.assertRaises(ValueError):
                search_dates_tool("MAD-BCN", PAST, FUTURE)
        search.assert_not_called()

    def test_search_explore_defaults_match_cli_and_accepts_filters(self) -> None:
        fake = _report(destinations=[])
        with patch("viajante.mcp_handlers.search_explore", return_value=fake) as search:
            payload = search_explore_tool(
                "MAD",
                FUTURE,
                adults=2,
                cabin="business",
                max_stops=0,
            )
        self.assertEqual(search.call_args.kwargs["top"], DEFAULT_EXPLORE_TOP)
        self.assertEqual(search.call_args.kwargs["adults"], 2)
        self.assertEqual(search.call_args.kwargs["cabin"], "business")
        self.assertEqual(search.call_args.kwargs["max_stops"], 0)
        self.assertEqual(payload["schema_version"], 1)
        self.assertNotIn("success", payload)

    def test_search_hotels_defaults_to_google(self) -> None:
        fake = _report(provider="google-hotels", queries=[])
        with patch("viajante.mcp_handlers.search_hotels", return_value=fake) as search:
            payload = search_hotels_tool("Prague", FUTURE, FUTURE_OUT)
        self.assertEqual(search.call_args.kwargs["source"], "google")
        self.assertEqual(payload["provider"], "google-hotels")
        self.assertNotIn("success", payload)

    def test_google_hotels_reject_min_rating_above_five(self) -> None:
        with patch("viajante.mcp_handlers.search_hotels") as search:
            with self.assertRaises(ValueError):
                search_hotels_tool("Prague", FUTURE, FUTURE_OUT, min_rating=8.5, source="google")
        search.assert_not_called()

    def test_overlapping_search_is_rejected(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fake(*_args: Any, **_kwargs: Any) -> MagicMock:
            started.set()
            self.assertTrue(release.wait(2))
            return _report(queries=[])

        with patch("viajante.mcp_handlers.search_flights", fake):
            worker = threading.Thread(target=lambda: search_flights_tool([f"MAD-BCN:{FUTURE}"]))
            worker.start()
            self.assertTrue(started.wait(2))
            with self.assertRaises(ValueError) as ctx:
                search_flights_tool([f"MAD-BCN:{FUTURE_OUT}"])
            self.assertIn("already running", str(ctx.exception))
            rows = lookup_airports_tool("MAD", limit=1)
            self.assertGreaterEqual(len(rows), 1)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())


class _FakeFastMCP:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tools: list[str] = []

    def tool(self, *_args: object, **_kwargs: object):
        def deco(fn: Any) -> Any:
            self.tools.append(fn.__name__)
            return fn

        return deco


def _sdk_module_names() -> tuple[str, ...]:
    return ("mcp", "mcp.server", "mcp.server.fastmcp")


class McpServerImportTests(unittest.TestCase):
    def test_mcp_server_keeps_fastmcp_off_module_import(self) -> None:
        text = Path("src/viajante/mcp_server.py").read_text(encoding="utf-8")
        self.assertFalse(text.startswith("from mcp") or text.startswith("import mcp"))
        self.assertNotIn("\nfrom mcp", text)
        self.assertNotIn("\nimport mcp", text)
        self.assertIn("    from mcp.server.fastmcp import FastMCP", text)

    def test_help_does_not_import_fastmcp(self) -> None:
        from viajante.mcp_server import main

        for name in _sdk_module_names():
            self.assertNotIn(name, sys.modules)
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            main(["--help"])
        help_text = buffer.getvalue()
        for name in _sdk_module_names():
            self.assertNotIn(name, sys.modules)
        self.assertIn("viajante-mcp", help_text)
        self.assertIn("search_flights", help_text)
        self.assertIn("stdio", help_text)

    def test_build_server_registers_tools_without_sdk(self) -> None:
        for name in _sdk_module_names():
            self.assertNotIn(name, sys.modules)
        fake_mcp = types.ModuleType("mcp")
        fake_server = types.ModuleType("mcp.server")
        fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
        fake_fastmcp.FastMCP = _FakeFastMCP
        fake_mcp.server = fake_server
        fake_server.fastmcp = fake_fastmcp
        sys.modules["mcp"] = fake_mcp
        sys.modules["mcp.server"] = fake_server
        sys.modules["mcp.server.fastmcp"] = fake_fastmcp

        def _drop_fakes() -> None:
            for name in _sdk_module_names():
                sys.modules.pop(name, None)

        self.addCleanup(_drop_fakes)

        from viajante.mcp_server import build_server

        server = build_server()
        self.assertIsInstance(server, _FakeFastMCP)
        self.assertEqual(server.name, "viajante")
        self.assertEqual(
            server.tools,
            [
                "search_flights",
                "search_dates",
                "search_explore",
                "search_hotels",
                "lookup_airports",
            ],
        )


if __name__ == "__main__":
    unittest.main()
