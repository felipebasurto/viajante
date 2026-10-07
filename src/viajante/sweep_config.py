"""Validated runtime configuration for the Google Flights sweep transport."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

SweepMode = Literal["standard", "conservative"]

_MODE_CONCURRENCY: dict[SweepMode, int] = {
    "standard": 8,
    "conservative": 2,
}


@dataclass(frozen=True)
class SweepConfig:
    mode: SweepMode
    concurrency: int


def get_sweep_config() -> SweepConfig:
    """Read and validate sweep mode; invalid values fail before provider work."""
    raw_mode = os.environ.get("VIAJANTE_SWEEP_MODE", "standard")
    if raw_mode not in _MODE_CONCURRENCY:
        choices = ", ".join(_MODE_CONCURRENCY)
        raise ValueError(f"VIAJANTE_SWEEP_MODE must be one of: {choices}; got {raw_mode!r}")
    mode: SweepMode = raw_mode  # type: ignore[assignment]
    return SweepConfig(mode=mode, concurrency=_MODE_CONCURRENCY[mode])
