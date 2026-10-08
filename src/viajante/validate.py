"""Deterministic validation of selected, owned flight offers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Mapping, Optional, Sequence

from viajante.flight_filters import _overnight_from_owned_clocks
from viajante.models import ConstraintCheck, ItineraryValidationReport, normalize_currency
from viajante.parsers import clock_minutes as _clock_minutes
from viajante.parsers import parse_stops_count
from viajante.temporal import local_instant, segment_instant

_SUPPORTED_CONSTRAINTS = {
    "arrival_deadline",
    "chronological",
    "arrive_before",
    "bags",
    "carry_on",
    "claimed_fare_total",
    "depart_after",
    "max_layover",
    "max_segments",
    "max_stay_days",
    "max_stops",
    "min_layover",
    "min_stay_days",
    "minimum_savings",
    "no_airport_changes",
    "no_consecutive_same_operator",
    "no_overnight",
    "relaxations",
    "require_reproducible",
    "required_leg_count",
    "travel_end",
    "travel_start",
}


def _mapping(value: object, *, role: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{role} must be an object")
    return value


def _items(value: object, *, role: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{role} must be an array")
    return tuple(value)


def _number(value: object, *, role: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{role} must be a number")
    number = float(value)
    if number < minimum:
        raise ValueError(f"{role} must be at least {minimum:g}")
    return number


def _integer(value: object, *, role: str, minimum: int = 0) -> int:
    number = _number(value, role=role, minimum=float(minimum))
    if not number.is_integer():
        raise ValueError(f"{role} must be an integer")
    return int(number)


def _boolean(value: object, *, role: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{role} must be true or false")
    return value


def _iso_date(value: object, *, role: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{role} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{role} must be an ISO date") from exc


def _clock(value: object, *, role: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{role} must be HH:MM")
    minutes = _clock_minutes(value)
    if minutes is None:
        raise ValueError(f"{role} must be HH:MM")
    return minutes


def _status(
    constraint: str,
    status: str,
    detail: str,
) -> ConstraintCheck:
    return ConstraintCheck(constraint=constraint, status=status, detail=detail)  # type: ignore[arg-type]


def _query_without_url(query: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in query.items() if key != "google_flights_url"}


def _selected_rows(
    legs: Sequence[Mapping[str, object]],
) -> tuple[tuple[Mapping[str, object], Mapping[str, object]], ...]:
    selected: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for index, raw in enumerate(legs):
        row = _mapping(raw, role=f"legs[{index}]")
        if row.get("status") != "ok":
            continue
        query = _mapping(row.get("query"), role=f"legs[{index}].query")
        offer = _mapping(row.get("offer"), role=f"legs[{index}].offer")
        selected.append((query, offer))
    return tuple(selected)


def _journey_legs(offer: Mapping[str, object]) -> Optional[tuple[Mapping[str, object], ...]]:
    value = offer.get("legs")
    if value is None:
        return None
    return tuple(_mapping(item, role="offer.legs[]") for item in _items(value, role="offer.legs"))


def _segments(offer: Mapping[str, object]) -> Optional[tuple[Mapping[str, object], ...]]:
    journey_legs = _journey_legs(offer)
    if not journey_legs:
        return None
    segments: list[Mapping[str, object]] = []
    for journey in journey_legs:
        value = journey.get("segments")
        if value is None:
            return None
        rows = tuple(
            _mapping(item, role="offer.legs[].segments[]")
            for item in _items(value, role="offer.legs[].segments")
        )
        if not rows:
            return None
        segments.extend(rows)
    return tuple(segments)


def _query_legs(query: Mapping[str, object]) -> Optional[tuple[Mapping[str, object], ...]]:
    kind = query.get("trip", "one-way")
    if kind == "multi":
        value = query.get("legs")
        if value is None:
            return None
        return tuple(
            _mapping(item, role="query.legs[]") for item in _items(value, role="query.legs")
        )
    if kind == "rt":
        return query, {
            "origin": query.get("destination"),
            "destination": query.get("origin"),
            "departure_date": query.get("return_date"),
        }
    return (query,) if kind == "one-way" else None


def _complete_journeys(query: Mapping[str, object], offer: Mapping[str, object]) -> bool:
    expected = _query_legs(query)
    observed = _journey_legs(offer)
    return bool(expected and observed and len(expected) == len(observed))


def _query_dates(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> Optional[tuple[date, ...]]:
    dates: list[date] = []
    for query, _offer in selected:
        journeys = _query_legs(query)
        if not journeys:
            return None
        for journey in journeys:
            value = journey.get("departure_date")
            if not isinstance(value, str):
                return None
            try:
                dates.append(date.fromisoformat(value))
            except ValueError:
                return None
    return tuple(dates)


def _evidence_check(legs: Sequence[Mapping[str, object]]) -> ConstraintCheck:
    if any(row.get("status") != "ok" for row in legs):
        return _status("evidence", "fail", "an error or missing result was selected")
    evidence = []
    for index, row in enumerate(legs):
        offer = _mapping(row.get("offer"), role=f"legs[{index}].offer")
        value = offer.get("evidence")
        if value is None:
            return _status("evidence", "unknown", "one or more offers lack provenance")
        evidence.append(_mapping(value, role=f"legs[{index}].offer.evidence"))
    if len({item.get("evidence_id") for item in evidence}) != len(evidence):
        return _status("evidence", "fail", "the same owned offer was selected more than once")
    return _status("evidence", "pass", "every selected offer has distinct owned provenance")


def _query_binding_check(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> ConstraintCheck:
    for query, offer in selected:
        evidence_value = offer.get("evidence")
        if evidence_value is None:
            return _status("query_binding", "unknown", "offer provenance is missing")
        evidence = _mapping(evidence_value, role="offer.evidence")
        evidence_query = evidence.get("query")
        if evidence_query is None:
            return _status("query_binding", "unknown", "evidence query is missing")
        if _query_without_url(query) != _query_without_url(
            _mapping(evidence_query, role="offer.evidence.query")
        ):
            return _status("query_binding", "fail", "an offer was moved to a different query")
    return _status("query_binding", "pass", "every offer matches its evidence query")


def _route_check(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> ConstraintCheck:
    for (left, _), (right, _) in zip(selected, selected[1:], strict=False):
        destination = left.get("destination")
        origin = right.get("origin")
        if not isinstance(destination, str) or not isinstance(origin, str):
            return _status("route_continuity", "unknown", "query endpoints are missing")
        if destination != origin:
            return _status(
                "route_continuity",
                "fail",
                f"{destination} does not connect to {origin}",
            )
    return _status("route_continuity", "pass", "selected query endpoints connect")


def _currency_and_totals(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
    currency: Optional[str],
) -> tuple[Optional[str], Optional[float], Optional[float], ConstraintCheck]:
    named = normalize_currency(currency) if currency else None
    currencies: list[str] = []
    prices: list[float] = []
    ranked: list[float] = []
    for _query, offer in selected:
        evidence_value = offer.get("evidence")
        evidence_currency = None
        if evidence_value is not None:
            evidence = _mapping(evidence_value, role="offer.evidence")
            source_currency = evidence.get("currency")
            if source_currency is not None:
                if not isinstance(source_currency, str):
                    raise ValueError("offer.evidence.currency must be a currency code")
                evidence_currency = normalize_currency(source_currency)
        explicit_currency = offer.get("currency")
        if explicit_currency is not None and not isinstance(explicit_currency, str):
            raise ValueError("offer currency must be a currency code")
        if (
            explicit_currency is not None
            and evidence_currency is not None
            and normalize_currency(explicit_currency) != evidence_currency
        ):
            return (
                None,
                None,
                None,
                _status("currency", "fail", "offer currency differs from its evidence"),
            )
        value = explicit_currency or evidence_currency or named
        if not isinstance(value, str):
            return (
                None,
                None,
                None,
                _status("currency", "unknown", "an offer currency is missing"),
            )
        currencies.append(normalize_currency(value))
        price = offer.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            return (
                None,
                None,
                None,
                _status("currency", "unknown", "an owned positive fare is missing"),
            )
        prices.append(float(price))
        buffer = offer.get("baggage_buffer", 0)
        if isinstance(buffer, bool) or not isinstance(buffer, (int, float)) or buffer < 0:
            raise ValueError("offer baggage_buffer must be a non-negative number")
        ranked.append(float(price) + float(buffer))
    unique = set(currencies)
    if len(unique) > 1 or (named and unique and unique != {named}):
        return (
            None,
            None,
            None,
            _status("currency", "fail", "selected offers use different currencies"),
        )
    if not selected:
        return named, None, None, _status("currency", "unknown", "no owned offers selected")
    owned = next(iter(unique))
    return (
        owned,
        sum(prices),
        sum(ranked),
        _status("currency", "pass", f"all fares are in {owned}"),
    )


def _segment_metrics(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> tuple[int, Optional[int], Optional[tuple[Mapping[str, object], ...]]]:
    journey_count = 0
    all_segments: list[Mapping[str, object]] = []
    complete = True
    for query, offer in selected:
        journeys = _journey_legs(offer)
        journey_count += len(journeys or ())
        segments = _segments(offer)
        if segments is None or not _complete_journeys(query, offer):
            complete = False
        else:
            all_segments.extend(segments)
    if not complete:
        return journey_count, None, None
    return journey_count, len(all_segments), tuple(all_segments)


def _segment_clock_check(
    constraint: str,
    segments: Optional[Sequence[Mapping[str, object]]],
    *,
    bound: int,
    field: str,
    before: bool,
) -> ConstraintCheck:
    if segments is None:
        return _status(constraint, "unknown", "individual segment clocks are missing")
    for segment in segments:
        value = segment.get(field)
        minutes = _clock_minutes(value if isinstance(value, str) else None)
        if minutes is None:
            return _status(constraint, "unknown", f"a segment {field} clock is missing")
        if (before and minutes > bound) or (not before and minutes < bound):
            return _status(constraint, "fail", f"segment {field} {value} violates the bound")
    return _status(constraint, "pass", "all owned segment clocks satisfy the bound")


def _segment_airport_check(
    segments: Optional[Sequence[Mapping[str, object]]],
) -> ConstraintCheck:
    if segments is None:
        return _status("no_airport_changes", "unknown", "segment airports are missing")
    for left, right in zip(segments, segments[1:], strict=False):
        destination = left.get("destination")
        origin = right.get("origin")
        if not isinstance(destination, str) or not isinstance(origin, str):
            return _status("no_airport_changes", "unknown", "a segment airport is missing")
        if destination != origin:
            return _status(
                "no_airport_changes",
                "fail",
                f"connection changes airport from {destination} to {origin}",
            )
    return _status("no_airport_changes", "pass", "owned segment airports connect")


def _operator_check(
    segments: Optional[Sequence[Mapping[str, object]]],
) -> ConstraintCheck:
    if segments is None:
        return _status(
            "no_consecutive_same_operator",
            "unknown",
            "individual segment operators are missing",
        )
    operators: list[str] = []
    for segment in segments:
        airline = segment.get("airline")
        if not isinstance(airline, str) or not airline.strip():
            return _status(
                "no_consecutive_same_operator",
                "unknown",
                "a segment operator is missing",
            )
        operators.append(airline.strip().casefold())
    for left, right in zip(operators, operators[1:], strict=False):
        if left == right:
            return _status(
                "no_consecutive_same_operator",
                "fail",
                "adjacent segments use the same operator",
            )
    return _status(
        "no_consecutive_same_operator",
        "pass",
        "adjacent owned segment operators differ",
    )


def _overnight_check(
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
) -> ConstraintCheck:
    for _query, offer in selected:
        journeys = _journey_legs(offer)
        if not journeys:
            return _status("no_overnight", "unknown", "individual segment clocks are missing")
        for journey in journeys:
            raw = journey.get("segments")
            if raw is None:
                return _status("no_overnight", "unknown", "individual segment clocks are missing")
            segments = tuple(
                _mapping(item, role="offer.legs[].segments[]")
                for item in _items(raw, role="offer.legs[].segments")
            )
            if not segments:
                return _status("no_overnight", "unknown", "individual segment clocks are missing")
            for inbound, outbound in zip(segments, segments[1:], strict=False):
                arrival = inbound.get("arrival")
                departure = outbound.get("departure")
                overnight = _overnight_from_owned_clocks(
                    arrival if isinstance(arrival, str) else None,
                    departure if isinstance(departure, str) else None,
                    None,
                )
                if overnight is None:
                    return _status("no_overnight", "unknown", "a connection night is not provable")
                if overnight:
                    return _status("no_overnight", "fail", "an owned connection crosses a night")
    return _status("no_overnight", "pass", "owned connections do not cross a night")


def _layover_check(
    constraint: str,
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
    *,
    bound: float,
    maximum: bool,
) -> ConstraintCheck:
    values: list[float] = []
    for query, offer in selected:
        if not _complete_journeys(query, offer):
            return _status(constraint, "unknown", "journey evidence is incomplete")
        journeys = _journey_legs(offer)
        for journey in journeys:
            text = journey.get("stops")
            stops = parse_stops_count(text) if isinstance(text, str) else None
            if stops is None and len(journeys) == 1:
                stops = offer.get("stops_count")
            if isinstance(stops, bool) or not isinstance(stops, int):
                return _status(constraint, "unknown", "a journey stop count is missing")
            layovers = journey.get("layovers")
            if layovers is None:
                if stops > 0:
                    return _status(constraint, "unknown", "layover evidence is missing")
                continue
            rows = _items(layovers, role="offer.legs[].layovers")
            if len(rows) < stops:
                return _status(constraint, "unknown", "layover evidence is incomplete")
            for raw in rows:
                row = _mapping(raw, role="offer.legs[].layovers[]")
                hours = row.get("hours")
                if isinstance(hours, bool) or not isinstance(hours, (int, float)):
                    return _status(constraint, "unknown", "layover hours are missing")
                values.append(float(hours))
    for value in values:
        if (maximum and value > bound) or (not maximum and value < bound):
            return _status(constraint, "fail", f"layover {value:g}h violates the bound")
    return _status(constraint, "pass", "owned layovers satisfy the bound")


def _baggage_check(
    constraint: str,
    selected: Sequence[tuple[Mapping[str, object], Mapping[str, object]]],
    *,
    requested: int,
    field: str,
) -> ConstraintCheck:
    for _query, offer in selected:
        value = offer.get(field)
        if value is None:
            return _status(
                constraint,
                "unknown",
                f"{field} is missing; needs_bag_verify does not prove inclusion",
            )
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"offer {field} must be an integer")
        if value < requested:
            return _status(constraint, "fail", f"owned {field} is below the request")
    return _status(constraint, "pass", f"owned {field} satisfies the request")


def _temporal_complete(selected, complete):
    if not complete:
        return False
    for query, offer in selected:
        expected = len(query.get("legs", ())) or (2 if query.get("trip") == "rt" else 1)
        if len(_journey_legs(offer) or ()) != expected:
            return False
    return True


def _chronological_check(segments, complete):
    if not complete or not segments:
        return _status("chronological", "unknown", "complete segment times are missing")
    previous = None
    unknown = False
    for segment in segments:
        departure = segment_instant(segment, "departure")
        arrival = segment_instant(segment, "arrival")
        if departure is None or arrival is None:
            unknown = True
        if departure is not None and arrival is not None and arrival < departure:
            return _status("chronological", "fail", "a segment arrives before it departs in UTC")
        if previous is not None and departure is not None and departure < previous:
            return _status("chronological", "fail", "selected segments overlap in UTC")
        previous = arrival
    return _status(
        "chronological",
        "unknown" if unknown else "pass",
        "missing or ambiguous civil times"
        if unknown
        else "owned instants are in sequence; connection protection and margins are unverified",
    )


def _arrival_deadline_check(segments, complete, value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("arrival_deadline must be an ISO date and time")
    try:
        deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("arrival_deadline must be an ISO date and time") from exc
    if not complete or not segments:
        return _status("arrival_deadline", "unknown", "final segment is missing")
    final = segments[-1]
    # An explicit offset is already an instant. Compare it in UTC even when the
    # arrival airport's zone is a different offset. A naive deadline is local
    # civil time at that airport; ambiguous or missing zones stay unknown.
    if deadline.tzinfo is not None:
        bound = deadline.astimezone(timezone.utc)
    else:
        bound = local_instant(deadline, final.get("arrival_timezone"))
    arrival = segment_instant(final, "arrival")
    if bound is None or arrival is None:
        return _status(
            "arrival_deadline", "unknown", "final arrival or local deadline is missing or ambiguous"
        )
    return _status(
        "arrival_deadline",
        "pass" if arrival <= bound else "fail",
        "final arrival meets the deadline"
        if arrival <= bound
        else "final arrival exceeds the deadline",
    )


def _stay_check(constraint, selected, complete, bound):
    journeys = [journey for _, offer in selected for journey in (_journey_legs(offer) or ())]
    if len(journeys) < 2:
        return _status(constraint, "unknown", "no intervening stay between selected journeys")
    if not complete:
        return _status(constraint, "unknown", "complete journeys are missing")
    unknown = False
    for left, right in zip(journeys, journeys[1:], strict=False):
        inbound, outbound = left.get("segments"), right.get("segments")
        if not inbound or not outbound:
            unknown = True
            continue
        arrival, departure = inbound[-1], outbound[0]
        if not arrival.get("destination") or arrival.get("destination") != departure.get("origin"):
            unknown = True
            continue
        a, d = segment_instant(arrival, "arrival"), segment_instant(departure, "departure")
        if a is None or d is None:
            unknown = True
            continue
        days = (
            _iso_date(departure.get("departure_date"), role="departure_date")
            - _iso_date(arrival.get("arrival_date"), role="arrival_date")
        ).days
        if (
            d < a
            or (constraint == "min_stay_days" and days < bound)
            or (constraint == "max_stay_days" and days > bound)
        ):
            return _status(
                constraint, "fail", f"owned stay of {days} local days violates the bound"
            )
    return _status(
        constraint,
        "unknown" if unknown else "pass",
        "a stay interval is unproven"
        if unknown
        else "owned intervening stays satisfy the bound; no stay inferred after the final flight",
    )


def validate_itinerary(
    legs: Sequence[Mapping[str, object]],
    constraints: Mapping[str, object],
    *,
    currency: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ItineraryValidationReport:
    """Validate selected v2 offer rows without fetching or filling evidence gaps."""

    rows = tuple(_mapping(row, role=f"legs[{index}]") for index, row in enumerate(legs))
    scenario = dict(_mapping(constraints, role="constraints"))
    unknown_keys = set(scenario) - _SUPPORTED_CONSTRAINTS
    if unknown_keys:
        names = ", ".join(sorted(unknown_keys))
        raise ValueError(f"unsupported itinerary constraints: {names}")
    raw_relaxations = scenario.pop("relaxations", ())
    relaxations = tuple(
        _mapping(item, role="constraints.relaxations[]")
        for item in _items(raw_relaxations, role="constraints.relaxations")
    )
    selected = _selected_rows(rows)
    checks = [
        _evidence_check(rows),
        _query_binding_check(selected),
        _route_check(selected),
    ]
    owned_currency, fare_total, ranked_total, currency_check = _currency_and_totals(
        selected, currency
    )
    checks.append(currency_check)
    journeys_complete = all(_complete_journeys(query, offer) for query, offer in selected)
    checks.append(
        _status(
            "journey_completeness",
            "pass" if journeys_complete else "unknown",
            "all queried journeys are present"
            if journeys_complete
            else "queried journeys are missing",
        )
    )
    journey_count, segment_count, segments = _segment_metrics(selected)
    dates = _query_dates(selected)
    required_count = None
    if "required_leg_count" in scenario:
        required_count = _integer(
            scenario["required_leg_count"], role="required_leg_count", minimum=1
        )
        status = "pass" if len(rows) == required_count else "fail"
        checks.append(
            _status(
                "required_leg_count",
                status,
                f"selected {len(rows)} of {required_count} required rows",
            )
        )
    complete = journeys_complete and (required_count is None or len(rows) == required_count)

    if "travel_start" in scenario or "travel_end" in scenario:
        if "travel_start" not in scenario or "travel_end" not in scenario:
            raise ValueError("travel_start and travel_end must be named together")
        start = _iso_date(scenario["travel_start"], role="travel_start")
        end = _iso_date(scenario["travel_end"], role="travel_end")
        if end < start:
            raise ValueError("travel_end must not be before travel_start")
        if dates is None:
            checks.append(_status("travel_window", "unknown", "query dates are missing"))
        elif any(day < start or day > end for day in dates):
            checks.append(
                _status("travel_window", "fail", "a selected query is outside the window")
            )
        else:
            checks.append(_status("travel_window", "pass", "all query dates are in the window"))

    if "claimed_fare_total" in scenario:
        claimed = _number(scenario["claimed_fare_total"], role="claimed_fare_total")
        if fare_total is None:
            checks.append(_status("fare_total", "unknown", "same-currency fares are incomplete"))
        elif abs(claimed - fare_total) > 0.005:
            checks.append(
                _status(
                    "fare_total",
                    "fail",
                    f"claimed {claimed:g}; owned fares total {fare_total:g}",
                )
            )
        else:
            checks.append(_status("fare_total", "pass", "claimed and owned totals match"))

    if "max_segments" in scenario:
        maximum = _integer(scenario["max_segments"], role="max_segments", minimum=1)
        if not complete or segment_count is None:
            checks.append(
                _status("max_segments", "unknown", "complete individual segments are missing")
            )
        elif segment_count > maximum:
            checks.append(
                _status("max_segments", "fail", f"{segment_count} segments exceed {maximum}")
            )
        else:
            checks.append(
                _status("max_segments", "pass", f"{segment_count} segments are within {maximum}")
            )

    if "no_consecutive_same_operator" in scenario and _boolean(
        scenario["no_consecutive_same_operator"], role="no_consecutive_same_operator"
    ):
        checks.append(
            _operator_check(segments)
            if complete
            else _status(
                "no_consecutive_same_operator",
                "unknown",
                "the itinerary is incomplete",
            )
        )

    if "depart_after" in scenario:
        checks.append(
            _segment_clock_check(
                "depart_after",
                segments,
                bound=_clock(scenario["depart_after"], role="depart_after"),
                field="departure",
                before=False,
            )
        )
    if "arrive_before" in scenario:
        checks.append(
            _segment_clock_check(
                "arrive_before",
                segments,
                bound=_clock(scenario["arrive_before"], role="arrive_before"),
                field="arrival",
                before=True,
            )
        )
    if "no_airport_changes" in scenario and _boolean(
        scenario["no_airport_changes"], role="no_airport_changes"
    ):
        checks.append(_segment_airport_check(segments))
    if "no_overnight" in scenario and _boolean(scenario["no_overnight"], role="no_overnight"):
        checks.append(_overnight_check(selected))

    if "max_stops" in scenario:
        maximum = _integer(scenario["max_stops"], role="max_stops")
        status = "pass"
        detail = "owned stop counts satisfy the bound"
        for _query, offer in selected:
            stops = offer.get("stops_count")
            if not isinstance(stops, int):
                status, detail = "unknown", "an offer stop count is missing"
                break
            if stops > maximum:
                status, detail = "fail", f"{stops} stops exceed {maximum}"
                break
        checks.append(_status("max_stops", status, detail))
    if "min_layover" in scenario:
        checks.append(
            _layover_check(
                "min_layover",
                selected,
                bound=_number(scenario["min_layover"], role="min_layover"),
                maximum=False,
            )
        )
    if "max_layover" in scenario:
        checks.append(
            _layover_check(
                "max_layover",
                selected,
                bound=_number(scenario["max_layover"], role="max_layover"),
                maximum=True,
            )
        )
    if "bags" in scenario:
        checks.append(
            _baggage_check(
                "bags",
                selected,
                requested=_integer(scenario["bags"], role="bags"),
                field="checked_bags",
            )
        )
    if "carry_on" in scenario:
        checks.append(
            _baggage_check(
                "carry_on",
                selected,
                requested=_integer(scenario["carry_on"], role="carry_on"),
                field="carry_on",
            )
        )
    if "require_reproducible" in scenario and _boolean(
        scenario["require_reproducible"], role="require_reproducible"
    ):
        status = "pass"
        detail = "every offer carries owned URL evidence"
        for _query, offer in selected:
            evidence_value = offer.get("evidence")
            if evidence_value is None:
                status, detail = "unknown", "offer provenance is missing"
                break
            evidence = _mapping(evidence_value, role="offer.evidence")
            if evidence.get("url_kind") == "none":
                status, detail = "fail", "an offer has no owned query or booking URL"
                break
        checks.append(_status("require_reproducible", status, detail))

    if "chronological" in scenario and _boolean(scenario["chronological"], role="chronological"):
        checks.append(_chronological_check(segments, _temporal_complete(selected, complete)))
    if "arrival_deadline" in scenario:
        checks.append(
            _arrival_deadline_check(
                segments, _temporal_complete(selected, complete), scenario["arrival_deadline"]
            )
        )

    for constraint in ("min_stay_days", "max_stay_days"):
        if constraint in scenario:
            bound = _integer(scenario[constraint], role=constraint)
            checks.append(
                _stay_check(constraint, selected, _temporal_complete(selected, complete), bound)
            )
    if "minimum_savings" in scenario:
        _number(scenario["minimum_savings"], role="minimum_savings")
        checks.append(
            _status(
                "minimum_savings",
                "unknown",
                "no owned comparison candidate was supplied",
            )
        )

    violations = tuple(check.constraint for check in checks if check.status == "fail")
    unknown = tuple(check.constraint for check in checks if check.status == "unknown")
    feasible = False if violations else None if unknown else True
    span = (max(dates) - min(dates)).days if dates else None
    validated_at = now or datetime.now(timezone.utc)
    return ItineraryValidationReport(
        validated_at=validated_at,
        scenario=scenario,
        relaxations=relaxations,
        checks=tuple(checks),
        legs=rows,
        feasible=feasible,
        currency=owned_currency,
        fare_total=fare_total,
        ranked_total=ranked_total,
        offer_row_count=len(rows),
        journey_leg_count=journey_count,
        segment_count=segment_count,
        trip_span_days=span,
        violations=violations,
        unknown=unknown,
    )
