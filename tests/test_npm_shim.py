from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "npm"


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version = "([^"]+)"', text)
    if match is None:
        raise AssertionError("pyproject.toml has no version")
    return match.group(1)


class NpmShimTests(unittest.TestCase):
    def test_package_name_bins_and_version_lock(self) -> None:
        pkg = json.loads((NPM / "package.json").read_text(encoding="utf-8"))
        server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        version = _pyproject_version()
        self.assertEqual(pkg["name"], "viajante")
        self.assertEqual(pkg["version"], version)
        self.assertEqual(server["version"], version)
        self.assertEqual(pkg["bin"]["viajante"], "./bin/cli.js")
        self.assertEqual(pkg["bin"]["viajante-mcp"], "./bin/cli.js")
        self.assertTrue((NPM / "bin" / "cli.js").is_file())
        npm_pkg = next(row for row in server["packages"] if row["registryType"] == "npm")
        pypi_pkg = next(row for row in server["packages"] if row["registryType"] == "pypi")
        self.assertEqual(npm_pkg["identifier"], "viajante")
        self.assertEqual(npm_pkg["version"], version)
        self.assertEqual(pypi_pkg["version"], version)

    def test_shim_pins_uvx_to_this_version(self) -> None:
        text = (NPM / "bin" / "cli.js").read_text(encoding="utf-8")
        self.assertIn("viajante[mcp]==${version}", text)
        self.assertIn("viajante==${version}", text)
        self.assertIn('spawn("uvx"', text)
        self.assertNotIn("src/viajante", text)


if __name__ == "__main__":
    unittest.main()
