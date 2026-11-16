"""Refuse a release whose tag and version pins disagree.

``python scripts/check-release.py v1.5.7`` exits 1 and names every pin that is not
1.5.7. The publish workflow runs it before anything reaches PyPI, npm, or the MCP
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


def pins(root: Path = ROOT) -> dict[str, object]:
    """Every place the release version is written down, by file."""
    server = json.loads((root / "server.json").read_text(encoding="utf-8"))
    hermes_mcp = json.loads((root / "integrations/hermes/mcp.json").read_text(encoding="utf-8"))
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
    spec = next(
        (
            arg
            for entry in hermes_mcp.get("mcpServers", {}).values()
            for arg in entry.get("args", ())
            if str(arg).startswith("@viajante/mcp@")
        ),
        None,
    )
    found["integrations/hermes/mcp.json"] = None if spec is None else str(spec).split("@")[-1]
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
