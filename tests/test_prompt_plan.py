from __future__ import annotations

import unittest
from datetime import date

from viajante.airports import is_known_iata
from viajante.flights import parse_flight_plan, plan_unit_count
from viajante.google_flights_rpc import build_shopping_inner
from viajante.models import HotelQuery, MultiCity, RoundTrip, Trip
from viajante.prompt_plan import _IATA_TO_ENGLISH, plan_prompt, plan_to_trips


def _shopping_bags_slot(trip: Trip) -> object:
    return build_shopping_inner(trip)[1][10]


class PromptPlanSmokeTests(unittest.TestCase):
    def test_bos_lhr_one_date(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 9, 1))
        self.assertEqual(plan.trip, "one-way")
        self.assertEqual(plan.refuse, ())
        parsed = parse_flight_plan(
            plan.route_specs,
            trip=plan.trip or "one-way",
            max_stops=plan.max_stops if plan.max_stops is not None else 1,
            adults=plan.adults or 1,
            cabin=plan.cabin or "economy",
        )
        self.assertEqual(parsed[0].origin, "BOS")
        self.assertEqual(parsed[0].destination, "LHR")
        self.assertIsNone(plan.currency)
        self.assertIsNone(plan.country)

    def test_named_currency_country_flags_land_on_flights(self) -> None:
        plan = plan_prompt("Flights JFK-LHR on 2026-09-15 --currency usd --country us")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.currency, "USD")
        self.assertEqual(plan.country, "US")

    def test_boston_london_city_names(self) -> None:
        plan = plan_prompt("I want to fly from Boston to London on 2026-09-04")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 9, 4))

    def test_airports_lookup_london(self) -> None:
        plan = plan_prompt("What is the IATA code for London airports?")
        self.assertEqual(plan.intent, "airports")
        self.assertEqual(plan.airports_query, "london")

    def test_invalid_iata_is_refused(self) -> None:
        plan = plan_prompt("Flights XXX-LHR on 2026-09-01")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("invalid_iata", plan.refuse)
        self.assertFalse(is_known_iata("XXX"))

    def test_missing_date_is_refused(self) -> None:
        plan = plan_prompt("Flights BOS-LHR")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("missing_date", plan.refuse)


class PromptPlanMediumTests(unittest.TestCase):
    def test_packaged_round_trip(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip BOS-LHR on 2026-10-09 returning 2026-10-12, --trip rt"
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 10, 9))
        self.assertEqual(plan.return_date, date(2026, 10, 12))
        parsed = parse_flight_plan(
            plan.route_specs,
            trip="rt",
            max_stops=1,
        )
        self.assertIsInstance(parsed, RoundTrip)
        self.assertEqual(parsed.return_date, date(2026, 10, 12))

    def test_packaged_round_trip_typical_prompt_stays_rt(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip YYZ-LIS on 2026-10-09 returning 2026-10-16, --trip rt. "
            "Is the fare cheap, typical, or expensive versus typical? Stamp "
            "typical / vs_typical when the same-stay grid has at least 3 priced days; "
            "omit both on a miss. Do not invent a fare or a typical."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.origin, "YYZ")
        self.assertEqual(plan.destination, "LIS")
        self.assertEqual(plan.departure_date, date(2026, 10, 9))
        self.assertEqual(plan.return_date, date(2026, 10, 16))
        self.assertEqual(plan.locale, "en")
        self.assertTrue(plan.require_return_legs)
        parsed = parse_flight_plan(plan.route_specs, trip="rt", max_stops=1)
        self.assertIsInstance(parsed, RoundTrip)

    def test_two_one_ways_sugar(self) -> None:
        plan = plan_prompt(
            "NRT-ICN outbound on 2026-10-09 and return on 2026-10-12 as two one-way, "
            "without --trip rt"
        )
        self.assertEqual(plan.trip, "one-way")
        self.assertEqual(len(plan.route_specs), 2)
        parsed = parse_flight_plan(plan.route_specs, trip="one-way", max_stops=1)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].origin, "NRT")
        self.assertEqual(parsed[1].origin, "ICN")

    def test_named_return_is_packaged_rt(self) -> None:
        plan = plan_prompt("LAX-NRT on 2026-11-03 returning 2026-11-12")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.route_specs, ("LAX-NRT:2026-11-03:2026-11-12",))
        parsed = parse_flight_plan(plan.route_specs, trip="rt", max_stops=1)
        self.assertIsInstance(parsed, RoundTrip)
        self.assertEqual(parsed.return_date, date(2026, 11, 12))

    def test_explicit_two_one_ways_are_not_rt(self) -> None:
        plan = plan_prompt(
            "BOM-DXB on 2026-09-25 returning 2026-09-28 as two one-way, without --trip rt"
        )
        self.assertEqual(plan.trip, "one-way")
        self.assertNotEqual(plan.trip, "rt")
        self.assertEqual(
            list(plan.route_specs),
            ["BOM-DXB:2026-09-25", "DXB-BOM:2026-09-28"],
        )

    def test_explore_from_sin(self) -> None:
        plan = plan_prompt("Explore cheap destinations from SIN starting 2026-09-15, 7 days")
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "SIN")
        self.assertEqual(plan.departure_date, date(2026, 9, 15))
        self.assertEqual(plan.days, 7)

    def test_dates_calendar(self) -> None:
        plan = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "JFK")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.date_from, date(2026, 9, 1))
        self.assertEqual(plan.date_to, date(2026, 9, 14))
        self.assertEqual(plan.route_specs, ())

    def test_cheapest_dates_with_nights_is_one_calendar_not_daily_flights(self) -> None:
        plan = plan_prompt("cheapest dates BOS-LHR in November 2026, 5 nights")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.date_from, date(2026, 11, 1))
        self.assertEqual(plan.date_to, date(2026, 11, 30))
        self.assertEqual(plan.days, 5)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ())
        self.assertNotEqual(len(plan.route_specs), 30)

    def test_flex_around_window_then_one_packaged_search(self) -> None:
        plan = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights")
        self.assertEqual(plan.intent, "flex")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 9, 12))
        self.assertEqual(plan.date_from, date(2026, 9, 9))
        self.assertEqual(plan.date_to, date(2026, 9, 15))
        self.assertEqual(plan.flex_days, 3)
        self.assertEqual(plan.days, 7)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ())

    def test_dates_command_with_nights_is_calendar_not_legs(self) -> None:
        plan = plan_prompt("viajante dates BOS-LHR --from 2026-11-01 --to 2026-11-30 --nights 5")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.date_from, date(2026, 11, 1))
        self.assertEqual(plan.date_to, date(2026, 11, 30))
        self.assertEqual(plan.days, 5)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ())

    def test_hyphenated_nights_stay_on_cheapest_dates(self) -> None:
        plan = plan_prompt("cheapest dates BOS-LHR in November 2026, 5-night stay")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.days, 5)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.route_specs, ())

    def test_around_plus_minus_is_flex_not_one_day(self) -> None:
        plan = plan_prompt("JFK-LHR around 15 Sep 2026, ±3 days, 7 nights")
        self.assertEqual(plan.intent, "flex")
        self.assertEqual(plan.origin, "JFK")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 9, 15))
        self.assertEqual(plan.date_from, date(2026, 9, 12))
        self.assertEqual(plan.date_to, date(2026, 9, 18))
        self.assertEqual(plan.flex_days, 3)
        self.assertEqual(plan.days, 7)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ())
        self.assertNotIn("€", plan.notes)
        slash = plan_prompt("JFK-LHR around 2026-09-15, +/- 3 days, 7 nights")
        self.assertEqual(slash.intent, "flex")
        self.assertEqual(slash.flex_days, 3)
        self.assertEqual(slash.date_from, date(2026, 9, 12))
        self.assertEqual(slash.date_to, date(2026, 9, 18))

    def test_around_date_defaults_a_real_flex_window(self) -> None:
        plan = plan_prompt("Flights BOS-LHR around 2026-09-12")
        self.assertEqual(plan.intent, "flex")
        self.assertEqual(plan.flex_days, 3)
        self.assertEqual(plan.departure_date, date(2026, 9, 12))
        self.assertEqual(plan.date_from, date(2026, 9, 9))
        self.assertEqual(plan.date_to, date(2026, 9, 15))
        self.assertEqual(plan.route_specs, ())
        self.assertEqual(plan.locale, "en")
        self.assertNotIn("€", plan.notes)

    def test_cheapest_week_is_dates_not_fixed_flights(self) -> None:
        plan = plan_prompt("Cheapest week NRT-SIN in November 2026")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "NRT")
        self.assertEqual(plan.destination, "SIN")
        self.assertEqual(plan.date_from, date(2026, 11, 1))
        self.assertEqual(plan.date_to, date(2026, 11, 30))
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ())
        self.assertNotEqual(plan.intent, "flights")
        self.assertNotIn("€", plan.notes)

    def test_flexible_dates_range_is_calendar_not_flex(self) -> None:
        plan = plan_prompt("Flexible dates BOS-LHR from 2026-09-01 to 2026-09-14")
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.date_from, date(2026, 9, 1))
        self.assertEqual(plan.date_to, date(2026, 9, 14))
        self.assertEqual(plan.route_specs, ())
        month = plan_prompt("Flexible dates BOS-LHR September 2026")
        self.assertEqual(month.intent, "dates")
        self.assertEqual(month.date_from, date(2026, 9, 1))
        self.assertEqual(month.date_to, date(2026, 9, 30))

    def test_hotels_tokyo_nights(self) -> None:
        plan = plan_prompt("Hotel in Tokyo from 2026-12-04 to 2026-12-07")
        self.assertEqual(plan.intent, "hotels")
        self.assertEqual(plan.location.casefold(), "tokyo")
        self.assertEqual(plan.check_in, date(2026, 12, 4))
        self.assertEqual(plan.check_out, date(2026, 12, 7))
        query = HotelQuery(
            plan.location,
            plan.check_in,
            plan.check_out,
            adults=plan.adults or 2,
            rooms=plan.rooms or 1,
        )
        self.assertEqual(query.nights, 3)

    def test_rooms_change_occupancy(self) -> None:
        one = plan_prompt("Hotel in Cape Town 2026-09-10 to 2026-09-12, 2 adults, 1 room")
        two = plan_prompt("Hotel in Cape Town 2026-09-10 to 2026-09-12, 2 adults, 2 rooms")
        self.assertEqual(one.rooms, 1)
        self.assertEqual(two.rooms, 2)
        self.assertNotEqual(one.rooms, two.rooms)
        self.assertIsNone(one.children)

    def test_open_jaw_is_multi(self) -> None:
        plan = plan_prompt(
            "Open jaw: GRU-SCL on 2026-09-08 and EZE-GRU on 2026-09-12, --trip multi"
        )
        self.assertEqual(plan.trip, "multi")
        self.assertGreaterEqual(len(plan.route_specs), 2)
        parsed = parse_flight_plan(plan.route_specs, trip="multi", max_stops=1)
        self.assertIsInstance(parsed, MultiCity)

    def test_packaged_open_jaw_yvr_lhr_lgw_builds_search(self) -> None:
        plan = plan_prompt(
            "YVR-LHR on 2026-10-09 and LGW-YVR on 2026-10-13 as a packaged "
            "round-trip --trip rt. Do not invent a fare."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.origin, "YVR")
        self.assertEqual(
            list(plan.route_specs),
            ["YVR-LHR:2026-10-09", "LGW-YVR:2026-10-13"],
        )
        self.assertIn("LHR", "".join(plan.route_specs))
        self.assertIn("LGW", "".join(plan.route_specs))
        self.assertNotEqual(plan.route_specs, ("YVR-LHR:2026-10-09:2026-10-13",))
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        parsed = plan_to_trips(plan)
        self.assertIsInstance(parsed, MultiCity)
        assert isinstance(parsed, MultiCity)
        self.assertEqual(
            [(leg.origin, leg.destination, leg.departure_date) for leg in parsed.legs],
            [
                ("YVR", "LHR", date(2026, 10, 9)),
                ("LGW", "YVR", date(2026, 10, 13)),
            ],
        )
        self.assertEqual(plan_unit_count(parsed), 1)

    def test_any_london_airport_sets_nearby_and_keeps_heathrow(self) -> None:
        plan = plan_prompt("One-way BOS to any London airport on 2026-09-18. Do not invent a fare.")
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.nearby)
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.departure_date, date(2026, 9, 18))
        parsed = plan_to_trips(plan)
        dests = {query.destination for query in parsed}
        self.assertEqual(parsed[0].destination, "LHR")
        self.assertTrue({"LHR", "LGW", "STN"} <= dests)
        city = plan_prompt("I want to fly from Boston to London on 2026-09-04")
        self.assertFalse(city.nearby)
        self.assertEqual(city.destination, "LHR")
        flagged = plan_prompt("BOS-LHR on 2026-09-18 --nearby")
        self.assertTrue(flagged.nearby)
        tokyo = plan_prompt("Flights LAX to any Tokyo airport on 2026-11-03")
        self.assertTrue(tokyo.nearby)
        self.assertEqual(tokyo.origin, "LAX")
        self.assertEqual(tokyo.destination, "NRT")
        in_city = plan_prompt(
            "One-way BOS to any airport in London on 2026-09-18. Do not invent a fare."
        )
        self.assertTrue(in_city.nearby)
        self.assertEqual(in_city.destination, "LHR")
        either = plan_prompt(
            "One-way BOS to either London airport on 2026-09-18. Do not invent a fare."
        )
        self.assertTrue(either.nearby)
        self.assertEqual(either.destination, "LHR")
        named = plan_prompt(
            "YVR-LHR on 2026-10-09 and LGW-YVR on 2026-10-13 as a packaged "
            "round-trip --trip rt --nearby. Do not invent a fare."
        )
        self.assertTrue(named.nearby)
        self.assertEqual(
            list(named.route_specs),
            ["YVR-LHR:2026-10-09", "LGW-YVR:2026-10-13"],
        )
        kept = plan_to_trips(named)
        self.assertIsInstance(kept, MultiCity)
        assert isinstance(kept, MultiCity)
        self.assertEqual(
            [(leg.origin, leg.destination) for leg in kept.legs],
            [("YVR", "LHR"), ("LGW", "YVR")],
        )


