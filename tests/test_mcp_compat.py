"""MCP client compatibility: annotations, structured errors, guide, HTTP transport.

Offline. Every test runs against a throwaway state dir, never the developer's own.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import http.client
import importlib.util
import io
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import _isolate  # noqa: F401
from viajante import mcp_server
from viajante.envelope import EnvelopeShapeError
from viajante.flights import classify_failure
from viajante.google_flights import GoogleFlightsBlocked
from viajante.google_hotels_rpc import HotelsBlocked
from viajante.hotels import _classify_hotel_failure
from viajante.mcp_errors import infer_field, is_internal, structured_error
from viajante.mcp_guide import GUIDE, INSTRUCTIONS
from viajante.models import SearchError, SearchErrorCode
from viajante.ratelimit import (
    GOOGLE_RATE_LIMIT_FILE,
    SKIPLAGGED_RATE_LIMIT_FILE,
    cooldown_until,
    note_rate_limited,
    rate_limit_advice,
    rate_limit_status,
)
from viajante.skiplagged import SkiplaggedRateLimited
from viajante.skiplagged import _classify as classify_skiplagged

try:  # a dependency of the mcp extra, absent without it
    import jsonschema
except ImportError:
    jsonschema = None

# test_mcp asserts the SDK is not imported until build_server runs, so import it on demand.
NEEDS_SDK = unittest.skipIf(importlib.util.find_spec("mcp") is None, "mcp extra missing")


def _sdk() -> types.SimpleNamespace:
    from mcp import ClientSession
    from mcp.client import streamable_http

    # Newer SDKs deprecate streamablehttp_client for streamable_http_client; the floor has only
    # the old name.
    http_client = getattr(streamable_http, "streamable_http_client", None) or (
        streamable_http.streamablehttp_client
    )
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import TextContent
    from pydantic import AnyUrl

    return types.SimpleNamespace(
        ClientSession=ClientSession,
        http_client=http_client,
        memory_session=create_connected_server_and_client_session,
        TextContent=TextContent,
        AnyUrl=AnyUrl,
    )


FUTURE = (date.today() + timedelta(days=30)).isoformat()
FUTURE_END = (date.today() + timedelta(days=33)).isoformat()
PAST = (date.today() - timedelta(days=2)).isoformat()

# Tools that ask a provider over the network. Everything else must work with sockets blocked.
NETWORK_TOOLS = {
    "search_flights",
    "search_dates",
    "search_flex",
    "search_explore",
    "search_hotels",
    "search_hotel_rooms",
    "search_trip",
    "search_split_tickets",
    "search_hidden_city",
    "recheck_offer",
    "get_hotel_details",
    "watch_price",
}
# Tools that write to the local state directory: neither read-only nor idempotent.
WRITING_TOOLS = {"watch_price"}
LOCAL_TOOLS = {
    "get_runtime_info",
    "lookup_airports",
    "compare_awards",
    "lookup_transfers",
    "validate_itinerary",
    "plan_stay_blocks",
    "split_stay_costs",
    "verify_answer",
    "get_guide",
    "price_history",
}


class _StateDir(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)


def _session_call(server, calls):
    """Run `calls(session)` against the server over an in-memory MCP session."""

    async def main():
        async with _sdk().memory_session(server._mcp_server) as session:
            return await calls(session)

    return asyncio.run(main())


def _text(result) -> str:
    return result.content[0].text


def _error_body(result) -> dict:
    text = _text(result)
    return json.loads(text[text.index("{") :])["error"]


@NEEDS_SDK
class ToolMetadataTests(_StateDir):
    def setUp(self) -> None:
        super().setUp()
        self.server = mcp_server.build_server()
        self.tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}

    def test_tool_set_is_the_documented_one(self) -> None:
        self.assertEqual(set(self.tools), NETWORK_TOOLS | LOCAL_TOOLS)

    def test_every_tool_has_a_title_and_annotations(self) -> None:
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertTrue(tool.title and tool.title.strip())
                self.assertNotEqual(tool.title, name)
                hints = tool.annotations
                self.assertIsNotNone(hints)
                writes = name in WRITING_TOOLS
                self.assertIs(hints.readOnlyHint, not writes)
                self.assertIs(hints.destructiveHint, False)
                self.assertIs(hints.idempotentHint, not writes)
                self.assertIsInstance(hints.openWorldHint, bool)

    def test_open_world_hint_matches_the_tools_that_reach_a_provider(self) -> None:
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertEqual(tool.annotations.openWorldHint, name in NETWORK_TOOLS)

    def test_network_tools_are_exactly_the_ones_on_the_search_runner(self) -> None:
        """Tools on `run_mcp_tool` fetch from a provider; every other tool must stay local."""
        used: list[tuple[str, str]] = []

        def args_for(tool) -> dict:
            filler = {"string": "x", "array": [], "object": {}, "integer": 1, "number": 1.0}
            props = tool.inputSchema.get("properties", {})
            return {
                key: filler.get(props[key].get("type"), "x")
                for key in tool.inputSchema.get("required", [])
            }

        async def calls(session):
            for name, tool in self.tools.items():
                if name in {"get_runtime_info", "get_guide"}:
                    continue
                runner = AsyncMock(return_value=[] if name == "lookup_airports" else {})
                with (
                    patch.object(mcp_server, "run_mcp_tool", runner) as search,
                    patch.object(mcp_server, "run_lookup_tool", AsyncMock(return_value={})) as look,
                ):
                    await session.call_tool(name, args_for(tool))
                    used.append((name, "search" if search.await_count else "local"))
                    self.assertEqual(search.await_count + look.await_count, 1, name)

        _session_call(self.server, calls)
        # room_rates defaults false, so that call stays off the search worker.
        self.assertEqual(
            {n for n, kind in used if kind == "search"},
            NETWORK_TOOLS - {"get_hotel_details"},
        )

        async def rates(session):
            with (
                patch.object(mcp_server, "run_mcp_tool", AsyncMock(return_value={})) as search,
                patch.object(mcp_server, "run_lookup_tool", AsyncMock(return_value={})) as look,
            ):
                await session.call_tool(
                    "get_hotel_details", {"selection_id": "x", "room_rates": True}
                )
                self.assertEqual(search.await_count, 1)
                self.assertEqual(look.await_count, 0)

        _session_call(self.server, rates)

    def test_local_tools_run_with_the_network_blocked(self) -> None:
        def refuse(*_a: object, **_k: object) -> None:
            raise AssertionError("a local tool touched the network")

        samples = {
            "get_runtime_info": {},
            "get_guide": {},
            "price_history": {"route": "JFK-LHR"},
            "lookup_airports": {"query": "NRT", "limit": 1},
            "lookup_transfers": {"program": "aeroplan", "points": 50000},
            "plan_stay_blocks": {"roster": {"2026-12-01": ["Ana"], "2026-12-02": ["Ana"]}},
            "split_stay_costs": {
                "stays": [
                    {"name": "A", "check_in": "2026-12-01", "check_out": "2026-12-03", "total": 100}
                ],
                "roster": {"2026-12-01": ["Ana"], "2026-12-02": ["Ana"]},
                "currency": "USD",
            },
            "validate_itinerary": {"legs": [], "constraints": {}, "currency": "USD"},
            "verify_answer": {"answer": "nothing quoted"},
            "compare_awards": {
                "offer": {
                    "origin": "JFK",
                    "destination": "LHR",
                    "departure_date": FUTURE,
                    "program": "aeroplan",
                    "points": 70000,
                    "evidence": "user_supplied",
                },
                "cash_price": 1200,
                "currency": "USD",
            },
        }
        self.assertEqual(set(samples), LOCAL_TOOLS)

        async def calls(session):
            return {name: await session.call_tool(name, args) for name, args in samples.items()}

        with (
            patch("socket.socket.connect", refuse),
            patch("socket.getaddrinfo", refuse),
        ):
            results = _session_call(self.server, calls)
        for name, result in results.items():
            with self.subTest(tool=name):
                self.assertFalse(result.isError, _text(result))


@NEEDS_SDK
class InvalidParameterTests(_StateDir):
    def setUp(self) -> None:
        super().setUp()
        self.server = mcp_server.build_server()

    def call(self, tool: str, **args: object) -> dict:
        async def calls(session):
            return await session.call_tool(tool, args)

        result = _session_call(self.server, calls)
        self.assertTrue(result.isError, _text(result))
        return _error_body(result)

    def test_bad_inputs_have_one_stable_shape_and_a_provable_field(self) -> None:
        cases = [
            ("search_flights", {"routes": ["XXX-LHR:" + FUTURE]}, "routes"),
            ("search_flights", {"routes": ["JFK-LHR:2026-13-45"]}, "routes"),
            ("search_flights", {"routes": ["JFK-LHR:" + PAST]}, "routes"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "trip": "bogus"}, "trip"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "sort": "bogus"}, "sort"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "max_stops": 5}, "max_stops"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "currency": "EURO"}, "currency"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "fetch": "warp"}, "fetch"),
            ("search_flights", {"routes": ["JFK-LHR:" + FUTURE], "airlines": "NO!"}, "airlines"),
            ("search_dates", {"route": "JFK", "start": FUTURE, "end": FUTURE_END}, "route"),
            ("search_dates", {"route": "JFK-LHR", "start": "nope", "end": FUTURE_END}, "start"),
            ("search_explore", {"origin": "JFK", "start": FUTURE, "top": 0}, "top"),
            ("search_explore", {"origin": "JFK", "start": PAST, "currency": "USD"}, "start"),
            ("search_hidden_city", {"route": "JFK-LAX", "departure": PAST}, "departure"),
            (
                "search_hotels",
                {"location": "Tokyo", "check_in": FUTURE, "check_out": FUTURE_END},
                "currency",
            ),
            (
                "search_hotels",
                {"location": "Tokyo", "check_in": PAST, "check_out": FUTURE, "currency": "JPY"},
                "check_in",
            ),
            ("lookup_airports", {"query": "  "}, "query"),
            ("validate_itinerary", {"legs": [], "constraints": {"optimal": True}}, "constraints"),
        ]
        for tool, args, field in cases:
            with self.subTest(tool=tool, field=field, args=args):
                body = self.call(tool, **args)
                self.assertEqual(set(body), {"code", "field", "message"})
                self.assertEqual(body["code"], "invalid_parameter")
                self.assertEqual(body["field"], field)
                self.assertTrue(body["message"])

    def test_missing_and_mistyped_arguments_get_the_same_body(self) -> None:
        cases = [
            ("search_flights", {}, "routes", "routes: Field required"),
            (
                "search_flights",
                {"routes": "JFK-LHR"},
                "routes",
                "routes: Input should be a valid list",
            ),
            (
                "search_dates",
                {"route": "JFK-LHR", "start": 20300101, "end": FUTURE},
                "start",
                "start: Input should be a valid string",
            ),
            (
                "search_explore",
                {"origin": "JFK", "start": FUTURE, "top": "many"},
                "top",
                "top: Input should be a valid integer",
            ),
        ]
        for tool, args, field, message in cases:
            with self.subTest(tool=tool, args=args):

                async def calls(session, tool=tool, args=args):
                    return await session.call_tool(tool, args)

                result = _session_call(self.server, calls)
                self.assertTrue(result.isError)
                text = _text(result)
                prefix = f"Error executing tool {tool}: "
                self.assertTrue(text.startswith(prefix + "{"), text)
                body = json.loads(text[len(prefix) :])["error"]
                self.assertEqual(body["code"], "invalid_parameter")
                self.assertEqual(body["field"], field)
                self.assertTrue(body["message"].startswith(message), body["message"])
                self.assertNotIn("input_value", text)
                self.assertNotIn("errors.pydantic.dev", text)

    def test_several_bad_arguments_are_listed_and_field_needs_a_single_parameter(self) -> None:
        async def calls(session):
            return await session.call_tool("search_dates", {"route": "JFK-LHR"})

        body = _error_body(_session_call(self.server, calls))
        self.assertIsNone(body["field"])
        self.assertIn("start: Field required; end: Field required", body["message"])

        async def same_parameter(session):
            return await session.call_tool("search_flights", {"routes": [1, 2]})

        body = _error_body(_session_call(self.server, same_parameter))
        self.assertEqual(body["field"], "routes")
        self.assertIn("routes.0", body["message"])
        self.assertIn("routes.1", body["message"])

    def test_unknown_arguments_are_rejected_not_ignored(self) -> None:
        cases = [
            (
                {"routes": ["JFK-LHR:" + FUTURE], "max_stop": 1},
                "max_stop",
                "unknown argument: max_stop",
            ),
            ({"routes": ["JFK-LHR:" + FUTURE], "max_stop": 1, "tpo": 2}, None, "max_stop, tpo"),
        ]
        for args, field, message in cases:
            with self.subTest(args=args):

                async def calls(session, args=args):
                    return await session.call_tool("search_flights", args)

                result = _session_call(self.server, calls)
                self.assertTrue(result.isError)
                body = _error_body(result)
                self.assertEqual(body["code"], "invalid_parameter")
                self.assertEqual(body["field"], field)
                self.assertIn(message, body["message"])

    def test_decode_failures_are_not_blamed_on_the_caller(self) -> None:
        for exc in (
            json.JSONDecodeError("bad", "{", 0),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "x"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(ValueError) as ctx:
                    mcp_server._reraise(exc, {"query": "x"})
                self.assertIs(ctx.exception, exc)

    def test_stay_messages_point_at_stays(self) -> None:
        params = {"stays": [{}], "roster": {}, "currency": "USD"}
        self.assertEqual(infer_field("stay 1 needs a name", params), "stays")
        self.assertEqual(infer_field("stay 2: unknown keys ['x']", params), "stays")
        self.assertIsNone(infer_field("stay 1 needs a name", {"roster": {}}))

    def test_field_is_null_when_the_message_names_no_single_parameter(self) -> None:
        body = self.call("search_dates", route="JFK-LHR", start=FUTURE_END, end=FUTURE)
        self.assertEqual(body["code"], "invalid_parameter")
        self.assertIsNone(body["field"])
        self.assertIn("end date must be on or after the start date", body["message"])

    def test_the_message_keeps_the_original_wording(self) -> None:
        body = self.call("search_explore", origin="JFK", start=FUTURE, top=0)
        self.assertEqual(body["message"], "top must be positive")

    def test_busy_search_is_its_own_code_and_keeps_its_sentence(self) -> None:
        held = mcp_server._SEARCH_BUSY.acquire(blocking=False)
        self.assertTrue(held)
        self.addCleanup(mcp_server._SEARCH_BUSY.release)
        body = self.call("search_flights", routes=["JFK-LHR:" + FUTURE])
        self.assertEqual(body["code"], "search_in_progress")
        self.assertIsNone(body["field"])
        self.assertIn("a viajante search is already running in this process", body["message"])

    def test_handlers_still_raise_plain_value_errors(self) -> None:
        from viajante.mcp_handlers import search_explore_tool

        with self.assertRaisesRegex(ValueError, "^top must be positive$"):
            search_explore_tool("JFK", start=FUTURE, top=0, currency="USD")

    def test_infer_field_never_picks_between_two_parameters(self) -> None:
        params = {"start": "2026-01-02", "end": "2026-01-01"}
        self.assertIsNone(infer_field("end date must be on or after the start date", params))
        self.assertEqual(infer_field("start date is in the past", params), "start")
        self.assertIsNone(infer_field("something unrelated", params))


def _epoch(iso: str) -> float:
    return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))


class RetryFieldTests(_StateDir):
    def test_search_error_carries_retry_fields_only_while_a_cooldown_runs(self) -> None:
        until = time.time() + 90
        error = SearchError(SearchErrorCode.BLOCKED, "429", rate_limited=True, retry_until=until)
        payload = error.to_dict()
        self.assertEqual(payload["rate_limited"], True)
        self.assertEqual(
            payload["retry_after"],
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(math.ceil(until))),
        )
        self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", payload["retry_after"]))
        self.assertIn(payload["retry_after_seconds"], (90, 91))
        self.assertGreaterEqual(_epoch(payload["retry_after"]), until)
        self.assertIsInstance(payload["retry_after_seconds"], int)

        expired = SearchError(
            SearchErrorCode.BLOCKED, "429", rate_limited=True, retry_until=time.time() - 1
        )
        self.assertEqual(set(expired.to_dict()), {"code", "message", "rate_limited"})
        unknown = SearchError(SearchErrorCode.BLOCKED, "429", rate_limited=True)
        self.assertEqual(set(unknown.to_dict()), {"code", "message", "rate_limited"})
        plain = SearchError(SearchErrorCode.NO_RESULTS, "none", retry_until=until)
        self.assertEqual(set(plain.to_dict()), {"code", "message"})

    def test_google_flights_429_names_the_recorded_cooldown(self) -> None:
        state = note_rate_limited(300.0)
        advice = rate_limit_advice(state)
        error = classify_failure(GoogleFlightsBlocked(advice, status=429))
        payload = error.to_dict()
        self.assertEqual(payload["message"], advice)
        self.assertGreater(payload["retry_after_seconds"], 290)
        self.assertLessEqual(payload["retry_after_seconds"], 301)

    def test_a_429_without_the_cooldown_advice_is_not_given_one(self) -> None:
        note_rate_limited(300.0)
        proxied = classify_failure(GoogleFlightsBlocked("Google Flights HTTP 429", status=429))
        self.assertTrue(proxied.rate_limited)
        self.assertNotIn("retry_after", proxied.to_dict())
        self.assertIsNone(cooldown_until("Google Flights HTTP 429"))

    def test_google_hotels_429_names_the_recorded_cooldown(self) -> None:
        advice = rate_limit_advice(note_rate_limited(120.0))
        payload = _classify_hotel_failure(HotelsBlocked(advice, rate_limited=True)).to_dict()
        self.assertIn("retry_after", payload)
        self.assertLessEqual(payload["retry_after_seconds"], 121)

    def test_skiplagged_reads_its_own_cooldown_file(self) -> None:
        advice = rate_limit_advice(
            note_rate_limited(60.0, file=SKIPLAGGED_RATE_LIMIT_FILE), provider="Skiplagged"
        )
        payload = classify_skiplagged(SkiplaggedRateLimited(advice)).to_dict()
        self.assertLessEqual(payload["retry_after_seconds"], 61)
        self.assertIsNone(cooldown_until(advice, GOOGLE_RATE_LIMIT_FILE))


@NEEDS_SDK
class SearchEnvelopeTests(_StateDir):
    def test_a_paused_search_returns_the_envelope_with_the_per_error_retry_fields(self) -> None:
        note_rate_limited(300.0)
        server = mcp_server.build_server()

        async def calls(session):
            return await session.call_tool(
                "search_flights", {"routes": [f"JFK-LHR:{FUTURE}"], "fetch": "sweep"}
            )

        result = _session_call(server, calls)
        self.assertFalse(result.isError)
        body = result.structuredContent
        error = body["queries"][0]["error"]
        self.assertEqual((body["status"], body["empty_reason"]), ("rate_limited", "not_loaded"))
        self.assertEqual(body["retry_after"], error["retry_after"])
        self.assertEqual(body["retry_after_seconds"], error["retry_after_seconds"])
        # A client that retries at the stated instant is no longer paused.
        self.assertIsNone(rate_limit_status(_epoch(body["retry_after"])))
        self.assertGreater(body["retry_after_seconds"], 290)

    def test_an_unrecognised_result_shape_is_not_an_invalid_parameter(self) -> None:
        report = MagicMock()
        report.to_dict.return_value = {"not": "a search result"}
        server = mcp_server.build_server()

        async def calls(session):
            return await session.call_tool("search_flights", {"routes": [f"JFK-LHR:{FUTURE}"]})

        with patch("viajante.mcp_handlers.search_flights", return_value=report):
            result = _session_call(server, calls)
        self.assertTrue(result.isError)
        text = _text(result)
        self.assertIn("viajante could not read the shape", text)
        self.assertNotIn("invalid_parameter", text)
        self.assertFalse(text.removeprefix("Error executing tool search_flights: ").startswith("{"))

    def test_shape_errors_are_internal(self) -> None:
        exc = EnvelopeShapeError("viajante could not read the shape")
        self.assertTrue(is_internal(exc))
        self.assertIsInstance(exc, ValueError)
        self.assertIs(structured_error(exc, {"routes": []}), exc)


class GuideTests(_StateDir):
    @staticmethod
    def _flat(text: str) -> str:
        return " ".join(text.split())

    def test_instructions_are_short_and_keep_the_load_bearing_rules(self) -> None:
        self.assertLess(len(INSTRUCTIONS.encode()), 2200)
        text = self._flat(INSTRUCTIONS)
        for phrase in (
            "viajante://guide",
            "get_guide",
            "verify_answer",
            "currency",
            "bags / carry_on",
            "Empty is not absent",
            "rate_limited true",
            "retry_after",
            "search_hidden_city",
            "never mix its rows with Google evidence",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_no_rule_from_the_old_instructions_was_lost(self) -> None:
        old = Path("tests/data/mcp_help_1_4_1.txt").read_text(encoding="utf-8")
        covered = self._flat(INSTRUCTIONS + "\n" + GUIDE)
        sentences = [
            s
            for paragraph in re.split(r"\n\s*\n", old)
            for s in re.split(r"(?<=[.;:])\s+", self._flat(paragraph))
            if len(s) > 12
        ]
        self.assertGreater(len(sentences), 60)
        missing = [s for s in sentences if s not in covered]
        self.assertEqual(missing, [])

    def test_guide_mentions_metro_codes_hotel_details_and_arrival_deadline(self) -> None:
        guide = self._flat(GUIDE)
        for phrase in (
            "metro code",
            "get_hotel_details",
            "arrival_deadline",
            "18 provider queries",
            "at most 20 entries",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, guide)
        self.assertIn("Also search_split_tickets", INSTRUCTIONS)

    def test_guide_lists_get_guide_among_tools_that_may_run_during_a_search(self) -> None:
        text = self._flat(GUIDE)
        self.assertIn("Also get_guide, which returns this guide", text)
        self.assertIn("get_guide may also run during a search", text)

    def test_help_prints_usage_then_the_short_instructions(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            mcp_server.main(["--help"])
        text = buffer.getvalue()
        for phrase in ("--transport", "--host", "--port", "get_guide", "viajante://guide"):
            self.assertIn(phrase, text)


@NEEDS_SDK
class GuideSurfaceTests(_StateDir):
    def test_guide_is_a_markdown_resource_and_a_tool(self) -> None:
        server = mcp_server.build_server()

        async def calls(session):
            listed = await session.list_resources()
            read = await session.read_resource(_sdk().AnyUrl("viajante://guide"))
            tool = await session.call_tool("get_guide", {})
            return listed, read, tool

        listed, read, tool = _session_call(server, calls)
        resource = next(r for r in listed.resources if str(r.uri) == "viajante://guide")
        self.assertEqual(resource.mimeType, "text/markdown")
        self.assertEqual(read.contents[0].text, GUIDE)
        self.assertFalse(tool.isError)
        body = json.loads(_text(tool))
        self.assertEqual(body["guide"], GUIDE)
        self.assertEqual((body["status"], body["completeness"]), ("ok", "complete"))
        self.assertEqual(tool.structuredContent["guide"], GUIDE)
        self.assertIn("retry_after", body)
        self.assertTrue(GUIDE.startswith("# viajante MCP guide"))

    def test_get_guide_schema_declares_the_guide_beside_the_envelope(self) -> None:
        server = mcp_server.build_server()

        async def calls(session):
            listed = await session.list_tools()
            return listed, await session.call_tool("get_guide", {})

        listed, result = _session_call(server, calls)
        schemas = {tool.name: tool.outputSchema for tool in listed.tools}
        schema = schemas["get_guide"]
        self.assertEqual(schema["properties"]["guide"]["type"], "string")
        self.assertIn("guide", schema["required"])
        envelope = set(schemas["get_runtime_info"]["properties"])
        self.assertTrue(envelope <= set(schema["properties"]))
        self.assertEqual(set(schema["properties"]) - envelope, {"guide"})
        self.assertNotIn("guide", schemas["get_runtime_info"]["properties"])
        self.assertIsNone(schemas["lookup_airports"])
        jsonschema.validate(result.structuredContent, schema)
        self.assertEqual(result.structuredContent["guide"], GUIDE)

    def test_server_instructions_are_the_short_text(self) -> None:
        server = mcp_server.build_server()
        self.assertEqual(server.instructions, INSTRUCTIONS)


class MainArgumentTests(_StateDir):
    def run_main(self, *argv: str):
        server = MagicMock()
        stderr = io.StringIO()
        with (
            patch.object(mcp_server, "build_server", return_value=server) as build,
            contextlib.redirect_stderr(stderr),
        ):
            mcp_server.main(list(argv))
        return build, server, stderr.getvalue()

    def test_stdio_is_the_default(self) -> None:
        build, server, err = self.run_main()
        build.assert_called_once_with()
        server.run.assert_called_once_with(transport="stdio")
        self.assertEqual(err, "")

    def test_http_binds_loopback_by_default_without_a_warning(self) -> None:
        build, server, err = self.run_main("--transport", "streamable-http")
        build.assert_called_once_with(host="127.0.0.1", port=8000)
        server.run.assert_called_once_with(transport="streamable-http")
        self.assertEqual(err, "")

    def test_non_loopback_host_warns_about_the_shared_ip_and_cooldown(self) -> None:
        build, _server, err = self.run_main(
            "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9001"
        )
        build.assert_called_once_with(host="0.0.0.0", port=9001)
        self.assertIn("no authentication", err)
        self.assertIn("this machine's IP", err)
        self.assertIn("cooldown", err)

    def test_any_loopback_address_is_quiet(self) -> None:
        for host in ("127.0.0.2", "127.255.255.254", "::1", "[::1]", "localhost"):
            with self.subTest(host=host):
                build, _server, err = self.run_main(
                    "--transport", "streamable-http", "--host", host
                )
                self.assertEqual(err, "")
                self.assertEqual(build.call_args.kwargs["host"], host)

    def test_other_addresses_warn(self) -> None:
        for host in ("0.0.0.0", "192.168.1.20", "::", "example.com"):
            with self.subTest(host=host):
                _build, _server, err = self.run_main(
                    "--transport", "streamable-http", "--host", host
                )
                self.assertIn("no authentication", err)

    def test_host_or_port_without_http_is_an_error(self) -> None:
        for argv in (["--port", "9"], ["--host", "127.0.0.1"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    mcp_server.main(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_port_must_be_a_real_port(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            mcp_server.main(["--transport", "streamable-http", "--port", "70000"])

    def test_server_json_publishes_no_remote(self) -> None:
        data = json.loads(Path("server.json").read_text(encoding="utf-8"))
        self.assertNotIn("remotes", data)
        for package in data["packages"]:
            self.assertEqual(package["transport"], {"type": "stdio"})


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_http_server(case: unittest.TestCase, host: str = "127.0.0.1") -> int:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "viajante.mcp_server",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--host",
            host,
        ],
        env={**os.environ, "VIAJANTE_STATE_DIR": os.environ["VIAJANTE_STATE_DIR"]},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    case.addCleanup(proc.stderr.close)
    case.addCleanup(proc.wait, 10)
    case.addCleanup(proc.terminate)
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            case.fail(f"server exited early: {proc.stderr.read()}")
        with contextlib.suppress(OSError), socket.create_connection((host, port), 0.5):
            return port
        time.sleep(0.1)
    case.fail("server did not start listening")


def _raw_initialize(port: int, headers: dict[str, str]) -> int:
    return _raw_initialize_at("127.0.0.1", port, headers)


def _raw_initialize_at(address: str, port: int, headers: dict[str, str]) -> int:
    """POST an initialize with exactly these headers (http.client adds none of its own)."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        }
    )
    conn = http.client.HTTPConnection(address, port, timeout=10)
    try:
        conn.putrequest("POST", "/mcp", skip_host=True, skip_accept_encoding=True)
        for name, value in {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Content-Length": str(len(body)),
            **headers,
        }.items():
            conn.putheader(name, value)
        conn.endheaders(body.encode())
        return conn.getresponse().status
    finally:
        conn.close()


