"""Thin prompt → query plan for the graded bench battery.

This is not a booking flow. It maps a natural-language trip request onto
the owned CLI boundary (route grammar, IATA, trip kind, hotels occupancy)
so deterministic corpus cases can be checked offline.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from typing import Any, Mapping, Optional, Sequence, Tuple, cast
from zoneinfo import ZoneInfo

from viajante.airports import airport_geo, is_known_iata, same_city_iata
from viajante.carriers import (
    ALLIANCE_PHRASES,
    airline_names_longest_first,
    parse_airline_codes,
    parse_alliances,
)
from viajante.flights import (
    FlightPlan,
    expand_nearby_trips,
    parse_flight_plan,
    parse_overnight_airports,
    parse_via_airports,
)
from viajante.models import (
    FETCH_LANGUAGE,
    FlightCabin,
    HotelQuery,
    normalize_country,
    normalize_currency,
)

Intent = str
_PLAN_CABINS = frozenset({"economy", "premium-economy", "business", "first"})


def _fold(text: str) -> str:
    """Casefold and strip Latin combining marks (Hôtel/à → hotel/a). CJK stays."""
    pieces: list[str] = []
    for char in text.replace("\u2014", "-").replace("\u2013", "-"):
        name = unicodedata.name(char, "")
        if name.startswith("LATIN"):
            decomposed = unicodedata.normalize("NFKD", char)
            pieces.append("".join(part for part in decomposed if not unicodedata.combining(part)))
        else:
            pieces.append(char)
    return " ".join("".join(pieces).split()).casefold()


_CITY_IATA_RAW = {
    "madrid": "MAD",
    "barcelona": "BCN",
    "oporto": "OPO",
    "porto": "OPO",
    "lisboa": "LIS",
    "lisbon": "LIS",
    "paris": "CDG",
    "parís": "CDG",
    "parisen": "CDG",
    "roma": "FCO",
    "rome": "FCO",
    "londres": "LHR",
    "london": "LHR",
    "nueva york": "JFK",
    "new york": "JFK",
    "new york city": "JFK",
    "nyc": "JFK",
    "tokio": "NRT",
    "tokyo": "NRT",
    "copenhagen": "CPH",
    "kaupmannahofn": "CPH",
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
    "nulyn": "DUB",
    "ddulyn": "DUB",
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
    "ekapa": "CPT",
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
    "東京": "NRT",
    "パリ": "CDG",
    "ロンドン": "LHR",
}

_CITY_IATA: dict[str, str] = {}
for _alias, _iata in _CITY_IATA_RAW.items():
    _CITY_IATA[_fold(_alias)] = _iata

# English hotel/city labels compiled from aliases already in `_CITY_IATA_RAW`.
# Do not grow one-off inflections; add an alias to that table instead.
_ENGLISH_CITY_LABELS: tuple[str, ...] = (
    "Madrid",
    "Barcelona",
    "Porto",
    "Lisbon",
    "Paris",
    "Rome",
    "London",
    "New York",
    "Tokyo",
    "Halifax",
    "Fiji",
    "Prague",
    "Istanbul",
    "Auckland",
    "Sydney",
    "Beijing",
    "Shanghai",
    "Delhi",
    "Nairobi",
    "Lagos",
    "Johannesburg",
    "Suva",
    "Boston",
    "Chicago",
    "Los Angeles",
    "Miami",
    "Cancun",
    "Dublin",
    "Amsterdam",
    "Frankfurt",
    "Munich",
    "Zurich",
    "Vienna",
    "Budapest",
    "Warsaw",
    "Athens",
    "Dubai",
    "Singapore",
    "Hong Kong",
    "Seoul",
    "Osaka",
    "Melbourne",
    "Palma",
    "Valencia",
    "Seville",
    "Bilbao",
    "Malaga",
    "Milan",
    "Naples",
    "Palermo",
    "Marrakech",
    "Vancouver",
    "Cape Town",
    "Buenos Aires",
    "Sao Paulo",
    "Santiago",
    "Cairo",
    "Mumbai",
    "Bangkok",
    "Jakarta",
    "Manila",
    "Honolulu",
    "Perth",
    "Christchurch",
    "Addis Ababa",
    "Casablanca",
    "Lima",
    "Bogota",
    "Mexico City",
    "Toronto",
    "Doha",
    "Heathrow",
    "Gatwick",
    "Newark",
    "Copenhagen",
)
_ENGLISH_LABEL_BY_FOLD: Mapping[str, str] = {_fold(label): label for label in _ENGLISH_CITY_LABELS}
_IATA_TO_ENGLISH: dict[str, str] = {}
for _alias, _iata in _CITY_IATA.items():
    _label = _ENGLISH_LABEL_BY_FOLD.get(_alias)
    if _label:
        _IATA_TO_ENGLISH.setdefault(_iata, _label)

_WORD_NUMBERS = {
    "un": 1,
    "una": 1,
    "uma": 1,
    "une": 1,
    "um": 1,
    "ein": 1,
    "eine": 1,
    "eitt": 1,
    "one": 1,
    "dau": 2,
    "dos": 2,
    "dois": 2,
    "duas": 2,
    "deux": 2,
    "zwei": 2,
    "due": 2,
    "tveir": 2,
    "two": 2,
    "tres": 3,
    "trois": 3,
    "drei": 3,
    "tre": 3,
    "three": 3,
    "cuatro": 4,
    "quatre": 4,
    "vier": 4,
    "quattro": 4,
    "four": 4,
    "cinco": 5,
    "five": 5,
    "seis": 6,
    "six": 6,
    "siete": 7,
    "seven": 7,
    "ocho": 8,
    "eight": 8,
    "bat": 1,
    "kan": 1,
    "huk": 1,
    "ஒரு": 1,
    "አንድ": 1,
    "ერთი": 1,
    "нэг": 1,
    "elilodwa": 1,
    "واحدة": 1,
    "واحد": 1,
    "bi": 2,
    "meji": 2,
    "இரண்டு": 2,
    "ሁለት": 2,
    "ორი": 2,
    "хоёр": 2,
    "iskay": 2,
    "ababili": 2,
    "شخصان": 2,
    "நான்கு": 4,
}

_OCCUPANCY_NUM = (
    r"(\d+|"
    + "|".join(re.escape(word) for word in sorted(_WORD_NUMBERS, key=len, reverse=True))
    + r")"
)

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
_MONTH_NUM = {
    **_MONTHS,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_IATA_PAIR = re.compile(r"(?<![A-Za-z0-9])([A-Z]{3})-([A-Z]{3})(?![A-Za-z0-9])")
_ISO_DATE = re.compile(r"(?<![0-9])(20\d{2}-\d{2}-\d{2})(?![0-9])")
_FLAG = re.compile(
    r"--(trip|max-stops|adults|children|infants-in-seat|infants-on-lap|cabin|rooms|"
    r"max-layover|min-layover|max-duration|from|days|nights|flex|fetch|sort|"
    r"depart-window|arrive-before|depart-after|currency|country|airlines|"
    r"exclude-airlines|alliance|"
    r"exclude-alliance|exclude-via|exclude-airports|include-airports|via|"
    r"no-overnight|require-overnight|bags|price-cap)\s+(\S+)",
    re.IGNORECASE,
)
_BARE_NEARBY = re.compile(r"--nearby\b", re.IGNORECASE)
_BARE_CARRY_ON = re.compile(r"--carry-on\b", re.IGNORECASE)
_ANY_CITY_AIRPORT = re.compile(
    r"\b(?:any(?:\s+of(?:\s+the)?)?|either)\s+([a-z]+(?:\s+[a-z]+){0,3})\s+airports?\b"
)
_ANY_AIRPORT_IN_CITY = re.compile(r"\bany\s+airports?\s+in\s+([a-z]+(?:\s+[a-z]+){0,3})\b")
_CITY_AREA_AIRPORTS = re.compile(r"\b([a-z]+(?:\s+[a-z]+){0,3})\s+area\s+airports?\b")
_SPANISH_DATE = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-záéíóú]+)\s+(?:de\s+)?(20\d{2})\b",
    re.IGNORECASE,
)
_EN_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
_EN_DMY = re.compile(
    rf"\b(\d{{1,2}})\s+({_EN_MONTH})\.?\s+(20\d{{2}})\b",
    re.IGNORECASE,
)
_EN_MDY = re.compile(
    rf"\b({_EN_MONTH})\.?\s+(\d{{1,2}}),?\s+(20\d{{2}})\b",
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
_ASCII_CITY_NAMES: tuple[str, ...] = tuple(
    name for name in _CITY_NAMES_LONGEST_FIRST if name.isascii()
)
_CJK_CITY_NAMES: tuple[str, ...] = tuple(
    name for name in _CITY_NAMES_LONGEST_FIRST if not name.isascii()
)
_CITY_NAME_RANK: Mapping[str, int] = {
    name: index for index, name in enumerate(_CITY_NAMES_LONGEST_FIRST)
}
_CITY_IATA_COMBINED = re.compile(
    r"\b(?:" + "|".join(re.escape(name) for name in _ASCII_CITY_NAMES) + r")\b"
)
_FROM_TO_PAIR = re.compile(
    r"\b(?:de|desde|from)\s+([a-záéíóúüñ ]+?)\s+(?:a|to|hacia)\s+([a-záéíóúüñ ]+?)"
    r"(?:\s+(?:el|on|del|al|passing|pasando|,)|\s+\d|$)"
)
_TO_FROM_PAIR = re.compile(
    r"\bto\s+([a-záéíóúüñ ]+?)\s+from\s+([a-záéíóúüñ ]+?)"
    r"(?:\s+(?:passing|pasando|on|el|,)|\s+\d|$)"
)
_HOTEL_LOCATION = re.compile(
    r"\b(?:hotels?|hoteles|hoteli|hoteeli|alojamiento|unterkunft|hospedagem|"
    r"gistihus[a-z]*|ostatua|gwesty|accommodation|lodging|tambo|stay|stays)\s+"
    r"(?:en|in|at|a|à|em|ni|i|yn|von|vom|de|du|في)?\s*"
    r"([A-Za-z\u3040-\u30ff\u4e00-\u9fff ]+)",
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
_IATA_TOKEN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{3})(?![A-Za-z0-9])")
_IATA_TOKEN_UPPER = re.compile(r"(?<![A-Za-z0-9])([A-Z]{3})(?![A-Za-z0-9])")
_AIRPORTS_LOOKUP = re.compile(
    r"\b(iata code|código iata|codigo iata|qué aeropuerto|que aeropuerto)\b"
)
_EXPLORE_WORDS = re.compile(
    r"\b(explora|explore|destinos|destinations|dónde ir|donde ir|"
    r"busca destinos|to everywhere|everywhere from|everywhere under|"
    r"where is cheap|cheap destinations)\b"
)
_DATES_CALENDAR = re.compile(
    r"\b(calendario|date grid|cheapest friday|"
    r"más barato por día|mas barato por dia|qué día es más barato|"
    r"que dia es mas barato|cheapest days?|cheapest dates|"
    r"cheapest week|flexible dates|price calendar|flexible calendar)\b"
    r"|(?<!same\s)\bcalendar\b(?!\s+date)"
)
_DATES_COMMAND = re.compile(r"(?:^|\bviajante\s+)?dates\s+[a-z]{3}-[a-z]{3}\b")
_FREE_CANCELLATION = re.compile(
    r"\bfree cancellation\b|\bcancelaci[oó]n gratuita\b",
    re.IGNORECASE,
)
_NONREFUNDABLE = re.compile(
    r"\bnon-?refundable\b|\bno reembolsable\b|\bprepaid non-?refundable\b",
    re.IGNORECASE,
)
_HOTEL_WORDS = re.compile(
    r"(?<![a-z0-9_])(?:hotels?|hoteles|hoteli|hoteeli|alojamiento|unterkunft|"
    r"hospedagem|gistihus[a-z]*|ostatua|gwesty|accommodation|lodging|tambo)"
    r"(?![a-z0-9_])"
    r"|\bstays?\s+(?:in|at)\b"
    r"|宿泊|숙소|ホテル|호텔|فندق|إقامة|სასტუმრო|ሆቴል|សណ្ឋាគារ|"
    r"зочид\s*буудал|தங்குமிட|indawo\s+yokulala"
)
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
# Quote N secret/unnamed dests the prompt never named. Do not invent IATA.
_UNNAMED_DEST_QUOTE = re.compile(
    r"\b(?:secret|unnamed)\s+destinations?\b|"
    r"\bdestinations?\b.{0,48}?\b(?:i|we|you)\s+did(?:\s+not|'t)\s+name\b"
)
_UNNAMED_DEST_COUNT = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight)\s+"
    r"(?:cheapest\s+)?(?:secret|unnamed)\s+destinations?\b"
)
_ENGLISH_COUNT_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
}
_FIXED_DATES_FIRST = re.compile(r"fixed(?: natural)? dates first")
_PLUS_MINUS_1 = re.compile(r"(?:±|\+/-)\s*1")
_PLUS_MINUS_N = re.compile(r"(?:±|\+/-|plus\s+(?:or\s+)?minus)\s*(\d+)")
_AROUND_WORD = re.compile(r"\baround\b")
_FLEXIBLE_WORD = re.compile(r"\bflexible\b")
_DEFAULT_FLEX_DAYS = 3
_MAX_2_STOPS = re.compile(r"max(?:imum)?\s+2\s+stops|m[aá]ximo 2 escalas|max 2 stops")
_MAX_1_STOP = re.compile(
    r"max(?:imum)?\s+1\s+stop|m[aá]ximo 1 escala|at most (?:one|1) stop|"
    r"მაქსიმუმ\s+1\s+გადაჯდომა"
)
_SIN_ESCALAS_MAS = re.compile(r"sin escalas de m[aá]s")
_ESCALAS_SANAS = re.compile(r"escalas sanas")
_NONSTOP = re.compile(r"\b(nonstop|directos?|sin escalas)\b")
_COUNT = _OCCUPANCY_NUM
_ADULT_STEMS = (
    r"adults?|adultos?|adultes?|adulti|erwachsene[nrs]?|"
    r"fullor[dð]nir|wazima|heldu|oedolyn|oedolion|"
    r"agbalagba|abadala|hatun\s+runakuna|பெரியவர்கள்|"
    r"ጎልማሶች|ზრდასრულ(?:ებ)?ი|том\s+хүн|بالغان"
)
_ADULTS = re.compile(_COUNT + r"\s+(?:" + _ADULT_STEMS + r")(?![A-Za-z0-9_])")
_ADULTS_REVERSE = re.compile(r"(?:" + _ADULT_STEMS + r")\s+" + _COUNT)
_SAME_ADULT = re.compile(r"\bsame\s+adult\b")
_ADULTS_CJK = re.compile(r"(?:大人|성인)\s*(\d+)")
_CHILDREN = re.compile(
    _COUNT + r"\s+(?:children|child|kids|kid|ninos?|hijos?|criancas?|enfants?|"
    r"kinder|kind(?! of)|bambin[oi]|filhos?)\b"
)
_INFANTS_IN_SEAT_EN = re.compile(_COUNT + r"\s+infants?\s+in[- ]seats?")
_INFANTS_ON_LAP_EN = re.compile(_COUNT + r"\s+infants?\s+on[- ]laps?")
_INFANTS_EN = re.compile(_COUNT + r"\s+infants?\b(?!\s+in[- ]seats?)(?!\s+on[- ]laps?)")
_ROOMS_ES_PLURAL = re.compile(r"(\d+|una|un|one|dos|two)\s+habitaciones")
_ROOMS_ES_SINGULAR = re.compile(r"(\d+|una|un|one)\s+habitaci[oó]n")
_ROOM_STEMS = (
    r"rooms?|chambres?|zimmer|quartos?|herbergi|ystafell|chumba|gela|"
    r"habitaciones?|cuarto|ikamelo|yara|அறைகள்|அறை|ክፍል|ოთახი|өрөө|غرفة"
)
_ROOMS_EN = re.compile(_COUNT + r"\s+rooms?")
_ROOMS_I18N = re.compile(_COUNT + r"\s+(?:" + _ROOM_STEMS + r")(?![A-Za-z0-9_])")
_ROOMS_REVERSE = re.compile(r"(?:" + _ROOM_STEMS + r")\s+" + _COUNT)
_ROOMS_CJK = re.compile(r"(?:部屋|객실|방)\s*(\d+)")
_DAYS_ES = re.compile(r"(\d+)\s*d[ií]as")
_DAYS_EN = re.compile(r"(\d+)\s+days")
_NIGHTS = re.compile(r"(\d+)\s*-?\s*(?:nights?|noches?)\b")
_FLEX_DAYS = re.compile(r"\bflex\s+(\d+)(?:\s+days?)?\b")
_NO_ASIA = re.compile(r"\b(no asia|not asia)\b")
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
    r"\b(?:returning|return(?:ing)?(?:\s+on)?|ida y vuelta|ida e volta|"
    r"aller-retour|retour|vuelta|volta|"
    r"ruckflug|hin-?\s*und\s*ruckflug|"
    r"kurudi|itzuli|dychwelyd|kutimuy|pada\s+wa|ngibuye|til\s+baka|"
    r"back\s+(?:on|by|before|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday))\b"
    r"|往復|戻り|왕복|귀국|буцах|დავბრუნდე|ተመልሼ|ត្រឡប់|திரும்பு|"
    r"ذهاب\s*وعودة|العودة",
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
_VS_HUB = re.compile(r"\b(?:vs\.?|versus|compared\s+(?:to|with))\b", re.IGNORECASE)
_BAG_NOUN = r"(?:bags?|luggage|baggage)"
_CARRY_ON_ONLY = re.compile(
    r"\b(?:"
    + "|".join(
        (
            r"carry[- ]on only",
            r"hand luggage only",
            r"cabin bag only",
            r"solo equipaje de mano",
            r"nur handgepack",
            r"bagage cabine uniquement",
            r"uniquement bagage cabine",
            r"solo bagaglio a mano",
            r"so bagagem de mao",
        )
    )
    + r")\b",
    re.IGNORECASE,
)
_NO_CHECKED = re.compile(
    r"\b(?:"
    + "|".join(
        (
            r"no checked(?:\s+" + _BAG_NOUN + r")?",
            r"no hold(?:\s+" + _BAG_NOUN + r")",
            r"no bags",
            r"sin (?:equipaje|maletas?) facturad[oa]s?",
            r"ohne aufgegebenes gepack",
            r"sans bagage(?:s)? en soute",
            r"sem bagagem despachada",
            r"senza bagaglio stiva",
        )
    )
    + r")\b",
    re.IGNORECASE,
)
_CHECKED_N = re.compile(
    _COUNT + r"\s+checked(?:\s+" + _BAG_NOUN + r")?\b",
    re.IGNORECASE,
)
_ARRIVE_BEFORE = re.compile(
    r"\b(?:arrive|land|be there)\s+(?:[a-z]{3}\s+)?(?:before|by)\s+"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_DEPART_AFTER = re.compile(
    r"\b(?:(?:depart(?:ing|ure)?|leave)\s+(?:20\d{2}-\d{2}-\d{2}\s+)?|"
    r"(?:on\s+)?20\d{2}-\d{2}-\d{2}\s+)"
    r"after\s+"
    r"(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+)?"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.IGNORECASE,
)
_CLOCK_TOKEN = r"(\d{1,2}(?::\d{2})?h?\s*(?:am|pm)?)"
_DEPART_WINDOW_VERBS = r"(?:leave|depart(?:ure|ing)?s?|salir|salida|partir|abflug)"
_DEPART_WINDOW_BETWEEN = re.compile(
    rf"\b{_DEPART_WINDOW_VERBS}\s+(?:between|entre|zwischen)\s+"
    + _CLOCK_TOKEN
    + r"\s+(?:and|y|et|und|e)\s+"
    + _CLOCK_TOKEN,
    re.IGNORECASE,
)
_DEPART_WINDOW_BETWEEN_DASH = re.compile(
    rf"\b{_DEPART_WINDOW_VERBS}\s+(?:between|entre|zwischen)\s+"
    r"(\d{1,2}(?::\d{2})?)\s*-\s*(\d{1,2}(?::\d{2})?)(?!:)(?!-\d)",
    re.IGNORECASE,
)
_DEPART_WINDOW_DASH = re.compile(
    rf"\b{_DEPART_WINDOW_VERBS}\s+(\d{{1,2}}(?::\d{{2}})?)\s*-\s*(\d{{1,2}}(?::\d{{2}})?)"
    r"(?!:)(?!-\d)",
    re.IGNORECASE,
)
_SORT_BY = re.compile(
    r"\b(?:sort(?:ed)?|order)\s+by\s+"
    r"(price|fare|duration|(?:departure|arrival)(?:\s+time)?|ranked)\b",
    re.IGNORECASE,
)
_SORT_BARE = re.compile(
    r"\b(?:sort(?:ed)?|order)\s+(price|fare|duration|departure|arrival|ranked)\b",
    re.IGNORECASE,
)
_SORT_CHEAPEST_FIRST = re.compile(r"\bcheapest\s+first\b", re.IGNORECASE)
_SORT_SHORTEST_FIRST = re.compile(
    r"\bshortest(?:\s+(?:flight|duration|travel(?:\s+time)?))?\s+first\b",
    re.IGNORECASE,
)
_SORT_EARLIEST_FIRST = re.compile(r"\bearliest(?:\s+departure)?\s+first\b", re.IGNORECASE)
_SORT_FASTEST = re.compile(r"\bfastest\b", re.IGNORECASE)
_SORT_EARLIEST = re.compile(r"\bearliest\b", re.IGNORECASE)
_SORT_I18N = re.compile(
    r"\b(?:ordenar\s+por|trier\s+par|nach)\s+"
    r"(precio|tarifa|prix|preis|duracion|duree|dauer|salida|depart|abflug|"
    r"llegada|arrivee|ankunft)\b",
    re.IGNORECASE,
)
_SORT_I18N_ALIASES = {
    "precio": "price",
    "tarifa": "fare",
    "prix": "price",
    "preis": "price",
    "duracion": "duration",
    "duree": "duration",
    "dauer": "duration",
    "salida": "departure",
    "depart": "departure",
    "abflug": "departure",
    "llegada": "arrival",
    "arrivee": "arrival",
    "ankunft": "arrival",
}
_JA_NOT_IATA = re.compile(r"([A-Z]{3})は使わない")
# Compile already-covered weekday aliases. Do not grow a per-language regex world.
_WEEKDAY_ALIASES = {
    "monday": "monday",
    "tuesday": "tuesday",
    "wednesday": "wednesday",
    "thursday": "thursday",
    "friday": "friday",
    "saturday": "saturday",
    "sunday": "sunday",
    "montag": "monday",
    "jumatatu": "monday",
    "umsombuluko": "monday",
}
_WEEKDAY_ALT = "|".join(
    sorted((re.escape(name) for name in _WEEKDAY_ALIASES), key=len, reverse=True)
)
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WORK_BACK_CONTEXT = r"(?:must work|work|back(?:\s+in)?|in the office|muss|kazi|ngisebenze)"
_WORK_BACK_BY = re.compile(
    rf"\b{_WORK_BACK_CONTEXT}\s+({_WEEKDAY_ALT})"
    rf"(?:\s+(?:before|by|at|um|saa|ngo))?"
    rf"\s*-?\s*{_CLOCK_TOKEN}",
    re.IGNORECASE,
)
_CIVIL_DEPART_HOUR = 6
_CRUISE_KMH = 800.0
_BLOCK_PAD_HOURS = 1.0
_EARTH_RADIUS_KM = 6371.0
_MIN_LAYOVER_H = re.compile(
    r"(?:at least|min(?:imum)?|no less than|≥|>=|მინიმუმ)\s*(\d+(?:\.\d+)?)\s*"
    r"(?:h\b|hours?\b|horas\b|საათ)",
    re.IGNORECASE,
)
_VIA_AIRPORT_IATA = re.compile(
    r"\b(?:via|through|connecting?\s+(?:in|via|at)|"
    r"transfer(?:ring)?\s+(?:in|at)|stop(?:over|ping)?\s+(?:in|at)|"
    r"layover\s+(?:in|at))\s+"
    r"([A-Z]{3})\b",
    re.IGNORECASE,
)
_VIA_PLACE_LEAD = re.compile(
    r"\b(?:via|through|connecting?\s+(?:in|via|at)|"
    r"transfer(?:ring)?\s+(?:in|at)|stop(?:over|ping)?\s+(?:in|at)|"
    r"layover\s+(?:in|at))\s+",
    re.IGNORECASE,
)
_EXCLUDE_VIA_IATA = re.compile(
    r"\b(?:not|never|no|avoid|without|don'?t|rather than|instead of)\s+"
    r"(?:(?:a|any)\s+)?"
    r"(?:via|through|connecting?\s+(?:in|via|at)|"
    r"transfer(?:ring)?\s+(?:in|at)|stop(?:over|ping)?\s+(?:in|at)|"
    r"layover\s+(?:in|at))\s+"
    r"([A-Z]{3})\b",
    re.IGNORECASE,
)
_KA_VIA_IATA = re.compile(r"გადავჯდ[^\s,]*\s+([A-Z]{3})")
_REQUIRE_OVERNIGHT_IATA = re.compile(
    r"\b(?:must|need(?:s)? to|required to)\s+overnight\s+(?:in|at)\s+([A-Z]{3})\b",
    re.IGNORECASE,
)
_NO_OVERNIGHT_IATA = re.compile(
    r"\b(?:never|no|not|don'?t|nunca)\s+overnight\s+(?:in|at|en)\s+([A-Z]{3})\b",
    re.IGNORECASE,
)
_KA_NO_OVERNIGHT_IATA = re.compile(r"([A-Z]{3})-ში\s+ღამის\s+გათევა\s+არასდროს")
_KA_MAX_STOPS = re.compile(r"მაქსიმუმ\s+([012])\s+გადაჯდომა")
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
    children: Optional[int] = None
    infants_in_seat: Optional[int] = None
    infants_on_lap: Optional[int] = None
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
    include_airports: Tuple[str, ...] = ()
    via_regions: Tuple[str, ...] = ()
    via_airports: Tuple[str, ...] = ()
    exclude_via: Tuple[str, ...] = ()
    no_overnight: Tuple[str, ...] = ()
    require_overnight: Tuple[str, ...] = ()
    refuse: Tuple[str, ...] = ()
    hotels: bool = False
    baggage: Optional[str] = None
    bags: Optional[int] = None
    carry_on: Optional[int] = None
    include_airlines: Tuple[str, ...] = ()
    exclude_airlines: Tuple[str, ...] = ()
    alliance: Tuple[str, ...] = ()
    exclude_alliance: Tuple[str, ...] = ()
    arrive_before: Optional[str] = None
    depart_after: Optional[str] = None
    depart_window: Optional[str] = None
    sort: Optional[str] = None
    prefer_airports: Tuple[str, ...] = ()
    work_back_by: Optional[str] = None
    route_specs: Tuple[str, ...] = ()
    nearby: bool = False
    search_trip: bool = False
    notes: str = ""
    locale: str = FETCH_LANGUAGE
    flex_days: Optional[int] = None
    currency: Optional[str] = None
    country: Optional[str] = None

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
            "children": self.children,
            "infants_in_seat": self.infants_in_seat,
            "infants_on_lap": self.infants_on_lap,
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
            "include_airports": list(self.include_airports),
            "via_regions": list(self.via_regions),
            "via_airports": list(self.via_airports),
            "exclude_via": list(self.exclude_via),
            "no_overnight": list(self.no_overnight),
            "require_overnight": list(self.require_overnight),
            "refuse": list(self.refuse),
            "hotels": self.hotels,
            "baggage": self.baggage,
            "bags": self.bags,
            "carry_on": self.carry_on,
            "include_airlines": list(self.include_airlines),
            "exclude_airlines": list(self.exclude_airlines),
            "alliance": list(self.alliance),
            "exclude_alliance": list(self.exclude_alliance),
            "arrive_before": self.arrive_before,
            "depart_after": self.depart_after,
            "depart_window": self.depart_window,
            "sort": self.sort,
            "prefer_airports": list(self.prefer_airports),
            "work_back_by": self.work_back_by,
            "route_specs": list(self.route_specs),
            "nearby": self.nearby,
            "search_trip": self.search_trip,
            "notes": self.notes,
            "locale": self.locale,
            "flex_days": self.flex_days,
            "currency": self.currency,
            "country": self.country,
        }

    def matches(self, expect: Mapping[str, Any]) -> tuple[bool, str]:
        data = self.to_dict()
        subset_keys = {
            "refuse",
            "exclude_regions",
            "exclude_airports",
            "include_airports",
            "no_overnight",
            "require_overnight",
            "prefer_airports",
        }
        exact_list_keys = {
            "via_regions",
            "via_airports",
            "exclude_via",
            "route_specs",
            "destinations",
            "include_airlines",
            "exclude_airlines",
            "alliance",
            "exclude_alliance",
        }
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


def _named_currency(flags: Mapping[str, str]) -> Optional[str]:
    raw = flags.get("currency")
    if not raw:
        return None
    try:
        return normalize_currency(raw)
    except ValueError:
        return None


def _named_country(flags: Mapping[str, str]) -> Optional[str]:
    raw = flags.get("country")
    if not raw:
        return None
    try:
        return normalize_country(raw)
    except ValueError:
        return None


def _nearby_city_name(folded: str) -> Optional[str]:
    for pattern in (_ANY_CITY_AIRPORT, _ANY_AIRPORT_IN_CITY, _CITY_AREA_AIRPORTS):
        hit = pattern.search(folded)
        if hit is None:
            continue
        city = " ".join(hit.group(1).split())
        if _resolve_city_iata(city):
            return city
    return None


def _has_bare_flag(raw: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(raw):
        prefix = raw[max(0, match.start() - 9) : match.start()].casefold()
        if prefix.endswith("sin ") or prefix.endswith("without "):
            continue
        return True
    return False


def _wants_nearby(raw: str, folded: str) -> bool:
    if _has_bare_flag(raw, _BARE_NEARBY):
        return True
    return _nearby_city_name(folded) is not None


def _iso_dates(text: str) -> list[date]:
    dates: list[date] = []
    for raw in _ISO_DATE.findall(text):
        dates.append(date.fromisoformat(raw))
    for match in _SPANISH_DATE.finditer(text):
        month = _MONTHS.get(match.group(2).casefold())
        if month is None:
            continue
        dates.append(date(int(match.group(3)), month, int(match.group(1))))
    for match in _EN_DMY.finditer(text):
        month = _MONTH_NUM.get(match.group(2).casefold())
        if month is None:
            continue
        dates.append(date(int(match.group(3)), month, int(match.group(1))))
    for match in _EN_MDY.finditer(text):
        month = _MONTH_NUM.get(match.group(1).casefold())
        if month is None:
            continue
        dates.append(date(int(match.group(3)), month, int(match.group(2))))
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
    found: list[tuple[int, str, str]] = []
    for match in _CITY_IATA_COMBINED.finditer(folded):
        name = match.group(0)
        found.append((match.start(), name, _CITY_IATA[name]))
    for name in _CJK_CITY_NAMES:
        start = 0
        while True:
            index = folded.find(name, start)
            if index < 0:
                break
            found.append((index, name, _CITY_IATA[name]))
            start = index + len(name)
    found.sort(key=lambda item: item[0])
    return [(name, iata) for _, name, iata in found]


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


_ANY_CITY_PREFIX = re.compile(r"^any\s+(?:of\s+(?:the\s+)?)?")
_AIRPORT_SUFFIX = re.compile(r"\s+airports?$")


def _resolve_city_iata(name: str) -> Optional[str]:
    cleaned = " ".join(name.split())
    cleaned = _ANY_CITY_PREFIX.sub("", cleaned)
    cleaned = _AIRPORT_SUFFIX.sub("", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
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


def _english_place_name(raw: str) -> Optional[str]:
    """Map a user-language city token onto the English fetch query string."""
    known = _known_english_city(raw)
    if known is not None:
        return known
    cleaned = _fold(raw).strip(" ,.")
    return cleaned or None


def _known_english_city(raw: str) -> Optional[str]:
    """English city name only when the token is in the owned city table."""
    folded = _fold(raw)
    iata = _CITY_IATA.get(folded) or _lookup_alias(folded)
    if iata is None:
        hits = _iter_city_iata(folded)
        if hits:
            iata = hits[0][1]
    if iata is None:
        return None
    return _IATA_TO_ENGLISH.get(iata)


def _first_english_city(folded: str) -> Optional[str]:
    hits = _iter_city_iata(folded)
    if not hits:
        return None
    return _IATA_TO_ENGLISH.get(hits[0][1])


def _hotel_location(text: str) -> Optional[str]:
    folded = _fold(text)
    match = _HOTEL_LOCATION.search(folded)
    if match is None:
        return None
    english = _known_english_city(match.group(1))
    if english:
        return english
    raw = _HOTEL_LOCATION_SPLIT.split(match.group(1), maxsplit=1)[0].strip(" ,.")
    return _known_english_city(raw) if raw else None


def _int_token(token: str) -> Optional[int]:
    if token.isdigit():
        return int(token)
    return _WORD_NUMBERS.get(token)


def _int_after(patterns: Sequence[re.Pattern[str]], folded: str) -> Optional[int]:
    for pattern in patterns:
        match = pattern.search(folded)
        if match is None:
            continue
        value = _int_token(match.group(1))
        if value is not None:
            return value
    return None


def _int_all(patterns: Sequence[re.Pattern[str]], folded: str) -> list[int]:
    """Every occupancy number those patterns hit, in prompt order. No invented counts."""
    hits: list[tuple[int, int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(folded):
            value = _int_token(match.group(1))
            if value is None:
                continue
            hits.append((match.start(), match.end(), value))
    hits.sort()
    values: list[int] = []
    last_end = -1
    for start, end, value in hits:
        if start < last_end:
            continue
        values.append(value)
        last_end = end
    return values


def _append_note(notes: str, extra: str) -> str:
    extra = extra.strip()
    if not extra:
        return notes
    return f"{notes} {extra}".strip() if notes else extra


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
        token = match.group(1).strip(" ?.")
        english = _english_place_name(token)
        return (english or token).casefold()
    for pattern, name in _AIRPORTS_QUERY_CITIES:
        if pattern.search(folded):
            return name
    iata = _IATA_TOKEN.search(text)
    if iata and is_known_iata(iata.group(1)):
        return iata.group(1).upper()
    english = _first_english_city(folded)
    if english:
        return english.casefold()
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
    return bool(_DATES_CALENDAR.search(folded) or _DATES_COMMAND.search(folded))


def _flex_days_value(
    folded: str,
    flags: Mapping[str, str],
    dates: Sequence[date] = (),
) -> Optional[int]:
    if "flex" in flags:
        try:
            value = int(flags["flex"])
        except ValueError:
            return None
        return value if value >= 1 else None
    match = _FLEX_DAYS.search(folded)
    if match is not None:
        value = int(match.group(1))
        return value if value >= 1 else None
    plus = _PLUS_MINUS_N.search(folded)
    if plus is not None:
        value = int(plus.group(1))
        return value if value >= 1 else None
    if "around the world" in folded or "vuelta al mundo" in folded:
        return None
    if _AROUND_WORD.search(folded):
        return _DEFAULT_FLEX_DAYS
    if _FLEXIBLE_WORD.search(folded) and len(dates) == 1 and _MONTH_YEAR.search(folded) is None:
        return _DEFAULT_FLEX_DAYS
    return None


def _is_flex_window(folded: str, flags: Mapping[str, str], dates: Sequence[date]) -> bool:
    if not dates:
        return False
    if "around the world" in folded or "vuelta al mundo" in folded:
        return False
    return _flex_days_value(folded, flags, dates) is not None


def _is_hotels(folded: str) -> bool:
    return bool(_HOTEL_WORDS.search(folded))


def _has_flight_words(folded: str) -> bool:
    return bool(_FLIGHT_WORDS.search(folded))


def _named_cabins(folded: str) -> Tuple[str, ...]:
    """Cabins the prompt named. Premium-economy is not also economy."""
    named: list[str] = []
    if "premium-economy" in folded or "premium economy" in folded:
        named.append("premium-economy")
    if _BUSINESS_CABIN.search(folded):
        named.append("business")
    if _FIRST_CABIN.search(folded):
        named.append("first")
    economy = _ECONOMY_CABIN.search(folded)
    if economy is not None:
        prefix = folded[max(0, economy.start() - 16) : economy.start()]
        if "premium-" not in prefix and "premium " not in prefix:
            named.append("economy")
    return tuple(named)


def _cabin(folded: str, flags: Mapping[str, str]) -> Optional[str]:
    if "cabin" in flags:
        value = flags["cabin"].casefold()
        if value in {"economy", "premium-economy", "business", "first"}:
            return value
    named = _named_cabins(folded)
    if len(named) == 1:
        return named[0]
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
    ka_stops = _KA_MAX_STOPS.search(folded)
    if ka_stops:
        return int(ka_stops.group(1))
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
    if (
        "round-trip" in folded
        or "round trip" in folded
        or "aller-retour" in folded
        or "ida e volta" in folded
        or "ruckflug" in folded
        or "往復" in folded
        or "왕복" in folded
    ):
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


def _around_the_world_notes(
    *,
    max_stops: Optional[int],
    exclude_regions: Sequence[str] = (),
    max_layover: Optional[float] = None,
) -> str:
    """Honesty line for an unnamed circumnavigation. No invented cities or fares."""
    bits = ["Around-the-world shortlist", "circuit closes at the named origin"]
    if exclude_regions:
        bits.append("without " + ", ".join(exclude_regions))
    if max_layover is not None:
        bits.append(f"layover cap {max_layover:g}h")
    if max_stops is not None:
        bits.append(f"max_stops {max_stops} on every leg")
    bits.append("do not invent fares or hops")
    return "; ".join(bits)


def _via_regions_notes() -> str:
    """Honesty line for a named via-continents hop list. No invented codes or fares."""
    return (
        "Named via_regions is a shortlist constraint; "
        "do not invent airport codes or a fare per hop; "
        "keep the named regions and origin/dest/date."
    )


def _origin_in_excluded_regions(
    origin: Optional[str],
    exclude_regions: Sequence[str],
) -> Tuple[str, ...]:
    """Owned IANA tz prefix vs excluded region. Unknown tz cannot prove inside."""
    if origin is None or not exclude_regions:
        return ()
    geo = airport_geo(origin)
    if geo is None:
        return ()
    tz = geo[0].casefold()
    return tuple(region for region in exclude_regions if tz.startswith(f"{region.casefold()}/"))


def _origin_inside_excluded_region_notes(origin: str, regions: Sequence[str]) -> str:
    """Honesty line: named origin sits in an excluded region. No invented dests or fares."""
    dropped = ", ".join(regions)
    return (
        f"{origin} sits inside excluded {dropped}; priced shortlist may be empty. "
        f"Do not invent dests, IATA, or fares outside {dropped}."
    )


def _weekday_nofly(folded: str) -> bool:
    """English weekday-no-fly / weekend-only clock constraint. Do not grow language regexes."""
    if "weekday no-fly" in folded or "weekday nofly" in folded:
        return True
    if "no-fly weekday" in folded or "nofly weekday" in folded:
        return True
    if "no weekday fly" in folded or "no weekday flying" in folded:
        return True
    if "no midweek fly" in folded or "no midweek flying" in folded:
        return True
    if "weekend only" in folded or "only weekend" in folded:
        return True
    if "weekends only" in folded or "only weekends" in folded:
        return True
    return False


def _weekend_clocks(
    weekday: Optional[str],
    depart_after: Optional[str],
    work_back_by: Optional[str],
) -> bool:
    """True when Friday depart_after and Monday work_back_by are already owned clocks."""
    if weekday != "friday" or not depart_after or not work_back_by:
        return False
    return work_back_by.startswith("monday ")


def _weekday_nofly_notes() -> str:
    """Honesty line: weekday no-fly is an owned clock constraint. No invented hops or fares."""
    return (
        "Weekday no-fly is an owned clock constraint; "
        "keep Friday depart_after and Monday work_back_by; "
        "do not invent a midweek hop or drop a clock; "
        "do not invent a fare."
    )


def _is_unnamed_dest_quote(folded: str, listed: Sequence[str]) -> bool:
    """True when the prompt asks to quote dests it never named. Named lists stay."""
    if listed:
        return False
    return bool(_UNNAMED_DEST_QUOTE.search(folded))


def _unnamed_dest_quote_count(folded: str) -> Optional[int]:
    match = _UNNAMED_DEST_COUNT.search(folded)
    if match is None:
        return None
    return _int_token(match.group(1))


def _unnamed_dest_quote_notes(count: Optional[int]) -> str:
    """Honesty line: unnamed dests cannot be quoted. No invented cities or fares."""
    if count is None:
        lead = "Unnamed dests cannot be quoted"
    else:
        word = _ENGLISH_COUNT_WORDS.get(count, str(count))
        noun = "dest" if count == 1 else "dests"
        lead = f"{word.capitalize()} unnamed {noun} cannot be quoted"
    return f"{lead}. Do not invent a shortlist, IATA, or hotels. Explore without fake dests."


def _same_city_named_pairs(named: Sequence[str]) -> Tuple[Tuple[str, str], ...]:
    """Owned same-city peers in prompt order. No invented codes."""
    codes = [code for code in dict.fromkeys(named) if code]
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, code in enumerate(codes):
        peers = set(same_city_iata(code))
        if len(peers) < 2:
            continue
        for other in codes[index + 1 :]:
            if other not in peers:
                continue
            key = (code, other) if code < other else (other, code)
            if key in seen:
                continue
            seen.add(key)
            pairs.append((code, other))
    return tuple(pairs)


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
    match = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?h?\s*(am|pm)?\s*", raw, re.IGNORECASE)
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


def _named_hhmm(
    folded: str,
    flags: Mapping[str, str],
    *,
    flag: str,
    pattern: re.Pattern[str],
) -> Optional[str]:
    """Named HH:MM only. Flags win; vibe wording does not invent a clock."""
    flagged = flags.get(flag)
    if flagged:
        return _hhmm(flagged)
    hit = pattern.search(folded)
    if hit is None:
        return None
    return _hhmm(hit.group(1))


def _window_side(raw: str) -> Optional[tuple[str, bool]]:
    """Canonical bound plus whether the prompt used a bare hour (CLI 6-20)."""
    text = raw.strip()
    clock = _hhmm(text)
    if clock is None:
        return None
    hour_only = bool(re.fullmatch(r"\d{1,2}h?", text, re.IGNORECASE))
    if hour_only:
        return str(int(clock[:2])), True
    return clock, False


_SORT_ALIASES = {
    "price": "price",
    "fare": "fare",
    "duration": "duration",
    "departure": "departure",
    "arrival": "arrival",
    "ranked": "ranked",
}


def _depart_window(folded: str, flags: Mapping[str, str]) -> Optional[str]:
    flagged = flags.get("depart-window")
    if flagged:
        return flagged
    hit = (
        _DEPART_WINDOW_BETWEEN.search(folded)
        or _DEPART_WINDOW_BETWEEN_DASH.search(folded)
        or _DEPART_WINDOW_DASH.search(folded)
    )
    if hit is None:
        return None
    start = _window_side(hit.group(1))
    end = _window_side(hit.group(2))
    if start is None or end is None:
        return None
    start_clock = _hhmm(hit.group(1))
    end_clock = _hhmm(hit.group(2))
    if start_clock is None or end_clock is None or start_clock > end_clock:
        return None
    if start[1] and end[1]:
        return f"{start[0]}-{end[0]}"
    return f"{start_clock}-{end_clock}"


def _sort_key(folded: str, flags: Mapping[str, str]) -> Optional[str]:
    flagged = flags.get("sort")
    if flagged:
        return _SORT_ALIASES.get(flagged.strip().casefold())
    hit = _SORT_BY.search(folded) or _SORT_BARE.search(folded)
    if hit is not None:
        token = hit.group(1).casefold().split()[0]
        return _SORT_ALIASES.get(token)
    if _SORT_CHEAPEST_FIRST.search(folded):
        return "price"
    if _SORT_SHORTEST_FIRST.search(folded) or _SORT_FASTEST.search(folded):
        return "duration"
    if _SORT_EARLIEST_FIRST.search(folded) or _SORT_EARLIEST.search(folded):
        return "departure"
    i18n = _SORT_I18N.search(folded)
    if i18n is not None:
        return _SORT_I18N_ALIASES.get(i18n.group(1).casefold())
    return None


def _match_is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 24) : start].casefold()
    return bool(_NEGATION_BEFORE.search(prefix))


def _baggage_label(
    *,
    carry_only: bool,
    no_checked: bool,
    bags: Optional[int],
) -> Optional[str]:
    if carry_only:
        return "carry_on_only"
    if no_checked:
        return "no_checked"
    if bags == 1:
        return "checked_1"
    return None


def _bag_fields(
    folded: str,
    flags: Mapping[str, str],
    raw: str,
) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """Map named bag wording onto --bags N / --carry-on. Leave unset when unnamed."""
    bags: Optional[int] = None
    if "bags" in flags:
        try:
            value = int(flags["bags"])
        except ValueError:
            value = -1
        if value >= 0:
            bags = value
    carry_on = 1 if _has_bare_flag(raw, _BARE_CARRY_ON) else None
    carry_only = bool(_CARRY_ON_ONLY.search(folded))
    no_checked = bool(_NO_CHECKED.search(folded))
    checked_n = _int_after((_CHECKED_N,), folded)
    if carry_only and checked_n is not None and checked_n >= 1:
        # Carry-on only and N checked cannot both hold. Do not pick one.
        return None, None, None
    if bags is None and no_checked:
        bags = 0
    if bags is None and checked_n is not None:
        bags = checked_n
    if carry_on is None and carry_only:
        carry_on = 1
    return _baggage_label(carry_only=carry_only, no_checked=no_checked, bags=bags), bags, carry_on


def _named_price_cap_eur(folded: str, flags: Mapping[str, str]) -> Optional[int]:
    """Named cap only. Unnamed stays None. Do not invent a fare or a cap."""
    if "price-cap" in flags:
        try:
            value = int(flags["price-cap"])
        except ValueError:
            return None
        return value if value > 0 else None
    cap = _PRICE_CAP_EUR_SIGN.search(folded)
    if cap is None:
        cap = _PRICE_CAP_EUR_WORD.search(folded)
    if cap is None:
        return None
    value = int(cap.group(1))
    return value if value > 0 else None


_ENGLISH_IATA_WORDS = frozenset(
    {
        "to",
        "or",
        "in",
        "at",
        "be",
        "me",
        "no",
        "do",
        "so",
        "ok",
        "if",
        "as",
        "an",
        "by",
        "up",
        "of",
        "on",
        "it",
        "is",
        "am",
        "we",
        "us",
        "he",
        "my",
        "go",
        "re",
    }
)
_CARRIER_NEGATION = re.compile(
    r"(?:do not|don'?t)(?:\s+\w+){0,2}\s+$"
    r"|(?:never|without|not|except|excluding|no|sin|sans|ohne|sem|senza|keine)\s+$",
    re.IGNORECASE,
)
_CODE_JOIN = r"\s*(?:or|and|/|o|ou|oder)\s*"
_CODE_SPLIT = re.compile(_CODE_JOIN)
_ONLY_CODES = re.compile(
    r"(?:only|just|exclusively)\s+((?:[a-z0-9]{2}" + _CODE_JOIN + r")*[a-z0-9]{2})\b"
    r"|(?<![a-z0-9])((?:[a-z0-9]{2}" + _CODE_JOIN + r")*[a-z0-9]{2})\s+only",
    re.IGNORECASE,
)
_NOT_CODE = re.compile(
    r"\b(?:not|except|excluding|without|no)\s+([a-z0-9]{2})\b",
    re.IGNORECASE,
)
_OR_CODES = re.compile(
    r"\b([a-z0-9]{2}(?:" + _CODE_JOIN + r"[a-z0-9]{2})+)\b",
    re.IGNORECASE,
)
_PREFIX_CODE = re.compile(r"\b([a-z]{2})\s*(?:or|and|/|o|ou|oder|e)\s+$")
_SUFFIX_CODE = re.compile(r"^\s*(?:or|and|/|o|ou|oder|e)\s*([a-z]{2})\b")
_AIRLINE_NAME_TABLE = airline_names_longest_first()


def _carrier_is_negated(text: str, start: int) -> bool:
    prefix = text[max(0, start - 28) : start]
    return bool(_CARRIER_NEGATION.search(prefix))


def _phrase_starts(folded: str, phrase: str) -> list[int]:
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])")
    return [match.start() for match in pattern.finditer(folded)]


def _add_unique(dst: list[str], codes: Sequence[str]) -> None:
    for code in codes:
        if code not in dst:
            dst.append(code)


def _codes_from_blob(blob: str) -> tuple[str, ...]:
    codes: list[str] = []
    for part in _CODE_SPLIT.split(blob.strip()):
        token = part.strip().upper()
        if len(token) == 2 and token.isalpha() and token.casefold() not in _ENGLISH_IATA_WORDS:
            _add_unique(codes, (token,))
    return tuple(codes)


def _mask_alliance_phrases(folded: str) -> str:
    masked = folded
    for phrase, _canonical in ALLIANCE_PHRASES:
        masked = re.sub(
            rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
            lambda match: " " * (match.end() - match.start()),
            masked,
        )
    return masked


def _adjacent_airline_codes(folded: str, start: int, name: str) -> tuple[str, ...]:
    prefix = folded[max(0, start - 12) : start]
    suffix = folded[start + len(name) :]
    found: list[str] = []
    pre = _PREFIX_CODE.search(prefix)
    if pre:
        _add_unique(found, _codes_from_blob(pre.group(1)))
    post = _SUFFIX_CODE.match(suffix)
    if post:
        _add_unique(found, _codes_from_blob(post.group(1)))
    return tuple(found)


def _carriers_from_prompt(
    folded: str, flags: Mapping[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    include: list[str] = []
    exclude: list[str] = []
    alliance: list[str] = []
    exclude_alliance: list[str] = []
    if "airlines" in flags:
        _add_unique(include, parse_airline_codes(flags["airlines"]) or ())
    if "exclude-airlines" in flags:
        _add_unique(exclude, parse_airline_codes(flags["exclude-airlines"]) or ())
    if "alliance" in flags:
        _add_unique(alliance, parse_alliances(flags["alliance"]) or ())
    if "exclude-alliance" in flags:
        _add_unique(exclude_alliance, parse_alliances(flags["exclude-alliance"]) or ())
    for phrase, canonical in ALLIANCE_PHRASES:
        for start in _phrase_starts(folded, phrase):
            if _carrier_is_negated(folded, start):
                _add_unique(exclude_alliance, (canonical,))
            else:
                _add_unique(alliance, (canonical,))
    for name, codes in _AIRLINE_NAME_TABLE:
        for start in _phrase_starts(folded, name):
            neighbors = _adjacent_airline_codes(folded, start, name)
            if _carrier_is_negated(folded, start):
                _add_unique(exclude, codes)
                _add_unique(exclude, neighbors)
            else:
                _add_unique(include, codes)
                _add_unique(include, neighbors)
    carrier_text = _mask_alliance_phrases(folded)
    for match in _ONLY_CODES.finditer(carrier_text):
        blob = match.group(1) or match.group(2)
        _add_unique(include, _codes_from_blob(blob))
    for match in _NOT_CODE.finditer(carrier_text):
        _add_unique(exclude, _codes_from_blob(match.group(1)))
    for match in _OR_CODES.finditer(carrier_text):
        _add_unique(include, _codes_from_blob(match.group(1)))
    include = [code for code in include if code not in exclude]
    alliance = [name for name in alliance if name not in exclude_alliance]
    return tuple(include), tuple(exclude), tuple(alliance), tuple(exclude_alliance)


def _work_back_by(folded: str) -> Optional[str]:
    match = _WORK_BACK_BY.search(folded)
    if match is None:
        return None
    clock = _hhmm(match.group(2))
    if clock is None:
        return None
    weekday = _WEEKDAY_ALIASES.get(match.group(1).casefold())
    if weekday is None:
        return None
    return f"{weekday} {clock}"


def _great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1, rlat2, rlon2 = (radians(lat1), radians(lon1), radians(lat2), radians(lon2))
    dlat, dlon = rlat2 - rlat1, rlon2 - rlon1
    chord = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return _EARTH_RADIUS_KM * 2 * asin(min(1.0, sqrt(chord)))


def _min_block_hours(dep: str, arr: str) -> Optional[float]:
    """Lower-bound airborne block. Not an airline schedule. Do not invent a fare."""
    left = airport_geo(dep)
    right = airport_geo(arr)
    if left is None or right is None:
        return None
    km = _great_circle_km(left[1], left[2], right[1], right[2])
    return km / _CRUISE_KMH + _BLOCK_PAD_HOURS


def _next_weekday_on_or_after(day: date, weekday: str) -> Optional[date]:
    want = _WEEKDAY_INDEX.get(weekday)
    if want is None:
        return None
    return date.fromordinal(day.toordinal() + (want - day.weekday()) % 7)


def _clock_hm(clock: str) -> Optional[tuple[int, int]]:
    parts = clock.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _work_back_impossible_note(
    *,
    origin: Optional[str],
    destination: Optional[str],
    departure: Optional[date],
    returning: Optional[date],
    depart_after: Optional[str],
    work_back_by: Optional[str],
) -> Optional[str]:
    """Note when the named office clock is timezone-impossible. Keep route and field.

    Do not invent hops, a midnight departure, or a feasible IDL itinerary.
    Civil earliest depart is 06:00 local when the prompt named no depart_after.
    """
    if work_back_by is None or origin is None or destination is None:
        return None
    bits = work_back_by.split()
    if len(bits) != 2:
        return None
    weekday, clock = bits
    work_hm = _clock_hm(clock)
    if work_hm is None or weekday not in _WEEKDAY_INDEX:
        return None
    if returning is not None:
        dep_code, arr_code, when = destination, origin, returning
    elif departure is not None:
        dep_code, arr_code, when = origin, destination, departure
    else:
        return None
    if dep_code == arr_code:
        return None
    block = _min_block_hours(dep_code, arr_code)
    dep_geo = airport_geo(dep_code)
    arr_geo = airport_geo(arr_code)
    if block is None or dep_geo is None or arr_geo is None:
        return None
    # Outbound depart_after does not move the return. One-way uses the named clock.
    if returning is not None:
        start_clock = f"{_CIVIL_DEPART_HOUR:02d}:00"
    else:
        start_clock = depart_after or f"{_CIVIL_DEPART_HOUR:02d}:00"
    start_hm = _clock_hm(start_clock)
    if start_hm is None:
        return None
    work_day = _next_weekday_on_or_after(when, weekday)
    if work_day is None:
        return None
    try:
        dep_tz = ZoneInfo(dep_geo[0])
        arr_tz = ZoneInfo(arr_geo[0])
        dep_local = datetime(
            when.year, when.month, when.day, start_hm[0], start_hm[1], tzinfo=dep_tz
        )
        work_local = datetime(
            work_day.year,
            work_day.month,
            work_day.day,
            work_hm[0],
            work_hm[1],
            tzinfo=arr_tz,
        )
    except (ValueError, OSError, KeyError):
        return None
    eta = dep_local + timedelta(hours=block)
    if eta <= work_local:
        return None
    return (
        f"{dep_code}-{arr_code} on {when.isoformat()} cannot make {work_back_by} "
        f"at {arr_code} (timezone/IDL). Keep both clocks; do not invent hops "
        "or drop work_back_by. Do not invent a fare."
    )


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
    for match in _JA_NOT_IATA.finditer(raw):
        code = match.group(1).upper()
        if is_known_iata(code) and code not in found:
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


def _iata_group_hits(pattern: re.Pattern[str], raw: str) -> list[str]:
    found: list[str] = []
    for match in pattern.finditer(raw):
        code = match.group(1).upper()
        if is_known_iata(code) and code not in found:
            found.append(code)
    return found


def _extend_unique(dst: list[str], src: Sequence[str]) -> None:
    for code in src:
        if code not in dst:
            dst.append(code)


def _first_place_iata(text: str) -> Optional[str]:
    """First owned IATA or city-table code after a via/layover lead. No invented codes."""
    stripped = text.strip().lstrip(",.")
    if not stripped:
        return None
    token = stripped.split()[0].strip(" ,.")
    if len(token) == 3 and token.isalpha() and is_known_iata(token.upper()):
        return token.upper()
    folded = _fold(stripped)
    words = [part.strip(" ,.") for part in folded.split() if part.strip(" ,.")]
    for length in range(min(3, len(words)), 0, -1):
        name = " ".join(words[:length])
        code = _CITY_IATA.get(name)
        if code:
            return code
    return None


def _places_after_via_leads(raw: str, *, negated: bool) -> list[str]:
    found: list[str] = []
    for match in _VIA_PLACE_LEAD.finditer(raw):
        if _match_is_negated(raw, match.start()) != negated:
            continue
        code = _first_place_iata(raw[match.end() :])
        if code and code not in found:
            found.append(code)
    return found


def _via_airports(raw: str) -> list[str]:
    found: list[str] = []
    for match in _VIA_AIRPORT_IATA.finditer(raw):
        if _match_is_negated(raw, match.start()):
            continue
        code = match.group(1).upper()
        if is_known_iata(code) and code not in found:
            found.append(code)
    _extend_unique(found, _iata_group_hits(_KA_VIA_IATA, raw))
    _extend_unique(found, _places_after_via_leads(raw, negated=False))
    return found


def _exclude_via_airports(raw: str) -> list[str]:
    found = _iata_group_hits(_EXCLUDE_VIA_IATA, raw)
    _extend_unique(found, _places_after_via_leads(raw, negated=True))
    return found


def _has_via_context(folded: str) -> bool:
    return bool(_VIA_PLACE_LEAD.search(folded))


def _require_overnight_airports(raw: str) -> list[str]:
    return _iata_group_hits(_REQUIRE_OVERNIGHT_IATA, raw)


def _no_overnight_codes(raw: str, folded: str) -> list[str]:
    found: list[str] = []
    _extend_unique(found, _iata_group_hits(_NO_OVERNIGHT_IATA, raw))
    _extend_unique(found, _iata_group_hits(_KA_NO_OVERNIGHT_IATA, raw))
    if found:
        return found
    if "no overnight" in folded or "not overnight" in folded or "never overnight" in folded:
        return ["any"]
    return []


def plan_to_trips(plan: PromptPlan) -> FlightPlan:
    """Build the owned search plan. Does not fetch and does not invent fares."""
    if plan.intent != "flights":
        raise ValueError(f"plan_to_trips requires a flights plan, got {plan.intent!r}")
    if plan.refuse:
        raise ValueError("plan_to_trips cannot search a refused plan: " + ", ".join(plan.refuse))
    if not plan.route_specs:
        raise ValueError("plan_to_trips needs route_specs")
    cabin = cast(
        FlightCabin,
        plan.cabin if plan.cabin in _PLAN_CABINS else "economy",
    )
    parsed = parse_flight_plan(
        plan.route_specs,
        trip=plan.trip or "one-way",
        max_stops=plan.max_stops if plan.max_stops is not None else 1,
        adults=plan.adults if plan.adults is not None else 1,
        children=plan.children if plan.children is not None else 0,
        infants_in_seat=plan.infants_in_seat if plan.infants_in_seat is not None else 0,
        infants_on_lap=plan.infants_on_lap if plan.infants_on_lap is not None else 0,
        cabin=cabin,
        bags=plan.bags,
        carry_on=plan.carry_on,
        price_cap_eur=plan.price_cap_eur,
    )
    if not plan.nearby or not isinstance(parsed, tuple):
        return parsed
    return expand_nearby_trips(parsed, nearby=True)


def plan_to_hotel_query(plan: PromptPlan) -> HotelQuery:
    """Hotel query from a plan that already named a stay. Does not invent a total."""
    if not plan.hotels:
        raise ValueError("plan_to_hotel_query requires hotels=true")
    if plan.refuse:
        raise ValueError(
            "plan_to_hotel_query cannot search a refused plan: " + ", ".join(plan.refuse)
        )
    if plan.location is None or plan.check_in is None or plan.check_out is None:
        raise ValueError("plan_to_hotel_query needs location, check_in, and check_out")
    return HotelQuery(
        plan.location,
        plan.check_in,
        plan.check_out,
        adults=plan.adults if plan.adults is not None else 2,
        rooms=plan.rooms if plan.rooms is not None else 1,
    )


def plan_prompt(text: str, *, today: Optional[date] = None) -> PromptPlan:
    raw = " ".join(text.split())
    folded = _fold(raw)
    flags = _flag_map(raw)
    nearby = _wants_nearby(raw, folded)
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
    require_overnight = _require_overnight_airports(raw)
    via_airports = _via_airports(raw)
    exclude_via = _exclude_via_airports(raw)
    no_overnight = _no_overnight_codes(raw, folded)
    if "via" in flags:
        _extend_unique(via_airports, parse_via_airports(flags["via"]) or ())
    if "exclude-via" in flags:
        _extend_unique(
            exclude_via,
            parse_via_airports(flags["exclude-via"], role="exclude-via") or (),
        )
    if "no-overnight" in flags:
        _extend_unique(
            no_overnight,
            parse_overnight_airports(flags["no-overnight"], role="no-overnight") or (),
        )
    if "require-overnight" in flags:
        _extend_unique(
            require_overnight,
            parse_overnight_airports(flags["require-overnight"], role="require-overnight") or (),
        )
    if "exclude-airports" in flags:
        _extend_unique(
            exclude_airports,
            parse_via_airports(flags["exclude-airports"], role="exclude-airports") or (),
        )
    include_airports: list[str] = []
    if "include-airports" in flags:
        _extend_unique(
            include_airports,
            parse_via_airports(flags["include-airports"], role="include-airports") or (),
        )
    via_airports = [code for code in via_airports if code not in exclude_via]

    if _NO_ASIA.search(folded) or "tercermundista" in folded:
        exclude_regions.append("asia")
    for code in _excluded_airports(raw):
        if code not in exclude_airports:
            exclude_airports.append(code)
    keep_connect = set(require_overnight) | set(via_airports)
    for code in no_overnight:
        if code != "any" and code not in keep_connect and code not in exclude_airports:
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
        if min_hit and (
            "layover" in folded
            or "connection" in folded
            or "escala" in folded
            or "გადაჯდომა" in folded
        ):
            min_layover = float(min_hit.group(1))
    max_duration: Optional[float] = None
    if "max-duration" in flags:
        max_duration = float(flags["max-duration"])
    else:
        dur = _MAX_DURATION.search(folded)
        if dur:
            max_duration = float(dur.group(1))

    adults = None
    adult_counts: list[int] = []
    if "adults" in flags:
        adults = int(flags["adults"])
    else:
        adult_counts = _int_all(
            (_ADULTS, _ADULTS_REVERSE, _ADULTS_CJK),
            folded,
        )
        unique_adults = list(dict.fromkeys(adult_counts))
        if len(unique_adults) == 1:
            adults = unique_adults[0]
        elif not unique_adults and _SAME_ADULT.search(folded):
            adults = 1

    children = None
    if "children" in flags:
        children = int(flags["children"])
    else:
        children = _int_after((_CHILDREN,), folded)

    infants_in_seat = None
    if "infants-in-seat" in flags:
        infants_in_seat = int(flags["infants-in-seat"])
    else:
        infants_in_seat = _int_after((_INFANTS_IN_SEAT_EN,), folded)

    infants_on_lap = None
    if "infants-on-lap" in flags:
        infants_on_lap = int(flags["infants-on-lap"])
    else:
        infants_on_lap = _int_after((_INFANTS_ON_LAP_EN,), folded)
        if infants_on_lap is None:
            infants_on_lap = _int_after((_INFANTS_EN,), folded)

    rooms = None
    room_counts: list[int] = []
    if "rooms" in flags:
        rooms = int(flags["rooms"])
    else:
        room_counts = _int_all(
            (
                _ROOMS_ES_PLURAL,
                _ROOMS_ES_SINGULAR,
                _ROOMS_EN,
                _ROOMS_I18N,
                _ROOMS_REVERSE,
                _ROOMS_CJK,
            ),
            folded,
        )
        unique_rooms = list(dict.fromkeys(room_counts))
        if len(unique_rooms) == 1:
            rooms = unique_rooms[0]

    days = None
    nights_stay: Optional[int] = None
    if "nights" in flags:
        nights_stay = int(flags["nights"])
    else:
        nights_match = _NIGHTS.search(folded)
        if nights_match:
            nights_stay = int(nights_match.group(1))
    if "days" in flags:
        days = int(flags["days"])
    else:
        days_match = _DAYS_ES.search(folded)
        if days_match is None:
            days_match = _DAYS_EN.search(folded)
        if days_match:
            days = int(days_match.group(1))
        elif nights_stay is not None:
            days = nights_stay

    cabin = _cabin(folded, flags)
    date_strategy = _date_strategy(folded)
    max_stops = _max_stops(folded, flags)
    include_airlines, exclude_airlines, alliance, exclude_alliance = _carriers_from_prompt(
        folded, flags
    )
    fetch = flags.get("fetch")
    if require_clock or (night and ("hora" in folded or "clock" in folded)):
        fetch = fetch or "detail"

    weekday = None
    if "viernes" in folded or "friday" in folded:
        weekday = "friday"

    price_cap = _named_price_cap_eur(folded, flags)
    currency = _named_currency(flags)
    country = _named_country(flags)

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
        via_block = set(via_airports) | set(exclude_via)
        codes = [
            match
            for match in _IATA_TOKEN_UPPER.findall(raw)
            if is_known_iata(match) and match not in via_block
        ]
        if len(codes) >= 2:
            destination = codes[1]
        elif len(codes) == 1 and origin and codes[0] != origin:
            destination = codes[0]
    if destination is None:
        nearby_city = _nearby_city_name(folded)
        if nearby_city is not None:
            dest = _resolve_city_iata(nearby_city)
            if dest and dest != origin:
                destination = dest

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

    via_airports = [code for code in via_airports if code not in {origin, destination}]
    exclude_via = [code for code in exclude_via if code not in {origin, destination}]
    named_od_airports = set(_AIRPORT_NAME_TO_IATA.values())
    if _has_via_context(folded):
        still_exclude: list[str] = []
        for code in exclude_airports:
            overnight = code in no_overnight
            if (
                code not in {origin, destination}
                and code not in named_od_airports
                and not overnight
            ):
                _extend_unique(exclude_via, [code])
            else:
                still_exclude.append(code)
        exclude_airports = still_exclude

    destinations = _listed_destinations(raw, origin)
    unnamed_dest_quote = _is_unnamed_dest_quote(folded, destinations)
    if unnamed_dest_quote:
        destinations = ()

    trip = _trip_kind(folded, flags, len(pairs), date_count=len(dates))
    split_return = bool(_ASKED_TWO_ONE_WAYS.search(folded))
    if trip is None and origin and destination:
        trip = "one-way"
    if around:
        # Circumnavigation is a multi-city circuit, not one packaged O-D.
        # Closing destination=origin below must not collapse this to one-way.
        trip = "multi" if len(pairs) < 2 else (trip or "multi")
        if destination is None and origin and is_known_iata(origin):
            destination = origin

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
    elif around and not known_pairs:
        # Do not emit a fake origin-origin spec or invent hops the user did not name.
        route_specs = ()
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
    elif len(known_pairs) >= 2 and (trip == "rt" or "open jaw" in folded):
        notes = (
            "Keep every dated open-jaw city pair; "
            "do not collapse them into one origin-destination. Do not invent a fare."
        )
    elif set(require_overnight) & set(no_overnight) or (
        require_overnight and "any" in no_overnight
    ):
        codes = [
            code for code in require_overnight if code in no_overnight or "any" in no_overnight
        ]
        joined = "/".join(codes) if codes else "the named airport"
        notes = f"Unsatisfiable layover: overnight {joined} is both required and forbidden."
    elif around:
        notes = _around_the_world_notes(
            max_stops=max_stops,
            exclude_regions=exclude_regions,
            max_layover=max_layover,
        )
    elif via_regions:
        notes = _via_regions_notes()
    named_cabins = _named_cabins(folded)
    if "cabin" not in flags and len(named_cabins) >= 2:
        joined = " and ".join(named_cabins)
        notes = _append_note(
            notes,
            f"Unsatisfiable cabin: {joined} on one seat. Do not pick a cabin. "
            "Do not invent a fare.",
        )
    unique_adults = list(dict.fromkeys(adult_counts))
    unique_rooms = list(dict.fromkeys(room_counts))
    if len(unique_adults) >= 2:
        flight_n, hotel_n = unique_adults[0], unique_adults[1]
        room_bit = f" and {unique_rooms[-1]} rooms" if unique_rooms else ""
        notes = _append_note(
            notes,
            f"Flights are {flight_n} adults; hotel is {hotel_n} adults{room_bit}. "
            "Keep both occupancies; do not silently pick one --adults. "
            "Do not invent a fare.",
        )
    if max_stops == 0 and via_airports:
        via_joined = "/".join(via_airports)
        notes = _append_note(
            notes,
            f"Nonstop max 0 stops cannot also be via {via_joined}. "
            "Keep both constraints; do not drop via or the nonstop. "
            "Do not invent a fare.",
        )
    overlap_overnight = [code for code in via_airports if code in no_overnight and code != "any"]
    if overlap_overnight:
        joined = "/".join(overlap_overnight)
        hours = f" with >={min_layover:g}h" if min_layover is not None else ""
        overnight_hours = (
            f" A {min_layover:g}h {joined} layover is an overnight."
            if min_layover is not None
            else " A long layover can be overnight."
        )
        notes = _append_note(
            notes,
            f"Connect {joined}{hours} and never overnight {joined}."
            f"{overnight_hours} Keep both constraints; do not drop via or "
            "no_overnight. keep_connect is require_overnight ∪ via. "
            "Do not invent a fare.",
        )
    pair_airports = {code for left, right in known_pairs for code in (left, right)}
    vs_hub = bool(_VS_HUB.search(folded))
    ordered_named: list[str] = []
    for token in _IATA_TOKEN_UPPER.findall(raw):
        if token in named_airports:
            _extend_unique(ordered_named, [token])
    for name, code in _AIRPORT_NAME_TO_IATA.items():
        if name in folded and code in named_airports:
            _extend_unique(ordered_named, [code])
    for left, right in _same_city_named_pairs(ordered_named):
        if left in via_airports or right in via_airports:
            continue
        if left in pair_airports and right in pair_airports:
            continue
        if not (vs_hub or left in exclude_airports or right in exclude_airports):
            continue
        notes = _append_note(
            notes,
            f"Named {left} vs {right} is a constraint, not a license to invent "
            "a second destination or a price.",
        )
    if rest_of_trip:
        notes = _append_note(
            notes,
            "Prices and destinations only; do not plan hotels, trains, or the rest of the trip.",
        )
    if date_strategy == "fixed_then_plus_minus_1":
        notes = _append_note(
            notes,
            "Do not brute-force a date matrix; shortlist then ±1 on 1-3 finalists.",
        )
    if exclude_regions and _is_explore(folded):
        origin_inside = _origin_in_excluded_regions(origin, exclude_regions)
        if origin_inside and origin:
            notes = _append_note(
                notes,
                _origin_inside_excluded_region_notes(origin, origin_inside),
            )
        else:
            origin_bit = origin or "this origin"
            dropped = ", ".join(exclude_regions)
            notes = _append_note(
                notes,
                f"Priced {origin_bit} explore shortlist; drop {dropped}.",
            )
    if unnamed_dest_quote:
        notes = _append_note(
            notes,
            _unnamed_dest_quote_notes(_unnamed_dest_quote_count(folded)),
        )
    if nearby:
        notes = _append_note(
            notes,
            "Expand origin or dest with --nearby to owned same-city IATA; "
            "keep named airports. Do not invent a fare.",
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
    if plan_hotels and location is None and destination:
        location = _IATA_TO_ENGLISH.get(destination)
    if plan_hotels and location is None:
        location = _first_english_city(folded)
    check_in = dates[0] if plan_hotels and dates else None
    check_out = dates[1] if plan_hotels and len(dates) >= 2 else None

    search_trip = False
    if plan_hotels and (pairs or (origin and destination)):
        flight_start = departure
        flight_end = returning or departure
        if flight_start is not None and check_in is not None and check_out is not None:
            search_trip = flight_start <= check_out and check_in <= flight_end
    if search_trip:
        notes = _append_note(
            notes,
            "Owned search_trip / trip_total is flight fare + hotel stay when dates "
            "overlap; omit the sum if either side misses, dates do not overlap, or "
            "currencies differ. Do not invent a fare or a stay.",
        )
    if trip == "rt":
        notes = _append_note(
            notes,
            "Stamp typical_eur / vs_typical / typical_deal from the owned same-stay "
            "calendar when it has at least three priced days; omit on a miss or "
            "multi-city. Do not invent a typical.",
        )
    if plan_hotels and _FREE_CANCELLATION.search(folded) and _NONREFUNDABLE.search(folded):
        notes = _append_note(
            notes,
            "Free cancellation only and prepaid non-refundable only cannot both hold. "
            "Keep both; default search_hotels is free cancellation, "
            "--allow-non-refundable is the opt-out. Do not invent a stay.",
        )

    if departure is not None and departure < today and not plan_hotels:
        refuse.append("past_date")

    baggage, bags, carry_on = _bag_fields(folded, flags, raw)
    arrive_before = _named_hhmm(folded, flags, flag="arrive-before", pattern=_ARRIVE_BEFORE)
    depart_after = _named_hhmm(folded, flags, flag="depart-after", pattern=_DEPART_AFTER)
    depart_window = _depart_window(folded, flags)
    sort = _sort_key(folded, flags)
    work_back_by = _work_back_by(folded)
    impossible_clock = _work_back_impossible_note(
        origin=origin if origin and is_known_iata(origin) else None,
        destination=destination if destination and is_known_iata(destination) else None,
        departure=departure,
        returning=returning,
        depart_after=depart_after,
        work_back_by=work_back_by,
    )
    if impossible_clock:
        notes = _append_note(notes, impossible_clock)
    if _weekday_nofly(folded) or _weekend_clocks(weekday, depart_after, work_back_by):
        notes = _append_note(notes, _weekday_nofly_notes())
    jaw_airports: list[str] = []
    if trip == "rt" and len(known_pairs) >= 2:
        pair_counts: dict[str, int] = {}
        for left, right in known_pairs:
            pair_counts[left] = pair_counts.get(left, 0) + 1
            pair_counts[right] = pair_counts.get(right, 0) + 1
        for left, right in known_pairs:
            for code in (left, right):
                if pair_counts[code] == 1 and code not in jaw_airports:
                    jaw_airports.append(code)
    if around:
        prefer_seed = (*use_airports, *via_airports, *require_overnight)
    elif jaw_airports:
        prefer_seed = (*use_airports, *via_airports, *require_overnight, *jaw_airports)
    else:
        prefer_seed = (
            origin,
            destination,
            *use_airports,
            *via_airports,
            *require_overnight,
        )
    prefer_airports: Tuple[str, ...] = tuple(
        dict.fromkeys(
            code
            for code in prefer_seed
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
    elif _is_flex_window(folded, flags, dates):
        intent = "flex"
        span = _flex_days_value(folded, flags, dates)
        anchor = dates[0]
        if span is not None:
            date_from = date.fromordinal(anchor.toordinal() - span)
            date_to = date.fromordinal(anchor.toordinal() + span)
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
    if unnamed_dest_quote:
        extra_refuse.append("booking")
        extra_refuse.append("hotels")

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
    skip_include = {code for code in (origin, *via_airports, *require_overnight) if code}
    if intent == "explore":
        for code in destinations:
            if code not in skip_include:
                _extend_unique(include_airports, [code])
        for code in prefer_airports:
            if code not in skip_include:
                _extend_unique(include_airports, [code])
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
        english = _english_place_name(query)
        return PromptPlan(
            intent="airports",
            airports_query=(english or query).casefold(),
        )

    if intent == "hotels":
        return PromptPlan(
            intent="hotels",
            location=location,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
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
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            date_strategy=date_strategy,
            max_stops=max_stops,
            price_cap_eur=price_cap,
            max_layover=max_layover,
            exclude_regions=tuple(exclude_regions),
            exclude_airports=tuple(exclude_airports),
            include_airports=tuple(include_airports),
            via_airports=tuple(via_airports),
            exclude_via=tuple(exclude_via),
            no_overnight=tuple(no_overnight),
            require_overnight=tuple(require_overnight),
            refuse=all_refuse,
            baggage=baggage,
            bags=bags,
            carry_on=carry_on,
            nearby=nearby,
            depart_window=depart_window,
            arrive_before=arrive_before,
            depart_after=depart_after,
            min_layover=min_layover,
            max_duration=max_duration,
            alliance=alliance,
            exclude_alliance=exclude_alliance,
            notes=notes,
            currency=currency,
            country=country,
            sort=sort,
        )

    if intent == "flex":
        date_trip = (
            "rt" if nights_stay is not None else (trip if trip in {"one-way", "rt"} else "one-way")
        )
        return PromptPlan(
            intent="flex",
            origin=origin,
            destination=destination,
            departure_date=dates[0] if dates else departure,
            date_from=date_from,
            date_to=date_to,
            trip=date_trip,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            max_stops=max_stops,
            days=nights_stay,
            baggage=baggage,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap,
            flex_days=_flex_days_value(folded, flags, dates),
            via_airports=tuple(via_airports),
            exclude_via=tuple(exclude_via),
            no_overnight=tuple(no_overnight),
            require_overnight=tuple(require_overnight),
            alliance=alliance,
            exclude_alliance=exclude_alliance,
            depart_window=depart_window,
            arrive_before=arrive_before,
            depart_after=depart_after,
            nearby=nearby,
            route_specs=(),
            locale=FETCH_LANGUAGE,
            notes=notes,
            max_layover=max_layover,
            min_layover=min_layover,
            max_duration=max_duration,
            currency=currency,
            country=country,
            exclude_airports=tuple(exclude_airports),
            include_airports=tuple(include_airports),
        )

    if intent == "dates":
        date_trip = (
            "rt" if nights_stay is not None else (trip if trip in {"one-way", "rt"} else "one-way")
        )
        return PromptPlan(
            intent="dates",
            origin=origin,
            destination=destination,
            date_from=date_from or departure,
            date_to=date_to or returning,
            trip=date_trip,
            weekday=weekday,
            adults=adults,
            children=children,
            infants_in_seat=infants_in_seat,
            infants_on_lap=infants_on_lap,
            cabin=cabin,
            max_stops=max_stops,
            days=nights_stay if nights_stay is not None else days,
            baggage=baggage,
            bags=bags,
            carry_on=carry_on,
            price_cap_eur=price_cap,
            via_airports=tuple(via_airports),
            exclude_via=tuple(exclude_via),
            no_overnight=tuple(no_overnight),
            require_overnight=tuple(require_overnight),
            alliance=alliance,
            exclude_alliance=exclude_alliance,
            depart_window=depart_window,
            arrive_before=arrive_before,
            depart_after=depart_after,
            nearby=nearby,
            route_specs=(),
            max_layover=max_layover,
            min_layover=min_layover,
            max_duration=max_duration,
            currency=currency,
            country=country,
            exclude_airports=tuple(exclude_airports),
            include_airports=tuple(include_airports),
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
        children=children,
        infants_in_seat=infants_in_seat,
        infants_on_lap=infants_on_lap,
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
        include_airports=tuple(include_airports),
        via_regions=via_regions,
        via_airports=tuple(via_airports),
        exclude_via=tuple(exclude_via),
        no_overnight=tuple(no_overnight),
        require_overnight=tuple(require_overnight),
        refuse=all_refuse,
        hotels=plan_hotels,
        baggage=baggage,
        bags=bags,
        carry_on=carry_on,
        include_airlines=include_airlines,
        exclude_airlines=exclude_airlines,
        alliance=alliance,
        exclude_alliance=exclude_alliance,
        arrive_before=arrive_before,
        depart_after=depart_after,
        depart_window=depart_window,
        sort=sort,
        prefer_airports=prefer_airports,
        work_back_by=work_back_by,
        route_specs=route_specs,
        nearby=nearby,
        search_trip=search_trip,
        notes=notes,
        currency=currency,
        country=country,
    )
