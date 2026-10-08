"""Shared primitives: money, currency, typical helpers, owned-value checks,
JSON put helpers, search errors, and evidence and coverage types."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Mapping, Optional, Sequence, Tuple, get_args

from viajante.airports import is_known_iata

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


def _store_naive_utc(report: object) -> None:
    searched_at = report.searched_at  # type: ignore[attr-defined]
    if searched_at.tzinfo is not None:
        naive = searched_at.astimezone(timezone.utc).replace(tzinfo=None)
        object.__setattr__(report, "searched_at", naive)


def _iso_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _put_present(payload: dict[str, object], key: str, value: object) -> None:
    if value is not None:
        payload[key] = value


def _put_truthy(payload: dict[str, object], key: str, value: object) -> None:
    if value:
        payload[key] = value


def _store_nearby_label(obj: object) -> None:
    label = obj.nearby_label  # type: ignore[attr-defined]
    object.__setattr__(obj, "nearby_label", (label.strip() if label else None) or None)


EvidenceKnowledge = Literal["known", "unknown"]

UrlEvidenceKind = Literal["booking", "query", "none"]


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


def _deadline_stop(codes: Sequence[Optional[SearchErrorCode]]) -> Optional[str]:
    """Why a deadline made a scope incomplete, from the rows' own error codes."""
    cut = sum(code == SearchErrorCode.DEADLINE for code in codes)
    if not cut:
        return None
    return (
        f"{cut} of {len(codes)} units did not finish before deadline_seconds: "
        "not loaded, not proven empty"
    )


EvidenceLevel = Literal["confirmed", "user_supplied", "cached", "estimated"]

_EVIDENCE: tuple[EvidenceLevel, ...] = ("confirmed", "user_supplied", "cached", "estimated")


def _require_evidence(value: str) -> EvidenceLevel:
    if value not in _EVIDENCE:
        raise ValueError(f"invalid evidence: {value!r}")
    return value  # type: ignore[return-value]
