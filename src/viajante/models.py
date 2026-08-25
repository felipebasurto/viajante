"""Domain types and JSON mapping for flight and hotel reports."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
from statistics import median
from typing import Literal, Mapping, Optional, Sequence, Tuple, Union

from viajante.airports import is_known_iata

# Fetch/browser locale is English so owned card parsers stay on English evidence.
FETCH_LANGUAGE = "en"
FETCH_LOCALE = "en-US"

FlightCabin = Literal["economy", "premium-economy", "business", "first"]
_CABINS: tuple[FlightCabin, ...] = ("economy", "premium-economy", "business", "first")
VsTypical = Literal["below", "near", "above"]
_VS_TYPICAL: tuple[VsTypical, ...] = ("below", "near", "above")
NEAR_TYPICAL_RATIO = 0.10


def vs_typical(price_eur: float, typical_eur: Optional[float]) -> Optional[VsTypical]:
    """Coarse label against an owned typical. None when there is no typical."""
    if typical_eur is None or typical_eur <= 0:
        return None
    if price_eur < typical_eur * (1.0 - NEAR_TYPICAL_RATIO):
        return "below"
    if price_eur > typical_eur * (1.0 + NEAR_TYPICAL_RATIO):
        return "above"
    return "near"


def vs_typical_pct(price_eur: float, typical_eur: Optional[float]) -> Optional[int]:
    """Signed percent of the fare versus an owned typical. None without a typical."""
    if typical_eur is None or typical_eur <= 0:
        return None
    return int(round((price_eur / typical_eur - 1.0) * 100.0))


def _require_typical_triple(
    typical_eur: Optional[float],
    vs: Optional[VsTypical],
    pct: Optional[int],
) -> None:
    have = (typical_eur is None, vs is None, pct is None)
    if len(set(have)) != 1:
        raise ValueError(
            "typical_eur, vs_typical, and vs_typical_pct must all be set or all omitted"
        )
    if typical_eur is not None and typical_eur <= 0:
        raise ValueError("typical_eur must be positive")
    if vs is not None and vs not in _VS_TYPICAL:
        raise ValueError(f"invalid vs_typical: {vs!r}")


def _typical_json(
    typical_eur: Optional[float],
    vs: Optional[VsTypical],
    pct: Optional[int],
) -> dict[str, object]:
    if typical_eur is None or vs is None or pct is None:
        return {}
    return {
        "typical_eur": typical_eur,
        "vs_typical": vs,
        "vs_typical_pct": pct,
        "typical_deal": format_typical_deal(vs, typical_eur, pct),
    }


def format_typical_deal(
    vs: Optional[VsTypical],
    typical_eur: Optional[float],
    pct: Optional[int],
) -> Optional[str]:
    """English one-liner, or None when typical is omitted."""
    if vs is None or typical_eur is None or pct is None:
        return None
    if pct > 0:
        shown = f"+{pct}%"
    elif pct < 0:
        shown = f"−{abs(pct)}%"
    else:
        shown = "0%"
    return f"{vs} typical {typical_eur:.0f} € ({shown})"


def _normalize_iata(code: str, *, role: str) -> str:
    normalized = code.strip().upper()
    if len(normalized) != 3 or not normalized.isalpha():
        raise ValueError(f"invalid {role} IATA code: {code!r}")
    if not is_known_iata(normalized):
        raise ValueError(f"unknown {role} IATA code: {code!r}")
    return normalized


def _require_adults(adults: int) -> None:
    if adults < 1:
        raise ValueError("adults must be at least 1")


def _require_non_negative(value: int, *, role: str) -> None:
    if value < 0:
        raise ValueError(f"{role} must not be negative")


def _require_occupancy(
    *,
    adults: int,
    children: int,
    infants_in_seat: int,
    infants_on_lap: int,
) -> None:
    _require_adults(adults)
    _require_non_negative(children, role="children")
    _require_non_negative(infants_in_seat, role="infants_in_seat")
    _require_non_negative(infants_on_lap, role="infants_on_lap")
    if infants_on_lap > adults:
        raise ValueError("infants_on_lap cannot exceed adults")


def _require_cabin(cabin: FlightCabin) -> None:
    if cabin not in _CABINS:
        raise ValueError(f"invalid cabin: {cabin!r}")


def _require_bag_count(value: Optional[int], *, role: str) -> None:
    if value is None:
        return
    if value < 0:
        raise ValueError(f"{role} must not be negative")


def _require_price_cap(value: Optional[int]) -> None:
    if value is None:
        return
    if value <= 0:
        raise ValueError("price_cap_eur must be positive")


def _optional_bag_fields(bags: Optional[int], carry_on: Optional[int]) -> dict[str, int]:
    payload: dict[str, int] = {}
    if bags is not None:
        payload["bags"] = bags
    if carry_on is not None:
        payload["carry_on"] = carry_on
    return payload


def _optional_price_cap_fields(price_cap_eur: Optional[int]) -> dict[str, int]:
    if price_cap_eur is None:
        return {}
    return {"price_cap_eur": price_cap_eur}


def _optional_occupancy_fields(
    children: int,
    infants_in_seat: int,
    infants_on_lap: int,
) -> dict[str, int]:
    payload: dict[str, int] = {}
    if children:
        payload["children"] = children
    if infants_in_seat:
        payload["infants_in_seat"] = infants_in_seat
    if infants_on_lap:
        payload["infants_on_lap"] = infants_on_lap
    return payload


def normalize_currency(value: str) -> str:
    text = value.strip().upper()
    if len(text) != 3 or not text.isalpha():
        raise ValueError(f"invalid currency code: {value!r}")
    return text


def normalize_country(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip().upper()
    if not text:
        return None
    if len(text) != 2 or not text.isalpha():
        raise ValueError(f"invalid country code: {value!r}")
    return text


_ALLIANCES = frozenset({"oneworld", "skyteam", "star"})


def _require_airline_codes(codes: Optional[Tuple[str, ...]], *, role: str) -> None:
    if codes is None:
        return
    for code in codes:
        if not (2 <= len(code) <= 3 and str(code).isalnum()):
            raise ValueError(f"invalid {role} code: {code!r}")


def _require_alliances(names: Optional[Tuple[str, ...]], *, role: str) -> None:
    if names is None:
        return
    for name in names:
        if name not in _ALLIANCES:
            raise ValueError(f"invalid {role}: {name!r}")


def _optional_carrier_fields(
    airlines: Optional[Tuple[str, ...]],
    exclude_airlines: Optional[Tuple[str, ...]],
    alliances: Optional[Tuple[str, ...]],
    exclude_alliances: Optional[Tuple[str, ...]],
) -> dict[str, list[str]]:
    payload: dict[str, list[str]] = {}
    if airlines:
        payload["airlines"] = list(airlines)
    if exclude_airlines:
        payload["exclude_airlines"] = list(exclude_airlines)
    if alliances:
        payload["alliances"] = list(alliances)
    if exclude_alliances:
        payload["exclude_alliances"] = list(exclude_alliances)
    return payload


@dataclass(frozen=True)
class FlightLeg:
    origin: str
    destination: str
    departure_date: date
    max_stops: int = 1

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if origin == destination:
            raise ValueError("origin and destination must differ")
        if self.max_stops not in (0, 1, 2):
            raise ValueError("max_stops must be 0, 1, or 2")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)


@dataclass(frozen=True)
class FlightQuery:
    origin: str
    destination: str
    departure_date: date
    max_stops: int = 1
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0
    cabin: FlightCabin = "economy"
    bags: Optional[int] = None
    carry_on: Optional[int] = None
    price_cap_eur: Optional[int] = None
    airlines: Optional[Tuple[str, ...]] = None
    exclude_airlines: Optional[Tuple[str, ...]] = None
    alliances: Optional[Tuple[str, ...]] = None
    exclude_alliances: Optional[Tuple[str, ...]] = None
    nearby_label: Optional[str] = None

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if self.max_stops not in (0, 1, 2):
            raise ValueError("max_stops must be 0, 1, or 2")
        _require_occupancy(
            adults=self.adults,
            children=self.children,
            infants_in_seat=self.infants_in_seat,
            infants_on_lap=self.infants_on_lap,
        )
        _require_cabin(self.cabin)
        _require_bag_count(self.bags, role="bags")
        _require_bag_count(self.carry_on, role="carry_on")
        _require_price_cap(self.price_cap_eur)
        _require_airline_codes(self.airlines, role="airlines")
        _require_airline_codes(self.exclude_airlines, role="exclude_airlines")
        _require_alliances(self.alliances, role="alliances")
        _require_alliances(self.exclude_alliances, role="exclude_alliances")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        label = self.nearby_label.strip() if self.nearby_label else None
        object.__setattr__(self, "nearby_label", label or None)

    @property
    def legs(self) -> Tuple[FlightLeg, ...]:
        return (
            FlightLeg(
                self.origin,
                self.destination,
                self.departure_date,
                self.max_stops,
            ),
        )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "trip": "one-way",
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "max_stops": self.max_stops,
            "adults": self.adults,
            "cabin": self.cabin,
        }
        payload.update(
            _optional_occupancy_fields(self.children, self.infants_in_seat, self.infants_on_lap)
        )
        payload.update(_optional_bag_fields(self.bags, self.carry_on))
        payload.update(_optional_price_cap_fields(self.price_cap_eur))
        payload.update(
            _optional_carrier_fields(
                self.airlines,
                self.exclude_airlines,
                self.alliances,
                self.exclude_alliances,
            )
        )
        return payload


@dataclass(frozen=True)
class RoundTrip:
    origin: str
    destination: str
    departure_date: date
    return_date: date
    max_stops: int = 1
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0
    cabin: FlightCabin = "economy"
    bags: Optional[int] = None
    carry_on: Optional[int] = None
    price_cap_eur: Optional[int] = None
    airlines: Optional[Tuple[str, ...]] = None
    exclude_airlines: Optional[Tuple[str, ...]] = None
    alliances: Optional[Tuple[str, ...]] = None
    exclude_alliances: Optional[Tuple[str, ...]] = None
    nearby_label: Optional[str] = None

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if origin == destination:
            raise ValueError("origin and destination must differ")
        if self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        if self.max_stops not in (0, 1, 2):
            raise ValueError("max_stops must be 0, 1, or 2")
        _require_occupancy(
            adults=self.adults,
            children=self.children,
            infants_in_seat=self.infants_in_seat,
            infants_on_lap=self.infants_on_lap,
        )
        _require_cabin(self.cabin)
        _require_bag_count(self.bags, role="bags")
        _require_bag_count(self.carry_on, role="carry_on")
        _require_price_cap(self.price_cap_eur)
        _require_airline_codes(self.airlines, role="airlines")
        _require_airline_codes(self.exclude_airlines, role="exclude_airlines")
        _require_alliances(self.alliances, role="alliances")
        _require_alliances(self.exclude_alliances, role="exclude_alliances")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        label = self.nearby_label.strip() if self.nearby_label else None
        object.__setattr__(self, "nearby_label", label or None)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "trip": "rt",
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "return_date": self.return_date.isoformat(),
            "max_stops": self.max_stops,
            "adults": self.adults,
            "cabin": self.cabin,
        }
        payload.update(
            _optional_occupancy_fields(self.children, self.infants_in_seat, self.infants_on_lap)
        )
        payload.update(_optional_bag_fields(self.bags, self.carry_on))
        payload.update(_optional_price_cap_fields(self.price_cap_eur))
        payload.update(
            _optional_carrier_fields(
                self.airlines,
                self.exclude_airlines,
                self.alliances,
                self.exclude_alliances,
            )
        )
        return payload

    @property
    def legs(self) -> Tuple[FlightLeg, FlightLeg]:
        return (
            FlightLeg(self.origin, self.destination, self.departure_date, self.max_stops),
            FlightLeg(self.destination, self.origin, self.return_date, self.max_stops),
        )


@dataclass(frozen=True)
class MultiCity:
    legs: Tuple[FlightLeg, ...]
    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0
    cabin: FlightCabin = "economy"
    bags: Optional[int] = None
    carry_on: Optional[int] = None
    price_cap_eur: Optional[int] = None
    airlines: Optional[Tuple[str, ...]] = None
    exclude_airlines: Optional[Tuple[str, ...]] = None
    alliances: Optional[Tuple[str, ...]] = None
    exclude_alliances: Optional[Tuple[str, ...]] = None

    def __post_init__(self) -> None:
        if len(self.legs) < 2:
            raise ValueError("multi-city needs at least two legs")
        dates = [leg.departure_date for leg in self.legs]
        if dates != sorted(dates):
            raise ValueError("multi-city dates must be non-decreasing")
        _require_occupancy(
            adults=self.adults,
            children=self.children,
            infants_in_seat=self.infants_in_seat,
            infants_on_lap=self.infants_on_lap,
        )
        _require_cabin(self.cabin)
        _require_bag_count(self.bags, role="bags")
        _require_bag_count(self.carry_on, role="carry_on")
        _require_price_cap(self.price_cap_eur)
        _require_airline_codes(self.airlines, role="airlines")
        _require_airline_codes(self.exclude_airlines, role="exclude_airlines")
        _require_alliances(self.alliances, role="alliances")
        _require_alliances(self.exclude_alliances, role="exclude_alliances")

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "trip": "multi",
            "origin": self.legs[0].origin,
            "destination": self.legs[-1].destination,
            "departure_date": self.legs[0].departure_date.isoformat(),
            "max_stops": max(leg.max_stops for leg in self.legs),
            "adults": self.adults,
            "cabin": self.cabin,
            "legs": [
                {
                    "origin": leg.origin,
                    "destination": leg.destination,
                    "departure_date": leg.departure_date.isoformat(),
                    "max_stops": leg.max_stops,
                }
                for leg in self.legs
            ],
        }
        payload.update(
            _optional_occupancy_fields(self.children, self.infants_in_seat, self.infants_on_lap)
        )
        payload.update(_optional_bag_fields(self.bags, self.carry_on))
        payload.update(_optional_price_cap_fields(self.price_cap_eur))
        payload.update(
            _optional_carrier_fields(
                self.airlines,
                self.exclude_airlines,
                self.alliances,
                self.exclude_alliances,
            )
        )
        return payload


Trip = Union[FlightQuery, RoundTrip, MultiCity]


@dataclass(frozen=True)
class RawSegment:
    origin: Optional[str] = None
    destination: Optional[str] = None
    departure: Optional[str] = None
    arrival: Optional[str] = None
    airline: Optional[str] = None
    flight_number: Optional[str] = None

    def to_dict(self) -> Mapping[str, object]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "departure": self.departure,
            "arrival": self.arrival,
            "airline": self.airline,
            "flight_number": self.flight_number,
        }


@dataclass(frozen=True)
class RawLayover:
    city: Optional[str] = None
    hours: Optional[float] = None

    def to_dict(self) -> Mapping[str, object]:
        return {"city": self.city, "hours": self.hours}


@dataclass(frozen=True)
class RawJourneyLeg:
    departure: Optional[str]
    arrival: Optional[str]
    duration: Optional[str] = None
    stops: Optional[str] = None
    segments: Tuple[RawSegment, ...] = ()
    layovers: Tuple[RawLayover, ...] = ()

    def to_dict(self) -> Mapping[str, object]:
        return {
            "departure": self.departure,
            "arrival": self.arrival,
            "duration": self.duration,
            "stops": self.stops,
            "segments": [segment.to_dict() for segment in self.segments],
            "layovers": [layover.to_dict() for layover in self.layovers],
        }


@dataclass(frozen=True)
class FlightOffer:
    airline: Optional[str]
    departure: Optional[str]
    arrival: Optional[str]
    price: str
    price_eur: float
    duration: Optional[str]
    duration_hours: Optional[float]
    stops: Optional[str]
    stops_count: Optional[int]
    baggage_buffer_eur: int
    needs_bag_verify: bool
    layover_city: Optional[str] = None
    layover_hours: Optional[float] = None
    flight_numbers: Optional[Tuple[str, ...]] = None
    booking_token: Optional[str] = None
    google_flights_url: Optional[str] = None
    legs: Tuple[RawJourneyLeg, ...] = ()
    typical_eur: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None
    cheapest_date: Optional[date] = None
    cheapest_eur: Optional[float] = None
    checked_bags: Optional[int] = None
    carry_on: Optional[int] = None

    def __post_init__(self) -> None:
        if self.price_eur <= 0:
            raise ValueError("price_eur must be positive")
        if self.baggage_buffer_eur < 0:
            raise ValueError("baggage_buffer_eur must not be negative")
        if self.baggage_buffer_eur > 0 and not self.needs_bag_verify:
            raise ValueError("a baggage buffer only applies to a carrier flagged for verification")
        have_typical = (
            self.typical_eur is None,
            self.vs_typical is None,
            self.vs_typical_pct is None,
        )
        if len(set(have_typical)) != 1:
            raise ValueError(
                "typical_eur, vs_typical, and vs_typical_pct must all be set or all omitted"
            )
        if self.typical_eur is not None and self.typical_eur <= 0:
            raise ValueError("typical_eur must be positive")
        if self.vs_typical is not None and self.vs_typical not in _VS_TYPICAL:
            raise ValueError(f"invalid vs_typical: {self.vs_typical!r}")
        if (self.cheapest_date is None) != (self.cheapest_eur is None):
            raise ValueError("cheapest_date and cheapest_eur must both be set or both omitted")
        if self.cheapest_eur is not None and self.cheapest_eur <= 0:
            raise ValueError("cheapest_eur must be positive")
        if self.cheapest_date is not None and self.typical_eur is None:
            raise ValueError("cheapest day is omitted unless typical_eur is set")
        _require_bag_count(self.checked_bags, role="checked_bags")
        _require_bag_count(self.carry_on, role="carry_on")
        if not self.legs:
            layovers: Tuple[RawLayover, ...] = ()
            if self.layover_city is not None or self.layover_hours is not None:
                layovers = (RawLayover(city=self.layover_city, hours=self.layover_hours),)
            object.__setattr__(
                self,
                "legs",
                (
                    RawJourneyLeg(
                        departure=self.departure,
                        arrival=self.arrival,
                        duration=self.duration,
                        stops=self.stops,
                        layovers=layovers,
                    ),
                ),
            )

    def typical_deal(self) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical_eur, self.vs_typical_pct)

    def to_dict(self) -> Mapping[str, object]:
        lead = self.legs[0]
        two_stop = self.stops_count is not None and self.stops_count >= 2
        payload: dict[str, object] = {
            "airline": self.airline,
            "departure": lead.departure,
            "arrival": lead.arrival,
            "price": self.price,
            "price_eur": self.price_eur,
            "typical_eur": self.typical_eur,
            "vs_typical": self.vs_typical,
            "vs_typical_pct": self.vs_typical_pct,
            "typical_deal": self.typical_deal(),
            "duration": self.duration,
            "duration_hours": self.duration_hours,
            "stops": self.stops,
            "stops_count": self.stops_count,
            "layover_city": None if two_stop else self.layover_city,
            "layover_hours": None if two_stop else self.layover_hours,
            "flight_numbers": list(self.flight_numbers) if self.flight_numbers else None,
            "booking_token": self.booking_token,
            "baggage_buffer_eur": self.baggage_buffer_eur,
            "needs_bag_verify": self.needs_bag_verify,
            "legs": [leg.to_dict() for leg in self.legs],
        }
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.checked_bags is not None:
            payload["checked_bags"] = self.checked_bags
        if self.carry_on is not None:
            payload["carry_on"] = self.carry_on
        if self.cheapest_date is not None and self.cheapest_eur is not None:
            payload["cheapest_date"] = self.cheapest_date.isoformat()
            payload["cheapest_eur"] = self.cheapest_eur
        return payload


@dataclass(frozen=True)
class StopsCompareSide:
    """Cheapest parsed offer in one stop bucket. Cabin fare only; never invented."""

    price: str
    price_eur: float
    duration: Optional[str]
    duration_hours: Optional[float]
    airline: Optional[str]
    stops: Optional[str]
    stops_count: int
    departure: Optional[str] = None
    arrival: Optional[str] = None
    layover_city: Optional[str] = None
    layover_hours: Optional[float] = None

    def __post_init__(self) -> None:
        if self.price_eur <= 0:
            raise ValueError("price_eur must be positive")
        if self.stops_count not in (0, 1):
            raise ValueError("stops_count must be 0 or 1")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "airline": self.airline,
            "departure": self.departure,
            "arrival": self.arrival,
            "price": self.price,
            "price_eur": self.price_eur,
            "duration": self.duration,
            "duration_hours": self.duration_hours,
            "stops": self.stops,
            "stops_count": self.stops_count,
            "layover_city": self.layover_city,
            "layover_hours": self.layover_hours,
        }

    @classmethod
    def from_offer(cls, offer: FlightOffer) -> "StopsCompareSide":
        if offer.stops_count not in (0, 1):
            raise ValueError("stops compare side is only nonstop or 1-stop")
        return cls(
            price=offer.price,
            price_eur=offer.price_eur,
            duration=offer.duration,
            duration_hours=offer.duration_hours,
            airline=offer.airline,
            stops=offer.stops,
            stops_count=offer.stops_count,
            departure=offer.departure,
            arrival=offer.arrival,
            layover_city=offer.layover_city,
            layover_hours=offer.layover_hours,
        )


@dataclass(frozen=True)
class StopsCompare:
    """Cheapest nonstop vs cheapest 1-stop from one parsed offer set."""

    nonstop: Optional[StopsCompareSide] = None
    one_stop: Optional[StopsCompareSide] = None

    def __post_init__(self) -> None:
        if self.nonstop is None and self.one_stop is None:
            raise ValueError("stops_compare needs a nonstop or 1-stop side")
        if self.nonstop is not None and self.nonstop.stops_count != 0:
            raise ValueError("nonstop side must have stops_count 0")
        if self.one_stop is not None and self.one_stop.stops_count != 1:
            raise ValueError("one_stop side must have stops_count 1")

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {}
        if self.nonstop is not None:
            payload["nonstop"] = self.nonstop.to_dict()
        if self.one_stop is not None:
            payload["one_stop"] = self.one_stop.to_dict()
        return payload


class SearchErrorCode(str, Enum):
    NO_RESULTS = "no_results"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    MARKUP_DRIFT = "markup_drift"
    FETCH_FAILED = "fetch_failed"
    BROWSER_UNAVAILABLE = "browser_unavailable"


@dataclass(frozen=True)
class SearchError:
    code: SearchErrorCode
    message: str

    def to_dict(self) -> Mapping[str, str]:
        return {"code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class QuerySuccess:
    query: Trip
    raw_count: int
    eligible_count: int
    offers: Tuple[FlightOffer, ...]
    google_flights_url: Optional[str] = None
    stops_compare: Optional[StopsCompare] = None
    status: Literal["ok"] = field(init=False, default="ok")

    def __post_init__(self) -> None:
        if self.raw_count < self.eligible_count:
            raise ValueError("raw_count must be >= eligible_count")
        if self.eligible_count < len(self.offers):
            raise ValueError("eligible_count must be >= number of offers")

    def to_dict(self) -> Mapping[str, object]:
        query = dict(self.query.to_dict())
        if self.google_flights_url:
            query["google_flights_url"] = self.google_flights_url
        payload: dict[str, object] = {
            "status": self.status,
            "query": query,
            "raw_count": self.raw_count,
            "eligible_count": self.eligible_count,
            "offers": [offer.to_dict() for offer in self.offers],
        }
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        return payload


@dataclass(frozen=True)
class QueryFailure:
    query: Trip
    error: SearchError
    google_flights_url: Optional[str] = None
    status: Literal["error"] = field(init=False, default="error")

    def to_dict(self) -> Mapping[str, object]:
        query = dict(self.query.to_dict())
        if self.google_flights_url:
            query["google_flights_url"] = self.google_flights_url
        return {
            "status": self.status,
            "query": query,
            "error": self.error.to_dict(),
        }


QueryResult = Union[QuerySuccess, QueryFailure]

FetchBackend = Literal["sweep", "detail", "sweep_then_detail"]


@dataclass(frozen=True)
class SearchReport:
    searched_at: datetime
    queries: Tuple[QueryResult, ...]
    locale: str = "en"
    currency: str = "EUR"
    fetch_backend: Optional[FetchBackend] = None
    fetch_ms: Optional[int] = None
    schema_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )

    def to_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "queries": [result.to_dict() for result in self.queries],
        }


DateTripKind = Literal["one-way", "rt"]

# Same floor as typical.MIN_DAILY_PRICES. Tests pin the two together.
MIN_PRICED_DAYS_FOR_SUMMARY = 3


@dataclass(frozen=True)
class DateCalendarSummary:
    """Min / median / max over owned priced days. Absent when the grid is thin."""

    min_eur: float
    median_eur: float
    max_eur: float
    cheapest_date: date
    n_priced: int

    def to_dict(self) -> Mapping[str, object]:
        return {
            "min_eur": self.min_eur,
            "median_eur": self.median_eur,
            "max_eur": self.max_eur,
            "cheapest_date": self.cheapest_date.isoformat(),
            "n_priced": self.n_priced,
        }


def owned_calendar_summary(
    pairs: Sequence[tuple[date, Optional[float]]],
) -> Optional[DateCalendarSummary]:
    """Stats from owned daily prices only. None if fewer than three priced days."""
    priced = [(day, float(price)) for day, price in pairs if price is not None and price > 0]
    if len(priced) < MIN_PRICED_DAYS_FOR_SUMMARY:
        return None
    prices = [price for _day, price in priced]
    cheapest_date, min_eur = min(priced, key=lambda item: (item[1], item[0]))
    return DateCalendarSummary(
        min_eur=min_eur,
        median_eur=float(median(prices)),
        max_eur=max(prices),
        cheapest_date=cheapest_date,
        n_priced=len(priced),
    )


def _stamp_date_row_typical(
    row: "DatePriceRow",
    typical_eur: Optional[float],
) -> "DatePriceRow":
    """Stamp or omit the owned window median. Empty/error rows stay omitted."""
    if row.status != "ok" or row.price_eur is None or row.price_eur <= 0:
        if row.typical_eur is None:
            return row
        return replace(row, typical_eur=None, vs_typical=None, vs_typical_pct=None)
    label = vs_typical(row.price_eur, typical_eur)
    pct = vs_typical_pct(row.price_eur, typical_eur)
    if typical_eur is None or label is None or pct is None:
        if row.typical_eur is None:
            return row
        return replace(row, typical_eur=None, vs_typical=None, vs_typical_pct=None)
    if row.typical_eur == typical_eur and row.vs_typical == label and row.vs_typical_pct == pct:
        return row
    return replace(row, typical_eur=typical_eur, vs_typical=label, vs_typical_pct=pct)


@dataclass(frozen=True)
class DatePriceRow:
    departure_date: date
    price_eur: Optional[float] = None
    airline: Optional[str] = None
    stops_count: Optional[int] = None
    return_date: Optional[date] = None
    status: Literal["ok", "empty", "error"] = "ok"
    error: Optional[SearchError] = None
    stops_compare: Optional[StopsCompare] = None
    google_flights_url: Optional[str] = None
    typical_eur: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None

    def __post_init__(self) -> None:
        _require_typical_triple(self.typical_eur, self.vs_typical, self.vs_typical_pct)
        if (self.status != "ok" or self.price_eur is None) and self.typical_eur is not None:
            raise ValueError("empty/error rows omit typical")

    def typical_deal(self) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical_eur, self.vs_typical_pct)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "date": self.departure_date.isoformat(),
            "price_eur": self.price_eur,
            "airline": self.airline,
            "stops_count": self.stops_count,
            "status": self.status,
        }
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        payload.update(_typical_json(self.typical_eur, self.vs_typical, self.vs_typical_pct))
        return payload


@dataclass(frozen=True)
class DateCalendarReport:
    searched_at: datetime
    origin: str
    destination: str
    start_date: date
    end_date: date
    days: Tuple[DatePriceRow, ...]
    locale: str = "en"
    currency: str = "EUR"
    trip: DateTripKind = "one-way"
    nights: Optional[int] = None
    fetch_backend: Optional[str] = "calendar"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    nearby_label: Optional[str] = None
    summary: Optional[DateCalendarSummary] = field(init=False, default=None)
    schema_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )
        label = self.nearby_label.strip() if self.nearby_label else None
        object.__setattr__(self, "nearby_label", label or None)
        object.__setattr__(
            self,
            "summary",
            owned_calendar_summary([(row.departure_date, row.price_eur) for row in self.days]),
        )
        typical = None if self.summary is None else self.summary.median_eur
        object.__setattr__(
            self,
            "days",
            tuple(_stamp_date_row_typical(row, typical) for row in self.days),
        )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "origin": self.origin,
            "destination": self.destination,
            "from": self.start_date.isoformat(),
            "to": self.end_date.isoformat(),
            "trip": self.trip,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "days": [row.to_dict() for row in self.days],
        }
        if self.nights is not None:
            payload["nights"] = self.nights
        if self.summary is not None:
            payload["summary"] = self.summary.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        return payload


FlexFetchBackend = Literal["calendar", "calendar_then_sweep"]


@dataclass(frozen=True)
class FlexSearchReport:
    """Calendar window pick plus at most one shopping search. Never invents a fare."""

    searched_at: datetime
    origin: str
    destination: str
    around: date
    flex_days: int
    start_date: date
    end_date: date
    days: Tuple[DatePriceRow, ...]
    chosen_date: Optional[date] = None
    return_date: Optional[date] = None
    offers: Tuple[FlightOffer, ...] = ()
    stops_compare: Optional[StopsCompare] = None
    typical_eur: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    locale: str = "en"
    currency: str = "EUR"
    trip: DateTripKind = "one-way"
    nights: Optional[int] = None
    fetch_backend: Optional[FlexFetchBackend] = "calendar"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    error: Optional[SearchError] = None
    nearby_label: Optional[str] = None
    schema_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.flex_days < 1:
            raise ValueError("flex_days must be at least 1")
        if self.typical_eur is not None and self.typical_eur <= 0:
            raise ValueError("typical_eur must be positive")
        if self.vs_typical is not None and self.vs_typical not in _VS_TYPICAL:
            raise ValueError(f"invalid vs_typical: {self.vs_typical!r}")
        if self.vs_typical is not None and self.typical_eur is None:
            raise ValueError("vs_typical requires typical_eur")
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )
        label = self.nearby_label.strip() if self.nearby_label else None
        object.__setattr__(self, "nearby_label", label or None)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "origin": self.origin,
            "destination": self.destination,
            "around": self.around.isoformat(),
            "flex_days": self.flex_days,
            "from": self.start_date.isoformat(),
            "to": self.end_date.isoformat(),
            "trip": self.trip,
            "chosen_date": self.chosen_date.isoformat() if self.chosen_date else None,
            "typical_eur": self.typical_eur,
            "vs_typical": self.vs_typical,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "days": [row.to_dict() for row in self.days],
            "offers": [offer.to_dict() for offer in self.offers],
        }
        if self.nights is not None:
            payload["nights"] = self.nights
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


@dataclass(frozen=True)
class ExploreDestination:
    iata: str
    city: str
    country: Optional[str]
    price_eur: Optional[float] = None
    stops_compare: Optional[StopsCompare] = None
    google_flights_url: Optional[str] = None
    typical_eur: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None

    def __post_init__(self) -> None:
        _require_typical_triple(self.typical_eur, self.vs_typical, self.vs_typical_pct)
        if self.price_eur is None and self.typical_eur is not None:
            raise ValueError("typical requires an owned dest fare")

    def typical_deal(self) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical_eur, self.vs_typical_pct)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "iata": self.iata,
            "city": self.city,
            "country": self.country,
            "price_eur": self.price_eur,
        }
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        payload.update(_typical_json(self.typical_eur, self.vs_typical, self.vs_typical_pct))
        return payload


@dataclass(frozen=True)
class ExploreReport:
    searched_at: datetime
    origin: str
    start_date: date
    days: int
    destinations: Tuple[ExploreDestination, ...]
    locale: str = "en"
    currency: str = "EUR"
    fetch_backend: Optional[str] = "explore"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    error: Optional[SearchError] = None
    nearby_label: Optional[str] = None
    schema_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )
        label = self.nearby_label.strip() if self.nearby_label else None
        object.__setattr__(self, "nearby_label", label or None)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "origin": self.origin,
            "from": self.start_date.isoformat(),
            "days": self.days,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "destinations": [row.to_dict() for row in self.destinations],
        }
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


class CancellationEvidence(str, Enum):
    FREE = "free"
    NON_REFUNDABLE = "non_refundable"
    UNKNOWN = "unknown"


class PropertyTypeEvidence(str, Enum):
    ENTIRE_HOME = "entire_home"
    NOT_ENTIRE_HOME = "not_entire_home"
    UNKNOWN = "unknown"


class LodgingKind(str, Enum):
    ENTIRE_HOME = "entire_home"
    PRIVATE_ROOM = "private_room"
    HOTEL = "hotel"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HotelQuery:
    location: str
    check_in: date
    check_out: date
    adults: int = 2
    rooms: int = 1
    min_rating: Optional[float] = None
    entire_home: bool = False
    free_cancellation: bool = True

    def __post_init__(self) -> None:
        location = " ".join(self.location.split())
        if not location:
            raise ValueError("location must not be blank")
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if self.adults <= 0:
            raise ValueError("adults must be positive")
        if self.rooms <= 0:
            raise ValueError("rooms must be positive")
        if self.min_rating is not None and not 0.0 <= self.min_rating <= 10.0:
            raise ValueError("min_rating must be between 0.0 and 10.0")
        object.__setattr__(self, "location", location)

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    def to_dict(self) -> Mapping[str, object]:
        return {
            "location": self.location,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "adults": self.adults,
            "rooms": self.rooms,
            "min_rating": self.min_rating,
            "entire_home": self.entire_home,
            "free_cancellation": self.free_cancellation,
            "nights": self.nights,
        }


@dataclass(frozen=True)
class RawHotelCard:
    title: str
    address: Optional[str]
    total_price: str
    rating: Optional[str]
    details: str
    link: Optional[str]


@dataclass(frozen=True)
class HotelPage:
    cards: Tuple[RawHotelCard, ...]


HotelProvider = Literal["booking.com", "google-hotels"]


@dataclass(frozen=True)
class AppliedHotelFilters:
    chips: Tuple[str, ...]
    url: str

    def to_dict(self) -> Mapping[str, object]:
        return {
            "chips": list(self.chips),
            "url": self.url,
        }


@dataclass(frozen=True)
class HotelOffer:
    title: str
    address: Optional[str]
    total_price: str
    total_price_eur: float
    rating: Optional[str]
    rating_score: Optional[float]
    details: str
    cancellation_evidence: CancellationEvidence
    property_type_evidence: PropertyTypeEvidence
    lodging_kind: LodgingKind
    bedrooms: Optional[int]
    bathrooms: Optional[int]
    beds: Optional[int]
    link: Optional[str]

    def __post_init__(self) -> None:
        title = self.title.strip()
        if not title:
            raise ValueError("title must not be blank")
        if self.total_price_eur <= 0:
            raise ValueError("total_price_eur must be positive")
        if self.rating_score is not None and not 0.0 <= self.rating_score <= 10.0:
            raise ValueError("rating_score must be between 0.0 and 10.0")
        object.__setattr__(self, "title", title)

    def to_dict(self) -> Mapping[str, object]:
        return {
            "title": self.title,
            "address": self.address,
            "total_price": self.total_price,
            "total_price_eur": self.total_price_eur,
            "rating": self.rating,
            "rating_score": self.rating_score,
            "details": self.details,
            "cancellation_evidence": self.cancellation_evidence.value,
            "property_type_evidence": self.property_type_evidence.value,
            "lodging_kind": self.lodging_kind.value,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "beds": self.beds,
            "link": self.link,
        }


@dataclass(frozen=True)
class HotelQuerySuccess:
    query: HotelQuery
    applied: AppliedHotelFilters
    raw_count: int
    eligible_count: int
    offers: Tuple[HotelOffer, ...]
    status: Literal["ok"] = field(init=False, default="ok")

    def __post_init__(self) -> None:
        if self.raw_count < self.eligible_count:
            raise ValueError("raw_count must be >= eligible_count")
        if self.eligible_count < len(self.offers):
            raise ValueError("eligible_count must be >= number of offers")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "query": self.query.to_dict(),
            "applied": self.applied.to_dict(),
            "raw_count": self.raw_count,
            "eligible_count": self.eligible_count,
            "offers": [offer.to_dict() for offer in self.offers],
        }


@dataclass(frozen=True)
class HotelQueryFailure:
    query: HotelQuery
    applied: AppliedHotelFilters
    error: SearchError
    status: Literal["error"] = field(init=False, default="error")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "query": self.query.to_dict(),
            "applied": self.applied.to_dict(),
            "error": self.error.to_dict(),
        }


HotelQueryResult = Union[HotelQuerySuccess, HotelQueryFailure]


HotelFetchBackend = Literal["booking", "google"]


@dataclass(frozen=True)
class HotelSearchReport:
    searched_at: datetime
    queries: Tuple[HotelQueryResult, ...]
    locale: str = FETCH_LANGUAGE
    currency: str = "EUR"
    schema_version: int = field(init=False, default=1)
    provider: HotelProvider = "booking.com"
    price_basis: Literal["total_stay"] = field(init=False, default="total_stay")
    fetch_backend: Optional[HotelFetchBackend] = None
    fetch_ms: Optional[int] = None

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )

    def to_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "price_basis": self.price_basis,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "queries": [result.to_dict() for result in self.queries],
        }


@dataclass(frozen=True)
class TripTotal:
    """Owned flight fare plus owned hotel stay. Omitted unless both sides hit."""

    flight_fare_eur: float
    hotel_stay_eur: float
    total_eur: float
    nights: int
    hotel_price_basis: Literal["total_stay"] = field(init=False, default="total_stay")

    def __post_init__(self) -> None:
        if self.flight_fare_eur <= 0:
            raise ValueError("flight_fare_eur must be a positive owned fare")
        if self.hotel_stay_eur <= 0:
            raise ValueError("hotel_stay_eur must be a positive owned stay total")
        if abs(self.total_eur - (self.flight_fare_eur + self.hotel_stay_eur)) > 1e-9:
            raise ValueError("total_eur must equal flight_fare_eur + hotel_stay_eur")
        if self.nights < 1:
            raise ValueError("nights must be at least 1")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "flight_fare_eur": self.flight_fare_eur,
            "hotel_stay_eur": self.hotel_stay_eur,
            "total_eur": self.total_eur,
            "hotel_price_basis": self.hotel_price_basis,
            "nights": self.nights,
        }


@dataclass(frozen=True)
class TripSearchReport:
    """Nested owned flight and hotel reports, plus an optional trip total."""

    searched_at: datetime
    flights: SearchReport
    hotels: HotelSearchReport
    trip_total: Optional[TripTotal] = None
    locale: str = FETCH_LANGUAGE
    currency: str = "EUR"
    fetch_ms: Optional[int] = None
    schema_version: int = field(init=False, default=1)

    def __post_init__(self) -> None:
        if self.searched_at.tzinfo is not None:
            object.__setattr__(
                self,
                "searched_at",
                self.searched_at.astimezone(timezone.utc).replace(tzinfo=None),
            )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": self.searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": self.currency,
            "locale": self.locale,
            "fetch_ms": self.fetch_ms,
            "flights": dict(self.flights.to_dict()),
            "hotels": dict(self.hotels.to_dict()),
        }
        if self.trip_total is not None:
            payload["trip_total"] = dict(self.trip_total.to_dict())
        return payload
