from __future__ import annotations

import copy
import io
import json
import unittest
from datetime import date, datetime, timedelta
from inspect import getsource
from pathlib import Path
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.cli import main
from viajante.control import SearchDeadline
from viajante.models import (
    HIDDEN_CITY_WARNINGS,
    HiddenCityOffer,
    HiddenCityReport,
    SearchErrorCode,
)
from viajante.skiplagged import (
    SkiplaggedShapeError,
    parse_skiplagged_offers,
    search_hidden_city,
)

FUTURE = date.today() + timedelta(days=40)
FIXTURES = Path(__file__).parent / "fixtures" / "skiplagged"
NOV_17 = date(2026, 11, 17)
NOV_22 = date(2026, 11, 22)


def _fixture(name: str) -> dict:
    """A captured sk_flights_search `result` object (see fixtures/skiplagged/README.md)."""
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _replay(result: object, calls: list[dict] | None = None):
    """Injected transport: a fixed handshake, then the given tools/call result."""

    def rpc(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
        if calls is not None:
            calls.append(payload)
        method = payload.get("method")
        if method == "initialize":
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
            return 200, {"mcp-session-id": "s1"}, body
        if method == "notifications/initialized":
            return 202, {}, ""
        return 200, {}, json.dumps({"jsonrpc": "2.0", "id": 2, "result": result})

    return rpc


class SkiplaggedFixtureParseTests(unittest.TestCase):
    def test_one_way_cards_are_usd_offers_with_their_deep_link(self) -> None:
        offers = parse_skiplagged_offers(
            _fixture("flights_jfk_mia_oneway"),
            origin="JFK",
            destination="MIA",
            departure_date=NOV_17,
            currency="USD",
        )
        self.assertEqual(len(offers), 8)
        self.assertEqual({offer.currency for offer in offers}, {"USD"})
        self.assertEqual({offer.evidence for offer in offers}, {"confirmed"})
        self.assertEqual({offer.source for offer in offers}, {"skiplagged"})
        self.assertTrue(all(offer.booking_url for offer in offers))
        self.assertTrue(
            all(
                offer.booking_url.startswith("https://skiplagged.com/flights/JFK/MIA/2026-11-17")
                for offer in offers
            )
        )
        self.assertEqual(offers[0].price, 149.0)
        self.assertEqual(offers[0].airline, "American Airlines")
        self.assertEqual(offers[0].duration, "3h 18m")

    def test_hidden_city_attribute_flags_the_nonstop_cards(self) -> None:
        offers = parse_skiplagged_offers(
            _fixture("flights_jfk_mia_oneway"),
            origin="JFK",
            destination="MIA",
            departure_date=NOV_17,
        )
        hidden = [offer for offer in offers if offer.hidden_city]
        self.assertEqual(len(hidden), 2)
        self.assertEqual({offer.stops_count for offer in hidden}, {0})
        self.assertEqual(hidden[0].warnings, HIDDEN_CITY_WARNINGS)
        standard = [offer for offer in offers if not offer.hidden_city]
        self.assertEqual(len(standard), 6)
        self.assertEqual(standard[0].warnings, ())

    def test_ticketed_destination_is_never_in_the_payload(self) -> None:
        # Not in any captured card: no field names the beyond city. Stays null.
        offers = parse_skiplagged_offers(
            _fixture("flights_jfk_mia_oneway"),
            origin="JFK",
            destination="MIA",
            departure_date=NOV_17,
        )
        self.assertTrue(all(offer.ticketed_destination is None for offer in offers))

    def test_one_stop_layover_comes_from_the_table_row(self) -> None:
        offers = parse_skiplagged_offers(
            _fixture("flights_jfk_ord_oneway"),
            origin="JFK",
            destination="ORD",
            departure_date=NOV_17,
        )
        one_stop = [offer for offer in offers if offer.stops_count == 1]
        self.assertEqual(len(one_stop), 1)
        self.assertEqual(one_stop[0].layover_city, "BOS")
        self.assertFalse(one_stop[0].hidden_city)
        nonstop = [offer for offer in offers if offer.stops_count == 0]
        self.assertTrue(all(offer.layover_city is None for offer in nonstop))

    def test_layover_is_unknown_when_the_table_text_is_gone(self) -> None:
        result = _fixture("flights_jfk_den_oneway")
        result["content"] = []
        offers = parse_skiplagged_offers(
            result, origin="JFK", destination="DEN", departure_date=NOV_17
        )
        one_stop = [offer for offer in offers if offer.stops_count == 1]
        self.assertTrue(one_stop)
        self.assertTrue(all(offer.layover_city is None for offer in one_stop))
        self.assertEqual(one_stop[0].stops_count, 1)

    def test_round_trip_hidden_return_leg_marks_the_offer(self) -> None:
        offers = parse_skiplagged_offers(
            _fixture("flights_jfk_mia_roundtrip"),
            origin="JFK",
            destination="MIA",
            departure_date=NOV_17,
            return_date=NOV_22,
        )
        self.assertEqual(len(offers), 8)
        self.assertTrue(all(offer.hidden_city for offer in offers))
        self.assertTrue(all(offer.return_date == NOV_22 for offer in offers))
        self.assertEqual({offer.currency for offer in offers}, {"USD"})
        self.assertEqual(offers[-1].airline, "Delta Air Lines")
        self.assertEqual(offers[-1].price, 309.0)

    def test_currency_keep_filters_captured_usd_cards(self) -> None:
        kept = parse_skiplagged_offers(
            _fixture("flights_jfk_den_oneway"),
            origin="JFK",
            destination="DEN",
            departure_date=NOV_17,
            currency="EUR",
        )
        self.assertEqual(kept, ())

    def test_one_bad_card_does_not_drop_priced_neighbors(self) -> None:
        result = copy.deepcopy(_fixture("flights_jfk_den_oneway"))
        cards = result["structuredContent"]["flights"]
        cards[0]["price"]["amount"] = 0
        del cards[1]["price"]["currency"]
        cards[2]["price"] = "not a price"
        offers = parse_skiplagged_offers(
            result, origin="JFK", destination="DEN", departure_date=NOV_17
        )
        self.assertEqual(len(offers), 5)

    def test_missing_flights_list_is_a_shape_change(self) -> None:
        with self.assertRaises(SkiplaggedShapeError):
            parse_skiplagged_offers(
                {"content": [], "structuredContent": {"pagination": {}}},
                origin="JFK",
                destination="DEN",
                departure_date=NOV_17,
            )

    def test_tool_error_result_is_not_an_empty_result(self) -> None:
        from viajante.skiplagged import SkiplaggedError

        with self.assertRaises(SkiplaggedError) as caught:
            parse_skiplagged_offers(
                {"isError": True, "content": [{"type": "text", "text": "bad origin"}]},
                origin="JFK",
                destination="DEN",
                departure_date=NOV_17,
            )
        self.assertNotIsInstance(caught.exception, SkiplaggedShapeError)
        self.assertIn("bad origin", str(caught.exception))


class HiddenCitySearchTests(unittest.TestCase):
    def test_rpc_failure_does_not_invent_fares(self) -> None:
        def boom(_url: str, _payload: dict, _headers: dict) -> tuple[int, dict, str]:
            raise OSError("offline")

        report = search_hidden_city("JFK", "LHR", FUTURE, rpc=boom)
        self.assertEqual(report.offers, ())
        self.assertIsNotNone(report.error)
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.FETCH_FAILED)
        self.assertEqual(report.source, "skiplagged")
        self.assertEqual(report.warnings, HIDDEN_CITY_WARNINGS)
        self.assertIsNone(report.currency)
        self.assertNotIn("currency", report.to_dict())

    def test_empty_flights_list_is_no_results(self) -> None:
        report = search_hidden_city(
            "NRT", "SIN", FUTURE, rpc=_replay({"structuredContent": {"flights": []}})
        )
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.NO_RESULTS)
        self.assertIsNone(report.currency)

    def test_shape_change_is_markup_drift_not_no_results(self) -> None:
        report = search_hidden_city("JFK", "MIA", FUTURE, rpc=_replay({"content": []}))
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.MARKUP_DRIFT)

    def test_tool_error_is_fetch_failed_with_its_message(self) -> None:
        report = search_hidden_city(
            "JFK",
            "MIA",
            FUTURE,
            rpc=_replay({"isError": True, "content": [{"type": "text", "text": "bad origin"}]}),
        )
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.FETCH_FAILED)
        self.assertIn("bad origin", report.error.message)

    def test_rate_limit_stops_the_search_after_one_request(self) -> None:
        calls: list[dict] = []

        def limited(_url: str, payload: dict, _headers: dict) -> tuple[int, dict, str]:
            calls.append(payload)
            if payload.get("method") == "initialize":
                return (
                    200,
                    {"mcp-session-id": "s1"},
                    json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
                )
            if payload.get("method") == "notifications/initialized":
                return 202, {}, ""
            return 429, {"Retry-After": "120"}, ""

        report = search_hidden_city("JFK", "MIA", FUTURE, rpc=limited)
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.BLOCKED)
        self.assertTrue(report.error.rate_limited)
        self.assertEqual([c.get("method") for c in calls].count("tools/call"), 1)

    def test_usd_keep_returns_the_captured_cards(self) -> None:
        report = search_hidden_city(
            "JFK", "MIA", NOV_17, currency="USD", rpc=_replay(_fixture("flights_jfk_mia_oneway"))
        )
        self.assertIsNone(report.error)
        self.assertEqual(len(report.offers), 8)
        self.assertEqual(report.currency, "USD")

    def test_eur_keep_on_usd_cards_is_currency_mismatch(self) -> None:
        report = search_hidden_city(
            "JFK", "MIA", NOV_17, currency="EUR", rpc=_replay(_fixture("flights_jfk_mia_oneway"))
        )
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.CURRENCY_MISMATCH)
        self.assertNotEqual(report.error.code, SearchErrorCode.NO_RESULTS)
        self.assertEqual(report.currency, "USD")
        self.assertIn("USD", report.error.message)
        self.assertIn("EUR", report.error.message)
        self.assertIn("does not convert", report.error.message)
        self.assertEqual(report.to_dict()["error"]["code"], "currency_mismatch")
        self.assertEqual(report.to_dict()["currency"], "USD")

    def test_empty_rows_with_eur_keep_stay_no_results(self) -> None:
        report = search_hidden_city(
            "MAD",
            "MIA",
            FUTURE,
            currency="EUR",
            rpc=_replay({"structuredContent": {"flights": []}}),
        )
        self.assertEqual(report.offers, ())
        assert report.error is not None
        self.assertEqual(report.error.code, SearchErrorCode.NO_RESULTS)
        self.assertEqual(report.currency, "EUR")

    def test_success_keeps_the_cheapest_top_offers(self) -> None:
        calls: list[dict] = []
        report = search_hidden_city(
            "JFK",
            "MIA",
            NOV_17,
            top=3,
            rpc=_replay(_fixture("flights_jfk_mia_oneway"), calls),
        )
        self.assertEqual([offer.price for offer in report.offers], [149.0, 149.0, 156.0])
        tool_calls = [c for c in calls if c.get("method") == "tools/call"]
        self.assertEqual(len(tool_calls), 1)
        arguments = tool_calls[0]["params"]["arguments"]
        self.assertEqual(arguments["limit"], 3)
        self.assertEqual(arguments["sort"], "price")
        self.assertEqual(arguments["departureDate"], "2026-11-17")
        self.assertNotIn("returnDate", arguments)

    def test_round_trip_sends_return_date(self) -> None:
        calls: list[dict] = []
        report = search_hidden_city(
            "JFK",
            "MIA",
            NOV_17,
            return_date=NOV_22,
            rpc=_replay(_fixture("flights_jfk_mia_roundtrip"), calls),
        )
        arguments = next(c for c in calls if c.get("method") == "tools/call")["params"]["arguments"]
        self.assertEqual(arguments["returnDate"], "2026-11-22")
        self.assertEqual(len(report.offers), 8)

    def test_reuses_mcp_session(self) -> None:
        methods: list[str] = []
        rpc = _replay(_fixture("flights_jfk_den_oneway"), calls=None)

        def recording(url: str, payload: dict, headers: dict) -> tuple[int, dict, str]:
            methods.append(str(payload.get("method")))
            return rpc(url, payload, headers)

        search_hidden_city("JFK", "DEN", FUTURE, rpc=recording)
        search_hidden_city("JFK", "DEN", FUTURE, rpc=recording)
        self.assertEqual(methods.count("initialize"), 1)
        self.assertEqual(methods.count("notifications/initialized"), 1)
        self.assertEqual(methods.count("tools/call"), 2)

    def test_past_departure_is_rejected_before_rpc(self) -> None:
        past = date.today() - timedelta(days=1)
        with self.assertRaises(ValueError):
            search_hidden_city("JFK", "LHR", past, rpc=lambda *_: (200, {}, "{}"))

    def test_search_does_not_import_google_flights(self) -> None:
        import viajante.skiplagged as module

        source = getsource(module)
        self.assertNotIn("viajante.google_flights", source)
        self.assertNotIn("viajante.flights", source)


