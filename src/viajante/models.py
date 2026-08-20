"""Domain types and JSON mapping for flight and hotel reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Literal, Mapping, Optional, Tuple, Union

from viajante.airports import is_known_iata

FlightCabin = Literal["economy", "premium-economy", "business", "first"]
_CABINS: tuple[FlightCabin, ...] = ("economy", "premium-economy", "business", "first")
VsTypical = Literal["below", "near", "above"]
_VS_TYPICAL: tuple[VsTypical, ...] = ("below", "near", "above")


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


def _require_cabin(cabin: FlightCabin) -> None:
    if cabin not in _CABINS:
        raise ValueError(f"invalid cabin: {cabin!r}")


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
    cabin: FlightCabin = "economy"

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if self.max_stops not in (0, 1, 2):
            raise ValueError("max_stops must be 0, 1, or 2")
        _require_adults(self.adults)
        _require_cabin(self.cabin)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)

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
        return {
            "trip": "one-way",
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "max_stops": self.max_stops,
            "adults": self.adults,
            "cabin": self.cabin,
        }


@dataclass(frozen=True)
class RoundTrip:
    origin: str
    destination: str
    departure_date: date
    return_date: date
    max_stops: int = 1
    adults: int = 1
    cabin: FlightCabin = "economy"

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if origin == destination:
            raise ValueError("origin and destination must differ")
        if self.return_date <= self.departure_date:
            raise ValueError("return_date must be after departure_date")
        if self.max_stops not in (0, 1, 2):
            raise ValueError("max_stops must be 0, 1, or 2")
        _require_adults(self.adults)
        _require_cabin(self.cabin)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)

    def to_dict(self) -> Mapping[str, object]:
        return {
            "trip": "rt",
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "return_date": self.return_date.isoformat(),
            "max_stops": self.max_stops,
            "adults": self.adults,
            "cabin": self.cabin,
        }

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
    cabin: FlightCabin = "economy"

    def __post_init__(self) -> None:
        if len(self.legs) < 2:
            raise ValueError("multi-city needs at least two legs")
        dates = [leg.departure_date for leg in self.legs]
        if dates != sorted(dates):
            raise ValueError("multi-city dates must be non-decreasing")
        _require_adults(self.adults)
        _require_cabin(self.cabin)

    def to_dict(self) -> Mapping[str, object]:
        return {
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
    legs: Tuple[RawJourneyLeg, ...] = ()
    typical_eur: Optional[float] = None
    vs_typical: Optional[VsTypical] = None

    def __post_init__(self) -> None:
        if self.price_eur <= 0:
            raise ValueError("price_eur must be positive")
        if self.baggage_buffer_eur < 0:
            raise ValueError("baggage_buffer_eur must not be negative")
        if self.baggage_buffer_eur > 0 and not self.needs_bag_verify:
            raise ValueError("a baggage buffer only applies to a carrier flagged for verification")
        if (self.typical_eur is None) != (self.vs_typical is None):
            raise ValueError("typical_eur and vs_typical must both be set or both omitted")
        if self.typical_eur is not None and self.typical_eur <= 0:
            raise ValueError("typical_eur must be positive")
        if self.vs_typical is not None and self.vs_typical not in _VS_TYPICAL:
            raise ValueError(f"invalid vs_typical: {self.vs_typical!r}")
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

    def to_dict(self) -> Mapping[str, object]:
        lead = self.legs[0]
        two_stop = self.stops_count is not None and self.stops_count >= 2
        return {
            "airline": self.airline,
            "departure": lead.departure,
            "arrival": lead.arrival,
            "price": self.price,
            "price_eur": self.price_eur,
            "typical_eur": self.typical_eur,
            "vs_typical": self.vs_typical,
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
            "raw_count": self.raw_count,
            "eligible_count": self.eligible_count,
            "offers": [offer.to_dict() for offer in self.offers],
        }


@dataclass(frozen=True)
class QueryFailure:
    query: Trip
    error: SearchError
    status: Literal["error"] = field(init=False, default="error")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "status": self.status,
            "query": self.query.to_dict(),
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


@dataclass(frozen=True)
class DatePriceRow:
    departure_date: date
    price_eur: Optional[float] = None
    airline: Optional[str] = None
    stops_count: Optional[int] = None
    status: Literal["ok", "empty", "error"] = "ok"
    error: Optional[SearchError] = None

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "date": self.departure_date.isoformat(),
            "price_eur": self.price_eur,
            "airline": self.airline,
            "stops_count": self.stops_count,
            "status": self.status,
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
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
    fetch_backend: Optional[str] = "calendar"
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
            "origin": self.origin,
            "destination": self.destination,
            "from": self.start_date.isoformat(),
            "to": self.end_date.isoformat(),
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "days": [row.to_dict() for row in self.days],
        }


@dataclass(frozen=True)
class ExploreDestination:
    iata: str
    city: str
    country: Optional[str]
    price_eur: Optional[float] = None

    def to_dict(self) -> Mapping[str, object]:
        return {
            "iata": self.iata,
            "city": self.city,
            "country": self.country,
            "price_eur": self.price_eur,
        }


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
    error: Optional[SearchError] = None
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
            "origin": self.origin,
            "from": self.start_date.isoformat(),
            "days": self.days,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "destinations": [row.to_dict() for row in self.destinations],
        }
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
    locale: str = "es"
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
