"""Offline IATA airport lookup. No network."""

from __future__ import annotations

import csv
import marshal
from dataclasses import dataclass
from importlib.resources import files
from typing import Mapping, Optional, Sequence, Tuple

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
        "CTS",
    }
)
_QUERY_REWRITE = {
    "lisboa": "lisbon",
    "ciudad de méxico": "mexico city",
    "ciudad de mexico": "mexico city",
}
_QUERY_EXTRA_IATA = {
    "sapporo": ("CTS",),
}
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
_BY_CITY_COUNTRY: Optional[dict[tuple[str, str], tuple[Airport, ...]]] = None
NEARBY_MAX = 5


def _load_iata_airports() -> dict[str, Airport]:
    """Load iata/name/city/country. Prefer the marshal blob; geo still uses airportsdata."""
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


def _by_code() -> dict[str, Airport]:
    """Code → Airport. Does not build the city/name scan tables."""
    global _BY_CODE
    loaded = _BY_CODE
    if loaded is None:
        loaded = _load_iata_airports()
        _BY_CODE = loaded
    return loaded


def is_known_iata(code: str) -> bool:
    # Known-code checks skip the city/name scan tables used by lookup_airports.
    if len(code) == 3 and code.isalpha():
        text = code if code.isupper() else code.upper()
    else:
        text = code.strip().upper()
        if len(text) != 3 or not text.isalpha():
            return False
    return text in _by_code()


def _lookup_indexes() -> tuple[
    tuple[tuple[Airport, str, str, str], ...],
    dict[str, Airport],
    dict[str, tuple[Airport, ...]],
]:
    global _LOOKUP_ROWS, _BY_CITY
    by_code = _by_code()
    if _LOOKUP_ROWS is None:
        by_city: dict[str, list[Airport]] = {}
        rows: list[tuple[Airport, str, str, str]] = []
        for airport in by_code.values():
            city_folded = airport.city.casefold()
            name_folded = airport.name.casefold()
            by_city.setdefault(city_folded, []).append(airport)
            rows.append((airport, city_folded, name_folded, airport.iata.casefold()))
        for airports in by_city.values():
            airports.sort(key=_lookup_rank)
        _BY_CITY = {city: tuple(airports) for city, airports in by_city.items()}
        _LOOKUP_ROWS = tuple(rows)
    assert _BY_CITY is not None
    return _LOOKUP_ROWS, by_code, _BY_CITY


def get_airport(code: str) -> Optional[Airport]:
    text = code.strip().upper()
    return _by_code().get(text)


_GEO: Optional[dict[str, tuple[str, float, float]]] = None
_GEO_REGIONS: Optional[frozenset[str]] = None


def _airport_geo_index() -> dict[str, tuple[str, float, float]]:
    """IANA tz, lat, lon from the published CSV. Lazy; not paid at import."""
    global _GEO
    cached = _GEO
    if cached is not None:
        return cached
    source = files("airportsdata").joinpath("airports.csv")
    geo: dict[str, tuple[str, float, float]] = {}
    with source.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        iata_i = header.index("iata")
        lat_i = header.index("lat")
        lon_i = header.index("lon")
        tz_i = header.index("tz")
        for raw in reader:
            code = raw[iata_i] if iata_i < len(raw) else ""
            tz = raw[tz_i] if tz_i < len(raw) else ""
            if not code or not tz:
                continue
            try:
                geo[code] = (tz, float(raw[lat_i]), float(raw[lon_i]))
            except (IndexError, TypeError, ValueError):
                continue
    _GEO = geo
    return geo


def airport_geo(code: str) -> Optional[tuple[str, float, float]]:
    """IANA timezone, latitude, longitude. Omit when the published row has no tz."""
    text = code.strip().upper()
    if len(text) != 3 or not text.isalpha():
        return None
    return _airport_geo_index().get(text)


def owned_tz_region_tokens() -> frozenset[str]:
    """IANA continent prefixes already published on owned airport rows.

    Built from ``tz`` (``Asia/Tokyo`` → ``asia``). Not a hand-made dest list.
    """
    global _GEO_REGIONS
    cached = _GEO_REGIONS
    if cached is not None:
        return cached
    tokens: set[str] = set()
    for tz, _, _ in _airport_geo_index().values():
        prefix, sep, _rest = tz.partition("/")
        if sep and prefix:
            tokens.add(prefix.casefold())
    owned = frozenset(tokens)
    _GEO_REGIONS = owned
    return owned


