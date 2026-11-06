"""Per-provider cooldown state: one recorded rate limit pauses that provider machine-wide.

The state file lives in the shared state directory, so every viajante install on the
machine honours it. Each record names the viajante version and the endpoint that wrote
it, so a stale install's limit can be told apart from this one's.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from typing import Mapping, Optional
from urllib.parse import urlsplit

from viajante.runtime import package_version
from viajante.storage import default_state_dir, exclusive_lock, write_json_atomic

GOOGLE_RATE_LIMIT_FILE = "google-rate-limit.json"
SKIPLAGGED_RATE_LIMIT_FILE = "skiplagged-rate-limit.json"
# ponytail: neither provider publishes a quota. The cooldown is a guess: 2 min, or the
# Retry-After the provider names (at most 30 min). Upgrade: learn it from recoveries.
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
_CAUSE_OF_BASIS = {
    "provider_retry_after": "http_429",
    "heuristic_http_429": "http_429",
    "heuristic_rpc_13": "rpc_13",
    "unknown": "unknown",
}


def _read_rate_limit(file: str, now: float) -> Optional[dict]:
    try:
        state = json.loads((default_state_dir() / file).read_text(encoding="utf-8"))
        if not all(isinstance(state[k], (int, float)) for k in ("at", "until")):
            return None
        at, until = float(state["at"]), float(state["until"])
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return None
    # A stamp more than one cap from now would hold the provider off for good or overflow the
    # clock format. NaN and inf fail these comparisons too; the span check bounds `at`.
    cap = RATE_LIMIT_MAX_COOLDOWN_SECONDS
    if not now - cap <= until <= now + cap:
        return None
    if not 0 <= until - at <= cap:
        return None
    # Records written before these fields existed stay readable: their provenance is
    # unknown rather than guessed.
    state = dict(state)
    # isinstance first: an unhashable basis (a list or dict) would make the set lookup raise.
    if not isinstance(state.get("basis"), str) or state["basis"] not in _COOLDOWN_BASES:
        state["basis"] = "unknown"
    if state.get("cause") not in _CAUSE_OF_BASIS.values():
        state["cause"] = "unknown"
    for key in ("viajante_version", "endpoint"):
        if not isinstance(state.get(key), str):
            state[key] = None
    # A cooldown length that is not a number in range is unknown: 0, as for records without one.
    if not (isinstance(state.get("cooldown_s"), (int, float)) and 0 <= state["cooldown_s"] <= cap):
        state["cooldown_s"] = 0
    return state


def endpoint_label(endpoint: str) -> str:
    """Host and path only: a scheme, query, or fragment never reaches the state file."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    return f"{parts.netloc}{parts.path}"


def rate_limit_status(
    now: Optional[float] = None, *, file: str = GOOGLE_RATE_LIMIT_FILE
) -> Optional[dict]:
    """The recorded cooldown for this machine while it runs, else None."""
    current = time.time() if now is None else now
    state = _read_rate_limit(file, current)
    return state if state is not None and state["until"] > current else None


def note_rate_limited(
    retry_after: Optional[float] = None,
    now: Optional[float] = None,
    *,
    file: str = GOOGLE_RATE_LIMIT_FILE,
    basis: Optional[str] = None,
    cause: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict:
    """Record a real rate limit in the state dir so the next search in any process waits."""
    if retry_after is not None and not math.isfinite(retry_after):
        retry_after = None
    if basis is None:
        basis = (
            "provider_retry_after"
            if retry_after is not None and retry_after > 0
            else "heuristic_http_429"
        )
    if basis not in _COOLDOWN_BASES:
        raise ValueError(f"invalid cooldown basis: {basis!r}")
    cause = _CAUSE_OF_BASIS[basis] if cause is None else cause
    if cause not in _CAUSE_OF_BASIS.values():
        raise ValueError(f"invalid cooldown cause: {cause!r}")
    current = time.time() if now is None else now
    cooldown = RATE_LIMIT_COOLDOWN_SECONDS
    if retry_after is not None and retry_after > 0:
        cooldown = min(retry_after, RATE_LIMIT_MAX_COOLDOWN_SECONDS)
    state = {
        "at": current,
        "until": current + cooldown,
        "cooldown_s": cooldown,
        "basis": basis,
        "cause": cause,
        "viajante_version": package_version(),
        "endpoint": endpoint_label(endpoint) if endpoint else None,
    }
    # A running cooldown stands against a guess: a burst of bare 429s never extends it. Only a
    # named Retry-After can move it, and only later; a shorter named one never shortens it.
    try:
        path = default_state_dir() / file
        with exclusive_lock(path):
            previous = _read_rate_limit(file, current)
            if previous is not None and previous["until"] > current:
                named_and_later = (
                    basis == "provider_retry_after" and state["until"] > previous["until"]
                )
                if not named_and_later:
                    return {**previous, "persisted": True}
            write_json_atomic(state, path)
        return {**state, "persisted": True}
    except OSError:
        previous = _read_rate_limit(file, current)
        if previous is not None and previous["until"] > current:
            return {**previous, "persisted": True}
        return {**state, "persisted": False}


def recorded_expiry(state: Mapping[str, object]) -> Optional[float]:
    """Only a persisted cooldown can pause a later process."""
    return float(state["until"]) if state.get("persisted", True) else None


def _provenance(state: Mapping[str, object]) -> str:
    version = state.get("viajante_version")
    current = package_version()
    source = f"viajante {version}" if version else "a viajante that does not record its version"
    where = f" from {state['endpoint']}" if state.get("endpoint") else ""
    if version == current:
        return f"; recorded by {source}{where}"
    return (
        f"; recorded by {source}{where}, not by this viajante {current} "
        "(another install on this machine may have caused it)"
    )


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
    if state.get("persisted") is False:
        return (
            f"{prefix}{observation} ({reason}). Viajante could not save the cooldown. "
            "Tell the user to wait; do not retry or switch fetch mode."
        )
    return (
        f"{prefix}{observation} ({reason} at {clock(state['at'])} UTC{_provenance(state)}). "
        f"Viajante pauses {provider} {PAUSE_PHRASE} {clock(state['until'])} UTC (~{minutes} min). "
        "Tell the user to wait; do not retry or switch fetch mode."
    )
