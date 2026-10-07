"""Domain types and JSON mapping for flight and hotel reports."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
from statistics import median
from typing import TYPE_CHECKING, Literal, Mapping, Optional, Sequence, Tuple, Union, get_args

from viajante.airports import is_known_iata

if TYPE_CHECKING:
    # recommend.py builds on these models; the import is for annotations only.
    from viajante.recommend import Recommendation

# Fetch/browser locale is English so owned card parsers stay on English evidence.
FETCH_LANGUAGE = "en"
FETCH_LOCALE = "en-US"

FlightCabin = Literal["economy", "premium-economy", "business", "first"]
_CABINS: tuple[FlightCabin, ...] = get_args(FlightCabin)
VsTypical = Literal["below", "near", "above"]
_VS_TYPICAL: tuple[VsTypical, ...] = get_args(VsTypical)
NEAR_TYPICAL_RATIO = 0.10


def format_money(amount: float, currency: str, *, width: int = 0) -> str:
    """Owned amount in the quote currency. € only when currency is EUR."""
    number = f"{amount:{width}.0f}" if width else f"{amount:.0f}"
    unit = "€" if currency == "EUR" else currency
    return f"{number} {unit}"


def vs_typical(price: float, typical: Optional[float]) -> Optional[VsTypical]:
    """Coarse label against an owned typical. None when there is no typical."""
    if typical is None or typical <= 0:
        return None
    if price < typical * (1.0 - NEAR_TYPICAL_RATIO):
        return "below"
    if price > typical * (1.0 + NEAR_TYPICAL_RATIO):
        return "above"
    return "near"


def vs_typical_pct(price: float, typical: Optional[float]) -> Optional[int]:
    """Signed percent of the fare versus an owned typical. None without a typical."""
    if typical is None or typical <= 0:
        return None
    return int(round((price / typical - 1.0) * 100.0))


def _require_typical_triple(
    typical: Optional[float],
    vs: Optional[VsTypical],
    pct: Optional[int],
) -> None:
    have = (typical is None, vs is None, pct is None)
    if len(set(have)) != 1:
        raise ValueError("typical, vs_typical, and vs_typical_pct must all be set or all omitted")
    if typical is not None and typical <= 0:
        raise ValueError("typical must be positive")
    if vs is not None and vs not in _VS_TYPICAL:
        raise ValueError(f"invalid vs_typical: {vs!r}")


def _typical_json(
    typical: Optional[float],
    vs: Optional[VsTypical],
    pct: Optional[int],
    currency: str,
) -> dict[str, object]:
    if typical is None or vs is None or pct is None:
        return {}
    return {
        "typical": typical,
        "vs_typical": vs,
        "vs_typical_pct": pct,
        "typical_deal": format_typical_deal(vs, typical, pct, currency),
    }


def format_typical_deal(
    vs: Optional[VsTypical],
    typical: Optional[float],
    pct: Optional[int],
    currency: str,
) -> Optional[str]:
    """English one-liner, or None when typical is omitted."""
    if vs is None or typical is None or pct is None:
        return None
    if pct > 0:
        shown = f"+{pct}%"
    elif pct < 0:
        shown = f"−{abs(pct)}%"
    else:
        shown = "0%"
    return f"{vs} typical {format_money(typical, currency)} ({shown})"


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


def _require_positive_amount(value: float, *, role: str) -> None:
    if value <= 0:
        raise ValueError(f"{role} must be positive")


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
        raise ValueError("price_cap must be positive")


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


def _store_naive_utc(report: object) -> None:
    searched_at = report.searched_at  # type: ignore[attr-defined]
    if searched_at.tzinfo is not None:
        naive = searched_at.astimezone(timezone.utc).replace(tzinfo=None)
        object.__setattr__(report, "searched_at", naive)


def _iso_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_nearby_label(obj: object) -> None:
    label = obj.nearby_label  # type: ignore[attr-defined]
    object.__setattr__(obj, "nearby_label", (label.strip() if label else None) or None)


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
        if self.departure_timezone is not None:
            payload["departure_timezone"] = self.departure_timezone
        if self.arrival_timezone is not None:
            payload["arrival_timezone"] = self.arrival_timezone
        if self.carrier is not None:
            payload["carrier"] = self.carrier
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


EvidenceKnowledge = Literal["known", "unknown"]
UrlEvidenceKind = Literal["booking", "query", "none"]
ConstraintStatus = Literal["pass", "fail", "unknown"]


@dataclass(frozen=True)
class EvidenceCompleteness:
    """Facts an agent may safely derive from one owned offer."""

    segment_airports: EvidenceKnowledge = "unknown"
    segment_operators: EvidenceKnowledge = "unknown"
    flight_numbers: EvidenceKnowledge = "unknown"
    segment_clocks: EvidenceKnowledge = "unknown"
    layovers: EvidenceKnowledge = "unknown"
    baggage: EvidenceKnowledge = "unknown"
    segment_dates: EvidenceKnowledge = "unknown"
    segment_timezones: EvidenceKnowledge = "unknown"

    def to_dict(self) -> Mapping[str, str]:
        return {
            "segment_airports": self.segment_airports,
            "segment_operators": self.segment_operators,
            "flight_numbers": self.flight_numbers,
            "segment_clocks": self.segment_clocks,
            "layovers": self.layovers,
            "baggage": self.baggage,
            "segment_dates": self.segment_dates,
            "segment_timezones": self.segment_timezones,
        }


@dataclass(frozen=True)
class OfferEvidence:
    """Immutable provenance copied with an offer when it leaves its report."""

    evidence_id: str
    query: Mapping[str, object]
    currency: str = field(kw_only=True)
    retrieved_at: datetime
    fetch_backend: Optional[str]
    query_url: Optional[str]
    offer_url: Optional[str]
    url_kind: UrlEvidenceKind
    source: Literal["google_flights"] = "google_flights"

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("evidence_id is required")
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        if self.url_kind == "booking" and not self.offer_url:
            raise ValueError("booking URL evidence needs an offer_url")
        if self.url_kind == "query" and not self.query_url:
            raise ValueError("query URL evidence needs a query_url")
        if self.url_kind == "none" and (self.query_url or self.offer_url):
            raise ValueError("none URL evidence cannot carry a URL")

    def to_dict(self) -> Mapping[str, object]:
        retrieved_at = self.retrieved_at
        if retrieved_at.tzinfo is not None:
            retrieved_at = retrieved_at.astimezone(timezone.utc).replace(tzinfo=None)
        return {
            "evidence_id": self.evidence_id,
            "source": self.source,
            "query": dict(self.query),
            "currency": self.currency,
            "retrieved_at": retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetch_backend": self.fetch_backend,
            "query_url": self.query_url,
            "offer_url": self.offer_url,
            "url_kind": self.url_kind,
        }


@dataclass(frozen=True)
class SearchCoverage:
    """Completion within one declared finite scope, never a global proof."""

    scope: Mapping[str, object]
    attempted: int
    succeeded: int
    empty: int
    failed: int
    complete: bool
    strategy: Literal["finite", "heuristic"] = "finite"
    stopping_reason: str = "completed_scope"
    unsearched: Optional[str] = None

    def __post_init__(self) -> None:
        counts = (self.attempted, self.succeeded, self.empty, self.failed)
        if any(value < 0 for value in counts):
            raise ValueError("coverage counts must not be negative")
        if self.succeeded + self.empty + self.failed != self.attempted:
            raise ValueError("coverage outcomes must equal attempted")
        if not self.stopping_reason.strip():
            raise ValueError("coverage stopping_reason is required")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "scope": dict(self.scope),
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "empty": self.empty,
            "failed": self.failed,
            "complete": self.complete,
            "strategy": self.strategy,
            "stopping_reason": self.stopping_reason,
            "unsearched": self.unsearched,
        }


@dataclass(frozen=True)
class ConstraintCheck:
    constraint: str
    status: ConstraintStatus
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.constraint.strip():
            raise ValueError("constraint name is required")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "constraint": self.constraint,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ItineraryValidationReport:
    validated_at: datetime
    scenario: Mapping[str, object]
    checks: Tuple[ConstraintCheck, ...]
    legs: Tuple[Mapping[str, object], ...]
    feasible: Optional[bool]
    currency: Optional[str]
    fare_total: Optional[float]
    ranked_total: Optional[float]
    offer_row_count: int
    journey_leg_count: int
    segment_count: Optional[int]
    trip_span_days: Optional[int]
    violations: Tuple[str, ...] = ()
    unknown: Tuple[str, ...] = ()
    relaxations: Tuple[Mapping[str, object], ...] = ()
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        if min(self.offer_row_count, self.journey_leg_count) < 0:
            raise ValueError("itinerary counts must not be negative")
        if self.segment_count is not None and self.segment_count < 0:
            raise ValueError("segment_count must not be negative")
        if self.trip_span_days is not None and self.trip_span_days < 0:
            raise ValueError("trip_span_days must not be negative")
        statuses = {check.status for check in self.checks}
        expected = False if "fail" in statuses else None if "unknown" in statuses else True
        if self.feasible is not expected:
            raise ValueError("feasible must aggregate check statuses")

    def to_dict(self) -> Mapping[str, object]:
        validated_at = self.validated_at
        if validated_at.tzinfo is not None:
            validated_at = validated_at.astimezone(timezone.utc).replace(tzinfo=None)
        return {
            "schema_version": self.schema_version,
            "validated_at": validated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scenario": dict(self.scenario),
            "relaxations": [dict(item) for item in self.relaxations],
            "feasible": self.feasible,
            "currency": self.currency,
            "fare_total": self.fare_total,
            "ranked_total": self.ranked_total,
            "offer_row_count": self.offer_row_count,
            "journey_leg_count": self.journey_leg_count,
            "segment_count": self.segment_count,
            "trip_span_days": self.trip_span_days,
            "violations": list(self.violations),
            "unknown": list(self.unknown),
            "checks": [check.to_dict() for check in self.checks],
            "legs": [dict(leg) for leg in self.legs],
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
    cheapest_date: Optional[date] = None
    cheapest: Optional[float] = None
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
        if (self.cheapest_date is None) != (self.cheapest is None):
            raise ValueError("cheapest_date and cheapest must both be set or both omitted")
        if self.cheapest is not None and self.cheapest <= 0:
            raise ValueError("cheapest must be positive")
        if self.cheapest_date is not None and self.typical is None:
            raise ValueError("cheapest day is omitted unless typical is set")
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
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.checked_bags is not None:
            payload["checked_bags"] = self.checked_bags
        if self.carry_on is not None:
            payload["carry_on"] = self.carry_on
        if self.cheapest_date is not None and self.cheapest is not None:
            payload["cheapest_date"] = self.cheapest_date.isoformat()
            payload["cheapest"] = self.cheapest
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


EmptyReason = Literal["provider_empty", "filtered_out", "not_loaded"]


class SearchErrorCode(str, Enum):
    NO_RESULTS = "no_results"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    MARKUP_DRIFT = "markup_drift"
    FETCH_FAILED = "fetch_failed"
    BROWSER_UNAVAILABLE = "browser_unavailable"
    CURRENCY_MISMATCH = "currency_mismatch"
    DEADLINE = "deadline"


@dataclass(frozen=True)
class SearchError:
    code: SearchErrorCode
    message: str
    rate_limited: bool = False
    # Epoch seconds a recorded cooldown ends; None unless a cooldown file named one.
    retry_until: Optional[float] = None
    timeout: bool = False
    diagnostics: Optional[Mapping[str, object]] = None

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {"code": self.code.value, "message": self.message}
        if self.rate_limited:
            payload["rate_limited"] = True
            now = time.time()
            if self.retry_until is not None and self.retry_until > now:
                # Round up once: the ISO instant is never earlier than the real end, and
                # the seconds come from that same instant.
                end = math.ceil(self.retry_until)
                payload["retry_after"] = datetime.fromtimestamp(end, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                payload["retry_after_seconds"] = max(1, math.ceil(end - now))
        if self.timeout:
            payload["timeout"] = True
        if self.diagnostics is not None:
            payload["diagnostics"] = dict(self.diagnostics)
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
            payload["recommendation"] = self.recommendation.to_dict(currency)
        if self.empty_reason is not None:
            payload["empty_reason"] = self.empty_reason
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


def _deadline_stop(codes: Sequence[Optional[SearchErrorCode]]) -> Optional[str]:
    """Why a deadline made a scope incomplete, from the rows' own error codes."""
    cut = sum(code == SearchErrorCode.DEADLINE for code in codes)
    if not cut:
        return None
    return (
        f"{cut} of {len(codes)} units did not finish before deadline_seconds: "
        "not loaded, not proven empty"
    )


