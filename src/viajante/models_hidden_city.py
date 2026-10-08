"""Skiplagged hidden-city offer and report types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Mapping, Optional, Tuple

from viajante.models_common import (
    FETCH_LANGUAGE,
    EvidenceLevel,
    SearchError,
    _iso_z,
    _normalize_iata,
    _put_present,
    _put_truthy,
    _require_evidence,
    _require_positive_amount,
    _store_naive_utc,
    normalize_currency,
)

HIDDEN_CITY_SOURCE = "skiplagged"

HIDDEN_CITY_WARNINGS: tuple[str, ...] = (
    "Hidden-city / point-beyond tickets often violate airline conditions of carriage.",
    "Do not check a bag to the ticketed destination if you intend to leave at a layover.",
    "Missing a segment can cancel remaining flights on the same ticket.",
    "Irregular operations can rebook you onto a routing that skips the layover city.",
    "Confirm the fare and terms on the booking link before paying. Viajante does not book.",
)


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
        _put_truthy(payload, "booking_url", self.booking_url)
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
        _put_truthy(payload, "currency", self.currency)
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        _put_present(payload, "fetch_ms", self.fetch_ms)
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload
