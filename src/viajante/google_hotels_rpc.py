"""Owned Google Hotels AtySUc encode and compact parse."""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote, urlencode

from viajante.models import FETCH_LANGUAGE, HotelQuery, RawHotelCard

HOTELS_RPC_URL = "https://www.google.com/_/TravelFrontendUi/data/batchexecute"
HOTELS_RPC_ID = "AtySUc"
HOTELS_SEARCH_URL = "https://www.google.com/travel/search"
_ANTI_XSSI = ")]}'"
_PROPERTY_HOTELS = 1
_PROPERTY_VACATION_RENTALS = 2
_SORT_LOWEST_PRICE = 3
# Google drops brands/amenities/free-cancellation when this tail is missing.
_REQUEST_META = (1, None, None, None, None, None, 13, None, 0)
_HOTEL_ENTRY_KEY = "397419284"
NON_PROPERTY_TITLES = frozenset(
    {
        "closed",
        "open",
        "sold out",
        "unavailable",
        "no rooms",
        "fully booked",
    }
)


class HotelsParseMiss(ValueError):
    """Compact hotel body was not a readable stay-total payload."""


class EmptyHotelResults(Exception):
    """Hotel RPC returned a search echo with no priced stays."""


class HotelsRejected(Exception):
    """Hotel RPC rejected the query."""


class HotelsBlocked(Exception):
    """Hotel RPC was blocked or challenged."""


def build_hotels_inner(
    query: HotelQuery,
    *,
    currency: str = "EUR",
) -> list[Any]:
    dates_slot = [
        None,
        [
            [query.check_in.year, query.check_in.month, query.check_in.day],
            [query.check_out.year, query.check_out.month, query.check_out.day],
            query.nights,
        ],
        None,
        None,
        None,
        [None, 0],
    ]
    extras = _occupancy_extras(query)
    filter_details = [
        None,
        None,
        None,
        1 if query.free_cancellation else None,
        _SORT_LOWEST_PRICE,
        None,
        currency,
        None,
    ]
    search_params = [
        _PROPERTY_VACATION_RENTALS if query.entire_home else _PROPERTY_HOTELS,
        extras,
        [None, dates_slot],
        None,
        [filter_details, None, [], [None, None, 1]],
    ]
    return [f"{query.location} hotels", search_params, list(_REQUEST_META)]


def _occupancy_extras(query: HotelQuery) -> Any:
    # Google default occupancy is 2 adults / 1 room. Anything else must be sent.
    if query.adults == 2 and query.rooms == 1:
        return None
    return [[[3]] * query.adults, query.rooms]


def _rpc_params(html_lang: str, currency: str) -> dict[str, str]:
    return {
        "hl": html_lang,
        "curr": currency,
        "soc-app": "162",
        "soc-platform": "1",
        "soc-device": "1",
        "rt": "c",
    }


def build_hotels_request(
    query: HotelQuery,
    *,
    html_lang: str = FETCH_LANGUAGE,
    currency: str = "EUR",
) -> tuple[str, str]:
    inner = json.dumps(build_hotels_inner(query, currency=currency), separators=(",", ":"))
    envelope = [[[HOTELS_RPC_ID, inner, None, "1"]]]
    body = "f.req=" + quote(json.dumps(envelope, separators=(",", ":")), safe="")
    url = f"{HOTELS_RPC_URL}?{urlencode(_rpc_params(html_lang, currency))}"
    return url, body


HOTELS_POST_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Same-Domain": "1",
    "Origin": "https://www.google.com",
    "Referer": HOTELS_SEARCH_URL,
}


def parse_hotels_body(text: str) -> tuple[RawHotelCard, ...]:
    if _looks_blocked(text):
        raise HotelsBlocked("Google Hotels blocked the sweep")
    payload = _first_wrb_data(text)
    if payload is None:
        raise HotelsParseMiss("no wrb.fr hotel payload")
    records = _collect_hotel_records(payload)
    cards = tuple(card for record in records if (card := _record_to_card(record)) is not None)
    if cards:
        return cards
    if records:
        raise HotelsParseMiss("hotel records had no stay-total price")
    if _has_search_echo(payload):
        raise EmptyHotelResults()
    raise HotelsParseMiss("no hotel entries in compact payload")


def _looks_blocked(text: str) -> bool:
    lowered = text.casefold()
    return "/sorry/" in lowered or "unusual traffic" in lowered


