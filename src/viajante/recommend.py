"""Explainable flight recommendation over one query's parsed offers.

Offline and deterministic. It reads fields the provider returned and nothing
else: no reference prices, no FX, no invented fares or fare rules. Unknown
stays unknown and is worded as such.

Scoring dimensions and weights are ``SCORE_WEIGHTS``. Price and duration
penalties are how much worse an offer is than the best in the compared pool,
as a fraction capped at 1 (so 5 minutes or a few percent barely register).
The stops penalty is the stop count over the two-stop maximum. Unknown
duration or stop count takes the worst penalty of 1: unknown never earns
credit. The score is ``100 * (1 - weighted penalty)``, so higher is better.

Requirements the caller named are hard. When no offer meets all of them the
fewest requirements are relaxed; ties relax the earlier name in
``RELAX_ORDER`` first. The relaxed names are reported, never dropped silently.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import combinations
from typing import AbstractSet, Callable, Literal, Mapping, Optional, Sequence, Tuple

from viajante.models import FlightOffer
from viajante.parsers import clock_minutes

RECOMMENDATION_SCHEMA_VERSION = 2
# Hide connections slower than this multiple of the fastest nonstop/shortest
# elapsed time. Short-haul overnight hops drop out; long-haul 1-stops stay.
RANKED_SLOW_CONNECTION_FACTOR = 3.0
SCORE_WEIGHTS: Mapping[str, float] = {"price": 0.5, "duration": 0.35, "stops": 0.15}
# First name is relaxed first: clock preferences, then duration, then bags, stops last.
RELAX_ORDER: Tuple[str, ...] = (
    "arrive_before",
    "depart_after",
    "depart_window",
    "max_duration",
    "carry_on",
    "bags",
    "max_stops",
)
DEPARTURE_SLOTS: Tuple[Tuple[str, int, int], ...] = (
    ("night", 0, 360),
    ("morning", 360, 720),
    ("afternoon", 720, 1080),
    ("evening", 1080, 1440),
)
SHORTLIST_SIZE = 3
MAX_STOPS = 2  # the product searches at most two stops

Status = Literal["met", "unmet", "unknown"]
PriceComparison = Literal["compared", "skipped_mixed_currency", "skipped_unknown_currency"]


def _fastest_duration(offers: Sequence[FlightOffer]) -> Optional[float]:
    hours = [offer.duration_hours for offer in offers if offer.duration_hours is not None]
    return min(hours) if hours else None


def hide_slow_connections(offers: Sequence[FlightOffer]) -> Tuple[FlightOffer, ...]:
    """Drop connections many times slower than the fastest nonstop/shortest offer."""
    nonstops = [offer for offer in offers if offer.stops_count == 0]
    baseline = _fastest_duration(nonstops) or _fastest_duration(offers)
    if baseline is None or baseline <= 0:
        return tuple(offers)
    limit = baseline * RANKED_SLOW_CONNECTION_FACTOR
    kept: list[FlightOffer] = []
    for offer in offers:
        duration = offer.duration_hours
        connecting = offer.stops_count is not None and offer.stops_count > 0
        if connecting and duration is not None and duration > limit:
            continue
        kept.append(offer)
    return tuple(kept) if kept else tuple(offers)


def _cost(offer: FlightOffer) -> float:
    return offer.price + offer.baggage_buffer


def _duration_key(offer: FlightOffer) -> float:
    return offer.duration_hours if offer.duration_hours is not None else float("inf")


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Requirements:
    """Hard requirements the caller named. ``None`` means not stated."""

    max_stops: Optional[int] = None
    depart_window: Optional[Tuple[int, int]] = None
    depart_after: Optional[int] = None
    arrive_before: Optional[int] = None
    max_duration: Optional[float] = None
    carry_on: Optional[int] = None
    bags: Optional[int] = None

    def stated(self) -> Tuple[str, ...]:
        return tuple(name for name in RELAX_ORDER if getattr(self, name) is not None)

    def describe(self, name: str) -> object:
        value = getattr(self, name)
        if name == "depart_window":
            return f"{_hhmm(value[0])}-{_hhmm(value[1])}"
        if name in ("depart_after", "arrive_before"):
            return _hhmm(value)
        return value

    def to_dict(self) -> Mapping[str, object]:
        return {name: self.describe(name) for name in self.stated()}


def _status(offer: FlightOffer, name: str, value: object) -> Status:
    """Mirror the hard filters: an unknown clock or a direct-only ask is not proven."""
    if name == "max_stops":
        if offer.stops_count is None:
            return "unknown" if value >= 1 else "unmet"
        return "met" if offer.stops_count <= value else "unmet"
    if name in ("depart_window", "depart_after"):
        minutes = clock_minutes(offer.departure)
        if minutes is None:
            return "unmet"
        if name == "depart_window":
            return "met" if value[0] <= minutes <= value[1] else "unmet"
        return "met" if minutes >= value else "unmet"
    if name == "arrive_before":
        minutes = clock_minutes(offer.arrival)
        return "met" if minutes is not None and minutes <= value else "unmet"
    if name == "max_duration":
        if offer.duration_hours is None:
            return "unknown"
        return "met" if offer.duration_hours <= value else "unmet"
    known = offer.checked_bags if name == "bags" else offer.carry_on
    if known is None:
        return "unknown"
    return "met" if known >= value else "unmet"


def departure_slot(offer: FlightOffer) -> Optional[str]:
    minutes = clock_minutes(offer.departure)
    if minutes is None:
        return None
    return next(name for name, start, end in DEPARTURE_SLOTS if start <= minutes < end)


@dataclass(frozen=True)
class ShortlistEntry:
    labels: Tuple[str, ...]
    offer: FlightOffer
    score: float
    breakdown: Mapping[str, Optional[float]]
    requirements: Mapping[str, Status]
    highlights: Tuple[str, ...]
    tradeoffs: Tuple[str, ...]

    def to_dict(self, listed_ids: AbstractSet[str], currency: str) -> Mapping[str, object]:
        """Reference the offer by ``evidence_id``; embed it when ``offers`` does not list it.

        An offer without evidence cannot be matched, so it is embedded too.
        """
        evidence_id = self.offer.evidence.evidence_id if self.offer.evidence else None
        payload: dict[str, object] = {
            "labels": list(self.labels),
            "evidence_id": evidence_id,
            "google_flights_url": self.offer.google_flights_url,
            "score": self.score,
            "breakdown": dict(self.breakdown),
            "departure_slot": departure_slot(self.offer),
            "requirements": dict(self.requirements),
            "highlights": list(self.highlights),
            "tradeoffs": list(self.tradeoffs),
        }
        if evidence_id is None or evidence_id not in listed_ids:
            payload["offer"] = self.offer.to_dict(currency)
        return payload


@dataclass(frozen=True)
class Recommendation:
    requirements: Requirements
    relaxed_requirements: Tuple[str, ...]
    weights: Mapping[str, float]
    price_comparison: PriceComparison
    compared: int
    duplicates_removed: int
    slow_connections_hidden: int
    entries: Tuple[ShortlistEntry, ...]
    notes: Tuple[str, ...]
    schema_version: int = RECOMMENDATION_SCHEMA_VERSION

    def map_offers(self, fn: Callable[[FlightOffer], FlightOffer]) -> "Recommendation":
        return replace(
            self, entries=tuple(replace(entry, offer=fn(entry.offer)) for entry in self.entries)
        )

    def to_dict(self, listed_ids: AbstractSet[str], currency: str) -> Mapping[str, object]:
        """Compact payload: an offer in the query's ``offers`` is referenced, not embedded.

        ``listed_ids`` holds the ``evidence_id`` of every offer the query serializes; the
        currency formats an embedded offer. Weights are emitted only when price is not
        compared, since that is the only case where they differ from ``SCORE_WEIGHTS``.
        """
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "requirements": dict(self.requirements.to_dict()),
            "relaxed_requirements": list(self.relaxed_requirements),
            "price_comparison": self.price_comparison,
            "compared": self.compared,
            "duplicates_removed": self.duplicates_removed,
            "slow_connections_hidden": self.slow_connections_hidden,
            "shortlist": [entry.to_dict(listed_ids, currency) for entry in self.entries],
            "notes": list(self.notes),
        }
        if self.price_comparison != "compared":
            payload["weights"] = dict(self.weights)
        return payload


def _excess(values: Sequence[Optional[float]]) -> list[float]:
    """How much worse than the best known value, as a fraction capped at 1; unknown is 1."""
    known = [value for value in values if value is not None]
    best = min(known) if known else 0.0
    if best <= 0:
        return [0.0 if value == best else 1.0 for value in values]
    return [1.0 if value is None else min(1.0, (value - best) / best) for value in values]


def _minutes_text(hours: float) -> str:
    total = round(hours * 60)
    whole, rest = divmod(total, 60)
    if not whole:
        return f"{rest} min"
    return f"{whole} h {rest} min" if rest else f"{whole} h"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _duration_text(offer: FlightOffer) -> Optional[str]:
    if offer.duration:
        return offer.duration
    return _minutes_text(offer.duration_hours) if offer.duration_hours is not None else None


def _layover_text(offer: FlightOffer) -> str:
    layovers = offer.legs[0].layovers
    if not layovers:
        return "layover details not shown"
    parts = []
    for layover in layovers:
        city = layover.city or "layover city not shown"
        length = f"{layover.hours:g} h" if layover.hours is not None else "length not shown"
        parts.append(f"{city} ({length})")
    return "via " + ", ".join(parts)


def _facts(
    offer: FlightOffer,
    statuses: Mapping[str, Status],
    requirements: Requirements,
) -> tuple[Tuple[str, ...], Tuple[str, ...]]:
    good: list[str] = []
    bad: list[str] = []
    count = offer.stops_count
    if count is None:
        bad.append("Stop count not shown")
    elif count == 0:
        good.append("Nonstop")
    else:
        bad.append(f"{_plural(count, 'stop')}, {_layover_text(offer)}")
    duration = _duration_text(offer)
    if duration:
        good.append(f"Duration {duration}")
    else:
        bad.append("Duration not shown")
    if offer.departure and offer.arrival:
        good.append(f"Departs {offer.departure}, arrives {offer.arrival}")
    else:
        bad.append("Departure or arrival time not shown")
    if offer.airline:
        good.append(f"Carrier: {offer.airline}")
    else:
        bad.append("Carrier not shown")
    good.append(f"Fare {offer.price_text}")
    if offer.baggage_buffer:
        bad.append("Ranked with a caller-named baggage buffer; the bag fee itself is not verified")
    if offer.checked_bags is None:
        bad.append("Checked bag fee unknown")
    elif offer.checked_bags == 0:
        bad.append("No checked bag included")
    else:
        good.append(f"{_plural(offer.checked_bags, 'checked bag')} included")
    if offer.carry_on is None:
        bad.append("Carry-on not shown")
    elif offer.carry_on == 0:
        bad.append("No carry-on included")
    else:
        good.append(f"{_plural(offer.carry_on, 'carry-on bag')} included")
    bad.append("Fare rules (refund, change) not shown")
    for name, status in statuses.items():
        asked = f"{name} {requirements.describe(name)}"
        if status == "unmet":
            bad.append(f"Does not meet requested {asked}")
        elif status == "unknown":
            bad.append(f"Requested {asked} is not confirmed on this card")
    return tuple(good), tuple(bad)


def recommend_offers(
    offers: Sequence[FlightOffer],
    requirements: Optional[Requirements] = None,
    *,
    currency: Optional[str] = None,
    relax: bool = True,
) -> Optional[Recommendation]:
    """Pick one recommended offer and a varied shortlist from one query's offers.

    ``currency`` is the quote currency for offers that carry no evidence yet.
    Offers whose currency differs (or is unproven) are never price-compared.
    Returns None when nothing qualifies.
    """
    requirements = requirements or Requirements()
    stated = requirements.stated()
    rows = [
        (offer, {name: _status(offer, name, getattr(requirements, name)) for name in stated})
        for offer in offers
    ]
    relaxed: Tuple[str, ...] = ()
    candidates: list[tuple[FlightOffer, Mapping[str, Status]]] = []
    for size in range(len(stated) + 1 if relax else 1):
        for relaxed in combinations(stated, size):
            candidates = [
                row
                for row in rows
                if all(status != "unmet" or name in relaxed for name, status in row[1].items())
            ]
            if candidates:
                break
        if candidates:
            break
    if not candidates:
        return None
    statuses = {id(offer): status for offer, status in candidates}

    def offer_currency(offer: FlightOffer) -> Optional[str]:
        return offer.evidence.currency if offer.evidence else currency

    pool = _dedupe([offer for offer, _ in candidates], offer_currency)
    duplicates = len(candidates) - len(pool)
    compared = hide_slow_connections(pool)
    hidden = len(pool) - len(compared)
    currencies = {offer_currency(offer) for offer in compared}
    comparison: PriceComparison = "compared"
    if None in currencies:
        comparison = "skipped_unknown_currency"
    elif len(currencies) > 1:
        comparison = "skipped_mixed_currency"
    use_price = comparison == "compared"

    weights = {
        name: weight for name, weight in SCORE_WEIGHTS.items() if use_price or name != "price"
    }
    total = sum(weights.values())
    weights = {name: round(weights.get(name, 0.0) / total, 4) for name in SCORE_WEIGHTS}
    penalty = {
        "price": _excess([_cost(offer) for offer in compared]) if use_price else None,
        "duration": _excess([offer.duration_hours for offer in compared]),
        "stops": [
            1.0 if o.stops_count is None else min(1.0, o.stops_count / MAX_STOPS) for o in compared
        ],
    }
    scored: list[tuple[FlightOffer, float, Mapping[str, Optional[float]]]] = []
    for index, offer in enumerate(compared):
        parts = {
            name: round(values[index], 3) if values is not None else None
            for name, values in penalty.items()
        }
        weighted = sum(weights[name] * (value or 0.0) for name, value in parts.items())
        scored.append((offer, round(100 * (1 - weighted), 1), parts))
    scored.sort(key=lambda row: _rank_key(row[0], row[1], use_price))
    by_offer = {id(offer): (score, parts) for offer, score, parts in scored}
    ranked = [offer for offer, _, _ in scored]

    def cheap_key(offer: FlightOffer) -> tuple:
        return (_cost(offer), _rank_key(offer, 0.0, True))

    def fast_key(offer: FlightOffer) -> tuple:
        return _fast_key(offer, use_price)

    cheapest = min(ranked, key=cheap_key) if use_price else None
    known_fast = [offer for offer in ranked if offer.duration_hours is not None]
    fastest = min(known_fast, key=fast_key) if known_fast else None

    notes: list[str] = []
    chosen: list[tuple[FlightOffer, list[str]]] = [(ranked[0], ["top_score"])]

    def signatures() -> set[tuple[Optional[int], Optional[str]]]:
        return {_signature(offer) for offer, _ in chosen}

    for label, best, key, noun in (
        ("lowest_price", cheapest, cheap_key, "lowest fare"),
        ("shortest", fastest, fast_key, "shortest duration"),
    ):
        if best is None:
            continue
        held = next((labels for offer, labels in chosen if offer is best), None)
        if held is not None:
            held.append(label)
        elif _signature(best) not in signatures():
            chosen.append((best, [label]))
        else:
            alternative = min(
                (o for o in ranked if _signature(o) not in signatures()), key=key, default=None
            )
            if alternative is not None:
                chosen.append((alternative, [f"{label}_distinct"]))
            what = (
                f"{best.airline or 'carrier not shown'}, departs "
                f"{best.departure or 'time not shown'}, {best.price_text}"
                if noun == "lowest fare"
                else f"{_duration_text(best)}"
            )
            notes.append(
                f"The {noun} compared ({what}) shares its stop count and departure "
                "slot with another pick, so it is not listed separately."
            )
    while len(chosen) < SHORTLIST_SIZE:
        extra = next((o for o in ranked if _signature(o) not in signatures()), None)
        if extra is None:
            break
        chosen.append((extra, ["alternative"]))

    entries = []
    for offer, labels in chosen:
        good, bad = _facts(offer, statuses[id(offer)], requirements)
        score, parts = by_offer[id(offer)]
        entries.append(
            ShortlistEntry(
                labels=tuple(labels),
                offer=offer,
                score=score,
                breakdown=parts,
                requirements=statuses[id(offer)],
                highlights=good,
                tradeoffs=bad,
            )
        )
    if relaxed:
        notes.insert(
            0,
            "No offer met every stated requirement. Relaxed (fewest first, earlier names in "
            f"the fixed relax order first on ties): {', '.join(relaxed)}.",
        )
    if comparison != "compared":
        notes.append("Offers do not share a proven currency; fares are not compared or scored.")
    return Recommendation(
        requirements=requirements,
        relaxed_requirements=tuple(relaxed),
        weights=weights,
        price_comparison=comparison,
        compared=len(ranked),
        duplicates_removed=duplicates,
        slow_connections_hidden=hidden,
        entries=tuple(entries),
        notes=tuple(notes),
    )


def _signature(offer: FlightOffer) -> tuple[Optional[int], Optional[str]]:
    return (offer.stops_count, departure_slot(offer))


def _rank_key(offer: FlightOffer, score: float, price: bool) -> tuple:
    minutes = clock_minutes(offer.departure)
    return (
        -score,
        _cost(offer) if price else 0.0,
        _duration_key(offer),
        minutes if minutes is not None else 24 * 60,
        offer.airline or "",
        offer.price_text,
    )


def _fast_key(offer: FlightOffer, price: bool) -> tuple:
    stops = offer.stops_count if offer.stops_count is not None else 99
    return (_duration_key(offer), stops, _rank_key(offer, 0.0, price))


def _dedupe(
    offers: Sequence[FlightOffer], currency_of: Callable[[FlightOffer], Optional[str]]
) -> list[FlightOffer]:
    """Same carrier, clocks and stop count are one flight; keep the cheaper fare."""
    kept: dict[tuple, FlightOffer] = {}
    for offer in offers:
        key = (currency_of(offer), offer.airline, offer.departure, offer.arrival, offer.stops_count)
        held = kept.get(key)
        if held is None or (_cost(offer), _duration_key(offer)) < (
            _cost(held),
            _duration_key(held),
        ):
            kept[key] = offer
    return list(kept.values())
