"""Opt-in split-ticket itineraries built only from separately fetched one-way quotes.

Two kinds, never mixed with packaged fares:

* ``hub``: origin->hub on one ticket plus hub->destination on another (a self-transfer).
* ``mixed_one_ways``: the cheapest outbound one-way plus the cheapest return one-way,
  compared with the packaged round-trip as a whole.

Every part is a real quote from its own search. A leg price is never derived from a
packaged price, never estimated, and never converted: totals are summed only when every
part carries the same owned currency, otherwise the parts stay and the total is unknown.
The search budget is capped, a recorded Google cooldown stops further searches, and
searches run sequentially in the calling process.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, Mapping, Optional, Sequence

from viajante.airports import airport_geo
from viajante.flights import (
    DEFAULT_TOP,
    _overlay_carrier_filters,
    parse_code_list,
    search_flights,
)
from viajante.models import (
    FlightOffer,
    FlightQuery,
    QueryFailure,
    QuerySuccess,
    RoundTrip,
    SearchCoverage,
    SearchError,
    SearchErrorCode,
    SearchReport,
)
from viajante.parsers import normalize_clock
from viajante.quote import first_origin_iata, resolve_quote_currency
from viajante.ratelimit import rate_limit_advice, rate_limit_status
from viajante.temporal import local_instant

# A planning default for a self-transfer, not provider evidence. Border control, bag
# re-check, or a terminal change can need longer: it is a parameter on purpose.
DEFAULT_MIN_CONNECTION_HOURS = 3.0
DEFAULT_SPLIT_HUBS = 3
# Hard ceiling: each hub costs 2 searches (3 with overnight), so at most 15 extra searches.
MAX_SPLIT_HUBS = 5
# Airports a caller may name explicitly as connection points (``via``).
MAX_VIA = 5
SPLIT_LEG_TOP = 10
# Rows kept from each non-requested currency group and from the unknown-total group.
MAX_OTHER_CURRENCY_ROWS = 3

# ISO 4217 minor units for the currencies that are not 2-digit; used only to round a sum.
_ZERO_DECIMAL = frozenset("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX VND VUV XAF XOF XPF".split())
_THREE_DECIMAL = frozenset("BHD IQD JOD KWD LYD OMR TND".split())

SplitKind = Literal["hub", "mixed_one_ways"]
SearchFn = Callable[..., SearchReport]

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


def _money(value: float, currency: str) -> float:
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
        return _money(sum(part.offer.price for part in self.parts), self.currency)

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
        return _money(self.packaged.price - self.total, self.currency)

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
            # Both amounts are non-negative so each direction is a verifiable positive number.
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


def _offer_currency(offer: FlightOffer, report: SearchReport) -> str:
    return offer.evidence.currency if offer.evidence is not None else report.currency


def _instant(
    day: Optional[date],
    clock: Optional[str],
    code: Optional[str],
    zone: Optional[str] = None,
) -> Optional[datetime]:
    """UTC instant of a local civil time.

    The segment's IANA zone wins. The airport catalogue is only the fallback when
    that zone is absent. None when the day, clock, or zone is missing, or the civil
    time is nonexistent or ambiguous (a DST change).
    """
    owned = normalize_clock(clock)
    if day is None or owned is None:
        return None
    if not zone:
        geo = airport_geo(code) if code else None
        zone = geo[0] if geo else None
    hour, minute = owned.split(":", 1)
    civil = datetime(day.year, day.month, day.day, int(hour), int(minute))
    return local_instant(civil, zone)


def _departure_point(
    offer: FlightOffer, query: FlightQuery
) -> tuple[Optional[date], Optional[datetime]]:
    """Local departure day and UTC instant (the queried date unless the segment says so)."""
    journey = offer.legs[0]
    first = journey.segments[0] if journey.segments else None
    day = (first.departure_date if first else None) or query.departure_date
    clock = first.departure if first and first.departure else journey.departure
    code = (first.origin if first else None) or query.origin
    zone = first.departure_timezone if first else None
    return day, _instant(day, clock, code, zone)


def _arrival_point(
    offer: FlightOffer, query: FlightQuery
) -> tuple[Optional[date], Optional[datetime]]:
    """Local arrival day and UTC instant, only from an owned arrival date."""
    last = offer.legs[0].segments[-1] if offer.legs[0].segments else None
    if last is None:
        return None, None
    return last.arrival_date, _instant(
        last.arrival_date,
        last.arrival,
        last.destination or query.destination,
        last.arrival_timezone,
    )


def _packaged_quote(packaged: Optional[SearchReport], currency: str) -> Optional[PackagedQuote]:
    """Cheapest packaged offer quoted in ``currency``; other currencies never compete."""
    if packaged is None:
        return None
    best: Optional[tuple[FlightOffer, str, Optional[str]]] = None
    for result in packaged.queries:
        if not isinstance(result, QuerySuccess):
            continue
        for offer in result.offers:
            if _offer_currency(offer, packaged) != currency:
                continue
            if best is None or offer.price < best[0].price:
                best = (offer, currency, result.google_flights_url)
    if best is None:
        return None
    offer, currency, query_url = best
    return PackagedQuote(offer.price, currency, offer.google_flights_url or query_url)


def layover_hubs(report: SearchReport, origin: str, destination: str) -> tuple[str, ...]:
    """Transfer airports seen in a packaged report's owned segments, most frequent first."""
    seen: Counter[str] = Counter()
    for result in report.queries:
        if not isinstance(result, QuerySuccess):
            continue
        for offer in result.offers:
            airports = {
                segment.destination
                for journey in offer.legs
                for segment in journey.segments[:-1]
                if segment.destination
            }
            seen.update(airports - {origin, destination})
    return tuple(code for code, _count in sorted(seen.items(), key=lambda row: (-row[1], row[0])))


