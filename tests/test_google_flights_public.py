from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.control import SearchCancelled, SearchDeadline
from viajante.dates import search_dates, search_flex
from viajante.google_flights import SweepHttpResponse
from viajante.google_flights_public import (
    GoogleFlightsUnsupported,
    PublicGoogleFlightsHttpSource,
)
from viajante.google_flights_rpc import RawFlightCard
from viajante.models import FlightQuery, MultiCity, RawJourneyLeg, RawSegment, RoundTrip

OUT = date(2026, 11, 14)
BACK = date(2026, 11, 25)


def _leg(origin: str, dest: str, day: date, clock: str, flight: str) -> RawJourneyLeg:
    return RawJourneyLeg(
        clock,
        "20:00",
        "10 hr",
        "nonstop",
        (RawSegment(origin, dest, clock, "20:00", "Test Air", flight, day, "TA"),),
    )


def _card(origin: str, dest: str, day: date, clock: str, flight: str, price: str) -> RawFlightCard:
    return RawFlightCard(
        "Test Air",
        clock,
        "20:00",
        "10 hr",
        "nonstop",
        price,
        flight_numbers=(flight,),
        legs=(_leg(origin, dest, day, clock, flight),),
    )


def _context(*, adults=2, currency="EUR", cabin="Economy"):
    controls = {
        "adult passengers": adults,
        "children aged 2 to 11": 0,
        "infants in their own seat": 0,
        "infants on lap": 0,
    }
    return (
        f'<button aria-label="Currency {currency}"></button><div role="combobox">{cabin}</div>'
        + "".join(
            f'<div aria-label="Number of {label}"><span aria-live="polite">{count}</span></div>'
            for label, count in controls.items()
        )
    )


def _source() -> PublicGoogleFlightsHttpSource:
    return PublicGoogleFlightsHttpSource(currency="EUR", client=object())


class PublicFlightsSourceTests(unittest.TestCase):
    def test_one_way_fails_closed_on_route_date_and_stop_mismatch(self) -> None:
        source = _source()
        query = FlightQuery("HAN", "SIN", OUT, max_stops=0, adults=2)
        wrong = (
            _card("SGN", "SIN", OUT, "08:00", "TA1", "€100"),
            _card("HAN", "SIN", date(2026, 11, 15), "08:00", "TA2", "€100"),
            RawFlightCard(
                "Test Air",
                "08:00",
                "20:00",
                "10 hr",
                "1 stop",
                "€100",
                legs=(
                    RawJourneyLeg(
                        "08:00",
                        "20:00",
                        "10 hr",
                        "1 stop",
                        (
                            RawSegment(
                                "HAN", "BKK", "08:00", "12:00", "Test Air", "TA3", OUT, "TA"
                            ),
                            RawSegment(
                                "BKK", "SIN", "14:00", "20:00", "Test Air", "TA4", OUT, "TA"
                            ),
                        ),
                    ),
                ),
            ),
        )
        with patch.object(source, "_read_page", return_value=(wrong, "")):
            with self.assertRaisesRegex(Exception, "did not prove the requested route"):
                source.fetch(query)

    def test_bags_carriers_and_multi_city_fail_before_any_get(self) -> None:
        class NoNetwork:
            def get(self, *args, **kwargs):
                raise AssertionError("unsupported query must not send a request")

        source = PublicGoogleFlightsHttpSource(currency="EUR", client=NoNetwork())
        for query in (
            FlightQuery("HAN", "SIN", OUT, adults=2, bags=1),
            FlightQuery("HAN", "SIN", OUT, adults=2, carry_on=1),
            FlightQuery("HAN", "SIN", OUT, adults=2, airlines=("TA",)),
            FlightQuery("HAN", "SIN", OUT, adults=2, exclude_airlines=("TA",)),
            FlightQuery("HAN", "SIN", OUT, adults=2, alliances=("star",)),
            MultiCity(
                (
                    FlightQuery("HAN", "SIN", OUT, adults=2).legs[0],
                    FlightQuery("SIN", "HAN", BACK, adults=2).legs[0],
                ),
                adults=2,
            ),
        ):
            with self.subTest(query=query), self.assertRaises(GoogleFlightsUnsupported):
                source.fetch(query)

    def test_round_trip_replaces_outbound_package_reference_with_provider_total(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outbound = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        html = "<div>HAN–SIN 8:00 AM Choose return round trip</div>"
        reads = []

        def read(trip, selected=None):
            reads.append(selected)
            return ((outbound,) if selected is None else (returned,)), (
                "<div>initial</div>" if selected is None else html
            )

        with patch.object(source, "_read_page", side_effect=read):
            offers = source.fetch(query)
        self.assertEqual(len(reads), 2)
        self.assertIsNone(reads[0])
        self.assertIsNotNone(reads[1])
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, "€340")
        self.assertEqual(len(offers[0].legs), 2)
        self.assertEqual(offers[0].legs[1].segments[0].flight_number, "TA202")
        self.assertTrue(source.scope_bound)

    def test_equal_priced_return_options_are_preserved(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outbound = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")
        returns = (
            _card("SIN", "HAN", BACK, "09:00", "TA202", "€340"),
            _card("SIN", "HAN", BACK, "11:00", "TA204", "€340"),
        )
        html = "<div>HAN–SIN 8:00 AM Choose return round trip</div>"
        with patch.object(source, "_read_page", side_effect=[((outbound,), ""), (returns, html)]):
            offers = source.fetch(query)
        self.assertEqual(len(offers), 2)
        self.assertEqual([offer.price for offer in offers], ["€340", "€340"])
        self.assertEqual(
            [offer.legs[1].segments[0].flight_number for offer in offers], ["TA202", "TA204"]
        )

    def test_wrong_selected_outbound_echo_cannot_prove_a_complete_rt(self) -> None:
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        outbound = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        wrong = "<div>SGN–SIN 8:00 AM Choose return round trip</div>"
        pages = [((outbound,), ""), ((returned,), wrong)]
        with patch.object(source, "_read_page", side_effect=pages):
            with self.assertRaisesRegex(Exception, "Selected outbound echo was not proven"):
                source.fetch(query)

    def test_public_transport_is_get_only_and_disables_automatic_typical(self) -> None:
        source = _source()
        self.assertFalse(source.automatic_typical)
        query = FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)
        good = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")

        class GetOnly:
            def __init__(self):
                self.urls = []

            def get(self, url, *, timeout):
                self.urls.append(url)
                return SweepHttpResponse(200, _context(), url)

        client = GetOnly()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client)
        with patch("viajante.google_flights_public.parse_shopping_page", return_value=(good,)):
            cards, days = source.fetch_with_calendar(query, OUT, BACK)
        self.assertEqual(cards, (good,))
        self.assertEqual(days, ())
        self.assertEqual(len(client.urls), 1)
        self.assertIn("/travel/flights?", client.urls[0])
        self.assertFalse(hasattr(client, "post"))

    def test_serial_get_stops_after_429_and_marks_pending_request_unsent(self) -> None:
        class SerialClient:
            def __init__(self):
                self.urls = []

            def get(self, url, *, timeout):
                self.urls.append(url)
                return SweepHttpResponse(429, "", url)

        source = PublicGoogleFlightsHttpSource(currency="EUR", client=SerialClient())
        urls = [
            "https://www.google.com/travel/flights?a=1",
            "https://www.google.com/travel/flights?a=2",
        ]
        result = source._serial_gets(source._injected_client, urls)
        self.assertEqual(len(source._injected_client.urls), 1)
        self.assertEqual(len(result), 2)
        pending = result[1]
        self.assertEqual(pending.status, 0)
        self.assertFalse(pending.request_sent)
        self.assertEqual(pending.attempts, 0)
        self.assertTrue(pending.stopped)


