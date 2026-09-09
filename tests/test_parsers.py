from __future__ import annotations

import unittest

from viajante.models import CancellationEvidence, LodgingKind, PropertyTypeEvidence
from viajante.parsers import (
    normalize_clock,
    parse_cancellation_evidence,
    parse_duration_hours,
    parse_lodging_kind,
    parse_price,
    parse_property_type_evidence,
    parse_rating,
    parse_stops_count,
    parse_unit_hints,
)

PRICE_CASES = [
    ("€129", 129.0),
    ("€1234", 1234.0),
    ("€1234.56", 1234.56),
    ("1.024 €", 1024.0),
    ("520 €", 520.0),
    ("12,50 €", 12.5),
    ("€1,024.50", 1024.5),
    ("1.234.567 €", 1234567.0),
    ("235\xa0€", 235.0),
    ("€ 99", 99.0),
    ("120.50", 120.5),
    ("-20 €", -20.0),
    ("Ryanair - 120 €", 120.0),
    ("BHD 12.345", 12.345),
    ("CHF 1'234", 1234.0),
    ("CHF 1\u2019234", 1234.0),
    ("USD 1,2,3", None),
    ("", None),
    ("gratis", None),
]

DURATION_CASES = [
    ("15 h 45 min", 15.75),
    ("8 h 20 min", 8 + 20 / 60),
    ("55 min", 55 / 60),
    ("2 h", 2.0),
    ("2 hr 50 min", 2 + 50 / 60),
    ("1 day 3 hr", 27.0),
    ("2 days", 48.0),
    ("1 day 2 hr 30 min", 26.5),
    ("", None),
    (None, None),
    ("overnight", None),
]

STOPS_CASES = [
    ("Nonstop", 0),
    ("Non-stop", 0),
    ("Direct", 0),
    ("Directo", None),
    ("Sin escalas", None),
    ("Sin paradas", None),
    ("1 stop", 1),
    ("Unknown", None),
    (None, None),
]


