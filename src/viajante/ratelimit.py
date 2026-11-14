"""Per-provider cooldown state: one recorded rate limit pauses that provider machine-wide.

The state file lives in the shared state directory, so every viajante install on the
machine honours it. Each record names the viajante version and the endpoint that wrote
it, so a stale install's limit can be told apart from this one's.
"""

from __future__ import annotations

import json
import math
import threading
import time
from datetime import datetime, timezone
from typing import Mapping, Optional
from urllib.parse import urlsplit

from viajante.runtime import package_version
from viajante.storage import default_state_dir, exclusive_lock, write_json_atomic

GOOGLE_RATE_LIMIT_FILE = "google-rate-limit.json"
GOOGLE_THROTTLE_HITS_FILE = "google-throttle-hits.json"
SKIPLAGGED_RATE_LIMIT_FILE = "skiplagged-rate-limit.json"
# ponytail: neither provider publishes a quota. The cooldown is a guess: 2 min, or the
# Retry-After the provider names (at most 30 min). Upgrade: learn it from recoveries.
RATE_LIMIT_COOLDOWN_SECONDS = 120.0
RATE_LIMIT_MAX_COOLDOWN_SECONDS = 1800.0
# ponytail: one status-13 error frame is one failed search, not an IP block. Three of
# those, or a catalog with no wrb.fr payload, inside this same 2-minute guess is a
# burst that clears only after a pause. Upgrade: learn the count from recoveries.
THROTTLE_REPEAT_THRESHOLD = 3
THROTTLE_REPEAT_WINDOW_SECONDS = RATE_LIMIT_COOLDOWN_SECONDS
NOT_SENT = "Not sent. "
PAUSE_PHRASE = "searches until"
# Shown when the status-13 cooldown is recorded. One error frame is not this pause.
# "Likely": a status 13 does not prove the IP is a hosted sandbox.
_HOSTED_THROTTLE_HINT = (
    "Google Flights is likely blocking this shared IP because viajante is running "
    "in a cloud-hosted agent or sandbox. It works much better on your own computer, "
    "in the ChatGPT or Claude desktop app with the viajante MCP (npx -y @viajante/mcp), "
    "or with pip install viajante."
)
# The first pause, before the cooldown is recorded. The code stays in diagnostics.
PAUSE_MESSAGE = (
    "Google Flights paused this request. That often happens after many searches, "
    "or on a shared cloud IP. Availability is unknown. Wait 15-30 minutes. "
    "Don't add airports or start a new search until then. "
    "This works better on your own computer, in the ChatGPT or Claude desktop app "
    "with npx -y @viajante/mcp, or with pip install viajante."
)
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
    cap = RATE_LIMIT_MAX_COOLDOWN_SECONDS
    if not now - cap <= until <= now + cap:
        return None
    if not 0 <= until - at <= cap:
        return None
    # Records written before these fields existed stay readable: their provenance is
    # unknown rather than guessed.
    state = dict(state)
    if not isinstance(state.get("basis"), str) or state["basis"] not in _COOLDOWN_BASES:
        state["basis"] = "unknown"
    if state.get("cause") not in _CAUSE_OF_BASIS.values():
        state["cause"] = "unknown"
    for key in ("viajante_version", "endpoint"):
        if not isinstance(state.get(key), str):
            state[key] = None
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


_throttle_hits: list[float] = []
_throttle_lock = threading.Lock()


def throttle_hits_file(rate_file: str = GOOGLE_RATE_LIMIT_FILE) -> str:
    """Sibling of the cooldown file, so separate CLI runs share one burst count."""
    if rate_file.endswith("-rate-limit.json"):
        return rate_file.replace("-rate-limit.json", "-throttle-hits.json")
    return rate_file + ".throttle-hits.json"


def _read_throttle_hits(path, now: float) -> list[float]:
    cutoff = now - THROTTLE_REPEAT_WINDOW_SECONDS
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        hits = payload.get("hits", ())
    except (OSError, ValueError, TypeError, AttributeError):
        return []
    kept: list[float] = []
    for hit in hits:
        if isinstance(hit, (int, float)) and math.isfinite(hit) and float(hit) > cutoff:
            kept.append(float(hit))
    return kept


def reset_throttle_signals() -> None:
    """Drop the burst counter so one search does not leak into the next."""
    with _throttle_lock:
        _throttle_hits.clear()
    try:
        (default_state_dir() / GOOGLE_THROTTLE_HITS_FILE).unlink(missing_ok=True)
    except OSError:
        pass


def note_repeated_throttle(
    now: Optional[float] = None,
    *,
    endpoint: Optional[str] = None,
    file: str = GOOGLE_RATE_LIMIT_FILE,
) -> Optional[dict]:
    """Count one status-13 or missing-payload signal. At the threshold, record the cooldown.

    A data-less RPC status 13 still records on its own, in the caller. This is the
    burst of signals that today are not a cooldown: a public-page error frame and a
    catalog with no ``wrb.fr`` payload. Hits are stored beside the cooldown file, so
    separate CLI runs add up. Later signals in the same window return the recorded
    cooldown and do not extend it.
    """
    current = time.time() if now is None else now
    path = default_state_dir() / throttle_hits_file(file)
    with _throttle_lock:
        try:
            with exclusive_lock(path):
                hits = _read_throttle_hits(path, current)
                hits.append(current)
                write_json_atomic({"hits": hits}, path)
                _throttle_hits[:] = hits
                tripped = len(hits) >= THROTTLE_REPEAT_THRESHOLD
        except OSError:
            cutoff = current - THROTTLE_REPEAT_WINDOW_SECONDS
            _throttle_hits[:] = [hit for hit in _throttle_hits if hit > cutoff]
            _throttle_hits.append(current)
            tripped = len(_throttle_hits) >= THROTTLE_REPEAT_THRESHOLD
    if not tripped:
        return None
    return note_rate_limited(
        now=current,
        basis="heuristic_rpc_13",
        cause="rpc_13",
        endpoint=endpoint,
        file=file,
    )


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
        pause = (
            "Viajante could not save the cooldown. "
            if state.get("persisted") is False
            else (
                f"Viajante pauses {provider} {PAUSE_PHRASE} {clock(state['until'])} UTC "
                f"(~{minutes} min). "
            )
        )
        return (
            f"{prefix}{_HOSTED_THROTTLE_HINT} {pause}"
            "Tell the user to wait; do not retry or switch fetch mode."
        )
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
