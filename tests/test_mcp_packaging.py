"""The MCP packages beside the PyPI release stay in step with the server they start.

Offline. Reads the checked-in manifests, the Dockerfile and the registered tools.
"""

from __future__ import annotations

import _isolate  # noqa: F401
import asyncio
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class DesktopExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _json("mcpb/manifest.json")

    def test_it_runs_the_pypi_package_through_uv(self) -> None:
        server = self.manifest["server"]
        self.assertEqual(self.manifest["manifest_version"], "0.4")
        self.assertEqual(server["type"], "uv")
        self.assertTrue((ROOT / "mcpb" / server["entry_point"]).is_file())
        self.assertEqual(server["mcp_config"]["command"], "uv")
        self.assertIn(server["entry_point"], server["mcp_config"]["args"])
        entry = (ROOT / "mcpb" / server["entry_point"]).read_text(encoding="utf-8")
        self.assertIn("from viajante.mcp_server import main", entry)

    def test_it_installs_the_same_extras_as_the_npx_shim(self) -> None:
        shim = (ROOT / "npm/bin/cli.js").read_text(encoding="utf-8")
        self.assertIn("viajante[mcp,browser]==", shim)
        project = (ROOT / "mcpb/pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"viajante[mcp,browser]==', project)

    def test_it_lists_exactly_the_tools_the_server_registers(self) -> None:
        try:
            from viajante.mcp_server import build_server
        except ImportError:
            self.skipTest("the mcp extra is not installed")
        registered = {tool.name for tool in asyncio.run(build_server().list_tools())}
        listed = [tool["name"] for tool in self.manifest["tools"]]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed), registered)
        self.assertFalse(self.manifest["tools_generated"])
        for tool in self.manifest["tools"]:
            self.assertTrue(tool["description"].strip(), tool["name"])

    def test_every_description_is_a_name_not_a_verdict(self) -> None:
        text = json.dumps(self.manifest["tools"]).casefold()
        for word in ("cheapest flight", "best", "recommend"):
            self.assertNotIn(word, text)


class ClaudeCodePluginTests(unittest.TestCase):
    def test_the_marketplace_names_the_plugin_folder(self) -> None:
        market = _json(".claude-plugin/marketplace.json")
        (entry,) = market["plugins"]
        folder = ROOT / entry["source"]
        plugin = json.loads((folder / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["name"], plugin["name"])
        self.assertTrue((folder / ".mcp.json").is_file())
        servers = json.loads((folder / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(list(servers), ["viajante"])

    def test_the_skill_is_the_one_hermes_ships(self) -> None:
        plugin = ROOT / "integrations/claude-code/skills/viajante/SKILL.md"
        hermes = ROOT / "integrations/hermes/skills/viajante/SKILL.md"
        self.assertEqual(plugin.read_bytes(), hermes.read_bytes())


class ContainerImageTests(unittest.TestCase):
    def test_the_entrypoint_is_the_console_script_and_the_copies_exist(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        scripts = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r'ENTRYPOINT \["viajante-mcp"\]')
        self.assertRegex(scripts, r'(?m)^viajante-mcp = "viajante\.mcp_server:main"$')
        for line in re.findall(r"^COPY (.+) \./?$", dockerfile, re.M):
            for name in line.split():
                if name != "src":
                    self.assertTrue((ROOT / name).is_file(), name)
        self.assertIn("src", dockerfile)

    def test_the_image_installs_this_checkout_not_a_pinned_release(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn('pip install --no-cache-dir ".[mcp]"', dockerfile)
        self.assertNotIn("viajante==", dockerfile)


if __name__ == "__main__":
    unittest.main()
