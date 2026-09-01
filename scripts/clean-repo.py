#!/usr/bin/env python3
"""Remove known-safe junk from the viajante checkout.

Dry-run by default. Pass ``--write`` to delete. Never traverses ``.git``.
Secret-like names (``.env`` except ``.env.example``, ``*.pem``, ``*.key``)
are reported and never deleted. ``--include-build`` and ``--include-deps``
stay off unless named.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level only; off unless --include-deps.
DEP_DIRS = frozenset({".venv", "venv", "node_modules", "__pypackages__"})

# Top-level only; off unless --include-build.
BUILD_DIRS = frozenset(
    {
        "build",
        "dist",
        "htmlcov",
        ".tox",
        ".nox",
        ".eggs",
        "playwright-report",
        "test-results",
    }
)

# Remove these directories wherever they appear (not inside .git).
JUNK_DIRS = {
    "__macosx": "macOS zip metadata",
    "__pycache__": "Python bytecode cache",
    ".pytest_cache": "pytest cache",
    ".mypy_cache": "mypy cache",
    ".ruff_cache": "ruff cache",
}

OS_METADATA_NAMES = frozenset(
    {
        ".ds_store",
        "thumbs.db",
        "ehthumbs.db",
        "desktop.ini",
        "icon\r",
    }
)


def is_secret_like(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".pem") or lower.endswith(".key"):
        return True
    if lower == ".env" or lower.startswith(".env."):
        return lower != ".env.example"
    return False


def junk_file_reason(name: str) -> str | None:
    lower = name.lower()
    if lower in OS_METADATA_NAMES or lower.startswith("._"):
        return "OS metadata"
    if lower.endswith("~") or lower.endswith((".swp", ".swo", ".swn")):
        return "editor temp"
    if lower.endswith((".orig", ".bak")):
        return "editor temp"
    if len(name) > 2 and name.startswith("#") and name.endswith("#"):
        return "editor temp"
    if lower.endswith(".log"):
        return "log file"
    if lower.endswith((".pyc", ".pyo")):
        return "Python bytecode"
    return None


def build_file_reason(name: str) -> str | None:
    lower = name.lower()
    if lower == ".coverage" or lower.startswith(".coverage."):
        return "coverage data"
    if lower == "coverage.xml":
        return "coverage data"
    if lower.endswith(".egg"):
        return "build artifact"
    return None


def scan(
    root: Path,
    *,
    include_build: bool,
    include_deps: bool,
) -> tuple[list[tuple[Path, str]], list[Path]]:
    """Return (junk paths with reasons, secret-like paths). Never walks .git."""
    removals: list[tuple[Path, str]] = []
    secrets: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        try:
            rel = current.relative_to(root)
        except ValueError:
            dirnames[:] = []
            continue
        at_root = rel == Path(".")
        keep: list[str] = []
        for name in dirnames:
            if name == ".git":
                continue
            child = current / name
            key = name.lower()
            if at_root and name in DEP_DIRS:
                if include_deps:
                    removals.append((child, "dependency tree"))
                continue
            if at_root and name in BUILD_DIRS:
                if include_build:
                    removals.append((child, "build artifact"))
                continue
            if include_build and name.endswith(".egg-info"):
                removals.append((child, "Python egg-info"))
                continue
            reason = JUNK_DIRS.get(key)
            if reason:
                removals.append((child, reason))
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in filenames:
            path = current / name
            if is_secret_like(name):
                secrets.append(path)
                continue
            reason = junk_file_reason(name)
            if reason is None and include_build:
                reason = build_file_reason(name)
            if reason:
                removals.append((path, reason))

    removals.sort(key=lambda item: str(item[0]))
    secrets.sort(key=lambda path: str(path))
    return removals, secrets


def relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def protected_by_secret(path: Path, secrets: list[Path]) -> bool:
    for secret in secrets:
        try:
            if path == secret or secret.is_relative_to(path) or path.is_relative_to(secret):
                return True
        except ValueError:
            continue
    return False


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove known-safe junk from the viajante checkout (dry-run default)."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Delete matched junk. Default is dry-run (print only).",
    )
    parser.add_argument(
        "--include-build",
        action="store_true",
        help="Also remove top-level build artifacts (dist, egg-info, coverage). Default off.",
    )
    parser.add_argument(
        "--include-deps",
        action="store_true",
        help="Also remove top-level dependency trees (.venv, node_modules). Default off.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = REPO_ROOT
    if not (root / "pyproject.toml").is_file():
        print("error: cannot find repo root (pyproject.toml)", file=sys.stderr)
        return 2

    mode = "write" if args.write else "dry-run"
    print(f"clean-repo: {mode}  root={root}")
    if not args.write:
        print("pass --write to delete; --include-build / --include-deps stay off unless named")

    removals, secrets = scan(
        root,
        include_build=args.include_build,
        include_deps=args.include_deps,
    )

    for path, reason in removals:
        shown = relpath(root, path)
        if protected_by_secret(path, secrets):
            print(f"skip         {shown}  ({reason}; contains secret-like name)")
            continue
        if args.write:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as exc:
                print(f"error        {shown}  ({exc})", file=sys.stderr)
                return 2
            print(f"remove       {shown}  ({reason})")
        else:
            print(f"would remove {shown}  ({reason})")

    for path in secrets:
        print(f"secret       {relpath(root, path)}  (secret-like filename; not deleted)")

    n_remove = sum(1 for path, _ in removals if not protected_by_secret(path, secrets))
    verb = "removed" if args.write else "would remove"
    print(f"summary: {verb} {n_remove}; secrets {len(secrets)}")
    return 1 if secrets else 0


if __name__ == "__main__":
    raise SystemExit(main())
