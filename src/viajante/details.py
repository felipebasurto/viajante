"""Evidence for a selected hotel finalist. Room quotes stay a separate provider answer."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Mapping, Optional

from viajante.airports import airport_geo, lookup_airports, metro_of
from viajante.envelope import stamp_local, stamp_search
from viajante.hotels import _raw_distance_km
from viajante.skiplagged_hotels import search_hotel_rooms

_ENVELOPE = (
    "status",
    "completeness",
    "empty_reason",
    "empty_note",
    "error_code",
    "retry_after",
    "retry_after_seconds",
    "observed_at",
    "observed_at_basis",
)

# ponytail: airports within 100 km (the metro table's radius) are one place.
# Homonyms farther apart stay separate. Upgrade: subdivision codes.
_PLACE_KM = 100.0
# Same property, not merely the same city. Coordinate rounding stays inside this.
_PROPERTY_KM = 2.0


def _payload(report):
    return dict(report.to_dict()) if hasattr(report, "to_dict") else deepcopy(dict(report))


def _selected(report, query_index, offer_index):
    payload = _payload(report)
    for index in (query_index, offer_index):
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("query and offer indices must be non-negative integers")
    try:
        result = payload["queries"][query_index]
        offer = result["offers"][offer_index]
    except (IndexError, KeyError, TypeError) as exc:
        raise ValueError("selected query or offer is missing") from exc
    return payload, result, offer


def _quote(payload, result, offer):
    provider = payload.get("provider")
    quote = {
        "provider": provider if isinstance(provider, str) and provider.strip() else None,
        "currency": payload.get("currency"),
        "searched_at": payload.get("searched_at"),
        "query": result["query"],
        "offer": offer,
        **{
            key: payload[key]
            for key in ("price_basis", "fetch_backend", "viajante_version")
            if key in payload
        },
        **{
            key: result[key]
            for key in ("applied", "resolved_place", "place_bounds")
            if key in result
        },
    }
    return deepcopy(quote)


def _age(payload, now):
    value = payload.get("searched_at")
    if not isinstance(value, str):
        return None
    try:
        then = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    current = (
        current.replace(tzinfo=timezone.utc)
        if current.tzinfo is None
        else current.astimezone(timezone.utc)
    )
    then = (
        then.replace(tzinfo=timezone.utc) if then.tzinfo is None else then.astimezone(timezone.utc)
    )
    return max(0, int((current - then).total_seconds()))


def _point(value) -> Optional[tuple[float, float]]:
    if not isinstance(value, dict):
        return None
    lat, lng = value.get("latitude"), value.get("longitude")
    if isinstance(lat, bool) or isinstance(lng, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return None
    return float(lat), float(lng)


def _places(hits) -> list[list[int]]:
    parent = list(range(len(hits)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    geos = []
    metros = []
    for airport in hits:
        geo = airport_geo(airport.iata)
        geos.append((geo[1], geo[2]) if geo else None)
        metros.append(metro_of(airport.iata))
    for left in range(len(hits)):
        for right in range(left + 1, len(hits)):
            same_metro = metros[left] and metros[left] == metros[right]
            close = False
            if geos[left] and geos[right]:
                close = _raw_distance_km(geos[left], geos[right][0], geos[right][1]) <= _PLACE_KM
            if same_metro or close:
                parent[find(left)] = find(right)
    groups: dict[int, list[int]] = {}
    for index in range(len(hits)):
        groups.setdefault(find(index), []).append(index)
    return list(groups.values())


def _near_places(hits, groups, point: tuple[float, float]) -> list[list[int]]:
    chosen = []
    for group in groups:
        close = False
        for index in group:
            geo = airport_geo(hits[index].iata)
            if geo and _raw_distance_km(point, geo[1], geo[2]) <= _PLACE_KM:
                close = True
                break
        if close:
            chosen.append(group)
    return chosen


def _city_decision(result, offer):
    """One catalogue place, or the offer's own coordinates. Otherwise inconclusive."""
    location = result["query"]["location"].strip()
    parts = [part.strip() for part in location.split(",")]
    if len(parts) > 2 or not parts[0]:
        raise ValueError("room rates require an unambiguous city; use City or City, ISO country")
    city = parts[0]
    country = parts[1].upper() if len(parts) == 2 else None
    hits = [
        airport
        for airport in lookup_airports(city, limit=100)
        if airport.city.casefold() == city.casefold()
        and (country is None or airport.country == country)
    ]
    if not hits:
        raise ValueError("room rates require an unambiguous city; use City or City, ISO country")
    groups = _places(hits)
    point = _point(offer)
    if len(groups) == 1:
        resolved = result.get("resolved_place")
        if resolved and resolved.casefold() not in (city.casefold(), location.casefold()):
            raise ValueError(
                "resolved place differs from the named city; room-rate city is unproven"
            )
        return location, point
    if point and len(_near_places(hits, groups, point)) == 1:
        return location, point
    names = ", ".join(sorted({hits[group[0]].iata for group in groups}))
    return None, (
        f"{city} matches more than one place ({names}); "
        "room rates need one place, or the original hotel's coordinates or id"
    )


