from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from viajante.flights import (
    _normalize_offer,
    expand_nearby_trips,
    parse_flight_plan,
    parse_named_clock,
    parse_route_specs,
)
from viajante.google_flights import RawFlightCard
from viajante.models import (
    AppliedHotelFilters,
    CancellationEvidence,
    FlightOffer,
    FlightQuery,
    HotelOffer,
    HotelQuery,
    HotelQueryFailure,
    HotelQuerySuccess,
    HotelSearchReport,
    LodgingKind,
    PropertyTypeEvidence,
    QueryFailure,
    QuerySuccess,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
    TripSearchReport,
    TripTotal,
)
from viajante.prompt_plan import plan_prompt, plan_to_hotel_query, plan_to_trips
from viajante.trip import (
    dates_overlap,
    format_trip_total,
    owned_trip_total,
    search_trip,
    stay_window_from_trips,
    trip_date_span,
)


def _flight_offer(*, price: float = 412.0) -> FlightOffer:
    return FlightOffer(
        airline="Example Air",
        departure="08:40",
        arrival="11:30",
        price_text=f"€{price:.0f}",
        price=price,
        duration="2 h 50 min",
        duration_hours=2.8,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer=0,
        needs_bag_verify=False,
    )


def _hotel_offer(*, total_price: float = 246.0) -> HotelOffer:
    return HotelOffer(
        title="Old Town Apartment",
        address="Melbourne",
        total_price_text=f"{total_price:.0f} €",
        total_price=total_price,
        rating="8.9",
        rating_score=8.9,
        details="Free cancellation",
        cancellation_evidence=CancellationEvidence.FREE,
        property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
        lodging_kind=LodgingKind.ENTIRE_HOME,
        bedrooms=1,
        bathrooms=1,
        beds=2,
        link=None,
    )


def _flight_report(
    *results: QuerySuccess | QueryFailure,
    currency: str = "EUR",
) -> SearchReport:
    return SearchReport(
        searched_at=datetime(2026, 8, 11, 10, 32, 0, tzinfo=timezone.utc),
        queries=results,
        currency=currency,
        fetch_backend="sweep",
        fetch_ms=100,
    )


def _hotel_report(
    *results: HotelQuerySuccess | HotelQueryFailure,
    currency: str = "EUR",
) -> HotelSearchReport:
    return HotelSearchReport(
        searched_at=datetime(2026, 8, 11, 10, 33, 0, tzinfo=timezone.utc),
        queries=results,
        currency=currency,
        fetch_backend="google",
        fetch_ms=80,
        provider="google-hotels",
    )


def _applied() -> AppliedHotelFilters:
    return AppliedHotelFilters(chips=("free_cancellation=1",), url="https://example.test")


def _card(**kwargs: object) -> RawFlightCard:
    fields: dict[str, object] = {
        "airline": "Iberia",
        "departure": "08:00",
        "arrival": "09:20",
        "duration": "1 hr 20 min",
        "stops": "Nonstop",
        "price": "€90",
    }
    fields.update(kwargs)
    return RawFlightCard(**fields)  # type: ignore[arg-type]


class FakeFlightSource:
    def __init__(self, cards: tuple[RawFlightCard, ...]) -> None:
        self.cards = cards
        self.fetched_queries: list[object] = []
        self.closed = False
        self.config = SimpleNamespace(html_lang="en", currency="EUR")

    def fetch(self, query: object) -> tuple[RawFlightCard, ...]:
        self.fetched_queries.append(query)
        return self.cards

    def reset(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def _melbourne_stay() -> HotelSearchReport:
    return _hotel_report(
        HotelQuerySuccess(
            query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
            applied=_applied(),
            raw_count=1,
            eligible_count=1,
            offers=(_hotel_offer(total_price=50),),
        )
    )


def _sin_mel() -> RoundTrip:
    return RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10))


def _search_trip_cards(
    *cards: RawFlightCard,
    **filters: object,
) -> tuple[TripSearchReport, FakeFlightSource]:
    source = FakeFlightSource(cards)
    hotels = _melbourne_stay()
    with (
        patch("viajante.flights.GoogleFlightsHttpSource", return_value=source),
        patch("viajante.trip.search_hotels", return_value=hotels),
    ):
        report = search_trip(
            (_sin_mel(),),
            HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
            fetch="sweep",
            baggage_buffer=0,
            **filters,  # type: ignore[arg-type]
        )
    return report, source


