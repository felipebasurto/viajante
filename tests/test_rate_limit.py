from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from viajante import mcp_handlers
from viajante.flights import _needs_detail_fallback, classify_failure
from viajante.google_flights import (
    RATE_LIMIT_COOLDOWN_SECONDS,
    RATE_LIMIT_MAX_COOLDOWN_SECONDS,
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    note_rate_limited,
    rate_limit_status,
)
from viajante.google_hotels import GoogleHotelsSource
from viajante.google_hotels_rpc import HotelsBlocked
from viajante.models import FlightQuery, HotelQuery, QueryFailure

T0 = 1_800_000_000.0


class _StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)


class CooldownStateTests(_StateDir):
    def test_first_429_starts_the_base_cooldown_and_a_burst_does_not_extend_it(self) -> None:
        first = note_rate_limited(now=T0)
        self.assertEqual(first["until"], T0 + RATE_LIMIT_COOLDOWN_SECONDS)
        self.assertEqual(note_rate_limited(now=T0 + 5), first)
        self.assertIsNotNone(rate_limit_status(now=T0 + 1))
        self.assertIsNone(rate_limit_status(now=first["until"] + 1))

    def test_repeat_429_soon_after_a_cooldown_doubles_up_to_the_cap(self) -> None:
        state = note_rate_limited(now=T0)
        for _ in range(8):
            state = note_rate_limited(now=state["until"] + 1)
        self.assertEqual(state["cooldown_s"], RATE_LIMIT_MAX_COOLDOWN_SECONDS)

    def test_named_retry_after_wins(self) -> None:
        self.assertEqual(note_rate_limited(retry_after=42, now=T0)["cooldown_s"], 42)


class CooldownGateTests(_StateDir):
    def test_flights_and_hotels_send_nothing_during_a_cooldown(self) -> None:
        note_rate_limited()
        departure = date.today() + timedelta(days=30)
        with (
            patch("viajante.google_flights.shared_chrome_sweep_client") as flights_client,
            patch("viajante.google_hotels.shared_chrome_sweep_client") as hotels_client,
        ):
            with self.assertRaises(GoogleFlightsBlocked) as flights:
                GoogleFlightsHttpSource(currency="USD").fetch(FlightQuery("JFK", "LHR", departure))
            with self.assertRaises(HotelsBlocked) as hotels:
                GoogleHotelsSource(currency="USD").fetch(
                    HotelQuery("Tokyo", departure, departure + timedelta(days=2)), None, 5
                )
        flights_client.assert_not_called()
        hotels_client.assert_not_called()
        self.assertTrue(str(flights.exception).startswith("Not sent. Google is rate-limiting"))
        self.assertTrue(hotels.exception.rate_limited)
        error = classify_failure(flights.exception)
        self.assertTrue(error.rate_limited)
        self.assertIs(error.to_dict()["rate_limited"], True)
        failure = QueryFailure(query=FlightQuery("JFK", "LHR", departure), error=error)
        self.assertFalse(_needs_detail_fallback(failure))

    def test_proxied_search_is_not_paused(self) -> None:
        note_rate_limited()
        with patch("viajante.google_flights.shared_chrome_sweep_client") as client:
            GoogleFlightsHttpSource(proxy="http://proxy.example:8080")._ensure_client()
        client.assert_called_once()


class SearchCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        mcp_handlers._CACHE.clear()
        self.addCleanup(mcp_handlers._CACHE.clear)

    def test_identical_success_is_replayed_and_failures_are_not(self) -> None:
        calls: list[str] = []

        @mcp_handlers._cached
        def tool(route: str, *, fail: bool = False) -> dict:
            calls.append(route)
            error = {"code": "blocked"} if fail else None
            return {"searched_at": "t", "error": error, "lead": ["x"]}

        tool("JFK-LHR")
        again = tool("JFK-LHR")
        self.assertEqual(calls, ["JFK-LHR"])
        self.assertTrue(again["cached"])
        self.assertTrue(again["lead"][0].startswith("cached:"))
        tool("JFK-LHR", fail=True)
        tool("JFK-LHR", fail=True)
        self.assertEqual(calls, ["JFK-LHR", "JFK-LHR", "JFK-LHR"])


if __name__ == "__main__":
    unittest.main()
