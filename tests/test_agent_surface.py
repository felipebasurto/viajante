"""Agent-facing docs name commands and tools that exist.

Offline. Reads the checked-in text and the local CLI / MCP registration.
"""

from __future__ import annotations

import argparse
import inspect
import re
import subprocess
import sys
import unittest
from pathlib import Path

import _isolate  # noqa: F401
import viajante
from viajante.cli import _cli_parser
from viajante.hotels import search_hotels
from viajante.mcp_guide import GUIDE
from viajante.mcp_handlers import search_flights_tool

ROOT = Path(__file__).resolve().parents[1]

DOCS = (
    "llms.txt",
    "docs/usage.md",
    "docs/architecture.md",
    ".cursor/skills/viajante/SKILL.md",
    ".cursor/skills/viajante/mcp.md",
    "integrations/hermes/skills/viajante/SKILL.md",
    "landing/index.html",
)

# Backtick names that share a tool-like shape but are fields, not tools.
NOT_TOOLS = {
    "include_price_history",
    "price_cap",
    "price_changed",
    "price_comparison",
    "price_insight",
    "price_insight_error",
    "search_evidence",
    "search_in_progress",
    "split_filters",
    "split_tickets",
    # Library name. The MCP flight tool is search_flights.
    "get_flights",
}

# A command invocation, not prose ("viajante returns ..."). The next token is a
# flag, a route or place, the end of the line, or the closing backtick.
COMMAND = re.compile(
    r"(?:`|^[ \t]*)viajante\s+([a-z][a-z0-9-]*)(?=\s+(?:--|[\w./]*[A-Z0-9])|[ \t]*$|`)",
    re.M,
)
TOOLISH = re.compile(
    r"`((?:search|get|lookup|compare|validate|plan|split|verify|recheck)_[a-z0-9_]+|price_history|watch_price)`"
)


def _texts() -> list[tuple[str, str]]:
    rows = [(rel, (ROOT / rel).read_text(encoding="utf-8")) for rel in DOCS]
    rows.append(("mcp_guide.GUIDE", GUIDE))
    return rows


class AgentSurfaceTests(unittest.TestCase):
    def test_named_cli_commands_and_mcp_tools_exist(self) -> None:
        parser = _cli_parser()
        commands: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                commands.update(action.choices)
        # A child process, so this one does not import the MCP SDK. A later test
        # requires `mcp` to be absent from sys.modules.
        listed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import asyncio\n"
                "from viajante.mcp_server import build_server\n"
                "tools = asyncio.run(build_server().list_tools())\n"
                "print('\\n'.join(tool.name for tool in tools))\n",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        tools = set(listed.stdout.split())
        self.assertGreaterEqual(len(tools), 22)
        for rel, text in _texts():
            unknown_cmds = sorted(set(COMMAND.findall(text)) - commands)
            self.assertEqual(unknown_cmds, [], rel)
            unknown_tools = sorted(set(TOOLISH.findall(text)) - tools - NOT_TOOLS)
            self.assertEqual(unknown_tools, [], rel)

    def test_public_names_import_and_the_docstring_names_the_footguns(self) -> None:
        for name in viajante.__all__:
            self.assertIsNotNone(getattr(viajante, name), name)
        doc = viajante.__doc__ or ""
        self.assertIn('source="booking"', doc)
        self.assertIn('source="google"', doc)
        self.assertIn("selection_id", doc)
        self.assertIn("search_hotel_rooms", doc)
        self.assertEqual(inspect.signature(search_hotels).parameters["source"].default, "booking")
        self.assertIn("exclude_alliance", inspect.signature(search_flights_tool).parameters)
        self.assertNotIn("exclude_alliances", GUIDE)
        self.assertIn("lowest_price_distinct", GUIDE)
        self.assertIn("uv run viajante-mcp", GUIDE)
        self.assertIn("no remotes", GUIDE)


if __name__ == "__main__":
    unittest.main()