def _first_wrb_data(text: str) -> Optional[Any]:
    body = text.lstrip()
    if body.startswith(_ANTI_XSSI):
        body = body[len(_ANTI_XSSI) :].lstrip()
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(body):
        while idx < len(body) and body[idx] in " \t\r\n":
            idx += 1
        if idx >= len(body):
            break
        if body[idx].isdigit():
            newline = body.find("\n", idx)
            if newline < 0:
                break
            length_text = body[idx:newline].strip()
            if length_text.isdigit():
                size = int(length_text)
                chunk = body[newline + 1 : newline + 1 + size]
                try:
                    obj, _ = decoder.raw_decode(chunk)
                except json.JSONDecodeError:
                    idx = newline + 1
                    continue
                found = _wrb_data(obj)
                if found is not None:
                    return found
                idx = newline + 1 + size
                continue
        try:
            obj, _ = decoder.raw_decode(body, idx)
        except json.JSONDecodeError:
            break
        return _wrb_data(obj)
    return None


def _wrb_data(obj: object) -> Optional[Any]:
    if (
        isinstance(obj, list)
        and obj
        and isinstance(obj[0], list)
        and obj[0]
        and obj[0][0] == "wrb.fr"
    ):
        obj = obj[0]
    if not (isinstance(obj, list) and len(obj) >= 3 and obj[0] == "wrb.fr"):
        return None
    if obj[2] is None:
        raise HotelsRejected("Google Hotels rejected this search.")
    if not isinstance(obj[2], str):
        return None
    try:
        return json.loads(obj[2])
    except json.JSONDecodeError as exc:
        raise HotelsParseMiss("wrb.fr data is not JSON") from exc


def _collect_hotel_records(payload: object) -> list[list[Any]]:
    found: list[list[Any]] = []
    _walk_records(payload, found)
    return found


def _walk_records(obj: object, found: list[list[Any]]) -> None:
    record = _as_hotel_record(obj)
    if record is not None:
        found.append(record)
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_records(item, found)
    elif isinstance(obj, dict):
        keyed = obj.get(_HOTEL_ENTRY_KEY)
        if isinstance(keyed, list) and keyed and isinstance(keyed[0], list):
            found.append(keyed[0])
            return
        for value in obj.values():
            _walk_records(value, found)


def _as_hotel_record(obj: object) -> Optional[list[Any]]:
    if not (isinstance(obj, list) and len(obj) >= 8 and isinstance(obj[1], str) and obj[1]):
        return None
    if _stay_total(obj) is not None or _nightly_pair(obj) is not None:
        return obj
    return None


def _stay_total(record: list[Any]) -> Optional[str]:
    try:
        pair = record[6][2][9]
    except (IndexError, TypeError):
        return None
    if not isinstance(pair, list) or not pair:
        return None
    # Slot 9 is usually a [?, stay-total] pair; live compact bodies now
    # send a bare single-element ["€71"] list instead. Prefer the pair
    # slot first so a leading nightly-ish figure never wins.
    candidates: list[Any] = []
    if len(pair) >= 2:
        candidates.append(pair[1])
    candidates.append(pair[0])
    for candidate in candidates:
        if not (isinstance(candidate, str) and candidate):
            continue
        if "€" not in candidate and not any(ch.isdigit() for ch in candidate):
            continue
        return candidate
    return None


def _nightly_pair(record: list[Any]) -> Optional[list[Any]]:
    try:
        pair = record[6][2][1]
    except (IndexError, TypeError):
        return None
    if isinstance(pair, list) and pair and isinstance(pair[0], str) and pair[0].startswith("€"):
        return pair
    return None


def _has_search_echo(payload: object) -> bool:
    if not isinstance(payload, list) or len(payload) < 2:
        return False
    echo = payload[1]
    if not isinstance(echo, list):
        return False
    return any(isinstance(item, str) and item.endswith(" hotels") for item in echo)


def _record_to_card(record: list[Any]) -> Optional[RawHotelCard]:
    total = _stay_total(record)
    if total is None:
        return None
    title = record[1]
    if not isinstance(title, str) or not title.strip():
        return None
    if title.strip().casefold() in NON_PROPERTY_TITLES:
        return None
    return RawHotelCard(
        title=title,
        address=_address(record),
        total_price=total,
        rating=_rating(record),
        details=_details(record),
        link=_link(record),
    )


def _address(record: list[Any]) -> Optional[str]:
    try:
        node: Any = record[2][1]
    except (IndexError, TypeError):
        return None
    while isinstance(node, list) and node:
        node = node[0]
    if isinstance(node, str) and node.strip():
        return node.strip()
    return None


def _rating(record: list[Any]) -> Optional[str]:
    try:
        score = record[7][0][0]
    except (IndexError, TypeError):
        return None
    if isinstance(score, (int, float)) and 0 <= float(score) <= 5:
        return f"{float(score):g}"
    return None


def _details(record: list[Any]) -> str:
    try:
        text = record[11][0]
    except (IndexError, TypeError):
        return ""
    return text.strip() if isinstance(text, str) else ""


def _link(record: list[Any]) -> Optional[str]:
    try:
        path = record[6][2][4][0]
    except (IndexError, TypeError):
        return None
    if isinstance(path, str) and path.startswith("/"):
        return "https://www.google.com" + path
    return None
