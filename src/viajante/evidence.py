"""Owned evidence for MCP replies: what searches returned, and what a draft reply claims.

The ledger holds this process's recent search payloads. ``verify_answer`` flags
amounts, currencies, airport codes, ISO dates, and links in a draft that no
recorded payload owns. Reading the payload and choosing is the caller's job.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from typing import Iterable, Mapping, Optional

from viajante.airports import is_known_iata
from viajante.quote import _COUNTRY_CASH_CURRENCY

LEDGER_SIZE = 20
_MONEY_KEYS = frozenset(
    {
        "price",
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


def record(payload: Mapping[str, object]) -> Mapping[str, object]:
    with _lock:
        _ledger.append(payload)
    return payload


def owns_offer(evidence_id: str, price: float, currency: str) -> bool:
    """True when a recorded search returned an offer with this id, price and currency."""

    def found(node: object) -> bool:
        if isinstance(node, Mapping):
            proof = node.get("evidence")
            if (
                isinstance(proof, Mapping)
                and proof.get("evidence_id") == evidence_id
                and node.get("price") == price
                and str(node.get("currency") or proof.get("currency")).upper() == currency
            ):
                return True
            return any(found(child) for child in node.values())
        if isinstance(node, (list, tuple)):
            return any(found(child) for child in node)
        return False

    with _lock:
        return any(found(payload) for payload in _ledger)


def clear() -> None:
    with _lock:
        _ledger.clear()


class _Owned:
    def __init__(self, payloads: Iterable[Mapping[str, object]]) -> None:
        self.amounts: list[tuple[float, Optional[str]]] = []
        self.currencies: set[str] = set()
        self.texts: set[str] = set()
        self.codes: set[str] = set()
        self.dates: set[str] = set()
        for payload in payloads:
            self._walk(payload, None, None)

    def _walk(self, node: object, key: Optional[str], currency: Optional[str]) -> None:
        if isinstance(node, Mapping):
            if "currency" in node:
                value = node["currency"]
                currency = value.upper() if isinstance(value, str) else None
            for child_key, value in node.items():
                self._walk(value, str(child_key), currency)
        elif isinstance(node, (list, tuple)):
            for value in node:
                self._walk(value, key, currency)
        elif isinstance(node, bool) or node is None:
            return
        elif isinstance(node, (int, float)):
            if key in _MONEY_KEYS:
                self.amounts.append((float(node), currency))
        elif isinstance(node, str):
            self.texts.add(node)
            self.dates.update(_ISO_DATE.findall(node))
            if key == "currency":
                self.currencies.add(node.upper())
            if not node.startswith("http"):
                self.codes.update(_CODE.findall(node.upper()))

    def owns_amount(self, value: float, currency: Optional[str]) -> bool:
        # ponytail: rounding tolerance is max(1, 0.5%); a caller rounding a JPY fare to
        # the nearest thousand is flagged. Upgrade: per-currency minor-unit tolerance.
        return any(
            (currency is None or currency == code) and abs(value - owned) <= max(1.0, owned * 0.005)
            for owned, code in self.amounts
        )


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
        if not owned.owns_amount(value, code):
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
