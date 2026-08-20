"""Pure text parsers for flight and hotel card fields."""

from __future__ import annotations

import re

from viajante.models import CancellationEvidence, LodgingKind, PropertyTypeEvidence

_PRICE_NUMBER = re.compile(r"([\d.,]+)")
_DURATION_DAYS = re.compile(r"(\d+)\s*(?:d[ií]as?|days?|d)\b")
_DURATION_HOURS = re.compile(r"(\d+)\s*(?:h|hr|hrs|hours?|horas?)\b")
_DURATION_MINUTES = re.compile(r"(\d+)\s*(?:min|mins|minutes?|minutos?|m)\b")
_DIGITS = re.compile(r"(\d+)")


def parse_price_eur(price_text: str | None) -> float | None:
    if not price_text:
        return None
    cleaned = price_text.replace("\xa0", "").replace(" ", "").replace("€", "").strip()
    m = _PRICE_NUMBER.search(cleaned)
    if not m:
        return None
    num = m.group(1)
    if cleaned.startswith("-"):
        num = f"-{num}"
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            return float(num.replace(".", "").replace(",", "."))
        return float(num.replace(",", ""))
    if "," in num:
        frac = num.rsplit(",", 1)[-1]
        if len(frac) <= 2 and frac.isdigit():
            return float(num.replace(",", "."))
        return float(num.replace(",", ""))
    if "." in num:
        parts = num.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isdigit():
            return float(parts[0] + parts[1])
        if len(parts) > 2 and all(len(p) == 3 for p in parts[1:]):
            return float(parts[0] + "".join(parts[1:]))
        if len(parts) == 2 and len(parts[1]) <= 2:
            return float(num)
    try:
        return float(num)
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
        "directo",
        "direct",
        "sin escalas",
        "sin paradas",
    ):
        return 0
    m = _DIGITS.search(lower)
    if m:
        return int(m.group(1))
    return None


def parse_rating(rating_text: str | None) -> float | None:
    if not rating_text:
        return None
    text = rating_text.replace("\xa0", " ")
    label_patterns = (
        r"(?:puntuaci[oó]n|valoraci[oó]n|rating|scored)\s*:?\s*(\d+[.,]\d+|\d+)",
        r"^(\d+[.,]\d{1,2}|\d+)$",
        r"(?<!\d)(\d+[.,]\d{1,2})(?!\d)",
    )
    for pattern in label_patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            score = float(m.group(1).replace(",", "."))
            if 0.0 <= score <= 10.0:
                return score
            return None
    return None


_FREE_CANCEL_PATTERNS = (
    r"(?<!\bno\s)(?<!\bsin\s)cancelaci[oó]n\s+gratuita",
    r"(?<!\bno\s)(?<!\bsin\s)cancelaci[oó]n\s+gratis",
    r"(?<!\bno\s)free\s+cancell?ation",
)

_NON_REFUNDABLE_PATTERNS = (
    r"no\s+reembolsable",
    r"non[\s-]?refundable",
    r"no\s+cancell?ation(?!\s+(?:fees?|charges?|costs?))",
    r"no\s+se\s+puede\s+cancelar",
)


def parse_cancellation_evidence(card_text: str | None) -> CancellationEvidence:
    if not card_text:
        return CancellationEvidence.UNKNOWN
    text = card_text.replace("\xa0", " ").lower()
    has_free = any(re.search(pat, text) for pat in _FREE_CANCEL_PATTERNS)
    has_non_refundable = any(re.search(pat, text) for pat in _NON_REFUNDABLE_PATTERNS)
    if has_non_refundable:
        return CancellationEvidence.NON_REFUNDABLE
    if has_free:
        return CancellationEvidence.FREE
    return CancellationEvidence.UNKNOWN


_ENTIRE_HOME_PATTERNS = (
    r"apartamento\s+entero",
    r"alojamiento\s+entero",
    r"entire\s+home",
    r"entire\s+apartment",
    r"whole\s+place",
    r"casa\s+entera",
)

_NOT_ENTIRE_HOME_PATTERNS = (
    r"habitaci[oó]n\s+privada",
    r"private\s+room",
    r"shared\s+room",
    r"habitaci[oó]n\s+compartida",
    r"hotel\s+room",
    r"habitaci[oó]n\s+de\s+hotel",
)


def parse_property_type_evidence(card_text: str | None) -> PropertyTypeEvidence:
    if not card_text:
        return PropertyTypeEvidence.UNKNOWN
    text = card_text.replace("\xa0", " ").lower()
    if any(re.search(pat, text) for pat in _NOT_ENTIRE_HOME_PATTERNS):
        return PropertyTypeEvidence.NOT_ENTIRE_HOME
    if any(re.search(pat, text) for pat in _ENTIRE_HOME_PATTERNS):
        return PropertyTypeEvidence.ENTIRE_HOME
    return PropertyTypeEvidence.UNKNOWN


_PRIVATE_ROOM_PATTERNS = (
    r"habitaci[oó]n\s+privada",
    r"private\s+room",
    r"shared\s+room",
    r"habitaci[oó]n\s+compartida",
)

_HOTEL_ROOM_PATTERNS = (
    r"hotel\s+room",
    r"habitaci[oó]n\s+de\s+hotel",
)


_TITLE_ENTIRE_HOME_PATTERNS = (
    r"apartamentos?\b",
    r"apartments?\b",
    r"\bcasa\b",
)


def parse_lodging_kind(card_text: str | None, *, title: str | None = None) -> LodgingKind:
    text = (card_text or "").replace("\xa0", " ").lower()
    if text:
        if any(re.search(pat, text) for pat in _PRIVATE_ROOM_PATTERNS):
            return LodgingKind.PRIVATE_ROOM
        if any(re.search(pat, text) for pat in _HOTEL_ROOM_PATTERNS):
            return LodgingKind.HOTEL
        if any(re.search(pat, text) for pat in _ENTIRE_HOME_PATTERNS):
            return LodgingKind.ENTIRE_HOME
    title_text = (title or "").replace("\xa0", " ").lower()
    if title_text and any(re.search(pat, title_text) for pat in _TITLE_ENTIRE_HOME_PATTERNS):
        return LodgingKind.ENTIRE_HOME
    return LodgingKind.UNKNOWN


def parse_unit_hints(card_text: str | None) -> dict[str, int | None]:
    text = (card_text or "").replace("\xa0", " ").lower()
    bathrooms = None
    bedrooms = None
    beds = None

    for pat in (
        r"(\d+)\s*baños?",
        r"(\d+)\s*bathrooms?",
    ):
        m = re.search(pat, text)
        if m:
            bathrooms = int(m.group(1))
            break

    for pat in (
        r"(\d+)\s*dormitorios?",
        r"(\d+)\s*habitaci[oó]n(?:es)?",
        r"(\d+)\s*bedrooms?",
    ):
        m = re.search(pat, text)
        if m:
            bedrooms = int(m.group(1))
            break

    for pat in (
        r"(\d+)\s*camas?\b",
        r"(\d+)\s*beds\b",
        r"(\d+)\s*bed\b",
    ):
        m = re.search(pat, text)
        if m:
            beds = int(m.group(1))
            break

    return {"bedrooms": bedrooms, "bathrooms": bathrooms, "beds": beds}