@NEEDS_SDK
class HttpHostProtectionTests(_StateDir):
    """The SDK enables DNS-rebinding protection by itself only from 1.23; the floor is 1.14.1."""

    def test_foreign_host_and_origin_are_refused_and_local_ones_are_not(self) -> None:
        port = _start_http_server(self)
        own = f"127.0.0.1:{port}"
        self.assertEqual(_raw_initialize(port, {"Host": own}), 200)
        self.assertEqual(_raw_initialize(port, {"Host": f"localhost:{port}"}), 200)
        self.assertEqual(
            _raw_initialize(port, {"Host": own, "Origin": "http://localhost:3000"}), 200
        )
        self.assertEqual(_raw_initialize(port, {"Host": f"evil.example:{port}"}), 421)
        self.assertEqual(_raw_initialize(port, {"Host": "evil.example"}), 421)
        # 400 on SDK 1.14, 403 on newer ones: either way it is refused, never 200.
        self.assertIn(
            _raw_initialize(port, {"Host": own, "Origin": "http://evil.example"}), (400, 403)
        )
        self.assertIn(
            _raw_initialize(port, {"Host": own, "Origin": f"http://evil.example:{port}"}),
            (400, 403),
        )

    def test_another_loopback_address_is_allowed_by_name(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.2", 0))
            except OSError as exc:
                self.skipTest(f"127.0.0.2 is not configured on this host: {exc}")
        port = _start_http_server(self, "127.0.0.2")
        self.assertEqual(_raw_initialize_at("127.0.0.2", port, {"Host": f"127.0.0.2:{port}"}), 200)
        self.assertEqual(_raw_initialize_at("127.0.0.2", port, {"Host": "evil.example"}), 421)


@NEEDS_SDK
class StreamableHttpSessionTests(_StateDir):
    def test_real_client_session_over_http(self) -> None:
        port = _start_http_server(self)

        async def session_calls():
            sdk = _sdk()
            url = f"http://127.0.0.1:{port}/mcp"
            async with sdk.http_client(url) as (read, write, _session_id):
                async with sdk.ClientSession(read, write) as session:
                    init = await session.initialize()
                    tools = await session.list_tools()
                    split = await session.call_tool(
                        "split_stay_costs",
                        {
                            "stays": [
                                {
                                    "name": "Harbour Inn",
                                    "check_in": "2026-12-01",
                                    "check_out": "2026-12-03",
                                    "total": 200,
                                }
                            ],
                            "roster": {
                                "2026-12-01": ["Ana", "Luis"],
                                "2026-12-02": ["Ana", "Luis"],
                            },
                            "currency": "USD",
                        },
                    )
                    bad = await session.call_tool("lookup_airports", {"query": "  "})
                    return init, tools, split, bad

        init, tools, split, bad = asyncio.run(asyncio.wait_for(session_calls(), 60))
        self.assertEqual(init.serverInfo.name, "viajante")
        self.assertEqual(init.instructions, INSTRUCTIONS)
        self.assertEqual({t.name for t in tools.tools}, NETWORK_TOOLS | LOCAL_TOOLS)
        self.assertFalse(split.isError, _text(split))
        payload = json.loads(_text(split))
        self.assertEqual(payload["currency"], "USD")
        self.assertEqual(payload["stays"][0]["total"], 200)
        self.assertTrue(bad.isError)
        self.assertEqual(_error_body(bad)["field"], "query")
        self.assertIsInstance(bad.content[0], _sdk().TextContent)


# The stdio child imports these fixtures by path, so load them the same way here: the file is
# not a package member under `python -m unittest tests.test_mcp_compat`.
_SPLIT_FIXTURES = importlib.util.spec_from_file_location(
    "split_fixtures", Path(__file__).with_name("test_split.py")
)
assert _SPLIT_FIXTURES is not None and _SPLIT_FIXTURES.loader is not None
_split_fixtures = importlib.util.module_from_spec(_SPLIT_FIXTURES)
_SPLIT_FIXTURES.loader.exec_module(_split_fixtures)
SPLIT_ROUTE = _split_fixtures.ROUTE

SPLIT_SERVER = """
import sys
from unittest.mock import patch

sys.path.insert(0, {tests!r})
import test_split as t
from viajante.mcp_server import main
from viajante.split import search_split_tickets as real

fake = t.FakeSearch(t._hub_table())
with patch(
    "viajante.mcp_handlers.search_split_tickets",
    side_effect=lambda query, **kw: real(
        query, packaged=t._packaged_via("LAX"), search=fake, **kw
    ),
):
    main([])
"""


@NEEDS_SDK
class SplitTicketStdioTests(_StateDir):
    def test_search_split_tickets_round_trips_over_stdio_and_matches_its_output_schema(
        self,
    ) -> None:
        """A real stdio server with only the fetch faked: the SDK checks the structured result."""
        from jsonschema import validate
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", SPLIT_SERVER.format(tests=str(Path(__file__).parent))],
            env={**os.environ},
        )

        async def calls():
            sdk = _sdk()
            async with stdio_client(params) as (read, write):
                async with sdk.ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    result = await session.call_tool(
                        "search_split_tickets", {"route": SPLIT_ROUTE, "via": "LAX"}
                    )
                    return tools["search_split_tickets"], result

        tool, result = asyncio.run(asyncio.wait_for(calls(), 60))
        self.assertFalse(result.isError, _text(result))
        structured = result.structuredContent
        validate(structured, tool.outputSchema)
        self.assertEqual((structured["status"], structured["completeness"]), ("ok", "complete"))
        self.assertEqual(structured["observed_at"], structured["searched_at"])
        self.assertEqual(structured["observed_at_basis"], "fetch")
        row = structured["itineraries"][0]
        self.assertEqual((row["split_ticket"], row["connection_protected"]), (True, False))
        self.assertEqual(json.loads(_text(result)), structured)
        self.assertTrue(tool.annotations.openWorldHint)
        self.assertEqual(tool.title, "Split-ticket itineraries")

    def test_a_recorded_cooldown_returns_a_null_observation_over_stdio(self) -> None:
        """No fake at all: the real search stops on the recorded cooldown before any request."""
        from jsonschema import validate
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client

        until = time.time() + 300
        note_rate_limited(300.0, until - 300, file=GOOGLE_RATE_LIMIT_FILE)
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "viajante.mcp_server"], env={**os.environ}
        )

        async def calls():
            sdk = _sdk()
            async with stdio_client(params) as (read, write):
                async with sdk.ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    result = await session.call_tool(
                        "search_split_tickets", {"route": SPLIT_ROUTE, "via": "LAX"}
                    )
                    return tools["search_split_tickets"], result

        tool, result = asyncio.run(asyncio.wait_for(calls(), 60))
        self.assertFalse(result.isError, _text(result))
        structured = result.structuredContent
        validate(structured, tool.outputSchema)
        self.assertEqual(
            (structured["status"], structured["completeness"], structured["empty_reason"]),
            ("rate_limited", "blocked", "not_loaded"),
        )
        self.assertIsNone(structured["observed_at"])
        self.assertIsNone(structured["observed_at_basis"])
        self.assertEqual(structured["extra_searches"], 0)
        self.assertEqual(structured["retry_after"], structured["error"]["retry_after"])
        self.assertTrue(structured["retry_after_seconds"] >= 1)


if __name__ == "__main__":
    unittest.main()