class PromptPlanHardTests(unittest.TestCase):
    def test_refuse_booking(self) -> None:
        plan = plan_prompt("Book now this JFK-LHR flight on 2026-09-01")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("booking", plan.refuse)

    def test_refuse_trains(self) -> None:
        plan = plan_prompt("Take the train from Boston to New York on 2026-09-01")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("trains", plan.refuse)

    def test_refuse_cars(self) -> None:
        plan = plan_prompt("Rental car in Johannesburg from 2026-09-01 to 2026-09-05")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("cars", plan.refuse)

    def test_sane_layovers(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03, sane layovers of at most 22 hours")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.max_layover, 22.0)

    def test_destinations_not_the_rest_of_the_trip(self) -> None:
        plan = plan_prompt(
            "Search destinations and prices from YVR on 2026-09-15, not the rest of the trip"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "YVR")
        self.assertIn("itinerary_rest", plan.refuse)

    def test_not_asia(self) -> None:
        plan = plan_prompt("Destinations from NRT on 2026-09-15, not Asia")
        self.assertEqual(plan.intent, "explore")
        self.assertIn("asia", plan.exclude_regions)

    def test_night_arrivals_need_a_clock(self) -> None:
        plan = plan_prompt(
            "ICN-LAX on 2026-09-18 at night; the arrival must show a clock, not null"
        )
        self.assertTrue(plan.require_arrival_clock)
        self.assertEqual(plan.fetch, "detail")

    def test_packaged_rt_requires_return_legs(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip SYD-AKL 2026-10-12 to 2026-10-26; must include return legs"
        )
        self.assertEqual(plan.trip, "rt")
        self.assertTrue(plan.require_return_legs)

    def test_explore_shortlist_does_not_read_first_as_cabin(self) -> None:
        plan = plan_prompt(
            "Explore destinations from GRU in September 2026: SCL, EZE, LIM, BOG. "
            "Do not brute-force the full date matrix; fixed dates first, then ±1 only on finalists."
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "GRU")
        self.assertEqual(plan.date_from, date(2026, 9, 1))
        self.assertEqual(plan.date_to, date(2026, 9, 30))
        self.assertEqual(list(plan.destinations), ["SCL", "EZE", "LIM", "BOG"])
        self.assertEqual(list(plan.include_airports), ["SCL", "EZE", "LIM", "BOG"])
        self.assertIsNone(plan.cabin)
        self.assertEqual(plan.date_strategy, "fixed_then_plus_minus_1")
        ok, reason = plan.matches(
            {
                "intent": "explore",
                "origin": "GRU",
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
                "destinations": ["SCL", "EZE", "LIM", "BOG"],
                "date_strategy": "fixed_then_plus_minus_1",
                "cabin": None,
            }
        )
        self.assertTrue(ok, reason)

    def test_first_cabin_is_still_first_class(self) -> None:
        plan = plan_prompt("SYD-LAX on 2026-11-03 first cabin")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.cabin, "first")

    def test_ba_or_kl_only_sets_include_airlines(self) -> None:
        plan = plan_prompt("LHR-AMS on 2026-09-15, BA or KL only")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(list(plan.include_airlines), ["BA", "KL"])
        self.assertEqual(list(plan.exclude_airlines), [])

    def test_not_dl_sets_exclude_airlines(self) -> None:
        plan = plan_prompt("JFK-LHR on 2026-09-15, not DL")
        self.assertEqual(list(plan.exclude_airlines), ["DL"])
        self.assertEqual(list(plan.include_airlines), [])

    def test_oneworld_and_not_star_alliance(self) -> None:
        oneworld = plan_prompt("MAD-JFK on 2026-09-15 oneworld")
        self.assertEqual(list(oneworld.alliance), ["oneworld"])
        not_star = plan_prompt("MAD-FRA on 2026-09-15 not star alliance")
        self.assertEqual(list(not_star.exclude_alliance), ["star"])
        self.assertEqual(list(not_star.alliance), [])

    def test_named_airline_and_flags(self) -> None:
        named = plan_prompt("MAD-LHR on 2026-09-15 british airways only")
        self.assertEqual(list(named.include_airlines), ["BA"])
        flagged = plan_prompt("MAD-AMS on 2026-09-15 --airlines BA,KL --exclude-alliance star")
        self.assertEqual(list(flagged.include_airlines), ["BA", "KL"])
        self.assertEqual(list(flagged.exclude_alliance), ["star"])

    def test_english_to_is_not_transavia(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01 to London")
        self.assertEqual(list(plan.include_airlines), [])
        self.assertEqual(list(plan.exclude_airlines), [])

    def test_single_code_only_sets_include_airlines(self) -> None:
        plan = plan_prompt("MAD-LHR on 2026-09-15 BA only")
        self.assertEqual(list(plan.include_airlines), ["BA"])
        self.assertEqual(list(plan.exclude_airlines), [])

    def test_code_or_named_airline(self) -> None:
        plan = plan_prompt("MAD-LHR on 2026-09-15 BA or Iberia")
        self.assertEqual(list(plan.include_airlines), ["IB", "BA"])
        slash = plan_prompt("MAD-LHR on 2026-09-15 BA/IB only")
        self.assertEqual(list(slash.include_airlines), ["BA", "IB"])

    def test_no_code_sets_exclude_airlines(self) -> None:
        plan = plan_prompt("MAD-BCN on 2026-09-01, no FR")
        self.assertEqual(list(plan.exclude_airlines), ["FR"])
        self.assertEqual(list(plan.include_airlines), [])

    def test_do_not_use_named_airline_is_exclude(self) -> None:
        plan = plan_prompt("MAD-BCN on 2026-09-01, don't use Ryanair")
        self.assertIn("FR", plan.exclude_airlines)
        self.assertNotIn("FR", plan.include_airlines)
        spanish = plan_prompt("MAD-BCN el 2026-09-01, sin Ryanair")
        self.assertIn("FR", spanish.exclude_airlines)
        self.assertNotIn("FR", spanish.include_airlines)

    def test_star_alliance_only_does_not_invent_airline_code(self) -> None:
        plan = plan_prompt("MAD-JFK on 2026-09-15, Star Alliance only")
        self.assertEqual(list(plan.alliance), ["star"])
        self.assertEqual(list(plan.include_airlines), [])
        sky = plan_prompt("MAD-JFK on 2026-09-15, only skyteam")
        self.assertEqual(list(sky.alliance), ["skyteam"])
        self.assertEqual(list(sky.include_airlines), [])


class PromptPlanInsaneTests(unittest.TestCase):
    def test_halifax_fiji_via_continents(self) -> None:
        plan = plan_prompt(
            "Go to Fiji from Halifax passing through 2 European airports, "
            "one sub-Saharan, one Indian, one Chinese, and New Zealand. "
            "Leave 2026-11-02."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "YHZ")
        self.assertEqual(plan.destination, "NAN")
        self.assertEqual(plan.departure_date, date(2026, 11, 2))
        self.assertEqual(plan.trip, "one-way")
        self.assertTrue(plan.split_packages)
        self.assertEqual(
            list(plan.via_regions),
            ["europe", "europe", "sub_saharan", "india", "china", "new_zealand"],
        )
        # Named via_regions is a shortlist, not a license to invent hops or fares.
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.prefer_airports, ())
        self.assertEqual(plan.route_specs, ("YHZ-NAN:2026-11-02",))
        invented = {"LHR", "AMS", "CDG", "JNB", "NBO", "DEL", "BOM", "PEK", "PVG", "AKL"}
        owned = " ".join(plan.route_specs) + " " + " ".join(plan.via_airports)
        for code in invented:
            self.assertNotIn(code, owned)
            self.assertNotIn(code, plan.notes)
        folded = plan.notes.casefold()
        self.assertIn("via_regions", folded)
        self.assertIn("shortlist", folded)
        self.assertIn("constraint", folded)
        self.assertIn("do not invent", folded)
        self.assertIn("airport", folded)
        self.assertIn("fare", folded)
        self.assertIn("hop", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        unnamed = plan_prompt("Flights BOS-LHR on 2026-09-01")
        self.assertEqual(unnamed.via_regions, ())
        self.assertNotIn("via_regions", unnamed.notes.casefold())

    def test_contradictory_dates(self) -> None:
        plan = plan_prompt("Outbound JFK-LHR on 2026-09-20 and return on 2026-09-10")
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("contradictory_dates", plan.refuse)

    def test_eight_adults_one_room(self) -> None:
        plan = plan_prompt("Hotel in Tokyo, 8 adults, 1 room, 2026-12-04 to 2026-12-07")
        self.assertEqual(plan.intent, "hotels")
        self.assertEqual(plan.adults, 8)
        self.assertEqual(plan.rooms, 1)
        query = HotelQuery("Tokyo", date(2026, 12, 4), date(2026, 12, 7), adults=8, rooms=1)
        self.assertEqual(query.adults, 8)
        self.assertEqual(query.rooms, 1)

    def test_around_the_world_max_two_stops(self) -> None:
        plan = plan_prompt(
            "Cheapest around the world from Vancouver starting 2026-11-01, max 2 stops each leg"
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.around_the_world)
        self.assertEqual(plan.max_stops, 2)
        self.assertEqual(plan.origin, "YVR")
        # Circuit returns to the named origin. Do not invent intermediate cities.
        self.assertEqual(plan.destination, "YVR")
        self.assertEqual(plan.trip, "multi")
        self.assertEqual(plan.departure_date, date(2026, 11, 1))
        self.assertEqual(plan.route_specs, ())
        self.assertEqual(plan.prefer_airports, ())
        folded_notes = plan.notes.casefold()
        self.assertIn("shortlist", folded_notes)
        self.assertIn("max_stops 2", folded_notes)
        self.assertIn("every leg", folded_notes)
        self.assertIn("do not invent fares", folded_notes)
        self.assertNotIn("€", plan.notes)
        self.assertNotIn("EUR", plan.notes)
        self.assertNotEqual(plan.origin, "MAD")
        self.assertNotIn("MAD", "".join(plan.route_specs))

    def test_around_the_world_does_not_invent_madrid(self) -> None:
        plan = plan_prompt("Cheapest around the world starting 2026-11-01, max 2 stops each leg")
        self.assertTrue(plan.around_the_world)
        self.assertEqual(plan.max_stops, 2)
        self.assertEqual(plan.trip, "multi")
        self.assertEqual(plan.route_specs, ())
        self.assertNotEqual(plan.origin, "MAD")
        self.assertNotEqual(plan.destination, "MAD")

    def test_city_name_triple_open_jaw_keeps_all_pairs(self) -> None:
        plan = plan_prompt(
            "Three open jaws: from Sao Paulo to Santiago on 2026-09-07, "
            "from Buenos Aires to Lima on 2026-09-10, from Bogota to Sao Paulo "
            "on 2026-09-13."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "GRU")
        self.assertEqual(plan.destination, "GRU")
        self.assertEqual(plan.trip, "multi")
        self.assertEqual(
            list(plan.route_specs),
            [
                "GRU-SCL:2026-09-07",
                "EZE-LIM:2026-09-10",
                "BOG-GRU:2026-09-13",
            ],
        )
        self.assertNotEqual(plan.origin, "MAD")
        self.assertNotIn("MAD", "".join(plan.route_specs))
        parsed = parse_flight_plan(plan.route_specs, trip="multi", max_stops=1)
        self.assertIsInstance(parsed, MultiCity)
        self.assertEqual(len(parsed.legs), 3)
        self.assertEqual(parsed.legs[1].origin, "EZE")
        self.assertEqual(parsed.legs[1].destination, "LIM")
        self.assertEqual(parsed.legs[2].origin, "BOG")

    def test_five_continents_max_two_stops_is_impossible(self) -> None:
        plan = plan_prompt(
            "BOS to Sydney leaving 2026-11-01, touching Europe, sub-Saharan Africa, "
            "India, China, and New Zealand before SYD. Max 2 stops each packaged leg."
        )
        self.assertEqual(plan.intent, "refuse")
        self.assertIn("impossible_routing", plan.refuse)
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "SYD")
        self.assertEqual(plan.max_stops, 2)
        self.assertEqual(
            list(plan.via_regions),
            ["europe", "sub_saharan", "india", "china", "new_zealand"],
        )
        self.assertEqual(plan.route_specs, ())
        self.assertNotEqual(plan.origin, "MAD")
        folded_notes = plan.notes.casefold()
        self.assertIn("cannot", folded_notes)
        self.assertIn("5 via-regions", folded_notes)
        self.assertIn("max 2 stops", folded_notes)
        self.assertNotIn("€", plan.notes)
        self.assertNotIn("EUR", plan.notes)


