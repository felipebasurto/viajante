from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
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
        hermes = ROOT / "integrations" / "hermes"
        plugin = json.loads((hermes / "plugin.json").read_text(encoding="utf-8"))
        hermes_mcp = json.loads((hermes / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["version"], version)
        self.assertEqual(
            hermes_mcp["mcpServers"]["viajante"]["args"], ["-y", f"@viajante/mcp@{version}"]
        )
        self.assertEqual(pkg["bin"]["mcp"], "bin/mcp.js")
        self.assertEqual(pkg["bin"]["viajante"], "bin/viajante.js")
        self.assertEqual(pkg["bin"]["viajante-mcp"], "bin/mcp.js")
        self.assertTrue((NPM / "bin" / "cli.js").is_file())
        self.assertTrue((NPM / "bin" / "viajante.js").is_file())
        self.assertTrue((NPM / "bin" / "mcp.js").is_file())
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
        self.assertIn("viajante[mcp,browser]==${version}", text)
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
            ("viajante-mcp", f"viajante[mcp,browser]=={version}", "viajante-mcp"),
            ("mcp", f"viajante[mcp,browser]=={version}", "viajante-mcp"),
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

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_js_entries_stay_cli_or_mcp_when_argv_is_the_file(self) -> None:
        # npm on Windows runs `node bin/<file>.js`, so argv[1] is never the bin name.
        version = _pyproject_version()
        with tempfile.TemporaryDirectory() as tmp:
            uvx = Path(tmp) / "uvx"
            uvx.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
            uvx.chmod(uvx.stat().st_mode | stat.S_IEXEC)
            env = os.environ.copy()
            env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
            env.pop("VIAJANTE_BIN", None)
            cases = (
                ("viajante.js", f"viajante=={version}", "viajante"),
                ("mcp.js", f"viajante[mcp,browser]=={version}", "viajante-mcp"),
            )
            for entry, spec, binary in cases:
                result = subprocess.run(
                    ["node", str(NPM / "bin" / entry), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), f"--from {spec} {binary} --version")

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_missing_uv_names_uv_and_python(self) -> None:
        node = shutil.which("node")
        assert node is not None
        env = os.environ.copy()
        env["PATH"] = str(Path(node).parent)
        env.pop("VIAJANTE_BIN", None)
        result = subprocess.run(
            [node, str(NPM / "bin" / "viajante.js")],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("uv on PATH", result.stderr)
        self.assertIn("Python 3.10+", result.stderr)


if __name__ == "__main__":
    unittest.main()
