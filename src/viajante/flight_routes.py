"""Flight route specs, metro and same-city (nearby) expansion, and trip include/exclude."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from typing import Any, Literal, Optional, Sequence, Tuple

from viajante.airports import get_airport, metro_members, metro_of, same_city_iata
from viajante.models import FlightCabin, FlightLeg, FlightQuery, MultiCity, RoundTrip, Trip

_ROUTE_SPEC_RE = re.compile(r"^[A-Za-z]{3}-[A-Za-z]{3}:")


TripKind = Literal["one-way", "rt", "multi"]


FlightPlan = Tuple[FlightQuery, ...] | RoundTrip | Tuple[RoundTrip, ...] | MultiCity


ROUTE_GRAMMAR = "ORIGIN-DESTINATION:DATE[,DATE...] or ORIGIN-DESTINATION:OUT:BACK"


RT_GRAMMAR = "ORIGIN-DESTINATION:OUT:BACK"


MULTI_GRAMMAR = "ORIGIN-DESTINATION:DATE"


_RT_DATES = re.compile(r"^(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2})$")


_ONE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


_TRIP_ALIASES = {
    "one-way": "one-way",
    "oneway": "one-way",
    "one_way": "one-way",
    "rt": "rt",
    "round-trip": "rt",
    "round_trip": "rt",
    "roundtrip": "rt",
    "multi": "multi",
    "multi-city": "multi",
    "multi_city": "multi",
    "multicity": "multi",
}


def normalize_trip_kind(trip: str) -> TripKind:
    key = trip.strip().casefold().replace(" ", "-")
    try:
        return _TRIP_ALIASES[key]  # type: ignore[return-value]
    except KeyError:
        raise ValueError("trip must be 'one-way', 'rt', or 'multi'") from None


def _trip_max_stops(trip: Trip) -> int:
    if isinstance(trip, (FlightQuery, RoundTrip)):
        return trip.max_stops
    return max(leg.max_stops for leg in trip.legs)


def _progress_label(trip: Trip) -> str:
    if isinstance(trip, FlightQuery):
        base = f"{trip.origin} -> {trip.destination} {trip.departure_date.isoformat()}"
        return f"{base} ({trip.nearby_label})" if trip.nearby_label else base
    if isinstance(trip, RoundTrip):
        base = (
            f"{trip.origin} -> {trip.destination} {trip.departure_date.isoformat()}"
            f" / {trip.return_date.isoformat()}"
        )
        return f"{base} ({trip.nearby_label})" if trip.nearby_label else base
    return " / ".join(
        f"{leg.origin} -> {leg.destination} {leg.departure_date.isoformat()}" for leg in trip.legs
    )


def _nearby_city(code: str) -> Optional[str]:
    airport = get_airport(code)
    if airport is None or not airport.city.strip():
        return None
    return airport.city


def _nearby_pair_label(
    origin: str,
    dest: str,
    origin_codes: Tuple[str, ...],
    dest_codes: Tuple[str, ...],
) -> Optional[str]:
    parts: list[str] = []
    if len(origin_codes) > 1:
        city = _nearby_city(origin)
        parts.append(f"nearby {city} {origin}" if city else f"nearby {origin}")
    if len(dest_codes) > 1:
        city = _nearby_city(dest)
        parts.append(f"nearby {city} {dest}" if city else f"nearby {dest}")
    return "; ".join(parts) if parts else None


_METRO_LABEL = "metro "


METRO_QUERY_LIMIT = 18


def metro_codes_in_label(label: Optional[str]) -> frozenset[str]:
    """Metro codes a named route expanded, read back from its ``nearby_label``."""
    if not label:
        return frozenset()
    parts = (part.strip() for part in label.split(";"))
    return frozenset(part[len(_METRO_LABEL) :] for part in parts if part.startswith(_METRO_LABEL))


def _reject_same_metro(origin: str, destination: str) -> None:
    """A route whose ends are one metro, or an airport inside the other's metro, is empty."""
    left = origin.strip().upper()
    right = destination.strip().upper()
    left_metro = left if metro_members(left) else metro_of(left)
    right_metro = right if metro_members(right) else metro_of(right)
    if left_metro and left_metro == right_metro:
        raise ValueError(
            f"origin {left} and destination {right} resolve to the same metro {left_metro}"
        )