class PromptPlanBrutalTests(unittest.TestCase):
    def test_baggage_carry_on_only(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03, carry-on only, max 1 stop.")
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.bags)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(plan.max_stops, 1)
        parsed = plan_to_trips(plan)
        self.assertIsNone(parsed[0].bags)
        self.assertEqual(parsed[0].carry_on, 1)
        self.assertEqual(_shopping_bags_slot(parsed[0]), [0, 1])

    def test_baggage_checked_1(self) -> None:
        plan = plan_prompt("DEL-LHR on 2026-11-10, 1 checked bag, business class, max 1 stop.")
        self.assertEqual(plan.baggage, "checked_1")
        self.assertEqual(plan.bags, 1)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.cabin, "business")
        parsed = plan_to_trips(plan)
        self.assertEqual(parsed[0].bags, 1)
        self.assertIsNone(parsed[0].carry_on)
        self.assertEqual(_shopping_bags_slot(parsed[0]), [1, 0])

    def test_baggage_no_checked(self) -> None:
        plan = plan_prompt("ICN-LAX on 2026-09-18, no checked bags, premium economy, max 1 stop.")
        self.assertEqual(plan.baggage, "no_checked")
        self.assertEqual(plan.bags, 0)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.cabin, "premium-economy")
        parsed = plan_to_trips(plan)
        self.assertEqual(parsed[0].bags, 0)
        self.assertIsNone(parsed[0].carry_on)
        self.assertEqual(_shopping_bags_slot(parsed[0]), [0, 0])

    def test_baggage_unnamed_leaves_shopping_slot_none(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03, max 1 stop.")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        parsed = plan_to_trips(plan)
        self.assertIsNone(parsed[0].bags)
        self.assertIsNone(parsed[0].carry_on)
        self.assertIsNone(_shopping_bags_slot(parsed[0]))

    def test_baggage_no_hold_luggage_is_bags_zero(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03, no hold luggage, max 1 stop.")
        self.assertEqual(plan.baggage, "no_checked")
        self.assertEqual(plan.bags, 0)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(_shopping_bags_slot(plan_to_trips(plan)[0]), [0, 0])

    def test_baggage_two_checked_ships_owned_count(self) -> None:
        plan = plan_prompt("DEL-LHR on 2026-11-10, 2 checked bags, max 1 stop.")
        self.assertIsNone(plan.baggage)
        self.assertEqual(plan.bags, 2)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(_shopping_bags_slot(plan_to_trips(plan)[0]), [2, 0])

    def test_baggage_flags_fill_the_pair(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03 --bags 1 --carry-on, max 1 stop.")
        self.assertEqual(plan.bags, 1)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(_shopping_bags_slot(plan_to_trips(plan)[0]), [1, 1])

    def test_baggage_contradiction_does_not_pick_one(self) -> None:
        plan = plan_prompt("OSL-EWR on 2026-11-06, carry-on only and 2 checked bags, max 1 stop.")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertIsNone(_shopping_bags_slot(plan_to_trips(plan)[0]))

    def test_baggage_spanish_carry_on_only(self) -> None:
        plan = plan_prompt("JNB-SIN el 2026-11-03, solo equipaje de mano, max 1 stop.")
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertEqual(plan.carry_on, 1)
        self.assertIsNone(plan.bags)
        self.assertEqual(_shopping_bags_slot(plan_to_trips(plan)[0]), [0, 1])

    def test_named_price_cap_fills_owned_path_and_leaves_index_7_none(self) -> None:
        under = plan_prompt("JFK-LHR on 2026-09-15 under 200€")
        self.assertEqual(under.price_cap, 200)
        parsed = plan_to_trips(under)
        self.assertEqual(parsed[0].price_cap, 200)
        self.assertIsNone(build_shopping_inner(parsed[0])[1][7])
        four = plan_prompt("NRT-ICN on 2026-10-09 --price-cap 400 EUR")
        self.assertEqual(four.price_cap, 400)
        self.assertEqual(plan_to_trips(four)[0].price_cap, 400)
        self.assertIsNone(build_shopping_inner(plan_to_trips(four)[0])[1][7])

    def test_unnamed_price_cap_leaves_index_7_none(self) -> None:
        plan = plan_prompt("JNB-SIN on 2026-11-03, max 1 stop.")
        self.assertIsNone(plan.price_cap)
        parsed = plan_to_trips(plan)
        self.assertIsNone(parsed[0].price_cap)
        self.assertIsNone(build_shopping_inner(parsed[0])[1][7])

    def test_spanish_named_price_cap_is_owned(self) -> None:
        plan = plan_prompt("JFK-LHR el 2026-09-15, menos de 200 €")
        self.assertEqual(plan.price_cap, 200)
        self.assertEqual(plan_to_trips(plan)[0].price_cap, 200)

    def test_arrive_before(self) -> None:
        plan = plan_prompt("SFO-LHR on 2026-11-03, arrive before 09:00, max 1 stop.")
        self.assertEqual(plan.arrive_before, "09:00")

    def test_depart_after(self) -> None:
        plan = plan_prompt("ORD-CDG on 2026-11-06, depart after Friday 18:00, max 1 stop.")
        self.assertEqual(plan.depart_after, "18:00")
        self.assertEqual(plan.weekday, "friday")
        folded = plan.notes.casefold()
        self.assertNotIn("weekday no-fly", folded)
        self.assertNotIn("owned clock", folded)

    def test_depart_window_and_sort_by_duration(self) -> None:
        plan = plan_prompt(
            "JFK-LHR on 2026-09-15, leave between 06:00 and 20:00, sort by duration, max 1 stop."
        )
        self.assertEqual(plan.depart_window, "06:00-20:00")
        self.assertEqual(plan.sort, "duration")
        self.assertIsNone(plan.depart_after)
        ampm = plan_prompt("JFK-LHR on 2026-09-15, leave between 6am and 8pm, sort by duration.")
        self.assertEqual(ampm.depart_window, "06:00-20:00")

    def test_depart_window_dash_and_sort_by_price(self) -> None:
        plan = plan_prompt("SIN-NRT on 2026-10-09, leave 07:30-18:00, sort by price.")
        self.assertEqual(plan.depart_window, "07:30-18:00")
        self.assertEqual(plan.sort, "price")

    def test_sort_by_departure_and_arrival(self) -> None:
        depart = plan_prompt("GRU-EZE on 2026-11-03, order by departure.")
        arrive = plan_prompt("GRU-EZE on 2026-11-03, sort by arrival.")
        self.assertEqual(depart.sort, "departure")
        self.assertEqual(arrive.sort, "arrival")

    def test_depart_window_and_sort_flags(self) -> None:
        plan = plan_prompt("MAD-BCN on 2026-09-01 --depart-window 6-20 --sort arrival")
        self.assertEqual(plan.depart_window, "6-20")
        self.assertEqual(plan.sort, "arrival")

    def test_hour_depart_window_and_cheapest_first(self) -> None:
        plan = plan_prompt("JFK-LHR on 2026-09-15, leave 6-20, cheapest first")
        self.assertEqual(plan.depart_window, "6-20")
        self.assertEqual(plan.sort, "price")
        between = plan_prompt("JFK-LHR on 2026-09-15, leave between 6-20")
        self.assertEqual(between.depart_window, "6-20")
        departing = plan_prompt("JFK-LHR on 2026-09-15, departing between 6:00 and 20:00")
        self.assertEqual(departing.depart_window, "06:00-20:00")

    def test_sort_shortest_first_and_bare_sort_duration(self) -> None:
        shortest = plan_prompt("SIN-NRT on 2026-10-09, shortest first")
        self.assertEqual(shortest.sort, "duration")
        bare = plan_prompt("SIN-NRT on 2026-10-09, sort duration")
        self.assertEqual(bare.sort, "duration")

    def test_morning_vibe_does_not_invent_a_depart_window(self) -> None:
        plan = plan_prompt("JFK-LHR on 2026-09-15, leave in the morning")
        self.assertIsNone(plan.depart_window)
        self.assertIsNone(plan.sort)

    def test_morning_vibe_does_not_invent_arrive_before_or_depart_after(self) -> None:
        plan = plan_prompt("JFK-LHR on 2026-09-15, arrive in the morning, depart late")
        self.assertIsNone(plan.arrive_before)
        self.assertIsNone(plan.depart_after)
        flagged = plan_prompt("JFK-LHR on 2026-09-15 --arrive-before 10:00 --depart-after 18:00")
        self.assertEqual(flagged.arrive_before, "10:00")
        self.assertEqual(flagged.depart_after, "18:00")

    def test_prefer_airports_lhr_not_lgw(self) -> None:
        plan = plan_prompt("GRU-LHR on 2026-09-08, use LHR not LGW, max 1 stop.")
        self.assertEqual(plan.destination, "LHR")
        self.assertIn("LHR", plan.prefer_airports)
        self.assertIn("LGW", plan.exclude_airports)

    def test_prefer_airports_ewr_not_jfk(self) -> None:
        plan = plan_prompt(
            "From New York to London on 2026-10-09, use EWR not JFK, LHR not LGW, max 1 stop."
        )
        self.assertEqual(plan.origin, "EWR")
        self.assertEqual(plan.destination, "LHR")
        self.assertIn("EWR", plan.prefer_airports)
        self.assertIn("JFK", plan.exclude_airports)
        self.assertIn("LGW", plan.exclude_airports)

    def test_work_back_by_monday(self) -> None:
        plan = plan_prompt(
            "YYZ-NRT outbound on 2026-11-06 and return on 2026-11-09 as two one-way, "
            "without --trip rt, must work Monday 09:00 local."
        )
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertEqual(plan.trip, "one-way")

    def test_flights_and_hotels_same_prompt(self) -> None:
        plan = plan_prompt(
            "Fly DUB-JFK on 2026-10-09 returning 2026-10-13 as two one-way, without --trip rt, "
            "and hotel in New York from 2026-10-09 to 2026-10-13, 2 adults, 1 room, "
            "free cancellation. Do not invent a fare or a hotel price."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.location.casefold(), "new york")
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.adults, 2)
        self.assertNotIn("€", plan.notes)

    def test_overlay_refuse_keeps_flight_intent(self) -> None:
        plan = plan_prompt(
            "CHC-SYD on 2026-11-03, no trains, no rental car, do not complete the booking."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertIn("trains", plan.refuse)
        self.assertIn("cars", plan.refuse)
        self.assertIn("booking", plan.refuse)

    def test_french_hotel_prompt_emits_english_query(self) -> None:
        plan = plan_prompt("Hôtel à Paris du 2026-12-04 au 2026-12-07. Ne pas inventer de tarif.")
        self.assertEqual(plan.intent, "hotels")
        self.assertEqual(plan.location, "Paris")
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.check_in, date(2026, 12, 4))
        self.assertEqual(plan.check_out, date(2026, 12, 7))

    def test_german_hotel_tokio_emits_tokyo(self) -> None:
        plan = plan_prompt("Hotel in Tokio vom 2026-12-04 bis 2026-12-07. Keinen Preis erfinden.")
        self.assertEqual(plan.intent, "hotels")
        self.assertEqual(plan.location, "Tokyo")
        self.assertEqual(plan.locale, "en")

    def test_japanese_hotel_emits_english_tokyo(self) -> None:
        plan = plan_prompt("2026-12-04から2026-12-07まで東京のhotel。料金を作らないで。")
        self.assertEqual(plan.intent, "hotels")
        self.assertEqual(plan.location, "Tokyo")
        self.assertEqual(plan.locale, "en")

    def test_french_flights_emit_english_locale(self) -> None:
        plan = plan_prompt("Vols de Paris à Tokyo le 2026-11-03. Pas de tarif inventé.")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "CDG")
        self.assertEqual(plan.destination, "NRT")
        self.assertEqual(plan.locale, "en")
        self.assertNotIn("€", plan.notes)

    def test_two_adults_one_child_keeps_english_iata(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01, 2 adults 1 child")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.children, 1)
        self.assertEqual(plan.locale, "en")
        parsed = parse_flight_plan(
            plan.route_specs,
            trip=plan.trip or "one-way",
            max_stops=plan.max_stops if plan.max_stops is not None else 1,
            adults=plan.adults or 1,
            children=plan.children or 0,
        )
        self.assertEqual(parsed[0].origin, "BOS")
        self.assertEqual(parsed[0].destination, "LHR")
        self.assertEqual(parsed[0].adults, 2)
        self.assertEqual(parsed[0].children, 1)

    def test_word_number_adults_and_children(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01, two adults and one child")
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.children, 1)
        self.assertEqual(plan.locale, "en")
        family = plan_prompt("Flights BOS-LHR on 2026-09-01 for a family")
        self.assertIsNone(family.children)

    def test_i18n_children_keep_english_iata(self) -> None:
        spanish = plan_prompt("Vuelos MAD-BCN el 2026-09-01, 2 adultos y 1 niño")
        self.assertEqual(spanish.origin, "MAD")
        self.assertEqual(spanish.destination, "BCN")
        self.assertEqual(spanish.adults, 2)
        self.assertEqual(spanish.children, 1)
        self.assertEqual(spanish.locale, "en")
        french = plan_prompt("Vols CDG-NRT le 2026-10-09, deux adultes et un enfant")
        self.assertEqual(french.origin, "CDG")
        self.assertEqual(french.destination, "NRT")
        self.assertEqual(french.adults, 2)
        self.assertEqual(french.children, 1)
        german = plan_prompt("Flüge FRA-SIN am 2026-10-12, 2 Erwachsene und 1 Kind")
        self.assertEqual(german.origin, "FRA")
        self.assertEqual(german.destination, "SIN")
        self.assertEqual(german.adults, 2)
        self.assertEqual(german.children, 1)

    def test_infant_in_seat_vs_on_lap(self) -> None:
        seated = plan_prompt("Flights BOS-LHR on 2026-09-01, 2 adults, 1 infant in seat")
        self.assertEqual(seated.infants_in_seat, 1)
        self.assertIsNone(seated.infants_on_lap)
        lap = plan_prompt("Flights BOS-LHR on 2026-09-01, 2 adults, 1 infant on lap")
        self.assertEqual(lap.infants_on_lap, 1)
        self.assertIsNone(lap.infants_in_seat)
        bare = plan_prompt("Flights BOS-LHR on 2026-09-01, 1 adult, 1 infant")
        self.assertEqual(bare.infants_on_lap, 1)
        self.assertIsNone(bare.infants_in_seat)

    def test_overnight_ist_required_and_forbidden_keeps_both(self) -> None:
        plan = plan_prompt(
            "BKK-LHR on 2026-11-10, must overnight in IST, never overnight in IST, "
            "max 1 stop, 1 checked bag. Quote the fare in EUR."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "BKK")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.baggage, "checked_1")
        self.assertIn("IST", plan.require_overnight)
        self.assertIn("IST", plan.no_overnight)
        self.assertIn("IST", plan.prefer_airports)
        self.assertNotIn("IST", plan.exclude_airports)
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.exclude_via, ())
        folded_notes = plan.notes.casefold()
        self.assertIn("required", folded_notes)
        self.assertIn("forbidden", folded_notes)
        self.assertNotIn("€", plan.notes)

    def test_never_overnight_ist_without_via_still_excludes(self) -> None:
        plan = plan_prompt("DEL-LHR on 2026-11-03, never overnight in IST")
        self.assertIn("IST", plan.exclude_airports)
        self.assertIn("IST", plan.no_overnight)
        self.assertNotIn("IST", plan.prefer_airports)
        self.assertEqual(plan.require_overnight, ())

    def test_via_ist_and_no_overnight_keeps_prefer(self) -> None:
        plan = plan_prompt(
            "TBS-SIN on 2026-11-03 via IST, at least 12h connection, "
            "never overnight in IST, max 1 stop."
        )
        self.assertEqual(plan.origin, "TBS")
        self.assertEqual(plan.destination, "SIN")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.min_layover, 12.0)
        self.assertIn("IST", plan.prefer_airports)
        self.assertIn("IST", plan.no_overnight)
        self.assertNotIn("IST", plan.exclude_airports)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ())
        plan = plan_prompt(
            "მინდა გავფრინდე თბილისიდან სინგაპურში TBS-SIN 2026-11-03, "
            "აუცილებლად გადავჯდე IST-ში, მინიმუმ 12 საათიანი გადაჯდომა, "
            "მაგრამ IST-ში ღამის გათევა არასდროს. მაქსიმუმ 1 გადაჯდომა. "
            "ტარიფი არ გამოიგონო."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "TBS")
        self.assertEqual(plan.destination, "SIN")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.min_layover, 12.0)
        self.assertIn("IST", plan.prefer_airports)
        self.assertIn("IST", plan.no_overnight)
        self.assertNotIn("IST", plan.exclude_airports)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.locale, "en")
        self.assertNotIn("€", plan.notes)

    def test_via_ist_not_via_dxb_sets_include_and_exclude(self) -> None:
        plan = plan_prompt(
            "JFK-SIN on 2026-11-03 via IST, not via DXB, max 1 stop. Do not invent a fare."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "JFK")
        self.assertEqual(plan.destination, "SIN")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertIn("IST", plan.prefer_airports)
        self.assertNotIn("DXB", plan.prefer_airports)
        self.assertNotIn("DXB", plan.exclude_airports)
        self.assertNotIn("IST", plan.exclude_airports)

    def test_via_flags_set_include_and_exclude(self) -> None:
        plan = plan_prompt("JFK-SIN on 2026-11-03 --via IST --exclude-via DXB")
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))

    def test_via_city_names_map_to_owned_iata(self) -> None:
        plan = plan_prompt(
            "JFK-SIN on 2026-11-03 via Istanbul, not via Dubai, max 1 stop. Do not invent a fare."
        )
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertNotIn("DXB", plan.exclude_airports)
        connecting = plan_prompt("JFK-SIN on 2026-11-03 connecting in IST, avoid DXB, max 1 stop.")
        self.assertEqual(connecting.via_airports, ("IST",))
        self.assertEqual(connecting.exclude_via, ("DXB",))
        self.assertNotIn("DXB", connecting.exclude_airports)

    def test_same_calendar_date_is_not_a_dates_grid(self) -> None:
        plan = plan_prompt(
            "Fly AKL-HNL departing 2026-11-03 after 10:00 Auckland local, and arrive "
            "HNL before 09:00 on the same calendar date 2026-11-03. Max 1 stop. "
            "Do not invent a fare."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "AKL")
        self.assertEqual(plan.destination, "HNL")
        self.assertEqual(plan.depart_after, "10:00")
        self.assertEqual(plan.arrive_before, "09:00")

    def test_overlapping_hotel_and_flight_sets_search_trip(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip ADD-NBO on 2026-10-09 returning 2026-10-13, --trip rt, "
            "hotel in Nairobi those nights, 2 adults, 1 room. Print the owned trip total "
            "when both searches succeed. Omit the sum if either side misses. "
            "Do not invent a fare or a stay."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.hotels)
        self.assertTrue(plan.search_trip)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.location, "Nairobi")
        self.assertIn("search_trip", plan.notes)
        self.assertIn("typical", plan.notes)
        self.assertNotIn("€", plan.notes)

    def test_french_aller_retour_hotel_is_packaged_rt(self) -> None:
        plan = plan_prompt(
            "Vol aller-retour CDG-NRT le 2026-10-09, retour le 2026-10-16. "
            "Deux adultes. Hôtel à Tokyo du 2026-10-09 au 2026-10-16, une chambre. "
            "N'invente pas de tarif."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.trip, "rt")
        self.assertTrue(plan.hotels)
        self.assertTrue(plan.search_trip)
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.location, "Tokyo")
        self.assertEqual(plan.locale, "en")

    def test_german_ruckflug_unterkunft_is_packaged_rt(self) -> None:
        plan = plan_prompt(
            "Hin- und Rückflug FRA-SIN am 2026-10-12, Rückflug am 2026-10-19. "
            "Zwei Erwachsene. Unterkunft in Singapore vom 2026-10-12 bis 2026-10-19, "
            "ein Zimmer. Keinen Tarif erfinden."
        )
        self.assertEqual(plan.trip, "rt")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.location, "Singapore")
        self.assertEqual(plan.work_back_by, None)

    def test_japanese_return_hotel_and_exclude_hnd(self) -> None:
        plan = plan_prompt(
            "成田からシンガポール、NRT-SIN、2026-10-20出発、2026-10-24戻り。往復。"
            "大人2人。シンガポールの宿泊、部屋1。羽田HNDは使わない。運賃を作らないで。"
        )
        self.assertEqual(plan.trip, "rt")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.location, "Singapore")
        self.assertIn("HND", plan.exclude_airports)
        self.assertEqual(plan.locale, "en")

    def test_inflected_hotel_cities_map_to_owned_english(self) -> None:
        basque = plan_prompt(
            "Bilbotik Parisera joan nahi dut, BIO-CDG 2026-10-09, itzuli 2026-10-12. "
            "Bi heldu. Ostatua Parisen 2026-10-09-tik 2026-10-12-ra, gela bat. "
            "Ez asmatu preziorik."
        )
        self.assertEqual(basque.trip, "rt")
        self.assertTrue(basque.hotels)
        self.assertEqual(basque.location, "Paris")
        self.assertTrue(basque.search_trip)
        icelandic = plan_prompt(
            "Ég vil fljúga KEF-CPH 2026-10-09, lenda 2026-10-10 klukkan 00:15 "
            "að staðartíma í Kaupmannahöfn, og innritun á gistihúsi þegar 2026-10-09. "
            "Tveir fullorðnir, eitt herbergi, koma til baka 2026-10-12. Ekki búa til verð."
        )
        self.assertEqual(icelandic.trip, "rt")
        self.assertTrue(icelandic.hotels)
        self.assertEqual(icelandic.location, "Copenhagen")
        self.assertEqual(icelandic.adults, 2)
        self.assertEqual(icelandic.rooms, 1)
        self.assertTrue(icelandic.search_trip)
        welsh = plan_prompt(
            "Hoffwn hedfan o Gaerdydd i Ddulyn, CWL-DUB ar 2026-10-16, dychwelyd 2026-10-19. "
            "Dau oedolyn. Gwesty yn Nulyn o 2026-10-16 tan 2026-10-19, un ystafell. "
            "Paid â dyfeisio pris."
        )
        self.assertTrue(welsh.hotels)
        self.assertEqual(welsh.location, "Dublin")
        self.assertEqual(welsh.adults, 2)
        tamil = plan_prompt(
            "MAA-SIN 2026-10-20 திரும்பு 2026-10-24. விமானத்தில் இரண்டு பெரியவர்கள். "
            "சிங்கப்பூர் தங்குமிடத்தில் நான்கு பெரியவர்கள், இரண்டு அறைகள். "
            "விலையை உருவாக்க வேண்டாம்."
        )
        self.assertTrue(tamil.hotels)
        self.assertEqual(tamil.location, "Singapore")
        self.assertTrue(tamil.search_trip)
        same = plan_prompt(
            "Packaged round-trip CHC-AKL on 2026-11-03 returning 2026-11-06, --trip rt, "
            "business outbound and economy on the return for the same adult. "
            "Do not invent a fare."
        )
        self.assertEqual(same.adults, 1)
        self.assertEqual(same.trip, "rt")
        self.assertIsNone(same.cabin)
        self.assertIn("business", same.notes.casefold())
        self.assertIn("economy", same.notes.casefold())
        self.assertNotIn("€", same.notes)

    def test_mixed_first_and_economy_omits_cabin(self) -> None:
        plan = plan_prompt(
            "HEL-NRT on 2026-10-20, first class and economy for the same adult, "
            "max 1 stop, carry-on only. Quote both cabin fares in EUR."
        )
        self.assertEqual(plan.origin, "HEL")
        self.assertEqual(plan.destination, "NRT")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.adults, 1)
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.cabin)
        self.assertIn("first", plan.notes.casefold())
        self.assertIn("economy", plan.notes.casefold())
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")

    def test_nonstop_via_keeps_both_constraints(self) -> None:
        plan = plan_prompt(
            "LAX-SIN on 2026-11-03 nonstop, max 0 stops, via DXB. Do not invent a fare."
        )
        self.assertEqual(plan.max_stops, 0)
        self.assertEqual(plan.via_airports, ("DXB",))
        self.assertIn("DXB", plan.notes)
        self.assertIn("nonstop", plan.notes.casefold())
        self.assertNotIn("€", plan.notes)

    def test_nonstop_and_min_layover_keeps_both_and_notes_unsatisfiable(self) -> None:
        plan = plan_prompt(
            "SEA-KIX on 2026-11-03 nonstop, max 0 stops, at least 2h connection, "
            "at most 8h layover, 1 checked bag, business class. Quote the fare in EUR."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "SEA")
        self.assertEqual(plan.destination, "KIX")
        self.assertEqual(plan.max_stops, 0)
        self.assertEqual(plan.min_layover, 2.0)
        self.assertEqual(plan.max_layover, 8.0)
        self.assertEqual(plan.cabin, "business")
        self.assertNotIn("contradictory_routing", plan.refuse)
        folded = plan.notes.casefold()
        self.assertIn("unsatisfiable", folded)
        self.assertIn("nonstop", folded)
        self.assertIn("layover", folded)
        self.assertIn("keep both", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        flagged = plan_prompt(
            "SEA-KIX on 2026-11-03 nonstop --min-layover 2. Do not invent a fare."
        )
        self.assertEqual(flagged.intent, "flights")
        self.assertEqual(flagged.max_stops, 0)
        self.assertEqual(flagged.min_layover, 2.0)
        self.assertIn("keep both", flagged.notes.casefold())
        self.assertNotIn("€", flagged.notes)

    def test_lone_nonstop_has_no_layover_contradiction_note(self) -> None:
        plan = plan_prompt("SEA-KIX on 2026-11-03 nonstop, max 0 stops. Do not invent a fare.")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.max_stops, 0)
        self.assertIsNone(plan.min_layover)
        folded = plan.notes.casefold()
        self.assertNotIn("unsatisfiable", folded)
        self.assertNotIn("keep both", folded)
        self.assertNotIn("€", plan.notes)

    def test_unnamed_nonstop_with_min_layover_has_no_contradiction_note(self) -> None:
        plan = plan_prompt("YVR-NRT on 2026-11-03 --min-layover 1. Do not invent a fare.")
        self.assertEqual(plan.intent, "flights")
        self.assertIsNone(plan.max_stops)
        self.assertEqual(plan.min_layover, 1.0)
        folded = plan.notes.casefold()
        self.assertNotIn("unsatisfiable", folded)
        self.assertNotIn("nonstop has no layover", folded)
        self.assertNotIn("€", plan.notes)

    def test_via_and_min_layover_without_nonstop_skips_nonstop_layover_note(self) -> None:
        plan = plan_prompt(
            "TBS-SIN on 2026-11-03 via IST, at least 12h connection, max 1 stop. "
            "Do not invent a fare."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.min_layover, 12.0)
        folded = plan.notes.casefold()
        self.assertNotIn("nonstop has no layover", folded)
        self.assertNotIn("max 0 stops", folded)
        self.assertNotIn("€", plan.notes)

    def test_flight_hotel_occupancy_mismatch_keeps_both(self) -> None:
        plan = plan_prompt(
            "Fly CPT-JNB on 2026-09-18 returning 2026-09-22, 2 adults. "
            "Hotel in Johannesburg from 2026-09-18 to 2026-09-22, 4 adults, 2 rooms. "
            "Do not invent a fare or a hotel price."
        )
        self.assertTrue(plan.hotels)
        self.assertIsNone(plan.adults)
        self.assertEqual(plan.rooms, 2)
        self.assertIn("2 adults", plan.notes)
        self.assertIn("4 adults", plan.notes)
        self.assertIn("2 rooms", plan.notes)
        tamil = plan_prompt(
            "MAA-SIN 2026-10-20 திரும்பு 2026-10-24. விமானத்தில் இரண்டு பெரியவர்கள். "
            "சிங்கப்பூர் தங்குமிடத்தில் நான்கு பெரியவர்கள், இரண்டு அறைகள். "
            "விலையை உருவாக்க வேண்டாம்."
        )
        self.assertTrue(tamil.hotels)
        self.assertEqual(tamil.location, "Singapore")
        self.assertIsNone(tamil.adults)
        self.assertEqual(tamil.rooms, 2)
        self.assertIn("2 adults", tamil.notes)
        self.assertIn("4 adults", tamil.notes)

    def test_tamil_rt_hotel_maps_occupancy_flags(self) -> None:
        plan = plan_prompt(
            "சென்னையில் இருந்து சிங்கப்பூருக்கு பறக்க வேண்டும் MAA-SIN 2026-10-20, "
            "திரும்பு 2026-10-24. இரண்டு பெரியவர்கள். சிங்கப்பூரில் தங்குமிடம் "
            "2026-10-20 முதல் 2026-10-24, ஒரு அறை. விலையை உருவாக்க வேண்டாம்."
        )
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.location, "Singapore")
        self.assertTrue(plan.search_trip)

    def test_free_and_nonrefundable_keeps_both(self) -> None:
        plan = plan_prompt(
            "DXB-DOH on 2026-10-09 returning 2026-10-13, hotel in Doha, 2 adults, 1 room, "
            "free cancellation only, and also only prepaid non-refundable rates. "
            "Do not invent a fare or a hotel price."
        )
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.location, "Doha")
        folded = plan.notes.casefold()
        self.assertIn("free cancellation", folded)
        self.assertIn("non-refundable", folded)
        self.assertIn("allow-non-refundable", folded)

    def test_packaged_open_jaw_prefers_both_airports(self) -> None:
        plan = plan_prompt(
            "YVR-LHR on 2026-10-09 and LGW-YVR on 2026-10-13 as a packaged "
            "round-trip --trip rt. Do not invent a fare."
        )
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(list(plan.route_specs), ["YVR-LHR:2026-10-09", "LGW-YVR:2026-10-13"])
        self.assertEqual(list(plan.prefer_airports), ["LHR", "LGW"])
        self.assertIn("open-jaw", plan.notes.casefold())
        self.assertNotIn("€", plan.notes)

    def test_english_city_labels_compile_from_aliases(self) -> None:
        self.assertEqual(_IATA_TO_ENGLISH["CPH"], "Copenhagen")
        self.assertEqual(_IATA_TO_ENGLISH["CDG"], "Paris")
        self.assertEqual(_IATA_TO_ENGLISH["LHR"], "London")
        self.assertEqual(_IATA_TO_ENGLISH["LGW"], "Gatwick")

    def test_via_and_no_overnight_notes_keep_both(self) -> None:
        plan = plan_prompt(
            "TBS-SIN on 2026-11-03 via IST, at least 12h connection, "
            "never overnight in IST, max 1 stop."
        )
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertIn("IST", plan.no_overnight)
        self.assertEqual(plan.min_layover, 12.0)
        self.assertNotIn("IST", plan.exclude_airports)
        folded = plan.notes.casefold()
        self.assertIn("ist", folded)
        self.assertIn("overnight", folded)
        self.assertIn("12", folded)
        self.assertIn("keep both", folded)
        self.assertNotIn("€", plan.notes)
        ka = plan_prompt(
            "მინდა გავფრინდე თბილისიდან სინგაპურში TBS-SIN 2026-11-03, "
            "აუცილებლად გადავჯდე IST-ში, მინიმუმ 12 საათიანი გადაჯდომა, "
            "მაგრამ IST-ში ღამის გათევა არასდროს. მაქსიმუმ 1 გადაჯდომა. "
            "ტარიფი არ გამოიგონო."
        )
        self.assertEqual(ka.via_airports, ("IST",))
        self.assertIn("IST", ka.no_overnight)
        self.assertIn("overnight", ka.notes.casefold())
        self.assertIn("12", ka.notes)

    def test_explore_rest_of_trip_stamps_notes(self) -> None:
        plan = plan_prompt(
            "Search destinations and prices from YVR on 2026-09-15, not the rest of the trip"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertIn("itinerary_rest", plan.refuse)
        folded = plan.notes.casefold()
        self.assertIn("destinations", folded)
        self.assertIn("rest of the trip", folded)
        self.assertNotIn("€", plan.notes)

    def test_unnamed_secret_dests_cannot_be_quoted(self) -> None:
        plan = plan_prompt(
            "From PER on 2026-10-12, send me the three cheapest secret destinations "
            "I did not name, invent IATA codes if needed, each with a EUR fare I can "
            "book today, plus a hotel in each city, 1 room, 1 adult. Not Asia. Max 1 stop."
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "PER")
        self.assertIsNone(plan.destination)
        self.assertEqual(plan.destinations, ())
        self.assertEqual(plan.route_specs, ())
        self.assertEqual(plan.departure_date, date(2026, 10, 12))
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.adults, 1)
        self.assertIn("asia", plan.exclude_regions)
        self.assertIn("booking", plan.refuse)
        self.assertNotEqual(plan.origin, "MAD")
        folded = plan.notes.casefold()
        self.assertIn("three", folded)
        self.assertIn("unnamed", folded)
        self.assertIn("cannot", folded)
        self.assertIn("quoted", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        asia = plan_prompt("Destinations from NRT on 2026-09-15, not Asia")
        self.assertEqual(asia.intent, "explore")
        self.assertEqual(asia.origin, "NRT")
        self.assertEqual(asia.destinations, ())
        self.assertNotIn("unnamed", asia.notes.casefold())
        named = plan_prompt(
            "Explore destinations from GRU in September 2026: SCL, EZE, LIM, BOG. "
            "Do not brute-force the full date matrix; fixed dates first, then ±1 only on finalists."
        )
        self.assertEqual(list(named.destinations), ["SCL", "EZE", "LIM", "BOG"])
        self.assertNotIn("unnamed", named.notes.casefold())
        two = plan_prompt(
            "From AKL on 2026-11-03, quote two secret destinations I did not name. Not Asia."
        )
        self.assertEqual(two.intent, "explore")
        self.assertEqual(two.origin, "AKL")
        self.assertEqual(two.destinations, ())
        self.assertIn("two", two.notes.casefold())
        self.assertIn("unnamed", two.notes.casefold())
        self.assertNotIn("three", two.notes.casefold())

    def test_same_city_vs_is_constraint_not_second_dest(self) -> None:
        plan = plan_prompt(
            "IAD-LHR on 2026-10-09 returning 2026-10-12, LHR vs LGW, business class, "
            "carry-on only, arrive before 09:00, hotel in London, 2 adults, 1 room. "
            "Plan both. Do not invent a fare or a hotel price."
        )
        self.assertEqual(plan.destination, "LHR")
        self.assertFalse(plan.nearby)
        self.assertEqual(plan.trip, "rt")
        self.assertNotIn("LGW", "".join(plan.route_specs))
        folded = plan.notes.casefold()
        self.assertIn("lhr", folded)
        self.assertIn("lgw", folded)
        self.assertIn("constraint", folded)
        self.assertIn("second destination", folded)
        exclusive = plan_prompt("GRU-LHR on 2026-09-08, use LHR not LGW, max 1 stop.")
        self.assertEqual(exclusive.destination, "LHR")
        self.assertIn("LGW", exclusive.exclude_airports)
        self.assertFalse(exclusive.nearby)
        self.assertIn("lgw", exclusive.notes.casefold())
        self.assertIn("constraint", exclusive.notes.casefold())

    def test_around_the_world_notes_name_the_circuit(self) -> None:
        plan = plan_prompt(
            "Cheapest around the world from Vancouver starting 2026-11-01, max 2 stops each leg"
        )
        self.assertTrue(plan.around_the_world)
        self.assertEqual(plan.destination, "YVR")
        self.assertEqual(plan.trip, "multi")
        self.assertEqual(plan.route_specs, ())
        folded = plan.notes.casefold()
        self.assertIn("circuit", folded)
        self.assertIn("origin", folded)
        self.assertIn("shortlist", folded)
        self.assertIn("max_stops 2", folded)
        self.assertEqual(plan.via_regions, ())
        self.assertNotIn("via_regions", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotEqual(plan.origin, "MAD")


class PromptPlanWorkBackIdlTests(unittest.TestCase):
    """Named work_back_by prompts. Keep the field; do not invent an IDL hop."""

    def test_english_hnl_akl_sunday_night_cannot_make_monday_office(self) -> None:
        plan = plan_prompt(
            "HNL-AKL on 2026-11-08 after 21:00 Honolulu local, must work Monday "
            "09:00 in Auckland. Max 1 stop. Do not invent a fare."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "HNL")
        self.assertEqual(plan.destination, "AKL")
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertEqual(plan.depart_after, "21:00")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.route_specs, ("HNL-AKL:2026-11-08",))
        folded = plan.notes.casefold()
        self.assertIn("cannot make monday 09:00", folded)
        self.assertIn("timezone/idl", folded)
        self.assertIn("work_back_by", folded)
        self.assertNotIn("syd", folded)
        self.assertNotIn("€", plan.notes)

    def test_german_fra_akl_monday_return_cannot_make_frankfurt_office(self) -> None:
        plan = plan_prompt(
            "Hin- und Rückflug FRA-AKL am 2026-11-06, Rückflug am 2026-11-09. "
            "Ich muss Montag 09:00 in Frankfurt im Büro sein. Ein Erwachsener. "
            "Keinen Tarif erfinden."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "FRA")
        self.assertEqual(plan.destination, "AKL")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.return_date, date(2026, 11, 9))
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertEqual(plan.adults, 1)
        self.assertEqual(plan.route_specs, ("FRA-AKL:2026-11-06:2026-11-09",))
        folded = plan.notes.casefold()
        self.assertIn("cannot make monday 09:00", folded)
        self.assertIn("akl-fra", folded)
        self.assertIn("timezone/idl", folded)
        self.assertNotIn("syd", folded)
        self.assertNotIn("€", plan.notes)

    def test_swahili_nbo_lhr_keeps_jumatatu_office_clock(self) -> None:
        plan = plan_prompt(
            "Nataka ndege NBO-LHR tarehe 2026-11-06, kurudi 2026-11-09. "
            "Lazima nifanye kazi Jumatatu saa 09:00 asubuhi mjini Nairobi. "
            "Hoteli London kuanzia 2026-11-06 hadi 2026-11-09, watu wazima 2, "
            "chumba 1. Usibuni bei ya tiketi wala hoteli."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "NBO")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.return_date, date(2026, 11, 9))
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.location, "London")
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.locale, "en")
        self.assertEqual(plan.route_specs, ("NBO-LHR:2026-11-06:2026-11-09",))
        folded = plan.notes.casefold()
        self.assertIn("cannot make monday 09:00", folded)
        self.assertIn("lhr-nbo", folded)
        self.assertNotIn("€", plan.notes)

    def test_zulu_jnb_syd_does_not_invent_monday_office(self) -> None:
        plan = plan_prompt(
            "Ngifuna ukundiza JNB-SYD ngomhla ka-2026-11-06, ngibuye ngomhla "
            "ka-2026-11-09. Ngimele ngisebenze uMsombuluko ngo-09:00 eGoli. "
            "Abantu abadala ababili. Indawo yokulala eSydney kusukela "
            "2026-11-06 kuya 2026-11-09, ikamelo elilodwa. Ungaqambi intengo."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "JNB")
        self.assertEqual(plan.destination, "SYD")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.return_date, date(2026, 11, 9))
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.location, "Sydney")
        self.assertEqual(plan.route_specs, ("JNB-SYD:2026-11-06:2026-11-09",))
        folded = plan.notes.casefold()
        self.assertIn("cannot make monday 09:00", folded)
        self.assertIn("syd-jnb", folded)
        self.assertIn("timezone/idl", folded)
        self.assertNotIn("akl", folded)
        self.assertNotIn("€", plan.notes)

    def test_yyz_cdg_weekday_nofly_maps_work_back_by(self) -> None:
        plan = plan_prompt(
            "YYZ-CDG, weekday no-fly: depart after Friday 18:00 on 2026-10-09, "
            "back Monday before 09:00 on 2026-10-12, hotel in Paris, 1 room, "
            "1 adult, max 1 stop, carry-on only, no red-eye. Plan both. "
            "Do not invent a fare or a hotel price."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "YYZ")
        self.assertEqual(plan.destination, "CDG")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.weekday, "friday")
        self.assertEqual(plan.depart_after, "18:00")
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.location, "Paris")
        self.assertEqual(plan.max_stops, 1)
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertEqual(plan.route_specs, ("YYZ-CDG:2026-10-09:2026-10-12",))
        self.assertNotIn("cannot make monday 09:00", plan.notes.casefold())
        self.assertNotIn("€", plan.notes)


