"""Local memory of the prices viajante actually observed.

An opt-in, append-only log (``price-history.jsonl`` in the state directory). Each
line is one real search result: the query identity, the filters that change which
offers survive, the cheapest owned amount in the provider's currency, and when
the search ran. Nothing is estimated, converted, or predicted. A trend exists
only between observations of the same query in the same currency.

Recording is off unless ``VIAJANTE_PRICE_HISTORY=1`` (or a watch run, which is
an explicit request). Replayed MCP cache hits never reach the recorder, so they
are never logged as new observations.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import json
import math
import os
import re
import sys
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from viajante.control import check_cancelled
from viajante.models import HotelQuerySuccess, QuerySuccess
from viajante.runtime import package_version
from viajante.storage import (
    UnreadableStateError,
    default_state_dir,
    exclusive_lock,
    read_failure_reason,
    read_optional_bytes,
    write_bytes_atomic,
)

ENV_RECORD = "VIAJANTE_PRICE_HISTORY"
HISTORY_FILE = "price-history.jsonl"
# ponytail: every record rewrites the whole file atomically (read, append, trim) under an
# exclusive lock. At this cap that is a ~1 MB rewrite; upgrade: O_APPEND plus periodic
# compaction if the cap grows.
MAX_ENTRIES = 2000
DEFAULT_OBSERVATION_LIMIT = 20
SCHEMA_VERSION = 1
FLIGHT_PROVIDER = "google-flights"

_SUBJECT = "price history"
_TEXT_FIELDS = ("id", "kind", "query_key", "currency", "observed_at")
_OBSERVATION_FIELDS = (
    "id",
    "observed_at",
    "cheapest",
    "cheapest_text",
    "cheapest_label",
    "offers",
    "provider",
    "fetch_backend",
)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FLIGHT_FILTERS = (
    "top",
    "sort",
    "max_layover_hours",
    "min_layover_hours",
    "max_duration_hours",
    "depart_window",
    "arrive_before",
    "depart_after",
    "via",
    "exclude_via",
    "no_overnight",
    "require_overnight",
    "exclude_airports",
    "include_airports",
    "country",
)
_HOTEL_FILTERS = ("top", "source", "near", "max_distance_km")
_DATE_KEYS = ("departure_date", "return_date", "check_in", "check_out")
_ROUTE = re.compile(r"^([A-Za-z]{3})-([A-Za-z]{3})$")


@dataclass
class Recording:
    """What a forced recording block observed: stored entries, and any recording failure."""

    entries: list[dict] = field(default_factory=list)
    unsaved: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_sink: ContextVar[Optional[Recording]] = ContextVar("viajante_history_sink", default=None)


def recording_enabled() -> bool:
    return os.environ.get(ENV_RECORD, "").strip().lower() in _TRUE or _sink.get() is not None


@contextlib.contextmanager
def forced_recording() -> Iterator[Recording]:
    """Record every search in this block; report the entries it stored or failed to store."""
    sink = Recording()
    token = _sink.set(sink)
    try:
        yield sink
    finally:
        _sink.reset(token)


def _path():
    return default_state_dir() / HISTORY_FILE


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid(line: bytes) -> Optional[dict]:
    try:
        row = json.loads(line.decode("utf-8"))
    except ValueError:  # includes UnicodeDecodeError
        return None
    if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in _TEXT_FIELDS):
        return None
    cheapest = row.get("cheapest")
    if isinstance(cheapest, bool) or not isinstance(cheapest, (int, float)):
        return None
    try:
        if not math.isfinite(cheapest) or cheapest <= 0:
            return None
    except OverflowError:
        # JSON integers are arbitrary precision; malformed local history must not make
        # a reader or the next append fail while coercing one to float.
        return None
    query, filters = row.get("query"), row.get("filters")
    if not isinstance(query, dict) or not isinstance(filters, dict):
        return None
    legs = query.get("legs")
    if legs is not None and not (
        isinstance(legs, list)
        and all(
            isinstance(leg, dict) and isinstance(leg.get("departure_date"), str) for leg in legs
        )
    ):
        return None
    if row["kind"] == "hotel":
        text_keys, count_keys = ("location", "check_in", "check_out"), ("adults", "rooms")
    elif row["kind"] == "flight":
        text_keys, count_keys = ("origin", "destination", "departure_date", "trip"), ("adults",)
    else:
        return None
    if not all(isinstance(query.get(k), str) for k in text_keys):
        return None
    if not all(_positive_int(query.get(k)) for k in count_keys):
        return None
    return row


def _lines() -> list[bytes]:
    """Non-empty lines of the log. A missing file is empty; any other read error raises."""
    try:
        data = read_optional_bytes(_path())
    except OSError as exc:
        raise UnreadableStateError(read_failure_reason(exc, _SUBJECT)) from exc
    return [] if data is None else [line for line in data.split(b"\n") if line.strip()]


def read_observations(*, strict: bool = False) -> list[dict]:
    """Every readable entry, oldest first. Unparsable lines are skipped one by one.

    An unreadable file reads as empty unless ``strict``, which raises UnreadableStateError.
    """
    try:
        lines = _lines()
    except UnreadableStateError:
        if strict:
            raise
        return []
    return [row for row in map(_valid, lines) if row is not None]


def append_observations(entries: Sequence[Mapping[str, Any]]) -> None:
    """Append immutable entries; the oldest valid ones fall off beyond MAX_ENTRIES.

    Lines this version cannot parse are carried through byte for byte, never dropped.
    """
    if not entries:
        return
    new_lines = [
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True).encode("utf-8") for row in entries
    ]
    with exclusive_lock(_path()):
        lines = _lines() + new_lines
        valid = [index for index, line in enumerate(lines) if _valid(line) is not None]
        dropped = set(valid[: max(0, len(valid) - MAX_ENTRIES)])
        kept = [line for index, line in enumerate(lines) if index not in dropped]
        write_bytes_atomic(b"\n".join(kept) + b"\n", _path())


def clear_history() -> tuple[int, int]:
    """Delete the log. Returns (observations, unreadable lines) it held.

    Refuses (UnreadableStateError) when the file exists but cannot be read, so it never
    reports an unseen history as empty.
    """
    with exclusive_lock(_path()):
        lines = _lines()
        valid = sum(_valid(line) is not None for line in lines)
        _path().unlink(missing_ok=True)
    return valid, len(lines) - valid


def short_reason(exc: BaseException) -> str:
    """A reason safe to hand to an MCP client: no paths, no temp file names."""
    if isinstance(exc, UnreadableStateError):
        return exc.reason
    if isinstance(exc, OSError):
        return read_failure_reason(exc, _SUBJECT, writing=True)
    return f"price history could not be recorded ({type(exc).__name__})"


def _plain(value: object) -> object:
    if isinstance(value, (list, tuple)):
        items = [_plain(item) for item in value]
        return sorted(items) if all(isinstance(item, str) for item in items) else items
    return value


def _query_key(kind: str, query: Mapping[str, object], filters: Mapping[str, object]) -> str:
    blob = json.dumps(
        {"kind": kind, "query": query, "filters": filters}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _entry(
    kind: str,
    query: Mapping[str, object],
    filters: Mapping[str, object],
    *,
    currency: str,
    provider: str,
    fetch_backend: Optional[str],
    observed_at: datetime,
    cheapest: float,
    cheapest_text: str,
    cheapest_label: Optional[str],
    offers: int,
    eligible_count: int,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": uuid.uuid4().hex,
        "kind": kind,
        "query_key": _query_key(kind, query, filters),
        "query": dict(query),
        "filters": dict(filters),
        "currency": currency,
        "cheapest": cheapest,
        "cheapest_text": cheapest_text,
        "cheapest_label": cheapest_label,
        "offers": offers,
        "eligible_count": eligible_count,
        "provider": provider,
        "fetch_backend": fetch_backend,
        "observed_at": observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "viajante_version": package_version(),
    }


def flight_observations(report: Any, args: Mapping[str, Any]) -> list[dict]:
    filters = {k: _plain(args[k]) for k in _FLIGHT_FILTERS if args.get(k) is not None}
    filters["baggage_buffer"] = args.get("baggage_buffer") or 0
    rows = []
    for result in report.queries:
        if not isinstance(result, QuerySuccess) or not result.offers:
            continue
        best = min(result.offers, key=lambda offer: offer.price)
        rows.append(
            _entry(
                "flight",
                result.query.to_dict(),
                filters,
                currency=report.currency,
                provider=FLIGHT_PROVIDER,
                fetch_backend=report.fetch_backend,
                observed_at=report.searched_at,
                cheapest=best.price,
                cheapest_text=best.price_text,
                cheapest_label=best.airline,
                offers=len(result.offers),
                eligible_count=result.eligible_count,
            )
        )
    return rows


def hotel_observations(report: Any, args: Mapping[str, Any]) -> list[dict]:
    filters = {k: _plain(args[k]) for k in _HOTEL_FILTERS if args.get(k) is not None}
    rows = []
    for result in report.queries:
        if not isinstance(result, HotelQuerySuccess) or not result.offers:
            continue
        best = min(result.offers, key=lambda offer: offer.total_price)
        rows.append(
            _entry(
                "hotel",
                result.query.to_dict(),
                filters,
                currency=report.currency,
                provider=report.provider,
                fetch_backend=report.fetch_backend,
                observed_at=report.searched_at,
                cheapest=best.total_price,
                cheapest_text=best.total_price_text,
                cheapest_label=best.title,
                offers=len(result.offers),
                eligible_count=result.eligible_count,
            )
        )
    return rows


def _recorded(build: Callable[[Any, Mapping[str, Any]], list[dict]]):
    def decorate(search):
        signature = inspect.signature(search)

        @functools.wraps(search)
        def wrapper(*args, **kwargs):
            report = search(*args, **kwargs)
            check_cancelled()
            if recording_enabled():
                sink = _sink.get()
                entries: list[dict] = []
                try:
                    bound = signature.bind(*args, **kwargs)
                    bound.apply_defaults()
                    entries = build(report, bound.arguments)
                    append_observations(entries)
                except Exception as exc:  # recording must never lose a real search result
                    print(f"price history not recorded: {exc}", file=sys.stderr)
                    if sink is not None:
                        sink.errors.append(short_reason(exc))
                        sink.unsaved.extend(entries)
                else:
                    if sink is not None:
                        sink.entries.extend(entries)
            return report

        return wrapper

    return decorate


recorded_flights = _recorded(flight_observations)
recorded_hotels = _recorded(hotel_observations)


def _point(row: Mapping[str, Any]) -> dict:
    return {"observed_at": row["observed_at"], "cheapest": row["cheapest"]}


def change_between(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict:
    """Difference between two observations the caller has already matched."""
    delta = float(Decimal(str(current["cheapest"])) - Decimal(str(previous["cheapest"])))
    return {
        "previous": _point(previous),
        "price_change": delta,
        "price_change_abs": abs(delta),
        "percent": round(delta / previous["cheapest"] * 100, 1),
        "direction": "lower" if delta < 0 else "higher" if delta > 0 else "unchanged",
    }


def trend(rows: Sequence[Mapping[str, Any]]) -> dict:
    """Facts about one chronological same-query, same-currency series. No forecast."""
    last = rows[-1]
    out: dict[str, Any] = {
        "observation_count": len(rows),
        "first_seen": _point(rows[0]),
        "last_seen": _point(last),
        "lowest": _point(min(rows, key=lambda row: row["cheapest"])),
        "highest": _point(max(rows, key=lambda row: row["cheapest"])),
        "change_since_previous": None,
    }
    if len(rows) == 1:
        out["note"] = "Only one observation recorded; no change or trend can be reported."
    else:
        out["change_since_previous"] = change_between(rows[-2], last)
        out["note"] = "Your own recorded checks of this exact query; not a forecast."
    return out


def _dates(query: Mapping[str, Any]) -> set[str]:
    found = {str(query[key]) for key in _DATE_KEYS if query.get(key)}
    found.update(str(leg["departure_date"]) for leg in query.get("legs") or ())
    return found


def _same_place(a: object, b: str) -> bool:
    return " ".join(str(a).split()).casefold() == " ".join(b.split()).casefold()


def select(
    rows: Sequence[Mapping[str, Any]],
    *,
    kind: Optional[str] = None,
    query_key: Optional[str] = None,
    route: Optional[str] = None,
    date: Optional[str] = None,
    location: Optional[str] = None,
    currency: Optional[str] = None,
) -> list[Mapping[str, Any]]:
    pair = None
    if route is not None:
        match = _ROUTE.match(route.strip())
        if match is None:
            raise ValueError("route must be ORIGIN-DEST IATA codes, e.g. JFK-LHR")
        pair = (match.group(1).upper(), match.group(2).upper())
    wanted_currency = currency.strip().upper() if currency else None
    return [
        row
        for row in rows
        if (kind is None or row["kind"] == kind)
        and (query_key is None or row["query_key"] == query_key)
        and (wanted_currency is None or row["currency"] == wanted_currency)
        and (pair is None or (row["query"].get("origin"), row["query"].get("destination")) == pair)
        and (date is None or date in _dates(row["query"]))
        and (location is None or _same_place(row["query"].get("location", ""), location))
    ]


def series(rows: Sequence[Mapping[str, Any]], *, limit: int = DEFAULT_OBSERVATION_LIMIT) -> list:
    """Group by query and currency. Different currencies never share a series."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in sorted(rows, key=lambda row: row["observed_at"]):
        groups.setdefault((row["query_key"], row["currency"]), []).append(row)
    out = []
    for (key, currency), group in groups.items():
        shown = group[-limit:] if limit > 0 else group
        out.append(
            {
                "kind": group[0]["kind"],
                "query_key": key,
                "query": group[0]["query"],
                "filters": group[0]["filters"],
                "currency": currency,
                "observations": [
                    {name: row.get(name) for name in _OBSERVATION_FIELDS} for row in shown
                ],
                "observations_omitted": len(group) - len(shown),
                "trend": trend(group),
            }
        )
    return sorted(out, key=lambda item: item["trend"]["last_seen"]["observed_at"], reverse=True)