class TripJoinTests(unittest.TestCase):
    def test_packaged_rt_plus_hotel_sums_owned_fare_and_stay(self) -> None:
        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip(
                    "SIN",
                    "MEL",
                    date(2026, 11, 6),
                    date(2026, 11, 10),
                    adults=2,
                ),
                raw_count=4,
                eligible_count=1,
                offers=(_flight_offer(price=412),),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery(
                    "Melbourne",
                    date(2026, 11, 6),
                    date(2026, 11, 10),
                    adults=2,
                ),
                applied=_applied(),
                raw_count=8,
                eligible_count=1,
                offers=(_hotel_offer(total_price=246),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare, 412)
        self.assertEqual(total.hotel_stay, 246)
        self.assertEqual(total.total, 658)
        self.assertEqual(total.nights, 4)
        self.assertEqual(total.hotel_price_basis, "total_stay")
        self.assertEqual(hotels.price_basis, "total_stay")
        self.assertIn("total stay", format_trip_total(total, "EUR"))

    def test_two_one_ways_sum_each_cheapest_fare(self) -> None:
        out = FlightQuery("DUB", "JFK", date(2026, 10, 9), adults=2)
        back = FlightQuery("JFK", "DUB", date(2026, 10, 13), adults=2)
        flights = _flight_report(
            QuerySuccess(
                query=out,
                raw_count=2,
                eligible_count=2,
                offers=(_flight_offer(price=400), _flight_offer(price=350)),
            ),
            QuerySuccess(
                query=back,
                raw_count=2,
                eligible_count=1,
                offers=(_flight_offer(price=380),),
            ),
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("New York", date(2026, 10, 9), date(2026, 10, 13), adults=2),
                applied=_applied(),
                raw_count=3,
                eligible_count=1,
                offers=(_hotel_offer(total_price=500),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare, 730)
        self.assertEqual(total.hotel_stay, 500)
        self.assertEqual(total.total, 1230)

    def test_same_route_two_dates_uses_cheaper_day_not_sum(self) -> None:
        first = FlightQuery("SIN", "MEL", date(2026, 11, 6))
        second = FlightQuery("SIN", "MEL", date(2026, 11, 7))
        flights = _flight_report(
            QuerySuccess(
                query=first, raw_count=1, eligible_count=1, offers=(_flight_offer(price=400),)
            ),
            QuerySuccess(
                query=second, raw_count=1, eligible_count=1, offers=(_flight_offer(price=300),)
            ),
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(total_price=200),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare, 300)

    def test_omits_when_hotel_offers_empty(self) -> None:
        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                raw_count=4,
                eligible_count=1,
                offers=(_flight_offer(),),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=3,
                eligible_count=0,
                offers=(),
            )
        )
        self.assertIsNone(owned_trip_total(flights, hotels))

    def test_omits_when_flight_query_fails(self) -> None:
        flights = _flight_report(
            QueryFailure(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                error=SearchError(SearchErrorCode.NO_RESULTS, "no flights"),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(),),
            )
        )
        self.assertIsNone(owned_trip_total(flights, hotels))

    def test_omits_when_dates_do_not_overlap(self) -> None:
        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                raw_count=1,
                eligible_count=1,
                offers=(_flight_offer(),),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 12, 1), date(2026, 12, 5)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(),),
            )
        )
        self.assertIsNone(owned_trip_total(flights, hotels))

    def test_omits_when_currencies_differ(self) -> None:
        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                raw_count=1,
                eligible_count=1,
                offers=(_flight_offer(),),
            ),
            currency="EUR",
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(),),
            ),
            currency="USD",
        )
        self.assertIsNone(owned_trip_total(flights, hotels))

    def test_stay_window_from_two_dates(self) -> None:
        window = stay_window_from_trips(
            (
                FlightQuery("SIN", "MEL", date(2026, 11, 6)),
                FlightQuery("MEL", "SIN", date(2026, 11, 10)),
            )
        )
        self.assertEqual(window, (date(2026, 11, 6), date(2026, 11, 10)))
        self.assertIsNone(stay_window_from_trips((FlightQuery("SIN", "MEL", date(2026, 11, 6)),)))
        start, end = trip_date_span(RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)))
        self.assertTrue(dates_overlap(start, end, date(2026, 11, 6), date(2026, 11, 10)))

    def test_search_trip_runs_flights_then_hotels(self) -> None:
        order: list[str] = []
        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                raw_count=1,
                eligible_count=1,
                offers=(_flight_offer(price=100),),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(total_price=50),),
            )
        )

        def fake_flights(*_args: object, **_kwargs: object) -> SearchReport:
            order.append("flights")
            return flights

        def fake_hotels(*_args: object, **_kwargs: object) -> HotelSearchReport:
            order.append("hotels")
            return hotels

        query = HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10))
        with (
            patch("viajante.trip.search_flights", fake_flights),
            patch("viajante.trip.search_hotels", fake_hotels),
        ):
            report = search_trip(
                (RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),),
                query,
                hotel_source="google",
            )
        self.assertEqual(order, ["flights", "hotels"])
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.total, 150)
        self.assertEqual(report.hotels.price_basis, "total_stay")
        self.assertEqual(report.fetch_ms, 180)

    def test_search_trip_rejects_child_occupancy_before_fetch(self) -> None:
        hotel = HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10))
        with patch("viajante.trip.search_flights") as flights:
            with patch("viajante.trip.search_hotels") as hotels:
                with self.assertRaises(ValueError) as ctx:
                    search_trip(
                        (FlightQuery("SIN", "MEL", date(2026, 11, 6), children=1),),
                        hotel,
                    )
        flights.assert_not_called()
        hotels.assert_not_called()
        self.assertIn("adults-only", str(ctx.exception))
        self.assertIn("search_flights", str(ctx.exception))

    def test_search_trip_rejects_infant_occupancy_before_fetch(self) -> None:
        hotel = HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10))
        with patch("viajante.trip.search_flights") as flights:
            with self.assertRaises(ValueError):
                search_trip(
                    (
                        RoundTrip(
                            "SIN",
                            "MEL",
                            date(2026, 11, 6),
                            date(2026, 11, 10),
                            infants_in_seat=1,
                        ),
                    ),
                    hotel,
                )
        flights.assert_not_called()

    def test_trip_total_rejects_invented_zero(self) -> None:
        with self.assertRaises(ValueError):
            TripTotal(flight_fare=0, hotel_stay=10, total=10, nights=1)
        with self.assertRaises(ValueError):
            TripTotal(flight_fare=10, hotel_stay=10, total=21, nights=1)

    def test_search_trip_partial_hotel_failure_omits_total_keeps_both(self) -> None:
        from viajante.models import HotelQueryFailure, SearchError, SearchErrorCode

        flights = _flight_report(
            QuerySuccess(
                query=RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),
                raw_count=1,
                eligible_count=1,
                offers=(_flight_offer(price=100),),
            )
        )
        hotels = _hotel_report(
            HotelQueryFailure(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                error=SearchError(SearchErrorCode.BLOCKED, "blocked"),
            )
        )

        def fake_flights(*_args: object, **_kwargs: object) -> SearchReport:
            return flights

        def fake_hotels(*_args: object, **_kwargs: object) -> HotelSearchReport:
            return hotels

        query = HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10))
        with (
            patch("viajante.trip.search_flights", fake_flights),
            patch("viajante.trip.search_hotels", fake_hotels),
        ):
            report = search_trip(
                (RoundTrip("SIN", "MEL", date(2026, 11, 6), date(2026, 11, 10)),),
                query,
                hotel_source="google",
            )
        self.assertIsNone(report.trip_total)
        payload = report.to_dict()
        self.assertIn("flights", payload)
        self.assertIn("hotels", payload)
        self.assertNotIn("trip_total", payload)


