"""Stable error body for MCP input failures. No SDK import.

Invalid input still fails the tool call (``isError``). The exception text is a JSON
object, ``{"error": {"code", "field", "message"}}``, so a client can parse it and a
person can still read ``message``. The SDK puts its own ``Error executing tool <name>: ``
prefix in front of it; parse from the first ``{``.

``field`` names the tool parameter the message is about. It is ``null`` whenever the
message does not single one out; a field is never guessed.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Mapping, Optional

INVALID_PARAMETER = "invalid_parameter"
SEARCH_IN_PROGRESS = "search_in_progress"
_BUSY_PHRASE = "already running in this process"

# Messages that name a concept rather than a parameter. First tool parameter that exists wins.
_ALIASES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(pattern), fields)
    for pattern, fields in (
        (
            r"unknown (origin|destination) iata|invalid route|invalid date|at least one route"
            r"|return date must be after outbound",
            ("routes", "route", "origin"),
        ),
        (r"^departure date is in the past", ("departure", "routes")),
        (r"currency", ("currency",)),
        (r"baggage buffer", ("baggage_buffer",)),
    )
)


def _named_fields(message: str, fields: Iterable[str]) -> list[str]:
    text = message.lower().replace("-", "_")
    return [
        name
        for name in fields
        if re.search(rf"(?<![a-z0-9_]){re.escape(name).replace('_', '[_ ]')}(?![a-z0-9_])", text)
    ]


def _quoted_fields(message: str, params: Mapping[str, object]) -> list[str]:
    """Parameters whose own text value is quoted in the message, as Python's errors do."""
    found = []
    for name, value in params.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        if any(isinstance(v, str) and v.strip() and repr(v) in message for v in values):
            found.append(name)
    return found


def infer_field(message: str, params: Mapping[str, object]) -> Optional[str]:
    """The one tool parameter a message is about, else None. Never a guess among several."""
    folded = message.lower()
    for pattern, candidates in _ALIASES:
        if pattern.search(folded):
            for name in candidates:
                if name in params:
                    return name
    for found in (_named_fields(message, params), _quoted_fields(message, params)):
        if len(found) == 1:
            return found[0]
    return None


def error_body(code: str, message: str, field: Optional[str] = None) -> dict[str, dict]:
    return {"error": {"code": code, "field": field, "message": message}}


def structured_error(exc: ValueError, params: Mapping[str, object]) -> ValueError:
    """Re-raise-ready ValueError whose text is the JSON error body."""
    message = str(exc)
    if _BUSY_PHRASE in message:
        body = error_body(SEARCH_IN_PROGRESS, message)
    else:
        body = error_body(INVALID_PARAMETER, message, infer_field(message, params))
    return ValueError(json.dumps(body))