def _expand_metro_spec(spec: str) -> Tuple[tuple[str, Optional[str]], ...]:
    """One route spec per member pair when a side names a metro code; else the spec."""
    pair, colon, rest = spec.partition(":")
    origin, dash, destination = pair.strip().partition("-")
    if colon and dash and origin and destination:
        _reject_same_metro(origin, destination)
    named = [code.strip().upper() for code in (origin, destination) if metro_members(code)]
    if not colon or not dash or not named:
        return ((spec, None),)
    label = "; ".join(f"{_METRO_LABEL}{code}" for code in named)
    return tuple(
        (f"{start}-{end}:{rest}", label)
        for start in metro_members(origin) or (origin,)
        for end in metro_members(destination) or (destination,)
        if start != end
    )


def nearby_notes(
    trips: Sequence[Trip],
    exclude_airports: Optional[Sequence[str]] = None,
) -> Tuple[str, ...]:
    """Stderr legend for `--nearby` and metro expands. Named open-jaw is not expanded."""
    blocked = _named_iata(exclude_airports)
    notes: list[str] = []
    seen: set[tuple[str, str]] = set()
    seen_metros: set[str] = set()
    for trip in trips:
        label = getattr(trip, "nearby_label", None)
        if isinstance(trip, MultiCity) or not label:
            continue
        metros = metro_codes_in_label(label)
        for metro in sorted(metros - seen_metros):
            seen_metros.add(metro)
            codes = sorted(code for code in metro_members(metro) if code not in blocked)
            if codes:
                notes.append(f"metro {metro}: {', '.join(codes)}")
        if metros:
            continue
        for code in (trip.origin, trip.destination):
            if code in blocked:
                continue
            airport = get_airport(code)
            if airport is None or not airport.city.strip():
                continue
            key = (airport.city, airport.country)
            codes = tuple(item for item in same_city_iata(code) if item not in blocked)
            if len(codes) < 2 or key in seen:
                continue
            seen.add(key)
            notes.append(f"nearby {airport.city}: {', '.join(sorted(codes))}")
    return tuple(notes)


def _expand_od_trip(trip: FlightQuery | RoundTrip) -> Tuple[FlightQuery | RoundTrip, ...]:
    origin_codes = same_city_iata(trip.origin) or (trip.origin,)
    dest_codes = same_city_iata(trip.destination) or (trip.destination,)
    expanded = [
        replace(
            trip,
            origin=origin,
            destination=destination,
            nearby_label=_nearby_pair_label(origin, destination, origin_codes, dest_codes),
        )
        for origin in origin_codes
        for destination in dest_codes
        if origin != destination
    ]
    return tuple(expanded) if expanded else (trip,)


def expand_nearby_trips(trips: Sequence[Trip], *, nearby: bool = False) -> Tuple[Trip, ...]:
    """Fan out one-way and mirrored RT queries to owned same-city IATA.

    Default off. Packaged open-jaw / multi-city keeps every named airport
    (LGW stays LGW). Combining `nearby` with a named metro code is an error:
    the metro already names its airports, and expanding only the other side
    would silently drop part of the request. Does not invent codes or mix a
    mirrored RT into an open jaw.
    """
    if not nearby:
        return tuple(trips)
    if any(metro_codes_in_label(getattr(trip, "nearby_label", None)) for trip in trips):
        raise ValueError(
            "--nearby cannot be combined with a metro code; name airports or "
            "metros on both sides (for example NYC-LON), not --nearby"
        )
    expanded: list[Trip] = []
    for trip in trips:
        if isinstance(trip, (FlightQuery, RoundTrip)) and not trip.nearby_label:
            expanded.extend(_expand_od_trip(trip))
        else:
            expanded.append(trip)
    return tuple(expanded) if expanded else tuple(trips)


