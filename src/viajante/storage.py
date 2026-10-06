"""External state directory and atomic JSON writes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


def default_state_dir() -> Path:
    env = os.environ.get("VIAJANTE_STATE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "viajante"
    return Path.home() / ".local" / "state" / "viajante"


def reports_payload(result: object) -> dict:
    """One report's JSON, or ``{"queries": [...]}`` for a nearby fan-out tuple."""
    reports = result if isinstance(result, tuple) else (result,)
    if len(reports) == 1:
        return dict(reports[0].to_dict())
    return {"queries": [dict(row.to_dict()) for row in reports]}


def write_json_atomic(payload: Mapping[str, object], destination: Path) -> None:
    write_text_atomic(
        json.dumps(payload, indent=2, ensure_ascii=False),
        destination,
    )


def write_bytes_atomic(data: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=destination.parent,
        prefix=destination.name + ".",
        delete=False,
    )
    tmp = Path(stream.name)
    try:
        with stream:
            stream.write(data)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def write_text_atomic(text: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=destination.parent,
        prefix=destination.name + ".",
        delete=False,
    )
    tmp = Path(stream.name)
    try:
        with stream:
            stream.write(text)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)