def _query_coverage(results: Sequence[QueryResult]) -> SearchCoverage:
    cut = _deadline_stop(
        [result.error.code if isinstance(result, QueryFailure) else None for result in results]
    )
    scope_bound = any(isinstance(result, QuerySuccess) and result.scope_bound for result in results)
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
            **({"public_page_outbound_limit": 8} if scope_bound else {}),
        },
        attempted=len(results),
        succeeded=succeeded,
        empty=empty,
        failed=failed,
        complete=cut is None and not scope_bound,
        strategy="heuristic" if scope_bound else "finite",
        stopping_reason="deadline"
        if cut is not None
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


DateTripKind = Literal["one-way", "rt"]

# Same floor as typical.MIN_DAILY_PRICES. Tests pin the two together.
MIN_PRICED_DAYS_FOR_SUMMARY = 3


@dataclass(frozen=True)
class DateCalendarSummary:
    """Min / median / max over owned priced days. Absent when the grid is thin."""

    min_price: float
    median_price: float
    max_price: float
    cheapest_date: date
    n_priced: int

    def to_dict(self) -> Mapping[str, object]:
        return {
            "min_price": self.min_price,
            "median_price": self.median_price,
            "max_price": self.max_price,
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
    cheapest_date, min_price = min(priced, key=lambda item: (item[1], item[0]))
    return DateCalendarSummary(
        min_price=min_price,
        median_price=float(median(prices)),
        max_price=max(prices),
        cheapest_date=cheapest_date,
        n_priced=len(priced),
    )


def _stamp_date_row_typical(
    row: "DatePriceRow",
    typical: Optional[float],
) -> "DatePriceRow":
    """Stamp or omit the owned window median. Empty/error rows stay omitted."""
    if row.status != "ok" or row.price is None or row.price <= 0:
        if row.typical is None:
            return row
        return replace(row, typical=None, vs_typical=None, vs_typical_pct=None)
    label = vs_typical(row.price, typical)
    pct = vs_typical_pct(row.price, typical)
    if typical is None or label is None or pct is None:
        if row.typical is None:
            return row
        return replace(row, typical=None, vs_typical=None, vs_typical_pct=None)
    if row.typical == typical and row.vs_typical == label and row.vs_typical_pct == pct:
        return row
    return replace(row, typical=typical, vs_typical=label, vs_typical_pct=pct)


@dataclass(frozen=True)
class DatePriceRow:
    departure_date: date
    price: Optional[float] = None
    airline: Optional[str] = None
    stops_count: Optional[int] = None
    return_date: Optional[date] = None
    status: Literal["ok", "empty", "error"] = "ok"
    error: Optional[SearchError] = None
    stops_compare: Optional[StopsCompare] = None
    google_flights_url: Optional[str] = None
    typical: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None
    baggage_buffer: Optional[int] = None
    duration_hours: Optional[float] = None
    departure: Optional[str] = None
    arrival: Optional[str] = None
    empty_reason: Optional[EmptyReason] = None
    page_errors: Tuple[SearchError, ...] = ()
    scope_bound: bool = False

    def __post_init__(self) -> None:
        if self.empty_reason is not None and self.status != "empty":
            raise ValueError("empty_reason applies only to empty rows")
        _require_typical_triple(self.typical, self.vs_typical, self.vs_typical_pct)
        if (self.status != "ok" or self.price is None) and self.typical is not None:
            raise ValueError("empty/error rows omit typical")
        if self.baggage_buffer is not None and self.baggage_buffer < 0:
            raise ValueError("baggage_buffer must not be negative")
        if (self.status != "ok" or self.price is None) and self.baggage_buffer is not None:
            raise ValueError("empty/error rows omit baggage buffer")
        if (self.status != "ok" or self.price is None) and (
            self.duration_hours is not None or self.departure or self.arrival
        ):
            raise ValueError("shop duration/clocks require an owned day fare")

    def typical_deal(self, currency: str) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical, self.vs_typical_pct, currency)

    def to_dict(self, currency: str) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "date": self.departure_date.isoformat(),
            "price": self.price,
            "airline": self.airline,
            "stops_count": self.stops_count,
            "status": self.status,
        }
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.page_errors:
            payload["page_errors"] = [error.to_dict() for error in self.page_errors]
        if self.scope_bound:
            payload["scope_bound"] = True
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.baggage_buffer is not None:
            payload["baggage_buffer"] = self.baggage_buffer
        if self.duration_hours is not None:
            payload["duration_hours"] = self.duration_hours
        if self.departure:
            payload["departure"] = self.departure
        if self.arrival:
            payload["arrival"] = self.arrival
        if self.empty_reason is not None:
            payload["empty_reason"] = self.empty_reason
        payload.update(_typical_json(self.typical, self.vs_typical, self.vs_typical_pct, currency))
        return payload


