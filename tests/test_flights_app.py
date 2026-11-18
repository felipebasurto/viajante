"""The search_flights MCP App: the tool points at its view, the view is served, and the
view page keeps the evidence rules. Offline.

The SDK is imported in a subprocess so this module never leaves `mcp` loaded for the other
tests, which check that the SDK stays unimported until they need it.
"""

from __future__ import annotations

import _isolate  # noqa: F401
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "src" / "viajante" / "flights_app.html"

_PROBE = """
import asyncio, json
from viajante.mcp_server import APP_MIME_TYPE, FLIGHTS_APP_URI, build_server
server = build_server()
tools = asyncio.run(server.list_tools())
resources = asyncio.run(server.list_resources())
read = list(asyncio.run(server.read_resource(FLIGHTS_APP_URI)))
print(json.dumps({
    "resource_meta": read[0].meta,
    "uri": FLIGHTS_APP_URI,
    "mime": APP_MIME_TYPE,
    "tools": {t.name: (t.meta or {}) for t in tools},
    "resources": [{"uri": str(r.uri), "mime": r.mime_type} for r in resources],
}))
"""


def _probe() -> dict:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


class FlightsAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = _probe()
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_search_flights_points_at_the_view(self) -> None:
        meta = self.probe["tools"]["search_flights"]
        self.assertEqual(meta.get("ui", {}).get("resourceUri"), self.probe["uri"])

    def test_other_tools_do_not_claim_the_view(self) -> None:
        for name, meta in self.probe["tools"].items():
            if name != "search_flights":
                self.assertNotIn("ui", meta, name)

    def test_the_view_is_served_as_an_mcp_app(self) -> None:
        match = [r for r in self.probe["resources"] if r["uri"] == self.probe["uri"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["mime"], "text/html;profile=mcp-app")
        self.assertTrue(self.probe["uri"].startswith("ui://"))

    def test_the_view_has_no_network_and_loads_only_airline_logos(self) -> None:
        html = self.html
        self.assertNotIn("<link", html)
        self.assertNotIn("<iframe", html)
        self.assertIsNone(re.search(r'(?:src|href)="https?://', html))
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        origins = set(re.findall(r"https://[a-z0-9.-]+", html))
        self.assertEqual(origins, {"https://www.gstatic.com"})
        self.assertIn("https://www.gstatic.com/flights/airline_logos/", html)
        csp = self.probe["resource_meta"]["ui"]["csp"]
        self.assertEqual(csp, {"resourceDomains": ["https://www.gstatic.com"]})

    def test_the_view_speaks_the_mcp_apps_handshake(self) -> None:
        html = self.html
        self.assertIn('method: "ui/initialize"', html)
        self.assertIn('"ui/notifications/initialized"', html)
        self.assertIn('"ui/notifications/tool-result"', html)
        self.assertIn("event.source !== window.parent", html)
        # Without size-changed the host keeps the iframe at its small default height.
        self.assertIn('"ui/notifications/size-changed"', html)
        self.assertIn('"ui/open-link"', html)

    def test_the_view_keeps_the_design_stance(self) -> None:
        # No recommendation prose and no "none found" wording outside provider_empty.
        html = self.html.casefold()
        for banned in ("cheapest", "recommend", "best deal", "skip", "no flights found"):
            self.assertNotIn(banned, html, banned)
        self.assertIn('provider_empty: "', html)
        self.assertIn("filtered_out", html)
        self.assertIn("not_loaded", html)


if __name__ == "__main__":
    unittest.main()
