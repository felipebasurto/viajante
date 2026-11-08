"""Google-side flight options: the bounds sent to the public page, the echo that proves them,
the local clock checks that stay exact, and the refusals for options the page cannot echo."""

from __future__ import annotations

import contextlib
import io
import unittest
from dataclasses import dataclass
from datetime import date
from typing import Any
from unittest.mock import patch

import _isolate  # noqa: F401
from test_flights import card
from viajante.cli import main
from viajante.flight_filters import NO_OFFER_FILTERS, parse_offer_filters
from viajante.flight_offers import _normalize_offer
from viajante.flight_provider import provider_options
from viajante.flights import _overlay_provider_options, search_flights
from viajante.google_flights import GoogleFlightsMarkupError
from viajante.google_flights_public import _verify_provider_echo
from viajante.mcp_handlers import search_flights_tool
from viajante.models import FlightQuery, ProviderShop, RoundTrip
from viajante.models_flights import RELAX_ORDER, Requirements
from viajante.tfs import encode_tfs

DAY = date(2026, 12, 4)
HIDE_CHIP = "Hide separate & self-transfer tickets, Separate & self-transfer tickets, Selected"


def _query(**kwargs: Any) -> FlightQuery:
    return FlightQuery("MAD", "BCN", DAY, max_stops=1, **kwargs)


class ProviderOptionsTest(unittest.TestCase):
    def test_clock_bounds_round_outward_to_the_whole_hour(self) -> None:
        # 10:30 keeps the 10:xx bucket; 18:30 keeps through 18:59; 08:00 and 21:45 likewise.
        filters = parse_offer_filters(
            depart_after=630, depart_before=1110, arrive_after=480, arrive_before=1305
        )
        shop = provider_options(_query(), filters)
        self.assertEqual(shop.earliest_departure_hour, 10)
        self.assertEqual(shop.latest_departure_hour, 18)
        self.assertEqual(shop.earliest_arrival_hour, 8)
        self.assertEqual(shop.latest_arrival_hour, 21)

    def test_window_and_named_bounds_keep_the_tightest_side(self) -> None:
        # The local check needs both; the page bound must not loosen either one.
        filters = parse_offer_filters(
            depart_window=(360, 1259), depart_after=600, depart_before=1200
        )
        shop = provider_options(_query(), filters)
        self.assertEqual(shop.earliest_departure_hour, 10)
        self.assertEqual(shop.latest_departure_hour, 20)

    def test_duration_and_layover_round_up_to_whole_minutes(self) -> None:
        filters = parse_offer_filters(max_duration_hours=2.5, max_layover_hours=1.01)
        shop = provider_options(_query(), filters)
        self.assertEqual(shop.max_duration_minutes, 150)
        self.assertEqual(shop.max_layover_minutes, 61)

    def test_price_cap_and_echo_only_flags_are_carried(self) -> None:
        shop = provider_options(
            _query(price_cap=400),
            NO_OFFER_FILTERS,
            less_emissions=True,
            hide_separate_tickets=True,
        )
        self.assertEqual(shop.max_price, 400)
        self.assertTrue(shop.less_emissions)
        self.assertTrue(shop.hide_separate_tickets)

    def test_nothing_named_pushes_nothing(self) -> None:
        self.assertIsNone(provider_options(_query(), NO_OFFER_FILTERS))

    def test_min_layover_and_via_stay_local(self) -> None:
        # Neither is echoed by the public page, so neither reaches the request.
        filters = parse_offer_filters(min_layover_hours=0.5, via=("LHR",))
        self.assertIsNone(provider_options(_query(), filters))

    def test_json_key_appears_only_when_something_is_sent(self) -> None:
        self.assertNotIn("google_filters", _query().to_dict())
        sent = _query(provider=ProviderShop(max_price=400)).to_dict()
        self.assertEqual(sent["google_filters"], {"max_price": 400})


