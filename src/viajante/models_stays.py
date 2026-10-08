"""Offline stay-block planning and per-person stay cost split types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping, Optional, Tuple


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
