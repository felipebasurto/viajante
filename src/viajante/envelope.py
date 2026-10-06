"""One result envelope for every MCP tool: how complete a result is and why it is empty.

The envelope is stamped on the top level of an MCP payload and only reads what the
payload already owns (query rows, errors, ``coverage``, ``searched_at``). It never
adds a verdict, a recommendation, or a number the provider did not return.

``empty_reason`` is the contract an agent must keep when it talks to a traveller:

* ``provider_empty``: the provider answered and returned nothing. Only this reason
  may be described as "no flights/hotels/rooms found".
* ``filtered_out``: the provider returned rows and viajante's own filters removed
  every one. Say filters removed the results; availability is not disproved.
* ``not_loaded``: no usable response was obtained (rate limit, challenge, drift,
  timeout, failure). Say the search did not complete; availability is unknown.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional

from viajante.ratelimit import GOOGLE_RATE_LIMIT_FILE, SKIPLAGGED_RATE_LIMIT_FILE, rate_limit_status

STATUSES = ("ok", "no_results", "rate_limited", "blocked", "timeout", "failed")
COMPLETENESS = ("complete", "partial", "blocked")
EMPTY_REASONS = ("provider_empty", "filtered_out", "not_loaded")
# "provider" is reserved for a timestamp a provider itself returns; none does today.
OBSERVED_BASES = ("provider", "fetch")

EMPTY_NOTES = {
    "provider_empty": "The provider answered and returned no results for this request.",
    "filtered_out": (
        "The provider returned rows but viajante's own filters removed every one. "
        "Availability is not disproved."
    ),
    "not_loaded": (
        "The search did not complete: no usable provider response was obtained. "
        "Availability is unknown."
    ),
}

_FAILURE_PRIORITY = ("rate_limited", "blocked", "timeout", "failed")
_COOLDOWN_FILES = {"google": GOOGLE_RATE_LIMIT_FILE, "skiplagged": SKIPLAGGED_RATE_LIMIT_FILE}


@dataclass
class _Tally:
    usable: int = 0
    provider_empty: int = 0
    filtered_out: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (status, code, provider)
    empty_codes: list[str] = field(default_factory=list)
    scope_partial: bool = False
    observed: list[str] = field(default_factory=list)


def _failure_status(error: Mapping[str, object]) -> str:
    if error.get("rate_limited"):
        return "rate_limited"
    if error.get("code") == "blocked":
        return "blocked"
    if error.get("timeout"):
        return "timeout"
    return "failed"


def _error(
    error: Mapping[str, object],
    tally: _Tally,
    provider: str,
    *,
    empty_as: str = "provider_empty",
) -> str:
    """Count one provider error. Returns the empty reason it implies for its row."""
    code = str(error.get("code", "fetch_failed"))
    if code == "no_results":
        tally.empty_codes.append(code)
        setattr(tally, empty_as, getattr(tally, empty_as) + 1)
        return empty_as
    tally.failures.append((_failure_status(error), code, provider))
    return "not_loaded"


def _query_row(row: dict, tally: _Tally, provider: str) -> None:
    error = row.get("error")
    if isinstance(error, Mapping):
        row["empty_reason"] = _error(error, tally, provider)
    elif row.get("offers"):
        tally.usable += 1
    else:
        reason = row.get("empty_reason") or (
            "filtered_out" if (row.get("raw_count") or 0) > 0 else "provider_empty"
        )
        setattr(tally, reason, getattr(tally, reason) + 1)
        row["empty_reason"] = reason


def _date_row(row: dict, tally: _Tally, provider: str, *, calendar: bool) -> None:
    status = row.get("status")
    error = row.get("error")
    if isinstance(error, Mapping):
        _error(error, tally, provider)
    elif status == "ok" and row.get("price") is not None:
        tally.usable += 1
    else:
        # A row without its own reason came from outside viajante's constructors:
        # only a calendar cell proves the provider had nothing; anything else is a
        # filter claim at worst, never "no flights".
        reason = row.get("empty_reason") or ("provider_empty" if calendar else "filtered_out")
        setattr(tally, reason, getattr(tally, reason) + 1)


def _walk(node: object, tally: _Tally, provider: str = "google") -> None:
    if not isinstance(node, Mapping):
        return
    if "skiplagged" in (node.get("provider"), node.get("source")):
        provider = "skiplagged"
    if isinstance(node.get("searched_at"), str):
        tally.observed.append(node["searched_at"])
    coverage = node.get("coverage")
    if (
        isinstance(coverage, Mapping)
        and coverage.get("strategy", "finite") == "finite"
        and coverage.get("complete") is False
    ):
        tally.scope_partial = True

    own_error = False
    if isinstance(node.get("queries"), list):
        for item in node["queries"]:
            if isinstance(item, dict) and "query" in item:
                _query_row(item, tally, provider)
            else:
                _walk(item, tally, provider)
    elif isinstance(node.get("flights"), Mapping) or isinstance(node.get("hotels"), Mapping):
        _walk(node.get("flights"), tally, provider)
        _walk(node.get("hotels"), tally, provider)
        return
    elif isinstance(node.get("days"), list):
        calendar = node.get("fetch_backend") in ("calendar", "calendar_then_sweep")
        for row in node["days"]:
            if isinstance(row, dict):
                _date_row(row, tally, provider, calendar=calendar)
    elif isinstance(node.get("destinations"), list):
        tally.usable += len(node["destinations"])
        if not node["destinations"] and not node.get("error") and not node.get("pricing_errors"):
            reason = node.get("empty_reason") or "filtered_out"
            setattr(tally, reason, getattr(tally, reason) + 1)
        for row in node.get("pricing_errors") or ():
            if isinstance(row, Mapping) and isinstance(row.get("error"), Mapping):
                _error(row["error"], tally, provider)
    elif "rates" in node:
        own_error = True
        error = node.get("error")
        if node["rates"]:
            tally.usable += 1
        elif not isinstance(error, Mapping):
            tally.provider_empty += 1
        elif error.get("code") == "no_results" and node.get("requested_name"):
            # An exact-name match over the provider's list did not resolve one hotel.
            _error(error, tally, provider, empty_as="filtered_out")
        else:
            _error(error, tally, provider)
    elif "offers" in node:
        tally.usable += bool(node["offers"])
        error = node.get("error")
        if isinstance(error, Mapping) and not node["offers"]:
            if error.get("code") == "currency_mismatch":
                # The provider priced the route; the caller's named keep removed every row.
                tally.empty_codes.append("currency_mismatch")
                tally.filtered_out += 1
            else:
                _error(error, tally, provider)
        elif not node["offers"]:
            tally.provider_empty += 1
        return

    error = node.get("error")
    if isinstance(error, Mapping) and not own_error:
        _error(error, tally, provider)


def _iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after(tally: _Tally, now: Optional[float]) -> tuple[Optional[str], Optional[int]]:
    current = time.time() if now is None else now
    until = [
        state["until"]
        for _status, _code, provider in tally.failures
        if _status == "rate_limited"
        for state in [rate_limit_status(current, file=_COOLDOWN_FILES[provider])]
        if state is not None
    ]
    if not until:
        return None, None
    end = max(until)
    return _iso_z(end), max(1, math.ceil(end - current))


def stamp_search(payload: dict, *, now: Optional[float] = None) -> dict:
    """Stamp the envelope on a provider-backed MCP payload. Existing keys are untouched."""
    tally = _Tally()
    _walk(payload, tally)
    failures = tally.failures
    answered = tally.usable + tally.provider_empty + tally.filtered_out
    worst = min((f[0] for f in failures), key=_FAILURE_PRIORITY.index, default=None)
    if tally.usable:
        status, empty_reason = "ok", None
    elif failures:
        status, empty_reason = worst, "not_loaded"
    else:
        status = "no_results"
        # Nothing attempted (every query removed locally) is a filter outcome, not an empty answer.
        only_provider = tally.provider_empty and not tally.filtered_out
        empty_reason = "provider_empty" if only_provider else "filtered_out"
    if failures:
        completeness = "partial" if answered else "blocked"
    else:
        completeness = "partial" if tally.scope_partial else "complete"
    error_code = next((code for s, code, _ in failures if s == worst), None)
    if error_code is None and empty_reason in ("provider_empty", "filtered_out"):
        error_code = next(iter(tally.empty_codes), None)
    retry_after, retry_after_seconds = _retry_after(tally, now)
    payload.update(
        status=status,
        completeness=completeness,
        empty_reason=empty_reason,
        empty_note=EMPTY_NOTES.get(empty_reason) if empty_reason else None,
        error_code=error_code,
        retry_after=retry_after,
        retry_after_seconds=retry_after_seconds,
        observed_at=max(tally.observed, default=None),
        observed_at_basis="fetch" if tally.observed else None,
    )
    return payload


def stamp_local(payload: dict, *, partial: bool = False) -> dict:
    """Stamp the envelope on an offline tool payload: ran locally, nothing fetched."""
    payload.update(
        status="ok",
        completeness="partial" if partial else "complete",
        empty_reason=None,
        empty_note=None,
        error_code=None,
        retry_after=None,
        retry_after_seconds=None,
        observed_at=None,
        observed_at_basis=None,
    )
    return payload
