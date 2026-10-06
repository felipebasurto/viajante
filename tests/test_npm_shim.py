from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import _isolate  # noqa: F401

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
        self.assertEqual(pkg["name"], "@viajante/mcp")
        self.assertEqual(pkg["version"], version)
        self.assertEqual(server["version"], version)
        self.assertEqual(pkg["bin"]["mcp"], "bin/cli.js")
        self.assertEqual(pkg["bin"]["viajante"], "bin/cli.js")
        self.assertEqual(pkg["bin"]["viajante-mcp"], "bin/cli.js")
        self.assertTrue((NPM / "bin" / "cli.js").is_file())
        npm_pkg = next(row for row in server["packages"] if row["registryType"] == "npm")
        pypi_pkg = next(row for row in server["packages"] if row["registryType"] == "pypi")
        self.assertEqual(npm_pkg["identifier"], "@viajante/mcp")
        self.assertEqual(npm_pkg["version"], version)
        self.assertEqual(pypi_pkg["version"], version)
        locked = re.search(
            r'name = "viajante"\nversion = "([^"]+)"',
            (ROOT / "uv.lock").read_text(encoding="utf-8"),
        )
        self.assertIsNotNone(locked)
        self.assertEqual(locked.group(1), version)

    def test_shim_pins_uvx_to_this_version(self) -> None:
        text = (NPM / "bin" / "cli.js").read_text(encoding="utf-8")
        self.assertIn("viajante[mcp]==${version}", text)
        self.assertIn("viajante==${version}", text)
        self.assertIn('spawn("uvx"', text)
        self.assertIn('name !== "viajante"', text)
        self.assertNotIn("src/viajante", text)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_shim_executes_pinned_command_and_preserves_arguments(self) -> None:
        # Intercept child_process at the owned seam: no uvx, network or server.
        script = """
const shim = process.argv[1];
const name = process.argv[2];
require('node:child_process').spawn = (cmd, args, opts) => {
  console.log(JSON.stringify({cmd, args, stdio: opts.stdio}));
  return {on() {}};
};
process.argv = ['node', name, '--help'];
require(shim);
"""
        version = _pyproject_version()
        for name, spec, binary in (
            ("viajante", f"viajante=={version}", "viajante"),
            ("viajante-mcp", f"viajante[mcp]=={version}", "viajante-mcp"),
        ):
            result = subprocess.run(
                ["node", "-e", script, str(NPM / "bin" / "cli.js"), name],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "cmd": "uvx",
                    "args": ["--from", spec, binary, "--help"],
                    "stdio": "inherit",
                },
            )


if __name__ == "__main__":
    unittest.main()