class TripShopFilterTests(unittest.TestCase):
    def test_named_bags_via_airlines_price_cap_drop_the_same_cards_as_flights(self) -> None:
        silent = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€40",
        )
        too_few = _card(
            checked_bags=0,
            carry_on=1,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€28",
        )
        enough = _card(
            checked_bags=1,
            carry_on=1,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€55",
        )
        via_dxb = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="DXB",
            price="€60",
        )
        ryanair = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="LIS",
            price="€35",
        )
        over_cap = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€250",
        )
        cards = (silent, too_few, enough, via_dxb, ryanair, over_cap)
        filters = dict(bags=1, carry_on=1, via=("LIS",), airlines=("IB",), price_cap=200)
        expected = [_normalize_offer(card, 1, baggage_buffer=0, **filters) for card in cards]
        kept = [offer.price for offer in expected if offer is not None]
        report, source = _search_trip_cards(*cards, **filters)
        result = report.flights.queries[0]
        self.assertEqual(type(result).__name__, "QuerySuccess")
        self.assertEqual([offer.price for offer in result.offers], kept)
        self.assertEqual(kept, [40.0, 55.0])
        self.assertEqual(source.fetched_queries[0].bags, 1)
        self.assertEqual(source.fetched_queries[0].carry_on, 1)
        self.assertEqual(source.fetched_queries[0].airlines, ("IB",))
        self.assertEqual(source.fetched_queries[0].price_cap, 200)
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare, 40.0)
        self.assertEqual(report.trip_total.hotel_stay, 50.0)
        self.assertEqual(report.trip_total.total, 90.0)

    def test_unnamed_shop_filters_keep_contradicting_cards(self) -> None:
        cheap = _card(
            checked_bags=0,
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="DXB",
            price="€28",
        )
        report, source = _search_trip_cards(cheap)
        result = report.flights.queries[0]
        self.assertEqual([offer.price for offer in result.offers], [28.0])
        self.assertIsNone(source.fetched_queries[0].bags)
        self.assertIsNone(source.fetched_queries[0].carry_on)
        self.assertIsNone(source.fetched_queries[0].airlines)
        self.assertIsNone(source.fetched_queries[0].price_cap)
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare, 28.0)

    def test_named_clock_filters_drop_late_arrivals_and_early_departs(self) -> None:
        on_time = _card(arrival="09:00", departure="19:00", price="€90")
        late_arrive = _card(arrival="23:00", departure="19:00", price="€40")
        early_depart = _card(arrival="09:00", departure="08:00", price="€35")
        silent = _card(arrival=None, departure=None, price="€28")
        filters = dict(
            arrive_before=parse_named_clock("10:00", role="arrive-before"),
            depart_after=parse_named_clock("18:00", role="depart-after"),
        )
        expected = [
            _normalize_offer(card, 1, baggage_buffer=0, **filters)
            for card in (on_time, late_arrive, early_depart, silent)
        ]
        kept = [offer.price for offer in expected if offer is not None]
        report, _source = _search_trip_cards(on_time, late_arrive, early_depart, silent, **filters)
        result = report.flights.queries[0]
        self.assertEqual([offer.price for offer in result.offers], kept)
        self.assertEqual(kept, [90.0])
        unnamed, _ = _search_trip_cards(on_time, late_arrive, early_depart, silent)
        unnamed_result = unnamed.flights.queries[0]
        self.assertEqual(
            [offer.price for offer in unnamed_result.offers],
            [28.0, 35.0, 40.0, 90.0],
        )

    def test_trip_total_omitted_when_filters_empty_the_flight_side(self) -> None:
        too_few = _card(
            checked_bags=0,
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€28",
        )
        over_cap = _card(
            airline_codes=("IB",),
            stops="1 stop",
            layover_city="LIS",
            price="€250",
        )
        report, _source = _search_trip_cards(
            too_few,
            over_cap,
            bags=1,
            via=("LIS",),
            airlines=("IB",),
            price_cap=200,
        )
        result = report.flights.queries[0]
        self.assertEqual(result.offers, ())
        self.assertIsNone(report.trip_total)

    def test_exclude_via_and_exclude_airlines_match_normalize(self) -> None:
        keep = _card(stops="1 stop", layover_city="OPO", airline_codes=("IB",), price="€100")
        drop_via = _card(stops="1 stop", layover_city="LIS", airline_codes=("IB",), price="€80")
        drop_airline = _card(
            airline="Ryanair",
            airline_codes=("FR",),
            stops="1 stop",
            layover_city="OPO",
            price="€70",
        )
        cards = (keep, drop_via, drop_airline)
        filters = dict(exclude_via=("LIS",), exclude_airlines=("FR",))
        expected = [_normalize_offer(card, 1, baggage_buffer=0, **filters) for card in cards]
        kept = [offer.price for offer in expected if offer is not None]
        report, _source = _search_trip_cards(*cards, **filters)
        result = report.flights.queries[0]
        self.assertEqual([offer.price for offer in result.offers], kept)
        self.assertEqual(kept, [100.0])
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare, 100.0)


