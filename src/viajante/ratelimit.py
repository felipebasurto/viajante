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


def _read_rate_limit(file: str) -> Optional[dict]:
    try:
        state = json.loads((default_state_dir() / file).read_text(encoding="utf-8"))
        return state if all(isinstance(state[k], (int, float)) for k in ("at", "until")) else None
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
) -> dict:
    """Record a real rate limit in the state dir so the next search in any process waits."""
    current = time.time() if now is None else now
    previous = _read_rate_limit(file)
    if previous is not None and previous["until"] > current:
        return previous
    cooldown = RATE_LIMIT_COOLDOWN_SECONDS
    if retry_after is not None and retry_after > 0:
        cooldown = retry_after
    elif previous is not None and current < previous["until"] + previous.get("cooldown_s", 0):
        cooldown = min(RATE_LIMIT_MAX_COOLDOWN_SECONDS, previous["cooldown_s"] * 2)
    state = {"at": current, "until": current + cooldown, "cooldown_s": cooldown}
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
    return (
        f"{prefix}{provider} is rate-limiting this machine ({reason} at {clock(state['at'])} UTC). "
        f"Viajante pauses {provider} searches until {clock(state['until'])} UTC (~{minutes} min). "
        "Tell the user to wait; do not retry or switch fetch mode."
    )
