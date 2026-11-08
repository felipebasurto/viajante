"""Split-ticket domain types and their JSON: the parts, the itinerary, the search report.

A split itinerary is only ever built from separately priced one-way quotes, so these types
carry the evidence and the warnings with it. The search that produces them lives in
``split``; the named filters that judge them live in ``split_filters``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Mapping, Optional

from viajante.models_common import SearchCoverage, SearchError
from viajante.models_flights import FlightOffer, FlightQuery, RoundTrip, SearchReport

SplitKind = Literal["hub", "mixed_one_ways"]
MAX_OTHER_CURRENCY_ROWS = 3
_ZERO_DECIMAL = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split())
_THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())


SELF_TRANSFER_WARNING = (
    "Separate tickets: a missed connection between them is not protected or rebooked by "
    "either airline, bags may need to be collected and checked in again, and each ticket "
    "must be confirmed on its own link."
)
TIMING_UNPROVEN_NOTE = (
    "Return timing is not verified: the outbound arrival or the return departure lacks an "
    "owned date, clock, or unambiguous airport-local time, so the return may leave before "
    "the outbound lands."
)
MIXED_WARNING = (
    "Two separate one-way tickets: confirm the fare, bags, and changes of each on its own "
    "link; changing or cancelling one does not change the other."
)


def round_money(value: float, currency: str) -> float:
    """Round a sum to the currency's minor unit (0.1 + 0.2 is 0.3, not 0.3000...04)."""
    digits = 0 if currency in _ZERO_DECIMAL else 3 if currency in _THREE_DECIMAL else 2
    return round(value, digits)


@dataclass(frozen=True)
class PackagedQuote:
    """Cheapest packaged offer the caller's search returned: the savings baseline."""

    price: float
    currency: str
    google_flights_url: Optional[str] = None

    def to_dict(self) -> Mapping[str, object]:
        return {
            "price": self.price,
            "currency": self.currency,
            "google_flights_url": self.google_flights_url,
            "basis": "cheapest_returned_offer",
        }


@dataclass(frozen=True)
class SplitPart:
    """One separately priced ticket: its own query, offer, currency, and booking link."""

    role: Literal["first_leg", "second_leg", "outbound", "return"]
    query: FlightQuery
    offer: FlightOffer
    currency: str
    query_url: Optional[str] = None

    @property
    def google_flights_url(self) -> Optional[str]:
        return self.offer.google_flights_url or self.query_url

    def to_dict(self) -> Mapping[str, object]:
        query = dict(self.query.to_dict())
        if self.query_url:
            query["google_flights_url"] = self.query_url
        return {
            "role": self.role,
            "query": query,
            "currency": self.currency,
            "price": self.offer.price,
            "google_flights_url": self.google_flights_url,
            "offer": self.offer.to_dict(self.currency),
        }


