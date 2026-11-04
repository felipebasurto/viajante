"""Live canary: a few real searches through the CLI, so a Google layout change shows up.

Usage: python scripts/canary.py OUTDIR. One run is two searches (a one-way on a route that
always has flights, and a Google hotels search), paced and never retried. A round trip is
not part of it: from the CI runner's IP Google answers its return pages with error status
13 every time (seen on two runs), so it could only ever be red.

It writes one JSON report per case plus summary.json and summary.md to OUTDIR and exits 1
when any case is not a priced success. CI runs it weekly from .github/workflows/canary.yml;
it never gates a pull request.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

PAUSE_SECONDS = 4
CASE_TIMEOUT_SECONDS = 240


def cases(today: date) -> list[tuple[str, list[str]]]:
    out = (today + timedelta(days=30)).isoformat()
    stay_in = (today + timedelta(days=45)).isoformat()
    stay_out = (today + timedelta(days=48)).isoformat()
    return [
        (
            "flights-one-way",
            ["flights", f"JFK-LHR:{out}", "--currency", "USD", "--fetch", "sweep"],
        ),
        (
            "hotels-google",
            ["hotels", "Lisbon", stay_in, stay_out, "--currency", "EUR", "--source", "google"],
        ),
    ]


def outcome_of(report: Optional[dict[str, Any]]) -> tuple[str, Optional[str]]:
    """``("ok", None)`` for a priced success, else ``(label, error code or None)``."""
    if not isinstance(report, dict):
        return "failed", None
    queries = report.get("queries")
    if not isinstance(queries, list) or not queries:
        return "failed", None
    for query in queries:
        if not isinstance(query, dict):
            return "failed", None
        if query.get("status") != "ok":
            error = query.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            return "error", code if isinstance(code, str) else None
        if not query.get("offers"):
            return "no_offers", None
    return "ok", None


def binary() -> str:
    beside = Path(sys.executable).with_name("viajante")
    return str(beside) if beside.exists() else shutil.which("viajante") or str(beside)


def run_case(name: str, args: list[str], outdir: Path) -> dict[str, Any]:
    saved = outdir / f"{name}.json"
    started = time.monotonic()
    note = None
    try:
        subprocess.run(
            [binary(), *args, "--save", str(saved)],
            capture_output=True,
            text=True,
            timeout=CASE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        note = f"no answer within {CASE_TIMEOUT_SECONDS} s"
    report = None
    if saved.exists():
        try:
            report = json.loads(saved.read_text())
        except ValueError:
            note = "unreadable report"
    label, code = outcome_of(report)
    return {
        "case": name,
        "outcome": label,
        "error_code": code,
        "seconds": round(time.monotonic() - started, 1),
        "note": note,
    }


def markdown(rows: list[dict[str, Any]], today: date) -> str:
    lines = [f"Live canary, {today.isoformat()}", "", "| case | outcome | error code | seconds |"]
    lines.append("| --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            f"| {row['case']} | {row['outcome']} | {row['error_code'] or ''} | {row['seconds']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: canary.py OUTDIR", file=sys.stderr)
        return 2
    outdir = Path(argv[1])
    outdir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    rows = []
    for index, (name, args) in enumerate(cases(today)):
        if index:
            time.sleep(PAUSE_SECONDS)
        rows.append(run_case(name, args, outdir))
    (outdir / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    text = markdown(rows, today)
    (outdir / "summary.md").write_text(text)
    print(text, end="")
    return 0 if all(row["outcome"] == "ok" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
