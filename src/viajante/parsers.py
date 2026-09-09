"""Pure text parsers for flight and hotel card fields."""

from __future__ import annotations

import re

from viajante.models import CancellationEvidence, LodgingKind, PropertyTypeEvidence

_PRICE_NUMBER = re.compile(r"([\d.,']+)")
_CURRENCY_PREFIX = re.compile(r"([A-Za-z]{3})")
_THREE_DEC_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
_APOSTROPHE_GROUPS = re.compile(r"^-?\d{1,3}(?:'\d{3})+(?:[.,]\d{1,2})?$")
_DURATION_DAYS = re.compile(r"(\d+)\s*(?:days?|d)\b")
_DURATION_HOURS = re.compile(r"(\d+)\s*(?:h|hr|hrs|hours?)\b")
_DURATION_MINUTES = re.compile(r"(\d+)\s*(?:min|mins|minutes?|m)\b")
_DIGITS = re.compile(r"(\d+)")


def _parse_grouped_number(num: str, *, three_decimal: bool) -> float:
    if "'" in num:
        if not _APOSTROPHE_GROUPS.fullmatch(num):
            raise ValueError("malformed apostrophe grouping")
        num = num.replace("'", "")
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            return float(num.replace(".", "").replace(",", "."))
        return float(num.replace(",", ""))
    if "," in num:
        parts = num.split(",")
        if len(parts) > 2:
            if not all(len(part) == 3 and part.isdigit() for part in parts[1:]):
                raise ValueError("inconsistent comma groups")
            return float("".join(parts))
        frac = parts[-1]
        if len(frac) <= 2 and frac.isdigit():
            return float(num.replace(",", "."))
        if len(frac) == 3 and frac.isdigit():
            return float("".join(parts))
        raise ValueError("malformed comma number")
    if "." in num:
        parts = num.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isdigit():
            if three_decimal:
                return float(num)
            return float(parts[0] + parts[1])
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            return float(parts[0] + "".join(parts[1:]))
        if len(parts) == 2 and len(parts[1]) <= 2:
            return float(num)
        raise ValueError("malformed dotted number")
    return float(num)


def parse_price(price_text: str | None) -> float | None:
    if not price_text:
        return None
    cleaned = (
        price_text.replace("\xa0", "")
        .replace(" ", "")
        .replace("€", "")
        .replace("\u2019", "'")
        .strip()
    )
    match = _PRICE_NUMBER.search(cleaned)
    if not match:
        return None
    num = match.group(1)
    if cleaned.startswith("-"):
        num = f"-{num}"
    leftover = (cleaned[: match.start()] + cleaned[match.end() :]).replace("-", "")
    if re.search(r"\d", leftover):
        return None
    iso = None
    prefix = _CURRENCY_PREFIX.search(cleaned[: match.start()])
    if prefix:
        iso = prefix.group(1).upper()
    try:
        return _parse_grouped_number(num, three_decimal=iso in _THREE_DEC_CURRENCIES)
    except ValueError:
        return None


def parse_duration_hours(duration: str | None) -> float | None:
    if not duration:
        return None
    text = duration.replace("\xa0", " ").strip().lower()
    dm = _DURATION_DAYS.search(text)
    hm = _DURATION_HOURS.search(text)
    mm = _DURATION_MINUTES.search(text)
    if not (dm or hm or mm):
        return None
    days = float(dm.group(1)) if dm else 0.0
    hours = float(hm.group(1)) if hm else 0.0
    minutes = float(mm.group(1)) if mm else 0.0
    return days * 24.0 + hours + minutes / 60.0


_CLOCK_12H = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])\b",
)
_CLOCK_24H = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\b",
)


