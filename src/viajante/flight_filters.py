"""Named local post-filters on parsed flight cards: clocks, layovers, via, overnight, bags."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from viajante.airports import get_airport, is_known_iata
from viajante.google_flights import RawFlightCard
from viajante.models import FlightOffer
from viajante.parsers import clock_minutes as _clock_minutes
from viajante.parsers import parse_stops_count

_CLOCK_TOKEN = re.compile(
    r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)


def _clock_token_minutes(text: str) -> Optional[int]:
    match = _CLOCK_TOKEN.fullmatch(text.strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    ampm = (match.group(3) or "").casefold()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _looks_like_clock_token(text: str) -> bool:
    stripped = text.strip()
    return ":" in stripped or bool(re.search(r"(?i)[ap]m$", stripped))


def parse_depart_window(text: Optional[str]) -> Optional[Tuple[int, int]]:
    """Parse a local departure window as inclusive minutes from midnight.

    Hour form `6-20` keeps the whole start and end hours (06:00–20:59).
    Clock form `06:00-20:00` is exact on both ends.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00")
    try:
        start_text, end_text = raw.split("-", 1)
    except ValueError as exc:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00") from exc
    start_text = start_text.strip()
    end_text = end_text.strip()
    if _looks_like_clock_token(start_text) or _looks_like_clock_token(end_text):
        start = _clock_token_minutes(start_text)
        end = _clock_token_minutes(end_text)
        if start is None or end is None:
            raise ValueError("depart window must look like 6-20 or 06:00-20:00")
        if start > end:
            raise ValueError("depart window start must be at or before the end")
        return start, end
    try:
        start_hour, end_hour = int(start_text), int(end_text)
    except ValueError as exc:
        raise ValueError("depart window must look like 6-20 or 06:00-20:00") from exc
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ValueError("depart window hours must be between 0 and 23")
    if start_hour > end_hour:
        raise ValueError("depart window start must be at or before the end hour")
    return start_hour * 60, end_hour * 60 + 59


def parse_named_clock(text: Optional[str], *, role: str = "clock") -> Optional[int]:
    """Parse a named HH:MM clock as minutes from midnight. Unnamed stays unset."""
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        raise ValueError(f"{role} must look like HH:MM")
    minutes = _clock_token_minutes(raw)
    if minutes is None:
        minutes = _clock_minutes(raw)
    if minutes is None:
        raise ValueError(f"{role} must look like HH:MM")
    return minutes


def validate_layover_hours(
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
) -> None:
    """Reject negative layover/duration caps and a min above the max."""
    if max_layover_hours is not None and max_layover_hours < 0:
        raise ValueError("max_layover_hours must not be negative")
    if min_layover_hours is not None and min_layover_hours < 0:
        raise ValueError("min_layover_hours must not be negative")
    if max_duration_hours is not None and max_duration_hours < 0:
        raise ValueError("max_duration_hours must not be negative")
    if (
        min_layover_hours is not None
        and max_layover_hours is not None
        and min_layover_hours > max_layover_hours
    ):
        raise ValueError("min layover must be at or below max layover")


OVERNIGHT_ANY = "any"


def parse_via_airports(text: Optional[str], *, role: str = "via") -> Optional[Tuple[str, ...]]:
    """Parse comma-separated IATA codes for a via / exclude-via post-filter."""
    if text is None:
        return None
    codes = tuple(part.strip().upper() for part in text.split(",") if part.strip())
    if not codes:
        raise ValueError(f"{role} list must not be empty")
    parsed: list[str] = []
    for code in codes:
        if len(code) != 3 or not code.isalpha() or not is_known_iata(code):
            raise ValueError(f"unknown {role} IATA code: {code!r}")
        if code not in parsed:
            parsed.append(code)
    return tuple(parsed)


def parse_overnight_airports(
    text: Optional[str], *, role: str = "no-overnight"
) -> Optional[Tuple[str, ...]]:
    """Parse comma-separated IATA (or ``any``) for a named overnight constraint."""
    if text is None:
        return None
    parts = tuple(part.strip() for part in text.split(",") if part.strip())
    if not parts:
        raise ValueError(f"{role} list must not be empty")
    parsed: list[str] = []
    for raw in parts:
        token = raw.upper()
        if token == "ANY":
            code = OVERNIGHT_ANY
        elif len(token) == 3 and token.isalpha() and is_known_iata(token):
            code = token
        else:
            raise ValueError(f"unknown {role} IATA code: {raw!r}")
        if code not in parsed:
            parsed.append(code)
    return tuple(parsed)


def parse_code_list(codes: Optional[Sequence[str]], *, role: str) -> Optional[Tuple[str, ...]]:
    """Owned IATA list from a named sequence; unnamed or empty stays None."""
    return parse_via_airports(",".join(codes), role=role) if codes else None