def expand_nearby_origins(
    origin: str,
    *,
    nearby: bool = False,
    exclude_airports: Optional[Sequence[str]] = None,
) -> Tuple[tuple[str, Optional[str]], ...]:
    """Fan an explore origin out to owned same-city IATA.

    Default off. Returns ``(code, nearby_label)`` pairs. A city with no
    second major stays the named seed, unlabeled. Does not invent codes.
    Named ``exclude_airports`` drop matching codes; nearby cannot sneak
    an excluded same-city code back.
    """
    seed = origin.strip().upper()
    blocked = _named_iata(exclude_airports)
    if not nearby:
        return () if seed in blocked else ((seed, None),)
    codes = same_city_iata(seed)
    if len(codes) < 2:
        kept = codes[0] if codes else seed
        return () if kept in blocked else ((kept, None),)
    city = _nearby_city(seed)
    labeled: list[tuple[str, Optional[str]]] = []
    for code in codes:
        if code in blocked:
            continue
        label = f"nearby {city} {code}" if city else f"nearby {code}"
        labeled.append((code, label))
    return tuple(labeled)


def nearby_origin_notes(
    origin: str, exclude_airports: Optional[Sequence[str]] = None
) -> Tuple[str, ...]:
    """Stderr legend for explore ``--nearby``. No invented codes."""
    blocked = _named_iata(exclude_airports)
    codes = tuple(code for code in same_city_iata(origin) if code not in blocked)
    if len(codes) < 2:
        return ()
    airport = get_airport(origin)
    if airport is None or not airport.city.strip():
        return ()
    return (f"nearby {airport.city}: {', '.join(sorted(codes))}",)