class PromptPlanMatchTests(unittest.TestCase):
    def test_plan_to_dict_is_json_friendly(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01")
        data = plan.to_dict()
        self.assertEqual(data["origin"], "BOS")
        self.assertEqual(data["departure_date"], "2026-09-01")
        self.assertEqual(data["locale"], "en")
        self.assertIsInstance(data["refuse"], list)


class PromptPlanExcludeOriginRegionTests(unittest.TestCase):
    """Named origin inside an excluded region is an honesty constraint, not a dest list."""

    _INVENTED_NEIGHBORS = ("LHR", "CDG", "AMS", "JFK", "LAX", "SYD", "AKL", "MAD")

    def test_nrt_exclude_asia_stamps_origin_inside_and_does_not_invent_dests(self) -> None:
        plan = plan_prompt("Destinations from NRT on 2026-09-15, not Asia")
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "NRT")
        self.assertEqual(plan.departure_date, date(2026, 9, 15))
        self.assertIn("asia", plan.exclude_regions)
        self.assertEqual(plan.destinations, ())
        self.assertEqual(plan.route_specs, ())
        self.assertIsNone(plan.destination)
        folded = plan.notes.casefold()
        self.assertIn("nrt", folded)
        self.assertIn("inside", folded)
        self.assertIn("excluded", folded)
        self.assertIn("asia", folded)
        self.assertIn("empty", folded)
        self.assertIn("do not invent", folded)
        self.assertIn("dest", folded)
        self.assertIn("iata", folded)
        self.assertIn("fare", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        owned = " ".join(plan.destinations) + " " + " ".join(plan.route_specs)
        for code in self._INVENTED_NEIGHBORS:
            self.assertNotIn(code, owned)
            self.assertNotIn(code, plan.notes)

    def test_origin_outside_excluded_region_does_not_get_inside_note(self) -> None:
        plan = plan_prompt("Destinations from AKL on 2026-11-01, not Asia")
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "AKL")
        self.assertIn("asia", plan.exclude_regions)
        self.assertEqual(plan.destinations, ())
        folded = plan.notes.casefold()
        self.assertNotIn("sits inside", folded)
        self.assertNotIn("inside excluded", folded)
        self.assertIn("drop", folded)
        self.assertIn("asia", folded)
        self.assertNotIn("€", plan.notes)

    def test_unnamed_exclude_stays_unset(self) -> None:
        plan = plan_prompt("Destinations from NRT on 2026-09-15")
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "NRT")
        self.assertEqual(plan.exclude_regions, ())
        self.assertEqual(plan.destinations, ())
        folded = plan.notes.casefold()
        self.assertNotIn("inside excluded", folded)
        self.assertNotIn("sits inside", folded)

    def test_named_asian_dests_from_bom_stay_and_do_not_invent_more(self) -> None:
        plan = plan_prompt(
            "Explore destinations from BOM in September 2026: BKK, NRT, HKG. "
            "Not Asia. Do not invent fares."
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "BOM")
        self.assertEqual(list(plan.destinations), ["BKK", "NRT", "HKG"])
        self.assertIn("asia", plan.exclude_regions)
        folded = plan.notes.casefold()
        self.assertIn("inside", folded)
        self.assertIn("excluded", folded)
        owned = " ".join(plan.destinations)
        for code in self._INVENTED_NEIGHBORS:
            self.assertNotIn(code, owned)
            self.assertNotIn(code, plan.notes)


