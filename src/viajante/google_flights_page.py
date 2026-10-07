"""Safe extraction of the public Google Flights page's ds:1 shopping data."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Optional

from viajante.google_flights_rpc import CompactParseMiss, RawFlightCard, parse_shopping_data

_CALL = re.compile(r"AF_initDataCallback\s*\(")
_IDENTIFIER = re.compile(r"[$A-Za-z_][$\w]*")


class _InitDataScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._collecting = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.casefold() != "script":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        self._collecting = "ds:1" in classes
        self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._collecting:
            self.scripts.append("".join(self._parts))
            self._collecting = False
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self._parts.append(data)


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


def _read_callback_object(source: str, start: int) -> tuple[str, int]:
    if start >= len(source) or source[start] != "{":
        raise ValueError("callback argument is not an object")
    value, end = _skip_value(source, start)
    if not value.endswith("}"):
        raise ValueError("unterminated callback object")
    return value[1:-1], end


def _callback_properties(source: str) -> tuple[Optional[str], Optional[str]]:
    """Read only top-level key/data properties from a callback object literal."""
    index = 0
    key_value: Optional[str] = None
    data_value: Optional[str] = None
    while index < len(source):
        index = _skip_space(source, index)
        while index < len(source) and source[index] == ",":
            index = _skip_space(source, index + 1)
        if index >= len(source):
            break
        if source[index] in "'\"":
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
        if index < len(source) and source[index] not in ",":
            raise ValueError("invalid callback separator")
    return key_value, data_value


def _extract_ds1_data(script: str) -> object:
    for match in _CALL.finditer(script):
        try:
            object_start = _skip_space(script, match.end())
            props, _ = _read_callback_object(script, object_start)
            key, raw_data = _callback_properties(props)
            if key != "ds:1" or raw_data is None:
                continue
            data = json.loads(raw_data)
            if isinstance(data, list):
                return data
        except (ValueError, json.JSONDecodeError):
            continue
    raise CompactParseMiss("no readable AF_initDataCallback ds:1 data")


def extract_ds1_data(html: str) -> object:
    """Extract the balanced JSON data array for the page's ds:1 callback."""
    parser = _InitDataScripts()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise CompactParseMiss("Google Flights page markup could not be read") from exc
    for script in parser.scripts:
        try:
            return _extract_ds1_data(script)
        except CompactParseMiss:
            continue
    raise CompactParseMiss("Google Flights page has no readable ds:1 payload")


def parse_shopping_page(html: str, *, currency: str) -> tuple[RawFlightCard, ...]:
    """Parse public HTML shopping results through the owned compact-data decoder."""
    return parse_shopping_data(extract_ds1_data(html), currency=currency)