@dataclass(frozen=True)
class DateCalendarReport:
    searched_at: datetime
    origin: str
    destination: str
    start_date: date
    end_date: date
    days: Tuple[DatePriceRow, ...]
    currency: str = field(kw_only=True)
    locale: str = "en"
    trip: DateTripKind = "one-way"
    nights: Optional[int] = None
    fetch_backend: Optional[str] = "calendar"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    nearby_label: Optional[str] = None
    summary: Optional[DateCalendarSummary] = field(init=False, default=None)
    coverage: Optional[SearchCoverage] = None
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        _store_naive_utc(self)
        _store_nearby_label(self)
        object.__setattr__(
            self,
            "summary",
            owned_calendar_summary([(row.departure_date, row.price) for row in self.days]),
        )
        typical = None if self.summary is None else self.summary.median_price
        object.__setattr__(
            self,
            "days",
            tuple(_stamp_date_row_typical(row, typical) for row in self.days),
        )
        if self.coverage is None:
            succeeded = sum(row.status == "ok" and row.price is not None for row in self.days)
            empty = sum(row.status == "empty" for row in self.days)
            failed = len(self.days) - succeeded - empty
            scope_bound = any(row.scope_bound for row in self.days)
            expected = (self.end_date - self.start_date).days + 1
            cut = _deadline_stop(
                [row.error.code if row.error else None for row in self.days]
                + [error.code for row in self.days for error in row.page_errors]
            )
            coverage_complete = (
                cut is None
                and not scope_bound
                and len({row.departure_date for row in self.days}) == expected
            )
            object.__setattr__(
                self,
                "coverage",
                SearchCoverage(
                    scope={
                        "kind": "date_window",
                        **({"public_page_outbound_limit": 8} if scope_bound else {}),
                        "from": self.start_date.isoformat(),
                        "to": self.end_date.isoformat(),
                    },
                    attempted=len(self.days),
                    succeeded=succeeded,
                    empty=empty,
                    failed=failed,
                    complete=coverage_complete,
                    strategy="heuristic" if scope_bound else "finite",
                    stopping_reason=(
                        "completed_scope"
                        if coverage_complete
                        else "deadline"
                        if cut
                        else "bounded_outbound_board"
                        if scope_bound
                        else "partial_results"
                    ),
                    unsearched=(
                        None
                        if coverage_complete
                        else cut
                        or (
                            "outbound candidates outside the first eight returned"
                            if scope_bound
                            else "dates missing from the requested window"
                        )
                    ),
                ),
            )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "locale": self.locale,
            "origin": self.origin,
            "destination": self.destination,
            "from": self.start_date.isoformat(),
            "to": self.end_date.isoformat(),
            "trip": self.trip,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "days": [row.to_dict(self.currency) for row in self.days],
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
    typical: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    currency: str = field(kw_only=True)
    locale: str = "en"
    trip: DateTripKind = "one-way"
    nights: Optional[int] = None
    fetch_backend: Optional[FlexFetchBackend] = "calendar"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    error: Optional[SearchError] = None
    nearby_label: Optional[str] = None
    coverage: Optional[SearchCoverage] = None
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        if self.flex_days < 1:
            raise ValueError("flex_days must be at least 1")
        if self.typical is not None and self.typical <= 0:
            raise ValueError("typical must be positive")
        if self.vs_typical is not None and self.vs_typical not in _VS_TYPICAL:
            raise ValueError(f"invalid vs_typical: {self.vs_typical!r}")
        if self.vs_typical is not None and self.typical is None:
            raise ValueError("vs_typical requires typical")
        _store_naive_utc(self)
        _store_nearby_label(self)
        if self.coverage is None:
            succeeded = sum(row.status == "ok" and row.price is not None for row in self.days)
            empty = sum(row.status == "empty" for row in self.days)
            failed = len(self.days) - succeeded - empty
            scope_bound = any(row.scope_bound for row in self.days)
            expected = (self.end_date - self.start_date).days + 1
            codes = [row.error.code if row.error else None for row in self.days] + [
                error.code for row in self.days for error in row.page_errors
            ]
            cut = _deadline_stop(codes + [self.error.code if self.error else None])
            # A shop cut by the deadline is one more unit that was attempted and failed.
            shop_cut = self.error is not None and self.error.code == SearchErrorCode.DEADLINE
            coverage_complete = (
                cut is None
                and not scope_bound
                and len({row.departure_date for row in self.days}) == expected
            )
            object.__setattr__(
                self,
                "coverage",
                SearchCoverage(
                    scope={
                        "kind": "flex_window",
                        **({"public_page_outbound_limit": 8} if scope_bound else {}),
                        "around": self.around.isoformat(),
                        "from": self.start_date.isoformat(),
                        "to": self.end_date.isoformat(),
                    },
                    attempted=len(self.days) + shop_cut,
                    succeeded=succeeded,
                    empty=empty,
                    failed=failed + shop_cut,
                    complete=coverage_complete,
                    strategy="heuristic" if scope_bound else "finite",
                    stopping_reason=(
                        "completed_scope"
                        if coverage_complete
                        else "deadline"
                        if cut
                        else "bounded_outbound_board"
                        if scope_bound
                        else "partial_results"
                    ),
                    unsearched=(
                        None
                        if coverage_complete
                        else cut
                        or (
                            "outbound candidates outside the first eight returned"
                            if scope_bound
                            else "dates missing from the requested flex window"
                        )
                    ),
                ),
            )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
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
            "typical": self.typical,
            "vs_typical": self.vs_typical,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "days": [row.to_dict(self.currency) for row in self.days],
            "offers": [offer.to_dict(self.currency) for offer in self.offers],
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
    price: Optional[float] = None
    duration_hours: Optional[float] = None
    departure: Optional[str] = None
    arrival: Optional[str] = None
    stops_compare: Optional[StopsCompare] = None
    google_flights_url: Optional[str] = None
    typical: Optional[float] = None
    vs_typical: Optional[VsTypical] = None
    vs_typical_pct: Optional[int] = None
    baggage_buffer: Optional[int] = None

    def __post_init__(self) -> None:
        _require_typical_triple(self.typical, self.vs_typical, self.vs_typical_pct)
        if self.price is None and self.typical is not None:
            raise ValueError("typical requires an owned dest fare")
        if self.price is None and (
            self.duration_hours is not None or self.departure or self.arrival
        ):
            raise ValueError("shop duration/clocks require an owned dest fare")
        if self.baggage_buffer is not None and self.baggage_buffer < 0:
            raise ValueError("baggage_buffer must not be negative")
        if self.price is None and self.baggage_buffer is not None:
            raise ValueError("baggage buffer requires an owned dest fare")

    def typical_deal(self, currency: str) -> Optional[str]:
        return format_typical_deal(self.vs_typical, self.typical, self.vs_typical_pct, currency)

    def to_dict(self, currency: str) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "iata": self.iata,
            "city": self.city,
            "country": self.country,
            "price": self.price,
        }
        if self.duration_hours is not None:
            payload["duration_hours"] = self.duration_hours
        if self.departure:
            payload["departure"] = self.departure
        if self.arrival:
            payload["arrival"] = self.arrival
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.baggage_buffer is not None:
            payload["baggage_buffer"] = self.baggage_buffer
        payload.update(_typical_json(self.typical, self.vs_typical, self.vs_typical_pct, currency))
        return payload


