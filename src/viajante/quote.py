"""Quote currency for Google `curr`. No FX. No amount conversion.

Owned mapping: ISO 3166-1 alpha-2 (IATA `country`) → that country's ISO 4217
cash currency. Explicit `--currency` / MCP `currency` wins. Unnamed infers from
the named origin airport's country. Unproven origin does not become EUR.

This module does not convert EUR to USD or any other pair. It does not ship
exchange rates. Google returns amounts in the requested `curr`; callers (MCP
agents) may convert for the user.
"""

from __future__ import annotations

from typing import Optional

from viajante.airports import get_airport
from viajante.models import normalize_currency

# Same figure as flights.DEFAULT_BAGGAGE_BUFFER_EUR. EUR-only unnamed default.
# Not an FX amount. Do not convert this 70 into another currency.

CURRENCY_REQUIRED = (
    "Cannot prove a quote currency from the origin airport's country. "
    "Pass --currency / currency with an ISO 4217 code "
    "(JFK is USD, LHR is GBP, NRT is JPY, GRU is BRL). "
    "Viajante does not convert currencies; the caller may convert for the user."
)
HOTEL_CURRENCY_REQUIRED = (
    "Hotel search has no origin airport. Pass --currency / currency with an "
    "ISO 4217 code. Viajante does not convert currencies; the caller may "
    "convert for the user."
)

# Eurozone members plus territories that use EUR as cash (not "Europe" as a vibe).
_EUR = frozenset(
    {
        "AT",
        "BE",
        "BG",
        "BL",
        "CY",
        "DE",
        "EE",
        "ES",
        "FI",
        "FR",
        "GF",
        "GP",
        "GR",
        "HR",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MC",
        "ME",
        "MF",
        "MQ",
        "MT",
        "NL",
        "PM",
        "PT",
        "RE",
        "SI",
        "SK",
        "SM",
        "YT",
    }
)
_USD = frozenset(
    {
        "AS",
        "BQ",
        "EC",
        "FM",
        "GU",
        "MH",
        "MP",
        "PR",
        "PW",
        "SV",
        "TC",
        "TL",
        "UM",
        "US",
        "VG",
        "VI",
    }
)
_XOF = frozenset({"BF", "BJ", "CI", "GW", "ML", "NE", "SN", "TG"})
_XAF = frozenset({"CF", "CG", "CM", "GA", "GQ", "TD"})
_XCD = frozenset({"AG", "AI", "DM", "GD", "KN", "LC", "MS", "VC"})
_XPF = frozenset({"NC", "PF", "WF"})

_SINGLE: dict[str, str] = {
    "AE": "AED",
    "AF": "AFN",
    "AL": "ALL",
    "AM": "AMD",
    "AO": "AOA",
    "AR": "ARS",
    "AU": "AUD",
    "AW": "AWG",
    "AZ": "AZN",
    "BA": "BAM",
    "BB": "BBD",
    "BD": "BDT",
    "BH": "BHD",
    "BI": "BIF",
    "BM": "BMD",
    "BN": "BND",
    "BO": "BOB",
    "BR": "BRL",
    "BS": "BSD",
    "BT": "BTN",
    "BW": "BWP",
    "BY": "BYN",
    "BZ": "BZD",
    "CA": "CAD",
    "CC": "AUD",
    "CD": "CDF",
    "CH": "CHF",
    "CK": "NZD",
    "CL": "CLP",
    "CN": "CNY",
    "CO": "COP",
    "CR": "CRC",
    "CU": "CUP",
    "CV": "CVE",
    "CW": "ANG",
    "CX": "AUD",
    "CZ": "CZK",
    "DJ": "DJF",
    "DK": "DKK",
    "DO": "DOP",
    "DZ": "DZD",
    "EG": "EGP",
    "ER": "ERN",
    "ET": "ETB",
    "FJ": "FJD",
    "FK": "FKP",
    "FO": "DKK",
    "GB": "GBP",
    "GE": "GEL",
    "GG": "GBP",
    "GH": "GHS",
    "GI": "GIP",
    "GL": "DKK",
    "GM": "GMD",
    "GN": "GNF",
    "GT": "GTQ",
    "GY": "GYD",
    "HK": "HKD",
    "HN": "HNL",
    "HT": "HTG",
    "HU": "HUF",
    "ID": "IDR",
    "IL": "ILS",
    "IM": "GBP",
    "IN": "INR",
    "IQ": "IQD",
    "IR": "IRR",
    "IS": "ISK",
    "JE": "GBP",
    "JM": "JMD",
    "JO": "JOD",
    "JP": "JPY",
    "KE": "KES",
    "KG": "KGS",
    "KH": "KHR",
    "KI": "AUD",
    "KM": "KMF",
    "KP": "KPW",
    "KR": "KRW",
    "KW": "KWD",
    "KY": "KYD",
    "KZ": "KZT",
    "LA": "LAK",
    "LB": "LBP",
    "LK": "LKR",
    "LR": "LRD",
    "LS": "LSL",
    "LY": "LYD",
    "MA": "MAD",
    "MD": "MDL",
    "MG": "MGA",
    "MK": "MKD",
    "MM": "MMK",
    "MN": "MNT",
    "MO": "MOP",
    "MR": "MRU",
    "MU": "MUR",
    "MV": "MVR",
    "MW": "MWK",
    "MX": "MXN",
    "MY": "MYR",
    "MZ": "MZN",
    "NA": "NAD",
    "NF": "AUD",
    "NG": "NGN",
    "NI": "NIO",
    "NO": "NOK",
    "NP": "NPR",
    "NR": "AUD",
    "NU": "NZD",
    "NZ": "NZD",
    "OM": "OMR",
    "PA": "PAB",
    "PE": "PEN",
    "PG": "PGK",
    "PH": "PHP",
    "PK": "PKR",
    "PL": "PLN",
    "PY": "PYG",
    "QA": "QAR",
    "RO": "RON",
    "RS": "RSD",
    "RU": "RUB",
    "RW": "RWF",
    "SA": "SAR",
    "SB": "SBD",
    "SC": "SCR",
    "SD": "SDG",
    "SE": "SEK",
    "SG": "SGD",
    "SH": "GBP",
    "SL": "SLE",
    "SO": "SOS",
    "SR": "SRD",
    "SS": "SSP",
    "ST": "STN",
    "SX": "ANG",
    "SY": "SYP",
    "SZ": "SZL",
    "TH": "THB",
    "TJ": "TJS",
    "TM": "TMT",
    "TN": "TND",
    "TO": "TOP",
    "TR": "TRY",
    "TT": "TTD",
    "TV": "AUD",
    "TW": "TWD",
    "TZ": "TZS",
    "UA": "UAH",
    "UG": "UGX",
    "UY": "UYU",
    "UZ": "UZS",
    "VE": "VES",
    "VN": "VND",
    "VU": "VUV",
    "WS": "WST",
    "YE": "YER",
    "ZA": "ZAR",
    "ZM": "ZMW",
}