class PromptPlanWeekdayNoflyTests(unittest.TestCase):
    """Weekday no-fly / weekend-only is an owned clock constraint. Do not invent hops."""

    _INVENTED_HOPS = ("LHR", "AMS", "FRA", "JFK", "ORD", "DUB", "IST", "DOH")

    def test_weekday_nofly_rt_keeps_clocks_stamps_note_and_does_not_invent_hops(self) -> None:
        plan = plan_prompt(
            "YYZ-CDG, weekday no-fly: depart after Friday 18:00 on 2026-10-09, "
            "back Monday before 09:00 on 2026-10-12, hotel in Paris, 1 room, "
            "1 adult, max 1 stop, carry-on only, no red-eye. Plan both. "
            "Do not invent a fare or a hotel price."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "YYZ")
        self.assertEqual(plan.destination, "CDG")
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.weekday, "friday")
        self.assertEqual(plan.depart_after, "18:00")
        self.assertEqual(plan.work_back_by, "monday 09:00")
        self.assertEqual(plan.route_specs, ("YYZ-CDG:2026-10-09:2026-10-12",))
        self.assertEqual(plan.via_airports, ())
        self.assertTrue(set(plan.prefer_airports) <= {"YYZ", "CDG"})
        folded = plan.notes.casefold()
        self.assertIn("weekday no-fly", folded)
        self.assertIn("owned clock", folded)
        self.assertIn("depart_after", folded)
        self.assertIn("work_back_by", folded)
        self.assertIn("midweek", folded)
        self.assertIn("do not invent", folded)
        self.assertIn("fare", folded)
        self.assertNotIn("cannot make monday 09:00", folded)
        self.assertNotIn("€", plan.notes)
        self.assertNotRegex(plan.notes, r"\bEUR\b")
        owned = " ".join(plan.route_specs) + " " + " ".join(plan.via_airports)
        for code in self._INVENTED_HOPS:
            self.assertNotIn(code, owned)
            self.assertNotIn(code, plan.notes)

    def test_prompt_without_weekday_constraint_does_not_get_nofly_note(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip ADD-NBO on 2026-10-09 returning 2026-10-13, --trip rt, "
            "hotel in Nairobi those nights, 2 adults, 1 room. Print the owned trip total "
            "when both searches succeed. Omit the sum if either side misses. "
            "Do not invent a fare or a stay."
        )
        self.assertEqual(plan.trip, "rt")
        self.assertIsNone(plan.weekday)
        self.assertIsNone(plan.depart_after)
        self.assertIsNone(plan.work_back_by)
        folded = plan.notes.casefold()
        self.assertNotIn("weekday no-fly", folded)
        self.assertNotIn("owned clock", folded)
        self.assertNotIn("midweek hop", folded)
        self.assertNotIn("€", plan.notes)

    def test_unnamed_weekday_clocks_stay_unset(self) -> None:
        plan = plan_prompt("Flights BOS-LHR on 2026-09-01")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertIsNone(plan.weekday)
        self.assertIsNone(plan.depart_after)
        self.assertIsNone(plan.work_back_by)
        folded = plan.notes.casefold()
        self.assertNotIn("weekday no-fly", folded)
        self.assertNotIn("owned clock", folded)
        self.assertNotIn("depart_after", folded)
        self.assertNotIn("work_back_by", folded)