def explore_stop(
    error: Optional["SearchError"], pricing_errors: Sequence["QueryFailure"]
) -> tuple[str, str]:
    """Explore never proves the catalog; a deadline is named when it cut the search short."""
    cut = _deadline_stop(
        [error.code if error else None] + [row.error.code for row in pricing_errors]
    )
    shortlist = "destinations outside the provider shortlist and local top limit"
    if cut is None:
        return "shortlist_limit", shortlist
    return "deadline", f"{cut}; {shortlist}"


@dataclass(frozen=True)
class ExploreReport:
    searched_at: datetime
    origin: str
    start_date: date
    days: int
    destinations: Tuple[ExploreDestination, ...]
    currency: str = field(kw_only=True)
    locale: str = "en"
    fetch_backend: Optional[str] = "explore"
    fetch_ms: Optional[int] = None
    google_flights_url: Optional[str] = None
    error: Optional[SearchError] = None
    nearby_label: Optional[str] = None
    coverage: Optional[SearchCoverage] = None
    pricing_errors: Tuple[QueryFailure, ...] = field(default=(), kw_only=True)
    empty_reason: Optional[EmptyReason] = field(default=None, kw_only=True)
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        _store_naive_utc(self)
        _store_nearby_label(self)
        if self.coverage is None:
            succeeded = sum(row.price is not None for row in self.destinations)
            failed_destinations = {row.query.destination for row in self.pricing_errors}
            empty = sum(
                row.price is None and row.iata not in failed_destinations
                for row in self.destinations
            )
            failed = len(self.pricing_errors) + int(self.error is not None)
            reason, unsearched = explore_stop(self.error, self.pricing_errors)
            object.__setattr__(
                self,
                "coverage",
                SearchCoverage(
                    scope={
                        "kind": "explore_shortlist",
                        "origin": self.origin,
                        "from": self.start_date.isoformat(),
                        "days": self.days,
                    },
                    attempted=succeeded + empty + failed,
                    succeeded=succeeded,
                    empty=empty,
                    failed=failed,
                    complete=False,
                    strategy="heuristic",
                    stopping_reason=reason,
                    unsearched=unsearched,
                ),
            )

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "locale": self.locale,
            "origin": self.origin,
            "from": self.start_date.isoformat(),
            "days": self.days,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "coverage": self.coverage.to_dict() if self.coverage else None,
            "destinations": [row.to_dict(self.currency) for row in self.destinations],
        }
        if self.google_flights_url:
            payload["google_flights_url"] = self.google_flights_url
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.pricing_errors:
            payload["pricing_errors"] = [row.to_dict() for row in self.pricing_errors]
        if self.empty_reason is not None:
            payload["empty_reason"] = self.empty_reason
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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    review_count: Optional[int] = None
    place_types: Tuple[str, ...] = ()
    class_label: Optional[str] = None
    priced_adults: Optional[int] = None
    provider_id: Optional[str] = None
    # None preserves legacy card evidence; an empty string explicitly means no
    # unit evidence. Google property descriptions are never unit evidence.
    unit_details: Optional[str] = None
    link_context: Literal["stay", "property", "location", "none"] = "none"


