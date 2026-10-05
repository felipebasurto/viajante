"""Evidence for a selected finalist. Quotes remain separate across times/providers."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Mapping, Optional

from viajante.airports import lookup_airports
from viajante.flights import search_flights
from viajante.models import HotelSearchReport, QuerySuccess, SearchReport
from viajante.skiplagged_hotels import search_hotel_rooms


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
    return deepcopy(
        {
            "provider": payload.get("provider") or "google_flights",
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
    )


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


def flight_identity(offer: Mapping[str, object], query: Mapping[str, object]):
    journeys = offer.get("legs")
    expected = len(query.get("legs", ())) or (2 if query.get("trip") == "rt" else 1)
    if not isinstance(journeys, (list, tuple)) or len(journeys) != expected:
        return None
    identity = []
    fields = (
        "origin",
        "destination",
        "departure_date",
        "arrival_date",
        "departure",
        "arrival",
        "airline",
        "carrier",
        "flight_number",
    )
    for journey in journeys:
        segments = journey.get("segments")
        if not segments:
            return None
        rows = []
        for segment in segments:
            row = tuple(segment.get(field) for field in fields)
            if not all(isinstance(value, str) and value.strip() for value in row):
                return None
            rows.append(row)
        identity.append(tuple(rows))
    return tuple(identity)


def get_flight_details(
    report: SearchReport | Mapping[str, object],
    query_index: int,
    offer_index: int,
    *,
    refresh: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Read a report finalist, or re-shop its exact original query before truncation."""
    payload, result, offer = _selected(report, query_index, offer_index)
    original = _quote(payload, result, offer)
    completeness = offer.get("completeness") or {}
    detail = {
        "original_quote": original,
        "age_seconds": _age(payload, now),
        "unknown_fields": [key for key, value in completeness.items() if value == "unknown"]
        + [key for key in ("checked_bags", "carry_on") if offer.get(key) is None]
        + ["refund_terms", "extras", "current_availability"],
        "match_status": "snapshot",
        "new_quote": None,
    }
    if not refresh:
        return detail
    identity = flight_identity(offer, result["query"])
    if identity is None:
        return {**detail, "match_status": "incomplete_identity"}
    if not isinstance(report, SearchReport) or report.search_options is None:
        return {**detail, "match_status": "missing_search_context"}
    query_result = report.queries[query_index]
    if not isinstance(query_result, QuerySuccess):
        raise ValueError("selected query was not successful")
    if any(leg.departure_date < date.today() for leg in query_result.query.legs):
        raise ValueError("departure date is in the past")
    fresh = search_flights((query_result.query,), **report.search_options, _details_candidates=True)
    fresh_payload = fresh.to_dict()
    detail["refresh_result"] = fresh_payload
    fresh_result = fresh.queries[0]
    if not isinstance(fresh_result, QuerySuccess):
        return {**detail, "match_status": "provider_error"}
    fresh_row = fresh_payload["queries"][0]
    original_query = {
        key: value for key, value in result["query"].items() if key != "google_flights_url"
    }
    refreshed_query = {
        key: value for key, value in fresh_row["query"].items() if key != "google_flights_url"
    }
    if original_query != refreshed_query:
        return {**detail, "match_status": "query_mismatch"}
    matches = [
        index
        for index, candidate in enumerate(fresh_row["offers"])
        if flight_identity(candidate, fresh_row["query"]) == identity
    ]
    if len(matches) != 1:
        return {**detail, "match_status": "multiple_matches" if matches else "no_match"}
    index = matches[0]
    new = fresh_row["offers"][index]
    detail.update(
        match_status="matched",
        new_quote=_quote(fresh_payload, fresh_row, new),
        filter_violations=list(fresh_result.offers[index].refresh_filter_violations),
    )
    if payload["currency"] == fresh.currency:
        detail["price_change"] = round(new["price"] - offer["price"], 10)
    return detail


def _city(result):
    # Accept an exact catalogue city, optionally qualified by its ISO country.
    # Descriptions, street addresses and region vibes are not city evidence.
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
    if not hits or len({airport.country for airport in hits}) != 1:
        raise ValueError("room rates require an unambiguous city; use City or City, ISO country")
    resolved = result.get("resolved_place")
    if resolved and resolved.casefold() not in (city.casefold(), location.casefold()):
        raise ValueError("resolved place differs from the named city; room-rate city is unproven")
    return location


def get_hotel_details(
    report: HotelSearchReport | Mapping[str, object],
    query_index: int,
    offer_index: int,
    *,
    room_rates: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, object]:
    """Read a hotel finalist; optional Skiplagged room rates are a separate USD quote."""
    payload, result, offer = _selected(report, query_index, offer_index)
    detail = {
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
        return detail
    query = result["query"]
    if date.fromisoformat(query["check_in"]) < date.today():
        raise ValueError("check-in date is in the past")
    owned_id = offer.get("provider_id") if payload.get("fetch_backend") == "skiplagged" else None
    if owned_id is not None:
        try:
            hotel_id = int(owned_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Skiplagged finalist has no usable provider id") from exc
        named = {}
    else:
        hotel_id = None
        named = {"hotel_name": offer["title"], "city": _city(result)}
    rooms = search_hotel_rooms(
        hotel_id,
        date.fromisoformat(query["check_in"]),
        date.fromisoformat(query["check_out"]),
        adults=query["adults"],
        rooms=query["rooms"],
        **named,
    )
    detail["room_quotes"] = dict(rooms.to_dict())
    return detail
