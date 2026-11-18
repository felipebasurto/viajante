"""Refuse a release whose tag and version pins disagree.

``python scripts/check-release.py v1.6.0`` exits 1 and names every pin that is not
1.6.0. The publish workflow runs it before anything reaches PyPI, npm, or the MCP
Registry, none of which can take a version back.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _lock_version(root: Path) -> str | None:
    text = (root / "uv.lock").read_text(encoding="utf-8")
    match = re.search(r'^name = "viajante"\nversion = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def _pyproject_version(root: Path) -> str | None:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    match = re.search(r'^version = "([^"]+)"', project, re.M)
    return match.group(1) if match else None


def _json(root: Path, name: str) -> dict[str, object]:
    return json.loads((root / name).read_text(encoding="utf-8"))


def _npx_pin(mcp: dict[str, object]) -> str | None:
    """The version in a `@viajante/mcp@X` argument of an MCP config, if it names one."""
    servers = mcp.get("mcpServers", {})
    spec = next(
        (
            arg
            for entry in servers.values()
            for arg in entry.get("args", ())
            if str(arg).startswith("@viajante/mcp@")
        ),
        None,
    )
    return None if spec is None else str(spec).split("@")[-1]


def _bundle_pins(root: Path) -> dict[str, object]:
    """The Claude Desktop extension: its manifest, its own project, and the release it installs."""
    text = (root / "mcpb/pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[-1].split("\n[", 1)[0]
    own = re.search(r'^version = "([^"]+)"', project, re.M)
    dependency = re.search(r'"viajante\[mcp,browser\]==([^"]+)"', project)
    return {
        "mcpb/manifest.json": _json(root, "mcpb/manifest.json").get("version"),
        "mcpb/pyproject.toml": own.group(1) if own else None,
        "mcpb/pyproject.toml viajante[mcp,browser]": dependency.group(1) if dependency else None,
    }


def pins(root: Path = ROOT) -> dict[str, object]:
    """Every place the release version is written down, by file."""
    server = _json(root, "server.json")
    hermes_mcp = _json(root, "integrations/hermes/mcp.json")
    found: dict[str, object] = {
        "pyproject.toml": _pyproject_version(root),
        "uv.lock": _lock_version(root),
        "npm/package.json": json.loads((root / "npm/package.json").read_text(encoding="utf-8"))[
            "version"
        ],
        "server.json": server.get("version"),
        "integrations/hermes/plugin.json": json.loads(
            (root / "integrations/hermes/plugin.json").read_text(encoding="utf-8")
        ).get("version"),
    }
    for index, package in enumerate(server.get("packages", ())):
        found[f"server.json packages[{index}]"] = package.get("version")
    found["integrations/hermes/mcp.json"] = _npx_pin(hermes_mcp)
    found["integrations/claude-code/.claude-plugin/plugin.json"] = _json(
        root, "integrations/claude-code/.claude-plugin/plugin.json"
    ).get("version")
    found["integrations/claude-code/.mcp.json"] = _npx_pin(
        _json(root, "integrations/claude-code/.mcp.json")
    )
    found.update(_bundle_pins(root))
    return found


def check(tag: str, root: Path = ROOT) -> list[str]:
    """Problems with this tag; empty means every pin matches it."""
    match = TAG.match(tag)
    if match is None:
        return [f"tag {tag!r} is not vMAJOR.MINOR.PATCH"]
    version = match.group(1)
    return [
        f"{name} is {value!r}, the tag says {version!r}"
        for name, value in pins(root).items()
        if value != version
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check-release.py vMAJOR.MINOR.PATCH", file=sys.stderr)
        return 2
    problems = check(argv[0])
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if not problems:
        print(f"{argv[0]}: every version pin matches")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