@dataclass(frozen=True)
class HotelPage:
    cards: Tuple[RawHotelCard, ...]
    resolved_place: Optional[str] = None
    place_bounds: Optional[Tuple[float, float, float, float]] = None
    search_url: Optional[str] = None
    page_errors: Tuple[SearchError, ...] = ()


HotelProvider = Literal["booking.com", "google-hotels", "skiplagged"]


@dataclass(frozen=True)
class AppliedHotelFilters:
    chips: Tuple[str, ...]
    url: str
    # Requested filters this source cannot apply or check (it returns no evidence for them).
    not_applied: Tuple[str, ...] = ()
    url_context: Literal["stay", "property", "location", "none"] = "none"

    def to_dict(self) -> Mapping[str, object]:
        return {
            "chips": list(self.chips),
            "url": self.url,
            "not_applied": list(self.not_applied),
            "url_context": self.url_context,
        }


@dataclass(frozen=True)
class HotelOffer:
    title: str
    address: Optional[str]
    total_price_text: str
    total_price: float
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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    review_count: Optional[int] = None
    sleeps: Optional[int] = None
    place_types: Tuple[str, ...] = ()
    class_label: Optional[str] = None
    priced_adults: Optional[int] = None
    provider_id: Optional[str] = None
    distance_km: Optional[float] = None
    link_context: Literal["stay", "property", "location", "none"] = "none"
    lodging_evidence_conflict: bool = False

    def __post_init__(self) -> None:
        title = self.title.strip()
        if not title:
            raise ValueError("title must not be blank")
        _require_positive_amount(self.total_price, role="total_price")
        if self.rating_score is not None and not 0.0 <= self.rating_score <= 10.0:
            raise ValueError("rating_score must be between 0.0 and 10.0")
        object.__setattr__(self, "title", title)

    def to_dict(self) -> Mapping[str, object]:
        return {
            "title": self.title,
            "address": self.address,
            "total_price_text": self.total_price_text,
            "total_price": self.total_price,
            "rating": self.rating,
            "rating_score": self.rating_score,
            "details": self.details,
            "cancellation_evidence": self.cancellation_evidence.value,
            "property_type_evidence": self.property_type_evidence.value,
            "lodging_kind": self.lodging_kind.value,
            "lodging_evidence_conflict": self.lodging_evidence_conflict,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "beds": self.beds,
            "link": self.link,
            "link_context": self.link_context,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "review_count": self.review_count,
            "sleeps": self.sleeps,
            "place_types": list(self.place_types),
            "class_label": self.class_label,
            "priced_adults": self.priced_adults,
            "provider_id": self.provider_id,
            "distance_km": self.distance_km,
        }


@dataclass(frozen=True)
class HotelQuerySuccess:
    query: HotelQuery
    applied: AppliedHotelFilters
    raw_count: int
    eligible_count: int
    offers: Tuple[HotelOffer, ...]
    page_errors: Tuple[SearchError, ...] = field(default=(), kw_only=True)
    resolved_place: Optional[str] = None
    place_bounds: Optional[Tuple[float, float, float, float]] = None
    status: Literal["ok"] = field(init=False, default="ok")

    def __post_init__(self) -> None:
        if self.raw_count < self.eligible_count:
            raise ValueError("raw_count must be >= eligible_count")
        if self.eligible_count < len(self.offers):
            raise ValueError("eligible_count must be >= number of offers")

    def to_dict(self) -> Mapping[str, object]:
        payload = {
            "status": self.status,
            "query": self.query.to_dict(),
            "applied": self.applied.to_dict(),
            "raw_count": self.raw_count,
            "eligible_count": self.eligible_count,
            "resolved_place": self.resolved_place,
            "place_bounds": list(self.place_bounds) if self.place_bounds else None,
            "offers": [offer.to_dict() for offer in self.offers],
        }
        if self.page_errors:
            payload["page_errors"] = [error.to_dict() for error in self.page_errors]
        return payload


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


HotelFetchBackend = Literal["booking", "google", "skiplagged"]