def _require_bool(room_rates) -> None:
    if not isinstance(room_rates, bool):
        raise ValueError("room_rates must be a boolean")


def _lift(detail: dict, stamped: dict) -> dict:
    for key in _ENVELOPE:
        detail[key] = stamped[key]
    return detail


def _echo(rooms) -> str:
    """A confident match needs every party and date field the provider echoed."""
    missing = (
        rooms.answered_adults is None
        or rooms.answered_rooms is None
        or rooms.answered_check_in is None
        or rooms.answered_check_out is None
    )
    return "unknown" if missing else "matched"


def _accept_quotes(detail: dict, quotes: dict, rooms) -> dict:
    echo = _echo(rooms)
    detail["room_quotes"] = quotes
    detail["echo"] = echo
    _lift(detail, stamp_search(dict(quotes)))
    # Absence of an echo is not a confident match: never ok/complete.
    if echo == "unknown" and detail["status"] == "ok" and detail["completeness"] == "complete":
        detail["completeness"] = "partial"
    return detail


def _filtered(detail: dict, code: str) -> dict:
    return stamp_local(detail, status="no_results", error_code=code, empty_reason="filtered_out")


def _party_flags(query, rooms) -> dict[str, object]:
    flags: dict[str, object] = {}
    answered = {
        "adults": rooms.answered_adults,
        "rooms": rooms.answered_rooms,
        "check_in": rooms.answered_check_in.isoformat() if rooms.answered_check_in else None,
        "check_out": rooms.answered_check_out.isoformat() if rooms.answered_check_out else None,
    }
    occupancy = (
        rooms.answered_adults is not None and rooms.answered_adults != query["adults"]
    ) or (rooms.answered_rooms is not None and rooms.answered_rooms != query["rooms"])
    dates = (
        rooms.answered_check_in is not None
        and rooms.answered_check_in.isoformat() != query["check_in"]
    ) or (
        rooms.answered_check_out is not None
        and rooms.answered_check_out.isoformat() != query["check_out"]
    )
    if occupancy:
        flags["occupancy_mismatch"] = True
    if dates:
        flags["dates_mismatch"] = True
    if flags:
        flags["provider_answer"] = {
            key: value for key, value in answered.items() if value is not None
        }
    return flags


def get_hotel_details(
    report,
    query_index: int,
    offer_index: int,
    *,
    room_rates: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Read a hotel finalist. Optional Skiplagged room rates are a separate USD answer."""
    _require_bool(room_rates)
    payload, result, offer = _selected(report, query_index, offer_index)
    detail: dict[str, object] = {
        "original_quote": _quote(payload, result, offer),
        "age_seconds": _age(payload, now),
        "room_quotes": None,
        "limits": [
            "original terms apply only to the original quote",
            "unknown occupancy does not prove a party total",
            "unit conflicts remain unresolved",
            "missing cancellation deadlines remain unknown",
            "room rates do not prove combined capacity across rooms",
        ],
    }
    if not room_rates:
        return stamp_local(detail)
    query = result["query"]
    if date.fromisoformat(query["check_in"]) < date.today():
        raise ValueError("check-in date is in the past")
    owned_id = offer.get("provider_id") if payload.get("fetch_backend") == "skiplagged" else None
    anchor = None
    if owned_id is not None:
        try:
            hotel_id = int(owned_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Skiplagged finalist has no usable provider id") from exc
        named: Mapping[str, str] = {}
    else:
        hotel_id = None
        location, anchor = _city_decision(result, offer)
        if location is None:
            detail["room_rates_status"] = "inconclusive"
            detail["reason"] = anchor
            return stamp_local(detail, partial=True, error_code="ambiguous_city")
        named = {"hotel_name": offer["title"], "city": location}
    rooms = search_hotel_rooms(
        hotel_id,
        date.fromisoformat(query["check_in"]),
        date.fromisoformat(query["check_out"]),
        adults=query["adults"],
        rooms=query["rooms"],
        **named,
    )
    quotes = dict(rooms.to_dict())
    if rooms.error is not None:
        detail["room_quotes"] = quotes
        return _lift(detail, stamp_search(dict(quotes)))
    flags = _party_flags(query, rooms)
    if flags:
        detail.update(flags)
        code = "occupancy_mismatch" if flags.get("occupancy_mismatch") else "dates_mismatch"
        return _filtered(detail, code)
    if anchor is not None:
        quoted = _point(quotes)
        if quoted is None or _raw_distance_km(anchor, quoted[0], quoted[1]) > _PROPERTY_KM:
            detail["room_rates_status"] = "inconclusive"
            detail["reason"] = (
                "room quote coordinates do not match the original hotel; not a quote for that stay"
            )
            return _filtered(detail, "property_mismatch")
    return _accept_quotes(detail, quotes, rooms)
