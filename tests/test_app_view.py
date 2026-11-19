"""The results MCP App: search_flights and search_hotels point at the view, the view is
served, and the view page keeps the evidence rules. Offline.

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
HTML_PATH = ROOT / "src" / "viajante" / "app.html"
VIEW_TOOLS = ("search_flights", "search_hotels")

_PROBE = """
import asyncio, json
from viajante.mcp_server import APP_MIME_TYPE, APP_URI, build_server
server = build_server()
tools = asyncio.run(server.list_tools())
resources = asyncio.run(server.list_resources())
read = list(asyncio.run(server.read_resource(APP_URI)))
print(json.dumps({
    "resource_meta": read[0].meta,
    "uri": APP_URI,
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


class AppViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = _probe()
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_flights_and_hotels_point_at_the_view(self) -> None:
        for name in VIEW_TOOLS:
            meta = self.probe["tools"][name]
            self.assertEqual(meta.get("ui", {}).get("resourceUri"), self.probe["uri"], name)
            # Claude defers MCP tools behind tool search, and a deferred tool renders no view.
            self.assertIs(meta.get("anthropic/alwaysLoad"), True, name)

    def test_other_tools_do_not_claim_the_view(self) -> None:
        for name, meta in self.probe["tools"].items():
            if name not in VIEW_TOOLS:
                self.assertNotIn("ui", meta, name)
                self.assertNotIn("anthropic/alwaysLoad", meta, name)

    def test_hotel_cards_state_only_owned_evidence(self) -> None:
        html = self.html
        # Hotel rows are recognised by their total-stay price basis, never by a guess.
        self.assertIn('price_basis === "total_stay"', html)
        # Free cancellation only from the card's own evidence; a silent card is not confirmed.
        self.assertIn('cancellation_evidence === "free"', html)
        self.assertIn('"Check cancellation"', html)
        # Ratings keep the provider's scale: Google 0-5, Booking 0-10.
        self.assertIn('"google-hotels": { name: "Google Hotels", scale: 5 }', html)
        self.assertIn('"booking.com": { name: "Booking.com", scale: 10 }', html)
        self.assertNotIn("per night", html.casefold())

    def test_the_view_is_served_as_an_mcp_app(self) -> None:
        match = [r for r in self.probe["resources"] if r["uri"] == self.probe["uri"]]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0]["mime"], "text/html;profile=mcp-app")
        self.assertTrue(self.probe["uri"].startswith("ui://"))

    def test_the_view_loads_only_airline_logos_and_hotel_photos(self) -> None:
        html = self.html
        self.assertNotIn("<link", html)
        self.assertNotIn("<iframe", html)
        self.assertIsNone(re.search(r'(?:src|href)="https?://', html))
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        images = ["https://www.gstatic.com", "https://lh3.googleusercontent.com"]
        self.assertEqual(set(re.findall(r"https://[a-z0-9.-]+", html)), set(images))
        self.assertIn("https://www.gstatic.com/flights/airline_logos/", html)
        # A photo URL from any other host is dropped before it becomes an <img>.
        self.assertIn("indexOf(PHOTO_BASE) === 0", html)
        csp = self.probe["resource_meta"]["ui"]["csp"]
        self.assertEqual(csp, {"resourceDomains": images})

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
            # Whole words: the provider name Skiplagged is not advice.
            self.assertIsNone(re.search(rf"\b{banned}\b", html), banned)
        self.assertIn('provider_empty: "', html)
        self.assertIn("filtered_out", html)
        self.assertIn("not_loaded", html)


if __name__ == "__main__":
    unittest.main()
