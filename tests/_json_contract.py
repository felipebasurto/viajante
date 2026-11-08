"""Shared JSON-contract checks for the report tests.

A price carries its currency in a field, never in its key name, and no key names an emissions
figure. The forbidden set is the union of every report's list, so each report is judged by all
of them.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

FORBIDDEN_KEYS = frozenset(
    {
        "baggage_buffer_eur",
        "carbon",
        "cheapest_eur",
        "co2",
        "co2_kg",
        "emissions",
        "flight_fare_eur",
        "hotel_stay_eur",
        "max_eur",
        "median_eur",
        "min_eur",
        "price_cap_eur",
        "price_eur",
        "price_usd",
        "total_eur",
        "total_price_eur",
        "typical_eur",
    }
)


def assert_no_forbidden_keys(case: unittest.TestCase, data: Any) -> None:
    blob = json.dumps(data)
    for key in sorted(FORBIDDEN_KEYS):
        case.assertNotIn(f'"{key}"', blob)


def assert_json_serialisable(data: Any) -> None:
    json.loads(json.dumps(data, ensure_ascii=False))