@dataclass(frozen=True)
class SplitItinerary:
    kind: SplitKind
    parts: tuple[SplitPart, ...]
    hub: Optional[str] = None
    connection_minutes: Optional[int] = None
    overnight_at_hub: bool = False
    packaged: Optional[PackagedQuote] = None
    timing_proven: Optional[bool] = None

    @property
    def currency(self) -> Optional[str]:
        currencies = {part.currency for part in self.parts}
        return next(iter(currencies)) if len(currencies) == 1 else None

    @property
    def total(self) -> Optional[float]:
        """Sum of the parts' fares, only when every part is in one owned currency."""
        if self.currency is None:
            return None
        return round_money(sum(part.offer.price for part in self.parts), self.currency)

    @property
    def baggage_buffer_total(self) -> int:
        """The parts' baggage buffers added up: a ranking add-on, never part of ``total``."""
        return sum(part.offer.baggage_buffer for part in self.parts)

    @property
    def savings(self) -> Optional[float]:
        """Packaged cheapest minus split total (negative when the split costs more)."""
        if (
            self.total is None
            or self.currency is None
            or self.packaged is None
            or self.packaged.currency != self.currency
        ):
            return None
        return round_money(self.packaged.price - self.total, self.currency)

    def to_dict(self) -> Mapping[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "split_ticket": True,
            "self_transfer": self.kind == "hub",
            "connection_protected": False,
            "tickets": len(self.parts),
            "currency": self.currency,
            "total": self.total,
            "total_note": None
            if self.currency
            else "parts are in different currencies; the total is unknown and nothing converts",
            "warning": SELF_TRANSFER_WARNING if self.kind == "hub" else MIXED_WARNING,
            "parts": [part.to_dict() for part in self.parts],
        }
        if self.kind == "hub":
            payload["hub"] = self.hub
            payload["connection_minutes"] = self.connection_minutes
            payload["overnight_at_hub"] = self.overnight_at_hub
        else:
            payload["timing_proven"] = bool(self.timing_proven)
            payload["timing_note"] = None if self.timing_proven else TIMING_UNPROVEN_NOTE
        saved = self.savings
        if saved is not None and self.packaged is not None:
            comparison: dict[str, object] = {
                "direction": "cheaper" if saved > 0 else "costlier" if saved < 0 else "same",
                "currency": self.currency,
                "packaged": self.packaged.to_dict(),
            }
            if saved >= 0:
                comparison["savings"] = saved
            else:
                comparison["extra_cost"] = -saved
            payload["vs_packaged"] = comparison
        return payload


@dataclass(frozen=True)
class SplitReport:
    searched_at: datetime
    kind: SplitKind
    query: FlightQuery | RoundTrip
    currency: str
    itineraries: tuple[SplitItinerary, ...]
    legs: tuple[Mapping[str, object], ...]
    coverage: SearchCoverage
    min_connection_minutes: Optional[int] = None
    allow_overnight: bool = False
    leg_max_stops: Optional[int] = None
    hubs: tuple[str, ...] = ()
    hubs_source: Optional[Literal["user", "packaged_layovers"]] = None
    skipped_hubs: tuple[Mapping[str, str], ...] = ()
    max_hubs: Optional[int] = None
    extra_searches: int = 0
    max_extra_searches: int = 0
    packaged_searched: bool = False
    packaged: Optional[PackagedQuote] = None
    packaged_report: Optional[SearchReport] = None
    rejected: Mapping[str, int] = field(default_factory=dict)
    error: Optional[SearchError] = None
    omitted_other_currency: int = 0
    schema_version: int = field(init=False, default=1)

    @property
    def warning(self) -> str:
        return SELF_TRANSFER_WARNING if self.kind == "hub" else MIXED_WARNING

    def to_dict(self) -> Mapping[str, object]:
        searched_at = self.searched_at.astimezone(timezone.utc).replace(tzinfo=None)
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "searched_at": searched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": self.kind,
            "query": dict(self.query.to_dict()),
            "currency": self.currency,
            "min_connection_minutes": self.min_connection_minutes,
            "allow_overnight": self.allow_overnight,
            "leg_max_stops": self.leg_max_stops,
            "hubs": list(self.hubs),
            "hubs_source": self.hubs_source,
            "skipped_hubs": [dict(row) for row in self.skipped_hubs],
            "max_hubs": self.max_hubs,
            "extra_searches": self.extra_searches,
            "max_extra_searches": self.max_extra_searches,
            "packaged_searched": self.packaged_searched,
            "packaged": self.packaged.to_dict() if self.packaged else None,
            "warning": self.warning,
            "itineraries": [row.to_dict() for row in self.itineraries],
            "rejected": dict(self.rejected),
            "other_currency_row_cap": MAX_OTHER_CURRENCY_ROWS,
            "omitted_other_currency": self.omitted_other_currency,
            "legs": [dict(row) for row in self.legs],
            "coverage": self.coverage.to_dict(),
        }
        if self.packaged_report is not None:
            payload["packaged_report"] = self.packaged_report.to_dict()
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload
