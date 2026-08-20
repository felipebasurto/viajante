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
    "heathrow": "LHR",
    "gatwick": "LGW",
    "newark": "EWR",
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
# Longest aliases first so "new york" wins over a shorter overlapping token.
_CITY_NAMES_LONGEST_FIRST: tuple[str, ...] = tuple(
    name for name, _iata in sorted(_CITY_IATA.items(), key=lambda item: len(item[0]), reverse=True)
)
_CITY_NAME_RANK: Mapping[str, int] = {
    name: index for index, name in enumerate(_CITY_NAMES_LONGEST_FIRST)
}
_CITY_IATA_COMBINED = re.compile(
    r"\b(?:" + "|".join(re.escape(name) for name in _CITY_NAMES_LONGEST_FIRST) + r")\b"
)
_FROM_TO_PAIR = re.compile(
    r"\b(?:de|desde|from)\s+([a-záéíóúüñ ]+?)\s+(?:a|to|hacia)\s+([a-záéíóúüñ ]+?)"
    r"(?:\s+(?:el|on|del|al|passing|pasando|,)|$)"
)
_TO_FROM_PAIR = re.compile(
    r"\bto\s+([a-záéíóúüñ ]+?)\s+from\s+([a-záéíóúüñ ]+?)"
    r"(?:\s+(?:passing|pasando|on|el|,)|$)"
)
_HOTEL_LOCATION = re.compile(
    r"\b(?:hotel|hoteles|alojamiento|stay|stays)\s+(?:en|in|at)\s+([A-Za-zÁÉÍÓÚáéíóúüñ ]+)",
    re.IGNORECASE,
)
_HOTEL_LOCATION_SPLIT = re.compile(r"\b(del|de|from|on|el|al|a|,|\d)")
_EUROPE_COUNT = re.compile(r"(\d+)\s+(european|europeos?|aeropuertos europeos)")
_EUROPE_WORD = re.compile(r"\b(european|europeo|europeos|europe)\b")
_INDIA_WORD = re.compile(r"\b(indian|indio|india)\b")
_CHINA_WORD = re.compile(r"\b(chinese|chino|china)\b")
_AIRPORTS_CODE_FOR = re.compile(
    r"(?:code for|código iata de|codigo iata de|aeropuerto(?:s)? de)\s+"
    r"([A-Za-zÁÉÍÓÚáéíóúüñ ]+?)(?:\s+airports?)?\??$",
    re.IGNORECASE,
)
_AIRPORTS_QUERY_CITIES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{name}\b"), name)
    for name in (
        "london",
        "tokyo",
        "halifax",
        "cairo",
        "seoul",
        "vancouver",
        "singapore",
        "auckland",
    )
)
_IATA_TOKEN = re.compile(r"\b([A-Za-z]{3})\b")
_IATA_TOKEN_UPPER = re.compile(r"\b([A-Z]{3})\b")
_AIRPORTS_LOOKUP = re.compile(
    r"\b(iata code|código iata|codigo iata|qué aeropuerto|que aeropuerto)\b"
)
_EXPLORE_WORDS = re.compile(
    r"\b(explora|explore|destinos|destinations|dónde ir|donde ir|"
    r"busca destinos|to everywhere|everywhere from|everywhere under|"
    r"where is cheap|cheap destinations)\b"
)
_DATES_CALENDAR = re.compile(
    r"\b(calendario|calendar|date grid|cheapest friday|"
    r"más barato por día|mas barato por dia|qué día es más barato|"
    r"que dia es mas barato|cheapest day|price calendar)\b"
)
_HOTEL_WORDS = re.compile(r"\b(hotels?|hoteles|alojamiento)\b")
_FLIGHT_WORDS = re.compile(
    r"\b(vuelo|vuelos|volar|fly|flight|flights|one-way|ida|round-trip|open jaws?)\b"
)
_BUSINESS_CABIN = re.compile(r"\b(business|preferente)\b")
_FIRST_CABIN = re.compile(
    r"\b(?:first(?:-|\s+)(?:class|cabin)|cabin(?:-|\s+)first|primera(?:\s+clase)?)\b"
)
_ECONOMY_CABIN = re.compile(r"\b(economy|turista|econ[oó]mica)\b")
_LISTED_DESTS = re.compile(
    r"(?:destinations?|destinos)\b[^:\n]{0,120}:\s*"
    r"((?:[A-Za-z]{3}(?:\s*,\s*)+)+[A-Za-z]{3})"
)
_FIXED_DATES_FIRST = re.compile(r"fixed(?: natural)? dates first")
_PLUS_MINUS_1 = re.compile(r"(?:±|\+/-)\s*1")
_MAX_2_STOPS = re.compile(r"max(?:imum)?\s+2\s+stops|m[aá]ximo 2 escalas|max 2 stops")
_MAX_1_STOP = re.compile(r"max(?:imum)?\s+1\s+stop|m[aá]ximo 1 escala|at most (?:one|1) stop")
_SIN_ESCALAS_MAS = re.compile(r"sin escalas de m[aá]s")
_ESCALAS_SANAS = re.compile(r"escalas sanas")
_NONSTOP = re.compile(r"\b(nonstop|directos?|sin escalas)\b")
_ADULTS_ES = re.compile(r"(\d+|ocho|eight|dos|two|tres|three|cuatro|four)\s+adultos")
_ADULTS_EN = re.compile(r"(\d+)\s+adults?")
_ROOMS_ES_PLURAL = re.compile(r"(\d+|una|un|one|dos|two)\s+habitaciones")
_ROOMS_ES_SINGULAR = re.compile(r"(\d+|una|un|one)\s+habitaci[oó]n")
_ROOMS_EN = re.compile(r"(\d+)\s+rooms?")
_DAYS_ES = re.compile(r"(\d+)\s*d[ií]as")
_DAYS_EN = re.compile(r"(\d+)\s+days")
_NO_ASIA = re.compile(r"\b(no asia|not asia)\b")
_IST_WORD = re.compile(r"\bist\b")
_REQUIRE_CLOCK = re.compile(r"mostrar hora|clock not null|hora, no null|arrival(?:s)? must show")
_NIGHT_WORD = re.compile(r"\b(noche|night|nocturn)")
_MAX_LAYOVER_H = re.compile(r"(?:como mucho|at most|max(?:imum)?|≤|<=)\s*(\d+(?:\.\d+)?)\s*h")
_LAYOVER_HORAS = re.compile(r"(\d+(?:\.\d+)?)\s*horas")
_LAYOVER_DE_MAS = re.compile(r"de m[aá]s de\s*(\d+(?:\.\d+)?)\s*h")
_LAYOVER_OVER = re.compile(r"(?:more than|over)\s*(\d+(?:\.\d+)?)\s*h")
_SANE_WORD = re.compile(r"\bsane\b")
_MAX_DURATION = re.compile(r"max(?:imum)? duration\s*(\d+)")
_PRICE_CAP_EUR_SIGN = re.compile(r"(?:under|menos de|below|<)\s*(\d+)\s*€")
_PRICE_CAP_EUR_WORD = re.compile(r"(?:under|menos de|below)\s*(\d+)\s*(?:eur|euros)")
_ASKED_TWO_ONE_WAYS = re.compile(
    r"two one-way|two one ways|without --trip|without trip rt|"
    r"sin --trip|sin trip rt|dos one-way|separate tickets",
    re.IGNORECASE,
)
_NAMED_RETURN = re.compile(
    r"\b(?:returning|return(?:ing)?(?:\s+on)?|ida y vuelta|vuelta|"
    r"back\s+(?:on|by|before|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday))\b",
    re.IGNORECASE,
)
_AIRPORT_NAME_TO_IATA = {
    "heathrow": "LHR",
    "gatwick": "LGW",
    "newark": "EWR",
    "kennedy": "JFK",
}
_NEGATED_AIRPORT_IATA = re.compile(r"\b(?:not|avoid|rather than|instead of)\s+([A-Z]{3})\b")
_NEGATED_AIRPORT_NAME = re.compile(
    r"\b(?:not|avoid|rather than|instead of)\s+(heathrow|gatwick|newark|kennedy)\b",
    re.IGNORECASE,
)
_PREFER_AIRPORT_IATA = re.compile(r"\b(?:use|into|out of|prefer)\s+([A-Z]{3})\b")
_PREFER_AIRPORT_NAME = re.compile(
    r"\b(?:use|into|out of|prefer)\s+(heathrow|gatwick|newark|kennedy)\b",
    re.IGNORECASE,
)
_CARRY_ON_ONLY = re.compile(
    r"\b(?:carry[- ]on only|hand luggage only|cabin bag only)\b",
    re.IGNORECASE,
)
_NO_CHECKED = re.compile(
    r"\bno checked(?:\s+(?:bags?|luggage|baggage))?\b",
    re.IGNORECASE,
)
_CHECKED_ONE = re.compile(
    r"\b(?:1|one)\s+checked(?:\s+(?:bag|bags|luggage|baggage))?\b",
    re.IGNORECASE,
)
_ARRIVE_BEFORE = re.compile(
    r"\b(?:arrive|land|be there)\s+(?:before|by)\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_DEPART_AFTER = re.compile(
    r"\b(?:depart|leave|departure)\s+after\s+"
    r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_WORK_BACK_BY = re.compile(
    r"\b(?:must work|work|back(?:\s+in)?|in the office)\s+"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:before|by|at))?\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_BACK_DAY_BEFORE = re.compile(
    r"\bback\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+before\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_MIN_LAYOVER_H = re.compile(
    r"(?:at least|min(?:imum)?|no less than|≥|>=)\s*(\d+(?:\.\d+)?)\s*h",
    re.IGNORECASE,
)
_NO_PLUS_LAYOVER = re.compile(r"no\s+(\d+(?:\.\d+)?)\s*h\+", re.IGNORECASE)
_NO_TRAINS = re.compile(
    r"\b(?:no trains?|not trains?|don'?t (?:take )?(?:the )?trains?)\b",
    re.IGNORECASE,
)
_NO_CARS = re.compile(
    r"\b(?:no rental cars?|don'?t hire a car|no (?:hire|rental) car)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(r"(?:do not|don'?t|never|without|not)\s+$", re.IGNORECASE)


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
    hotels: bool = False
    baggage: Optional[str] = None
    arrive_before: Optional[str] = None
    depart_after: Optional[str] = None
    prefer_airports: Tuple[str, ...] = ()
    work_back_by: Optional[str] = None
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
            "hotels": self.hotels,
            "baggage": self.baggage,
            "arrive_before": self.arrive_before,
            "depart_after": self.depart_after,
            "prefer_airports": list(self.prefer_airports),
            "work_back_by": self.work_back_by,
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
            "prefer_airports",
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


def _iter_city_iata(folded: str) -> list[tuple[str, str]]:
    """Each city-alias hit in text order as (alias, IATA)."""
    hits: list[tuple[str, str]] = []
    for match in _CITY_IATA_COMBINED.finditer(folded):
        name = match.group(0)
        hits.append((name, _CITY_IATA[name]))
    return hits


def _first_city_iata(folded: str) -> Optional[str]:
    # Same rule as the old per-alias loop: longest matching name, not leftmost.
    best: Optional[str] = None
    best_rank: Optional[int] = None
    for name, iata in _iter_city_iata(folded):
        rank = _CITY_NAME_RANK[name]
        if best_rank is None or rank < best_rank:
            best = iata
            best_rank = rank
    return best


def _resolve_city_iata(name: str) -> Optional[str]:
    cleaned = " ".join(name.split())
    return _CITY_IATA.get(cleaned) or _CITY_IATA.get(cleaned.split()[0]) or _lookup_alias(cleaned)


def _city_pairs_from_text(text: str) -> list[tuple[str, str]]:
    """Every from/to city pair, in document order. Open jaws keep all of them."""
    folded = _fold(text)
    found: list[tuple[int, str, str]] = []
    for pattern, swapped in ((_FROM_TO_PAIR, False), (_TO_FROM_PAIR, True)):
        for match in pattern.finditer(folded):
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
    match = _HOTEL_LOCATION.search(text)
    if match is None:
        return None
    raw = match.group(1)
    raw = _HOTEL_LOCATION_SPLIT.split(raw, maxsplit=1)[0].strip(" ,.")
    return raw or None


def _int_after(patterns: Sequence[re.Pattern[str]], folded: str) -> Optional[int]:
    for pattern in patterns:
        match = pattern.search(folded)
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
    count_eu = _EUROPE_COUNT.search(folded)
    if count_eu:
        regions.extend(["europe"] * int(count_eu.group(1)))
    elif _EUROPE_WORD.search(folded):
        regions.append("europe")
    if "sub-saharan" in folded or "subsaharian" in folded:
        regions.append("sub_saharan")
    if _INDIA_WORD.search(folded):
        regions.append("india")
    if _CHINA_WORD.search(folded):
        regions.append("china")
    if "new zealand" in folded or "nueva zelanda" in folded:
        regions.append("new_zealand")
    return tuple(regions)


def _airports_query(text: str, folded: str) -> str:
    match = _AIRPORTS_CODE_FOR.search(text.strip())
    if match:
        return match.group(1).strip(" ?.")
    for pattern, name in _AIRPORTS_QUERY_CITIES:
        if pattern.search(folded):
            return name
    iata = _IATA_TOKEN.search(text)
    if iata and is_known_iata(iata.group(1)):
        return iata.group(1).upper()
    return text.strip()


def _is_airports_lookup(folded: str) -> bool:
    if _AIRPORTS_LOOKUP.search(folded):
        return True
    if "lookup" in folded and "airport" in folded:
        return True
    if folded.startswith("aeropuertos ") or "airports lookup" in folded:
        return True
    return False


def _is_explore(folded: str) -> bool:
    return bool(_EXPLORE_WORDS.search(folded))


def _is_dates_calendar(folded: str) -> bool:
    return bool(_DATES_CALENDAR.search(folded))


def _is_hotels(folded: str) -> bool:
    return bool(_HOTEL_WORDS.search(folded))


def _has_flight_words(folded: str) -> bool:
    return bool(_FLIGHT_WORDS.search(folded))


def _cabin(folded: str, flags: Mapping[str, str]) -> Optional[str]:
    if "cabin" in flags:
        value = flags["cabin"].casefold()
        if value in {"economy", "premium-economy", "business", "first"}:
            return value
    if "premium-economy" in folded or "premium economy" in folded:
        return "premium-economy"
    if _BUSINESS_CABIN.search(folded):
        return "business"
    # Bare "first" is an English ordinal ("fixed dates first"), not first class.
    if _FIRST_CABIN.search(folded):
        return "first"
    if _ECONOMY_CABIN.search(folded):
        return "economy"
    return None


def _listed_destinations(text: str, origin: Optional[str]) -> Tuple[str, ...]:
    """IATA shortlist after 'destinations …: SCL, EZE'. Origin is not a dest."""
    match = _LISTED_DESTS.search(text)
    if match is None:
        return ()
    found: list[str] = []
    for code in _IATA_TOKEN.findall(match.group(1)):
        upper = code.upper()
        if not is_known_iata(upper) or upper == origin or upper in found:
            continue
        found.append(upper)
    return tuple(found)


def _date_strategy(folded: str) -> Optional[str]:
    has_fixed_first = bool(_FIXED_DATES_FIRST.search(folded))
    has_pm1 = bool(_PLUS_MINUS_1.search(folded))
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
    if _MAX_2_STOPS.search(folded):
        return 2
    if _MAX_1_STOP.search(folded):
        return 1
    if _SIN_ESCALAS_MAS.search(folded) or _ESCALAS_SANAS.search(folded):
        return None
    if _NONSTOP.search(folded):
        return 0
    return None


def _trip_kind(
    folded: str,
    flags: Mapping[str, str],
    pair_count: int,
    *,
    date_count: int = 0,
) -> Optional[str]:
    if "trip" in flags:
        raw = flags["trip"].casefold()
        if raw in {"rt", "round-trip", "round_trip"}:
            return "rt"
        if raw in {"multi", "multi-city"}:
            return "multi"
        if raw in {"one-way", "oneway"}:
            return "one-way"
    if _ASKED_TWO_ONE_WAYS.search(folded):
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
    # Out+back dates without an explicit split are one packaged --trip rt.
    if date_count >= 2 and pair_count <= 1 and _NAMED_RETURN.search(folded):
        return "rt"
    return None


def asked_two_one_ways(text: str) -> bool:
    """True when the user asked to split outbound and return as two one-ways."""
    return bool(_ASKED_TWO_ONE_WAYS.search(_fold(text)))


def wants_packaged_rt(text: str) -> bool:
    """True when the prompt names out+back and did not ask to split tickets."""
    folded = _fold(text)
    if _ASKED_TWO_ONE_WAYS.search(folded):
        return False
    if len(_iso_dates(text)) < 2:
        return False
    pairs = _iata_pairs(text) or _city_pairs_from_text(text)
    if len(pairs) >= 2:
        return False
    return bool(_NAMED_RETURN.search(folded))


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
    split_return: bool = False,
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
        if split_return and trip == "one-way" and len(dates) >= 2 and len(pairs) <= 1:
            return (
                f"{origin}-{destination}:{dates[0].isoformat()}",
                f"{destination}-{origin}:{dates[1].isoformat()}",
            )
        return tuple(f"{origin}-{destination}:{when.isoformat()}" for when in dates)
    return ()


def _hhmm(raw: str) -> Optional[str]:
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*", raw, re.IGNORECASE)
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
    return f"{hour:02d}:{minute:02d}"


def _match_is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 24) : start].casefold()
    return bool(_NEGATION_BEFORE.search(prefix))