class ProviderEncodingTest(unittest.TestCase):
    def test_golden_matches_the_verified_field_numbers(self) -> None:
        # Decoded by hand against the field map: 8/9/10/11 clocks, 12 duration (flight)
        # and price (info), 18 max layover, 19 less-emissions, 17 hide-separate, 19 one-way.
        query = _query(
            provider=ProviderShop(
                max_price=400,
                earliest_departure_hour=8,
                latest_departure_hour=18,
                latest_arrival_hour=22,
                max_duration_minutes=150,
                max_layover_minutes=120,
                less_emissions=True,
                hide_separate_tickets=True,
            )
        )
        self.assertEqual(
            encode_tfs(query),
            "GiwSCjIwMjYtMTItMDQoAUAISBJYFmCWAWoFEgNNQURyBRIDQkNOkAF4mgEBAUIBAUgBYJADiAEBmAEC",
        )

    def test_unset_provider_leaves_the_request_unchanged(self) -> None:
        self.assertEqual(encode_tfs(_query()), encode_tfs(_query(provider=None)))


class EchoVerificationTest(unittest.TestCase):
    ECHOED = {
        "Times, Selected",
        "up to €400, Price, Selected",
        "Under 3 hr, Duration, Selected",
        "Layover: Max 2 hr, Connecting airports, Selected",
        "Less emissions, Emissions, Selected",
        HIDE_CHIP,
        None,
    }
    PROVIDER = ProviderShop(
        max_price=400,
        latest_departure_hour=18,
        max_duration_minutes=150,
        max_layover_minutes=120,
        less_emissions=True,
        hide_separate_tickets=True,
    )

    def test_every_echoed_option_passes(self) -> None:
        _verify_provider_echo(set(self.ECHOED), self.PROVIDER)

    def test_unrecognised_active_provider_option_fails_closed(self) -> None:
        @dataclass(frozen=True)
        class FutureOption(ProviderShop):
            new_filter: bool = True

        with self.assertRaisesRegex(GoogleFlightsMarkupError, "new_filter"):
            _verify_provider_echo(set(self.ECHOED), FutureOption())

    def test_missing_time_chip_is_markup_drift(self) -> None:
        labels = self.ECHOED - {"Times, Selected"}
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "time filter"):
            _verify_provider_echo(labels, self.PROVIDER)

    def test_price_echo_below_the_request_is_markup_drift(self) -> None:
        labels = (self.ECHOED - {"up to €400, Price, Selected"}) | {"up to €300, Price, Selected"}
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "price cap"):
            _verify_provider_echo(labels, self.PROVIDER)

    def test_tighter_duration_echo_is_markup_drift(self) -> None:
        labels = (self.ECHOED - {"Under 3 hr, Duration, Selected"}) | {
            "Under 2 hr, Duration, Selected"
        }
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "duration"):
            _verify_provider_echo(labels, self.PROVIDER)

    def test_looser_layover_echo_passes_and_missing_one_fails(self) -> None:
        looser = (self.ECHOED - {"Layover: Max 2 hr, Connecting airports, Selected"}) | {
            "Layover: Max 1 hr 30 min, Connecting airports, Selected"
        }
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "layover"):
            _verify_provider_echo(looser, self.PROVIDER)
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "layover"):
            _verify_provider_echo(
                self.ECHOED - {"Layover: Max 2 hr, Connecting airports, Selected"}, self.PROVIDER
            )

    def test_missing_emissions_or_separate_ticket_chip_is_markup_drift(self) -> None:
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "emissions"):
            _verify_provider_echo(
                self.ECHOED - {"Less emissions, Emissions, Selected"}, self.PROVIDER
            )
        with self.assertRaisesRegex(GoogleFlightsMarkupError, "separate-ticket"):
            _verify_provider_echo(
                self.ECHOED - {HIDE_CHIP},
                self.PROVIDER,
            )


