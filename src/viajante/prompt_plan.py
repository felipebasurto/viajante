"""Thin prompt → query plan for the graded bench battery.

This is not a booking flow. It maps a natural-language trip request onto
the owned CLI boundary (route grammar, IATA, trip kind, hotels occupancy)
so deterministic corpus cases can be checked offline.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence, Tuple

from viajante.airports import is_known_iata

Intent = str

_CITY_IATA = {
    "madrid": "MAD",
    "barcelona": "BCN",
    "oporto": "OPO",
    "porto": "OPO",
    "lisboa": "LIS",
    "lisbon": "LIS",
    "paris": "CDG",
    "parís": "CDG",
    "roma": "FCO",
    "rome": "FCO",
    "londres": "LHR",
    "london": "LHR",
    "nueva york": "JFK",
    "new york": "JFK",
    "tokio": "NRT",
    "tokyo": "NRT",
    "halifax": "YHZ",
    "fiji": "NAN",
    "fiyi": "NAN",
    "nadi": "NAN",
    "praga": "PRG",
    "prague": "PRG",
    "estambul": "IST",
    "istanbul": "IST",
    "auckland": "AKL",
    "sydney": "SYD",
    "pekin": "PEK",
    "pekín": "PEK",
    "beijing": "PEK",
    "shanghai": "PVG",
    "shanghái": "PVG",
    "delhi": "DEL",
    "nairobi": "NBO",
    "lagos": "LOS",
    "johannesburgo": "JNB",
    "johannesburg": "JNB",
    "suva": "SUV",
    "boston": "BOS",
    "chicago": "ORD",
    "los angeles": "LAX",
    "miami": "MIA",
    "cancun": "CUN",
    "cancún": "CUN",
    "dublin": "DUB",
    "amsterdam": "AMS",
    "frankfurt": "FRA",
    "munich": "MUC",
    "múnich": "MUC",
    "zurich": "ZRH",
    "zúrich": "ZRH",
    "viena": "VIE",
    "vienna": "VIE",
    "budapest": "BUD",
    "varsovia": "WAW",
    "warsaw": "WAW",
    "atenas": "ATH",
    "athens": "ATH",
    "dubai": "DXB",
    "singapur": "SIN",
    "singapore": "SIN",
    "hong kong": "HKG",
    "seul": "ICN",
    "seoul": "ICN",
    "osaka": "KIX",
    "melbourne": "MEL",
    "palma": "PMI",
    "mallorca": "PMI",
    "valencia": "VLC",
    "sevilla": "SVQ",
    "seville": "SVQ",
    "bilbao": "BIO",
    "malaga": "AGP",
    "málaga": "AGP",
    "milan": "MXP",
    "milán": "MXP",
    "napoles": "NAP",
    "nápoles": "NAP",
    "naples": "NAP",
    "palermo": "PMO",
    "marrakech": "RAK",
    "marrakesh": "RAK",
    "vancouver": "YVR",
    "cape town": "CPT",
    "buenos aires": "EZE",
    "sao paulo": "GRU",
    "são paulo": "GRU",
    "santiago": "SCL",
    "cairo": "CAI",
    "mumbai": "BOM",
    "bangkok": "BKK",
    "jakarta": "CGK",
    "manila": "MNL",
    "honolulu": "HNL",
    "perth": "PER",
    "christchurch": "CHC",
    "addis ababa": "ADD",
    "casablanca": "CMN",
    "lima": "LIM",
    "bogota": "BOG",
    "bogotá": "BOG",
    "mexico city": "MEX",
    "toronto": "YYZ",
    "doha": "DOH",
}

_WORD_NUMBERS = {
    "un": 1,
    "una": 1,
    "one": 1,
    "dos": 2,
    "two": 2,
    "tres": 3,
    "three": 3,
    "cuatro": 4,
    "four": 4,
    "cinco": 5,
    "five": 5,
    "seis": 6,
    "six": 6,
    "siete": 7,
    "seven": 7,
    "ocho": 8,
    "eight": 8,
}

_MONTHS = {
    "january": 1,
    "enero": 1,
    "february": 2,
    "febrero": 2,
    "march": 3,
    "marzo": 3,
    "april": 4,
    "abril": 4,
    "may": 5,
    "mayo": 5,
    "june": 6,
    "junio": 6,
    "july": 7,
    "julio": 7,
    "august": 8,
    "agosto": 8,
    "september": 9,
    "septiembre": 9,
    "setiembre": 9,
    "october": 10,
    "octubre": 10,
    "november": 11,
    "noviembre": 11,
    "december": 12,
    "diciembre": 12,
}

_IATA_PAIR = re.compile(r"\b([A-Z]{3})-([A-Z]{3})\b")
_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_FLAG = re.compile(
    r"--(trip|max-stops|adults|cabin|rooms|max-layover|min-layover|max-duration|"
    r"from|days|fetch)\s+(\S+)",
    re.IGNORECASE,
)
_SPANISH_DATE = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de\s+)?(20\d{2})\b",
    re.IGNORECASE,
)
_MONTH_YEAR = re.compile(
    r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
    r"octubre|noviembre|diciembre|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\s+(20\d{2})\b",
    re.IGNORECASE,
)

_BOOKING_PRIMARY = re.compile(
    r"reserva ya|book now|complete the booking|compra (el |los )?billetes|"
    r"introduce (la |tu )?tarjeta|pasarela de pago|finish checkout",
    re.IGNORECASE,
)
_TRAIN_PRIMARY = re.compile(
    r"\b(ave|renfe|tren|trenes|train|trains|rail|railway)\b",
    re.IGNORECASE,
)
_CAR_PRIMARY = re.compile(
    r"alquiler de coche|rental car|hire a car|coche de alquiler|rent[- ]a[- ]car",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return " ".join(text.replace("\u2014", "-").replace("\u2013", "-").split()).casefold()


def _contains_all(got: Any, wanted: Any) -> bool:
    got_list = [str(item) for item in (got or [])]
    if isinstance(wanted, str):
        wanted_list = [wanted]
    else:
        wanted_list = [str(item) for item in wanted]
    return all(item in got_list for item in wanted_list)


@dataclass(frozen=True)
class PromptPlan:
    intent: Intent
    origin: Optional[str] = None
    destination: Optional[str] = None
    destinations: Tuple[str, ...] = ()
    departure_date: Optional[date] = None
    return_date: Optional[date] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    trip: Optional[str] = None
    max_stops: Optional[int] = None
    adults: Optional[int] = None
    cabin: Optional[str] = None
    rooms: Optional[int] = None
    days: Optional[int] = None
    max_layover: Optional[float] = None
    min_layover: Optional[float] = None
    max_duration: Optional[float] = None
    location: Optional[str] = None
    check_in: Optional[date] = None
    check_out: Optional[date] = None
    airports_query: Optional[str] = None
    fetch: Optional[str] = None
    weekday: Optional[str] = None
    date_strategy: Optional[str] = None
    price_cap_eur: Optional[int] = None
    require_return_legs: bool = False
    require_arrival_clock: bool = False
    around_the_world: bool = False
    split_packages: bool = False
    exclude_regions: Tuple[str, ...] = ()
    exclude_airports: Tuple[str, ...] = ()
    via_regions: Tuple[str, ...] = ()
    no_overnight: Tuple[str, ...] = ()
    refuse: Tuple[str, ...] = ()
    route_specs: Tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        def iso(value: Optional[date]) -> Optional[str]:
            return value.isoformat() if value is not None else None

        return {
            "intent": self.intent,
            "origin": self.origin,
            "destination": self.destination,
            "destinations": list(self.destinations),
            "departure_date": iso(self.departure_date),
            "return_date": iso(self.return_date),
            "date_from": iso(self.date_from),
            "date_to": iso(self.date_to),
            "trip": self.trip,
            "max_stops": self.max_stops,
            "adults": self.adults,
            "cabin": self.cabin,
            "rooms": self.rooms,
            "days": self.days,
            "max_layover": self.max_layover,
            "min_layover": self.min_layover,
            "max_duration": self.max_duration,
            "location": self.location,
            "check_in": iso(self.check_in),
            "check_out": iso(self.check_out),
            "airports_query": self.airports_query,
            "fetch": self.fetch,
            "weekday": self.weekday,
            "date_strategy": self.date_strategy,
            "price_cap_eur": self.price_cap_eur,
            "require_return_legs": self.require_return_legs,
            "require_arrival_clock": self.require_arrival_clock,
            "around_the_world": self.around_the_world,
            "split_packages": self.split_packages,
            "exclude_regions": list(self.exclude_regions),
            "exclude_airports": list(self.exclude_airports),
            "via_regions": list(self.via_regions),
            "no_overnight": list(self.no_overnight),
            "refuse": list(self.refuse),
            "route_specs": list(self.route_specs),
            "notes": self.notes,
        }

    def matches(self, expect: Mapping[str, Any]) -> tuple[bool, str]:
        data = self.to_dict()
        subset_keys = {
            "refuse",
            "exclude_regions",
            "exclude_airports",
            "no_overnight",
        }
        exact_list_keys = {"via_regions", "route_specs", "destinations"}
        for key, wanted in expect.items():
            if key in {"notes", "id"}:
                continue
            got = data.get(key)
            if key in subset_keys:
                if not _contains_all(got, wanted):
                    return False, f"{key}: expected {wanted!r} in {got!r}"
                continue
            if key in exact_list_keys:
                got_list = list(got or [])
                wanted_list = list(wanted) if not isinstance(wanted, str) else [wanted]
                if got_list != wanted_list:
                    return False, f"{key}: expected {wanted_list!r}, got {got_list!r}"
                continue
            if key == "location":
                if got is None or str(got).casefold() != str(wanted).casefold():
                    return False, f"location: expected {wanted!r}, got {got!r}"
                continue
            if isinstance(wanted, bool):
                if bool(got) is not wanted:
                    return False, f"{key}: expected {wanted!r}, got {got!r}"
                continue
            if got != wanted:
                return False, f"{key}: expected {wanted!r}, got {got!r}"
        return True, "ok"


def _flag_map(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _FLAG.finditer(text):
        prefix = text[max(0, match.start() - 9) : match.start()].casefold()
        if prefix.endswith("sin ") or prefix.endswith("without "):
            continue
        found[match.group(1).casefold()] = match.group(2).strip(" ,.")
    return found


def _iso_dates(text: str) -> list[date]:
    dates: list[date] = []
    for raw in _ISO_DATE.findall(text):
        dates.append(date.fromisoformat(raw))
    for match in _SPANISH_DATE.finditer(text):
        month = _MONTHS.get(match.group(2).casefold())
        if month is None:
            continue
        dates.append(date(int(match.group(3)), month, int(match.group(1))))
    # de-dupe, preserve order
    seen: set[date] = set()
    unique: list[date] = []
    for item in dates:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _month_window(text: str) -> tuple[Optional[date], Optional[date]]:
    match = _MONTH_YEAR.search(text)
    if match is None:
        return None, None
    month = _MONTHS[match.group(1).casefold()]
    year = int(match.group(2))
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def _iata_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for origin, dest in _IATA_PAIR.findall(text):
        pairs.append((origin.upper(), dest.upper()))
    return pairs


def _first_city_iata(folded: str) -> Optional[str]:
    for name in sorted(_CITY_IATA, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", folded):
            return _CITY_IATA[name]
    return None


def _resolve_city_iata(name: str) -> Optional[str]:
    cleaned = " ".join(name.split())
    return _CITY_IATA.get(cleaned) or _CITY_IATA.get(cleaned.split()[0]) or _lookup_alias(cleaned)


def _city_pairs_from_text(text: str) -> list[tuple[str, str]]:
    """Every from/to city pair, in document order. Open jaws keep all of them."""
    folded = _fold(text)
    patterns = (
        (
            r"\b(?:de|desde|from)\s+([a-záéíóúüñ ]+?)\s+(?:a|to|hacia)\s+([a-záéíóúüñ ]+?)"
            r"(?:\s+(?:el|on|del|al|passing|pasando|,)|$)",
            False,
        ),
        (
            r"\bto\s+([a-záéíóúüñ ]+?)\s+from\s+([a-záéíóúüñ ]+?)"
            r"(?:\s+(?:passing|pasando|on|el|,)|$)",
            True,
        ),
    )
    found: list[tuple[int, str, str]] = []
    for pattern, swapped in patterns:
        for match in re.finditer(pattern, folded):
            left, right = match.group(1).strip(), match.group(2).strip()
            if swapped:
                dest_name, origin_name = left, right
            else:
                origin_name, dest_name = left, right
            origin = _resolve_city_iata(origin_name)
            dest = _resolve_city_iata(dest_name)
            if origin and dest and origin != dest:
                found.append((match.start(), origin, dest))
    found.sort(key=lambda item: item[0])
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _, origin, dest in found:
        pair = (origin, dest)
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def _lookup_alias(name: str) -> Optional[str]:
    cleaned = " ".join(name.split())
    if cleaned in _CITY_IATA:
        return _CITY_IATA[cleaned]
    for length in range(len(cleaned.split()), 0, -1):
        chunk = " ".join(cleaned.split()[:length])
        if chunk in _CITY_IATA:
            return _CITY_IATA[chunk]
    return None


def _hotel_location(text: str) -> Optional[str]:
    match = re.search(
        r"\b(?:hotel|hoteles|alojamiento|stay|stays)\s+(?:en|in|at)\s+([A-Za-zÁÉÍÓÚáéíóúüñ ]+)",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    raw = match.group(1)
    raw = re.split(r"\b(del|de|from|on|el|al|a|,|\d)", raw, maxsplit=1)[0].strip(" ,.")
    return raw or None


def _int_after(patterns: Sequence[str], folded: str) -> Optional[int]:
    for pattern in patterns:
        match = re.search(pattern, folded)
        if match is None:
            continue
        token = match.group(1)
        if token.isdigit():
            return int(token)
        if token in _WORD_NUMBERS:
            return _WORD_NUMBERS[token]
    return None


def _via_regions(folded: str) -> Tuple[str, ...]:
    regions: list[str] = []
    count_eu = re.search(r"(\d+)\s+(european|europeos?|aeropuertos europeos)", folded)
    if count_eu:
        regions.extend(["europe"] * int(count_eu.group(1)))
    elif re.search(r"\b(european|europeo|europeos|europe)\b", folded):
        regions.append("europe")
    if "sub-saharan" in folded or "subsaharian" in folded:
        regions.append("sub_saharan")
    if re.search(r"\b(indian|indio|india)\b", folded):
        regions.append("india")
    if re.search(r"\b(chinese|chino|china)\b", folded):
        regions.append("china")
    if "new zealand" in folded or "nueva zelanda" in folded:
        regions.append("new_zealand")
    return tuple(regions)


def _airports_query(text: str, folded: str) -> str:
    match = re.search(
        r"(?:code for|código iata de|codigo iata de|aeropuerto(?:s)? de)\s+"
        r"([A-Za-zÁÉÍÓÚáéíóúüñ ]+?)(?:\s+airports?)?\??$",
        text.strip(),
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" ?.")
    for name in (
        "london",
        "tokyo",
        "halifax",
        "cairo",
        "seoul",
        "vancouver",
        "singapore",
        "auckland",
    ):
        if re.search(rf"\b{name}\b", folded):
            return name
    iata = re.search(r"\b([A-Za-z]{3})\b", text)
    if iata and is_known_iata(iata.group(1)):
        return iata.group(1).upper()
    return text.strip()


def _is_airports_lookup(folded: str) -> bool:
    if re.search(r"\b(iata code|código iata|codigo iata|qué aeropuerto|que aeropuerto)\b", folded):
        return True
    if "lookup" in folded and "airport" in folded:
        return True
    if folded.startswith("aeropuertos ") or "airports lookup" in folded:
        return True
    return False


def _is_explore(folded: str) -> bool:
    return bool(
        re.search(
            r"\b(explora|explore|destinos|destinations|dónde ir|donde ir|"
            r"busca destinos|to everywhere|everywhere from|everywhere under|"
            r"where is cheap|cheap destinations)\b",
            folded,
        )
    )


def _is_dates_calendar(folded: str) -> bool:
    return bool(
        re.search(
            r"\b(calendario|calendar|date grid|cheapest friday|"
            r"más barato por día|mas barato por dia|qué día es más barato|"
            r"que dia es mas barato|cheapest day|price calendar)\b",
            folded,
        )
    )


def _is_hotels(folded: str) -> bool:
    return bool(re.search(r"\b(hotel|hoteles|alojamiento)\b", folded))


def _has_flight_words(folded: str) -> bool:
    return bool(
        re.search(
            r"\b(vuelo|vuelos|volar|flight|flights|one-way|ida|round-trip|open jaws?)\b",
            folded,
        )
    )


def _cabin(folded: str, flags: Mapping[str, str]) -> Optional[str]:
    if "cabin" in flags:
        value = flags["cabin"].casefold()
        if value in {"economy", "premium-economy", "business", "first"}:
            return value
    if "premium-economy" in folded or "premium economy" in folded:
        return "premium-economy"
    if re.search(r"\b(business|preferente)\b", folded):
        return "business"
    # Bare "first" is an English ordinal ("fixed dates first"), not first class.
    if re.search(
        r"\b(?:first(?:-|\s+)(?:class|cabin)|cabin(?:-|\s+)first|primera(?:\s+clase)?)\b",
        folded,
    ):
        return "first"
    if re.search(r"\b(economy|turista|econ[oó]mica)\b", folded):
        return "economy"
    return None


def _listed_destinations(text: str, origin: Optional[str]) -> Tuple[str, ...]:
    """IATA shortlist after 'destinations …: SCL, EZE'. Origin is not a dest."""
    match = re.search(
        r"(?:destinations?|destinos)\b[^:\n]{0,120}:\s*"
        r"((?:[A-Za-z]{3}(?:\s*,\s*)+)+[A-Za-z]{3})",
        text,
    )
    if match is None:
        return ()
    found: list[str] = []
    for code in re.findall(r"\b([A-Za-z]{3})\b", match.group(1)):
        upper = code.upper()
        if not is_known_iata(upper) or upper == origin or upper in found:
            continue
        found.append(upper)
    return tuple(found)


def _date_strategy(folded: str) -> Optional[str]:
    has_fixed_first = bool(re.search(r"fixed(?: natural)? dates first", folded))
    has_pm1 = bool(re.search(r"(?:±|\+/-)\s*1", folded))
    has_finalists = "finalist" in folded
    if has_fixed_first and (has_pm1 or has_finalists):
        return "fixed_then_plus_minus_1"
    return None


def _max_stops(folded: str, flags: Mapping[str, str]) -> Optional[int]:
    if "max-stops" in flags:
        try:
            value = int(flags["max-stops"])
        except ValueError:
            value = -1
        if value in (0, 1, 2):
            return value
    if re.search(r"max(?:imum)?\s+2\s+stops|m[aá]ximo 2 escalas|max 2 stops", folded):
        return 2
    if re.search(r"max(?:imum)?\s+1\s+stop|m[aá]ximo 1 escala", folded):
        return 1
    if re.search(r"sin escalas de m[aá]s", folded) or re.search(r"escalas sanas", folded):
        return None
    if re.search(r"\b(nonstop|directos?|sin escalas)\b", folded):
        return 0
    return None


def _trip_kind(folded: str, flags: Mapping[str, str], pair_count: int) -> Optional[str]:
    if "trip" in flags:
        raw = flags["trip"].casefold()
        if raw in {"rt", "round-trip", "round_trip"}:
            return "rt"
        if raw in {"multi", "multi-city"}:
            return "multi"
        if raw in {"one-way", "oneway"}:
            return "one-way"
    if "sin --trip" in folded or "sin trip rt" in folded or "dos one-way" in folded:
        return "one-way"
    if "without --trip" in folded or "without trip rt" in folded:
        return "one-way"
    if "two one-way" in folded or "two one ways" in folded:
        return "one-way"
    if "packaged" in folded or "empacada" in folded or "ida y vuelta" in folded:
        if "multi" in folded or "open jaw" in folded:
            return "multi"
        return "rt"
    if "round-trip" in folded or "round trip" in folded:
        return "rt"
    if "open jaw" in folded or pair_count >= 2 and "multi" in folded:
        return "multi"
    if pair_count >= 2:
        return "multi"
    return None


def _impossible_packaged_via(
    *,
    via_regions: Sequence[str],
    max_stops: Optional[int],
    pair_count: int,
    around: bool,
) -> bool:
    """True when one packaged O-D cannot visit the named via-regions under max_stops.

    A packaged shopping request with max K stops has at most K intermediate
    airports. Five continents with max 2 stops is not one BOS-SYD POST.
    Around-the-world shortlists and already-split multi-city pairs stay.
    """
    if around or pair_count >= 2 or not via_regions or max_stops is None:
        return False
    return len(via_regions) > max_stops


def _build_route_specs(
    *,
    origin: Optional[str],
    destination: Optional[str],
    dates: Sequence[date],
    trip: Optional[str],
    pairs: Sequence[tuple[str, str]],
) -> Tuple[str, ...]:
    dated_pairs: list[tuple[str, str, date]] = []
    if len(pairs) >= 2 and dates:
        for index, (origin_iata, dest_iata) in enumerate(pairs):
            when = dates[index] if index < len(dates) else dates[-1]
            dated_pairs.append((origin_iata, dest_iata, when))
        return tuple(f"{o}-{d}:{when.isoformat()}" for o, d, when in dated_pairs)
    if origin and destination and dates:
        if trip == "rt" and len(dates) >= 2:
            return (f"{origin}-{destination}:{dates[0].isoformat()}:{dates[1].isoformat()}",)
        if trip in {None, "one-way"} and len(dates) >= 2 and len(pairs) <= 1:
            if trip == "one-way":
                return (
                    f"{origin}-{destination}:{dates[0].isoformat()}",
                    f"{destination}-{origin}:{dates[1].isoformat()}",
                )
        return tuple(f"{origin}-{destination}:{when.isoformat()}" for when in dates)
    return ()


def plan_prompt(text: str, *, today: Optional[date] = None) -> PromptPlan:
    raw = " ".join(text.split())
    folded = _fold(raw)
    flags = _flag_map(raw)
    dates = _iso_dates(raw)
    pairs = _iata_pairs(raw)
    if not pairs:
        pairs = _city_pairs_from_text(raw)
    month_from, month_to = _month_window(raw)
    today = today or date.today()

    refuse: list[str] = []
    via_regions = _via_regions(folded)
    exclude_regions: list[str] = []
    exclude_airports: list[str] = []
    no_overnight: list[str] = []

    if re.search(r"\b(no asia|not asia)\b", folded) or "tercermundista" in folded:
        exclude_regions.append("asia")
    if re.search(r"\bist\b", folded) and (
        "overnight" in folded or "noche" in folded or "never" in folded or "nunca" in folded
    ):
        exclude_airports.append("IST")
        no_overnight.append("IST")

    around = "around the world" in folded or "vuelta al mundo" in folded
    rest_of_trip = "resto del viaje" in folded or "rest of the trip" in folded
    require_clock = bool(
        re.search(r"mostrar hora|clock not null|hora, no null|arrival(?:s)? must show", folded)
    )
    night = bool(re.search(r"\b(noche|night|nocturn)", folded))
    require_return = "return legs" in folded or (
        ("packaged" in folded or "empacada" in folded)
        and ("rt" in folded or "ida y vuelta" in folded)
    )

    max_layover: Optional[float] = None
    if "max-layover" in flags:
        max_layover = float(flags["max-layover"])
    else:
        lay = re.search(r"(?:como mucho|at most|max(?:imum)?|≤|<=)\s*(\d+(?:\.\d+)?)\s*h", folded)
        if lay is None:
            lay = re.search(r"(\d+(?:\.\d+)?)\s*horas", folded)
        if lay is None:
            lay = re.search(r"de m[aá]s de\s*(\d+(?:\.\d+)?)\s*h", folded)
        if lay is None:
            lay = re.search(r"(?:more than|over)\s*(\d+(?:\.\d+)?)\s*h", folded)
        if lay and (
            "escala" in folded
            or "layover" in folded
            or "sanas" in folded
            or re.search(r"\bsane\b", folded)
        ):
            max_layover = float(lay.group(1))

    min_layover: Optional[float] = None
    if "min-layover" in flags:
        min_layover = float(flags["min-layover"])
    max_duration: Optional[float] = None
    if "max-duration" in flags:
        max_duration = float(flags["max-duration"])
    else:
        dur = re.search(r"max(?:imum)? duration\s*(\d+)", folded)
        if dur:
            max_duration = float(dur.group(1))

    adults = None
    if "adults" in flags:
        adults = int(flags["adults"])
    else:
        adults = _int_after(
            (r"(\d+|ocho|eight|dos|two|tres|three|cuatro|four)\s+adultos", r"(\d+)\s+adults"),
            folded,
        )

    rooms = None
    if "rooms" in flags:
        rooms = int(flags["rooms"])
    else:
        rooms = _int_after(
            (
                r"(\d+|una|un|one|dos|two)\s+habitaciones",
                r"(\d+|una|un|one)\s+habitaci[oó]n",
                r"(\d+)\s+rooms?",
            ),
            folded,
        )

    days = None
    if "days" in flags:
        days = int(flags["days"])
    else:
        days_match = re.search(r"(\d+)\s*d[ií]as", folded)
        if days_match is None:
            days_match = re.search(r"(\d+)\s+days", folded)
        if days_match:
            days = int(days_match.group(1))

    cabin = _cabin(folded, flags)
    date_strategy = _date_strategy(folded)
    max_stops = _max_stops(folded, flags)
    fetch = flags.get("fetch")
    if require_clock or (night and ("hora" in folded or "clock" in folded)):
        fetch = fetch or "detail"

    weekday = None
    if "viernes" in folded or "friday" in folded:
        weekday = "friday"

    price_cap = None
    cap = re.search(r"(?:under|menos de|below|<)\s*(\d+)\s*€", folded)
    if cap is None:
        cap = re.search(r"(?:under|menos de|below)\s*(\d+)\s*(?:eur|euros)", folded)
    if cap:
        price_cap = int(cap.group(1))

    origin: Optional[str] = None
    destination: Optional[str] = None
    if pairs:
        origin, destination = pairs[0]
        if len(pairs) >= 2:
            destination = pairs[-1][1]
        if not is_known_iata(origin) or not all(
            is_known_iata(left) and is_known_iata(right) for left, right in pairs
        ):
            refuse.append("invalid_iata")
    elif around:
        origin = origin or _first_city_iata(folded)
    if origin is None:
        for match in re.findall(r"\b([A-Z]{3})\b", raw):
            if is_known_iata(match):
                origin = match
                break
    if destination is None:
        codes = [match for match in re.findall(r"\b([A-Z]{3})\b", raw) if is_known_iata(match)]
        if len(codes) >= 2:
            destination = codes[1]
        elif len(codes) == 1 and origin and codes[0] != origin:
            destination = codes[0]

    # "to Fiji from Halifax" already handled; city aliases for dest-only fantasy
    if origin is None and "halifax" in folded:
        origin = "YHZ"
    if destination is None and ("fiji" in folded or "fiyi" in folded or "nadi" in folded):
        destination = "NAN"

    destinations = _listed_destinations(raw, origin)

    trip = _trip_kind(folded, flags, len(pairs))
    if trip is None and origin and destination:
        trip = "one-way"
    if around:
        trip = trip or "multi"

    unknown_pairs = [
        pair for pair in pairs if not is_known_iata(pair[0]) or not is_known_iata(pair[1])
    ]
    if unknown_pairs and "invalid_iata" not in refuse:
        refuse.append("invalid_iata")

    if (
        len(dates) >= 2
        and dates[1] <= dates[0]
        and (trip == "rt" or "vuelta" in folded or "return" in folded or "ida" in folded)
    ):
        refuse.append("contradictory_dates")

    if (
        "nonstop" in folded
        and via_regions
        and ("fiji" in folded or "fiyi" in folded or destination == "NAN")
    ):
        refuse.append("contradictory_routing")
    elif _impossible_packaged_via(
        via_regions=via_regions,
        max_stops=max_stops,
        pair_count=len(pairs),
        around=around,
    ):
        refuse.append("impossible_routing")

    known_pairs = [pair for pair in pairs if is_known_iata(pair[0]) and is_known_iata(pair[1])]
    if "impossible_routing" in refuse:
        route_specs: Tuple[str, ...] = ()
    else:
        route_specs = _build_route_specs(
            origin=origin if origin and is_known_iata(origin) else None,
            destination=destination if destination and is_known_iata(destination) else None,
            dates=dates,
            trip=trip,
            pairs=known_pairs,
        )

    notes = ""
    if "impossible_routing" in refuse:
        via_n = len(via_regions)
        od = f"{origin}-{destination}" if origin and destination else "this route"
        notes = (
            f"A packaged {od} with max {max_stops} stops cannot touch "
            f"{via_n} via-regions; do not emit one shopping request or invent fares."
        )
    elif "open jaw" in folded and len(known_pairs) >= 2:
        notes = (
            "Keep every dated open-jaw city pair on --trip multi; "
            "do not collapse them into one origin-destination."
        )

    departure = dates[0] if dates else None
    returning = dates[1] if len(dates) >= 2 else None
    date_from = month_from
    date_to = month_to
    if len(dates) >= 2 and (
        _is_dates_calendar(folded) or _is_hotels(folded) or "calendario" in folded
    ):
        date_from = dates[0]
        date_to = dates[-1]

    location = _hotel_location(raw)
    check_in = dates[0] if _is_hotels(folded) and dates else None
    check_out = dates[1] if _is_hotels(folded) and len(dates) >= 2 else None

    if departure is not None and departure < today and not _is_hotels(folded):
        refuse.append("past_date")

    # Intent
    intent: Intent = "flights"
    extra_refuse: list[str] = []
    if _BOOKING_PRIMARY.search(raw) and not rest_of_trip:
        intent = "refuse"
        extra_refuse.append("booking")
    elif _CAR_PRIMARY.search(raw) and not _has_flight_words(folded) and not _is_hotels(folded):
        intent = "refuse"
        extra_refuse.append("cars")
    elif (
        _TRAIN_PRIMARY.search(raw)
        and not _has_flight_words(folded)
        and "airport" not in folded
        and not _is_explore(folded)
    ):
        intent = "refuse"
        extra_refuse.append("trains")
    elif _is_airports_lookup(folded):
        intent = "airports"
    elif _is_hotels(folded) and not _has_flight_words(folded) and not _is_explore(folded):
        intent = "hotels"
    elif _is_explore(folded):
        intent = "explore"
        if days is None:
            days = 7
        if origin is None:
            origin = _first_city_iata(folded)
    elif _is_dates_calendar(folded):
        intent = "dates"
        if date_from is None and departure is not None:
            date_from = departure
        if date_to is None and returning is not None:
            date_to = returning
        if date_from is None and month_from is not None:
            date_from = month_from
            date_to = month_to
    elif "invalid_iata" in refuse:
        intent = "refuse"
    elif (
        "contradictory_dates" in refuse
        or "contradictory_routing" in refuse
        or "impossible_routing" in refuse
    ):
        intent = "refuse"
    elif "past_date" in refuse:
        intent = "refuse"
    elif around or _has_flight_words(folded) or pairs or origin:
        if departure is None and not around and intent != "explore":
            intent = "refuse"
            extra_refuse.append("missing_date")
        else:
            intent = "flights"
    else:
        intent = "refuse"
        extra_refuse.append("missing_date")

    if rest_of_trip:
        extra_refuse.append("itinerary_rest")
        extra_refuse.append("hotels")
        extra_refuse.append("booking")

    if intent != "refuse":
        # keep invalid_iata as a hard refuse even if other intent words exist
        if (
            "invalid_iata" in refuse
            or "contradictory_dates" in refuse
            or "contradictory_routing" in refuse
            or "impossible_routing" in refuse
        ):
            intent = "refuse"
        elif "past_date" in refuse and intent == "flights":
            intent = "refuse"

    if intent == "refuse" and not extra_refuse and not refuse:
        extra_refuse.append("missing_date")

    all_refuse = tuple(dict.fromkeys([*refuse, *extra_refuse]))
    if intent != "refuse":
        # overlay refuses that are constraints, not the whole intent
        overlay = tuple(
            item
            for item in all_refuse
            if item in {"itinerary_rest", "hotels", "booking", "trains", "cars"}
        )
        all_refuse = overlay

    if intent == "airports":
        query = _airports_query(raw, folded)
        return PromptPlan(intent="airports", airports_query=query.casefold())

    if intent == "hotels":
        return PromptPlan(
            intent="hotels",
            location=location,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            rooms=rooms,
            refuse=all_refuse,
        )

    if intent == "explore":
        return PromptPlan(
            intent="explore",
            origin=origin,
            destinations=destinations,
            departure_date=departure,
            date_from=date_from or departure,
            date_to=date_to,
            days=days,
            adults=adults,
            cabin=cabin,
            date_strategy=date_strategy,
            max_stops=max_stops,
            price_cap_eur=price_cap,
            max_layover=max_layover,
            exclude_regions=tuple(exclude_regions),
            exclude_airports=tuple(exclude_airports),
            no_overnight=tuple(no_overnight),
            refuse=all_refuse,
        )

    if intent == "dates":
        return PromptPlan(
            intent="dates",
            origin=origin,
            destination=destination,
            date_from=date_from or departure,
            date_to=date_to or returning,
            weekday=weekday,
            adults=adults,
            cabin=cabin,
            max_stops=max_stops,
            route_specs=route_specs,
        )

    if intent == "refuse":
        return PromptPlan(
            intent="refuse",
            origin=origin if origin and is_known_iata(origin) else None,
            destination=destination if destination and is_known_iata(destination) else None,
            departure_date=departure,
            return_date=returning,
            trip=trip,
            max_stops=max_stops,
            refuse=all_refuse,
            via_regions=via_regions,
            route_specs=() if "impossible_routing" in all_refuse else route_specs,
            notes=notes,
        )

    split = bool(via_regions) and len(via_regions) + 1 > 6
    return PromptPlan(
        intent="flights",
        origin=origin,
        destination=destination,
        departure_date=departure,
        return_date=returning,
        date_from=date_from,
        date_to=date_to,
        trip=trip,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,
        days=days,
        max_layover=max_layover,
        min_layover=min_layover,
        max_duration=max_duration,
        fetch=fetch,
        weekday=weekday,
        price_cap_eur=price_cap,
        require_return_legs=require_return or trip == "rt",
        require_arrival_clock=require_clock,
        around_the_world=around,
        split_packages=split,
        exclude_regions=tuple(exclude_regions),
        exclude_airports=tuple(exclude_airports),
        via_regions=via_regions,
        no_overnight=tuple(no_overnight),
        refuse=all_refuse,
        route_specs=route_specs,
        notes=notes,
    )
