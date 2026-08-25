from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from viajante.flights import _normalize_offer
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


def _flight_offer(*, price_eur: float = 412.0) -> FlightOffer:
    return FlightOffer(
        airline="Example Air",
        departure="08:40",
        arrival="11:30",
        price=f"€{price_eur:.0f}",
        price_eur=price_eur,
        duration="2 h 50 min",
        duration_hours=2.8,
        stops="Nonstop",
        stops_count=0,
        baggage_buffer_eur=0,
        needs_bag_verify=False,
    )


def _hotel_offer(*, total_price_eur: float = 246.0) -> HotelOffer:
    return HotelOffer(
        title="Old Town Apartment",
        address="Melbourne",
        total_price=f"{total_price_eur:.0f} €",
        total_price_eur=total_price_eur,
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
            offers=(_hotel_offer(total_price_eur=50),),
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
            buffer_eur=0,
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
                offers=(_flight_offer(price_eur=412),),
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
                offers=(_hotel_offer(total_price_eur=246),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare_eur, 412)
        self.assertEqual(total.hotel_stay_eur, 246)
        self.assertEqual(total.total_eur, 658)
        self.assertEqual(total.nights, 4)
        self.assertEqual(total.hotel_price_basis, "total_stay")
        self.assertEqual(hotels.price_basis, "total_stay")
        self.assertIn("total stay", format_trip_total(total))

    def test_two_one_ways_sum_each_cheapest_fare(self) -> None:
        out = FlightQuery("DUB", "JFK", date(2026, 10, 9), adults=2)
        back = FlightQuery("JFK", "DUB", date(2026, 10, 13), adults=2)
        flights = _flight_report(
            QuerySuccess(
                query=out,
                raw_count=2,
                eligible_count=2,
                offers=(_flight_offer(price_eur=400), _flight_offer(price_eur=350)),
            ),
            QuerySuccess(
                query=back,
                raw_count=2,
                eligible_count=1,
                offers=(_flight_offer(price_eur=380),),
            ),
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("New York", date(2026, 10, 9), date(2026, 10, 13), adults=2),
                applied=_applied(),
                raw_count=3,
                eligible_count=1,
                offers=(_hotel_offer(total_price_eur=500),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare_eur, 730)
        self.assertEqual(total.hotel_stay_eur, 500)
        self.assertEqual(total.total_eur, 1230)

    def test_same_route_two_dates_uses_cheaper_day_not_sum(self) -> None:
        first = FlightQuery("SIN", "MEL", date(2026, 11, 6))
        second = FlightQuery("SIN", "MEL", date(2026, 11, 7))
        flights = _flight_report(
            QuerySuccess(
                query=first, raw_count=1, eligible_count=1, offers=(_flight_offer(price_eur=400),)
            ),
            QuerySuccess(
                query=second, raw_count=1, eligible_count=1, offers=(_flight_offer(price_eur=300),)
            ),
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(total_price_eur=200),),
            )
        )
        total = owned_trip_total(flights, hotels)
        self.assertIsNotNone(total)
        assert total is not None
        self.assertEqual(total.flight_fare_eur, 300)

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
                offers=(_flight_offer(price_eur=100),),
            )
        )
        hotels = _hotel_report(
            HotelQuerySuccess(
                query=HotelQuery("Melbourne", date(2026, 11, 6), date(2026, 11, 10)),
                applied=_applied(),
                raw_count=1,
                eligible_count=1,
                offers=(_hotel_offer(total_price_eur=50),),
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
        self.assertEqual(report.trip_total.total_eur, 150)
        self.assertEqual(report.hotels.price_basis, "total_stay")
        self.assertEqual(report.fetch_ms, 180)

    def test_trip_total_rejects_invented_zero(self) -> None:
        with self.assertRaises(ValueError):
            TripTotal(flight_fare_eur=0, hotel_stay_eur=10, total_eur=10, nights=1)
        with self.assertRaises(ValueError):
            TripTotal(flight_fare_eur=10, hotel_stay_eur=10, total_eur=21, nights=1)


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
        filters = dict(bags=1, carry_on=1, via=("LIS",), airlines=("IB",), price_cap_eur=200)
        expected = [_normalize_offer(card, 1, buffer_eur=0, **filters) for card in cards]
        kept = [offer.price_eur for offer in expected if offer is not None]
        report, source = _search_trip_cards(*cards, **filters)
        result = report.flights.queries[0]
        self.assertEqual(type(result).__name__, "QuerySuccess")
        self.assertEqual([offer.price_eur for offer in result.offers], kept)
        self.assertEqual(kept, [40.0, 55.0])
        self.assertEqual(source.fetched_queries[0].bags, 1)
        self.assertEqual(source.fetched_queries[0].carry_on, 1)
        self.assertEqual(source.fetched_queries[0].airlines, ("IB",))
        self.assertEqual(source.fetched_queries[0].price_cap_eur, 200)
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare_eur, 40.0)
        self.assertEqual(report.trip_total.hotel_stay_eur, 50.0)
        self.assertEqual(report.trip_total.total_eur, 90.0)

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
        self.assertEqual([offer.price_eur for offer in result.offers], [28.0])
        self.assertIsNone(source.fetched_queries[0].bags)
        self.assertIsNone(source.fetched_queries[0].carry_on)
        self.assertIsNone(source.fetched_queries[0].airlines)
        self.assertIsNone(source.fetched_queries[0].price_cap_eur)
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare_eur, 28.0)

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
            price_cap_eur=200,
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
        expected = [_normalize_offer(card, 1, buffer_eur=0, **filters) for card in cards]
        kept = [offer.price_eur for offer in expected if offer is not None]
        report, _source = _search_trip_cards(*cards, **filters)
        result = report.flights.queries[0]
        self.assertEqual([offer.price_eur for offer in result.offers], kept)
        self.assertEqual(kept, [100.0])
        self.assertIsNotNone(report.trip_total)
        assert report.trip_total is not None
        self.assertEqual(report.trip_total.flight_fare_eur, 100.0)


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


if __name__ == "__main__":
    unittest.main()
