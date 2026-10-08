"""Cheapest-per-day calendar, flexible-window, and explore report types."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from statistics import median
from typing import Literal, Mapping, Optional, Sequence, Tuple

from viajante.models_common import (
    _VS_TYPICAL,
    EmptyReason,
    SearchCoverage,
    SearchError,
    SearchErrorCode,
    VsTypical,
    _deadline_stop,
    _iso_z,
    _put_present,
    _put_truthy,
    _require_typical_triple,
    _store_naive_utc,
    _store_nearby_label,
    _typical_json,
    format_typical_deal,
    vs_typical,
    vs_typical_pct,
)
from viajante.models_flights import FlightOffer, QueryFailure, StopsCompare

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
    # The day's cheapest eligible fare before any buffer: the summary and typical base. Not
    # serialized; price is the row's winner.
    day_fare: Optional[float] = None

    def __post_init__(self) -> None:
        if self.empty_reason is not None and self.status != "empty":
            raise ValueError("empty_reason applies only to empty rows")
        if (self.status != "ok" or self.price is None) and self.day_fare is not None:
            raise ValueError("empty/error rows omit the day fare")
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
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        _put_present(payload, "baggage_buffer", self.baggage_buffer)
        _put_present(payload, "duration_hours", self.duration_hours)
        _put_truthy(payload, "departure", self.departure)
        _put_truthy(payload, "arrival", self.arrival)
        _put_present(payload, "empty_reason", self.empty_reason)
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
        # A row built without its day fare (by hand, not by a search) carries only its price.
        object.__setattr__(
            self,
            "summary",
            owned_calendar_summary(
                [
                    (row.departure_date, row.price if row.day_fare is None else row.day_fare)
                    for row in self.days
                ]
            ),
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
        _put_present(payload, "nights", self.nights)
        if self.summary is not None:
            payload["summary"] = self.summary.to_dict()
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        _put_truthy(payload, "nearby_label", self.nearby_label)
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
        _put_present(payload, "nights", self.nights)
        if self.return_date is not None:
            payload["return_date"] = self.return_date.isoformat()
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        _put_truthy(payload, "nearby_label", self.nearby_label)
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
        _put_present(payload, "duration_hours", self.duration_hours)
        _put_truthy(payload, "departure", self.departure)
        _put_truthy(payload, "arrival", self.arrival)
        if self.stops_compare is not None:
            payload["stops_compare"] = self.stops_compare.to_dict()
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        _put_present(payload, "baggage_buffer", self.baggage_buffer)
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
        _put_truthy(payload, "google_flights_url", self.google_flights_url)
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        if self.pricing_errors:
            payload["pricing_errors"] = [row.to_dict() for row in self.pricing_errors]
        _put_present(payload, "empty_reason", self.empty_reason)
        _put_truthy(payload, "nearby_label", self.nearby_label)
        return payload
