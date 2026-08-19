from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import viajante
from viajante.cli import _format_clock, _join_cancellation_rows, _print_report, main
from viajante.hotels import write_hotel_report_atomic
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
    RawJourneyLeg,
    RoundTrip,
    SearchError,
    SearchErrorCode,
    SearchReport,
)

FUTURE_DATE = date.today() + timedelta(days=30)
PAST_DATE = date.today() - timedelta(days=1)
ROUTE = f"MAD-BCN:{FUTURE_DATE.isoformat()}"

QUERY = FlightQuery("MAD", "BCN", FUTURE_DATE, max_stops=1)
SEARCHED_AT = datetime(2026, 8, 10, 9, 0, 0)


def _offer(
    *,
    airline: Optional[str] = "Example Air",
    departure: Optional[str] = "08:40",
    arrival: Optional[str] = "11:30",
    duration: Optional[str] = "2 h 50 min",
    duration_hours: Optional[float] = 2.8333333333333335,
    stops: Optional[str] = "Nonstop",
    stops_count: Optional[int] = 0,
    price_eur: float = 129.0,
    baggage_buffer_eur: int = 0,
    needs_bag_verify: bool = False,
    layover_city: Optional[str] = None,
    layover_hours: Optional[float] = None,
    booking_token: Optional[str] = None,
) -> FlightOffer:
    return FlightOffer(
        airline=airline,
        departure=departure,
        arrival=arrival,
        price=f"€{price_eur:.0f}",
        price_eur=price_eur,
        duration=duration,
        duration_hours=duration_hours,
        stops=stops,
        stops_count=stops_count,
        layover_city=layover_city,
        layover_hours=layover_hours,
        baggage_buffer_eur=baggage_buffer_eur,
        needs_bag_verify=needs_bag_verify,
        booking_token=booking_token,
    )


def _report(*offers: FlightOffer) -> SearchReport:
    shown = offers or (_offer(),)
    return SearchReport(
        searched_at=SEARCHED_AT,
        queries=(
            QuerySuccess(
                query=QUERY,
                raw_count=3,
                eligible_count=len(shown),
                offers=shown,
            ),
        ),
    )


def _rendered(report: SearchReport, *, sort: str = "ranked") -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_report(report, sort=sort)
    return buffer.getvalue()