class HiddenCityCliTests(unittest.TestCase):
    def test_hidden_city_help_lists_mixed_iata(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["hidden-city", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("JFK-LHR", help_text)
        self.assertIn("NRT-SIN", help_text)
        self.assertIn("GRU-EZE", help_text)
        self.assertIn("USD", help_text)
        self.assertIn("currency_mismatch", help_text)

    def test_cli_prints_mocked_offers(self) -> None:
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="LHR",
            departure_date=FUTURE,
            currency="USD",
            offers=(
                HiddenCityOffer(
                    origin="JFK",
                    destination="LHR",
                    departure_date=FUTURE,
                    price=350,
                    currency="USD",
                    evidence="confirmed",
                    airline="Delta",
                ),
            ),
        )
        buffer = io.StringIO()
        err = io.StringIO()
        with (
            patch("viajante.cli.search_hidden_city", return_value=report),
            patch("sys.stdout", buffer),
            patch("sys.stderr", err),
        ):
            code = main(["hidden-city", f"JFK-LHR:{FUTURE.isoformat()}"])
        self.assertEqual(code, 0)
        self.assertIn("350", buffer.getvalue())
        self.assertIn("Hidden-city", err.getvalue())

    def test_cli_prints_ticketed_and_layover(self) -> None:
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="BOS",
            departure_date=FUTURE,
            currency="USD",
            offers=(
                HiddenCityOffer(
                    origin="JFK",
                    destination="BOS",
                    departure_date=FUTURE,
                    price=89,
                    currency="USD",
                    evidence="confirmed",
                    airline="JetBlue",
                    layover_city="BOS",
                    ticketed_destination="LHR",
                    hidden_city=True,
                ),
            ),
        )
        buffer = io.StringIO()
        err = io.StringIO()
        with (
            patch("viajante.cli.search_hidden_city", return_value=report),
            patch("sys.stdout", buffer),
            patch("sys.stderr", err),
        ):
            code = main(["hidden-city", f"JFK-BOS:{FUTURE.isoformat()}"])
        self.assertEqual(code, 0)
        printed = buffer.getvalue()
        self.assertIn("ticketed LHR", printed)
        self.assertIn("via BOS", printed)
        self.assertIn("yes", printed)

    def test_out_back_sugar_is_a_round_trip(self) -> None:
        back = FUTURE + timedelta(days=5)
        report = HiddenCityReport(
            searched_at=datetime(2026, 9, 10, 12, 0, 0),
            origin="JFK",
            destination="MIA",
            departure_date=FUTURE,
            currency="USD",
            offers=(),
            return_date=back,
        )
        with patch("viajante.cli.search_hidden_city", return_value=report) as search:
            code = main(["hidden-city", f"JFK-MIA:{FUTURE.isoformat()}:{back.isoformat()}"])
        self.assertEqual(code, 0)
        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["return_date"], back)

    def test_comma_dates_are_rejected(self) -> None:
        later = FUTURE + timedelta(days=1)
        err = io.StringIO()
        with (
            patch("viajante.cli.search_hidden_city") as search,
            patch("sys.stderr", err),
        ):
            code = main(["hidden-city", f"JFK-LHR:{FUTURE.isoformat()},{later.isoformat()}"])
        self.assertEqual(code, 1)
        search.assert_not_called()
        self.assertIn("OUT:BACK", err.getvalue())