def normalize_clock(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = text.replace("\xa0", " ").strip()
    match = _CLOCK_12H.match(cleaned)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        suffix = match.group(3).upper()
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None
        hour = hour % 12
        if suffix == "PM":
            hour += 12
        return f"{hour:02d}:{minute:02d}"
    match = _CLOCK_24H.match(cleaned)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        if 0 <= minute <= 59 and 0 <= hour <= 47:
            # Compact next-day arrivals sometimes render as 24:05 rather than 00:05.
            return f"{hour % 24:02d}:{minute:02d}"
    return None


def parse_stops_count(stops: str | None) -> int | None:
    if stops is None:
        return None
    text = stops.replace("\xa0", " ").strip()
    lower = text.lower()
    if lower in ("unknown",):
        return None
    if lower in (
        "nonstop",
        "non-stop",
        "direct",
    ):
        return 0
    m = _DIGITS.search(lower)
    if m:
        return int(m.group(1))
    return None


_RATING_PATTERNS = (
    re.compile(
        r"(?:rating|scored)\s*:?\s*(\d+[.,]\d+|\d+)",
        re.IGNORECASE,
    ),
    re.compile(r"^(\d+[.,]\d{1,2}|\d+)$", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d+[.,]\d{1,2})(?!\d)", re.IGNORECASE),
)


def parse_rating(rating_text: str | None) -> float | None:
    if not rating_text:
        return None
    text = rating_text.replace("\xa0", " ")
    for pattern in _RATING_PATTERNS:
        m = pattern.search(text)
        if m:
            score = float(m.group(1).replace(",", "."))
            if 0.0 <= score <= 10.0:
                return score
            return None
    return None


_FREE_CANCEL = re.compile(r"(?<!\bno\s)free\s+cancell?ation")
_NON_REFUNDABLE = re.compile(
    r"non[\s-]?refundable|"
    r"no\s+cancell?ation(?!\s+(?:fees?|charges?|costs?))"
)


def parse_cancellation_evidence(card_text: str | None) -> CancellationEvidence:
    if not card_text:
        return CancellationEvidence.UNKNOWN
    text = card_text.replace("\xa0", " ").lower()
    if _NON_REFUNDABLE.search(text):
        return CancellationEvidence.NON_REFUNDABLE
    if _FREE_CANCEL.search(text):
        return CancellationEvidence.FREE
    return CancellationEvidence.UNKNOWN


_ENTIRE_HOME = re.compile(r"entire\s+home|entire\s+apartment|whole\s+place")
_NOT_ENTIRE_HOME = re.compile(r"private\s+room|shared\s+room|hotel\s+room")


def parse_property_type_evidence(card_text: str | None) -> PropertyTypeEvidence:
    if not card_text:
        return PropertyTypeEvidence.UNKNOWN
    text = card_text.replace("\xa0", " ").lower()
    if _NOT_ENTIRE_HOME.search(text):
        return PropertyTypeEvidence.NOT_ENTIRE_HOME
    if _ENTIRE_HOME.search(text):
        return PropertyTypeEvidence.ENTIRE_HOME
    return PropertyTypeEvidence.UNKNOWN


_PRIVATE_ROOM = re.compile(r"private\s+room|shared\s+room")
_HOTEL_ROOM = re.compile(r"hotel\s+room")
_TITLE_ENTIRE_HOME = re.compile(r"apartments?\b")


def parse_lodging_kind(card_text: str | None, *, title: str | None = None) -> LodgingKind:
    text = (card_text or "").replace("\xa0", " ").lower()
    if text:
        if _PRIVATE_ROOM.search(text):
            return LodgingKind.PRIVATE_ROOM
        if _HOTEL_ROOM.search(text):
            return LodgingKind.HOTEL
        if _ENTIRE_HOME.search(text):
            return LodgingKind.ENTIRE_HOME
    title_text = (title or "").replace("\xa0", " ").lower()
    if title_text and _TITLE_ENTIRE_HOME.search(title_text):
        return LodgingKind.ENTIRE_HOME
    return LodgingKind.UNKNOWN


# Pattern order is load-bearing: first pattern that matches anywhere wins.
_BATHROOM_PATTERNS = (re.compile(r"(\d+)\s*bathrooms?"),)
_BEDROOM_PATTERNS = (re.compile(r"(\d+)\s*bedrooms?"),)
_BED_PATTERNS = (
    re.compile(r"(\d+)\s*beds\b"),
    re.compile(r"(\d+)\s*bed\b"),
)


def _first_int(text: str, patterns: tuple[re.Pattern[str], ...]) -> int | None:
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return int(m.group(1))
    return None


def parse_unit_hints(card_text: str | None) -> dict[str, int | None]:
    text = (card_text or "").replace("\xa0", " ").lower()
    return {
        "bedrooms": _first_int(text, _BEDROOM_PATTERNS),
        "bathrooms": _first_int(text, _BATHROOM_PATTERNS),
        "beds": _first_int(text, _BED_PATTERNS),
    }