class PlanToHotelQueryTests(unittest.TestCase):
    def test_combined_plan_shares_dates_and_adults(self) -> None:
        plan = plan_prompt(
            "Packaged round-trip SIN-MEL on 2026-11-06 returning 2026-11-10, --trip rt, "
            "and hotel in Melbourne from 2026-11-06 to 2026-11-10, 2 adults, 1 room, "
            "free cancellation. Print the owned trip total when both searches succeed. "
            "Do not invent a fare or a hotel total."
        )
        self.assertEqual(plan.intent, "flights")
        self.assertTrue(plan.hotels)
        self.assertEqual(plan.trip, "rt")
        self.assertEqual(plan.adults, 2)
        self.assertEqual(plan.rooms, 1)
        self.assertEqual(plan.check_in, date(2026, 11, 6))
        self.assertEqual(plan.check_out, date(2026, 11, 10))
        self.assertEqual(plan.locale, "en")
        self.assertTrue(plan.search_trip)
        self.assertIn("search_trip", plan.notes)
        trips = plan_to_trips(plan)
        hotel = plan_to_hotel_query(plan)
        self.assertEqual(trips.origin, "SIN")
        self.assertEqual(trips.destination, "MEL")
        self.assertEqual(trips.adults, 2)
        self.assertEqual(hotel.location, "Melbourne")
        self.assertEqual(hotel.adults, 2)
        self.assertEqual(hotel.check_in, trips.departure_date)
        self.assertEqual(hotel.check_out, trips.return_date)

    def test_plan_to_hotel_query_refuses_flights_only(self) -> None:
        plan = plan_prompt("Flights SIN-MEL on 2026-11-06")
        with self.assertRaises(ValueError):
            plan_to_hotel_query(plan)