@dataclass(frozen=True)
class HotelSearchReport:
    searched_at: datetime
    queries: Tuple[HotelQueryResult, ...]
    currency: str = field(kw_only=True)
    locale: str = FETCH_LANGUAGE
    schema_version: int = field(init=False, default=2)
    provider: HotelProvider = "booking.com"
    price_basis: Literal["total_stay"] = field(init=False, default="total_stay")
    fetch_backend: Optional[HotelFetchBackend] = None
    fetch_ms: Optional[int] = None
    # A point the caller named; offers carry their straight-line distance to it.
    near: Optional[Tuple[float, float]] = None
    max_distance_km: Optional[float] = None
    # A deadline cut a step that left no failed row (for example a second results page).
    deadline_cut: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        _store_naive_utc(self)

    def to_dict(self) -> Mapping[str, object]:
        from viajante.runtime import package_version

        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "viajante_version": package_version(),
            "provider": self.provider,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "locale": self.locale,
            "price_basis": self.price_basis,
            "fetch_backend": self.fetch_backend,
            "fetch_ms": self.fetch_ms,
            "near": {"lat": self.near[0], "lng": self.near[1]} if self.near else None,
            "max_distance_km": self.max_distance_km,
            "property_matrix": _property_matrix(self.queries),
            "queries": [result.to_dict() for result in self.queries],
        }
        cut = _deadline_stop(
            [r.error.code if isinstance(r, HotelQueryFailure) else None for r in self.queries]
        )
        if cut is None and self.deadline_cut:
            cut = "deadline_seconds cut part of this search: that evidence was not loaded"
        page_failed = any(isinstance(r, HotelQuerySuccess) and r.page_errors for r in self.queries)
        if cut is not None or page_failed:
            payload["coverage"] = SearchCoverage(
                scope={"kind": "submitted_queries", "size": len(self.queries)},
                attempted=len(self.queries),
                succeeded=sum(isinstance(r, HotelQuerySuccess) for r in self.queries),
                empty=sum(
                    isinstance(r, HotelQueryFailure) and r.error.code == SearchErrorCode.NO_RESULTS
                    for r in self.queries
                ),
                failed=sum(
                    isinstance(r, HotelQueryFailure) and r.error.code != SearchErrorCode.NO_RESULTS
                    for r in self.queries
                ),
                complete=False,
                stopping_reason="deadline" if cut else "additional_page_failed",
                unsearched=(
                    f"{cut}; stays outside the submitted finite scope"
                    if cut
                    else "Additional results page(s) failed; their hotel evidence was not loaded"
                ),
            ).to_dict()
        return payload


def _property_key(offer: "HotelOffer") -> Tuple[str, str]:
    return (
        " ".join(offer.title.split()).casefold(),
        " ".join((offer.address or "").split()).casefold(),
    )


def _property_matrix(
    queries: Tuple["HotelQueryResult", ...],
) -> Optional[list[Mapping[str, object]]]:
    """Each property seen in a multi-stay search with its total per stay, or null where absent.

    Built only from the offers each stay returned (its ``top``), so a null means "not among
    them", not "unavailable". Rows are sorted by name so the order carries no price signal.
    Nothing is ranked or chosen.
    """
    if len(queries) < 2:
        return None
    rows: dict[Tuple[str, str], dict] = {}
    for index, result in enumerate(queries):
        if not isinstance(result, HotelQuerySuccess):
            continue
        for offer in result.offers:
            row = rows.setdefault(
                _property_key(offer),
                {
                    "title": offer.title,
                    "address": offer.address,
                    "latitude": offer.latitude,
                    "longitude": offer.longitude,
                    "distance_km": offer.distance_km,
                    "prices": [None] * len(queries),
                },
            )
            if row["prices"][index] is None or offer.total_price < row["prices"][index]:
                row["prices"][index] = offer.total_price
    return [rows[key] for key in sorted(rows)]


@dataclass(frozen=True)
class TripTotal:
    """Owned flight fare plus owned hotel stay. Omitted unless both sides hit."""

    flight_fare: float
    hotel_stay: float
    total: float
    nights: int
    hotel_price_basis: Literal["total_stay"] = field(init=False, default="total_stay")

    def __post_init__(self) -> None:
        if self.flight_fare <= 0:
            raise ValueError("flight_fare must be a positive owned fare")
        if self.hotel_stay <= 0:
            raise ValueError("hotel_stay must be a positive owned stay total")
        if abs(self.total - (self.flight_fare + self.hotel_stay)) > 1e-9:
            raise ValueError("total must equal flight_fare + hotel_stay")
        if self.nights < 1:
            raise ValueError("nights must be at least 1")

    def to_dict(self) -> Mapping[str, object]:
        return {
            "flight_fare": self.flight_fare,
            "hotel_stay": self.hotel_stay,
            "total": self.total,
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
    currency: str = field(kw_only=True)
    locale: str = FETCH_LANGUAGE
    fetch_ms: Optional[int] = None
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        _store_naive_utc(self)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "locale": self.locale,
            "fetch_ms": self.fetch_ms,
            "flights": dict(self.flights.to_dict()),
            "hotels": dict(self.hotels.to_dict()),
        }
        if self.trip_total is not None:
            payload["trip_total"] = dict(self.trip_total.to_dict())
        return payload


EvidenceLevel = Literal["confirmed", "user_supplied", "cached", "estimated"]
_EVIDENCE: tuple[EvidenceLevel, ...] = ("confirmed", "user_supplied", "cached", "estimated")
HIDDEN_CITY_SOURCE = "skiplagged"

HIDDEN_CITY_WARNINGS: tuple[str, ...] = (
    "Hidden-city / point-beyond tickets often violate airline conditions of carriage.",
    "Do not check a bag to the ticketed destination if you intend to leave at a layover.",
    "Missing a segment can cancel remaining flights on the same ticket.",
    "Irregular operations can rebook you onto a routing that skips the layover city.",
    "Confirm the fare and terms on the booking link before paying. Viajante does not book.",
)