def parse_route_specs(
    specs: Sequence[str],
    *,
    max_stops: int,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> Tuple[FlightQuery, ...]:
    if max_stops not in (0, 1, 2):
        raise ValueError("max_stops must be 0, 1, or 2")
    occupancy = {
        "adults": adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
        "cabin": cabin,
        "bags": bags,
        "carry_on": carry_on,
        "price_cap": price_cap,
    }
    queries: list[FlightQuery] = []
    for spec in specs:
        origin, destination, stripped = _split_route(spec, grammar=ROUTE_GRAMMAR)
        if "," in stripped and ":" in stripped:
            raise ValueError(
                f"invalid route: {spec!r}. Do not mix comma-separated dates with OUT:BACK"
            )
        rt_match = _RT_DATES.fullmatch(stripped)
        if rt_match:
            outbound = date.fromisoformat(rt_match.group(1))
            inbound = date.fromisoformat(rt_match.group(2))
            if inbound <= outbound:
                raise ValueError(f"return date must be after outbound in route {spec!r}")
            queries.append(
                FlightQuery(
                    origin=origin,
                    destination=destination,
                    departure_date=outbound,
                    max_stops=max_stops,
                    **occupancy,
                )
            )
            queries.append(
                FlightQuery(
                    origin=destination,
                    destination=origin,
                    departure_date=inbound,
                    max_stops=max_stops,
                    **occupancy,
                )
            )
            continue
        for date_text in stripped.split(","):
            date_text = date_text.strip()
            if not date_text:
                continue
            try:
                departure_date = date.fromisoformat(date_text)
            except ValueError as exc:
                raise ValueError(f"invalid date {date_text!r} in route {spec!r}") from exc
            queries.append(
                FlightQuery(
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    max_stops=max_stops,
                    **occupancy,
                )
            )
        if not any(part.strip() for part in stripped.split(",")):
            raise ValueError(f"invalid route: {spec!r}. Expected {ROUTE_GRAMMAR}")
    if not queries:
        raise ValueError("at least one route is required")
    return tuple(queries)


def as_trips(plan: FlightPlan | Trip | Sequence[Trip]) -> Tuple[Trip, ...]:
    if isinstance(plan, (FlightQuery, RoundTrip, MultiCity)):
        return (plan,)
    return tuple(plan)


def _is_route_spec(text: str) -> bool:
    return _ROUTE_SPEC_RE.match(text.strip()) is not None


def parse_flight_plan(
    specs: Sequence[str],
    *,
    trip: str = "one-way",
    max_stops: int,
    adults: int = 1,
    cabin: FlightCabin = "economy",
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
    children: int = 0,
    infants_in_seat: int = 0,
    infants_on_lap: int = 0,
) -> FlightPlan:
    kind = normalize_trip_kind(trip)
    shop = {
        "adults": adults,
        "children": children,
        "infants_in_seat": infants_in_seat,
        "infants_on_lap": infants_on_lap,
        "cabin": cabin,
        "bags": bags,
        "carry_on": carry_on,
        "price_cap": price_cap,
    }
    variants = tuple(variant for spec in specs for variant in _expand_metro_spec(spec))
    if all(label is None for _spec, label in variants):
        if kind == "one-way":
            return parse_route_specs(specs, max_stops=max_stops, **shop)
        if kind == "rt":
            return _parse_round_trip_plan(specs, max_stops=max_stops, **shop)
        return _parse_multi_city_plan(specs, max_stops=max_stops, **shop)
    if kind == "multi" or len(specs) != 1 and kind == "rt":
        raise ValueError(
            "metro codes expand one-way and --trip rt routes only; "
            "name airports for open-jaw or multi-city"
        )
    if kind == "rt":
        plan: FlightPlan = tuple(
            replace(_parse_round_trip_plan([spec], max_stops=max_stops, **shop), nearby_label=label)
            for spec, label in variants
        )
    else:
        plan = tuple(
            replace(query, nearby_label=label) if label else query
            for spec, label in variants
            for query in parse_route_specs([spec], max_stops=max_stops, **shop)
        )
    count = len(as_trips(plan))
    if count > METRO_QUERY_LIMIT:
        raise ValueError(
            f"metro expansion would send {count} provider queries; "
            f"the limit is {METRO_QUERY_LIMIT} per call"
        )
    return plan


def _split_route(spec: str, *, grammar: str) -> tuple[str, str, str]:
    try:
        pair, dates_part = spec.split(":", 1)
        origin, destination = pair.split("-", 1)
    except ValueError as exc:
        raise ValueError(f"invalid route: {spec!r}. Expected {grammar}") from exc
    if not origin or not destination or not dates_part.strip():
        raise ValueError(f"invalid route: {spec!r}. Expected {grammar}")
    return origin, destination, dates_part.strip()


def _parse_round_trip_plan(
    specs: Sequence[str], *, max_stops: int, **shop: Any
) -> RoundTrip | MultiCity:
    if len(specs) == 2:
        # ORIGIN-DEST:OUT:BACK always returns from DEST. YVR-LHR out + LGW-YVR back
        # cannot use that grammar without dropping LGW, so two DATE legs under
        # --trip rt POST one open-jaw multi-city package, not a mirrored RT.
        return _parse_multi_city_plan(specs, max_stops=max_stops, **shop)
    if len(specs) != 1:
        raise ValueError(f"--trip rt expects exactly one {RT_GRAMMAR}")
    spec = specs[0]
    if "," in spec:
        raise ValueError("--trip rt does not accept comma-separated dates")
    origin, destination, dates_part = _split_route(spec, grammar=RT_GRAMMAR)
    rt_match = _RT_DATES.fullmatch(dates_part)
    if rt_match is None:
        raise ValueError(f"--trip rt expects {RT_GRAMMAR}")
    outbound = date.fromisoformat(rt_match.group(1))
    inbound = date.fromisoformat(rt_match.group(2))
    return RoundTrip(origin, destination, outbound, inbound, max_stops=max_stops, **shop)


def _parse_multi_city_plan(specs: Sequence[str], *, max_stops: int, **shop: Any) -> MultiCity:
    if not 2 <= len(specs) <= 6:
        raise ValueError(f"--trip multi expects 2 to 6 {MULTI_GRAMMAR} routes")
    legs: list[FlightLeg] = []
    for spec in specs:
        if "," in spec:
            raise ValueError("--trip multi does not accept comma-separated dates")
        origin, destination, dates_part = _split_route(spec, grammar=MULTI_GRAMMAR)
        if _RT_DATES.fullmatch(dates_part):
            raise ValueError("--trip multi does not accept OUT:BACK")
        if _ONE_DATE.fullmatch(dates_part) is None:
            raise ValueError(f"invalid route: {spec!r}. Expected {MULTI_GRAMMAR}")
        legs.append(
            FlightLeg(origin, destination, date.fromisoformat(dates_part), max_stops=max_stops)
        )
    return MultiCity(tuple(legs), **shop)


def _named_iata(codes: Optional[Sequence[str]]) -> frozenset[str]:
    if not codes:
        return frozenset()
    return frozenset(code.strip().upper() for code in codes if str(code).strip())


def trip_uses_excluded_airport(trip: Trip, exclude_airports: Optional[Sequence[str]]) -> bool:
    """True when a named origin or dest is in the owned exclude list."""
    blocked = _named_iata(exclude_airports)
    if not blocked:
        return False
    if isinstance(trip, MultiCity):
        return any(leg.origin in blocked or leg.destination in blocked for leg in trip.legs)
    return trip.origin in blocked or trip.destination in blocked


def drop_excluded_airport_trips(
    trips: Sequence[Trip],
    exclude_airports: Optional[Sequence[str]] = None,
) -> Tuple[Trip, ...]:
    """Drop trips whose origin or dest is in the named exclude list.

    Does not invent a substitute airport. Named open-jaw stays unless a
    named airport on that jaw is excluded. Nearby expansions that match
    the list are dropped; remaining owned same-city codes stay.
    """
    blocked = _named_iata(exclude_airports)
    if not blocked:
        return tuple(trips)
    return tuple(trip for trip in trips if not trip_uses_excluded_airport(trip, blocked))


def trip_dest_in_include_list(trip: Trip, include_airports: Optional[Sequence[str]]) -> bool:
    """True when the trip dest is in the owned include list.

    Include is dests only: origin is not required to be in the list.
    Unnamed include keeps every trip. Multi-city return-to-home dests
    are not required to be in the dest shortlist.
    """
    allowed = _named_iata(include_airports)
    if not allowed:
        return True
    if isinstance(trip, MultiCity):
        home = trip.legs[0].origin if trip.legs else ""
        return all(leg.destination in allowed or leg.destination == home for leg in trip.legs)
    return trip.destination in allowed


def keep_included_dest_trips(
    trips: Sequence[Trip],
    include_airports: Optional[Sequence[str]] = None,
) -> Tuple[Trip, ...]:
    """Keep trips whose dest is in the named include list.

    Unnamed stays the full set. Does not rewrite a missing dest to a
    substitute. Nearby expansions already in the list stay; others drop.
    """
    allowed = _named_iata(include_airports)
    if not allowed:
        return tuple(trips)
    return tuple(trip for trip in trips if trip_dest_in_include_list(trip, allowed))


def _overlay_carrier_filters(
    trips: Sequence[Trip],
    *,
    airlines: Optional[Sequence[str]] = None,
    exclude_airlines: Optional[Sequence[str]] = None,
    alliances: Optional[Sequence[str]] = None,
    exclude_alliances: Optional[Sequence[str]] = None,
) -> Tuple[Trip, ...]:
    overlay: dict[str, Tuple[str, ...]] = {}
    if airlines:
        overlay["airlines"] = tuple(airlines)
    if exclude_airlines:
        overlay["exclude_airlines"] = tuple(exclude_airlines)
    if alliances:
        overlay["alliances"] = tuple(alliances)
    if exclude_alliances:
        overlay["exclude_alliances"] = tuple(exclude_alliances)
    if not overlay:
        return tuple(trips)
    return tuple(replace(trip, **overlay) for trip in trips)


def overlay_trip_fields(
    trips: Sequence[Trip],
    *,
    adults: Optional[int] = None,
    children: Optional[int] = None,
    infants_in_seat: Optional[int] = None,
    infants_on_lap: Optional[int] = None,
    cabin: Optional[FlightCabin] = None,
    max_stops: Optional[int] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    price_cap: Optional[int] = None,
) -> tuple[Trip, ...]:
    overlay: dict[str, object] = {}
    if adults is not None:
        overlay["adults"] = adults
    if children is not None:
        overlay["children"] = children
    if infants_in_seat is not None:
        overlay["infants_in_seat"] = infants_in_seat
    if infants_on_lap is not None:
        overlay["infants_on_lap"] = infants_on_lap
    if cabin is not None:
        overlay["cabin"] = cabin
    if bags is not None:
        overlay["bags"] = bags
    if carry_on is not None:
        overlay["carry_on"] = carry_on
    if price_cap is not None:
        overlay["price_cap"] = price_cap
    if max_stops is None and not overlay:
        return tuple(trips)
    out: list[Trip] = []
    for item in trips:
        extra = dict(overlay)
        if max_stops is not None:
            if isinstance(item, MultiCity):
                extra["legs"] = tuple(replace(leg, max_stops=max_stops) for leg in item.legs)
            else:
                extra["max_stops"] = max_stops
        out.append(replace(item, **extra))
    return tuple(out)