if __name__ == "__main__":
    unittest.main()


class PublicContextTests(unittest.TestCase):
    def test_context_requires_exact_currency_party_and_cabin(self):
        source = _source()
        query = FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)
        source._verify_context(_context(), query)
        for html in (
            _context(currency="USD"),
            _context(adults=1),
            _context(cabin="Business"),
            "<html></html>",
        ):
            with self.subTest(html=html), self.assertRaisesRegex(Exception, "did not echo"):
                source._verify_context(html, query)

    def test_selected_echo_normalizes_provider_unicode_spaces(self):
        outbound = _leg("HAN", "SIN", OUT, "09:35", "TA101")
        html = (
            "<body>Search results HAN–SIN Sat, Nov 14 9:35\u202fAM–2:10\u202fPM "
            "Choose return round trip</body>"
        )
        self.assertTrue(_source()._selected_echo(html, outbound))

    def test_second_blocking_response_reports_two_attempts(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def get(self, url, *, timeout):
                self.calls += 1
                return SweepHttpResponse(503 if self.calls == 1 else 429, "", url)

        client = Client()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client, sleep=lambda _: None)
        query = FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)
        with self.assertRaises(Exception) as caught:
            source.fetch(query)
        self.assertEqual(caught.exception.diagnostics["attempts"], 2)
        self.assertEqual(client.calls, 2)