def _require_evidence(value: str) -> EvidenceLevel:
    if value not in _EVIDENCE:
        raise ValueError(f"invalid evidence: {value!r}")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class HiddenCityOffer:
    origin: str
    destination: str
    departure_date: date
    price: float
    currency: str = field(kw_only=True)
    evidence: EvidenceLevel
    source: str = HIDDEN_CITY_SOURCE
    airline: Optional[str] = None
    duration: Optional[str] = None
    stops_count: Optional[int] = None
    layover_city: Optional[str] = None
    ticketed_destination: Optional[str] = None
    hidden_city: bool = False
    return_date: Optional[date] = None
    booking_url: Optional[str] = None
    warnings: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        ticketed = (
            _normalize_iata(self.ticketed_destination, role="ticketed_destination")
            if self.ticketed_destination
            else None
        )
        _require_positive_amount(self.price, role="price")
        currency = normalize_currency(self.currency)
        evidence = _require_evidence(self.evidence)
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "ticketed_destination", ticketed)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "evidence", evidence)
        if self.hidden_city and not self.warnings:
            object.__setattr__(self, "warnings", HIDDEN_CITY_WARNINGS)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "source": self.source,
            "evidence": self.evidence,
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "price": self.price,
            "currency": self.currency,
            "airline": self.airline,
            "duration": self.duration,
            "stops_count": self.stops_count,
            "layover_city": self.layover_city,
            "ticketed_destination": self.ticketed_destination,
            "hidden_city": self.hidden_city,
        }
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.booking_url:
            payload["booking_url"] = self.booking_url
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        return payload


@dataclass(frozen=True)
class HiddenCityReport:
    searched_at: datetime
    origin: str
    destination: str
    departure_date: date
    currency: Optional[str] = None
    offers: Tuple[HiddenCityOffer, ...] = ()
    error: Optional[SearchError] = None
    return_date: Optional[date] = None
    fetch_ms: Optional[int] = None
    warnings: Tuple[str, ...] = HIDDEN_CITY_WARNINGS
    source: str = HIDDEN_CITY_SOURCE
    locale: str = FETCH_LANGUAGE
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(
            self,
            "currency",
            normalize_currency(self.currency) if self.currency else None,
        )
        _store_naive_utc(self)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "source": self.source,
            "locale": self.locale,
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "offers": [offer.to_dict() for offer in self.offers],
            "warnings": list(self.warnings),
        }
        if self.currency:
            payload["currency"] = self.currency
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.fetch_ms is not None:
            payload["fetch_ms"] = self.fetch_ms
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


@dataclass(frozen=True)
class AwardOffer:
    origin: str
    destination: str
    departure_date: date
    program: str
    points: int
    evidence: EvidenceLevel
    cabin: FlightCabin = "economy"
    taxes: Optional[float] = None
    currency: Optional[str] = None
    remaining_seats: Optional[int] = None
    booking_url: Optional[str] = None
    source: Optional[str] = None
    airline: Optional[str] = None
    return_date: Optional[date] = None

    def __post_init__(self) -> None:
        origin = _normalize_iata(self.origin, role="origin")
        destination = _normalize_iata(self.destination, role="destination")
        if origin == destination:
            raise ValueError("origin and destination must differ")
        if self.points <= 0:
            raise ValueError("points must be positive")
        _require_cabin(self.cabin)
        evidence = _require_evidence(self.evidence)
        if evidence == "confirmed" and self.source is None:
            raise ValueError("confirmed award evidence needs a named source")
        program = self.program.strip().casefold()
        if not program:
            raise ValueError("program is required")
        currency = normalize_currency(self.currency) if self.currency else None
        if self.taxes is not None and self.taxes < 0:
            raise ValueError("taxes must not be negative")
        if self.taxes is not None and currency is None:
            raise ValueError("taxes need a named currency")
        if self.remaining_seats is not None and self.remaining_seats < 0:
            raise ValueError("remaining_seats must not be negative")
        object.__setattr__(self, "origin", origin)
        object.__setattr__(self, "destination", destination)
        object.__setattr__(self, "program", program)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "currency", currency)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "origin": self.origin,
            "destination": self.destination,
            "departure_date": self.departure_date.isoformat(),
            "program": self.program,
            "points": self.points,
            "cabin": self.cabin,
            "evidence": self.evidence,
        }
        if self.taxes is not None:
            payload["taxes"] = self.taxes
        if self.currency:
            payload["currency"] = self.currency
        if self.remaining_seats is not None:
            payload["remaining_seats"] = self.remaining_seats
        if self.booking_url:
            payload["booking_url"] = self.booking_url
        if self.source:
            payload["source"] = self.source
        if self.airline:
            payload["airline"] = self.airline
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        return payload


@dataclass(frozen=True)
class PointsBalance:
    program: str
    balance: int

    def __post_init__(self) -> None:
        program = self.program.strip().upper()
        if not program:
            raise ValueError("program is required")
        if self.balance < 0:
            raise ValueError("balance must not be negative")
        object.__setattr__(self, "program", program)

    def to_dict(self) -> Mapping[str, object]:
        return {"program": self.program, "balance": self.balance}


@dataclass(frozen=True)
class TransferPath:
    currency: str = field(kw_only=True)
    program: str
    ratio: float
    points_needed: int
    effective_points: int
    covers: bool
    last_verified: date
    transfer_minutes: Optional[int] = None
    promo_bonus_percent: float = 0.0

    def __post_init__(self) -> None:
        if self.ratio <= 0:
            raise ValueError("ratio must be positive")
        if self.points_needed <= 0:
            raise ValueError("points_needed must be positive")
        if self.effective_points < 0:
            raise ValueError("effective_points must not be negative")
        if self.promo_bonus_percent < 0:
            raise ValueError("promo_bonus_percent must not be negative")

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "currency": self.currency,
            "program": self.program,
            "ratio": self.ratio,
            "points_needed": self.points_needed,
            "effective_points": self.effective_points,
            "covers": self.covers,
            "last_verified": self.last_verified.isoformat(),
        }
        if self.transfer_minutes is not None:
            payload["transfer_minutes"] = self.transfer_minutes
        if self.promo_bonus_percent:
            payload["promo_bonus_percent"] = self.promo_bonus_percent
        return payload


@dataclass(frozen=True)
class PlaybookStep:
    kind: Literal["action", "warning", "info"]
    title: str
    body: str

    def to_dict(self) -> Mapping[str, object]:
        return {"kind": self.kind, "title": self.title, "body": self.body}


