from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from datetime import date, datetime, timedelta
from unittest.mock import patch

from viajante.cli import main
from viajante.flights import DEFAULT_BAGGAGE_BUFFER_EUR
from viajante.models import SearchReport
from viajante.quote import (
    cash_currency_for_country,
    cash_currency_for_origin,
    resolve_baggage_buffer,
    resolve_quote_currency,
)

FUTURE = date.today() + timedelta(days=30)
SEARCHED_AT = datetime(2026, 9, 1, 12, 0, 0)


def _report(currency: str) -> SearchReport:
    return SearchReport(searched_at=SEARCHED_AT, queries=(), currency=currency)


class OriginCashCurrencyTests(unittest.TestCase):
    def test_named_origins_map_to_that_country_cash_currency(self) -> None:
        self.assertEqual(cash_currency_for_origin("JFK"), "USD")
        self.assertEqual(cash_currency_for_origin("LHR"), "GBP")
        self.assertEqual(cash_currency_for_origin("NRT"), "JPY")
        self.assertEqual(cash_currency_for_origin("GRU"), "BRL")
        self.assertEqual(cash_currency_for_origin("SYD"), "AUD")
        self.assertEqual(cash_currency_for_origin("MAD"), "EUR")

    def test_unknown_iata_cannot_prove_a_currency(self) -> None:
        self.assertIsNone(cash_currency_for_origin("XXX"))
        self.assertIsNone(cash_currency_for_origin(""))

    def test_europe_is_not_a_currency_and_eu_is_not_eur(self) -> None:
        self.assertIsNone(cash_currency_for_country("europe"))
        self.assertIsNone(cash_currency_for_country("EU"))
        self.assertIsNone(cash_currency_for_country(""))


class ResolveQuoteCurrencyTests(unittest.TestCase):
    def test_explicit_code_wins_over_origin_country(self) -> None:
        self.assertEqual(resolve_quote_currency("gbp", "JFK"), "GBP")
        self.assertEqual(resolve_quote_currency("USD", "LHR"), "USD")

    def test_unnamed_follows_the_origin_country(self) -> None:
        self.assertEqual(resolve_quote_currency(None, "JFK"), "USD")
        self.assertEqual(resolve_quote_currency(None, "LHR"), "GBP")
        self.assertEqual(resolve_quote_currency(None, "NRT"), "JPY")
        self.assertEqual(resolve_quote_currency(None, "GRU"), "BRL")
        self.assertEqual(resolve_quote_currency(None, "MAD"), "EUR")

    def test_unproven_origin_requires_named_currency(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            resolve_quote_currency(None, "XXX")
        message = str(ctx.exception)
        self.assertIn("--currency", message)
        self.assertIn("does not convert", message.lower())

    def test_blank_explicit_does_not_become_eur(self) -> None:
        with self.assertRaises(ValueError):
            resolve_quote_currency("", "JFK")


class ResolveBaggageBufferTests(unittest.TestCase):
    def test_unnamed_is_70_only_when_quote_is_eur(self) -> None:
        self.assertEqual(resolve_baggage_buffer(None, "EUR"), DEFAULT_BAGGAGE_BUFFER_EUR)
        self.assertEqual(resolve_baggage_buffer(None, "USD"), 0)
        self.assertEqual(resolve_baggage_buffer(None, "JPY"), 0)

    def test_named_buffer_is_the_quote_amount_with_no_fx(self) -> None:
        self.assertEqual(resolve_baggage_buffer(50, "USD"), 50)
        self.assertEqual(resolve_baggage_buffer(0, "EUR"), 0)
        self.assertEqual(resolve_baggage_buffer(70, "JPY"), 70)


class QuoteCurrencyCliTests(unittest.TestCase):
    def test_flights_jfk_infers_usd_and_does_not_set_gl(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report("USD")) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", f"JFK-LHR:{FUTURE.isoformat()}", "--fetch", "sweep"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["currency"], "USD")
        self.assertIsNone(search.call_args.kwargs["country"])
        self.assertEqual(search.call_args.kwargs["buffer_eur"], 0)

    def test_flights_lhr_infers_gbp(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report("GBP")) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", f"LHR-JFK:{FUTURE.isoformat()}", "--fetch", "sweep"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["currency"], "GBP")

    def test_explicit_currency_wins_on_cli(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report("JPY")) as search:
            with patch("viajante.cli._print_report"):
                code = main(
                    [
                        "flights",
                        f"JFK-NRT:{FUTURE.isoformat()}",
                        "--currency",
                        "jpy",
                        "--fetch",
                        "sweep",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["currency"], "JPY")

    def test_mad_infers_eur_as_spain_cash_not_a_product_default(self) -> None:
        with patch("viajante.cli.search_flights", return_value=_report("EUR")) as search:
            with patch("viajante.cli._print_report"):
                code = main(["flights", f"MAD-BCN:{FUTURE.isoformat()}", "--fetch", "sweep"])
        self.assertEqual(code, 0)
        self.assertEqual(search.call_args.kwargs["currency"], "EUR")
        self.assertEqual(search.call_args.kwargs["buffer_eur"], DEFAULT_BAGGAGE_BUFFER_EUR)

    def test_hotels_without_currency_error_and_do_not_guess_eur(self) -> None:
        err = io.StringIO()
        checkout = (FUTURE + timedelta(days=3)).isoformat()
        with patch("viajante.cli.search_hotels") as search:
            with redirect_stderr(err):
                code = main(["hotels", "Tokyo", FUTURE.isoformat(), checkout])
        self.assertEqual(code, 1)
        search.assert_not_called()
        self.assertIn("--currency", err.getvalue())

    def test_help_does_not_call_eur_the_product_default(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            code = main(["--help"])
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertNotIn("quotes in EUR", text)
        self.assertNotIn("default EUR", text)
        self.assertIn("does not convert", text.lower())


if __name__ == "__main__":
    unittest.main()
