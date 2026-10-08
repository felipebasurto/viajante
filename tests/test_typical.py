from __future__ import annotations

import unittest
from datetime import date

import _isolate  # noqa: F401
from viajante.models import (
    NEAR_TYPICAL_RATIO,
    FlightOffer,
)
from viajante.typical import (
    MIN_DAILY_PRICES,
    typical_from_daily_prices,
    vs_typical,
    vs_typical_pct,
    with_typical,
)


def _offer(*, price: float = 100.0) -> FlightOffer:
    return FlightOffer(
        airline="Example Air",
        departure="08:00",
        arrival="10:00",
        price_text=f"€{price:.0f}",
        price=price,
        duration="2 hr",
        duration_hours=2.0,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer=0,
        needs_bag_verify=False,
    )


class TypicalFromDailyPricesTests(unittest.TestCase):
    def test_floor_and_band_are_pinned(self) -> None:
        self.assertEqual(MIN_DAILY_PRICES, 3)
        self.assertEqual(NEAR_TYPICAL_RATIO, 0.10)

    def test_median_of_owned_daily_prices(self) -> None:
        self.assertEqual(
            typical_from_daily_prices((300.0, 340.0, 360.0, 400.0, 420.0)),
            360.0,
        )
        self.assertEqual(typical_from_daily_prices((81.0, 67.0, 81.0)), 81.0)
        self.assertEqual(typical_from_daily_prices((10.0, 20.0, 30.0, 40.0)), 25.0)

    def test_missing_or_thin_lists_are_not_invented(self) -> None:
        self.assertIsNone(typical_from_daily_prices(()))
        self.assertIsNone(typical_from_daily_prices((None, None)))
        self.assertIsNone(typical_from_daily_prices((120.0,)))
        self.assertIsNone(typical_from_daily_prices((120.0, 80.0)))
        self.assertIsNone(typical_from_daily_prices((0.0, -5.0, 40.0, 50.0)))
        self.assertIsNone(typical_from_daily_prices((None, 40.0, None, 50.0)))


class VsTypicalTests(unittest.TestCase):
    def test_coarse_band_is_ten_percent(self) -> None:
        self.assertEqual(vs_typical(89.0, 100.0), "below")
        self.assertEqual(vs_typical(90.0, 100.0), "near")
        self.assertEqual(vs_typical(100.0, 100.0), "near")
        self.assertEqual(vs_typical(110.0, 100.0), "near")
        self.assertEqual(vs_typical(111.0, 100.0), "above")

    def test_no_typical_means_no_label(self) -> None:
        self.assertIsNone(vs_typical(50.0, None))
        self.assertIsNone(vs_typical(50.0, 0.0))
        self.assertIsNone(vs_typical_pct(50.0, None))
        self.assertIsNone(vs_typical_pct(50.0, 0.0))

    def test_signed_percent_is_fare_versus_median(self) -> None:
        self.assertEqual(vs_typical_pct(289.0, 340.0), -15)
        self.assertEqual(vs_typical_pct(340.0, 340.0), 0)
        self.assertEqual(vs_typical_pct(400.0, 340.0), 18)

    def test_offer_stamp_is_all_or_nothing(self) -> None:
        stamped = with_typical(_offer(price=289.0), 340.0)
        self.assertEqual(stamped.typical, 340.0)
        self.assertEqual(stamped.vs_typical, "below")
        self.assertEqual(stamped.vs_typical_pct, -15)
        self.assertEqual(stamped.typical_deal(currency="EUR"), "below typical 340 € (−15%)")
        omitted = with_typical(_offer(price=289.0), None)
        self.assertIsNone(omitted.typical)
        self.assertIsNone(omitted.vs_typical)
        self.assertIsNone(omitted.vs_typical_pct)
        self.assertIsNone(omitted.typical_deal(currency="EUR"))


class TypicalModelTests(unittest.TestCase):
    def test_typical_fields_must_be_paired(self) -> None:
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price_text="€100",
                price=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer=0,
                needs_bag_verify=False,
                typical=120.0,
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price_text="€100",
                price=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer=0,
                needs_bag_verify=False,
                vs_typical="below",
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price_text="€100",
                price=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer=0,
                needs_bag_verify=False,
                typical=120.0,
                vs_typical="below",
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price_text="€100",
                price=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer=0,
                needs_bag_verify=False,
                cheapest_date=date(2026, 9, 15),
                cheapest=80.0,
            )
        with self.assertRaises(ValueError):
            FlightOffer(
                airline="Air",
                departure="08:00",
                arrival="10:00",
                price_text="€100",
                price=100.0,
                duration="2 hr",
                duration_hours=2.0,
                stops="Nonstop",
                stops_count=0,
                baggage_buffer=0,
                needs_bag_verify=False,
                typical=120.0,
                vs_typical="near",
                vs_typical_pct=0,
                cheapest_date=date(2026, 9, 15),
            )


if __name__ == "__main__":
    unittest.main()