def parse_overnight_lists(
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
) -> tuple[Optional[Tuple[str, ...]], Optional[Tuple[str, ...]]]:
    """Parse both lists. Overlap keeps both; do not drop one."""
    parsed_no = (
        parse_overnight_airports(",".join(no_overnight), role="no-overnight")
        if no_overnight
        else None
    )
    parsed_require = (
        parse_overnight_airports(",".join(require_overnight), role="require-overnight")
        if require_overnight
        else None
    )
    return parsed_no, parsed_require


@dataclass(frozen=True)
class OfferFilters:
    """Named local post-filters on parsed cards. Field names are ``_normalize_offer`` kwargs."""

    max_layover_hours: Optional[float] = None
    min_layover_hours: Optional[float] = None
    max_duration_hours: Optional[float] = None
    depart_window: Optional[Tuple[int, int]] = None
    arrive_before: Optional[int] = None
    depart_after: Optional[int] = None
    via: Optional[Tuple[str, ...]] = None
    exclude_via: Optional[Tuple[str, ...]] = None
    no_overnight: Optional[Tuple[str, ...]] = None
    require_overnight: Optional[Tuple[str, ...]] = None

    @property
    def named(self) -> bool:
        return any(value is not None for value in vars(self).values())


NO_OFFER_FILTERS = OfferFilters()


def parse_offer_filters(
    *,
    max_layover_hours: Optional[float] = None,
    min_layover_hours: Optional[float] = None,
    max_duration_hours: Optional[float] = None,
    depart_window: Optional[Tuple[int, int]] = None,
    arrive_before: Optional[int] = None,
    depart_after: Optional[int] = None,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
) -> OfferFilters:
    validate_layover_hours(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
    )
    parsed_via = parse_code_list(via, role="via")
    parsed_exclude_via = parse_code_list(exclude_via, role="exclude-via")
    if parsed_via and parsed_exclude_via and set(parsed_via) & set(parsed_exclude_via):
        raise ValueError("via and exclude-via must not share a code")
    parsed_no, parsed_require = parse_overnight_lists(no_overnight, require_overnight)
    return OfferFilters(
        max_layover_hours=max_layover_hours,
        min_layover_hours=min_layover_hours,
        max_duration_hours=max_duration_hours,
        depart_window=depart_window,
        arrive_before=arrive_before,
        depart_after=depart_after,
        via=parsed_via,
        exclude_via=parsed_exclude_via,
        no_overnight=parsed_no,
        require_overnight=parsed_require,
    )


def _via_aliases(code: str) -> Tuple[str, ...]:
    aliases = [code.casefold()]
    airport = get_airport(code)
    if airport is not None and airport.city:
        city = airport.city.strip().casefold()
        if city and city not in aliases:
            aliases.append(city)
    return tuple(aliases)


def _token_matches_via(token: str, code: str) -> bool:
    folded = token.strip().casefold()
    if not folded:
        return False
    return folded in _via_aliases(code)


def _connection_tokens(raw: RawFlightCard) -> Tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(value: Optional[str]) -> None:
        if not value:
            return
        text = value.strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        tokens.append(text)

    add(raw.layover_city)
    for leg in raw.legs:
        for layover in leg.layovers:
            add(layover.city)
        airports: list[str] = []
        for segment in leg.segments:
            if segment.origin:
                airports.append(segment.origin)
            if segment.destination:
                airports.append(segment.destination)
        if len(airports) >= 3:
            for code in airports[1:-1]:
                add(code)
    return tuple(tokens)


def _connections_complete(raw: RawFlightCard) -> bool:
    """Owned locations for every connection, or an explicitly nonstop journey."""
    if not raw.legs:
        stops = parse_stops_count(raw.stops)
        return stops == 0 or (stops == 1 and bool(raw.layover_city))
    for leg in raw.legs:
        stops = parse_stops_count(leg.stops)
        if stops is None and len(raw.legs) == 1:
            stops = parse_stops_count(raw.stops)
        if stops is None:
            return False
        if stops == 0:
            continue
        if sum(bool(layover.city) for layover in leg.layovers) >= stops:
            continue
        if len(raw.legs) == 1 and stops == 1 and raw.layover_city:
            continue
        if len(leg.segments) != stops + 1 or not all(
            segment.origin and segment.destination for segment in leg.segments
        ):
            return False
    return True


def _passes_via_filters(
    raw: RawFlightCard,
    *,
    via: Optional[Sequence[str]] = None,
    exclude_via: Optional[Sequence[str]] = None,
) -> bool:
    if not via and not exclude_via:
        return True
    if exclude_via and not _connections_complete(raw):
        return False
    tokens = _connection_tokens(raw)
    if exclude_via and tokens:
        for code in exclude_via:
            if any(_token_matches_via(token, code) for token in tokens):
                return False
    if via:
        if not tokens:
            return False
        if not any(any(_token_matches_via(token, code) for token in tokens) for code in via):
            return False
    return True


def _overnight_from_owned_clocks(
    arrival: Optional[str],
    departure: Optional[str],
    hours: Optional[float],
) -> Optional[bool]:
    """True/False when owned clocks prove a night; None if unknown (do not invent)."""
    arr = _clock_minutes(arrival)
    dep = _clock_minutes(departure)
    if arr is None or dep is None:
        return None
    if dep < arr:
        return True
    if hours is None:
        return None
    extra = hours - (dep - arr) / 60.0
    if extra >= 23.0:
        return True
    if extra <= 1.0:
        return False
    return None


