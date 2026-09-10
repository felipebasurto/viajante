"""Local award math: CPP, transfer paths, imported offers. Never invents seats."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from viajante.models import (
    AwardCompareReport,
    AwardOffer,
    PlaybookStep,
    PointsBalance,
    TransferPath,
    normalize_currency,
)
from viajante.storage import write_json_atomic

# Public 1:1 card-to-program partners. Ratios are not live availability.
TRANSFER_LAST_VERIFIED = date(2026, 9, 10)
_TRANSFER_EDGES: tuple[tuple[str, str, float, Optional[int]], ...] = (
    ("MR", "aeroplan", 1.0, 0),
    ("MR", "avios", 1.0, 0),
    ("MR", "flying_blue", 1.0, 0),
    ("MR", "virgin_atlantic", 1.0, 0),
    ("MR", "qatar", 1.0, None),
    ("CHASE", "united", 1.0, 0),
    ("CHASE", "aeroplan", 1.0, 0),
    ("CHASE", "avios", 1.0, 0),
    ("CHASE", "flying_blue", 1.0, 0),
    ("CHASE", "virgin_atlantic", 1.0, 0),
    ("CAP1", "aeroplan", 1.0, 0),
    ("CAP1", "avios", 1.0, 0),
    ("CAP1", "flying_blue", 1.0, 0),
    ("CAP1", "virgin_atlantic", 1.0, 0),
    ("CITI", "avios", 1.0, None),
    ("CITI", "qatar", 1.0, None),
    ("BILT", "aeroplan", 1.0, 0),
    ("BILT", "united", 1.0, 0),
    ("BILT", "avios", 1.0, 0),
    ("BILT", "flying_blue", 1.0, 0),
)


def cents_per_point(
    cash_price: float,
    points: int,
    *,
    taxes: Optional[float] = None,
) -> float:
    """Owned CPP in cents. cash_price and taxes must be the same currency."""
    if cash_price <= 0:
        raise ValueError("cash_price must be positive")
    if points <= 0:
        raise ValueError("points must be positive")
    if taxes is not None and taxes < 0:
        raise ValueError("taxes must not be negative")
    outlay = 0.0 if taxes is None else taxes
    return round((cash_price - outlay) / points * 100.0, 2)


def parse_balances(raw: Any) -> tuple[PointsBalance, ...]:
    if isinstance(raw, Mapping) and "balances" in raw:
        rows = raw["balances"]
    else:
        rows = raw
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError("balances must be a list of {program, balance}")
    out: list[PointsBalance] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("each balance row must be an object")
        program = row.get("program")
        balance = row.get("balance")
        if not isinstance(program, str):
            raise ValueError("balance program is required")
        if not isinstance(balance, int) or isinstance(balance, bool):
            raise ValueError("balance must be an integer")
        out.append(PointsBalance(program=program, balance=balance))
    return tuple(out)


def load_balances(path: Path) -> tuple[PointsBalance, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return parse_balances(payload)


def award_offer_from_mapping(raw: Mapping[str, Any]) -> AwardOffer:
    origin = raw.get("origin")
    destination = raw.get("destination")
    departure = raw.get("departure_date")
    program = raw.get("program")
    points = raw.get("points")
    evidence = raw.get("evidence", "user_supplied")
    if not isinstance(origin, str) or not isinstance(destination, str):
        raise ValueError("award origin and destination are required")
    if not isinstance(departure, str):
        raise ValueError("award departure_date is required")
    if not isinstance(program, str):
        raise ValueError("award program is required")
    if not isinstance(points, int) or isinstance(points, bool):
        raise ValueError("award points must be an integer")
    if not isinstance(evidence, str):
        raise ValueError("award evidence is required")
    cabin = raw.get("cabin", "economy")
    if cabin not in ("economy", "premium-economy", "business", "first"):
        raise ValueError(f"invalid cabin: {cabin!r}")
    taxes = raw.get("taxes")
    if taxes is not None and not isinstance(taxes, (int, float)):
        raise ValueError("taxes must be a number")
    remaining = raw.get("remaining_seats")
    if remaining is not None and (not isinstance(remaining, int) or isinstance(remaining, bool)):
        raise ValueError("remaining_seats must be an integer")
    return_date = raw.get("return_date")
    source = raw.get("source")
    if evidence == "confirmed" and not source:
        raise ValueError("confirmed award evidence needs a named source")
    return AwardOffer(
        origin=origin,
        destination=destination,
        departure_date=date.fromisoformat(departure),
        program=program,
        points=points,
        evidence=evidence,  # type: ignore[arg-type]
        cabin=cabin,  # type: ignore[arg-type]
        taxes=float(taxes) if taxes is not None else None,
        currency=raw.get("currency") if isinstance(raw.get("currency"), str) else None,
        remaining_seats=remaining,
        booking_url=raw.get("booking_url") if isinstance(raw.get("booking_url"), str) else None,
        source=source if isinstance(source, str) else None,
        airline=raw.get("airline") if isinstance(raw.get("airline"), str) else None,
        return_date=date.fromisoformat(return_date) if isinstance(return_date, str) else None,
    )


def load_award_offer(path: Path) -> AwardOffer:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("award offer must be a JSON object")
    return award_offer_from_mapping(payload)


def transfer_paths(
    program: str,
    points_needed: int,
    balances: Sequence[PointsBalance] = (),
) -> tuple[TransferPath, ...]:
    if points_needed <= 0:
        raise ValueError("points must be positive")
    target = program.strip().casefold()
    if not target:
        raise ValueError("program is required")
    held = {row.program: row.balance for row in balances}
    paths: list[TransferPath] = []
    for currency, dest, ratio, minutes in _TRANSFER_EDGES:
        if dest != target:
            continue
        balance = held.get(currency, 0)
        effective = int(balance * ratio)
        paths.append(
            TransferPath(
                currency=currency,
                program=dest,
                ratio=ratio,
                points_needed=points_needed,
                effective_points=effective,
                covers=effective >= points_needed,
                last_verified=TRANSFER_LAST_VERIFIED,
                transfer_minutes=minutes,
            )
        )
    paths.sort(key=lambda path: (not path.covers, -(path.effective_points)))
    return tuple(paths)


def award_playbook(award: AwardOffer, paths: Sequence[TransferPath]) -> tuple[PlaybookStep, ...]:
    steps: list[PlaybookStep] = [
        PlaybookStep(
            kind="warning",
            title="Verify before transferring",
            body=(
                "Confirm the award is still available on the loyalty site before "
                "transferring points. Transfers are usually irreversible. "
                "Viajante does not invent award seats."
            ),
        ),
        PlaybookStep(
            kind="action",
            title="Open the program site",
            body=(
                f"Search {award.origin}-{award.destination} on {award.program} "
                f"for {award.departure_date.isoformat()} in {award.cabin}."
            ),
        ),
    ]
    funded = [path for path in paths if path.covers]
    if funded:
        best = funded[0]
        steps.append(
            PlaybookStep(
                kind="action",
                title="Transfer only what you need",
                body=(
                    f"A named {best.currency} balance covers {award.points} {award.program} "
                    f"points at ratio {best.ratio:g} (table {best.last_verified.isoformat()}). "
                    "Transfer after the seat is visible, then book immediately."
                ),
            )
        )
    elif paths:
        steps.append(
            PlaybookStep(
                kind="info",
                title="No funded transfer path",
                body=(
                    "The local transfer table lists partners for this program, but none of "
                    "the named balances cover the points. Do not invent a missing partner."
                ),
            )
        )
    else:
        steps.append(
            PlaybookStep(
                kind="info",
                title="No transfer table row",
                body=f"No local transfer partner is recorded for {award.program}.",
            )
        )
    if award.evidence != "confirmed":
        steps.append(
            PlaybookStep(
                kind="warning",
                title="Availability is not confirmed",
                body=f"This offer is {award.evidence}. It is not live award inventory.",
            )
        )
    return tuple(steps)


def compare_award(
    award: AwardOffer,
    *,
    cash_price: Optional[float] = None,
    currency: Optional[str] = None,
    balances: Sequence[PointsBalance] = (),
) -> AwardCompareReport:
    quote = normalize_currency(currency) if currency else award.currency
    if cash_price is not None and quote is None:
        raise ValueError("cash_price needs a named currency")
    cpp = None
    if cash_price is not None:
        cpp = cents_per_point(cash_price, award.points, taxes=award.taxes)
    paths = transfer_paths(award.program, award.points, balances)
    return AwardCompareReport(
        searched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        award=award,
        transfer_paths=paths,
        playbook=award_playbook(award, paths),
        cash_price=cash_price,
        currency=quote,
        cpp_cents=cpp,
    )


def write_award_compare_atomic(report: AwardCompareReport, destination: Path) -> None:
    write_json_atomic(report.to_dict(), destination)
