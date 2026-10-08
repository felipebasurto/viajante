"""Safe extraction of the public Google Flights page's ds:1 shopping data."""

from __future__ import annotations

import json
import re
from typing import Optional

from selectolax.lexbor import LexborHTMLParser

from viajante.google_flights_rpc import CompactParseMiss, RawFlightCard, parse_shopping_data

_CALL = re.compile(r"AF_initDataCallback\s*\(\s*\{")
_IDENTIFIER = re.compile(r"[$A-Za-z_][$\w]*")
_JSON = json.JSONDecoder()


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


def _skip_value(source: str, index: int) -> tuple[str, int]:
    """Text and end of one callback property value. JSON is taken in C; other JS is scanned."""
    try:
        _, end = _JSON.raw_decode(source, index)
    except ValueError:
        pass
    else:
        return source[index:end], end
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


def _callback_properties(source: str, index: int) -> tuple[Optional[str], Optional[str]]:
    """Top-level key and data of a callback object literal, read from just after its "{"."""
    key_value: Optional[str] = None
    data_value: Optional[str] = None
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
            key, raw_data = _callback_properties(script, match.end())
            if key != "ds:1" or raw_data is None:
                continue
            data = json.loads(raw_data)
        except ValueError:
            continue
        if isinstance(data, list):
            return data
    return None


def extract_ds1_data(html: str) -> list:
    """Extract the balanced JSON data array for the page's ds:1 callback."""
    root = LexborHTMLParser(html)
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
