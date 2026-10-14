"""Safe extraction of the public Google Flights page's ds:1 shopping data."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from typing import Iterator, Optional

from selectolax.lexbor import LexborHTMLParser

from viajante.google_flights_rpc import CompactParseMiss, RawFlightCard, parse_shopping_data

_CALL = re.compile(r"AF_initDataCallback\s*\(\s*\{")
_IDENTIFIER = re.compile(r"[$A-Za-z_][$\w]*")
_JSON = json.JSONDecoder()
_SHARED = threading.local()


@contextlib.contextmanager
def shared_page_tree(html: str) -> Iterator[None]:
    """Let every page check in this block reuse one HTML tree for ``html``.

    Building the tree is most of a public page's parse cost, and the echo checks and the
    ds:1 extraction read the same document. Only the exact string object passed here is
    shared; any other page is parsed as before.
    """
    previous = getattr(_SHARED, "slot", None)
    _SHARED.slot = [html, None]
    try:
        yield
    finally:
        _SHARED.slot = previous


def page_tree(html: str) -> LexborHTMLParser:
    """The HTML tree for ``html``, built once inside a ``shared_page_tree`` block."""
    slot = getattr(_SHARED, "slot", None)
    if slot is None or slot[0] is not html:
        return LexborHTMLParser(html)
    if slot[1] is None:
        slot[1] = LexborHTMLParser(html)
    return slot[1]


def _skip_space(source: str, index: int) -> int:
    while index < len(source) and source[index].isspace():
        index += 1
    return index


def _read_string(source: str, index: int) -> tuple[str, int]:
    quote = source[index]
    if quote not in "'\"":
        raise ValueError("expected string")
    start = index + 1
    index += 1
    escaped = False
    while index < len(source):
        char = source[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return source[start:index], index + 1
        index += 1
    raise ValueError("unterminated string")


def _skip_value(source: str, index: int) -> tuple[object, int]:
    """Value and end of one callback property value.

    JSON is decoded once in C and returned as an object. Other JS is scanned and returned as
    its text, which the caller may still have to reject. Unquoted JSON values are never
    strings, so a returned ``str`` always means scanned JS.
    """
    try:
        value, end = _JSON.raw_decode(source, index)
    except ValueError:
        pass
    else:
        return value, end
    start = index
    stack: list[str] = []
    pairs = {"[": "]", "{": "}", "(": ")"}
    while index < len(source):
        char = source[index]
        if char in "'\"":
            _, index = _read_string(source, index)
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in "]})":
            if not stack:
                break
            if stack.pop() != char:
                raise ValueError("unbalanced value")
        elif char == "," and not stack:
            break
        index += 1
    if stack:
        raise ValueError("unbalanced value")
    return source[start:index].strip(), index


def _callback_properties(source: str, index: int) -> tuple[Optional[str], object]:
    """Top-level key and data of a callback object literal, read from just after its "{"."""
    key_value: Optional[str] = None
    data_value: object = None
    while True:
        index = _skip_space(source, index)
        if index >= len(source):
            raise ValueError("unterminated callback object")
        char = source[index]
        if char == "}":
            return key_value, data_value
        if char == ",":
            index += 1
            continue
        if char in "'\"":
            property_key, index = _read_string(source, index)
        else:
            match = _IDENTIFIER.match(source, index)
            if not match:
                raise ValueError("invalid callback property")
            property_key = match.group(0)
            index = match.end()
        index = _skip_space(source, index)
        if index >= len(source) or source[index] != ":":
            raise ValueError("callback property has no value")
        index = _skip_space(source, index + 1)
        if index >= len(source):
            raise ValueError("callback property has no value")
        if source[index] in "'\"":
            raw_value, index = _read_string(source, index)
            if property_key == "key":
                key_value = raw_value
            if property_key == "data":
                # ds:1 data is a JSON array, so a string value is malformed.
                data_value = None
        else:
            raw_value, index = _skip_value(source, index)
            if property_key == "data":
                data_value = raw_value
        index = _skip_space(source, index)
        if index >= len(source) or source[index] not in ",}":
            raise ValueError("invalid callback separator")


def _ds1_data_in(script: str) -> Optional[list]:
    for match in _CALL.finditer(script):
        try:
            key, data = _callback_properties(script, match.end())
            if key != "ds:1" or data is None:
                continue
            if isinstance(data, str):
                data = json.loads(data)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return None


def extract_ds1_data(html: str) -> list:
    """Extract the balanced JSON data array for the page's ds:1 callback."""
    root = page_tree(html)
    for node in root.css("script"):
        if "ds:1" not in (node.attributes.get("class") or "").split():
            continue
        data = _ds1_data_in(node.text(deep=True))
        if data is not None:
            return data
    raise CompactParseMiss("Google Flights page has no readable ds:1 payload")


def parse_shopping_page(html: str, *, currency: str) -> tuple[RawFlightCard, ...]:
    """Parse public HTML shopping results through the owned compact-data decoder."""
    return parse_shopping_data(extract_ds1_data(html), currency=currency)
