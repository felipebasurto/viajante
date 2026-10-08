"""Flight query and trip types, raw Google Flights parse types, flight
offers, per-query outcomes, and the flight search report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal, Mapping, Optional, Sequence, Tuple, Union

from viajante.models_common import (
    EmptyReason,
    EvidenceCompleteness,
    EvidenceKnowledge,
    FlightCabin,
    OfferEvidence,
    SearchCoverage,
    SearchError,
    SearchErrorCode,
    VsTypical,
    _deadline_stop,
    _iso_z,
    _normalize_iata,
    _put_present,
    _put_truthy,
    _require_airline_codes,
    _require_alliances,
    _require_bag_count,
    _require_cabin,
    _require_occupancy,
    _require_positive_amount,
    _require_price_cap,
    _require_typical_triple,
    _store_naive_utc,
    _store_nearby_label,
    format_typical_deal,
)

if TYPE_CHECKING:
    # recommend.py builds on these models; the import is for annotations only.
    from viajante.recommend import Recommendation


def _require_shop_fields(trip: Trip) -> None:
    _require_occupancy(
        adults=trip.adults,
        children=trip.children,
        infants_in_seat=trip.infants_in_seat,
        infants_on_lap=trip.infants_on_lap,
    )
    _require_cabin(trip.cabin)
    _require_bag_count(trip.bags, role="bags")
    _require_bag_count(trip.carry_on, role="carry_on")
    _require_price_cap(trip.price_cap)
    _require_airline_codes(trip.airlines, role="airlines")
    _require_airline_codes(trip.exclude_airlines, role="exclude_airlines")
    _require_alliances(trip.alliances, role="alliances")
    _require_alliances(trip.exclude_alliances, role="exclude_alliances")


def _shop_fields_json(trip: Trip) -> dict[str, object]:
    """Named occupancy, bags, cap, and carrier lists. Zero, unset, or empty stays omitted."""
    payload: dict[str, object] = {}
    for key in ("children", "infants_in_seat", "infants_on_lap"):
        if getattr(trip, key):
            payload[key] = getattr(trip, key)
    for key in ("bags", "carry_on", "price_cap"):
        if getattr(trip, key) is not None:
            payload[key] = getattr(trip, key)
    for key in ("airlines", "exclude_airlines", "alliances", "exclude_alliances"):
        if getattr(trip, key):
            payload[key] = list(getattr(trip, key))
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
    price_cap: Optional[int] = None
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
        _require_shop_fields(self)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        _store_nearby_label(self)

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
        payload.update(_shop_fields_json(self))
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
    price_cap: Optional[int] = None
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
        _require_shop_fields(self)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        _store_nearby_label(self)

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
        payload.update(_shop_fields_json(self))
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
    price_cap: Optional[int] = None
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
        _require_shop_fields(self)

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
        payload.update(_shop_fields_json(self))
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
    departure_date: Optional[date] = None
    carrier: Optional[str] = None
    arrival_date: Optional[date] = None
    departure_timezone: Optional[str] = None
    arrival_timezone: Optional[str] = None

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "origin": self.origin,
            "destination": self.destination,
            "departure": self.departure,
            "arrival": self.arrival,
            "airline": self.airline,
            "flight_number": self.flight_number,
        }
        if self.departure_date is not None:
            payload["departure_date"] = self.departure_date.isoformat()
        if self.arrival_date is not None:
            payload["arrival_date"] = self.arrival_date.isoformat()
        _put_present(payload, "departure_timezone", self.departure_timezone)
        _put_present(payload, "arrival_timezone", self.arrival_timezone)
        _put_present(payload, "carrier", self.carrier)
        return payload


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


def _offer_completeness(
    legs: Sequence[RawJourneyLeg],
    *,
    stops_count: Optional[int],
    checked_bags: Optional[int],
    carry_on: Optional[int],
    expected_journeys: int,
) -> EvidenceCompleteness:
    segments = tuple(segment for leg in legs for segment in leg.segments)
    journeys_complete = len(legs) == expected_journeys
    segments_complete = journeys_complete and all(leg.segments for leg in legs)

    def known(predicate: bool) -> EvidenceKnowledge:
        return "known" if predicate else "unknown"

    layovers_known = journeys_complete and (
        stops_count == 0
        or (
            stops_count is not None
            and stops_count > 0
            and sum(len(leg.layovers) for leg in legs) >= stops_count
        )
    )
    if len(legs) > 1:
        layovers_known = journeys_complete and all(
            leg.segments and len(leg.layovers) >= len(leg.segments) - 1 for leg in legs
        )
    return EvidenceCompleteness(
        segment_airports=known(
            segments_complete
            and all(segment.origin and segment.destination for segment in segments)
        ),
        segment_operators=known(segments_complete and all(segment.airline for segment in segments)),
        flight_numbers=known(
            segments_complete and all(segment.flight_number for segment in segments)
        ),
        segment_clocks=known(
            segments_complete and all(segment.departure and segment.arrival for segment in segments)
        ),
        segment_dates=known(
            segments_complete
            and all(segment.departure_date and segment.arrival_date for segment in segments)
        ),
        segment_timezones=known(
            segments_complete
            and all(segment.departure_timezone and segment.arrival_timezone for segment in segments)
        ),
        layovers=known(layovers_known),
        baggage=known(checked_bags is not None or carry_on is not None),
    )


@dataclass(frozen=True)
class FlightOffer:
    airline: Optional[str]
    departure: Optional[str]
    arrival: Optional[str]
    price_text: str
    price: float
    duration: Optional[str]
    duration_hours: Optional[float]
    stops: Optional[str]
    stops_count: Optional[int]
    baggage_buffer: int
    needs_bag_verify: bool
    layover_city: Optional[str] = None
    layover_hours: Optional[float] = None
    flight_numbers: Optional[Tuple[str, ...]] = None
    booking_token: Optional[str] = None
    google_flights_url: Optional[str] = None
    legs: Tuple[RawJourneyLeg, ...] = ()
    typical: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None
    checked_bags: Optional[int] = None
    carry_on: Optional[int] = None
    evidence: Optional[OfferEvidence] = None
    completeness: Optional[EvidenceCompleteness] = None

    def __post_init__(self) -> None:
        _require_positive_amount(self.price, role="price")
        if self.baggage_buffer < 0:
            raise ValueError("baggage_buffer must not be negative")
        if self.baggage_buffer > 0 and not self.needs_bag_verify:
            raise ValueError("a baggage buffer only applies to a carrier flagged for verification")
        _require_typical_triple(self.typical, self.vs_typical, self.vs_typical_pct)
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
        if self.completeness is None:
            expected_journeys = len(self.legs)
            if self.evidence is not None:
                query = self.evidence.query
                if query.get("trip") == "rt":
                    expected_journeys = 2
                elif query.get("trip") == "multi":
                    expected_journeys = len(query["legs"])
            object.__setattr__(
                self,
                "completeness",
                _offer_completeness(
                    self.legs,
                    stops_count=self.stops_count,
                    checked_bags=self.checked_bags,
                    carry_on=self.carry_on,
                    expected_journeys=expected_journeys,
                ),
            )

    def typical_deal(self, currency: str) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical, self.vs_typical_pct, currency)

    def to_dict(self, currency: str) -> Mapping[str, object]:
        lead = self.legs[0]
        two_stop = self.stops_count is not None and self.stops_count >= 2
        payload: dict[str, object] = {
            "airline": self.airline,
            "departure": lead.departure,
            "arrival": lead.arrival,
            "price_text": self.price_text,
            "price": self.price,
            "typical": self.typical,
            "vs_typical": self.vs_typical,
            "vs_typical_pct": self.vs_typical_pct,
            "typical_deal": self.typical_deal(currency),
            "duration": self.duration,
            "duration_hours": self.duration_hours,
            "stops": self.stops,
            "stops_count": self.stops_count,
            "layover_city": None if two_stop else self.layover_city,
            "layover_hours": None if two_stop else self.layover_hours,
            "flight_numbers": list(self.flight_numbers) if self.flight_numbers else None,
            "booking_token": self.booking_token,
            "baggage_buffer": self.baggage_buffer,
            "needs_bag_verify": self.needs_bag_verify,
            "legs": [leg.to_dict() for leg in self.legs],
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "completeness": self.completeness.to_dict() if self.completeness else None,
        }
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        _put_present(payload, "checked_bags", self.checked_bags)
        _put_present(payload, "carry_on", self.carry_on)
        return payload


@dataclass(frozen=True)
class StopsCompareSide:
    """Cheapest parsed offer in one stop bucket. Cabin fare only; never invented."""

    price_text: str
    price: float
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
        _require_positive_amount(self.price, role="price")
        if self.stops_count not in (0, 1):
            raise ValueError("stops_count must be 0 or 1")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "airline": self.airline,
            "departure": self.departure,
            "arrival": self.arrival,
            "price_text": self.price_text,
            "price": self.price,
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
            price_text=offer.price_text,
            price=offer.price,
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


@dataclass(frozen=True)
class QuerySuccess:
    query: Trip
    raw_count: int
    eligible_count: int
    offers: Tuple[FlightOffer, ...]
    google_flights_url: Optional[str] = None
    stops_compare: Optional[StopsCompare] = None
    empty_reason: Optional[EmptyReason] = None
    recommendation: Optional["Recommendation"] = None
    page_errors: Tuple[SearchError, ...] = ()
    scope_bound: bool = False
    status: Literal["ok"] = field(init=False, default="ok")

    def __post_init__(self) -> None:
        if self.raw_count < self.eligible_count:
            raise ValueError("raw_count must be >= eligible_count")
        if self.eligible_count < len(self.offers):
            raise ValueError("eligible_count must be >= number of offers")
        if self.empty_reason is not None and self.offers:
            raise ValueError("empty_reason applies only to a query with no offers")

    def to_dict(self, currency: str) -> Mapping[str, object]:
        query = dict(self.query.to_dict())
        if self.google_flights_url:
            query["google_flights_url"] = self.google_flights_url
        payload: dict[str, object] = {
            "status": self.status,
            "query": query,
            "raw_count": self.raw_count,
            "eligible_count": self.eligible_count,
            "offers": [offer.to_dict(currency) for offer in self.offers],
        }
        if self.page_errors:
            payload["page_errors"] = [error.to_dict() for error in self.page_errors]
        if self.scope_bound:
            payload["scope_bound"] = True
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.recommendation is not None:
            listed = {offer.evidence.evidence_id for offer in self.offers if offer.evidence}
            payload["recommendation"] = self.recommendation.to_dict(listed, currency)
        _put_present(payload, "empty_reason", self.empty_reason)
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


def _query_coverage(results: Sequence[QueryResult]) -> SearchCoverage:
    cut = _deadline_stop(
        [result.error.code if isinstance(result, QueryFailure) else None for result in results]
    )
    scope_bound = any(isinstance(result, QuerySuccess) and result.scope_bound for result in results)
    multi_bound = next(
        (
            result.query
            for result in results
            if isinstance(result, QuerySuccess)
            and result.scope_bound
            and isinstance(result.query, MultiCity)
        ),
        None,
    )
    page_cut = any(
        isinstance(result, QuerySuccess)
        and any(error.code == SearchErrorCode.DEADLINE for error in result.page_errors)
        for result in results
    )
    if page_cut and cut is None:
        cut = "optional package expansion did not finish before deadline_seconds"
    succeeded = sum(isinstance(result, QuerySuccess) for result in results)
    empty = sum(
        isinstance(result, QueryFailure) and result.error.code == SearchErrorCode.NO_RESULTS
        for result in results
    )
    failed = len(results) - succeeded - empty
    return SearchCoverage(
        scope={
            "kind": "submitted_queries",
            "size": len(results),
            # Same ~8-selection budget as the transport's click cap.
            **(
                {"multi_city_leader_limit": max(2, 8 // (len(multi_bound.legs) - 1))}
                if multi_bound is not None
                else {"public_page_outbound_limit": 8}
                if scope_bound
                else {}
            ),
        },
        attempted=len(results),
        succeeded=succeeded,
        empty=empty,
        failed=failed,
        complete=cut is None and not scope_bound,
        strategy="heuristic" if scope_bound else "finite",
        stopping_reason="deadline"
        if cut is not None
        else "bounded_multi_city_leaders"
        if multi_bound is not None
        else "bounded_outbound_board"
        if scope_bound
        else "completed_scope",
        unsearched=(
            "queries outside the submitted finite scope"
            if cut is None
            else f"{cut}; queries outside the submitted finite scope"
        ),
    )


@dataclass(frozen=True)
class SearchReport:
    searched_at: datetime
    queries: Tuple[QueryResult, ...]
    currency: str = field(kw_only=True)
    locale: str = "en"
    fetch_backend: Optional[FetchBackend] = None
    fetch_ms: Optional[int] = None
    coverage: Optional[SearchCoverage] = None
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        _store_naive_utc(self)
        if self.coverage is None:
            object.__setattr__(self, "coverage", _query_coverage(self.queries))

    def to_dict(self) -> Mapping[str, object]:
        return {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "locale": self.locale,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "queries": [
                result.to_dict(currency=self.currency)
                if isinstance(result, QuerySuccess)
                else result.to_dict()
                for result in self.queries
            ],
        }