class CliTests(unittest.TestCase):
    def test_validation_before_search(self) -> None:
        with patch("viajante.cli.search_flights") as search:
            code = main(["flights", "BADROUTE"])
            self.assertEqual(code, 1)
            search.assert_not_called()

    def test_invalid_max_stops(self) -> None:
        with patch("viajante.cli.search_flights") as search:
            with redirect_stdout(io.StringIO()):
                code = main(["flights", ROUTE, "--max-stops", "3"])
            self.assertEqual(code, 1)
            search.assert_not_called()

    def test_max_stops_two_reaches_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", ROUTE, "--max-stops", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.args[0][0].max_stops, 2)

    def test_prints_results(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()):
            with patch("viajante.cli._print_report") as printer:
                code = main(["flights", ROUTE])
                self.assertEqual(code, 0)
                printer.assert_called_once()

    def test_save_only_when_requested(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()):
            with patch("viajante.cli.write_report_atomic") as writer:
                with patch("viajante.cli._print_report"):
                    main(["flights", ROUTE])
                writer.assert_not_called()

    def test_atomic_save(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()):
            with patch("viajante.cli._print_report"), redirect_stdout(io.StringIO()):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.json"
                    code = main(["flights", ROUTE, "--save", str(out)])
                    self.assertEqual(code, 0)
                    self.assertTrue(out.exists())
                    self.assertFalse(out.with_suffix(".json.tmp").exists())
                    data = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(data["schema_version"], 1)

    def test_failed_search_returns_nonzero(self) -> None:
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(
                QueryFailure(
                    query=QUERY,
                    error=SearchError(
                        SearchErrorCode.FETCH_FAILED,
                        "Google Flights search failed after 3 attempts.",
                    ),
                ),
            ),
        )
        with patch("viajante.cli.search_flights", return_value=report):
            with patch("viajante.cli._print_report"):
                self.assertEqual(main(["flights", ROUTE]), 2)

    def test_partial_failure_returns_three(self) -> None:
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(
                QuerySuccess(query=QUERY, raw_count=3, eligible_count=1, offers=(_offer(),)),
                QueryFailure(
                    query=QUERY,
                    error=SearchError(SearchErrorCode.FETCH_FAILED, "boom"),
                ),
            ),
        )
        with patch("viajante.cli.search_flights", return_value=report):
            with patch("viajante.cli._print_report"):
                self.assertEqual(main(["flights", ROUTE]), 3)

    def test_baggage_buffer_flag_reaches_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", ROUTE, "--baggage-buffer", "0"])
        self.assertEqual(search.call_args.kwargs["buffer_eur"], 0)

    def test_adults_and_cabin_reach_parsed_queries(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", ROUTE, "--adults", "2", "--cabin", "business"])
        self.assertEqual(code, 0)
        queries = search.call_args.args[0]
        self.assertEqual(queries[0].adults, 2)
        self.assertEqual(queries[0].cabin, "business")

    def test_zero_adults_is_rejected_before_searching(self) -> None:
        with patch("viajante.cli.search_flights") as search:
            self.assertEqual(main(["flights", ROUTE, "--adults", "0"]), 1)
            search.assert_not_called()

    def test_negative_baggage_buffer_is_rejected_before_searching(self) -> None:
        with patch("viajante.cli.search_flights") as search:
            self.assertEqual(main(["flights", ROUTE, "--baggage-buffer", "-1"]), 1)
            search.assert_not_called()

    def test_past_dates_are_rejected_before_starting_chromium(self) -> None:
        with patch("viajante.cli.search_flights") as search:
            code = main(["flights", f"MAD-BCN:{PAST_DATE.isoformat()}"])
            self.assertEqual(code, 1)
            search.assert_not_called()

    def test_today_is_accepted(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", f"MAD-BCN:{date.today().isoformat()}"])
            search.assert_called_once()


class ReportRenderingTests(unittest.TestCase):
    def test_missing_fields_never_render_as_none(self) -> None:
        blank = _offer(
            airline=None,
            departure=None,
            arrival=None,
            duration=None,
            duration_hours=None,
            stops=None,
            stops_count=None,
        )
        output = _rendered(_report(blank))
        self.assertNotIn("None", output)
        self.assertIn("? -> ?", output)

    def test_stops_are_shown(self) -> None:
        output = _rendered(_report(_offer(stops_count=0), _offer(stops_count=2)))
        self.assertIn("direct", output)
        self.assertIn("2 stops", output)

    def test_ranking_note_shows_the_effective_total(self) -> None:
        low_cost = _offer(
            airline="Ryanair", price_eur=50.0, baggage_buffer_eur=70, needs_bag_verify=True
        )
        output = _rendered(_report(low_cost))
        self.assertIn("50 €", output)
        self.assertIn("120 € ranked", output)
        self.assertNotIn("(+70 bag", output)

    def test_disabled_buffer_still_flags_the_carrier(self) -> None:
        low_cost = _offer(
            airline="Ryanair", price_eur=50.0, baggage_buffer_eur=0, needs_bag_verify=True
        )
        output = _rendered(_report(low_cost))
        self.assertIn("[baggage?]", output)
        self.assertNotIn("ranked", output)

    def test_baggage_reminder_after_successful_flight_results(self) -> None:
        output = _rendered(_report(_offer()))
        self.assertIn("Verify checked baggage on Google Flights before booking.", output)

    def test_baggage_reminder_omitted_when_all_queries_fail(self) -> None:
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(
                QueryFailure(
                    query=QUERY,
                    error=SearchError(SearchErrorCode.FETCH_FAILED, "blocked"),
                ),
            ),
        )
        output = _rendered(report)
        self.assertNotIn("Verify checked baggage", output)
        self.assertIn("ERROR: blocked", output)

    def test_long_airline_names_are_truncated_visibly(self) -> None:
        output = _rendered(_report(_offer(airline="A" * 60)))
        self.assertIn("…", output)
        self.assertNotIn("A" * 41, output)

    def test_booking_token_prints_a_google_flights_url(self) -> None:
        output = _rendered(_report(_offer(booking_token="tok")))
        self.assertIn("https://www.google.com/travel/flights", output)
        self.assertIn("booking_token=tok", output)
        self.assertNotIn("https://www.google.com/travel/flights", _rendered(_report(_offer())))

    def test_flight_success_prints_eligible_counts(self) -> None:
        output = _rendered(_report(_offer()))
        self.assertIn("Raw: 3; eligible: 1; shown: 1", output)

    def test_empty_eligible_still_prints_counts(self) -> None:
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(QuerySuccess(query=QUERY, raw_count=5, eligible_count=0, offers=()),),
        )
        output = _rendered(report)
        self.assertIn("(no eligible offers)", output)
        self.assertIn("Raw: 5; eligible: 0; shown: 0", output)
        self.assertIn("Verify checked baggage", output)

    def test_clock_strips_weekday_tail(self) -> None:
        self.assertEqual(_format_clock("10:35 AM on Fri, Oct 9"), "10:35 AM")
        self.assertEqual(_format_clock("1:10 PM on Sat, Oct 10"), "1:10 PM")
        output = _rendered(
            _report(
                _offer(
                    departure="10:35 AM on Fri, Oct 9",
                    arrival="1:10 PM on Sat, Oct 10",
                )
            )
        )
        self.assertIn("10:35 AM -> 1:10 PM", output)
        self.assertNotIn("on Fri", output)

    def test_sort_flag_reaches_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", ROUTE, "--sort", "fare"])
        self.assertEqual(search.call_args.kwargs["sort"], "fare")

    def test_max_layover_flag_reaches_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", ROUTE, "--max-layover", "10"])
        self.assertEqual(search.call_args.kwargs["max_layover_hours"], 10)

    def test_duration_and_min_layover_flags_reach_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(
                    [
                        "flights",
                        ROUTE,
                        "--max-duration",
                        "4",
                        "--min-layover",
                        "1",
                    ]
                )
        self.assertEqual(search.call_args.kwargs["max_duration_hours"], 4)
        self.assertEqual(search.call_args.kwargs["min_layover_hours"], 1)

    def test_layover_is_visible_on_one_stop_rows(self) -> None:
        output = _rendered(
            _report(
                _offer(
                    airline="Tap Air Portugal",
                    departure="13:40",
                    arrival="09:00",
                    duration="20 hr 20 min",
                    duration_hours=20 + 20 / 60,
                    stops="1 stop",
                    stops_count=1,
                    price_eur=74.0,
                    layover_city="Lisbon",
                    layover_hours=18.0,
                )
            )
        )
        self.assertIn("13:40 -> 09:00", output)
        self.assertIn("Lisbon", output)
        self.assertIn("18h", output)

    def test_round_trip_header_and_return_clocks(self) -> None:
        trip = RoundTrip("MAD", "PRG", date(2026, 12, 3), date(2026, 12, 9))
        offer = _offer(departure="07:00", arrival="09:30")
        object.__setattr__(
            offer,
            "legs",
            (
                RawJourneyLeg(departure="07:00", arrival="09:30", duration="2 hr 30 min"),
                RawJourneyLeg(departure="14:00", arrival="16:20", duration="2 hr 20 min"),
            ),
        )
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(QuerySuccess(query=trip, raw_count=1, eligible_count=1, offers=(offer,)),),
        )
        output = _rendered(report)
        self.assertIn("2026-12-03 / 2026-12-09", output)
        self.assertIn("round-trip", output)
        self.assertIn("07:00 -> 09:30", output)
        self.assertIn("return", output)
        self.assertIn("14:00 -> 16:20", output)

    def test_filter_flags_reach_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(
                    [
                        "flights",
                        ROUTE,
                        "--airlines",
                        "IB,I2",
                        "--exclude-airlines",
                        "FR,RK",
                        "--depart-window",
                        "7-12",
                        "--sort",
                        "duration",
                    ]
                )
        self.assertEqual(search.call_args.kwargs["airlines"], ("IB", "I2"))
        self.assertEqual(search.call_args.kwargs["exclude_airlines"], ("FR", "RK"))
        self.assertEqual(search.call_args.kwargs["depart_window"], (7, 12))
        self.assertEqual(search.call_args.kwargs["sort"], "duration")

    def test_fetch_flag_reaches_the_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", ROUTE, "--fetch", "sweep"])
        self.assertEqual(search.call_args.kwargs["fetch"], "sweep")

    def test_fetch_defaults_to_auto(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                main(["flights", ROUTE])
        self.assertEqual(search.call_args.kwargs["fetch"], "auto")

    def test_trip_rt_and_multi_reject_bad_grammar(self) -> None:
        cases = (
            ["flights", "--trip", "rt", "MAD-BCN:2026-09-01"],
            ["flights", "--trip", "multi", "MAD-BCN:2026-09-01"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with patch("viajante.cli.search_flights") as search:
                    with redirect_stderr(stderr):
                        code = main(argv)
                self.assertEqual(code, 1)
                self.assertTrue(stderr.getvalue().startswith("error:"))
                search.assert_not_called()

    def test_trip_round_trip_alias_reaches_search(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(
                    [
                        "flights",
                        "--trip",
                        "round-trip",
                        "MAD-OPO:2026-10-09:2026-10-12",
                        "--fetch",
                        "sweep",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(type(search.call_args.args[0][0]).__name__, "RoundTrip")

    def test_trip_rt_reaches_search_as_one_package(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(
                    ["flights", "--trip", "rt", "MAD-OPO:2026-10-09:2026-10-12", "--fetch", "sweep"]
                )
        self.assertEqual(code, 0)
        trips = search.call_args.args[0]
        self.assertEqual(len(trips), 1)
        self.assertEqual(type(trips[0]).__name__, "RoundTrip")

    def test_trip_one_way_keeps_rt_sugar(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", "--trip", "one-way", "MAD-OPO:2026-10-09:2026-10-12"])
        self.assertEqual(code, 0)
        self.assertEqual(len(search.call_args.args[0]), 2)

    def test_rt_sugar_builds_return_leg(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report()) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", "MAD-OPO:2026-10-09:2026-10-12"])
        self.assertEqual(code, 0)
        queries = search.call_args.args[0]
        self.assertEqual(len(queries), 2)
        self.assertEqual(queries[0].origin, "MAD")
        self.assertEqual(queries[0].destination, "OPO")
        self.assertEqual(queries[0].departure_date, date(2026, 10, 9))
        self.assertEqual(queries[1].origin, "OPO")
        self.assertEqual(queries[1].destination, "MAD")
        self.assertEqual(queries[1].departure_date, date(2026, 10, 12))

    def test_best_pair_line_uses_sort_key(self) -> None:
        outbound = FlightQuery("MAD", "OPO", date(2026, 10, 9), max_stops=1)
        inbound = FlightQuery("OPO", "MAD", date(2026, 10, 12), max_stops=1)
        report = SearchReport(
            searched_at=SEARCHED_AT,
            queries=(
                QuerySuccess(
                    query=outbound,
                    raw_count=1,
                    eligible_count=1,
                    offers=(
                        _offer(
                            airline="Ryanair",
                            price_eur=75.0,
                            baggage_buffer_eur=70,
                            needs_bag_verify=True,
                        ),
                    ),
                ),
                QuerySuccess(
                    query=inbound,
                    raw_count=1,
                    eligible_count=1,
                    offers=(_offer(airline="TAP", price_eur=80.0),),
                ),
            ),
        )
        ranked = _rendered(report, sort="ranked")
        self.assertIn("Best pair (ranked):", ranked)
        self.assertIn("MAD->OPO 145 € ranked", ranked)
        self.assertIn("OPO->MAD 80 € ranked", ranked)
        self.assertIn("= 225 €", ranked)
        fare = _rendered(report, sort="fare")
        self.assertIn("Best pair (fare):", fare)
        self.assertIn("MAD->OPO 75 € fare", fare)
        self.assertIn("= 155 €", fare)

    def test_flights_help_preserves_examples_epilog(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["flights", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("Examples:", help_text)
        self.assertIn("viajante flights JFK-LHR", help_text)
        self.assertIn("JFK-NRT:2026-10-09:2026-10-20", help_text)
        self.assertIn("--trip", help_text)
        self.assertIn("--sort", help_text)
        self.assertIn("--fetch", help_text)
        self.assertIn("--fetch sweep", help_text)
        self.assertIn("--max-layover", help_text)
        self.assertIn("--min-layover", help_text)
        self.assertIn("--max-duration", help_text)
        self.assertIn("--airlines", help_text)
        self.assertIn("--exclude-airlines", help_text)
        self.assertIn("--depart-window", help_text)
        self.assertIn("duration", help_text)

    def test_root_help_preserves_flight_examples_and_lists_subcommands(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("flights", help_text)
        self.assertIn("hotels", help_text)
        self.assertIn("dates", help_text)
        self.assertIn("explore", help_text)
        self.assertIn("airports", help_text)
        self.assertIn("bench", help_text)
        self.assertIn("Examples:", help_text)
        self.assertIn("viajante flights JFK-LHR", help_text)


def _sample_hotel_report(
    *,
    offers: tuple[HotelOffer, ...] = (),
    raw_count: int = 5,
    eligible_count: int = 3,
) -> HotelSearchReport:
    query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7))
    applied = AppliedHotelFilters(chips=("oos=1",), url="https://example.test")
    return HotelSearchReport(
        searched_at=datetime(2026, 8, 10, 9, 0, 0),
        queries=(
            HotelQuerySuccess(
                query=query,
                applied=applied,
                raw_count=raw_count,
                eligible_count=eligible_count,
                offers=offers,
            ),
        ),
    )


def _sample_hotel_offer(*, total_price: str = "420 €") -> HotelOffer:
    return HotelOffer(
        title="Old Town Apartment",
        address="Prague 1, Czech Republic",
        total_price=total_price,
        total_price_eur=420.0,
        rating="8.9",
        rating_score=8.9,
        details="Cancelación gratuita · Apartamento entero",
        cancellation_evidence=CancellationEvidence.FREE,
        property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
        lodging_kind=LodgingKind.ENTIRE_HOME,
        bedrooms=2,
        bathrooms=1,
        beds=2,
        link="https://www.booking.com/hotel/example.html",
    )


class PublicApiTests(unittest.TestCase):
    def test_exports_hotel_api(self) -> None:
        for name in (
            "FlightQuery",
            "SearchReport",
            "search_flights",
            "HotelQuery",
            "HotelSearchReport",
            "CancellationEvidence",
            "PropertyTypeEvidence",
            "search_hotels",
            "search_dates",
            "search_explore",
            "lookup_airports",
        ):
            self.assertTrue(hasattr(viajante, name), msg=name)
        self.assertEqual(
            set(viajante.__all__),
            {
                "CancellationEvidence",
                "DateCalendarReport",
                "ExploreReport",
                "FlightLeg",
                "FlightQuery",
                "HotelQuery",
                "HotelSearchReport",
                "MultiCity",
                "PropertyTypeEvidence",
                "RoundTrip",
                "SearchReport",
                "Trip",
                "lookup_airports",
                "search_dates",
                "search_explore",
                "search_flights",
                "search_hotels",
            },
        )


class HotelCliTests(unittest.TestCase):
    def test_hotels_help_returns_zero_without_search(self) -> None:
        with patch("viajante.cli.search_hotels") as search:
            code = main(["hotels", "--help"])
            self.assertEqual(code, 0)
            search.assert_not_called()

    def test_valid_args_build_exact_hotel_query(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()) as search:
            with patch("viajante.cli._print_hotel_report"):
                code = main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--adults",
                        "2",
                        "--rooms",
                        "1",
                        "--top",
                        "5",
                        "--min-rating",
                        "8.0",
                        "--entire-home",
                    ]
                )
                self.assertEqual(code, 0)
                search.assert_called_once()
                queries, kwargs = search.call_args
                self.assertEqual(kwargs["top"], 5)
                self.assertTrue(callable(kwargs["progress"]))
                self.assertEqual(len(queries[0]), 1)
                query = queries[0][0]
                self.assertEqual(
                    query,
                    HotelQuery(
                        location="Prague",
                        check_in=date(2026, 12, 4),
                        check_out=date(2026, 12, 7),
                        adults=2,
                        rooms=1,
                        min_rating=8.0,
                        entire_home=True,
                        free_cancellation=True,
                    ),
                )

    def test_compare_cancellation_builds_two_queries(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()) as search:
            with patch("viajante.cli._print_hotel_report"):
                code = main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--compare-cancellation",
                    ]
                )
                self.assertEqual(code, 0)
                queries = search.call_args[0][0]
                self.assertEqual(len(queries), 2)
                self.assertTrue(queries[0].free_cancellation)
                self.assertFalse(queries[1].free_cancellation)
                self.assertEqual(queries[0].location, queries[1].location)
                self.assertEqual(queries[0].check_in, queries[1].check_in)

    def test_join_cancellation_rows_matches_by_normalized_identity(self) -> None:
        free = _sample_hotel_offer()
        open_match = HotelOffer(
            title="  Old Town Apartment ",
            address="Prague 1,   Czech Republic",
            total_price="380 €",
            total_price_eur=380.0,
            rating="8.9",
            rating_score=8.9,
            details="No reembolsable",
            cancellation_evidence=CancellationEvidence.NON_REFUNDABLE,
            property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
            lodging_kind=LodgingKind.ENTIRE_HOME,
            bedrooms=2,
            bathrooms=1,
            beds=2,
            link=None,
        )
        open_only = HotelOffer(
            title="Other Stay",
            address="Prague 2",
            total_price="200 €",
            total_price_eur=200.0,
            rating=None,
            rating_score=None,
            details="",
            cancellation_evidence=CancellationEvidence.UNKNOWN,
            property_type_evidence=PropertyTypeEvidence.UNKNOWN,
            lodging_kind=LodgingKind.UNKNOWN,
            bedrooms=None,
            bathrooms=None,
            beds=None,
            link=None,
        )
        rows = _join_cancellation_rows((free,), (open_match, open_only))
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first[0].title, "Other Stay")
        self.assertIsNone(first[1])
        self.assertIsNotNone(first[2])
        matched = rows[1]
        self.assertIsNotNone(matched[1])
        self.assertIsNotNone(matched[2])
        assert matched[2] is not None
        self.assertEqual(matched[2].total_price_eur, 380.0)

    def test_compare_output_prints_join_when_both_succeed(self) -> None:
        free_query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7))
        open_query = HotelQuery(
            "Prague",
            date(2026, 12, 4),
            date(2026, 12, 7),
            free_cancellation=False,
        )
        applied_free = AppliedHotelFilters(chips=("oos=1",), url="https://example.test")
        applied_open = AppliedHotelFilters(chips=(), url="https://example.test")
        free_offer = _sample_hotel_offer()
        open_offer = HotelOffer(
            title="Old Town Apartment",
            address="Prague 1, Czech Republic",
            total_price="380 €",
            total_price_eur=380.0,
            rating="8.9",
            rating_score=8.9,
            details="No reembolsable",
            cancellation_evidence=CancellationEvidence.NON_REFUNDABLE,
            property_type_evidence=PropertyTypeEvidence.ENTIRE_HOME,
            lodging_kind=LodgingKind.ENTIRE_HOME,
            bedrooms=2,
            bathrooms=1,
            beds=2,
            link=None,
        )
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 0, 0),
            queries=(
                HotelQuerySuccess(
                    query=free_query,
                    applied=applied_free,
                    raw_count=1,
                    eligible_count=1,
                    offers=(free_offer,),
                ),
                HotelQuerySuccess(
                    query=open_query,
                    applied=applied_open,
                    raw_count=1,
                    eligible_count=1,
                    offers=(open_offer,),
                ),
            ),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--compare-cancellation",
                    ]
                )
            self.assertEqual(code, 0)
            output = buffer.getvalue().casefold()
            self.assertIn("cancellation compare", output)
            self.assertIn("free cancel 420 €", output)
            self.assertIn("no free cancel 380 €", output)
            self.assertIn("delta 40 €", output)
            self.assertIn("lodging: entire home", output)

    def test_compare_output_skips_join_when_one_query_fails(self) -> None:
        free_query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7))
        open_query = HotelQuery(
            "Prague",
            date(2026, 12, 4),
            date(2026, 12, 7),
            free_cancellation=False,
        )
        applied = AppliedHotelFilters(chips=("oos=1",), url="https://example.test")
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 0, 0),
            queries=(
                HotelQuerySuccess(
                    query=free_query,
                    applied=applied,
                    raw_count=1,
                    eligible_count=1,
                    offers=(_sample_hotel_offer(),),
                ),
                HotelQueryFailure(
                    query=open_query,
                    applied=AppliedHotelFilters(chips=(), url="https://example.test"),
                    error=SearchError(
                        SearchErrorCode.FETCH_FAILED,
                        "Booking.com hotel search failed after 3 attempts.",
                    ),
                ),
            ),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--compare-cancellation",
                    ]
                )
            self.assertEqual(code, 3)
            output = buffer.getvalue().casefold()
            self.assertNotIn("cancellation compare", output)
            self.assertIn("error:", output)

    def test_compare_cancellation_rejects_allow_non_refundable(self) -> None:
        with patch("viajante.cli.search_hotels") as search:
            code = main(
                [
                    "hotels",
                    "Prague",
                    "2026-12-04",
                    "2026-12-07",
                    "--compare-cancellation",
                    "--allow-non-refundable",
                ]
            )
            self.assertEqual(code, 1)
            search.assert_not_called()

    def test_allow_non_refundable_flips_cancellation_only(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()) as search:
            with patch("viajante.cli._print_hotel_report"):
                main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--allow-non-refundable",
                    ]
                )
                query = search.call_args[0][0][0]
                self.assertFalse(query.free_cancellation)
                self.assertFalse(query.entire_home)
                self.assertIsNone(query.min_rating)

    def test_google_source_reaches_search(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()) as search:
            with patch("viajante.cli._print_hotel_report"):
                code = main(["hotels", "Prague", "2026-12-04", "2026-12-07", "--source", "google"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["source"], "google")

    def test_google_source_rejects_compare_cancellation(self) -> None:
        with patch("viajante.cli.search_hotels") as search:
            code = main(
                [
                    "hotels",
                    "Prague",
                    "2026-12-04",
                    "2026-12-07",
                    "--source",
                    "google",
                    "--compare-cancellation",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_google_source_rejects_min_rating_above_five(self) -> None:
        with patch("viajante.cli.search_hotels") as search:
            code = main(
                [
                    "hotels",
                    "Prague",
                    "2026-12-04",
                    "2026-12-07",
                    "--source",
                    "google",
                    "--min-rating",
                    "8.5",
                ]
            )
        self.assertEqual(code, 1)
        search.assert_not_called()

    def test_validation_before_search(self) -> None:
        cases = [
            ["hotels", "Prague", "not-a-date", "2026-12-07"],
            ["hotels", "Prague", "2026-12-07", "2026-12-04"],
            ["hotels", "Prague", "2026-12-04", "2026-12-04"],
            ["hotels", "   ", "2026-12-04", "2026-12-07"],
            ["hotels", "Prague", "2026-12-04", "2026-12-07", "--adults", "0"],
            ["hotels", "Prague", "2026-12-04", "2026-12-07", "--rooms", "0"],
            ["hotels", "Prague", "2026-12-04", "2026-12-07", "--top", "0"],
            ["hotels", "Prague", "2026-12-04", "2026-12-07", "--min-rating", "11"],
            ["hotels", "Prague", "2026-12-04", "2026-12-07", "--min-rating", "-1"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with patch("viajante.cli.search_hotels") as search:
                    self.assertEqual(main(argv), 1)
                    search.assert_not_called()

    def test_entire_home_help_is_strict_filter_wording(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["hotels", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue().casefold()
        self.assertNotIn("prefer entire", help_text)
        self.assertIn("entire home", help_text)
        self.assertIn("unknown", help_text)
        self.assertIn("compare-cancellation", help_text)

    def test_default_filter_gloss_and_booking_chips(self) -> None:
        report = _sample_hotel_report(offers=(_sample_hotel_offer(),))
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
            output = buffer.getvalue()
            lowered = output.casefold()
            self.assertIn("free cancellation required", lowered)
            self.assertIn("booking chips: oos=1", lowered)

    def test_non_refundable_opt_out_filter_gloss(self) -> None:
        query = HotelQuery(
            "Prague",
            date(2026, 12, 4),
            date(2026, 12, 7),
            free_cancellation=False,
        )
        applied = AppliedHotelFilters(chips=(), url="https://example.test")
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 0, 0),
            queries=(
                HotelQuerySuccess(
                    query=query,
                    applied=applied,
                    raw_count=1,
                    eligible_count=1,
                    offers=(_sample_hotel_offer(),),
                ),
            ),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--allow-non-refundable",
                    ]
                )
            output = buffer.getvalue().casefold()
            self.assertIn("non-refundable rates allowed", output)
            self.assertIn("booking chips: (none)", output)

    def test_success_output_contract(self) -> None:
        report = _sample_hotel_report(
            offers=(_sample_hotel_offer(total_price="419,50 €"),),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
            self.assertEqual(code, 0)
            output = buffer.getvalue()
            self.assertIn("Prague", output)
            self.assertIn("2026-12-04", output)
            self.assertIn("2026-12-07", output)
            self.assertIn("3 night", output)
            self.assertIn("free cancellation required", output.casefold())
            self.assertIn("booking chips: oos=1", output.casefold())
            self.assertIn("419,50 € total stay", output)
            self.assertIn("rating 8.9", output)
            self.assertIn("Old Town Apartment", output)
            self.assertIn("Prague 1, Czech Republic", output)
            self.assertIn("cancellation: free", output.casefold())
            self.assertIn("lodging: entire home", output.casefold())
            self.assertIn("2 bedrooms, 1 bathroom, 2 beds", output.casefold())
            self.assertNotIn("fabuloso", output.casefold())
            self.assertIn("raw cards: 5", output.casefold())
            self.assertIn("eligible: 3", output.casefold())
            self.assertIn("shown: 1", output.casefold())
            self.assertIn("booking.com", output.casefold())

    def test_entire_home_output_shows_property_evidence(self) -> None:
        query = HotelQuery(
            "Prague",
            date(2026, 12, 4),
            date(2026, 12, 7),
            entire_home=True,
        )
        applied = AppliedHotelFilters(
            chips=("oos=1", "privacy_type=3", "ht_id=201"),
            url="https://example.test",
        )
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 0, 0),
            queries=(
                HotelQuerySuccess(
                    query=query,
                    applied=applied,
                    raw_count=5,
                    eligible_count=3,
                    offers=(_sample_hotel_offer(),),
                ),
            ),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(
                    [
                        "hotels",
                        "Prague",
                        "2026-12-04",
                        "2026-12-07",
                        "--entire-home",
                    ]
                )
            output = buffer.getvalue().casefold()
            self.assertIn("entire homes/apartments required", output)
            self.assertIn("lodging: entire home", output)
            self.assertIn("booking chips:", output)

    def test_silent_cancellation_when_oos_filter_applied(self) -> None:
        silent = HotelOffer(
            title="Quiet Stay",
            address="Prague 1",
            total_price="200 €",
            total_price_eur=200.0,
            rating="8,7 Fabuloso",
            rating_score=8.7,
            details="Wifi",
            cancellation_evidence=CancellationEvidence.UNKNOWN,
            property_type_evidence=PropertyTypeEvidence.UNKNOWN,
            lodging_kind=LodgingKind.UNKNOWN,
            bedrooms=None,
            bathrooms=None,
            beds=None,
            link=None,
        )
        report = _sample_hotel_report(offers=(silent,))
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
            output = buffer.getvalue().casefold()
            self.assertIn("rating 8.7", output)
            self.assertNotIn("fabuloso", output)
            self.assertIn("filter applied; card silent", output)
            self.assertNotIn("cancellation: unknown", output)

    def test_empty_success_output(self) -> None:
        report = _sample_hotel_report(offers=(), raw_count=2, eligible_count=0)
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
            self.assertEqual(code, 0)
            output = buffer.getvalue()
            self.assertIn("(no eligible stays)", output)
            self.assertIn("shown: 0", output.casefold())

    def test_failure_output_and_exit_code(self) -> None:
        query = HotelQuery("Prague", date(2026, 12, 4), date(2026, 12, 7))
        applied = AppliedHotelFilters(chips=("oos=1",), url="https://example.test")
        report = HotelSearchReport(
            searched_at=datetime(2026, 8, 10, 9, 0, 0),
            queries=(
                HotelQueryFailure(
                    query=query,
                    applied=applied,
                    error=SearchError(
                        SearchErrorCode.FETCH_FAILED,
                        "Booking.com hotel search failed after 3 attempts.",
                    ),
                ),
            ),
        )
        with patch("viajante.cli.search_hotels", return_value=report):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
            self.assertEqual(code, 2)
            output = buffer.getvalue()
            self.assertIn("ERROR:", output)
            self.assertIn("free cancellation required", output.casefold())
            self.assertIn("booking chips: oos=1", output.casefold())
            self.assertNotIn("verify the final total stay", output.casefold())

    def test_save_only_when_requested(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()):
            with patch("viajante.cli.write_hotel_report_atomic") as writer:
                main(["hotels", "Prague", "2026-12-04", "2026-12-07"])
                writer.assert_not_called()

    def test_atomic_save(self) -> None:
        with patch("viajante.cli.search_hotels", return_value=_sample_hotel_report()):
            with patch(
                "viajante.cli.write_hotel_report_atomic",
                wraps=write_hotel_report_atomic,
            ) as writer:
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.json"
                    code = main(
                        [
                            "hotels",
                            "Prague",
                            "2026-12-04",
                            "2026-12-07",
                            "--save",
                            str(out),
                        ]
                    )
                    self.assertEqual(code, 0)
                    writer.assert_called_once()
                    self.assertTrue(out.exists())
                    data = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(data["schema_version"], 1)
                    self.assertEqual(data["price_basis"], "total_stay")


if __name__ == "__main__":
    unittest.main()