def price_history(
    *,
    kind: Optional[str] = None,
    query_key: Optional[str] = None,
    route: Optional[str] = None,
    date: Optional[str] = None,
    location: Optional[str] = None,
    currency: Optional[str] = None,
    limit: int = DEFAULT_OBSERVATION_LIMIT,
) -> dict:
    """Recorded observations and per-series facts. Local read; sends no request."""
    if kind not in (None, "flight", "hotel"):
        raise ValueError("kind must be flight or hotel")
    read_error: Optional[str] = None
    try:
        stored = read_observations(strict=True)
    except UnreadableStateError as exc:
        stored, read_error = [], short_reason(exc)
    matched = select(
        stored,
        kind=kind,
        query_key=query_key,
        route=route,
        date=date,
        location=location,
        currency=currency,
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "recording_enabled": recording_enabled(),
        "stored_entries": None if read_error is not None else len(stored),
        "series": None if read_error is not None else series(matched, limit=limit),
    }
    if read_error is not None:
        payload["read_error"] = read_error
        payload["note"] = (
            f"The price history file exists but could not be read ({read_error}); "
            "this is not an empty history and nothing is inferred."
        )
    elif not matched:
        payload["note"] = (
            "No recorded observation matches. Recording is off unless "
            f"{ENV_RECORD}=1 is set (or a watch ran); nothing is inferred."
            if not stored
            else "No recorded observation matches these filters; nothing is inferred."
        )
    return payload
