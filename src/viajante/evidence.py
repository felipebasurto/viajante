"""Owned evidence for MCP replies: what searches returned, and what a draft reply claims.

The ledger holds this process's recent search payloads. ``verify_answer`` flags
amounts, currencies, airport codes, ISO dates, and links in a draft that no
recorded payload owns. Reading the payload and choosing is the caller's job.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from copy import deepcopy
from typing import Iterable, Mapping, Optional
from uuid import uuid4

from viajante.airports import is_known_iata
from viajante.quote import _COUNTRY_CASH_CURRENCY

LEDGER_SIZE = 20
_MONEY_KEYS = frozenset(
    {
        "price",
        "price_change",
        "typical",
        "cheapest",
        "total_price",
        "total",
        "flight_fare",
        "hotel_stay",
        "baggage_buffer",
        "fee",
        "rate_per_person_night",
    }
)
_ISO_4217 = frozenset(_COUNTRY_CASH_CURRENCY.values()) | {"EUR", "USD"}
_GLYPH_CURRENCY = {"€": "EUR", "£": "GBP", "₹": "INR", "₩": "KRW"}
_URL = re.compile(r"https?://[^\s)\]>\"'`]+")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_CODE = re.compile(r"\b[A-Z]{3}\b")
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?P<pre>[A-Z]{{3}}|[$€£¥₹₩])\s?(?P<a>{_NUM})|(?P<b>{_NUM})\s?(?P<post>[A-Z]{{3}}\b|[€£¥₹₩])"
)

_lock = threading.Lock()
_ledger: deque[Mapping[str, object]] = deque(maxlen=LEDGER_SIZE)
_selection_groups: deque[dict[str, object]] = deque(maxlen=LEDGER_SIZE)
_selections: dict[str, object] = {}


def selection_records(payload: Mapping[str, object], report=None) -> dict[str, object]:
    """Attach opaque references to actual offers, never calendar/catalog cells."""
    records = {}

    def add_offers(result, query_index, typed, context):
        for offer_index, offer in enumerate(result.get("offers", ())):
            if not isinstance(offer, dict):
                continue
            kind = "hotel" if "total_price" in offer else "flight"
            if kind == "flight" and "legs" not in offer:
                continue
            selection_id = offer.setdefault("selection_id", "sel_" + uuid4().hex)
            snapshot = {
                key: deepcopy(context.get(key))
                for key in ("currency", "searched_at", "provider", "fetch_backend")
            }
            snapshot["queries"] = [deepcopy(result)]
            records[selection_id] = (
                kind,
                typed or snapshot,
                query_index if typed else 0,
                offer_index,
            )

    def walk(node, context=None, typed=None):
        if not isinstance(node, dict):
            return
        context = {**(context or {}), **node}
        queries = node.get("queries")
        if isinstance(queries, list):
            for query_index, result in enumerate(queries):
                if not isinstance(result, dict):
                    continue
                direct = typed if hasattr(typed, "queries") else None
                add_offers(result, query_index, direct, context)
                child = typed[query_index] if isinstance(typed, tuple) else None
                walk(result, context, child)
        # Flex owns actual shopping offers; the calendar cells remain reference-free.
        if "offers" in node and "queries" not in node and "query" not in node:
            shop = getattr(typed, "details_report", None)
            for index, offer in enumerate(node["offers"]):
                evidence = offer.get("evidence") or {}
                query = evidence.get("query")
                if query:
                    selected = {"query": query, "offers": [offer]}
                    add_offers(selected, 0, None, context)
                    selection_id = offer["selection_id"]
                    if shop is not None:
                        records[selection_id] = ("flight", shop, 0, index)
        for key in ("flights", "hotels"):
            walk(node.get(key), context, getattr(typed, key, None))

    walk(payload, typed=deepcopy(report))
    return records


def record(payload: Mapping[str, object], *, selections=None) -> Mapping[str, object]:
    with _lock:
        _ledger.append(deepcopy(payload))
        _selection_groups.append(selections or {})
        _selections.clear()
        for group in _selection_groups:
            _selections.update(group)
    return payload


def selected_reference(selection_id: str, kind: str):
    with _lock:
        selected = _selections.get(selection_id)
    if selected is None or selected[0] != kind:
        raise ValueError("unknown or evicted selection_id for this process and tool")
    return selected[1:]


def clear() -> None:
    with _lock:
        _ledger.clear()
        _selection_groups.clear()
        _selections.clear()


class _Owned:
    def __init__(self, payloads: Iterable[Mapping[str, object]]) -> None:
        self.amounts: list[float] = []
        self.currencies: set[str] = set()
        self.texts: set[str] = set()
        self.codes: set[str] = set()
        self.dates: set[str] = set()
        for payload in payloads:
            self._walk(payload, None)

    def _walk(self, node: object, key: Optional[str]) -> None:
        if isinstance(node, Mapping):
            for child_key, value in node.items():
                self._walk(value, str(child_key))
        elif isinstance(node, (list, tuple)):
            for value in node:
                self._walk(value, key)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            if key in _MONEY_KEYS:
                amount = float(node)
                self.amounts.append(abs(amount) if key == "price_change" else amount)
        elif isinstance(node, str):
            self.texts.add(node)
            self.dates.update(_ISO_DATE.findall(node))
            if key == "currency":
                self.currencies.add(node.upper())
            if not node.startswith("http"):
                self.codes.update(_CODE.findall(node.upper()))

    def owns_amount(self, value: float) -> bool:
        # ponytail: rounding tolerance is max(1, 0.5%); a caller rounding a JPY fare to
        # the nearest thousand is flagged. Upgrade: per-currency minor-unit tolerance.
        return any(abs(value - owned) <= max(1.0, owned * 0.005) for owned in self.amounts)


def verify_answer(answer: str) -> dict[str, object]:
    """Flag claims in a draft reply that no recorded search payload owns."""
    with _lock:
        payloads = list(_ledger)
    if not payloads:
        return {
            "ok": False,
            "searches": 0,
            "unowned": [],
            "note": "no search ran in this process yet; nothing in the draft is owned",
        }
    owned = _Owned(payloads)
    unowned: list[dict[str, str]] = []

    def flag(kind: str, text: str) -> None:
        row = {"kind": kind, "text": text}
        if row not in unowned:
            unowned.append(row)

    checked = 0
    rest = answer
    for url in _URL.findall(answer):
        checked += 1
        url = url.rstrip(".,;:!?")
        if url not in owned.texts:
            flag("url", url)
        rest = rest.replace(url, " ")
    for day in _ISO_DATE.findall(rest):
        checked += 1
        if day not in owned.dates:
            flag("date", day)
    rest = _ISO_DATE.sub(" ", rest)
    money_codes: set[str] = set()
    for match in _MONEY.finditer(rest):
        mark = match.group("pre") or match.group("post")
        code = _GLYPH_CURRENCY.get(mark, mark if mark in _ISO_4217 else None)
        if len(mark) == 3 and code is None:
            continue
        checked += 1
        text = match.group(0).strip()
        value = float((match.group("a") or match.group("b")).replace(",", ""))
        if not owned.owns_amount(value):
            flag("amount", text)
        if code is not None:
            money_codes.add(code)
            if code not in owned.currencies:
                flag("currency", text)
    for code in _CODE.findall(rest):
        if code in money_codes or code in _ISO_4217 or not is_known_iata(code):
            continue
        checked += 1
        if code not in owned.codes:
            flag("airport", code)
    return {
        "ok": not unowned,
        "searches": len(payloads),
        "checked": checked,
        "unowned": unowned,
        "note": (
            "every checked claim appears in a recorded search payload"
            if not unowned
            else "drop or re-search these; do not quote them as found"
        ),
    }


def failure_codes(node: object) -> list[str]:
    if isinstance(node, (list, tuple)):
        return [code for value in node for code in failure_codes(value)]
    if not isinstance(node, Mapping):
        return []
    found = []
    error = node.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("code"), str):
        found.append(error["code"])
    return found + [code for value in node.values() for code in failure_codes(value)]