def _country_map() -> dict[str, str]:
    mapping = dict(_SINGLE)
    for code in _EUR:
        mapping[code] = "EUR"
    for code in _USD:
        mapping[code] = "USD"
    for code in _XOF:
        mapping[code] = "XOF"
    for code in _XAF:
        mapping[code] = "XAF"
    for code in _XCD:
        mapping[code] = "XCD"
    for code in _XPF:
        mapping[code] = "XPF"
    return mapping


_COUNTRY_CASH_CURRENCY = _country_map()


def cash_currency_for_country(country: Optional[str]) -> Optional[str]:
    """ISO 4217 cash currency for an ISO 3166-1 alpha-2 country. None if unproven."""
    if not country:
        return None
    text = country.strip().upper()
    if len(text) != 2 or not text.isalpha():
        return None
    return _COUNTRY_CASH_CURRENCY.get(text)


def cash_currency_for_origin(iata: Optional[str]) -> Optional[str]:
    """Cash currency for a named origin IATA, from that airport's owned country."""
    if not iata or not str(iata).strip():
        return None
    airport = get_airport(str(iata).strip())
    if airport is None:
        return None
    return cash_currency_for_country(airport.country)


def first_origin_iata(trip: object) -> Optional[str]:
    origin = getattr(trip, "origin", None)
    if isinstance(origin, str) and origin.strip():
        return origin.strip().upper()
    legs = getattr(trip, "legs", None)
    if legs:
        code = getattr(legs[0], "origin", None)
        if isinstance(code, str) and code.strip():
            return code.strip().upper()
    return None


def resolve_quote_currency(
    explicit: Optional[str],
    origin: Optional[str] = None,
    *,
    missing: str = CURRENCY_REQUIRED,
) -> str:
    """Named ISO 4217, else origin-country cash currency. Never guesses EUR."""
    if explicit is not None and str(explicit).strip():
        return normalize_currency(explicit)
    if explicit is not None:
        raise ValueError(f"invalid currency code: {explicit!r}")
    proven = cash_currency_for_origin(origin)
    if proven:
        return proven
    raise ValueError(missing)


def resolve_quote_and_buffer(
    explicit: Optional[str],
    origin: Optional[str],
    named_buffer: Optional[int],
    *,
    missing: str = CURRENCY_REQUIRED,
) -> tuple[str, int]:
    currency = resolve_quote_currency(explicit, origin, missing=missing)
    return currency, resolve_baggage_buffer(named_buffer, currency)


def resolve_baggage_buffer(named: Optional[int], currency: str) -> int:
    """Ranking add-on in the quote currency. No FX on the EUR 70 default.

    Unnamed is 70 only when the quote is EUR. Otherwise unnamed is 0.
    A named value is used as-is in the same currency Google was asked for.
    """
    if named is not None:
        if named < 0:
            raise ValueError("baggage buffer must not be negative")
        return named
    if currency == "EUR":
        return 70
    return 0
