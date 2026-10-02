"""Local stay bookkeeping: a per-night roster into blocks, and a per-person cost split.

Offline arithmetic only. It never searches, never picks where to sleep, and never converts
money: every amount stays in the one currency the caller names.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Optional, Sequence

from viajante.models import (
    PersonCost,
    SplitStay,
    StayBlock,
    StayBlocksReport,
    StayCostSplit,
    StayShare,
    normalize_currency,
)

_CENTS = Decimal(100)


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date: {value!r}") from exc


def _parse_roster(roster: Mapping[str, Sequence[str]]) -> list[tuple[date, tuple[str, ...]]]:
    if not isinstance(roster, Mapping) or not roster:
        raise ValueError("roster must map each night (YYYY-MM-DD) to the people sleeping")
    nights: list[tuple[date, tuple[str, ...]]] = []
    for raw_day, raw_people in roster.items():
        day = _day(raw_day, "roster night")
        if isinstance(raw_people, (str, bytes)) or not isinstance(raw_people, Sequence):
            raise ValueError(f"roster for {raw_day} must be a list of names")
        people: list[str] = []
        seen: set[str] = set()
        for raw_name in raw_people:
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"roster for {raw_day} has a blank name")
            name = " ".join(raw_name.split())
            if name.casefold() in seen:
                raise ValueError(f"{name!r} appears twice on {raw_day}")
            seen.add(name.casefold())
            people.append(name)
        if not people:
            raise ValueError(f"nobody sleeps on {raw_day}; drop that night")
        nights.append((day, tuple(people)))
    nights.sort(key=lambda row: row[0])
    for (earlier, _), (later, _) in zip(nights, nights[1:], strict=False):
        if later != earlier + timedelta(days=1):
            missing = earlier + timedelta(days=1)
            raise ValueError(f"roster nights must be consecutive; {missing.isoformat()} is missing")
    return nights


def _display_names(nights: list[tuple[date, tuple[str, ...]]]) -> tuple[str, ...]:
    """People in order of first appearance, spelled as first written."""
    ordered: dict[str, str] = {}
    for _, people in nights:
        for name in people:
            ordered.setdefault(name.casefold(), name)
    return tuple(ordered.values())


def plan_stay_blocks(roster: Mapping[str, Sequence[str]]) -> StayBlocksReport:
    """Group consecutive nights with the same people into check-in/check-out blocks."""
    nights = _parse_roster(roster)
    blocks: list[StayBlock] = []
    start = nights[0][0]
    current = frozenset(name.casefold() for name in nights[0][1])
    people = nights[0][1]
    for day, names in nights[1:]:
        key = frozenset(name.casefold() for name in names)
        if key != current:
            blocks.append(StayBlock(start, day, people))
            start, current, people = day, key, names
    blocks.append(StayBlock(start, nights[-1][0] + timedelta(days=1), people))
    return StayBlocksReport(
        blocks=tuple(blocks),
        people=_display_names(nights),
        person_nights=sum(len(names) for _, names in nights),
    )


def _cents(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a number")
    try:
        amount = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValueError(f"{label} must be a number") from exc
    if amount < 0 or not amount.is_finite():
        raise ValueError(f"{label} must not be negative")
    return int((amount * _CENTS).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _money(cents: int) -> float:
    return float(Decimal(cents) / _CENTS)


def _shares_in_cents(total_cents: int, nights_by_person: Mapping[str, int]) -> dict[str, int]:
    """Split total_cents by nights; the leftover cents go to the largest remainders, name order."""
    person_nights = sum(nights_by_person.values())
    shares: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    for name, nights in nights_by_person.items():
        whole, remainder = divmod(total_cents * nights, person_nights)
        shares[name] = whole
        remainders.append((-remainder, name))
    leftover = total_cents - sum(shares.values())
    for _neg, name in sorted(remainders)[:leftover]:
        shares[name] += 1
    return shares


def split_stay_costs(
    stays: Sequence[Mapping[str, object]],
    roster: Mapping[str, Sequence[str]],
    *,
    currency: str,
    fee_per_person_night: Optional[float] = None,
) -> StayCostSplit:
    """Split each stay's total among the people who sleep there, by their nights in it.

    A stay is ``{name, check_in, check_out, total}``. Its rate is its total divided by the
    person-nights the roster puts inside it, so someone who never sleeps there pays nothing
    toward it. ``fee_per_person_night`` is a per-night charge the caller names (a city tax).
    """
    code = normalize_currency(currency)
    nights = _parse_roster(roster)
    roster_by_day = {day: names for day, names in nights}
    names_by_key = {name.casefold(): name for name in _display_names(nights)}
    fee_cents = 0 if fee_per_person_night is None else _cents(fee_per_person_night, "fee")
    if not stays:
        raise ValueError("at least one stay is required")

    parsed: list[tuple[str, date, date, int]] = []
    for index, stay in enumerate(stays):
        if not isinstance(stay, Mapping):
            raise ValueError(f"stay {index + 1} must be an object")
        name = stay.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"stay {index + 1} needs a name")
        check_in = _day(stay.get("check_in"), f"{name} check_in")
        check_out = _day(stay.get("check_out"), f"{name} check_out")
        if check_out <= check_in:
            raise ValueError(f"{name}: check_out must be after check_in")
        parsed.append(
            (name.strip(), check_in, check_out, _cents(stay.get("total"), f"{name} total"))
        )
    parsed.sort(key=lambda row: row[1])
    for (a_name, _, a_out, _), (b_name, b_in, _, _) in zip(parsed, parsed[1:], strict=False):
        if a_out > b_in:
            raise ValueError(f"{a_name} and {b_name} overlap")

    person_stay: dict[str, list[StayShare]] = {key: [] for key in names_by_key}
    person_nights_total: dict[str, int] = {key: 0 for key in names_by_key}
    covered: set[date] = set()
    split_stays: list[SplitStay] = []
    grand_cents = 0
    for name, check_in, check_out, total_cents in parsed:
        by_person: dict[str, int] = {}
        day = check_in
        while day < check_out:
            if day not in roster_by_day:
                raise ValueError(f"{name} covers {day.isoformat()}, which is not in the roster")
            covered.add(day)
            for person in roster_by_day[day]:
                by_person[person.casefold()] = by_person.get(person.casefold(), 0) + 1
            day += timedelta(days=1)
        person_nights = sum(by_person.values())
        if person_nights == 0:
            raise ValueError(f"nobody sleeps in {name}")
        for key, cents in _shares_in_cents(total_cents, by_person).items():
            person_stay[key].append(StayShare(name, by_person[key], _money(cents)))
            person_nights_total[key] += by_person[key]
        grand_cents += total_cents
        rate = (Decimal(total_cents) / _CENTS / person_nights).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        split_stays.append(
            SplitStay(name, check_in, check_out, _money(total_cents), person_nights, float(rate))
        )

    people: list[PersonCost] = []
    for key, display in names_by_key.items():
        if not person_stay[key]:
            continue
        room_cents = sum(round(share.total * 100) for share in person_stay[key])
        fee = person_nights_total[key] * fee_cents
        grand_cents += fee
        people.append(
            PersonCost(
                name=display,
                nights=person_nights_total[key],
                fee=_money(fee),
                total=_money(room_cents + fee),
                shares=tuple(person_stay[key]),
            )
        )
    return StayCostSplit(
        currency=code,
        stays=tuple(split_stays),
        people=tuple(people),
        total=_money(grand_cents),
        unallocated_nights=tuple(day for day, _ in nights if day not in covered),
        fee_per_person_night=None if fee_per_person_night is None else _money(fee_cents),
    )