class RefusalTest(unittest.TestCase):
    def test_basic_economy_exclusion_is_refused_before_any_fetch(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclude_basic_economy is refused"):
            search_flights([_query()], exclude_basic_economy=True)

    def test_echo_only_flags_refused_on_detail(self) -> None:
        with self.assertRaisesRegex(ValueError, "fetch 'detail' cannot apply"):
            search_flights([_query()], fetch="detail", less_emissions=True)

    def test_echo_only_flags_refused_on_round_trip(self) -> None:
        round_trip = RoundTrip("MAD", "BCN", DAY, date(2026, 12, 10))
        with self.assertRaisesRegex(ValueError, "apply only to one-way"):
            search_flights([round_trip], hide_separate_tickets=True)

    def test_mcp_handler_refuses_basic_economy_exclusion(self) -> None:
        with self.assertRaisesRegex(ValueError, "exclude_basic_economy is refused"):
            search_flights_tool(["MAD-BCN:2026-12-04"], currency="EUR", exclude_basic_economy=True)

    def test_cli_reports_the_refusal_as_an_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(
                ["flights", "MAD-BCN:2026-12-04", "--currency", "EUR", "--exclude-basic-economy"]
            )
        self.assertEqual(code, 1)
        self.assertIn("exclude_basic_economy is refused", stderr.getvalue())


class SurfaceWiringTest(unittest.TestCase):
    """The CLI and MCP flags reach search_flights as the same keyword arguments."""

    class _Stop(Exception):
        pass

    def test_cli_flags_reach_search_flights(self) -> None:
        with patch("viajante.cli.search_flights", side_effect=self._Stop()) as search:
            with self.assertRaises(self._Stop):
                main(
                    [
                        "flights",
                        "MAD-BCN:2026-12-04",
                        "--currency",
                        "EUR",
                        "--depart-before",
                        "18:30",
                        "--arrive-after",
                        "08:00",
                        "--less-emissions",
                        "--hide-separate-tickets",
                    ]
                )
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["depart_before"], 18 * 60 + 30)
        self.assertEqual(kwargs["arrive_after"], 8 * 60)
        self.assertTrue(kwargs["less_emissions"])
        self.assertTrue(kwargs["hide_separate_tickets"])
        self.assertFalse(kwargs["exclude_basic_economy"])

    def test_mcp_arguments_reach_search_flights(self) -> None:
        with patch("viajante.mcp_handlers.search_flights", side_effect=self._Stop()) as search:
            with self.assertRaises(self._Stop):
                search_flights_tool(
                    ["MAD-BCN:2026-12-04"],
                    currency="EUR",
                    depart_before="18:30",
                    arrive_after="08:00",
                    less_emissions=True,
                    hide_separate_tickets=True,
                )
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["depart_before"], 18 * 60 + 30)
        self.assertEqual(kwargs["arrive_after"], 8 * 60)
        self.assertTrue(kwargs["less_emissions"])
        self.assertTrue(kwargs["hide_separate_tickets"])


class OverlayTest(unittest.TestCase):
    def test_one_way_gets_the_options_and_detail_keeps_them_local(self) -> None:
        filters = parse_offer_filters(depart_before=1080)
        (sent,) = _overlay_provider_options(
            [_query()], filters, planned="sweep", less_emissions=True, hide_separate_tickets=False
        )
        self.assertEqual(sent.provider.latest_departure_hour, 18)
        self.assertTrue(sent.provider.less_emissions)
        (local,) = _overlay_provider_options(
            [_query()], filters, planned="detail", less_emissions=False, hide_separate_tickets=False
        )
        self.assertIsNone(local.provider)


class LocalClockTest(unittest.TestCase):
    def test_depart_before_is_an_inclusive_local_bound(self) -> None:
        filters = parse_offer_filters(depart_before=18 * 60)
        self.assertIsNotNone(_normalize_offer(card(departure="18:00"), 1, **vars(filters)))
        self.assertIsNone(_normalize_offer(card(departure="18:01"), 1, **vars(filters)))

    def test_arrive_after_is_an_inclusive_local_bound(self) -> None:
        filters = parse_offer_filters(arrive_after=8 * 60)
        self.assertIsNotNone(_normalize_offer(card(arrival="08:00"), 1, **vars(filters)))
        self.assertIsNone(_normalize_offer(card(arrival="07:59"), 1, **vars(filters)))

    def test_new_clock_bounds_are_relaxable_requirements(self) -> None:
        requirements = Requirements(depart_before=18 * 60, arrive_after=8 * 60)
        self.assertEqual(
            requirements.to_dict(), {"arrive_after": "08:00", "depart_before": "18:00"}
        )
        self.assertIn("depart_before", RELAX_ORDER)
        self.assertIn("arrive_after", RELAX_ORDER)


if __name__ == "__main__":
    unittest.main()
