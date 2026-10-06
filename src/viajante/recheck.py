"""Re-check an earlier flight offer against a fresh Google Flights search.

The outcome is one of ``same_price``, ``price_changed``, ``substituted``,
``not_found`` or ``check_failed``. A positive outcome always rests on an offer from a
search that ran now; nothing is replayed from the MCP cache and no amount is inferred.
``not_found`` means a completed check found nothing. ``check_failed`` means the check
did not run to an answer (blocked, rate-limited, incomplete offers, any provider
error): ``check_completed`` is false and that says nothing about whether the offer
exists. A re-check is still not a booking guarantee; the price is confirmed on the
provider's own page.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from viajante.airports import airport_geo
from viajante.carriers import parse_airline_codes, parse_alliances
from viajante.flights import _clock_minutes, search_flights
from viajante.models import (
    FlightLeg,
    FlightQuery,
    MultiCity,
    QueryFailure,
    RoundTrip,
    SearchErrorCode,
    SearchReport,
    Trip,
    normalize_currency,
)
from viajante.parsers import normalize_clock
from viajante.storage import write_json_atomic

SCHEMA_VERSION = 1
# ponytail: a one-way shop returns every card in one request, so compare them all. A packaged
# trip shops its next journey once per kept outbound, so cap that fan-out. The cap is a ceiling:
# the result says how many offers were compared. Upgrade: attach return journeys only to
# outbounds whose first segment already matches.
ONE_WAY_TOP = 100
PACKAGED_TOP = 20
# ponytail: heuristic ceiling for "close alternative" when no flight number is shared.
CLOSE_DEPARTURE_MINUTES = 90
CAVEAT = (
    "A re-check is not a booking guarantee. Confirm the price and terms on the "
    "provider's own page before relying on them."
)
_FLIGHT_NUMBER = re.compile(r"^([A-Z0-9]{2})0*(\d+[A-Z]?)$")
_SHOP_KEYS = ("adults", "cabin")


@dataclass(frozen=True)
class _Segment:
    flight_number: Optional[str]
    carrier: Optional[str]
    airline: Optional[str]
    clock: Optional[str]
    day: Optional[str]
    origin: Optional[str]
    destination: Optional[str]


def _mapping(value: object, *, role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{role} must be an object")
    return value


def _text(value: object) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _flight_number(value: object) -> Optional[str]:
    text = _text(value)
    if text is None:
        return None
    compact = re.sub(r"\s+", "", text).upper()
    match = _FLIGHT_NUMBER.match(compact)
    return f"{match.group(1)}{match.group(2)}" if match else compact


def _journeys(offer: Mapping[str, Any]) -> tuple[tuple[_Segment, ...], ...]:
    legs = offer.get("legs")
    if not isinstance(legs, Sequence) or isinstance(legs, str) or not legs:
        raise ValueError("offer.legs is required to establish the itinerary identity")
    journeys = []
    for leg in legs:
        leg = _mapping(leg, role="offer.legs[]")
        rows = leg.get("segments")
        if rows is not None and (not isinstance(rows, Sequence) or isinstance(rows, str)):
            raise ValueError("offer.legs[].segments must be a list")
        if rows:
            journeys.append(
                tuple(
                    _Segment(
                        flight_number=_flight_number(row.get("flight_number")),
                        carrier=(_text(row.get("carrier")) or "").upper() or None,
                        airline=_text(row.get("airline")),
                        clock=normalize_clock(_text(row.get("departure"))),
                        day=_text(row.get("departure_date")),
                        origin=_text(row.get("origin")),
                        destination=_text(row.get("destination")),
                    )
                    for row in (_mapping(r, role="offer.legs[].segments[]") for r in rows)
                )
            )
        else:
            journeys.append(
                (
                    _Segment(
                        None,
                        None,
                        _text(offer.get("airline")),
                        normalize_clock(_text(leg.get("departure"))),
                        None,
                        None,
                        None,
                    ),
                )
            )
    return tuple(journeys)


def _same_carrier(old: _Segment, new: _Segment) -> bool:
    if old.carrier and new.carrier:
        return old.carrier == new.carrier
    if old.airline and new.airline:
        return old.airline.casefold() == new.airline.casefold()
    return False


def _segment_match(old: _Segment, new: _Segment, basis: str) -> bool:
    if old.clock is None or old.clock != new.clock:
        return False
    if old.day and new.day and old.day != new.day:
        return False
    if basis == "flight_numbers":
        return old.flight_number is not None and old.flight_number == new.flight_number
    return _same_carrier(old, new)


def _identical(old: tuple, new: tuple, basis: str) -> bool:
    return len(old) == len(new) and all(
        len(a) == len(b) and all(_segment_match(x, y, basis) for x, y in zip(a, b, strict=True))
        for a, b in zip(old, new, strict=True)
    )


def _gap(a: Optional[str], b: Optional[str]) -> Optional[int]:
    left, right = _clock_minutes(a), _clock_minutes(b)
    if left is None or right is None:
        return None
    diff = abs(left - right)
    return min(diff, 1440 - diff)


def _shared_numbers(a: tuple[_Segment, ...], b: tuple[_Segment, ...]) -> int:
    old = {s.flight_number for s in a if s.flight_number}
    return len(old & {s.flight_number for s in b if s.flight_number})


def _closeness(old: tuple, new: tuple) -> Optional[tuple[int, int]]:
    """(shared flight numbers, total departure gap) when every journey is close, else None.

    Close means a shared flight number, or a first departure within the window on the same
    marketing carrier. A different carrier at a similar time is another product.
    """
    if len(old) != len(new):
        return None
    shared, gap_total = 0, 0
    for a, b in zip(old, new, strict=True):
        shares = _shared_numbers(a, b)
        gap = _gap(a[0].clock, b[0].clock)
        near = gap is not None and gap <= CLOSE_DEPARTURE_MINUTES and _same_carrier(a[0], b[0])
        if not shares and not near:
            return None
        shared += shares
        gap_total += gap or 0
    return shared, gap_total


def _path(segments: tuple[_Segment, ...]) -> Optional[list[str]]:
    if not all(s.origin and s.destination for s in segments):
        return None
    return [segments[0].origin, *(s.destination for s in segments)]  # type: ignore[misc,list-item]


def _numbers(segments: tuple[_Segment, ...]) -> Optional[list[str]]:
    return [s.flight_number for s in segments] if all(s.flight_number for s in segments) else None  # type: ignore[misc]


def _differences(
    old: tuple, new: tuple, old_legs: Sequence[Mapping], new_legs: Sequence[Mapping]
) -> list[dict[str, object]]:
    """Provable differences per journey. A None side is unknown, never a guess."""
    rows: list[dict[str, object]] = []
    for index, (a, b) in enumerate(zip(old, new, strict=True)):
        pairs = {
            "flight_numbers": (_numbers(a), _numbers(b)),
            "route": (_path(a), _path(b)),
            "departure": (a[0].clock, b[0].clock),
            "connection_departures": (
                [x.clock for x in a[1:]] if len(a) > 1 else None,
                [x.clock for x in b[1:]],
            ),
            "arrival": (
                normalize_clock(_text(old_legs[index].get("arrival"))),
                normalize_clock(_text(new_legs[index].get("arrival"))),
            ),
        }
        for field, (before, after) in pairs.items():
            if before is not None and before != after:
                rows.append(
                    {"journey": index, "field": field, "previous": before, "current": after}
                )
    return rows


def _place(row: Mapping[str, Any], key: str, role: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{role} is missing {key} (an IATA code)")
    return value


def _whole(query: Mapping[str, Any], key: str) -> int:
    value = query[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"query.{key} must be a whole number")
    return value


def _list_field(query: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = query[key]
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and all(isinstance(item, str) for item in value):
        return tuple(value)
    kind = "alliance names" if "alliance" in key else "IATA codes"
    raise ValueError(f"query.{key} must be a list of {kind} or a string")


def _trip_from_query(query: Mapping[str, Any]) -> Trip:
    kind = query.get("trip", "one-way")
    for key in (*_SHOP_KEYS, "max_stops"):
        if query.get(key) is None:
            raise ValueError(f"query.{key} is required; it is never guessed")
    if not isinstance(query["cabin"], str):
        raise ValueError("query.cabin must be a string")
    shop: dict[str, Any] = {"adults": _whole(query, "adults"), "cabin": query["cabin"]}
    for key in ("children", "infants_in_seat", "infants_on_lap", "bags", "carry_on"):
        if query.get(key) is not None:
            shop[key] = _whole(query, key)
    for key, parse in (
        ("airlines", parse_airline_codes),
        ("exclude_airlines", parse_airline_codes),
        ("alliances", parse_alliances),
        ("exclude_alliances", parse_alliances),
    ):
        if query.get(key):
            names = _list_field(query, key)
            shop[key] = parse(",".join(names)) if isinstance(query[key], str) else names
    stops = query["max_stops"]

    def day(value: object, role: str) -> date:
        try:
            return date.fromisoformat(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"{role} must be an ISO date") from None

    if kind == "multi":
        rows = query.get("legs")
        if not isinstance(rows, Sequence) or isinstance(rows, str):
            raise ValueError("query.legs is required for a multi-city offer")
        legs = []
        for index, raw in enumerate(rows):
            role = f"query.legs[{index}]"
            row = _mapping(raw, role=role)
            legs.append(
                FlightLeg(
                    _place(row, "origin", role),
                    _place(row, "destination", role),
                    day(row.get("departure_date"), f"{role}.departure_date"),
                    row.get("max_stops", stops),
                )
            )
        legs = tuple(legs)
        return MultiCity(legs=legs, **shop)
    head = {
        "origin": _place(query, "origin", "query"),
        "destination": _place(query, "destination", "query"),
        "departure_date": day(query.get("departure_date"), "query.departure_date"),
        "max_stops": stops,
    }
    if kind == "rt":
        return RoundTrip(
            **head, return_date=day(query.get("return_date"), "query.return_date"), **shop
        )
    if kind == "one-way":
        return FlightQuery(**head, **shop)
    raise ValueError(f"query.trip must be one-way, rt, or multi, not {kind!r}")


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _unwrap(offer: Mapping[str, Any]) -> Mapping[str, Any]:
    if "offer" not in offer:
        return offer
    return _mapping(offer["offer"], role="offer.offer")


def _departed(day: date, clock: Optional[str], origin: str, now: datetime) -> bool:
    """True when the journey has left. Uses the origin's owned timezone when it has one."""
    minutes = _clock_minutes(clock)
    geo = airport_geo(origin)
    if minutes is not None and geo is not None:
        try:
            zone = ZoneInfo(geo[0])
        except (KeyError, ValueError):
            zone = None
        if zone is not None:
            return datetime.combine(day, time(minutes // 60, minutes % 60), zone) <= now
    return day < now.astimezone().date()


def _prepare(
    offer: Mapping[str, Any],
    query: Optional[Mapping[str, Any]],
    currency: Optional[str],
    now: datetime,
    ledger_offer: Optional[Callable[[str, float, str], Optional[Mapping[str, Any]]]],
) -> tuple[Mapping[str, Any], Mapping[str, Any], Trip, float, str, tuple, str, str]:
    row = _mapping(offer, role="offer")
    if query is None and isinstance(row.get("query"), Mapping):
        query = row["query"]
    offer = _unwrap(row)
    evidence = _mapping(offer.get("evidence") or {}, role="offer.evidence")
    if evidence.get("source", "google_flights") != "google_flights":
        raise ValueError("recheck_offer only re-checks Google Flights offers")
    query = _mapping(query if query is not None else evidence.get("query"), role="query")
    price = offer.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise ValueError("offer.price must be a positive number")
    carried = offer.get("currency") or evidence.get("currency")
    for named in (carried, currency):
        if named is not None and not isinstance(named, str):
            raise ValueError("currency must be an ISO 4217 code such as USD")
    if carried is None and currency is None:
        raise ValueError("currency is required: the offer carries none and none is guessed")
    own = normalize_currency(carried or currency)  # type: ignore[arg-type]
    if currency is not None and normalize_currency(currency) != own:
        raise ValueError(
            f"currency {normalize_currency(currency)} differs from the offer's own {own}; "
            "viajante does not convert, so re-check in the offer's currency"
        )

    trip = _trip_from_query(query)
    old = _journeys(offer)
    if len(old) != len(trip.legs):
        raise ValueError(
            f"offer carries {len(old)} journey(s) but its query needs {len(trip.legs)}; "
            "an incomplete itinerary cannot be re-checked"
        )
    segments = [s for journey in old for s in journey]
    if any(s.clock is None for s in segments):
        raise ValueError("every offer segment needs a departure clock (HH:MM)")
    for leg, journey in zip(trip.legs, old, strict=True):
        if _departed(leg.departure_date, journey[0].clock, leg.origin, now):
            raise ValueError(
                f"departure is in the past: {leg.departure_date.isoformat()} {journey[0].clock}"
            )
    basis = "flight_numbers" if all(s.flight_number for s in segments) else "carrier_times"
    if basis == "carrier_times" and not all(s.carrier or s.airline for s in segments):
        raise ValueError("offer segments carry neither flight numbers nor carriers")
    evidence_id = evidence.get("evidence_id")
    recorded = (
        ledger_offer(evidence_id, float(price), own)
        if isinstance(evidence_id, str) and ledger_offer is not None
        else None
    )
    owned = recorded is not None and _journeys(recorded) == old
    source = "search_evidence" if owned else "caller_supplied"
    return offer, evidence, trip, float(price), own, old, basis, source


def recheck_offer(
    offer: Mapping[str, Any],
    *,
    query: Optional[Mapping[str, Any]] = None,
    currency: Optional[str] = None,
    country: Optional[str] = None,
    fetch: Optional[str] = None,
    proxy: Optional[str] = None,
    search: Optional[Callable[..., SearchReport]] = None,
    now: Optional[datetime] = None,
    ledger_offer: Optional[Callable[[str, float, str], Optional[Mapping[str, Any]]]] = None,
) -> dict[str, object]:
    """Run one fresh Google Flights search and match ``offer`` by itinerary identity.

    ``offer`` is an offer from an earlier result (or a ``{query, offer}`` row), or enough
    of one: ``price`` and ``legs[].segments[]`` with flight numbers and departure clocks.
    ``query`` defaults to the offer's own evidence query. ``currency`` is the offer's own
    currency: an offer that carries none needs it named, and a name that differs from the
    one the offer carries is refused (viajante does not convert). ``ledger_offer(evidence_id,
    price, currency)`` returns the offer a search in this process returned under that id.
    Only when its itinerary also matches the caller's is ``previous`` ``search_evidence``;
    otherwise it is ``caller_supplied``.
    """
    try:
        offer, evidence, trip, price, own, old, basis, source = _prepare(
            offer, query, currency, now or datetime.now(timezone.utc), ledger_offer
        )
    except (KeyError, TypeError, AttributeError):
        raise ValueError("offer or query has a malformed field; check its types") from None
    segments = [s for journey in old for s in journey]

    packaged = len(trip.legs) > 1
    if fetch is None:
        backend = evidence.get("fetch_backend")
        fetch = backend if backend == "sweep" or (backend == "detail" and not packaged) else "auto"
    report = (search or search_flights)(
        [trip],
        top=PACKAGED_TOP if packaged else ONE_WAY_TOP,
        fetch=fetch,
        sort="fare",
        currency=own,
        country=country,
        proxy=proxy,
    )
    result = report.queries[0]
    notes: list[str] = []
    out: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": _stamp(report.searched_at),
        "check_completed": True,
        "match_basis": basis,
        "previous": {
            "price": price,
            "currency": own,
            "source": source,
            "itinerary": [[_segment_json(s) for s in journey] for journey in old],
        },
        "query": dict(trip.to_dict()),
        "fetch_backend": report.fetch_backend,
        "caveat": CAVEAT,
        "notes": notes,
    }
    if basis == "carrier_times":
        notes.append(
            "Flight numbers are absent from the offer; matched by carrier and scheduled "
            "departure times, which is a weaker identity."
        )
    if country is None:
        notes.append(
            "No Google country (gl) was applied: offer evidence does not record the original "
            "search's country. Name country if the original search used one."
        )
    if packaged:
        notes.append(
            "Each fresh outbound carries the one return that is unique at its cheapest "
            "package price. An original return that is still buyable but not that one can "
            "read as a substitution; confirm multi-journey trips on the provider's page."
        )

    if isinstance(result, QueryFailure):
        error = result.error
        if error.code == SearchErrorCode.NO_RESULTS:
            return _not_found(out, "provider_empty")
        reason = "rate_limited" if error.rate_limited else error.code.value
        return _check_failed(out, reason, dict(error.to_dict()))

    fresh = [item.to_dict(report.currency) for item in result.offers]
    complete = [(d, _journeys(d)) for d in fresh if len(d["legs"]) == len(old)]  # type: ignore[arg-type]
    if result.eligible_count > len(result.offers):
        notes.append(
            f"Only the {len(result.offers)} cheapest of {result.eligible_count} eligible "
            "offers were compared."
        )
    out["offers_compared"] = len(complete)

    identical = [(d, j) for d, j in complete if _identical(old, j, basis)]
    if identical:
        identical.sort(key=lambda pair: (pair[0]["price"] != price, pair[0]["price"]))
        if len(identical) > 1:
            notes.append(f"{len(identical)} fresh offers matched the itinerary; reporting one.")
        if any(s.day is None for s in segments):
            notes.append(
                "Segment departure dates were unavailable; the query date and departure "
                "clocks were compared."
            )
        return _found(out, identical[0][0], report.currency, price, "identical")

    missing = len(fresh) - len(complete)
    if missing:
        return _check_failed(
            out,
            "incomplete_offers",
            {
                "code": "incomplete_offers",
                "message": (
                    f"{missing} fresh offer(s) came back without every journey (the follow-up "
                    "search for the next journey failed or was ambiguous), so the original "
                    "itinerary cannot be ruled out."
                ),
            },
        )

    close = [(score, d, j) for d, j in complete if (score := _closeness(old, j)) is not None]
    if close:
        close.sort(key=lambda row: (-row[0][0], row[0][1], row[1]["price"]))
        _, found, journeys = close[0]
        out["differences"] = _differences(old, journeys, offer["legs"], found["legs"])
        return _found(out, found, report.currency, price, "substituted")

    if result.raw_count > 0 and result.eligible_count == 0:
        notes.append("Google returned offers, but none passed the query's own constraints.")
        return _not_found(out, "filtered")
    return _not_found(out, "provider_empty" if result.raw_count == 0 else "not_among_offers")


def _segment_json(segment: _Segment) -> dict[str, object]:
    return {
        "flight_number": segment.flight_number,
        "carrier": segment.carrier,
        "airline": segment.airline,
        "origin": segment.origin,
        "destination": segment.destination,
        "departure": segment.clock,
        "departure_date": segment.day,
    }


def _found(
    out: dict[str, object],
    offer: Mapping[str, Any],
    currency: str,
    previous_price: float,
    kind: str,
) -> dict[str, object]:
    out["current"] = {"price": offer["price"], "currency": currency, "offer": dict(offer)}
    if kind == "substituted":
        out["outcome"] = "substituted"
    elif offer["price"] == previous_price:
        out["outcome"] = "same_price"
    else:
        out["outcome"] = "price_changed"
    return out


def _not_found(out: dict[str, object], reason: str) -> dict[str, object]:
    out.update(outcome="not_found", current=None, reason=reason)
    return out


def _check_failed(
    out: dict[str, object], reason: str, error: Mapping[str, object]
) -> dict[str, object]:
    out.update(
        outcome="check_failed",
        check_completed=False,
        current=None,
        reason=reason,
        error=dict(error),
    )
    out["notes"].append(  # type: ignore[attr-defined]
        "The check could not be completed. This says nothing about whether the offer still exists."
    )
    return out


def format_recheck(result: Mapping[str, Any]) -> str:
    """Human summary for the CLI. Amounts are only printed beside their own currency."""
    previous = result["previous"]
    lines = [f"{result['outcome']}  (checked {result['checked_at']}, {result['match_basis']})"]
    lines.append(f"  previous {previous['price']:g} {previous['currency']}")
    current = result.get("current")
    if current:
        lines.append(f"  current  {current['price']:g} {current['currency']}")
    if result.get("reason"):
        lines.append(f"  reason   {result['reason']}")
    if result.get("error"):
        lines.append(f"  error    {result['error']['message']}")
    for row in result.get("differences", ()):
        lines.append(
            f"  differs  journey {row['journey']} {row['field']}: "
            f"{row['previous']} -> {row['current']}"
        )
    lines.extend(f"  note     {note}" for note in result["notes"])
    lines.append(f"  {result['caveat']}")
    return "\n".join(lines)


def _load(path: str, role: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{role} not found: {path}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{role} is not valid JSON (line {exc.lineno}, column {exc.colno})"
        ) from None


def run_recheck_cli(args: argparse.Namespace) -> int:
    """0: check completed (any outcome). 1: bad input. 2: check could not be completed."""
    try:
        offer = _mapping(_load(args.offer, "offer file"), role="offer file")
        query = _mapping(_load(args.query, "query file"), role="query file") if args.query else None
        result = recheck_offer(
            offer,
            query=query,
            currency=args.currency,
            country=args.country,
            fetch=args.fetch,
            proxy=args.proxy,
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(format_recheck(result))
    if args.save:
        write_json_atomic(result, Path(args.save))
        print(f"\nSaved {args.save}")
    return 0 if result["check_completed"] else 2
