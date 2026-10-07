"""Per-provider cooldown state: one recorded rate limit pauses that provider machine-wide."""

from __future__ import annotations

import contextlib
import json
import math
import time
from datetime import datetime, timezone
from typing import Mapping, Optional

from viajante.storage import default_state_dir, write_json_atomic

GOOGLE_RATE_LIMIT_FILE = "google-rate-limit.json"
SKIPLAGGED_RATE_LIMIT_FILE = "skiplagged-rate-limit.json"
# ponytail: neither provider publishes a quota. The cooldown is a guess: 2 min, doubling per
# repeat limit up to 30 min, unless Retry-After names one. Upgrade: learn it from recoveries.
RATE_LIMIT_COOLDOWN_SECONDS = 120.0
RATE_LIMIT_MAX_COOLDOWN_SECONDS = 1800.0
NOT_SENT = "Not sent. "
PAUSE_PHRASE = "searches until"
_COOLDOWN_BASES = {
    "provider_retry_after",
    "heuristic_http_429",
    "heuristic_rpc_13",
    "unknown",
}
_COOLDOWN_CAUSES = {"http_429", "rpc_13", "unknown"}


def _read_rate_limit(file: str) -> Optional[dict]:
    try:
        state = json.loads((default_state_dir() / file).read_text(encoding="utf-8"))
        if not all(isinstance(state[k], (int, float)) for k in ("at", "until")):
            return None
        # Older state files predate these diagnostic fields. Keep them readable
        # and identify their provenance as unknown rather than guessing.
        state = dict(state)
        if state.get("basis") not in _COOLDOWN_BASES:
            state["basis"] = "unknown"
        if state.get("cause") not in _COOLDOWN_CAUSES:
            state["cause"] = "unknown"
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return None


def rate_limit_status(
    now: Optional[float] = None, *, file: str = GOOGLE_RATE_LIMIT_FILE
) -> Optional[dict]:
    """The recorded cooldown for this machine while it runs, else None."""
    state = _read_rate_limit(file)
    current = time.time() if now is None else now
    return state if state is not None and state["until"] > current else None


def note_rate_limited(
    retry_after: Optional[float] = None,
    now: Optional[float] = None,
    *,
    file: str = GOOGLE_RATE_LIMIT_FILE,
    basis: Optional[str] = None,
    cause: Optional[str] = None,
) -> dict:
    """Record a real rate limit in the state dir so the next search in any process waits."""
    if basis is None:
        basis = (
            "provider_retry_after"
            if retry_after is not None and retry_after > 0
            else "heuristic_http_429"
        )
    if basis not in _COOLDOWN_BASES:
        raise ValueError(f"invalid cooldown basis: {basis!r}")
    if cause is None:
        cause = {
            "provider_retry_after": "http_429",
            "heuristic_http_429": "http_429",
            "heuristic_rpc_13": "rpc_13",
            "unknown": "unknown",
        }[basis]
    if cause not in _COOLDOWN_CAUSES:
        raise ValueError(f"invalid cooldown cause: {cause!r}")
    current = time.time() if now is None else now
    previous = _read_rate_limit(file)
    if previous is not None and previous["until"] > current:
        return previous
    cooldown = RATE_LIMIT_COOLDOWN_SECONDS
    if retry_after is not None and retry_after > 0:
        cooldown = retry_after
    elif previous is not None and current < previous["until"] + previous.get("cooldown_s", 0):
        cooldown = min(RATE_LIMIT_MAX_COOLDOWN_SECONDS, previous["cooldown_s"] * 2)
    state = {
        "at": current,
        "until": current + cooldown,
        "cooldown_s": cooldown,
        "basis": basis,
        "cause": cause,
    }
    with contextlib.suppress(OSError):
        write_json_atomic(state, default_state_dir() / file)
    return state


def rate_limit_advice(
    state: Mapping[str, float],
    *,
    sent: bool = True,
    provider: str = "Google",
    reason: str = "HTTP 429",
) -> str:
    def clock(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M")

    minutes = max(1, math.ceil((state["until"] - time.time()) / 60))
    prefix = "" if sent else NOT_SENT
    if state.get("cause") == "rpc_13":
        observation = f"{provider} returned RPC status 13; its cause is unknown"
    else:
        observation = f"{provider} is rate-limiting this machine"
    return (
        f"{prefix}{observation} ({reason} at {clock(state['at'])} UTC). "
        f"Viajante pauses {provider} {PAUSE_PHRASE} {clock(state['until'])} UTC (~{minutes} min). "
        "Tell the user to wait; do not retry or switch fetch mode."
    )


def cooldown_until(message: str, file: str = GOOGLE_RATE_LIMIT_FILE) -> Optional[float]:
    """End of the recorded cooldown, only when this failure carries its advice.

    A proxied 429 records no cooldown, so a direct cooldown that happens to be running
    must not be attributed to it.
    """
    if PAUSE_PHRASE not in message:
        return None
    state = rate_limit_status(file=file)
    return None if state is None else float(state["until"])