def _hub_airport_problem(early: FlightOffer, late: FlightOffer, hub: str) -> Optional[str]:
    """Why two tickets do not meet at the hub airport (None when both segments name it)."""
    arrives = early.legs[0].segments[-1].destination if early.legs[0].segments else None
    departs = late.legs[0].segments[0].origin if late.legs[0].segments else None
    if not arrives or not departs:
        return "airport_unproven"
    if arrives.upper() != hub or departs.upper() != hub:
        return "airport_mismatch"
    return None


def pair_hub_quotes(
    first: SearchReport,
    hub: str,
    min_connection_minutes: int,
    packaged: Optional[PackagedQuote] = None,
) -> tuple[list[SplitItinerary], Counter[str]]:
    """Pair origin->hub quotes (first query) with hub->destination quotes (the rest).

    Ticket 1 must land at the hub airport and ticket 2 must leave from it (owned segment
    airports), then the gap between the two instants, each converted to UTC with the
    segment timezone (the airport catalogue only when the segment has none), must be at
    least the minimum connection. A missing timezone or a
    nonexistent or ambiguous local time (a DST change) leaves the gap unproven: the pair
    is rejected, never guessed.
    """
    rejected: Counter[str] = Counter()
    found: list[SplitItinerary] = []
    leg_one, *leg_two = first.queries
    if not isinstance(leg_one, QuerySuccess):
        return found, rejected
    for second in leg_two:
        if not isinstance(second, QuerySuccess):
            continue
        for early in leg_one.offers:
            arrival_day, arrives = _arrival_point(early, leg_one.query)  # type: ignore[arg-type]
            for late in second.offers:
                problem = _hub_airport_problem(early, late, hub)
                if problem is not None:
                    rejected[problem] += 1
                    continue
                departure_day, departs = _departure_point(late, second.query)  # type: ignore[arg-type]
                if arrives is None or departs is None:
                    rejected["timing_unproven"] += 1
                    continue
                gap = int((departs - arrives).total_seconds() // 60)
                if gap < min_connection_minutes:
                    rejected["connection_too_short"] += 1
                    continue
                found.append(
                    SplitItinerary(
                        kind="hub",
                        parts=(
                            SplitPart(
                                "first_leg",
                                leg_one.query,  # type: ignore[arg-type]
                                early,
                                _offer_currency(early, first),
                                leg_one.google_flights_url,
                            ),
                            SplitPart(
                                "second_leg",
                                second.query,  # type: ignore[arg-type]
                                late,
                                _offer_currency(late, first),
                                second.google_flights_url,
                            ),
                        ),
                        hub=hub,
                        connection_minutes=gap,
                        overnight_at_hub=departure_day != arrival_day,
                        packaged=packaged,
                    )
                )
    return found, rejected


def pair_mixed_one_ways(
    report: SearchReport, packaged: Optional[PackagedQuote] = None
) -> tuple[list[SplitItinerary], Counter[str]]:
    """Cheapest feasible outbound one-way plus return one-way (two queries in the report).

    A pair is feasible when the return departs after the outbound lands, compared in UTC
    through each segment timezone, or the airport catalogue when the segment has none.
    A missing zone or a DST-ambiguous or nonexistent local time leaves the timing
    unproven. A pair
    that provably overlaps is rejected. A pair without an owned arrival date stays eligible
    only when nothing proven exists in its currency, and is flagged ``timing_proven: false``
    with a ``timing_note``. One pair per currency (the cheapest total within it); prices in
    different currencies never compete. When no currency has both tickets the total is
    unknown and each ticket is the cheapest of its own search.
    """
    rejected: Counter[str] = Counter()
    out, back = report.queries
    if not isinstance(out, QuerySuccess) or not isinstance(back, QuerySuccess):
        return [], rejected
    proven: list[SplitItinerary] = []
    unproven: list[SplitItinerary] = []
    for early in out.offers:
        _, arrives = _arrival_point(early, out.query)  # type: ignore[arg-type]
        for late in back.offers:
            _, departs = _departure_point(late, back.query)  # type: ignore[arg-type]
            known = arrives is not None and departs is not None
            if known and departs <= arrives:  # type: ignore[operator]
                rejected["return_before_arrival"] += 1
                continue
            row = SplitItinerary(
                kind="mixed_one_ways",
                parts=(
                    SplitPart(
                        "outbound",
                        out.query,  # type: ignore[arg-type]
                        early,
                        _offer_currency(early, report),
                        out.google_flights_url,
                    ),
                    SplitPart(
                        "return",
                        back.query,  # type: ignore[arg-type]
                        late,
                        _offer_currency(late, report),
                        back.google_flights_url,
                    ),
                ),
                packaged=packaged,
                timing_proven=known,
            )
            (proven if known else unproven).append(row)
    chosen: list[SplitItinerary] = []
    for code in dict.fromkeys(row.currency for row in (*proven, *unproven)):
        if code is None:
            continue
        # Per currency: the cheapest proven pair, else the cheapest unproven one.
        pool = [row for row in proven if row.currency == code] or [
            row for row in unproven if row.currency == code
        ]
        chosen.append(min(pool, key=lambda row: row.total))  # type: ignore[arg-type,return-value]
    if not chosen:
        pool = proven or unproven
        if pool:
            chosen.append(
                min(pool, key=lambda row: (row.parts[0].offer.price, row.parts[1].offer.price))
            )
    dropped = len(unproven) - sum(row.timing_proven is False for row in chosen)
    if dropped:
        rejected["timing_unproven"] = dropped
    return chosen, rejected


def _rank(
    rows: Sequence[SplitItinerary], currency: str, top: int
) -> tuple[list[SplitItinerary], int]:
    """Order and cut within one currency at a time; raw sums of different currencies never compare.

    The requested currency comes first with ``top`` rows. Other currencies follow in
    first-seen order, then rows whose total is unknown (parts in different currencies);
    each of those groups is cut to at most ``MAX_OTHER_CURRENCY_ROWS``. Returns the rows
    and how many other-currency rows were left out.
    """
    groups: dict[Optional[str], list[SplitItinerary]] = {}
    for row in rows:
        groups.setdefault(row.currency, []).append(row)
    order = sorted(groups, key=lambda code: (code is None, code != currency))
    ranked: list[SplitItinerary] = []
    omitted = 0
    for code in order:
        # Unproven timing never sorts above proven timing.
        group = sorted(
            groups[code],
            key=lambda row: (
                row.timing_proven is False,
                row.total or 0.0,
                row.connection_minutes or 0,
            ),
        )
        limit = top if code == currency else min(top, MAX_OTHER_CURRENCY_ROWS)
        ranked.extend(group[:limit])
        if code != currency:
            omitted += max(len(group) - limit, 0)
    return ranked, omitted


def with_carrier_filters(
    query: FlightQuery | RoundTrip,
    *,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
) -> FlightQuery | RoundTrip:
    """The query with the caller's carrier filters applied to it."""
    (row,) = _overlay_carrier_filters(
        (query,),
        airlines=airlines,
        exclude_airlines=exclude_airlines,
        alliances=alliances,
        exclude_alliances=exclude_alliances,
    )
    return row  # type: ignore[return-value]


def _leg_rows(report: SearchReport, hub: Optional[str] = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for result in report.queries:
        row: dict[str, object] = {
            "origin": result.query.legs[0].origin,
            "destination": result.query.legs[0].destination,
            "departure_date": result.query.legs[0].departure_date.isoformat(),
            "status": result.status,
        }
        if hub is not None:
            row["hub"] = hub
        if isinstance(result, QuerySuccess):
            row["offers"] = len(result.offers)
            row["raw_count"] = result.raw_count
        elif isinstance(result, QueryFailure):
            row["error"] = dict(result.error.to_dict())
        rows.append(row)
    return rows


def _rate_limited(reports: Sequence[SearchReport]) -> Optional[SearchError]:
    for report in reports:
        for result in report.queries:
            if isinstance(result, QueryFailure) and result.error.rate_limited:
                return result.error
    state = rate_limit_status()
    if state is not None:
        return SearchError(
            SearchErrorCode.BLOCKED,
            rate_limit_advice(state, sent=False),
            rate_limited=True,
            retry_until=state["until"],
        )
    return None


def _one_way(source: FlightQuery | RoundTrip, origin: str, destination: str, day: date, stops: int):
    return FlightQuery(
        origin,
        destination,
        day,
        max_stops=stops,
        adults=source.adults,
        children=source.children,
        infants_in_seat=source.infants_in_seat,
        infants_on_lap=source.infants_on_lap,
        cabin=source.cabin,
        bags=source.bags,
        carry_on=source.carry_on,
        airlines=source.airlines,
        exclude_airlines=source.exclude_airlines,
        alliances=source.alliances,
        exclude_alliances=source.exclude_alliances,
    )


def validate_split_request(
    query: object,
    *,
    via: Optional[Sequence[str]] = None,
    max_hubs: Optional[int] = None,
    min_connection_hours: float = DEFAULT_MIN_CONNECTION_HOURS,
    leg_max_stops: int = 0,
    top: int = DEFAULT_TOP,
) -> tuple[SplitKind, Optional[tuple[str, ...]]]:
    """Reject a bad split request before any search runs. Returns the kind and named via."""
    if isinstance(query, FlightQuery):
        kind: SplitKind = "hub"
    elif isinstance(query, RoundTrip):
        kind = "mixed_one_ways"
    else:
        raise ValueError(
            "split tickets take one one-way query (via a hub) or one round-trip "
            "(mixed one-ways); multi-city is not supported"
        )
    if kind == "mixed_one_ways" and via:
        raise ValueError("via applies only to a one-way query")
    if max_hubs is not None and not 1 <= max_hubs <= MAX_SPLIT_HUBS:
        raise ValueError(f"max_hubs must be between 1 and {MAX_SPLIT_HUBS}")
    if not math.isfinite(min_connection_hours) or min_connection_hours < 0:
        raise ValueError("min_connection_hours must be a non-negative number")
    if leg_max_stops not in (0, 1, 2):
        raise ValueError("leg_max_stops must be 0, 1, or 2")
    if top <= 0:
        raise ValueError("top must be positive")
    named = parse_code_list(via, role="via")
    if named is not None and len(named) > MAX_VIA:
        raise ValueError(f"via accepts at most {MAX_VIA} airports (got {len(named)})")
    return kind, named


def search_split_tickets(
    query: FlightQuery | RoundTrip,
    *,
    packaged: Optional[SearchReport] = None,
    via: Optional[Sequence[str]] = None,
    max_hubs: Optional[int] = None,
    min_connection_hours: float = DEFAULT_MIN_CONNECTION_HOURS,
    allow_overnight: bool = False,
    leg_max_stops: int = 0,
    top: int = DEFAULT_TOP,
    fetch: str = "sweep",
    currency: Optional[str] = None,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    progress: Optional[Callable[[str], None]] = None,
    search: SearchFn = search_flights,
) -> SplitReport:
    """Search split tickets for one one-way (via hubs) or one round-trip (mixed one-ways).

    ``packaged`` is the caller's own packaged report for the same query: it is the savings
    baseline and, for a one-way without named ``via``, the source of hub candidates. When
    omitted it is searched once first. ``via`` names up to ``MAX_VIA`` connection airports
    to try (all of them unless ``max_hubs`` says fewer); unnamed, hubs are discovered from
    the packaged layovers. Extra searches are capped (``max_hubs`` hubs of 2
    queries, or 3 with ``allow_overnight``; mixed one-ways use 2) and stop at the first
    recorded rate limit.
    """
    kind, named_via = validate_split_request(
        query,
        via=via,
        max_hubs=max_hubs,
        min_connection_hours=min_connection_hours,
        leg_max_stops=leg_max_stops,
        top=top,
    )
    currency = resolve_quote_currency(currency, first_origin_iata(query))
    report_progress = progress or (lambda _: None)
    shop = dict(fetch=fetch, currency=currency, country=country, proxy=proxy, progress=progress)

    packaged_searched = packaged is None
    if packaged is None:
        packaged = search([query], top=top, **shop)
    baseline = _packaged_quote(packaged, currency)
    legs: list[Mapping[str, object]] = []
    itineraries: list[SplitItinerary] = []
    rejected: Counter[str] = Counter()
    reports: list[SearchReport] = [packaged]
    skipped: list[Mapping[str, str]] = []
    tried: list[str] = []
    extra = 0
    error: Optional[SearchError] = _rate_limited(reports)
    stopping = "rate_limited" if error else "completed_scope"
    hubs_source: Optional[Literal["user", "packaged_layovers"]] = None
    max_hubs = max_hubs or (len(named_via) if named_via else DEFAULT_SPLIT_HUBS)
    max_extra = 2 if kind == "mixed_one_ways" else max_hubs * (3 if allow_overnight else 2)

    if kind == "mixed_one_ways":
        if error is None:
            assert isinstance(query, RoundTrip)
            trips = [
                _one_way(
                    query, query.origin, query.destination, query.departure_date, query.max_stops
                ),
                _one_way(
                    query, query.destination, query.origin, query.return_date, query.max_stops
                ),
            ]
            report_progress("split: outbound and return as separate one-way tickets")
            report = search(trips, top=SPLIT_LEG_TOP, sort="fare", **shop)
            extra += len(trips)
            reports.append(report)
            legs.extend(_leg_rows(report))
            error = _rate_limited([report])
            stopping = "rate_limited" if error else stopping
            found, short = pair_mixed_one_ways(report, baseline)
            itineraries.extend(found)
            rejected.update(short)
    else:
        assert isinstance(query, FlightQuery)
        if named_via is not None:
            hubs_source, candidates = "user", named_via
        else:
            hubs_source, candidates = (
                "packaged_layovers",
                layover_hubs(packaged, query.origin, query.destination),
            )
        candidates = tuple(
            code for code in candidates if code not in (query.origin, query.destination)
        )
        if named_via is not None:
            max_extra = min(max_hubs, len(candidates)) * (3 if allow_overnight else 2)
        if not candidates and error is None:
            stopping = "no_hub_candidates"
        minutes = round(min_connection_hours * 60)
        for index, hub in enumerate(candidates):
            if index >= max_hubs:
                skipped.append({"hub": hub, "reason": "hub_cap"})
                stopping = "hub_cap" if stopping == "completed_scope" else stopping
                continue
            if error is not None:
                skipped.append({"hub": hub, "reason": "rate_limited"})
                continue
            trips = [_one_way(query, query.origin, hub, query.departure_date, leg_max_stops)]
            days = [query.departure_date]
            if allow_overnight:
                days.append(query.departure_date + timedelta(days=1))
            trips += [_one_way(query, hub, query.destination, day, leg_max_stops) for day in days]
            report_progress(f"split: {query.origin}-{hub}-{query.destination} as two tickets")
            report = search(trips, top=SPLIT_LEG_TOP, sort="fare", **shop)
            extra += len(trips)
            tried.append(hub)
            reports.append(report)
            legs.extend(_leg_rows(report, hub))
            found, short = pair_hub_quotes(report, hub, minutes, baseline)
            itineraries.extend(found)
            rejected.update(short)
            error = _rate_limited([report])
            stopping = "rate_limited" if error else stopping

    if query.price_cap is not None:
        # The cap is in the quote currency; a total in another currency or unknown cannot prove it.
        itineraries = [
            row
            for row in itineraries
            if row.currency == currency and row.total is not None and row.total <= query.price_cap
        ]
    ranked, omitted = _rank(itineraries, currency, top)
    leg_total = len(legs)
    ok = sum(row["status"] == "ok" for row in legs)
    empty = sum(
        row["status"] == "error" and row["error"]["code"] == "no_results"  # type: ignore[index]
        for row in legs
    )
    coverage = SearchCoverage(
        scope={
            "kind": "split_tickets",
            "mode": kind,
            "hubs": tried,
            "timing_unproven_kept": sum(row.timing_proven is False for row in ranked),
        },
        attempted=leg_total,
        succeeded=ok,
        empty=empty,
        failed=leg_total - ok - empty,
        complete=False,
        strategy="heuristic",
        stopping_reason=stopping,
        unsearched="hubs, dates, and ticket pairings outside the searched scope",
    )
    return SplitReport(
        searched_at=datetime.now(timezone.utc),
        kind=kind,
        query=query,
        currency=currency,
        itineraries=tuple(ranked),
        legs=tuple(legs),
        coverage=coverage,
        min_connection_minutes=round(min_connection_hours * 60) if kind == "hub" else None,
        allow_overnight=allow_overnight and kind == "hub",
        leg_max_stops=leg_max_stops if kind == "hub" else None,
        hubs=tuple(tried),
        hubs_source=hubs_source,
        skipped_hubs=tuple(skipped),
        max_hubs=max_hubs if kind == "hub" else None,
        extra_searches=extra,
        max_extra_searches=max_extra,
        packaged_searched=packaged_searched,
        packaged=baseline,
        packaged_report=packaged if packaged_searched else None,
        rejected=dict(rejected),
        error=error,
        omitted_other_currency=omitted,
    )