def _baggage(folded: str) -> Optional[str]:
    if _CARRY_ON_ONLY.search(folded):
        return "carry_on_only"
    if _NO_CHECKED.search(folded):
        return "no_checked"
    if _CHECKED_ONE.search(folded):
        return "checked_1"
    return None


def _work_back_by(folded: str) -> Optional[str]:
    match = _BACK_DAY_BEFORE.search(folded) or _WORK_BACK_BY.search(folded)
    if match is None:
        return None
    clock = _hhmm(match.group(2))
    if clock is None:
        return None
    return f"{match.group(1).casefold()} {clock}"


def _named_iata_codes(raw: str, folded: str) -> set[str]:
    named: set[str] = set()
    for token in _IATA_TOKEN_UPPER.findall(raw):
        if is_known_iata(token):
            named.add(token)
    for name, code in _AIRPORT_NAME_TO_IATA.items():
        if name in folded:
            named.add(code)
    return named


def _excluded_airports(raw: str) -> list[str]:
    found: list[str] = []
    for match in _NEGATED_AIRPORT_IATA.finditer(raw):
        code = match.group(1).upper()
        if is_known_iata(code) and code not in found:
            found.append(code)
    for match in _NEGATED_AIRPORT_NAME.finditer(raw):
        code = _AIRPORT_NAME_TO_IATA[match.group(1).casefold()]
        if code not in found:
            found.append(code)
    return found


