"""Local civil times become instants only when the timezone resolves uniquely."""

from datetime import datetime, timezone
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_instant(value: datetime, zone: object) -> Optional[datetime]:
    if not isinstance(zone, str):
        return None
    try:
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    civil = value.replace(tzinfo=None)
    instants = set()
    for fold in (0, 1):
        aware = civil.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == civil:
            instants.add(utc)
    if len(instants) != 1:
        return None
    instant = next(iter(instants))
    if value.tzinfo is not None and value.astimezone(timezone.utc) != instant:
        return None
    return instant


def segment_instant(segment: Mapping[str, object], endpoint: str) -> Optional[datetime]:
    day, clock = segment.get(f"{endpoint}_date"), segment.get(endpoint)
    if not isinstance(day, str) or not isinstance(clock, str):
        return None
    try:
        civil = datetime.fromisoformat(f"{day}T{clock}")
    except ValueError:
        return None
    return local_instant(civil, segment.get(f"{endpoint}_timezone"))
