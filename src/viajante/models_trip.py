"""Owned trip total joining one flight search with one hotel search."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Mapping, Optional

from viajante.models_common import FETCH_LANGUAGE, _iso_z, _store_naive_utc
from viajante.models_flights import SearchReport
from viajante.models_hotels import HotelSearchReport


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
