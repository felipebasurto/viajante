from __future__ import annotations

import io
import math
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

from viajante.airports import (
    METRO_GROUPS,
    airport_geo,
    dest_blocked_by_exclude_regions,
    get_airport,
    is_known_iata,
    lookup_airports,
    matching_excluded_regions,
    metro_members,
    metro_of,
    owned_tz_region_tokens,
    parse_exclude_regions,
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

    def test_lisboa_finds_lisbon_not_only_huambo(self) -> None:
        codes = [row.iata for row in lookup_airports("Lisboa")]
        self.assertIn("LIS", codes)
        self.assertEqual(codes[0], "LIS")

    def test_ciudad_de_mexico_finds_mex(self) -> None:
        codes = {row.iata for row in lookup_airports("Ciudad de México")}
        self.assertIn("MEX", codes)

    def test_sapporo_includes_new_chitose_ahead_of_okadama(self) -> None:
        codes = [row.iata for row in lookup_airports("Sapporo")]
        self.assertIn("CTS", codes)
        self.assertIn("OKD", codes)
        self.assertLess(codes.index("CTS"), codes.index("OKD"))

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


def _km(a: str, b: str) -> float:
    geo_a, geo_b = airport_geo(a), airport_geo(b)
    assert geo_a is not None and geo_b is not None
    lat1, lon1, lat2, lon2 = map(math.radians, (geo_a[1], geo_a[2], geo_b[1], geo_b[2]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


class MetroTableTests(unittest.TestCase):
    def test_every_metro_is_provable_from_the_airport_table(self) -> None:
        seen: dict[str, str] = {}
        for metro, members in METRO_GROUPS.items():
            with self.subTest(metro=metro):
                self.assertFalse(is_known_iata(metro), "a metro code must not also be an airport")
                self.assertGreaterEqual(len(members), 2)
                airports = [get_airport(code) for code in members]
                self.assertTrue(all(airports), f"unknown member in {members}")
                self.assertEqual(len({a.country for a in airports if a}), 1)
                zones = {(airport_geo(code) or ("?",))[0] for code in members}
                self.assertEqual(len(zones), 1, zones)
                widest = max(_km(x, y) for x in members for y in members)
                self.assertLessEqual(widest, 100.0)
                for code in members:
                    self.assertNotIn(code, seen, f"{code} already in {seen.get(code)}")
                    seen[code] = metro

    def test_named_metro_lists_members_and_members_name_their_metro(self) -> None:
        self.assertEqual(metro_members("nyc"), ("JFK", "EWR", "LGA"))
        self.assertEqual(metro_members("JFK"), ())
        self.assertEqual(metro_of("ewr"), "NYC")
        self.assertIsNone(metro_of("MAD"))

    def test_lookup_surfaces_metro_codes(self) -> None:
        self.assertEqual([row.iata for row in lookup_airports("NYC")], ["JFK", "EWR", "LGA"])
        self.assertEqual([row.iata for row in lookup_airports("LHR")], ["LHR"])
        london = {row.iata: row.to_dict() for row in lookup_airports("london")}
        self.assertEqual(london["LHR"]["metro"], "LON")
        self.assertNotIn("metro", get_airport("MAD").to_dict())  # type: ignore[union-attr]


class ExcludeRegionsParseTests(unittest.TestCase):
    def test_named_asia_europe_are_owned_iana_prefixes(self) -> None:
        tokens = owned_tz_region_tokens()
        self.assertIn("asia", tokens)
        self.assertIn("europe", tokens)
        self.assertEqual(parse_exclude_regions("asia"), ("asia",))
        self.assertEqual(parse_exclude_regions("Asia,EUROPE"), ("asia", "europe"))
        self.assertIsNone(parse_exclude_regions(None))

    def test_unknown_token_is_rejected_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_exclude_regions("schengen")
        with self.assertRaises(ValueError):
            parse_exclude_regions("")
        with self.assertRaises(ValueError):
            parse_exclude_regions("india")

    def test_nrt_proves_asia_unknown_tz_cannot_prove_keep(self) -> None:
        self.assertEqual(matching_excluded_regions("NRT", ("asia",)), ("asia",))
        self.assertEqual(matching_excluded_regions("LHR", ("asia",)), ())
        self.assertEqual(matching_excluded_regions("XXX", ("asia",)), ())
        self.assertFalse(dest_blocked_by_exclude_regions("LHR", ("asia",)))
        self.assertTrue(dest_blocked_by_exclude_regions("NRT", ("asia",)))
        self.assertTrue(dest_blocked_by_exclude_regions("XXX", ("asia",)))
        self.assertFalse(dest_blocked_by_exclude_regions("NRT", ()))
        self.assertFalse(dest_blocked_by_exclude_regions("XXX", ()))


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

    def test_airports_metro_code_prints_members_with_their_metro(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["airports", "NYC"])
        self.assertEqual(code, 0)
        lines = buffer.getvalue().splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["JFK", "EWR", "LGA"])
        self.assertTrue(all(line.endswith("metro NYC") for line in lines))

    def test_airports_help_lists_examples(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["airports", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("viajante airports london", buffer.getvalue())


class BundledAirportNoticeTests(unittest.TestCase):
    def test_packaged_notice_includes_airportsdata_mit(self) -> None:
        from importlib.resources import files

        text = files("viajante").joinpath("THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        self.assertIn("airportsdata", text)
        self.assertIn("Permission is hereby granted", text)
        self.assertIn("Mike Borsetti", text)


if __name__ == "__main__":
    unittest.main()