class NearbyTripTests(unittest.TestCase):
    def test_nearby_expands_london_and_does_not_sum_alternative_fares(self) -> None:
        packaged = parse_flight_plan(
            ["BOS-LHR:2026-09-18:2026-09-22"],
            trip="rt",
            max_stops=1,
        )
        off = expand_nearby_trips((packaged,), nearby=False)
        self.assertEqual(len(off), 1)
        self.assertEqual((off[0].origin, off[0].destination), ("BOS", "LHR"))
        self.assertIsNone(off[0].nearby_label)

        expanded = expand_nearby_trips((packaged,), nearby=True)
        dests = {item.destination for item in expanded}
        self.assertGreater(len(expanded), 1)
        self.assertEqual((expanded[0].origin, expanded[0].destination), ("BOS", "LHR"))
        self.assertTrue({"LHR", "LGW", "STN", "LTN", "LCY"} <= dests)
        self.assertNotIn("BQH", dests)
        self.assertTrue(all(item.origin == "BOS" for item in expanded))
        self.assertTrue(all(item.nearby_label for item in expanded))

        source = FakeFlightSource((_card(price="€90"),))
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("London", date(2026, 9, 18), date(2026, 9, 22)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(total_price=50),),
            )
        )
        with (
            patch("viajante.flights.GoogleFlightsHttpSource", return_value=source),
            patch("viajante.trip.search_hotels", return_value=hotels),
        ):
            report = search_trip(
                expanded,
                HotelQuery("London", date(2026, 9, 18), date(2026, 9, 22)),
                fetch="sweep",
                baggage_buffer=0,
            )
        fetched = {query.destination for query in source.fetched_queries}
        self.assertEqual(fetched, dests)
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare, 90)
        self.assertEqual(report.trip_total.hotel_stay, 50)
        self.assertEqual(report.trip_total.total, 140)

    def test_nearby_unknown_city_does_not_invent_codes(self) -> None:
        queries = parse_route_specs(["MAD-BCN:2026-09-18"], max_stops=1)
        expanded = expand_nearby_trips(queries, nearby=True)
        self.assertEqual(len(expanded), 1)
        self.assertEqual((expanded[0].origin, expanded[0].destination), ("MAD", "BCN"))
        self.assertIsNone(expanded[0].nearby_label)


if __name__ == "__main__":
    unittest.main()
