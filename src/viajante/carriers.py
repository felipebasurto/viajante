"""Airline IATA aliases and alliance designators for shopping filters.

Alliance codes are the IATA designators (*O / *S / *A), not invented
member lists. Airline names come from AIRLINE_CODE_ALIASES only.
"""

from __future__ import annotations

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

ALLIANCE_SHOPPING_CODE = {
    "oneworld": "*O",
    "skyteam": "*S",
    "star": "*A",
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


def airline_names_longest_first() -> tuple[tuple[str, tuple[str, ...]], ...]:
    names: dict[str, list[str]] = {}
    for code, aliases in AIRLINE_CODE_ALIASES.items():
        for alias in aliases:
            names.setdefault(alias, []).append(code)
    return tuple(
        (name, tuple(codes))
        for name, codes in sorted(names.items(), key=lambda item: len(item[0]), reverse=True)
    )


def shopping_carrier_codes(trip: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    include = list(getattr(trip, "airlines", None) or ())
    include.extend(
        ALLIANCE_SHOPPING_CODE[name]
        for name in (getattr(trip, "alliances", None) or ())
        if name in ALLIANCE_SHOPPING_CODE
    )
    exclude = list(getattr(trip, "exclude_airlines", None) or ())
    exclude.extend(
        ALLIANCE_SHOPPING_CODE[name]
        for name in (getattr(trip, "exclude_alliances", None) or ())
        if name in ALLIANCE_SHOPPING_CODE
    )
    return _unique(include), _unique(exclude)


def carrier_filter_payload(
    include: Sequence[str],
    exclude: Sequence[str],
) -> Any:
    if not include and not exclude:
        return None
    include_codes = [[code] for code in include]
    exclude_codes = [[code] for code in exclude]
    if include and exclude:
        return [None, include_codes, exclude_codes]
    if include:
        return [None, include_codes]
    return [1, exclude_codes]
