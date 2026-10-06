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

from viajante.ratelimit import NOT_SENT


class EnvelopeShapeError(ValueError):
    """An internal bug: a search result whose shape the envelope cannot read."""


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


@dataclass
class _Tally:
    usable: int = 0
    provider_empty: int = 0
    filtered_out: int = 0
    not_loaded: int = 0  # units that came back without evidence and without an error
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (status, code, provider)
    empty_codes: list[tuple[str, str]] = field(default_factory=list)  # (empty_reason, code)
    scope_partial: bool = False
    observed: list[str] = field(default_factory=list)
    unsent: int = 0  # failures that never reached the provider (a recorded cooldown)
    # per-error (retry_after epoch seconds, retry_after_seconds as the error stated it)
    retry_ends: list[tuple[float, Optional[int]]] = field(default_factory=list)
    shapes: int = 0  # recognised payload shapes; zero means stamp_search was misused


def _failure_status(error: Mapping[str, object]) -> str:
    if error.get("rate_limited"):
        return "rate_limited"
    if error.get("timeout"):
        return "timeout"
    if error.get("code") == "blocked":
        return "blocked"
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
        # An error code only rides along with the reason it actually supports.
        if empty_as == "provider_empty":
            tally.empty_codes.append((empty_as, code))
        setattr(tally, empty_as, getattr(tally, empty_as) + 1)
        return empty_as
    tally.failures.append((_failure_status(error), code, provider))
    tally.unsent += str(error.get("message", "")).startswith(NOT_SENT)
    if error.get("rate_limited") and isinstance(error.get("retry_after"), str):
        try:
            end = datetime.strptime(error["retry_after"], "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
        else:
            seconds = error.get("retry_after_seconds")
            tally.retry_ends.append(
                (
                    end.replace(tzinfo=timezone.utc).timestamp(),
                    seconds if isinstance(seconds, int) else None,
                )
            )
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
        # never "no flights". A calendar cell is unproven, anything else a filter claim.
        reason = row.get("empty_reason") or ("not_loaded" if calendar else "filtered_out")
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
    shape = True
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
        # A catalog destination whose price shop failed or came back empty carries no
        # price: it proves nothing, so it is unproven rather than usable.
        priced = sum(
            isinstance(row, Mapping) and row.get("price") is not None
            for row in node["destinations"]
        )
        tally.usable += priced
        tally.not_loaded += len(node["destinations"]) - priced
        if not node["destinations"] and not node.get("error") and not node.get("pricing_errors"):
            reason = node.get("empty_reason") or "filtered_out"
            setattr(tally, reason, getattr(tally, reason) + 1)
        for row in node.get("pricing_errors") or ():
            if isinstance(row, Mapping) and isinstance(row.get("error"), Mapping):
                _error(row["error"], tally, provider)
        coverage = node.get("coverage")
        if isinstance(coverage, Mapping) and (node.get("error") or node.get("pricing_errors")):
            # Shops that answered without an eligible fare are answers, not failures. The
            # counter cannot tell a provider-empty shop from one the filters emptied.
            tally.filtered_out += int(coverage.get("empty") or 0)
    elif "rates" in node and provider == "skiplagged":
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
    elif "offers" in node and provider == "skiplagged":
        tally.usable += bool(node["offers"])
        error = node.get("error")
        if isinstance(error, Mapping) and not node["offers"]:
            if error.get("code") == "currency_mismatch":
                # The provider priced the route; the caller's named keep removed every row.
                tally.empty_codes.append(("filtered_out", "currency_mismatch"))
                tally.filtered_out += 1
            else:
                _error(error, tally, provider)
        elif not node["offers"]:
            tally.provider_empty += 1
        tally.shapes += 1
        return
    else:
        shape = False

    error = node.get("error")
    if isinstance(error, Mapping) and not own_error:
        _error(error, tally, provider)
        shape = True
    tally.shapes += shape


def _iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_after(tally: _Tally, now: Optional[float]) -> tuple[Optional[str], Optional[int]]:
    """The latest cooldown end a rate-limited error itself named; none if it named none."""
    current = time.time() if now is None else now
    until = [pair for pair in tally.retry_ends if pair[0] > current]
    if not until:
        return None, None
    end, seconds = max(until, key=lambda pair: pair[0])
    # The error already rounded its end up to a whole second and derived its seconds from
    # it; repeat both instead of deriving a second, slightly different pair.
    return _iso_z(end), seconds if seconds is not None else max(1, math.ceil(end - current))


def stamp_search(payload: dict, *, now: Optional[float] = None) -> dict:
    """Stamp the envelope on a provider-backed MCP payload. Existing keys are untouched."""
    tally = _Tally()
    _walk(payload, tally)
    if not tally.shapes:
        # Developer hint: a new provider-backed payload needs its shape in `_walk`; an
        # offline one uses `stamp_local`. The client only learns the result is unusable.
        raise EnvelopeShapeError(
            "viajante could not read the shape of this search result, so it was not "
            "returned. This is a viajante bug, not a provider answer; no search outcome is implied."
        )
    failures = tally.failures
    answered = tally.usable + tally.provider_empty + tally.filtered_out
    worst = min((f[0] for f in failures), key=_FAILURE_PRIORITY.index, default=None)
    if tally.usable:
        status, empty_reason = "ok", None
    elif failures:
        status, empty_reason = worst, "not_loaded"
    else:
        status = "no_results"
        # The weakest claim wins. Unproven units cannot be called empty, and nothing
        # attempted (every query removed locally) is a filter outcome, not an empty answer.
        if tally.not_loaded:
            empty_reason = "not_loaded"
        else:
            only_provider = tally.provider_empty and not tally.filtered_out
            empty_reason = "provider_empty" if only_provider else "filtered_out"
    if failures:
        completeness = "partial" if answered else "blocked"
    else:
        completeness = "partial" if tally.scope_partial or tally.not_loaded else "complete"
    error_code = next((code for s, code, _ in failures if s == worst), None)
    if error_code is None:
        error_code = next(
            (code for reason, code in tally.empty_codes if reason == empty_reason), None
        )
    units = answered + tally.not_loaded + len(failures)
    # A request answered from a recorded cooldown never reached the provider: nothing was observed.
    observed = tally.observed if not units or units > tally.unsent else []
    retry_after, retry_after_seconds = _retry_after(tally, now)
    payload.update(
        status=status,
        completeness=completeness,
        empty_reason=empty_reason,
        empty_note=EMPTY_NOTES.get(empty_reason) if empty_reason else None,
        error_code=error_code,
        retry_after=retry_after,
        retry_after_seconds=retry_after_seconds,
        observed_at=max(observed, default=None),
        observed_at_basis="fetch" if observed else None,
    )
    return payload


def stamp_split(payload: dict, *, now: Optional[float] = None) -> dict:
    """Stamp the envelope on a ``search_split_tickets`` result from its own ledger.

    ``stamp_search`` reads query rows; a split report keeps its fetched rows in ``legs``
    and ``packaged_report`` and its pairings in ``itineraries``. Any itinerary is ``ok``
    (``partial`` when a fetch failed); with none, a failed or cooled-down fetch wins over
    an empty answer, an answer whose pairings were all rejected is ``filtered_out``, and
    only provider-empty legs may be called ``provider_empty``.
    """
    legs, itineraries = payload.get("legs"), payload.get("itineraries")
    if not isinstance(legs, list) or not isinstance(itineraries, list):
        raise EnvelopeShapeError(
            "viajante could not read the shape of this split-ticket result, so it was not "
            "returned. This is a viajante bug, not a provider answer; no search outcome is implied."
        )
    tally = _Tally()
    seen: set[tuple[object, object]] = set()

    def count(error: Mapping[str, object]) -> None:
        if error.get("code") != "no_results":
            key = (error.get("code"), error.get("message"))
            if key in seen:
                return
            seen.add(key)
        _error(error, tally, "google")

    leg_empty = 0  # legs the provider answered with nothing at all
    for row in legs:
        error = row.get("error")
        if isinstance(error, Mapping):
            count(error)
            leg_empty += error.get("code") == "no_results"
        elif row.get("offers"):
            tally.usable += 1
        elif row.get("raw_count") == 0:
            tally.provider_empty += 1
            leg_empty += 1
        else:
            tally.filtered_out += 1
    packaged = payload.get("packaged_report")
    for row in packaged.get("queries", ()) if isinstance(packaged, Mapping) else ():
        if isinstance(row.get("error"), Mapping):
            count(row["error"])
        else:
            _query_row(row, tally, "google")
    if isinstance(payload.get("error"), Mapping):
        count(payload["error"])

    failures = tally.failures
    worst = min((f[0] for f in failures), key=_FAILURE_PRIORITY.index, default=None)
    answered = tally.usable + tally.provider_empty + tally.filtered_out
    empty_reason: Optional[str] = None
    if itineraries:
        status = "ok"
    elif failures:
        status, empty_reason = worst, "not_loaded"
    else:
        status = "no_results"
        # The packaged fare is a baseline, not a pairing input: when legs were searched
        # they alone decide whether the provider had nothing to pair.
        if legs:
            only_provider = leg_empty == len(legs)
        else:
            only_provider = tally.provider_empty and not (tally.usable or tally.filtered_out)
        empty_reason = "provider_empty" if only_provider else "filtered_out"
    if failures:
        completeness = "partial" if itineraries or answered else "blocked"
    else:
        completeness = "complete"
    error_code = next((code for s, code, _ in failures if s == worst), None)
    if error_code is None:
        error_code = next(
            (code for reason, code in tally.empty_codes if reason == empty_reason), None
        )
    if error_code is None and empty_reason == "provider_empty":
        error_code = "no_results"
    retry_after, retry_after_seconds = _retry_after(tally, now)
    # Nothing was observed when every counted failure was a recorded cooldown and nothing answered.
    units = answered + len(failures)
    observed = payload.get("searched_at") if (not units or units > tally.unsent) else None
    payload.update(
        status=status,
        completeness=completeness,
        empty_reason=empty_reason,
        empty_note=EMPTY_NOTES.get(empty_reason) if empty_reason else None,
        error_code=error_code,
        retry_after=retry_after,
        retry_after_seconds=retry_after_seconds,
        observed_at=observed,
        observed_at_basis="fetch" if observed else None,
    )
    return payload


def stamp_local(
    payload: dict,
    *,
    partial: bool = False,
    status: str = "ok",
    completeness: Optional[str] = None,
    error_code: Optional[str] = None,
) -> dict:
    """Stamp the envelope on an offline tool payload: ran locally, nothing fetched.

    ``status`` stays ``ok`` unless the tool's own verdict failed (``verify_answer``).
    """
    payload.update(
        status=status,
        completeness=completeness or ("partial" if partial else "complete"),
        empty_reason=None,
        empty_note=None,
        error_code=error_code,
        retry_after=None,
        retry_after_seconds=None,
        observed_at=None,
        observed_at_basis=None,
    )
    return payload


def stamp_recheck(payload: dict, *, now: Optional[float] = None) -> dict:
    """Stamp the envelope on a ``recheck_offer`` result from its own outcome.

    ``stamp_search`` reads query rows and cannot read this shape. A check that ran to an
    answer is ``ok``; a completed ``not_found`` is ``no_results`` only when the provider
    returned nothing or viajante's filters removed every row. A failed check says nothing
    about the offer, so it is ``not_loaded``.
    """
    outcome = payload["outcome"]
    if outcome == "incomplete_identity":
        return stamp_local(
            payload, status="failed", completeness="blocked", error_code="incomplete_identity"
        )
    status, completeness, empty_reason, error_code = "ok", "complete", None, None
    retry: tuple[Optional[str], Optional[int]] = (None, None)
    observed = payload.get("checked_at")
    if outcome == "check_failed":
        error = payload["error"]
        status = _failure_status(error)
        error_code = str(error.get("code", payload.get("reason")))
        empty_reason = "not_loaded"
        completeness = "partial" if payload.get("reason") == "incomplete_offers" else "blocked"
        tally = _Tally()
        _error(error, tally, "google")
        retry = _retry_after(tally, now)
    elif outcome == "not_found":
        reason = payload.get("reason")
        if reason in ("provider_empty", "filtered"):
            status = "no_results"
            empty_reason = "provider_empty" if reason == "provider_empty" else "filtered_out"
        elif payload.get("offers_truncated"):
            completeness = "partial"
    payload.update(
        status=status,
        completeness=completeness,
        empty_reason=empty_reason,
        empty_note=EMPTY_NOTES.get(empty_reason) if empty_reason else None,
        error_code=error_code,
        retry_after=retry[0],
        retry_after_seconds=retry[1],
        observed_at=observed,
        observed_at_basis="fetch" if observed else None,
    )
    return payload
