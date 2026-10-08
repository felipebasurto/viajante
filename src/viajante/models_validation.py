"""Itinerary validation constraint status, checks, and report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping, Optional, Tuple

ConstraintStatus = Literal["pass", "fail", "unknown"]


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