def _use_airports(raw: str) -> list[str]:
    found: list[str] = []
    for match in _PREFER_AIRPORT_IATA.finditer(raw):
        code = match.group(1).upper()
        if is_known_iata(code) and code not in found:
            found.append(code)
    for match in _PREFER_AIRPORT_NAME.finditer(raw):
        code = _AIRPORT_NAME_TO_IATA[match.group(1).casefold()]
        if code not in found:
            found.append(code)
    return found


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

    if _NO_ASIA.search(folded) or "tercermundista" in folded:
        exclude_regions.append("asia")
    if _IST_WORD.search(folded) and (
        "overnight" in folded or "noche" in folded or "never" in folded or "nunca" in folded
    ):
        exclude_airports.append("IST")
        no_overnight.append("IST")
    elif "no overnight" in folded or "not overnight" in folded or "never overnight" in folded:
        no_overnight.append("any")
    for code in _excluded_airports(raw):
        if code not in exclude_airports:
            exclude_airports.append(code)

    around = "around the world" in folded or "vuelta al mundo" in folded
    rest_of_trip = "resto del viaje" in folded or "rest of the trip" in folded
    require_clock = bool(_REQUIRE_CLOCK.search(folded))
    night = bool(_NIGHT_WORD.search(folded))
    require_return = "return legs" in folded or (
        ("packaged" in folded or "empacada" in folded)
        and ("rt" in folded or "ida y vuelta" in folded)
    )

    max_layover: Optional[float] = None
    if "max-layover" in flags:
        max_layover = float(flags["max-layover"])
    else:
        lay = _MAX_LAYOVER_H.search(folded)
        if lay is None:
            lay = _LAYOVER_HORAS.search(folded)
        if lay is None:
            lay = _LAYOVER_DE_MAS.search(folded)
        if lay is None:
            lay = _LAYOVER_OVER.search(folded)
        if lay and (
            "escala" in folded
            or "layover" in folded
            or "connection" in folded
            or "sanas" in folded
            or _SANE_WORD.search(folded)
        ):
            max_layover = float(lay.group(1))
        plus = _NO_PLUS_LAYOVER.search(folded)
        if plus and (max_layover is None or float(plus.group(1)) < max_layover):
            if "layover" in folded or "connection" in folded or "escala" in folded:
                max_layover = float(plus.group(1))

    min_layover: Optional[float] = None
    if "min-layover" in flags:
        min_layover = float(flags["min-layover"])
    else:
        min_hit = _MIN_LAYOVER_H.search(folded)
        if min_hit and ("layover" in folded or "connection" in folded or "escala" in folded):
            min_layover = float(min_hit.group(1))
    max_duration: Optional[float] = None
    if "max-duration" in flags:
        max_duration = float(flags["max-duration"])
    else:
        dur = _MAX_DURATION.search(folded)
        if dur:
            max_duration = float(dur.group(1))

    adults = None
    if "adults" in flags:
        adults = int(flags["adults"])
    else:
        adults = _int_after((_ADULTS_ES, _ADULTS_EN), folded)

    rooms = None
    if "rooms" in flags:
        rooms = int(flags["rooms"])
    else:
        rooms = _int_after((_ROOMS_ES_PLURAL, _ROOMS_ES_SINGULAR, _ROOMS_EN), folded)

    days = None
    if "days" in flags:
        days = int(flags["days"])
    else:
        days_match = _DAYS_ES.search(folded)
        if days_match is None:
            days_match = _DAYS_EN.search(folded)
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
    cap = _PRICE_CAP_EUR_SIGN.search(folded)
    if cap is None:
        cap = _PRICE_CAP_EUR_WORD.search(folded)
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
        for match in _IATA_TOKEN_UPPER.findall(raw):
            if is_known_iata(match):
                origin = match
                break
    if destination is None:
        codes = [match for match in _IATA_TOKEN_UPPER.findall(raw) if is_known_iata(match)]
        if len(codes) >= 2:
            destination = codes[1]
        elif len(codes) == 1 and origin and codes[0] != origin:
            destination = codes[0]

    # "to Fiji from Halifax" already handled; city aliases for dest-only fantasy
    if origin is None and "halifax" in folded:
        origin = "YHZ"
    if destination is None and ("fiji" in folded or "fiyi" in folded or "nadi" in folded):
        destination = "NAN"

    named_airports = _named_iata_codes(raw, folded)
    use_airports = _use_airports(raw)
    if len(pairs) <= 1:
        if origin and origin in exclude_airports:
            for code in (*use_airports, *named_airports):
                if code != destination and code not in exclude_airports:
                    origin = code
                    break
        if destination and destination in exclude_airports:
            for code in (*use_airports, *named_airports):
                if code != origin and code not in exclude_airports:
                    destination = code
                    break
        if pairs and origin and destination:
            pairs = [(origin, destination)]

    destinations = _listed_destinations(raw, origin)

    trip = _trip_kind(folded, flags, len(pairs), date_count=len(dates))
    split_return = bool(_ASKED_TWO_ONE_WAYS.search(folded))
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
            split_return=split_return,
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
    plan_hotels = _is_hotels(folded) and not rest_of_trip
    check_in = dates[0] if plan_hotels and dates else None
    check_out = dates[1] if plan_hotels and len(dates) >= 2 else None

    if departure is not None and departure < today and not plan_hotels:
        refuse.append("past_date")

    baggage = _baggage(folded)
    arrive_before = None
    arrive_hit = _ARRIVE_BEFORE.search(folded)
    if arrive_hit:
        arrive_before = _hhmm(arrive_hit.group(1))
    depart_after = None
    depart_hit = _DEPART_AFTER.search(folded)
    if depart_hit:
        depart_after = _hhmm(depart_hit.group(1))
    work_back_by = _work_back_by(folded)
    prefer_airports: Tuple[str, ...] = tuple(
        dict.fromkeys(
            code
            for code in (origin, destination, *use_airports)
            if code and code in named_airports and code not in exclude_airports
        )
    )

    # Intent
    intent: Intent = "flights"
    extra_refuse: list[str] = []
    booking_match = _BOOKING_PRIMARY.search(raw)
    booking_negated = bool(booking_match and _match_is_negated(raw, booking_match.start()))
    if _NO_TRAINS.search(folded):
        extra_refuse.append("trains")
    if _NO_CARS.search(folded):
        extra_refuse.append("cars")
    if booking_match and (booking_negated or rest_of_trip):
        extra_refuse.append("booking")
    if booking_match and not rest_of_trip and not booking_negated:
        intent = "refuse"
        extra_refuse.append("booking")
    elif (
        _CAR_PRIMARY.search(raw)
        and not _has_flight_words(folded)
        and not plan_hotels
        and not _NO_CARS.search(folded)
    ):
        intent = "refuse"
        extra_refuse.append("cars")
    elif (
        _TRAIN_PRIMARY.search(raw)
        and not _has_flight_words(folded)
        and "airport" not in folded
        and not _is_explore(folded)
        and not _NO_TRAINS.search(folded)
    ):
        intent = "refuse"
        extra_refuse.append("trains")
    elif _is_airports_lookup(folded):
        intent = "airports"
    elif plan_hotels and not _has_flight_words(folded) and not _is_explore(folded) and not pairs:
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
            hotels=True,
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
        rooms=rooms if plan_hotels else None,
        days=days,
        max_layover=max_layover,
        min_layover=min_layover,
        max_duration=max_duration,
        location=location if plan_hotels else None,
        check_in=check_in if plan_hotels else None,
        check_out=check_out if plan_hotels else None,
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
        hotels=plan_hotels,
        baggage=baggage,
        arrive_before=arrive_before,
        depart_after=depart_after,
        prefer_airports=prefer_airports,
        work_back_by=work_back_by,
        route_specs=route_specs,
        notes=notes,
    )
