"""External state directory, locking, and atomic writes shared by the state files."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Iterator, Mapping, Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]


class UnreadableStateError(OSError):
    """A state file exists but cannot be read or parsed. Never the same as an empty state.

    ``str()`` is the reason, phrased without paths so it can reach an MCP client.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def default_state_dir() -> Path:
    env = os.environ.get("VIAJANTE_STATE_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "viajante"
    return Path.home() / ".local" / "state" / "viajante"


def read_optional_bytes(path: Path) -> Optional[bytes]:
    """The file's bytes, or None when it does not exist. Any other read error raises."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def read_failure_reason(exc: OSError, subject: str, *, writing: bool = False) -> str:
    """Why a state file could not be read or written, phrased for an MCP client."""
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"permission denied {'writing' if writing else 'accessing'} {subject}"
    verb = "write" if writing else "access"
    return f"could not {verb} {subject} ({exc.strerror or type(exc).__name__})"


def reports_payload(result: object) -> dict:
    """One report's JSON, or ``{"queries": [...]}`` for a nearby fan-out tuple."""
    reports = result if isinstance(result, tuple) else (result,)
    if len(reports) == 1:
        return dict(reports[0].to_dict())
    return {"queries": [dict(row.to_dict()) for row in reports]}


@contextlib.contextmanager
def exclusive_lock(data_file: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``<data_file>.lock`` so read-modify-write cycles never interleave.

    Works across threads and processes (flock on POSIX, msvcrt.locking on Windows). Where
    neither is available this is a documented no-op: two writers at the same instant can
    then lose an update. The lock file (mode 0600, like the data files) stays in place and
    holds no data.
    """
    data_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = data_file.with_name(data_file.name + ".lock")
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows only
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def write_bytes_atomic(data: bytes, destination: Path) -> None:
    """Write through a temp file beside the target, fsync, then rename over it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=destination.name + ".", delete=False
    )
    tmp = Path(stream.name)
    try:
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def write_text_atomic(text: str, destination: Path) -> None:
    write_bytes_atomic(text.encode("utf-8"), destination)


def write_json_atomic(payload: Mapping[str, object], destination: Path) -> None:
    write_text_atomic(json.dumps(payload, indent=2, ensure_ascii=False), destination)
