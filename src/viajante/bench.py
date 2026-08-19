"""Offline keep-or-revert bench: gate plus one score_ms number.

The looping agent reads program.md. This module is the measuring stick:
unittest + ruff must pass, then score_ms is tests_ms + parse_ms of the
checked-in owned shopping / wrb.fr / card-parse corpus. No Chromium.
No live Google unless VIAJANTE_BENCH_LIVE=1, and that extra never
becomes the keep/revert score.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from viajante.flights import DEFAULT_BAGGAGE_BUFFER_EUR, DEFAULT_TOP, search_flights
from viajante.google_flights import NoFlightsFound, parse_flight_cards, parse_http_flight_cards
from viajante.google_flights_rpc import (
    EmptyShoppingResults,
    ShoppingRejected,
    parse_shopping_body,
)
from viajante.models import FlightQuery

LIVE_ENV = "VIAJANTE_BENCH_LIVE"
BENCH_RUNNING_ENV = "VIAJANTE_BENCH_RUNNING"
MANIFEST_NAME = "manifest.json"

# Product defaults the bench must keep honest. Changing these to "win" is a fail.
REQUIRED_FLIGHT_TOP = 8
REQUIRED_BAGGAGE_BUFFER_EUR = 70

# Named owned fixtures. Dropping a name from the manifest is a fail.
REQUIRED_FIXTURE_NAMES = frozenset(
    {
        "shopping-one-stop.wrb",
        "shopping-round-trip.wrb",
        "shopping-midnight-noon.wrb",
        "shopping-tap-layover.wrb",
        "shopping-iberia-hour-only.wrb",
        "shopping-iberia-late-nonstop.wrb",
        "shopping-iberia-fco-late.wrb",
        "shopping-ryanair-fco-late.wrb",
        "shopping-two-stop.wrb",
        "shopping-longhaul-group.wrb",
        "shopping-empty.wrb",
        "shopping-rejected.wrb",
        "cards-best.html",
        "cards-http.html",
        "cards-empty.html",
    }
)

# Floors so emptying or shrinking the corpus cannot "win".
MIN_FIXTURE_FILES = 15
MIN_CORPUS_BYTES = 8_000
MIN_PARSED_CARDS = 14
MIN_FILE_BYTES: Mapping[str, int] = {
    "shopping-one-stop.wrb": 150,
    "shopping-round-trip.wrb": 200,
    "shopping-midnight-noon.wrb": 140,
    "shopping-tap-layover.wrb": 850,
    "shopping-iberia-hour-only.wrb": 500,
    "shopping-iberia-late-nonstop.wrb": 500,
    "shopping-iberia-fco-late.wrb": 500,
    "shopping-ryanair-fco-late.wrb": 500,
    "shopping-two-stop.wrb": 1_100,
    "shopping-longhaul-group.wrb": 1_600,
    "shopping-empty.wrb": 60,
    "shopping-rejected.wrb": 150,
    "cards-best.html": 800,
    "cards-http.html": 350,
    "cards-empty.html": 60,
}


class BenchIntegrityError(RuntimeError):
    """Corpus, defaults, or bench wiring was altered in a way that voids the score."""


@dataclass(frozen=True)
class ParseStats:
    files: int
    bytes_read: int
    cards: int


@dataclass(frozen=True)
class BenchReport:
    gate: str
    tests_ms: Optional[int] = None
    parse_ms: Optional[int] = None
    score_ms: Optional[int] = None
    sweep_ms: Optional[int] = None
    detail: str = ""


def repo_root(start: Optional[Path] = None) -> Path:
    here = start if start is not None else Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").is_file() and (parent / "tests" / "bench").is_dir():
            return parent
    raise BenchIntegrityError("cannot find viajante checkout (pyproject.toml + tests/bench)")


def corpus_dir(root: Optional[Path] = None) -> Path:
    return (root or repo_root()) / "tests" / "bench"


def load_manifest(root: Optional[Path] = None) -> dict[str, Any]:
    path = corpus_dir(root) / MANIFEST_NAME
    if not path.is_file():
        raise BenchIntegrityError(f"missing {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BenchIntegrityError("bench manifest must be a JSON object")
    return data


def _ms_since(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def check_product_defaults() -> None:
    if DEFAULT_TOP != REQUIRED_FLIGHT_TOP:
        raise BenchIntegrityError(
            f"DEFAULT_TOP must stay {REQUIRED_FLIGHT_TOP} (got {DEFAULT_TOP})"
        )
    if DEFAULT_BAGGAGE_BUFFER_EUR != REQUIRED_BAGGAGE_BUFFER_EUR:
        raise BenchIntegrityError(
            "DEFAULT_BAGGAGE_BUFFER_EUR must stay "
            f"{REQUIRED_BAGGAGE_BUFFER_EUR} (got {DEFAULT_BAGGAGE_BUFFER_EUR})"
        )
    top_default = inspect.signature(search_flights).parameters["top"].default
    if top_default != REQUIRED_FLIGHT_TOP:
        raise BenchIntegrityError(
            f"search_flights top default must stay {REQUIRED_FLIGHT_TOP} (got {top_default})"
        )


def validate_corpus(root: Optional[Path] = None) -> list[dict[str, Any]]:
    data = load_manifest(root)
    fixtures = data.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise BenchIntegrityError("bench manifest fixtures must be a non-empty list")
    names = [row.get("name") for row in fixtures if isinstance(row, dict)]
    listed = {name for name in names if isinstance(name, str)}
    missing = REQUIRED_FIXTURE_NAMES - listed
    if missing:
        raise BenchIntegrityError(f"bench manifest dropped required fixtures: {sorted(missing)}")
    extra = listed - REQUIRED_FIXTURE_NAMES
    if extra:
        raise BenchIntegrityError(
            f"bench manifest added undeclared fixtures (empty files cannot win): {sorted(extra)}"
        )
    directory = corpus_dir(root)
    total_bytes = 0
    checked: list[dict[str, Any]] = []
    for row in fixtures:
        if not isinstance(row, dict):
            raise BenchIntegrityError("each fixture entry must be an object")
        name = row.get("name")
        kind = row.get("kind")
        if not isinstance(name, str) or not isinstance(kind, str):
            raise BenchIntegrityError("each fixture needs name and kind")
        path = directory / name
        if not path.is_file():
            raise BenchIntegrityError(f"missing fixture {path}")
        size = path.stat().st_size
        if size <= 0:
            raise BenchIntegrityError(f"empty fixture is forbidden: {name}")
        floor = MIN_FILE_BYTES[name]
        if size < floor:
            raise BenchIntegrityError(f"fixture {name} shrank below {floor} bytes ({size})")
        total_bytes += size
        checked.append({"name": name, "kind": kind, "path": path, "bytes": size})
    if len(checked) < MIN_FIXTURE_FILES:
        raise BenchIntegrityError(
            f"fixture set shrank below {MIN_FIXTURE_FILES} files ({len(checked)})"
        )
    if total_bytes < MIN_CORPUS_BYTES:
        raise BenchIntegrityError(
            f"fixture corpus shrank below {MIN_CORPUS_BYTES} bytes ({total_bytes})"
        )
    return checked


def parse_fixture(kind: str, text: str) -> int:
    if kind == "shopping":
        return len(parse_shopping_body(text))
    if kind == "shopping_empty":
        try:
            parse_shopping_body(text)
        except EmptyShoppingResults:
            return 0
        raise BenchIntegrityError("shopping_empty fixture must raise EmptyShoppingResults")
    if kind == "shopping_rejected":
        try:
            parse_shopping_body(text)
        except ShoppingRejected:
            return 0
        raise BenchIntegrityError("shopping_rejected fixture must raise ShoppingRejected")
    if kind == "html":
        return len(parse_flight_cards(text))
    if kind == "html_http":
        return len(parse_http_flight_cards(text))
    if kind == "html_empty":
        try:
            cards = parse_flight_cards(text)
        except NoFlightsFound:
            return 0
        if cards:
            raise BenchIntegrityError("html_empty fixture must yield no cards")
        raise BenchIntegrityError("html_empty fixture must be an owned empty state")
    raise BenchIntegrityError(f"unknown fixture kind: {kind}")


def parse_corpus(root: Optional[Path] = None) -> ParseStats:
    fixtures = validate_corpus(root)
    cards = 0
    bytes_read = 0
    for row in fixtures:
        path: Path = row["path"]
        text = path.read_text(encoding="utf-8")
        bytes_read += len(text.encode("utf-8"))
        found = parse_fixture(row["kind"], text)
        if row["kind"] in {"shopping", "html", "html_http"} and found < 1:
            raise BenchIntegrityError(f"fixture {row['name']} parsed to zero cards")
        cards += found
    if cards < MIN_PARSED_CARDS:
        raise BenchIntegrityError(f"parsed {cards} cards; need at least {MIN_PARSED_CARDS}")
    return ParseStats(files=len(fixtures), bytes_read=bytes_read, cards=cards)


def format_report(report: BenchReport) -> str:
    lines = [f"gate: {report.gate}"]
    if report.gate == "ok":
        assert report.tests_ms is not None
        assert report.parse_ms is not None
        assert report.score_ms is not None
        lines.append(f"tests_ms: {report.tests_ms}")
        lines.append(f"parse_ms: {report.parse_ms}")
        lines.append(f"score_ms: {report.score_ms}")
        if report.sweep_ms is not None:
            lines.append(f"sweep_ms: {report.sweep_ms}")
    return "\n".join(lines) + "\n"


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def run_gate(root: Path) -> tuple[bool, str, Optional[int]]:
    if os.environ.get(BENCH_RUNNING_ENV) == "1":
        return False, "viajante bench cannot nest inside itself", None
    env = os.environ.copy()
    env[BENCH_RUNNING_ENV] = "1"
    env.pop(LIVE_ENV, None)
    python = sys.executable
    checks = (
        ([python, "-m", "ruff", "check", "src", "tests"], "ruff check"),
        ([python, "-m", "ruff", "format", "--check", "src", "tests"], "ruff format"),
        ([python, "-m", "unittest", "discover", "-s", "tests"], "unittest"),
    )
    tests_ms: Optional[int] = None
    logs: list[str] = []
    for argv, label in checks:
        started = time.perf_counter()
        result = _run_command(argv, cwd=root, env=env)
        elapsed = _ms_since(started)
        if label == "unittest":
            tests_ms = elapsed
        output = result.stdout or ""
        if result.returncode != 0:
            logs.append(f"{label} failed (exit {result.returncode})\n{output}")
            return False, "\n".join(logs), tests_ms
    return True, "", tests_ms


def maybe_live_sweep() -> Optional[int]:
    if os.environ.get(LIVE_ENV) != "1":
        return None
    query = FlightQuery(
        "MAD",
        "BCN",
        date.today() + timedelta(days=21),
        max_stops=0,
    )
    started = time.perf_counter()
    search_flights((query,), top=1, fetch="sweep", buffer_eur=0)
    return _ms_since(started)


def run_bench(*, root: Optional[Path] = None) -> int:
    try:
        checkout = root or repo_root()
        check_product_defaults()
        validate_corpus(checkout)
    except BenchIntegrityError as exc:
        print(format_report(BenchReport(gate="fail", detail=str(exc))), end="")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    ok, detail, tests_ms = run_gate(checkout)
    if not ok:
        print(format_report(BenchReport(gate="fail", detail=detail)), end="")
        if detail:
            print(detail, file=sys.stderr)
        return 1

    try:
        started = time.perf_counter()
        parse_corpus(checkout)
        parse_ms = _ms_since(started)
    except BenchIntegrityError as exc:
        print(format_report(BenchReport(gate="fail", detail=str(exc))), end="")
        print(f"error: {exc}", file=sys.stderr)
        return 1

    sweep_ms: Optional[int] = None
    if os.environ.get(LIVE_ENV) == "1":
        try:
            sweep_ms = maybe_live_sweep()
        except Exception as exc:
            print(f"warning: live sweep skipped: {exc}", file=sys.stderr)

    assert tests_ms is not None
    report = BenchReport(
        gate="ok",
        tests_ms=tests_ms,
        parse_ms=parse_ms,
        score_ms=tests_ms + parse_ms,
        sweep_ms=sweep_ms,
    )
    print(format_report(report), end="")
    return 0