class PublicCalendarIntegrationTests(unittest.TestCase):
    def _responses(self, start: date):
        return (
            SweepHttpResponse(200, _context(), "https://www.google.com/travel/flights"),
            SweepHttpResponse(429, "slow down", "https://www.google.com/travel/flights"),
            SweepHttpResponse(
                0,
                "Not sent. Remaining batch stopped after a provider block.",
                "https://www.google.com/travel/flights",
                request_sent=False,
                attempts=0,
                stopped=True,
            ),
        )

    def test_search_dates_keeps_priced_day_and_reports_stopped_days_as_unknown(self):
        start = date(2026, 11, 14)
        card = _card("HAN", "SIN", start, "08:00", "TA101", "€41")

        class GetManyOnly:
            def __init__(self):
                self.calls = 0
                self.sent_urls = []

            def get_many(self, urls, *, timeout):
                self.calls += 1
                self.sent_urls.extend(urls[:2])
                return self_outer._responses(start)

            def get(self, *args, **kwargs):
                raise AssertionError("a stopped calendar batch must not start a fresh GET")

        self_outer = self
        client = GetManyOnly()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client)
        with patch("viajante.google_flights_public.parse_shopping_page", return_value=(card,)):
            report = search_dates(
                "HAN",
                "SIN",
                start,
                date(2026, 11, 16),
                adults=2,
                max_stops=0,
                currency="EUR",
                source=source,
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(client.sent_urls), 2)
        self.assertEqual(report.days[0].price, 41)
        self.assertEqual(report.days[0].status, "ok")
        self.assertEqual([row.status for row in report.days], ["ok", "error", "error"])
        self.assertTrue(all(row.error is not None for row in report.days[1:]))
        self.assertTrue(all(row.error.code.value == "blocked" for row in report.days[1:]))

    def test_search_flex_keeps_priced_grid_without_a_post_block_shop(self):
        around = date(2026, 11, 15)
        card = _card("HAN", "SIN", date(2026, 11, 14), "08:00", "TA101", "€41")

        class GetManyOnly:
            def __init__(self):
                self.calls = 0
                self.sent_urls = []

            def get_many(self, urls, *, timeout):
                self.calls += 1
                self.sent_urls.extend(urls[:2])
                return self_outer._responses(date(2026, 11, 14))

            def get(self, *args, **kwargs):
                raise AssertionError("flex must not start a fresh shop after the 429")

        self_outer = self
        client = GetManyOnly()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client)
        with patch("viajante.google_flights_public.parse_shopping_page", return_value=(card,)):
            report = search_flex(
                "HAN",
                "SIN",
                around,
                1,
                adults=2,
                max_stops=0,
                currency="EUR",
                source=source,
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(client.sent_urls), 2)
        self.assertEqual(len(report.days), 3)
        self.assertEqual(report.days[0].price, 41)
        self.assertTrue(all(row.error is not None for row in report.days[1:]))
        self.assertIsNone(report.chosen_date)
        self.assertEqual(report.offers, ())

    def test_context_mismatch_is_unknown_not_provider_empty_in_dates(self):
        start = date(2026, 11, 14)
        card = _card("HAN", "SIN", start, "08:00", "TA101", "€41")

        class WrongContext:
            def get_many(self, urls, *, timeout):
                return [SweepHttpResponse(200, _context(currency="USD"), urls[0])]

        source = PublicGoogleFlightsHttpSource(currency="EUR", client=WrongContext())
        with patch("viajante.google_flights_public.parse_shopping_page", return_value=(card,)):
            report = search_dates(
                "HAN",
                "SIN",
                start,
                start,
                adults=2,
                max_stops=0,
                currency="EUR",
                source=source,
            )
        self.assertEqual(report.days[0].status, "error")
        self.assertEqual(report.days[0].error.code.value, "markup_drift")
        self.assertIsNone(report.days[0].price)


class PublicPartialRoundTripTests(unittest.TestCase):
    def test_deadline_after_first_package_keeps_it_and_records_partial_metadata(self):
        source = _source()
        query = RoundTrip("HAN", "SIN", OUT, BACK, adults=2, max_stops=0)
        first_out = _card("HAN", "SIN", OUT, "08:00", "TA101", "€100")
        second_out = _card("HAN", "SIN", OUT, "10:00", "TA103", "€110")
        returned = _card("SIN", "HAN", BACK, "09:00", "TA202", "€340")
        echo = "<div>HAN–SIN 8:00 AM Choose return round trip</div>"
        pages = [
            ((first_out, second_out), "initial board"),
            ((returned,), echo),
        ]
        checks = 0

        def deadline_on_second_outbound():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise SearchDeadline()

        with (
            patch.object(source, "_read_page", side_effect=pages) as read,
            patch(
                "viajante.google_flights_public.checkpoint",
                side_effect=deadline_on_second_outbound,
            ),
        ):
            offers = source.fetch(query)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].price, "€340")
        self.assertEqual(len(offers[0].legs), 2)
        partial_error, scope_bound = source.metadata_for(query)
        self.assertIsInstance(partial_error, SearchDeadline)
        self.assertTrue(scope_bound)

    def test_cancellation_base_exception_propagates_before_any_get(self):
        class NoNetwork:
            def __init__(self):
                self.calls = 0

            def get(self, url, *, timeout):
                self.calls += 1
                raise AssertionError("cancelled before request")

        client = NoNetwork()
        source = PublicGoogleFlightsHttpSource(currency="EUR", client=client)
        query = FlightQuery("HAN", "SIN", OUT, adults=2, max_stops=0)
        with (
            patch("viajante.google_flights_public.checkpoint", side_effect=SearchCancelled()),
            self.assertRaises(SearchCancelled),
        ):
            source.fetch(query)
        self.assertEqual(client.calls, 0)
