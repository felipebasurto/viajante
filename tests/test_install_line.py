"""The MCP install line agrees across the five copies AGENTS.md names.

Offline. Reads the checked-in files only; no copy is generated from another.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import _isolate  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]

# The copies AGENTS.md keeps "on that same order": primary npx first, no-Node uvx second.
COPIES = (
    "README.md",
    "llms.txt",
    "src/viajante/mcp_guide.py",
    ".cursor/skills/viajante/SKILL.md",
    "landing/index.html",
)
# The `-p` lookahead skips the CLI line `npx -y -p @viajante/mcp viajante airports`.
NPX = re.compile(r"npx\b(?![^\n]*?\s-p\s)[\s\S]{0,60}?@viajante/mcp\b")
UVX = re.compile(r"uvx\b[\s\S]{0,120}?viajante\[mcp\][\s\S]{0,40}?viajante-mcp\b")


class InstallLineAgreementTest(unittest.TestCase):
    def test_five_copies_name_the_same_install_in_the_same_order(self) -> None:
        for rel in COPIES:
            with self.subTest(copy=rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                npx = NPX.search(text)
                if npx is None:
                    self.fail("primary `npx -y @viajante/mcp` install is missing")
                self.assertNotIn("@viajante/mcp@", text, "the install line must not pin a version")
                uvx = UVX.search(text)
                if uvx is None:
                    self.fail("no-Node `uvx --from viajante[mcp] viajante-mcp` is missing")
                self.assertLess(npx.start(), uvx.start(), "npx must come before uvx")

    def test_landing_asks_web_or_desktop_before_copying(self) -> None:
        text = (ROOT / "landing/index.html").read_text(encoding="utf-8")
        names = re.findall(r'data-name="([^"]+)"', text)
        self.assertEqual(
            names,
            ["Claude", "ChatGPT", "Grok Bot", "Muse", "Hermes Agent", "Instinct"],
        )
        self.assertEqual(text.count('data-surface="web"'), 2)
        self.assertEqual(text.count('data-surface="desktop"'), 2)
        self.assertEqual(text.count(">Web</button>"), 2)
        self.assertEqual(text.count(">Desktop app</button>"), 2)
        self.assertIn('aria-expanded="false"', text)
        self.assertIn('aria-pressed="false"', text)
        self.assertEqual(text.count("You are ChatGPT on the web"), 1)
        self.assertEqual(text.count("You cannot edit a local MCP config"), 1)
        self.assertEqual(text.count("You are the ChatGPT desktop app"), 1)
        self.assertIn("select MCP servers, then Add server", text)
        self.assertIn('command "npx" and args ["-y", "@viajante/mcp"]', text)
        self.assertEqual(text.count("You are Claude on claude.ai"), 1)
        self.assertEqual(text.count("You are Claude Desktop"), 1)
        self.assertIn("claude_desktop_config.json", text)
        self.assertIn("not published as a desktop extension", text)
        self.assertIn("Install in Hermes Agent", text)


if __name__ == "__main__":
    unittest.main()
