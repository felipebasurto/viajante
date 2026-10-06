"""On-demand re-run of a saved flight or hotel query, compared with its last observation.

User-triggered only: no scheduler, no notification, no background loop. A watch is
a named set of ``search_flights`` / ``search_hotels`` tool arguments. Running it
goes through the normal tool (cooldown, 5-minute cache, one-search lock) and
records what that search really returned.
"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from viajante.envelope import ENVELOPE_KEYS, stamp_local
from viajante.evidence import record
from viajante.history import (
    SCHEMA_VERSION,
    HistoryReadError,
    change_between,
    forced_recording,
    price_history,
    read_observations,
    short_reason,
)
from viajante.mcp_handlers import check_search_params, search_flights_tool, search_hotels_tool
from viajante.storage import default_state_dir, exclusive_lock, write_json_atomic

WATCHES_FILE = "price-watches.json"
MAX_WATCHES = 50
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Same kind words as price_history and the stored observations.
_TOOLS: dict[str, Callable[..., Mapping[str, object]]] = {
    "flight": search_flights_tool,
    "hotel": search_hotels_tool,
}


class WatchesReadError(OSError):
    """The saved-watches file exists but could not be read. Never the same as no watches."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{reason}; saved watches were not changed")
        self.reason = reason


def _path():
    return default_state_dir() / WATCHES_FILE


def watches_read_reason(exc: BaseException) -> str:
    """A reason safe to hand to an MCP client: no paths."""
    if isinstance(exc, PermissionError):
        return "permission denied accessing saved watches"
    if isinstance(exc, OSError):
        return f"could not access saved watches ({exc.strerror or type(exc).__name__})"
    return "the saved watches file is corrupt"


def _load() -> dict[str, dict]:
    """Saved watches. A missing file is empty; any other read or parse failure raises."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise WatchesReadError(watches_read_reason(exc)) from exc
    watches = data.get("watches") if isinstance(data, dict) else None
    if not isinstance(watches, dict) or not all(
        isinstance(spec, dict)
        and spec.get("kind") in _TOOLS
        and isinstance(spec.get("params"), dict)
        for spec in watches.values()
    ):
        raise WatchesReadError(watches_read_reason(ValueError()))
    return watches


def _store(watches: Mapping[str, dict]) -> None:
    write_json_atomic(
        {"schema_version": SCHEMA_VERSION, "watches": dict(watches)},
        _path(),
    )


def list_watches() -> list[dict]:
    return [{"name": name, **spec} for name, spec in sorted(_load().items())]


def remove_watch(name: str) -> bool:
    with exclusive_lock(_path()):
        watches = _load()
        if watches.pop(name, None) is None:
            return False
        _store(watches)
    return True


def save_watch(name: str, kind: str, params: Mapping[str, Any]) -> dict:
    """Validate and save a watch. Saving under an existing name replaces that watch."""
    if not _NAME.match(name):
        raise ValueError("watch name must be 1-64 letters, digits, '.', '_' or '-'")
    if kind not in _TOOLS:
        raise ValueError("kind must be flight or hotel")
    if "proxy" in params:
        raise ValueError("params.proxy is not stored in a watch; it may carry credentials")
    try:
        inspect.signature(_TOOLS[kind]).bind(**params)
    except TypeError as exc:
        raise ValueError(f"params do not match search_{kind}: {exc}") from exc
    check_search_params(_TOOLS[kind], params)
    spec = {
        "kind": kind,
        "params": dict(params),
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with exclusive_lock(_path()):
        watches = _load()
        if name not in watches and len(watches) >= MAX_WATCHES:
            raise ValueError(f"at most {MAX_WATCHES} watches; remove one first")
        watches[name] = spec
        _store(watches)
    return spec


def _errors(payload: Mapping[str, Any]) -> list[dict]:
    rows = payload.get("queries") or ()
    return [
        {key: row["error"].get(key) for key in ("code", "message", "rate_limited")}
        for row in rows
        if isinstance(row, Mapping) and row.get("status") == "error"
    ]


def watch_price_tool(
    name: Optional[str] = None,
    *,
    kind: Optional[str] = None,
    params: Optional[Mapping[str, Any]] = None,
) -> dict:
    """List watches (no name), save one (kind + params), or re-run one and report change.

    ``kind`` is ``flight`` or ``hotel``, as in price_history.
    """
    if name is None:
        try:
            return stamp_local({"watches": list_watches()})
        except WatchesReadError as exc:
            return stamp_local(
                {"watches": None, "read_error": exc.reason},
                status="failed",
                completeness="blocked",
                error_code="watches_unreadable",
            )
    if (kind is None) != (params is None):
        raise ValueError("pass kind and params together to save a watch")
    if kind is not None and params is not None:
        save_watch(name, kind, params)
    spec = _load().get(name)
    if spec is None:
        raise ValueError(f"no watch named {name!r}; save one with kind and params")
    with forced_recording() as written:
        payload = _TOOLS[spec["kind"]](**spec["params"])
    read_error: Optional[str] = None
    try:
        stored = read_observations(strict=True)
    except HistoryReadError as exc:
        stored, read_error = [], short_reason(exc)
    results = []
    ids = [row["id"] for row in stored]
    stored_ids = {row["id"] for row in written.entries}
    for entry in [*written.entries, *written.unsaved]:
        before = stored[: ids.index(entry["id"])] if entry["id"] in ids else stored
        earlier = [
            row
            for row in before
            if row["query_key"] == entry["query_key"] and row["currency"] == entry["currency"]
        ]
        other = any(
            row["query_key"] == entry["query_key"] and row["currency"] != entry["currency"]
            for row in stored
        )
        result: dict[str, Any] = {
            "kind": entry["kind"],
            "query_key": entry["query_key"],
            "query": entry["query"],
            "currency": entry["currency"],
            "current": {"observed_at": entry["observed_at"], "cheapest": entry["cheapest"]},
            "change": change_between(earlier[-1], entry) if earlier else None,
            "recorded": entry["id"] in stored_ids,
        }
        if read_error is not None:
            result["note"] = "history could not be read; no comparison"
        elif not earlier:
            result["note"] = "First observation of this query in this currency; nothing to compare."
            if other:
                result["note"] += " Observations in other currencies are never compared."
        results.append(result)
    cached = bool(payload.get("cached"))
    out: dict[str, Any] = {
        "name": name,
        "kind": spec["kind"],
        "cached": cached,
        "recorded": len(written.entries),
        "results": results,
        "errors": _errors(payload),
    }
    out.update({key: payload.get(key) for key in ENVELOPE_KEYS})
    if read_error is not None:
        out["read_error"] = read_error
        if out["status"] == "ok":
            out["completeness"] = "partial"
    if cached:
        out["note"] = (
            "Replayed from the 5-minute cache; no new request was sent and no observation "
            "was recorded. Read price_history for the stored series."
        )
    elif not results and not out["errors"]:
        out["note"] = "The search returned no priced offer, so nothing was recorded."
    if written.errors:
        out["recording_error"] = written.errors[0]
        out["note"] = (
            f"recording failed: {written.errors[0]}. The observed price is shown but not stored."
        )
    return record(out)  # type: ignore[return-value]


def price_history_tool(**filters: Any) -> dict:
    payload = price_history(**filters)
    if payload.get("read_error") is None:
        stamp_local(payload)
    else:
        stamp_local(
            payload, status="failed", completeness="blocked", error_code="history_unreadable"
        )
    return record(payload)  # type: ignore[return-value]