def _layover_nights(raw: RawFlightCard) -> Tuple[tuple[Optional[str], Optional[bool]], ...]:
    events: list[tuple[Optional[str], Optional[bool]]] = []
    for leg in raw.legs:
        segs = leg.segments
        lays = leg.layovers
        count = max(len(lays), max(0, len(segs) - 1))
        for index in range(count):
            city: Optional[str] = None
            hours: Optional[float] = None
            arr: Optional[str] = None
            dep: Optional[str] = None
            if index < len(lays):
                city = lays[index].city
                hours = lays[index].hours
            if index + 1 < len(segs):
                inbound = segs[index]
                outbound = segs[index + 1]
                arr = inbound.arrival
                dep = outbound.departure
                if not city:
                    city = inbound.destination or outbound.origin
            events.append((city, _overnight_from_owned_clocks(arr, dep, hours)))
    if not events and (raw.layover_city or raw.layover_hours is not None):
        events.append(
            (raw.layover_city, _overnight_from_owned_clocks(None, None, raw.layover_hours))
        )
    return tuple(events)


def _overnight_city_match(city: Optional[str], code: str) -> Optional[bool]:
    if code == OVERNIGHT_ANY:
        return True
    if not city or not city.strip():
        return None
    if _token_matches_via(city, code):
        return True
    return False


def _satisfies_no_overnight(
    events: Sequence[tuple[Optional[str], Optional[bool]]],
    codes: Sequence[str],
) -> bool:
    for code in codes:
        for city, overnight in events:
            match = _overnight_city_match(city, code)
            if match is False:
                continue
            if match is None:
                return False
            if overnight is False:
                continue
            return False
    return True


def _satisfies_require_overnight(
    events: Sequence[tuple[Optional[str], Optional[bool]]],
    codes: Sequence[str],
) -> bool:
    for code in codes:
        for city, overnight in events:
            if _overnight_city_match(city, code) is True and overnight is True:
                return True
    return False


def _passes_overnight_filters(
    raw: RawFlightCard,
    *,
    no_overnight: Optional[Sequence[str]] = None,
    require_overnight: Optional[Sequence[str]] = None,
) -> bool:
    if not no_overnight and not require_overnight:
        return True
    events = _layover_nights(raw)
    if require_overnight and not _satisfies_require_overnight(events, require_overnight):
        return False
    if no_overnight:
        if not _connections_complete(raw):
            return False
        if not events:
            stops = parse_stops_count(raw.stops)
            if stops is None or stops > 0:
                return False
        elif not _satisfies_no_overnight(events, no_overnight):
            return False
    return True


def owned_clock(text: Optional[str]) -> Optional[str]:
    return text if _clock_minutes(text) is not None else None


def _passes_depart_window(raw: RawFlightCard, window: Optional[Tuple[int, int]]) -> bool:
    if window is None:
        return True
    minutes = _clock_minutes(raw.departure)
    if minutes is None:
        return False
    start, end = window
    return start <= minutes <= end


def _passes_clock_bound(clock_text: Optional[str], bound: Optional[int], *, before: bool) -> bool:
    """Unknown clock cannot prove a named bound."""
    if bound is None:
        return True
    minutes = _clock_minutes(clock_text)
    if minutes is None:
        return False
    return minutes <= bound if before else minutes >= bound


def _eligible_stops(stops: Optional[str], max_stops: int) -> bool:
    count = parse_stops_count(stops)
    if count is not None:
        return count <= max_stops
    return max_stops >= 1


def _passes_bag_request(
    raw: RawFlightCard,
    *,
    bags: Optional[int],
    carry_on: Optional[int],
) -> bool:
    if bags is not None and raw.checked_bags is not None and raw.checked_bags < bags:
        return False
    if carry_on is not None and raw.carry_on is not None and raw.carry_on < carry_on:
        return False
    return True


def _meets_requirements(
    raw: RawFlightCard,
    offer: FlightOffer,
    max_stops: int,
    *,
    depart_window: Optional[Tuple[int, int]],
    arrive_before: Optional[int],
    depart_after: Optional[int],
    max_duration_hours: Optional[float],
    bags: Optional[int],
    carry_on: Optional[int],
) -> bool:
    """The requirements a recommendation may relax. Unknown clocks or stops cannot prove them."""
    if not _eligible_stops(raw.stops, max_stops):
        return False
    if not _passes_depart_window(raw, depart_window):
        return False
    if not _passes_bag_request(raw, bags=bags, carry_on=carry_on):
        return False
    if (
        max_duration_hours is not None
        and offer.duration_hours is not None
        and offer.duration_hours > max_duration_hours
    ):
        return False
    if not _passes_clock_bound(offer.arrival, arrive_before, before=True):
        return False
    return _passes_clock_bound(offer.departure, depart_after, before=False)