class HiddenCityDeadlineTests(unittest.TestCase):
    def test_deadline_propagates_instead_of_becoming_a_fetch_failure(self) -> None:
        def rpc(url, payload, headers):
            raise SearchDeadline()

        with self.assertRaises(SearchDeadline):
            search_hidden_city("JFK", "MIA", FUTURE, rpc=rpc)


class HiddenCitySelectionTests(unittest.TestCase):
    def test_priced_offer_with_string_evidence_does_not_crash_or_gain_an_id(self) -> None:
        from viajante.evidence import selection_records

        payload = {"offers": [{"price": 622.0, "evidence": "confirmed"}]}
        self.assertEqual(selection_records(payload), {})
        self.assertNotIn("selection_id", payload["offers"][0])

    def test_mcp_tool_returns_a_captured_offer(self) -> None:
        from viajante import evidence, mcp_handlers

        evidence.clear()
        self.addCleanup(evidence.clear)
        rpc = _replay(_fixture("flights_jfk_mia_oneway"))

        def search(origin, destination, departure_date, **kwargs):
            return search_hidden_city(origin, destination, departure_date, rpc=rpc, **kwargs)

        with patch("viajante.mcp_handlers.search_hidden_city", side_effect=search):
            payload = mcp_handlers.search_hidden_city_tool("JFK-MIA", NOV_17.isoformat())
        offer = payload["offers"][0]
        self.assertEqual(offer["price"], 149.0)
        self.assertEqual(offer["currency"], "USD")
        self.assertEqual(offer["evidence"], "confirmed")
        self.assertTrue(offer["hidden_city"])
        self.assertNotIn("selection_id", offer)


if __name__ == "__main__":
    unittest.main()