class PromptPlanFamilyShopFilterTests(unittest.TestCase):
    """Named bags / via / cap land on dates, flex, explore, and search_trip."""

    def test_dates_named_bags_via_cap_land_on_dates_intent(self) -> None:
        plan = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, "
            "carry-on only, via IST, not via DXB, under 200€"
        )
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.origin, "JFK")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.bags)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertEqual(plan.price_cap, 200)
        self.assertEqual(plan.route_specs, ())

    def test_dates_checked_bags_and_flag_cap_land(self) -> None:
        plan = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, "
            "1 checked bag, --price-cap 400 EUR"
        )
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.bags, 1)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.price_cap, 400)
        self.assertIsNone(plan.baggage_buffer)

    def test_dates_named_baggage_buffer_copies_without_inventing_bags(self) -> None:
        plan = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 --baggage-buffer 40"
        )
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.baggage_buffer, 40)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertIsNone(plan.baggage)
        self.assertEqual(plan.route_specs, ())

    def test_dates_named_baggage_buffer_prose_copies(self) -> None:
        plan = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 rank with a 40€ bag buffer"
        )
        self.assertEqual(plan.intent, "dates")
        self.assertEqual(plan.baggage_buffer, 40)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_dates_bags_vibe_does_not_invent_buffer(self) -> None:
        plan = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 cheap with bags")
        self.assertEqual(plan.intent, "dates")
        self.assertIsNone(plan.baggage_buffer)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_dates_unnamed_shop_filters_stay_unset(self) -> None:
        plan = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14")
        self.assertEqual(plan.intent, "dates")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.exclude_via, ())
        self.assertEqual(plan.exclude_airports, ())
        self.assertEqual(plan.include_airports, ())
        self.assertIsNone(plan.price_cap)
        self.assertIsNone(plan.depart_window)
        self.assertIsNone(plan.arrive_before)
        self.assertIsNone(plan.depart_after)
        self.assertIsNone(plan.max_layover)
        self.assertIsNone(plan.min_layover)
        self.assertIsNone(plan.max_duration)
        self.assertEqual(plan.alliance, ())
        self.assertEqual(plan.exclude_alliance, ())
        self.assertIsNone(plan.children)
        self.assertIsNone(plan.infants_in_seat)
        self.assertIsNone(plan.infants_on_lap)
        self.assertIsNone(plan.currency)
        self.assertIsNone(plan.country)
        self.assertIsNone(plan.baggage_buffer)
        self.assertEqual(plan.no_overnight, ())
        self.assertEqual(plan.require_overnight, ())
        self.assertIsNone(plan.sort)

    def test_dates_named_sort_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 --sort duration"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(flagged.sort, "duration")
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, sort by duration"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.sort, "duration")
        fastest = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, fastest")
        self.assertEqual(fastest.intent, "dates")
        self.assertEqual(fastest.sort, "duration")
        earliest = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, earliest")
        self.assertEqual(earliest.intent, "dates")
        self.assertEqual(earliest.sort, "departure")
        vibe = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, cheap week in Europe"
        )
        self.assertEqual(vibe.intent, "dates")
        self.assertIsNone(vibe.sort)

    def test_dates_named_exclude_airports_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt("Price calendar BOS-NRT from 2026-09-01 to 2026-09-14, not HND")
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.destination, "NRT")
        self.assertIn("HND", named.exclude_airports)
        flagged = plan_prompt(
            "Price calendar BOS-NRT from 2026-09-01 to 2026-09-14 --exclude-airports HND"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertIn("HND", flagged.exclude_airports)
        vibe = plan_prompt("Price calendar BOS-NRT from 2026-09-01 to 2026-09-14, avoid Tokyo")
        self.assertEqual(vibe.intent, "dates")
        self.assertEqual(vibe.exclude_airports, ())

    def test_dates_named_include_airports_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt(
            "Price calendar BOS-NRT from 2026-09-01 to 2026-09-14 --include-airports NRT,HND"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(list(flagged.include_airports), ["NRT", "HND"])
        vibe = plan_prompt("Price calendar BOS-NRT from 2026-09-01 to 2026-09-14, Europe")
        self.assertEqual(vibe.intent, "dates")
        self.assertEqual(vibe.include_airports, ())
        self.assertNotIn("NRT", vibe.include_airports)

    def test_flights_named_include_airports_flag_does_not_invent_dest(self) -> None:
        plan = plan_prompt("Flights BOS-NRT on 2026-11-12 --include-airports NRT,HND")
        self.assertEqual(plan.intent, "flights")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "NRT")
        self.assertEqual(list(plan.include_airports), ["NRT", "HND"])
        vibe = plan_prompt("Flights BOS-NRT on 2026-11-12, Europe")
        self.assertEqual(vibe.intent, "flights")
        self.assertEqual(vibe.include_airports, ())

    def test_dates_named_overnight_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, never overnight in IST"
        )
        self.assertEqual(named.intent, "dates")
        self.assertIn("IST", named.no_overnight)
        self.assertEqual(named.require_overnight, ())
        required = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, must overnight in IST"
        )
        self.assertEqual(required.intent, "dates")
        self.assertIn("IST", required.require_overnight)
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 "
            "--no-overnight IST --require-overnight IST"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertIn("IST", flagged.no_overnight)
        self.assertIn("IST", flagged.require_overnight)
        vibe = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, red-eye")
        self.assertEqual(vibe.intent, "dates")
        self.assertEqual(vibe.no_overnight, ())
        self.assertEqual(vibe.require_overnight, ())

    def test_flex_named_overnight_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, no overnight")
        self.assertEqual(named.intent, "flex")
        self.assertIn("any", named.no_overnight)
        required = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, must overnight in IST")
        self.assertEqual(required.intent, "flex")
        self.assertIn("IST", required.require_overnight)
        both = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days, must overnight in IST, never overnight in IST"
        )
        self.assertEqual(both.intent, "flex")
        self.assertIn("IST", both.no_overnight)
        self.assertIn("IST", both.require_overnight)
        vibe = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, red-eye")
        self.assertEqual(vibe.intent, "flex")
        self.assertEqual(vibe.no_overnight, ())
        self.assertEqual(vibe.require_overnight, ())

    def test_explore_named_overnight_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, no overnight"
        )
        self.assertEqual(named.intent, "explore")
        self.assertIn("any", named.no_overnight)
        required = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, must overnight in IST"
        )
        self.assertEqual(required.intent, "explore")
        self.assertIn("IST", required.require_overnight)
        both = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, "
            "must overnight in IST, never overnight in IST"
        )
        self.assertEqual(both.intent, "explore")
        self.assertIn("IST", both.no_overnight)
        self.assertIn("IST", both.require_overnight)
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, red-eye"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertEqual(vibe.no_overnight, ())
        self.assertEqual(vibe.require_overnight, ())

    def test_dates_named_occupancy_lands_family_does_not_invent(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, 2 adults 1 child"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.adults, 2)
        self.assertEqual(named.children, 1)
        self.assertIsNone(named.infants_in_seat)
        self.assertIsNone(named.infants_on_lap)
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 "
            "--adults 2 --children 1 --infants-in-seat 1 --infants-on-lap 1"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(flagged.adults, 2)
        self.assertEqual(flagged.children, 1)
        self.assertEqual(flagged.infants_in_seat, 1)
        self.assertEqual(flagged.infants_on_lap, 1)
        family = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 for a family")
        self.assertEqual(family.intent, "dates")
        self.assertIsNone(family.children)
        self.assertIsNone(family.infants_in_seat)
        self.assertIsNone(family.infants_on_lap)

    def test_dates_named_currency_country_land_usd_word_does_not_invent(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 --currency USD --country US"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.currency, "USD")
        self.assertEqual(named.country, "US")
        vibe = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 in USD")
        self.assertEqual(vibe.intent, "dates")
        self.assertIsNone(vibe.currency)
        self.assertIsNone(vibe.country)
        invalid = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 --currency euro"
        )
        self.assertEqual(invalid.intent, "dates")
        self.assertIsNone(invalid.currency)

    def test_dates_named_alliance_lands_does_not_invent_members(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, Star Alliance only"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(list(named.alliance), ["star"])
        self.assertEqual(list(named.include_airlines), [])
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 "
            "--alliance oneworld --exclude-alliance star"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(list(flagged.alliance), ["oneworld"])
        self.assertEqual(list(flagged.exclude_alliance), ["star"])

    def test_dates_named_depart_window_lands_morning_vibe_does_not(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, leave between 06:00 and 20:00"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.depart_window, "06:00-20:00")
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 --depart-window 7-12"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(flagged.depart_window, "7-12")
        vibe = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, leave in the morning"
        )
        self.assertEqual(vibe.intent, "dates")
        self.assertIsNone(vibe.depart_window)

    def test_dates_named_arrive_before_depart_after_land_morning_vibe_does_not(self) -> None:
        named = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, arrive before 10:00"
        )
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.arrive_before, "10:00")
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 "
            "--arrive-before 10:00 --depart-after 18:00"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(flagged.arrive_before, "10:00")
        self.assertEqual(flagged.depart_after, "18:00")
        vibe = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, arrive in the morning"
        )
        self.assertEqual(vibe.intent, "dates")
        self.assertIsNone(vibe.arrive_before)
        self.assertIsNone(vibe.depart_after)

    def test_dates_named_layover_and_duration_land_short_vibe_does_not(self) -> None:
        named = plan_prompt("Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, max 3h layover")
        self.assertEqual(named.intent, "dates")
        self.assertEqual(named.max_layover, 3.0)
        flagged = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14 "
            "--max-layover 3 --min-layover 1 --max-duration 8"
        )
        self.assertEqual(flagged.intent, "dates")
        self.assertEqual(flagged.max_layover, 3.0)
        self.assertEqual(flagged.min_layover, 1.0)
        self.assertEqual(flagged.max_duration, 8.0)
        duration = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, maximum duration 8"
        )
        self.assertEqual(duration.intent, "dates")
        self.assertEqual(duration.max_duration, 8.0)
        vibe = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, short connections"
        )
        self.assertEqual(vibe.intent, "dates")
        self.assertIsNone(vibe.max_layover)
        self.assertIsNone(vibe.min_layover)
        self.assertIsNone(vibe.max_duration)

    def test_dates_contradiction_does_not_pick_one(self) -> None:
        plan = plan_prompt(
            "Price calendar JFK-LHR from 2026-09-01 to 2026-09-14, carry-on only and 2 checked bags"
        )
        self.assertEqual(plan.intent, "dates")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_flex_named_bags_via_cap_land_on_flex_intent(self) -> None:
        plan = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights, "
            "carry-on only, via IST, not via DXB, under 200€"
        )
        self.assertEqual(plan.intent, "flex")
        self.assertEqual(plan.origin, "BOS")
        self.assertEqual(plan.destination, "LHR")
        self.assertEqual(plan.flex_days, 3)
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.bags)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertEqual(plan.price_cap, 200)
        self.assertEqual(plan.route_specs, ())

    def test_flex_checked_bags_land(self) -> None:
        plan = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 1 checked bag")
        self.assertEqual(plan.intent, "flex")
        self.assertEqual(plan.bags, 1)
        self.assertIsNone(plan.carry_on)

    def test_flex_unnamed_shop_filters_stay_unset(self) -> None:
        plan = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights")
        self.assertEqual(plan.intent, "flex")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.exclude_via, ())
        self.assertEqual(plan.exclude_airports, ())
        self.assertEqual(plan.include_airports, ())
        self.assertIsNone(plan.price_cap)
        self.assertIsNone(plan.depart_window)
        self.assertIsNone(plan.arrive_before)
        self.assertIsNone(plan.depart_after)
        self.assertIsNone(plan.max_layover)
        self.assertIsNone(plan.min_layover)
        self.assertIsNone(plan.max_duration)
        self.assertEqual(plan.alliance, ())
        self.assertEqual(plan.exclude_alliance, ())
        self.assertIsNone(plan.children)
        self.assertIsNone(plan.infants_in_seat)
        self.assertIsNone(plan.infants_on_lap)
        self.assertIsNone(plan.currency)
        self.assertIsNone(plan.country)
        self.assertEqual(plan.no_overnight, ())
        self.assertEqual(plan.require_overnight, ())

    def test_flex_named_exclude_airports_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt("BOS-NRT around 12 Sep 2026, flex 3 days, not HND")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.destination, "NRT")
        self.assertIn("HND", named.exclude_airports)
        flagged = plan_prompt("BOS-NRT around 12 Sep 2026, flex 3 days --exclude-airports HND")
        self.assertEqual(flagged.intent, "flex")
        self.assertIn("HND", flagged.exclude_airports)
        vibe = plan_prompt("BOS-NRT around 12 Sep 2026, flex 3 days, avoid Tokyo")
        self.assertEqual(vibe.intent, "flex")
        self.assertEqual(vibe.exclude_airports, ())

    def test_flex_named_include_airports_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt("BOS-NRT around 12 Sep 2026, flex 3 days --include-airports NRT,HND")
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(list(flagged.include_airports), ["NRT", "HND"])
        vibe = plan_prompt("BOS-NRT around 12 Sep 2026, flex 3 days, Europe")
        self.assertEqual(vibe.intent, "flex")
        self.assertEqual(vibe.include_airports, ())

    def test_flex_named_occupancy_lands_family_does_not_invent(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 2 adults 1 child")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.adults, 2)
        self.assertEqual(named.children, 1)
        flagged = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days "
            "--adults 2 --children 1 --infants-in-seat 1 --infants-on-lap 1"
        )
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(flagged.adults, 2)
        self.assertEqual(flagged.children, 1)
        self.assertEqual(flagged.infants_in_seat, 1)
        self.assertEqual(flagged.infants_on_lap, 1)
        family = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days for a family")
        self.assertEqual(family.intent, "flex")
        self.assertIsNone(family.children)

    def test_flex_named_currency_country_land_usd_word_does_not_invent(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days --currency USD --country US")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.currency, "USD")
        self.assertEqual(named.country, "US")
        vibe = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days in USD")
        self.assertEqual(vibe.intent, "flex")
        self.assertIsNone(vibe.currency)
        self.assertIsNone(vibe.country)

    def test_flex_named_alliance_lands_does_not_invent_members(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights, Star Alliance only")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(list(named.alliance), ["star"])
        self.assertEqual(list(named.include_airlines), [])
        flagged = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days --alliance oneworld --exclude-alliance star"
        )
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(list(flagged.alliance), ["oneworld"])
        self.assertEqual(list(flagged.exclude_alliance), ["star"])

    def test_flex_named_depart_window_lands_morning_vibe_does_not(self) -> None:
        named = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights, leave between 06:00 and 20:00"
        )
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.depart_window, "06:00-20:00")
        flagged = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days --depart-window 7-12")
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(flagged.depart_window, "7-12")
        vibe = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, leave in the morning")
        self.assertEqual(vibe.intent, "flex")
        self.assertIsNone(vibe.depart_window)

    def test_flex_named_arrive_before_depart_after_land_morning_vibe_does_not(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, depart after 18:00")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.depart_after, "18:00")
        flagged = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days --arrive-before 10:00 --depart-after 18:00"
        )
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(flagged.arrive_before, "10:00")
        self.assertEqual(flagged.depart_after, "18:00")
        vibe = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, leave in the morning")
        self.assertEqual(vibe.intent, "flex")
        self.assertIsNone(vibe.arrive_before)
        self.assertIsNone(vibe.depart_after)

    def test_flex_named_layover_and_duration_land_short_vibe_does_not(self) -> None:
        named = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights, max 3h layover")
        self.assertEqual(named.intent, "flex")
        self.assertEqual(named.max_layover, 3.0)
        flagged = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days "
            "--max-layover 3 --min-layover 1 --max-duration 8"
        )
        self.assertEqual(flagged.intent, "flex")
        self.assertEqual(flagged.max_layover, 3.0)
        self.assertEqual(flagged.min_layover, 1.0)
        self.assertEqual(flagged.max_duration, 8.0)
        vibe = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, short connections")
        self.assertEqual(vibe.intent, "flex")
        self.assertIsNone(vibe.max_layover)
        self.assertIsNone(vibe.min_layover)
        self.assertIsNone(vibe.max_duration)

    def test_flex_contradiction_does_not_pick_one(self) -> None:
        plan = plan_prompt(
            "BOS-LHR around 12 Sep 2026, flex 3 days, carry-on only and 2 checked bags"
        )
        self.assertEqual(plan.intent, "flex")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_explore_named_bags_via_cap_land_on_explore_intent(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, "
            "carry-on only, via IST, not via DXB, under 200€"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.origin, "SIN")
        self.assertEqual(plan.days, 7)
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.bags)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertEqual(plan.price_cap, 200)

    def test_explore_checked_bags_land(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, 2 checked bags"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.bags, 2)
        self.assertIsNone(plan.carry_on)
        self.assertIsNone(plan.baggage_buffer)

    def test_explore_named_baggage_buffer_copies_without_inventing_bags(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days --baggage-buffer 40"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.baggage_buffer, 40)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertIsNone(plan.baggage)

    def test_explore_named_baggage_buffer_prose_copies(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "rank with a 40€ bag buffer"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertEqual(plan.baggage_buffer, 40)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_explore_bags_vibe_does_not_invent_buffer(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days cheap with bags"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertIsNone(plan.baggage_buffer)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_explore_unnamed_shop_filters_stay_unset(self) -> None:
        plan = plan_prompt("Explore cheap destinations from SIN starting 2026-09-15, 7 days")
        self.assertEqual(plan.intent, "explore")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.exclude_via, ())
        self.assertEqual(plan.exclude_airports, ())
        self.assertEqual(plan.include_airports, ())
        self.assertEqual(plan.exclude_regions, ())
        self.assertIsNone(plan.price_cap)
        self.assertIsNone(plan.depart_window)
        self.assertIsNone(plan.arrive_before)
        self.assertIsNone(plan.depart_after)
        self.assertIsNone(plan.max_layover)
        self.assertIsNone(plan.min_layover)
        self.assertIsNone(plan.max_duration)
        self.assertEqual(plan.alliance, ())
        self.assertEqual(plan.exclude_alliance, ())
        self.assertIsNone(plan.children)
        self.assertIsNone(plan.infants_in_seat)
        self.assertIsNone(plan.infants_on_lap)
        self.assertIsNone(plan.currency)
        self.assertIsNone(plan.country)
        self.assertIsNone(plan.sort)
        self.assertIsNone(plan.baggage_buffer)
        self.assertEqual(plan.no_overnight, ())
        self.assertEqual(plan.require_overnight, ())

    def test_explore_named_exclude_airports_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, not HND"
        )
        self.assertEqual(named.intent, "explore")
        self.assertIn("HND", named.exclude_airports)
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days --exclude-airports HND"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertIn("HND", flagged.exclude_airports)
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, avoid Tokyo"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertEqual(vibe.exclude_airports, ())

    def test_explore_named_exclude_regions_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt("Destinations from NRT on 2026-09-15, not Asia")
        self.assertEqual(named.intent, "explore")
        self.assertIn("asia", named.exclude_regions)
        flagged = plan_prompt(
            "Explore cheap destinations from NRT starting 2026-09-15, 7 days --exclude-regions asia"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertIn("asia", flagged.exclude_regions)
        vibe = plan_prompt(
            "Explore cheap destinations from NRT starting 2026-09-15, 7 days, skip the Far East"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertEqual(vibe.exclude_regions, ())

    def test_explore_named_include_airports_and_dest_list_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "--include-airports NRT,HND"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(list(flagged.include_airports), ["NRT", "HND"])
        dest_list = plan_prompt(
            "Explore destinations from GRU in September 2026: NRT, HND. "
            "Do not brute-force the full date matrix; fixed dates first, then ±1 only on finalists."
        )
        self.assertEqual(dest_list.intent, "explore")
        self.assertEqual(list(dest_list.include_airports), ["NRT", "HND"])
        self.assertEqual(list(dest_list.destinations), ["NRT", "HND"])
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, Europe"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertEqual(vibe.include_airports, ())
        via_overnight = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, via IST, "
            "overnight in IST, max 1 stop."
        )
        self.assertEqual(via_overnight.intent, "explore")
        self.assertNotIn("SIN", via_overnight.include_airports)
        self.assertNotIn("IST", via_overnight.include_airports)
        use_nrt = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, use NRT"
        )
        self.assertEqual(use_nrt.intent, "explore")
        self.assertIn("NRT", use_nrt.include_airports)
        self.assertNotIn("SIN", use_nrt.include_airports)

    def test_explore_named_occupancy_lands_family_does_not_invent(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, 2 adults 1 child"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.adults, 2)
        self.assertEqual(named.children, 1)
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "--adults 2 --children 1 --infants-in-seat 1 --infants-on-lap 1"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(flagged.adults, 2)
        self.assertEqual(flagged.children, 1)
        self.assertEqual(flagged.infants_in_seat, 1)
        self.assertEqual(flagged.infants_on_lap, 1)
        family = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days for a family"
        )
        self.assertEqual(family.intent, "explore")
        self.assertIsNone(family.children)

    def test_explore_named_currency_country_land_usd_word_does_not_invent(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from JFK starting 2026-09-15, 7 days "
            "--currency USD --country US"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.currency, "USD")
        self.assertEqual(named.country, "US")
        vibe = plan_prompt("Explore cheap destinations from JFK starting 2026-09-15, 7 days in USD")
        self.assertEqual(vibe.intent, "explore")
        self.assertIsNone(vibe.currency)
        self.assertIsNone(vibe.country)

    def test_explore_named_alliance_lands_does_not_invent_members(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, Star Alliance only"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(list(named.alliance), ["star"])
        self.assertEqual(list(named.include_airlines), [])
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "--alliance oneworld --exclude-alliance star"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(list(flagged.alliance), ["oneworld"])
        self.assertEqual(list(flagged.exclude_alliance), ["star"])

    def test_explore_named_depart_window_lands_morning_vibe_does_not(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, "
            "leave between 06:00 and 20:00"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.depart_window, "06:00-20:00")
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days --depart-window 7-12"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(flagged.depart_window, "7-12")
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, leave in the morning"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertIsNone(vibe.depart_window)

    def test_explore_named_arrive_before_depart_after_land_morning_vibe_does_not(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, arrive before 10:00"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.arrive_before, "10:00")
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "--arrive-before 10:00 --depart-after 18:00"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(flagged.arrive_before, "10:00")
        self.assertEqual(flagged.depart_after, "18:00")
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, arrive in the morning"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertIsNone(vibe.arrive_before)
        self.assertIsNone(vibe.depart_after)

    def test_explore_named_layover_and_duration_land_short_vibe_does_not(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, max 3h layover"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.max_layover, 3.0)
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days "
            "--max-layover 3 --min-layover 1 --max-duration 8"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(flagged.max_layover, 3.0)
        self.assertEqual(flagged.min_layover, 1.0)
        self.assertEqual(flagged.max_duration, 8.0)
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, short connections"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertIsNone(vibe.max_layover)
        self.assertIsNone(vibe.min_layover)
        self.assertIsNone(vibe.max_duration)

    def test_explore_named_sort_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days --sort duration"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertEqual(flagged.sort, "duration")
        named = plan_prompt(
            "Explore cheap destinations from JFK starting 2026-09-15, 7 days, sort by duration"
        )
        self.assertEqual(named.intent, "explore")
        self.assertEqual(named.sort, "duration")
        fastest = plan_prompt(
            "Explore cheap destinations from JFK starting 2026-09-15, 7 days, fastest"
        )
        self.assertEqual(fastest.intent, "explore")
        self.assertEqual(fastest.sort, "duration")
        earliest = plan_prompt(
            "Explore cheap destinations from JFK starting 2026-09-15, 7 days, earliest"
        )
        self.assertEqual(earliest.intent, "explore")
        self.assertEqual(earliest.sort, "departure")
        vibe = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days in Europe"
        )
        self.assertEqual(vibe.intent, "explore")
        self.assertIsNone(vibe.sort)

    def test_explore_contradiction_does_not_pick_one(self) -> None:
        plan = plan_prompt(
            "Explore cheap destinations from SIN starting 2026-09-15, 7 days, "
            "carry-on only and 2 checked bags"
        )
        self.assertEqual(plan.intent, "explore")
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)

    def test_trip_named_bags_via_cap_land_on_search_trip(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip ADD-NBO on 2026-10-09 returning 2026-10-13, --trip rt, "
            "hotel in Nairobi those nights, 2 adults, 1 room, carry-on only, "
            "via IST, not via DXB, under 200€. Print the owned trip total "
            "when both searches succeed. Omit the sum if either side misses. "
            "Do not invent a fare or a stay."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.search_trip)
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.baggage, "carry_on_only")
        self.assertIsNone(plan.bags)
        self.assertEqual(plan.carry_on, 1)
        self.assertEqual(plan.via_airports, ("IST",))
        self.assertEqual(plan.exclude_via, ("DXB",))
        self.assertEqual(plan.price_cap, 200)
        parsed = plan_to_trips(plan)
        self.assertIsNone(parsed.bags)
        self.assertEqual(parsed.carry_on, 1)
        self.assertEqual(parsed.price_cap, 200)

    def test_trip_unnamed_shop_filters_stay_unset(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip ADD-NBO on 2026-10-09 returning 2026-10-13, --trip rt, "
            "hotel in Nairobi those nights, 2 adults, 1 room. Print the owned trip total "
            "when both searches succeed. Omit the sum if either side misses. "
            "Do not invent a fare or a stay."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.search_trip)
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)
        self.assertEqual(plan.via_airports, ())
        self.assertEqual(plan.exclude_via, ())
        self.assertEqual(plan.exclude_airports, ())
        self.assertEqual(plan.include_airports, ())
        self.assertIsNone(plan.price_cap)

    def test_trip_named_exclude_airports_lands_vibe_does_not_invent(self) -> None:
        named = plan_prompt(
            "Packaged round-trip NRT-SIN on 2026-10-20 returning 2026-10-24, --trip rt, "
            "hotel in Singapore those nights, 2 adults, 1 room, not HND. "
            "Print the owned trip total when both searches succeed. "
            "Omit the sum if either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(named.intent, "flights")
        self.assertTrue(named.search_trip)
        self.assertIn("HND", named.exclude_airports)
        flagged = plan_prompt(
            "Packaged round-trip NRT-SIN on 2026-10-20 returning 2026-10-24, --trip rt, "
            "hotel in Singapore those nights, 2 adults, 1 room --exclude-airports HND. "
            "Print the owned trip total when both searches succeed. "
            "Omit the sum if either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(flagged.intent, "flights")
        self.assertTrue(flagged.search_trip)
        self.assertIn("HND", flagged.exclude_airports)
        vibe = plan_prompt(
            "Packaged round-trip NRT-SIN on 2026-10-20 returning 2026-10-24, --trip rt, "
            "hotel in Singapore those nights, 2 adults, 1 room, avoid Tokyo. "
            "Print the owned trip total when both searches succeed. "
            "Omit the sum if either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(vibe.intent, "flights")
        self.assertTrue(vibe.search_trip)
        self.assertEqual(vibe.exclude_airports, ())

    def test_trip_named_include_airports_lands_vibe_does_not_invent(self) -> None:
        flagged = plan_prompt(
            "Packaged round-trip NRT-SIN on 2026-10-20 returning 2026-10-24, --trip rt, "
            "hotel in Singapore those nights, 2 adults, 1 room --include-airports SIN,HND. "
            "Print the owned trip total when both searches succeed. "
            "Omit the sum if either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(flagged.intent, "flights")
        self.assertTrue(flagged.search_trip)
        self.assertEqual(list(flagged.include_airports), ["SIN", "HND"])
        vibe = plan_prompt(
            "Packaged round-trip NRT-SIN on 2026-10-20 returning 2026-10-24, --trip rt, "
            "hotel in Singapore those nights, 2 adults, 1 room, Europe. "
            "Print the owned trip total when both searches succeed. "
            "Omit the sum if either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(vibe.intent, "flights")
        self.assertTrue(vibe.search_trip)
        self.assertEqual(vibe.include_airports, ())

    def test_trip_contradiction_does_not_pick_one(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip ADD-NBO on 2026-10-09 returning 2026-10-13, --trip rt, "
            "hotel in Nairobi those nights, 2 adults, 1 room, "
            "carry-on only and 2 checked bags. Do not invent a fare or a stay."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.search_trip)
        self.assertIsNone(plan.baggage)
        self.assertIsNone(plan.bags)
        self.assertIsNone(plan.carry_on)


class PromptPlanFamilyNearbyTests(unittest.TestCase):
    """Named nearby wording lands on dates, flex, explore, and search_trip."""

    def test_dates_named_nearby_lands_unnamed_stays_off(self) -> None:
        named = plan_prompt("cheapest week BOS to any London airport from 2026-09-01 to 2026-09-14")
        self.assertEqual(named.intent, "dates")
        self.assertTrue(named.nearby)
        self.assertEqual(named.origin, "BOS")
        self.assertEqual(named.destination, "LHR")
        self.assertEqual(named.destinations, ())
        flagged = plan_prompt("Price calendar BOS-LHR from 2026-09-01 to 2026-09-14 --nearby")
        self.assertEqual(flagged.intent, "dates")
        self.assertTrue(flagged.nearby)
        self.assertEqual(flagged.destination, "LHR")
        unnamed = plan_prompt("Price calendar BOS-LHR from 2026-09-01 to 2026-09-14")
        self.assertEqual(unnamed.intent, "dates")
        self.assertFalse(unnamed.nearby)
        mad = plan_prompt("Price calendar MAD-BCN from 2026-09-01 to 2026-09-14 --nearby")
        self.assertEqual(mad.intent, "dates")
        self.assertTrue(mad.nearby)
        self.assertEqual(mad.origin, "MAD")
        self.assertEqual(mad.destination, "BCN")
        self.assertEqual(mad.destinations, ())
        self.assertEqual(mad.route_specs, ())

    def test_flex_named_nearby_lands_unnamed_stays_off(self) -> None:
        named = plan_prompt("BOS to any London airport around 12 Sep 2026, flex 3 days, 7 nights")
        self.assertEqual(named.intent, "flex")
        self.assertTrue(named.nearby)
        self.assertEqual(named.origin, "BOS")
        self.assertEqual(named.destination, "LHR")
        self.assertEqual(named.flex_days, 3)
        self.assertEqual(named.destinations, ())
        flagged = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights --nearby")
        self.assertEqual(flagged.intent, "flex")
        self.assertTrue(flagged.nearby)
        unnamed = plan_prompt("BOS-LHR around 12 Sep 2026, flex 3 days, 7 nights")
        self.assertEqual(unnamed.intent, "flex")
        self.assertFalse(unnamed.nearby)
        mad = plan_prompt("MAD-BCN around 12 Sep 2026, flex 3 days --nearby")
        self.assertEqual(mad.intent, "flex")
        self.assertTrue(mad.nearby)
        self.assertEqual(mad.origin, "MAD")
        self.assertEqual(mad.destination, "BCN")
        self.assertEqual(mad.destinations, ())
        self.assertEqual(mad.route_specs, ())

    def test_explore_named_nearby_lands_unnamed_stays_off(self) -> None:
        named = plan_prompt(
            "Explore cheap destinations from any London airport starting 2026-09-15, 7 days"
        )
        self.assertEqual(named.intent, "explore")
        self.assertTrue(named.nearby)
        self.assertEqual(named.origin, "LHR")
        self.assertEqual(named.destination, None)
        self.assertEqual(named.destinations, ())
        flagged = plan_prompt(
            "Explore cheap destinations from LHR starting 2026-09-15, 7 days --nearby"
        )
        self.assertEqual(flagged.intent, "explore")
        self.assertTrue(flagged.nearby)
        self.assertEqual(flagged.origin, "LHR")
        unnamed = plan_prompt("Explore cheap destinations from LHR starting 2026-09-15, 7 days")
        self.assertEqual(unnamed.intent, "explore")
        self.assertFalse(unnamed.nearby)
        mad = plan_prompt(
            "Explore cheap destinations from MAD starting 2026-09-15, 7 days --nearby"
        )
        self.assertEqual(mad.intent, "explore")
        self.assertTrue(mad.nearby)
        self.assertEqual(mad.origin, "MAD")
        self.assertEqual(mad.destinations, ())

    def test_trip_named_nearby_lands_unnamed_stays_off(self) -> None:
        named = plan_prompt(
            "Packaged round-trip BOS to any London airport on 2026-09-18 returning "
            "2026-09-22, --trip rt, hotel in London those nights, 2 adults, 1 room. "
            "Print the owned trip total when both searches succeed. Omit the sum if "
            "either side misses. Do not invent a fare or a stay."
        )
        self.assertEqual(named.intent, "flights")
        self.assertTrue(named.search_trip)
        self.assertTrue(named.nearby)
        self.assertEqual(named.origin, "BOS")
        self.assertEqual(named.destination, "LHR")
        parsed = plan_to_trips(named)
        self.assertEqual(parsed.origin, "BOS")
        self.assertEqual(parsed.destination, "LHR")
        flagged = plan_prompt(
            "Packaged round-trip BOS-LHR on 2026-09-18 returning 2026-09-22, "
            "--trip rt --nearby, hotel in London those nights, 2 adults, 1 room. "
            "Print the owned trip total when both searches succeed. Omit the sum if "
            "either side misses. Do not invent a fare or a stay."
        )
        self.assertTrue(flagged.search_trip)
        self.assertTrue(flagged.nearby)
        unnamed = plan_prompt(
            "Packaged round-trip BOS-LHR on 2026-09-18 returning 2026-09-22, --trip rt, "
            "hotel in London those nights, 2 adults, 1 room. Print the owned trip total "
            "when both searches succeed. Omit the sum if either side misses. "
            "Do not invent a fare or a stay."
        )
        self.assertTrue(unnamed.search_trip)
        self.assertFalse(unnamed.nearby)
        mad = plan_prompt(
            "Packaged round-trip MAD-BCN on 2026-09-18 returning 2026-09-22, "
            "--trip rt --nearby, hotel in Barcelona those nights, 2 adults, 1 room. "
            "Print the owned trip total when both searches succeed. Omit the sum if "
            "either side misses. Do not invent a fare or a stay."
        )
        self.assertTrue(mad.search_trip)
        self.assertTrue(mad.nearby)
        self.assertEqual(mad.origin, "MAD")
        self.assertEqual(mad.destination, "BCN")
        self.assertEqual(mad.destinations, ())
        kept = plan_to_trips(mad)
        self.assertEqual(kept.origin, "MAD")
        self.assertEqual(kept.destination, "BCN")


if __name__ == "__main__":
    unittest.main()