def parse_exclude_regions(text: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Parse comma-separated owned IANA region tokens for ``--exclude-regions``.

    Unnamed stays unset. Unknown tokens are rejected; do not invent a region.
    """
    if text is None:
        return None
    parts = tuple(
        part.strip().casefold().replace(" ", "_") for part in text.split(",") if part.strip()
    )
    if not parts:
        raise ValueError("exclude-regions list must not be empty")
    known = owned_tz_region_tokens()
    parsed: list[str] = []
    for token in parts:
        if token not in known:
            raise ValueError(f"unknown exclude-regions token: {token!r}")
        if token not in parsed:
            parsed.append(token)
    return tuple(parsed)


def matching_excluded_regions(
    code: Optional[str],
    exclude_regions: Sequence[str],
) -> Tuple[str, ...]:
    """Owned IANA tz prefix vs excluded region. Unknown tz cannot prove inside."""
    if not code or not exclude_regions:
        return ()
    geo = airport_geo(code)
    if geo is None:
        return ()
    tz = geo[0].casefold()
    return tuple(region for region in exclude_regions if tz.startswith(f"{region.casefold()}/"))


def dest_blocked_by_exclude_regions(
    code: str,
    exclude_regions: Sequence[str],
) -> bool:
    """Drop dest when a named region filter applies and owned tz cannot prove keep."""
    if not exclude_regions:
        return False
    if airport_geo(code) is None:
        return True
    return bool(matching_excluded_regions(code, exclude_regions))


def _by_city_country() -> dict[tuple[str, str], tuple[Airport, ...]]:
    """City+country → airports. Built from the code table; no city-name scan."""
    global _BY_CITY_COUNTRY
    cached = _BY_CITY_COUNTRY
    if cached is None:
        groups: dict[tuple[str, str], list[Airport]] = {}
        for airport in _by_code().values():
            if airport.city.strip():
                groups.setdefault((airport.city, airport.country), []).append(airport)
        cached = {key: tuple(rows) for key, rows in groups.items()}
        _BY_CITY_COUNTRY = cached
    return cached


def same_city_iata(code: str) -> Tuple[str, ...]:
    """Owned same-city passenger codes for optional `--nearby` expand.

    Groups by the table's ``city`` and ``country``. Returns the named seed
    alone when there is no second major in that group. Never invents a code.
    """
    airport = get_airport(code)
    if airport is None:
        return ()
    seed = airport.iata
    if not airport.city.strip():
        return (seed,)
    peers = _by_city_country().get((airport.city, airport.country), (airport,))
    majors = [row for row in peers if row.iata in _MAJOR_IATA]
    chosen = {row.iata for row in majors}
    chosen.add(seed)
    if len(chosen) < 2:
        return (seed,)
    rest = [
        row.iata
        for row in sorted(peers, key=_lookup_rank)
        if row.iata in chosen and row.iata != seed
    ]
    return (seed, *rest)[:NEARBY_MAX]


def lookup_airports(query: str, *, limit: int = 20) -> Tuple[Airport, ...]:
    needle = " ".join(query.split()).casefold()
    if not needle:
        raise ValueError("airport query must not be blank")
    extra_codes = _QUERY_EXTRA_IATA.get(needle, ())
    needle = _QUERY_REWRITE.get(needle, needle)
    rows, by_code, by_city = _lookup_indexes()
    if len(needle) == 3 and needle.isalpha():
        exact = by_code.get(needle.upper())
        if exact is not None:
            return (exact,)
    city_hits = list(by_city.get(needle, ()))
    if len(city_hits) >= limit:
        return tuple(city_hits[:limit])
    taken = {airport.iata for airport in city_hits}
    extra_hits = [by_code[code] for code in extra_codes if code in by_code and code not in taken]
    taken.update(airport.iata for airport in extra_hits)
    extra_hits.sort(key=_lookup_rank)
    other_hits = [
        airport
        for airport, city_folded, name_folded, iata_folded in rows
        if airport.iata not in taken
        and (needle in city_folded or needle in name_folded or needle == iata_folded)
    ]
    other_hits.sort(key=_lookup_rank)
    return tuple((extra_hits + city_hits + other_hits)[:limit])


def _lookup_rank(airport: Airport) -> tuple[int, int, str, str]:
    name = airport.name.casefold()
    minor = 1 if any(marker in name for marker in _MINOR_NAME_MARKERS) else 0
    major = 0 if airport.iata in _MAJOR_IATA else 1
    return (major, minor, airport.iata, airport.name)
