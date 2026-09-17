from __future__ import annotations

import unittest
from pathlib import Path

from viajante.mcp_server import _HELP


class ValidationGuidanceTests(unittest.TestCase):
    def test_skill_requires_evidence_only_itinerary_assembly(self) -> None:
        text = Path(".cursor/skills/viajante/SKILL.md").read_text(encoding="utf-8")
        for phrase in (
            "validate_itinerary",
            "PASS / FAIL / UNKNOWN",
            "needs_bag_verify",
            "empty `segments`",
            "Never move a fare to another date",
            "not found in the tested scope",
            "separate scenario",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_mcp_help_calls_validation_local_and_offline(self) -> None:
        self.assertIn("validate_itinerary", _HELP)
        self.assertIn("local and offline", _HELP)
        self.assertIn("unknown evidence never becomes pass", _HELP)

    def test_mcp_install_doc_lists_validation(self) -> None:
        text = Path(".cursor/skills/viajante/mcp.md").read_text(encoding="utf-8")
        self.assertIn("`validate_itinerary`", text)
        self.assertIn("offline tri-state validation", text)


if __name__ == "__main__":
    unittest.main()
