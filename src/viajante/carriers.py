"""Airline IATA aliases and alliance designators for shopping filters.

Alliance codes are the IATA designators (*O / *S / *A), not invented
member lists. Airline names come from AIRLINE_CODE_ALIASES only.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Sequence, Tuple

AIRLINE_CODE_ALIASES = {
    "AF": ("air france",),
    "BA": ("british airways",),
    "FR": ("ryanair",),
    "I2": ("iberia express",),
    "IB": ("iberia",),
    "KL": ("klm",),
    "LH": ("lufthansa",),
    "RK": ("ryanair",),
    "TO": ("transavia",),
    "TP": ("tap", "tap air portugal"),
    "U2": ("easyjet", "easy jet"),
    "UX": ("air europa",),
    "VY": ("vueling",),
    "W6": ("wizz", "wizz air"),
}

# Enum names the Google Flights public page writes into a `tfs` leg when an
# alliance checkbox is ticked in the UI (captured from live page traffic).
ALLIANCE_TFS_CODE = {
    "oneworld": "ONEWORLD",
    "skyteam": "SKYTEAM",
    "star": "STAR_ALLIANCE",
}

ALLIANCE_ALIASES = {
    "oneworld": "oneworld",
    "one-world": "oneworld",
    "one world": "oneworld",
    "*o": "oneworld",
    "skyteam": "skyteam",
    "sky-team": "skyteam",
    "sky team": "skyteam",
    "*s": "skyteam",
    "star": "star",
    "star-alliance": "star",
    "staralliance": "star",
    "star alliance": "star",
    "*a": "star",
}

# Longest English alliance phrases first. Bare "star" is not a phrase.
ALLIANCE_PHRASES: tuple[tuple[str, str], ...] = (
    ("star alliance", "star"),
    ("star-alliance", "star"),
    ("staralliance", "star"),
    ("one world", "oneworld"),
    ("oneworld", "oneworld"),
    ("sky team", "skyteam"),
    ("skyteam", "skyteam"),
)


def _unique(codes: Sequence[str]) -> Tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return tuple(ordered)


def parse_airline_codes(text: Optional[str]) -> Optional[Tuple[str, ...]]:
    if text is None:
        return None
    codes = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not codes:
        raise ValueError("airline list must not be empty")
    for code in codes:
        if not (2 <= len(code) <= 3 and code.isalnum()):
            raise ValueError(f"invalid airline code: {code!r}")
    return codes


def parse_alliances(text: Optional[str]) -> Optional[Tuple[str, ...]]:
    if text is None:
        return None
    names: list[str] = []
    for part in text.split(","):
        token = " ".join(part.strip().casefold().replace("_", " ").replace("-", " ").split())
        if not token:
            continue
        canonical = ALLIANCE_ALIASES.get(token) or ALLIANCE_ALIASES.get(token.replace(" ", "-"))
        if canonical is None:
            raise ValueError(f"invalid alliance: {part.strip()!r}")
        names.append(canonical)
    if not names:
        raise ValueError("alliance list must not be empty")
    return _unique(names)


def tfs_carrier_codes(trip: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    include = list(getattr(trip, "airlines", None) or ())
    include.extend(
        ALLIANCE_TFS_CODE[name]
        for name in (getattr(trip, "alliances", None) or ())
        if name in ALLIANCE_TFS_CODE
    )
    exclude = list(getattr(trip, "exclude_airlines", None) or ())
    exclude.extend(
        ALLIANCE_TFS_CODE[name]
        for name in (getattr(trip, "exclude_alliances", None) or ())
        if name in ALLIANCE_TFS_CODE
    )
    return _unique(include), _unique(exclude)


_AIRLINE_STRIP = re.compile(r"[^a-z0-9 ]+")


def _normalize_airline(airline_text: Optional[str]) -> str:
    return _AIRLINE_STRIP.sub("", (airline_text or "").casefold())


def _airline_filter_hit(raw: Any, token: str) -> bool:
    needle = token.strip().upper()
    codes = {code.upper() for code in (raw.airline_codes or ())}
    if codes:
        return needle in codes
    name = _normalize_airline(raw.airline)
    names = (needle.casefold(), *AIRLINE_CODE_ALIASES.get(needle, ()))
    return any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", name) for alias in names)


def _passes_airline_filters(
    raw: Any,
    *,
    airlines: Optional[Sequence[str]],
    exclude_airlines: Optional[Sequence[str]],
) -> bool:
    if airlines and not any(_airline_filter_hit(raw, token) for token in airlines):
        return False
    if exclude_airlines and not (raw.airline_codes or _normalize_airline(raw.airline)):
        return False
    if exclude_airlines and any(_airline_filter_hit(raw, token) for token in exclude_airlines):
        return False
    return True
