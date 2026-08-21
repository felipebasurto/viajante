from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from viajante.airports import (
    airport_geo,
    get_airport,
    is_known_iata,
    lookup_airports,
    same_city_iata,
)
from viajante.cli import main
from viajante.models import FlightQuery


class AirportLookupTests(unittest.TestCase):
    def test_mad_resolves(self) -> None:
        airport = get_airport("mad")
        assert airport is not None
        self.assertEqual(airport.iata, "MAD")
        self.assertIn("Madrid", airport.city)
        self.assertTrue(is_known_iata("MAD"))
        self.assertEqual([row.iata for row in lookup_airports("MAD")], ["MAD"])

    def test_london_finds_heathrow_and_gatwick(self) -> None:
        rows = lookup_airports("london")
        codes = {row.iata for row in rows}
        self.assertTrue({"LHR", "LGW", "STN"} <= codes)
        majors = [row.iata for row in rows if row.iata in {"LHR", "LGW", "STN", "LCY", "LTN"}]
        first = [row.iata for row in rows[:5]]
        self.assertEqual(majors, first)
        self.assertNotEqual(rows[0].iata, "BQH")
        self.assertLess(first.index("LHR"), 5)

    def test_barcelona_finds_bcn(self) -> None:
        rows = lookup_airports("barcelona")
        self.assertIn("BCN", {row.iata for row in rows})

    def test_xxx_is_not_an_airport(self) -> None:
        self.assertFalse(is_known_iata("XXX"))
        self.assertIsNone(get_airport("XXX"))
        with self.assertRaises(ValueError):
            FlightQuery("XXX", "BCN", date(2026, 9, 1))

    def test_blank_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            lookup_airports("   ")

    def test_same_city_iata_expands_london_and_tokyo_majors(self) -> None:
        london = same_city_iata("LHR")
        self.assertEqual(london[0], "LHR")
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= set(london))
        self.assertNotIn("BQH", london)
        self.assertNotIn("NHT", london)
        gatwick = same_city_iata("LGW")
        self.assertEqual(gatwick[0], "LGW")
        self.assertIn("LHR", gatwick)
        tokyo = same_city_iata("NRT")
        self.assertEqual(tokyo[0], "NRT")
        self.assertEqual(set(tokyo), {"NRT", "HND"})
        self.assertEqual(same_city_iata("MAD"), ("MAD",))
        self.assertEqual(same_city_iata("XXX"), ())

    def test_airport_geo_has_tz_for_idl_pair(self) -> None:
        hnl = airport_geo("HNL")
        akl = airport_geo("AKL")
        assert hnl is not None and akl is not None
        self.assertEqual(hnl[0], "Pacific/Honolulu")
        self.assertEqual(akl[0], "Pacific/Auckland")
        self.assertLess(hnl[2], 0)
        self.assertGreater(akl[2], 0)
        self.assertIsNone(airport_geo("XXX"))


class AirportCliTests(unittest.TestCase):
    def test_airports_mad_prints_the_code(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["airports", "MAD"])
        self.assertEqual(code, 0)
        self.assertIn("MAD", buffer.getvalue())
        self.assertIn("Madrid", buffer.getvalue())

    def test_airports_london_prints_major_codes(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["airports", "london"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("LHR", output)
        self.assertIn("LGW", output)

    def test_airports_help_lists_examples(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["airports", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("viajante airports london", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
