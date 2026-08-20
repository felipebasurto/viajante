"""Offline IATA airport lookup. No network."""

from __future__ import annotations

import csv
import marshal
from dataclasses import dataclass
from importlib.resources import files
from typing import Mapping, Optional, Tuple

# Passenger airports that should outrank general-aviation / municipal fields
# when a city name matches several codes (London: LHR before BQH).
_MAJOR_IATA = frozenset(
    {
        "AMS",
        "ATH",
        "BCN",
        "BER",
        "BRU",
        "BUD",
        "CDG",
        "CPH",
        "DUB",
        "DUS",
        "FCO",
        "FRA",
        "GVA",
        "HAM",
        "HEL",
        "IST",
        "LCY",
        "LGW",
        "LHR",
        "LIS",
        "LTN",
        "LYS",
        "MAD",
        "MAN",
        "MUC",
        "MXP",
        "NCE",
        "ORY",
        "OSL",
        "PMI",
        "PRG",
        "STN",
        "VIE",
        "WAW",
        "ZRH",
        "ATL",
        "BOS",
        "DEN",
        "DFW",
        "EWR",
        "IAD",
        "IAH",
        "JFK",
        "LAX",
        "LGA",
        "MIA",
        "ORD",
        "SEA",
        "SFO",
        "YYZ",
        "DXB",
        "DOH",
        "HKG",
        "HND",
        "ICN",
        "NRT",
        "PEK",
        "PVG",
        "SIN",
        "SYD",
    }
)
_MINOR_NAME_MARKERS = (
    "airfield",
    "air field",
    "air base",
    "airbase",
    "afb",
    "heliport",
    "helipad",
    "raf ",
    "municipal",
)


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str

    def to_dict(self) -> Mapping[str, str]:
        return {
            "iata": self.iata,
            "name": self.name,
            "city": self.city,
            "country": self.country,
        }


_LOOKUP_ROWS: Optional[tuple[tuple[Airport, str, str, str], ...]] = None
_BY_CODE: Optional[dict[str, Airport]] = None
_BY_CITY: Optional[dict[str, tuple[Airport, ...]]] = None


def _load_iata_airports() -> dict[str, Airport]:
    """Load published IATA rows. Prefer the checked-in marshal blob over CSV."""
    cached = files("viajante").joinpath("iata_rows.marshal")
    try:
        payload = marshal.loads(cached.read_bytes())
        return {
            code: Airport(iata=code, name=name or code, city=city, country=country)
            for code, name, city, country in payload
        }
    except (FileNotFoundError, OSError, ValueError, TypeError, EOFError):
        return _load_iata_airports_from_csv()


def _load_iata_airports_from_csv() -> dict[str, Airport]:
    """Read only the fields we publish. Skip DictReader and numeric columns."""
    source = files("airportsdata").joinpath("airports.csv")
    by_code: dict[str, Airport] = {}
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        iata_i = header.index("iata")
        name_i = header.index("name")
        city_i = header.index("city")
        country_i = header.index("country")
        for raw in reader:
            code = raw[iata_i]
            if not code:
                continue
            name = raw[name_i] or code
            by_code[code] = Airport(
                iata=code,
                name=name,
                city=raw[city_i],
                country=raw[country_i],
            )
    return by_code


def is_known_iata(code: str) -> bool:
    text = code.strip().upper()
    if len(text) != 3 or not text.isalpha():
        return False
    _rows, by_code, _by_city = _lookup_indexes()
    return text in by_code


def _lookup_indexes() -> tuple[
    tuple[tuple[Airport, str, str, str], ...],
    dict[str, Airport],
    dict[str, tuple[Airport, ...]],
]:
    global _LOOKUP_ROWS, _BY_CODE, _BY_CITY
    if _LOOKUP_ROWS is None:
        by_code = _load_iata_airports()
        by_city: dict[str, list[Airport]] = {}
        rows: list[tuple[Airport, str, str, str]] = []
        for airport in by_code.values():
            city_folded = airport.city.casefold()
            name_folded = airport.name.casefold()
            by_city.setdefault(city_folded, []).append(airport)
            rows.append((airport, city_folded, name_folded, airport.iata.casefold()))
        for airports in by_city.values():
            airports.sort(key=_lookup_rank)
        _BY_CODE = by_code
        _BY_CITY = {city: tuple(airports) for city, airports in by_city.items()}
        _LOOKUP_ROWS = tuple(rows)
    assert _BY_CODE is not None
    assert _BY_CITY is not None
    return _LOOKUP_ROWS, _BY_CODE, _BY_CITY


def get_airport(code: str) -> Optional[Airport]:
    text = code.strip().upper()
    _rows, by_code, _by_city = _lookup_indexes()
    return by_code.get(text)


def lookup_airports(query: str, *, limit: int = 20) -> Tuple[Airport, ...]:
    needle = " ".join(query.split()).casefold()
    if not needle:
        raise ValueError("airport query must not be blank")
    rows, by_code, by_city = _lookup_indexes()
    if len(needle) == 3 and needle.isalpha():
        exact = by_code.get(needle.upper())
        if exact is not None:
            return (exact,)
    city_hits = list(by_city.get(needle, ()))
    if len(city_hits) >= limit:
        return tuple(city_hits[:limit])
    taken = {airport.iata for airport in city_hits}
    other_hits = [
        airport
        for airport, city_folded, name_folded, iata_folded in rows
        if airport.iata not in taken
        and (needle in city_folded or needle in name_folded or needle == iata_folded)
    ]
    other_hits.sort(key=_lookup_rank)
    return tuple((city_hits + other_hits)[:limit])


def _lookup_rank(airport: Airport) -> tuple[int, int, str, str]:
    name = airport.name.casefold()
    minor = 1 if any(marker in name for marker in _MINOR_NAME_MARKERS) else 0
    major = 0 if airport.iata in _MAJOR_IATA else 1
    return (major, minor, airport.iata, airport.name)