class ParserTests(unittest.TestCase):
    def test_parse_price(self) -> None:
        for text, want in PRICE_CASES:
            with self.subTest(text=text):
                self.assertEqual(parse_price(text), want)

    def test_parse_duration_hours(self) -> None:
        for text, want in DURATION_CASES:
            with self.subTest(text=text):
                got = parse_duration_hours(text)
                if want is None:
                    self.assertIsNone(got)
                else:
                    self.assertIsNotNone(got)
                    assert got is not None
                    self.assertAlmostEqual(got, want)

    def test_parse_stops_count(self) -> None:
        for text, want in STOPS_CASES:
            with self.subTest(text=text):
                self.assertEqual(parse_stops_count(text), want)

    def test_normalize_clock_unifies_sweep_and_detail(self) -> None:
        cases = [
            ("1:40 PM", "13:40"),
            ("13:40", "13:40"),
            ("9:05 AM", "09:05"),
            ("09:05", "09:05"),
            ("12:10 AM", "00:10"),
            ("12:00 PM", "12:00"),
            ("10:35 AM on Fri, Oct 9", "10:35"),
            ("1:10 PM on Sat, Oct 10", "13:10"),
            ("23:10+1", "23:10"),
            ("00:30", "00:30"),
            ("24:05", "00:05"),
            ("24:30", "00:30"),
            (None, None),
            ("", None),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(normalize_clock(text), want)

    def test_overnight_durations_are_not_undercounted(self) -> None:
        overnight = parse_duration_hours("1 day 3 hr")
        same_day = parse_duration_hours("10 hr")
        assert overnight is not None and same_day is not None
        self.assertGreater(overnight, same_day)

    def test_parse_rating(self) -> None:
        cases = [
            ("Rating: 8.4", 8.4),
            ("Scored 8.5", 8.5),
            ("9", 9.0),
            ("Rating: 7.2", 7.2),
            ("Rating: 10.0", 10.0),
            ("8,7 Fabulous", 8.7),
            ("Fabulous 8,7", 8.7),
            ("1.234 reviews", None),
            ("Fabulous 1.234 reviews", None),
            ("1.234 reviews · 8,7", 8.7),
            ("", None),
            (None, None),
            ("2 bedrooms 3 beds", None),
            ("Rating: 11.0", None),
            ("Scored -1", None),
            ("Puntuación: 8,4", 8.4),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_rating(text), want)

    def test_parse_cancellation_evidence(self) -> None:
        cases = [
            ("Free cancellation", CancellationEvidence.FREE),
            ("Non-refundable", CancellationEvidence.NON_REFUNDABLE),
            (
                "Free cancellation. No cancellation fees.",
                CancellationEvidence.FREE,
            ),
            ("No free cancellation", CancellationEvidence.UNKNOWN),
            (
                "No free cancellation. Non-refundable",
                CancellationEvidence.NON_REFUNDABLE,
            ),
            (
                "Breakfast\nFree cancellation",
                CancellationEvidence.FREE,
            ),
            (
                "Hotel Bruno\nFree cancellation",
                CancellationEvidence.FREE,
            ),
            (
                "Modern apartment\nFree cancellation",
                CancellationEvidence.FREE,
            ),
            ("Casino free cancellation", CancellationEvidence.FREE),
            ("Cancelación gratuita", CancellationEvidence.UNKNOWN),
            ("No reembolsable", CancellationEvidence.UNKNOWN),
            ("", CancellationEvidence.UNKNOWN),
            (None, CancellationEvidence.UNKNOWN),
            ("Price per night", CancellationEvidence.UNKNOWN),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_cancellation_evidence(text), want)

    def test_parse_property_type_evidence(self) -> None:
        cases = [
            ("Entire home", PropertyTypeEvidence.ENTIRE_HOME),
            ("Whole place", PropertyTypeEvidence.ENTIRE_HOME),
            ("Apartamento entero", PropertyTypeEvidence.UNKNOWN),
            ("Private room", PropertyTypeEvidence.NOT_ENTIRE_HOME),
            ("Shared room", PropertyTypeEvidence.NOT_ENTIRE_HOME),
            ("Hotel room", PropertyTypeEvidence.NOT_ENTIRE_HOME),
            ("Habitación privada", PropertyTypeEvidence.UNKNOWN),
            ("2 bedrooms", PropertyTypeEvidence.UNKNOWN),
            ("", PropertyTypeEvidence.UNKNOWN),
            (None, PropertyTypeEvidence.UNKNOWN),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_property_type_evidence(text), want)

    def test_parse_lodging_kind(self) -> None:
        cases = [
            ("Entire home", LodgingKind.ENTIRE_HOME),
            ("Apartamento entero", LodgingKind.UNKNOWN),
            ("Private room", LodgingKind.PRIVATE_ROOM),
            ("Shared room", LodgingKind.PRIVATE_ROOM),
            ("Hotel room", LodgingKind.HOTEL),
            ("Habitación de hotel", LodgingKind.UNKNOWN),
            ("Hotel Bruno\nFree cancellation", LodgingKind.UNKNOWN),
            ("2 bedrooms", LodgingKind.UNKNOWN),
            ("", LodgingKind.UNKNOWN),
            (None, LodgingKind.UNKNOWN),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_lodging_kind(text), want)
        self.assertEqual(
            parse_lodging_kind("Wifi included", title="Vinohrady Apartment"),
            LodgingKind.ENTIRE_HOME,
        )
        self.assertEqual(
            parse_lodging_kind("Wifi included", title="Casa Azul"),
            LodgingKind.UNKNOWN,
        )
        self.assertEqual(
            parse_lodging_kind("Wifi included", title="Hotel Bruno"),
            LodgingKind.UNKNOWN,
        )
        self.assertEqual(
            parse_lodging_kind("Private room", title="Vinohrady Apartment"),
            LodgingKind.PRIVATE_ROOM,
        )

    def test_parse_unit_hints(self) -> None:
        cases = [
            (
                "2 bedrooms · 1 bathroom · 3 beds",
                {"bedrooms": 2, "bathrooms": 1, "beds": 3},
            ),
            (
                "2 dormitorios · 1 baño · 3 camas",
                {"bedrooms": None, "bathrooms": None, "beds": None},
            ),
            (
                "1 bedroom · 1 bathroom",
                {"bedrooms": 1, "bathrooms": 1, "beds": None},
            ),
            ("", {"bedrooms": None, "bathrooms": None, "beds": None}),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_unit_hints(text), want)


if __name__ == "__main__":
    unittest.main()
