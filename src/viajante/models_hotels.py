"""Hotel query, card, offer, outcome, search report, and room rate types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Literal, Mapping, Optional, Tuple, Union

from viajante.models_common import (
    FETCH_LANGUAGE,
    SearchCoverage,
    SearchError,
    SearchErrorCode,
    _deadline_stop,
    _iso_z,
    _put_present,
    _require_positive_amount,
    _store_naive_utc,
)


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
        _put_present(payload, "answered_adults", self.answered_adults)
        _put_present(payload, "answered_rooms", self.answered_rooms)
        if self.answered_check_in is not None:
            payload["answered_check_in"] = self.answered_check_in.isoformat()
        if self.answered_check_out is not None:
            payload["answered_check_out"] = self.answered_check_out.isoformat()
        return payload