@dataclass(frozen=True)
class AwardCompareReport:
    searched_at: datetime
    award: AwardOffer
    transfer_paths: Tuple[TransferPath, ...]
    playbook: Tuple[PlaybookStep, ...]
    cash_price: Optional[float] = None
    currency: Optional[str] = None
    cpp_cents: Optional[float] = None
    locale: str = FETCH_LANGUAGE
    schema_version: int = field(init=False, default=2)

    def __post_init__(self) -> None:
        if self.cash_price is not None:
            _require_positive_amount(self.cash_price, role="cash_price")
        if (self.cash_price is None) != (self.cpp_cents is None):
            raise ValueError("cash_price and cpp_cents must both be set or both omitted")
        currency = normalize_currency(self.currency) if self.currency else self.award.currency
        if self.cash_price is not None and currency is None:
            raise ValueError("cash_price needs a named currency")
        if self.award.currency and currency and self.award.currency != currency:
            raise ValueError("award currency and cash currency must match")
        object.__setattr__(self, "currency", currency)
        _store_naive_utc(self)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": _iso_z(self.searched_at),
            "locale": self.locale,
            "award": dict(self.award.to_dict()),
            "transfer_paths": [path.to_dict() for path in self.transfer_paths],
            "playbook": [step.to_dict() for step in self.playbook],
        }
        if self.currency:
            payload["currency"] = self.currency
        if self.cash_price is not None:
            payload["cash_price"] = self.cash_price
        if self.cpp_cents is not None:
            payload["cpp_cents"] = self.cpp_cents
        return payload


@dataclass(frozen=True)
class HotelRoomRate:
    """One bookable room rate as the provider listed it. Never ranked or merged."""

    title: str
    total_price: float
    price_per_night: Optional[float]
    taxes_and_fees: Optional[float]
    occupancy_limit: Optional[int]
    refundable: Optional[bool]
    free_cancellation: Optional[bool]
    bed_types: Tuple[str, ...]
    booking_link: Optional[str]

    def to_dict(self) -> Mapping[str, object]:
        return {
            "title": self.title,
            "total_price": self.total_price,
            "price_per_night": self.price_per_night,
            "taxes_and_fees": self.taxes_and_fees,
            "occupancy_limit": self.occupancy_limit,
            "refundable": self.refundable,
            "free_cancellation": self.free_cancellation,
            "bed_types": list(self.bed_types),
            "booking_link": self.booking_link,
        }


@dataclass(frozen=True)
class HotelRoomsReport:
    searched_at: datetime
    hotel_id: Optional[str]
    check_in: date
    check_out: date
    adults: int
    rooms: int
    currency: str = field(kw_only=True)
    provider: Literal["skiplagged"] = "skiplagged"
    price_basis: Literal["total_stay"] = field(init=False, default="total_stay")
    schema_version: int = field(init=False, default=2)
    requested_name: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    star_rating: Optional[float] = None
    review_rating: Optional[float] = None
    review_count: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    link: Optional[str] = None
    rates: Tuple[HotelRoomRate, ...] = ()
    error: Optional[SearchError] = None
    fetch_ms: Optional[int] = None
    # What the provider echoed, when it did. Absent means the answer did not say.
    answered_adults: Optional[int] = None
    answered_rooms: Optional[int] = None
    answered_check_in: Optional[date] = None
    answered_check_out: Optional[date] = None

    def __post_init__(self) -> None:
        _store_naive_utc(self)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "searched_at": _iso_z(self.searched_at),
            "currency": self.currency,
            "price_basis": self.price_basis,
            "hotel_id": self.hotel_id,
            "requested_name": self.requested_name,
            "name": self.name,
            "address": self.address,
            "city": self.city,
            "star_rating": self.star_rating,
            "review_rating": self.review_rating,
            "review_count": self.review_count,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "link": self.link,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "adults": self.adults,
            "rooms": self.rooms,
            "rates": [rate.to_dict() for rate in self.rates],
            "error": self.error.to_dict() if self.error else None,
            "fetch_ms": self.fetch_ms,
        }
        if self.answered_adults is not None:
            payload["answered_adults"] = self.answered_adults
        if self.answered_rooms is not None:
            payload["answered_rooms"] = self.answered_rooms
        if self.answered_check_in is not None:
            payload["answered_check_in"] = self.answered_check_in.isoformat()
        if self.answered_check_out is not None:
            payload["answered_check_out"] = self.answered_check_out.isoformat()
        return payload


@dataclass(frozen=True)
class StayBlock:
    """Consecutive nights with the same people. Pure bookkeeping, never advice."""

    check_in: date
    check_out: date
    people: Tuple[str, ...]

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def headcount(self) -> int:
        return len(self.people)

    def to_dict(self) -> Mapping[str, object]:
        return {
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "nights": self.nights,
            "headcount": self.headcount,
            "people": list(self.people),
        }


@dataclass(frozen=True)
class StayBlocksReport:
    blocks: Tuple[StayBlock, ...]
    people: Tuple[str, ...]
    person_nights: int

    def to_dict(self) -> Mapping[str, object]:
        return {
            "blocks": [block.to_dict() for block in self.blocks],
            "people": list(self.people),
            "nights": sum(block.nights for block in self.blocks),
            "person_nights": self.person_nights,
        }


@dataclass(frozen=True)
class StayShare:
    stay: str
    nights: int
    total: float

    def to_dict(self) -> Mapping[str, object]:
        return {"stay": self.stay, "nights": self.nights, "total": self.total}


@dataclass(frozen=True)
class PersonCost:
    name: str
    nights: int
    fee: float
    total: float
    shares: Tuple[StayShare, ...]

    def to_dict(self) -> Mapping[str, object]:
        return {
            "name": self.name,
            "nights": self.nights,
            "fee": self.fee,
            "total": self.total,
            "shares": [share.to_dict() for share in self.shares],
        }


@dataclass(frozen=True)
class SplitStay:
    name: str
    check_in: date
    check_out: date
    total: float
    person_nights: int
    rate_per_person_night: float

    def to_dict(self) -> Mapping[str, object]:
        return {
            "name": self.name,
            "check_in": self.check_in.isoformat(),
            "check_out": self.check_out.isoformat(),
            "nights": (self.check_out - self.check_in).days,
            "total": self.total,
            "person_nights": self.person_nights,
            "rate_per_person_night": self.rate_per_person_night,
        }


@dataclass(frozen=True)
class StayCostSplit:
    currency: str
    stays: Tuple[SplitStay, ...]
    people: Tuple[PersonCost, ...]
    total: float
    unallocated_nights: Tuple[date, ...]
    fee_per_person_night: Optional[float] = None

    def to_dict(self) -> Mapping[str, object]:
        return {
            "currency": self.currency,
            "fee_per_person_night": self.fee_per_person_night,
            "stays": [stay.to_dict() for stay in self.stays],
            "people": [person.to_dict() for person in self.people],
            "total": self.total,
            "unallocated_nights": [day.isoformat() for day in self.unallocated_nights],
        }
