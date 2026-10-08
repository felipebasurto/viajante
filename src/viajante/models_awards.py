"""Local award comparison, points balance, transfer path, and playbook types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Mapping, Optional, Tuple

from viajante.models_common import (
    FETCH_LANGUAGE,
    EvidenceLevel,
    FlightCabin,
    _iso_z,
    _normalize_iata,
    _put_present,
    _put_truthy,
    _require_cabin,
    _require_evidence,
    _require_positive_amount,
    _store_naive_utc,
    normalize_currency,
)


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
        _put_present(payload, "taxes", self.taxes)
        _put_truthy(payload, "currency", self.currency)
        _put_present(payload, "remaining_seats", self.remaining_seats)
        _put_truthy(payload, "booking_url", self.booking_url)
        _put_truthy(payload, "source", self.source)
        _put_truthy(payload, "airline", self.airline)
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
        _put_present(payload, "transfer_minutes", self.transfer_minutes)
        _put_truthy(payload, "promo_bonus_percent", self.promo_bonus_percent)
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
        _put_truthy(payload, "currency", self.currency)
        _put_present(payload, "cash_price", self.cash_price)
        _put_present(payload, "cpp_cents", self.cpp_cents)
        return payload
