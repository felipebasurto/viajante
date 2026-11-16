"""Google's own price level, decoded from the public results page already fetched."""

from __future__ import annotations

import _isolate  # noqa: F401
import inspect
import json
import random
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from viajante.flights import _run_search
from viajante.google_flights import SweepHttpResponse
from viajante.google_flights_page import price_insight_from_page
from viajante.google_flights_public import PublicGoogleFlightsHttpSource
from viajante.google_flights_rpc import RawFlightCard, parse_price_insight
from viajante.history import read_observations
from viajante.mcp_guide import GUIDE
from viajante.mcp_handlers import search_flights_tool
from viajante.models import (
    FlightOffer,
    FlightQuery,
    PriceInsight,
    QuerySuccess,
    RoundTrip,
    SearchReport,
)
from viajante.watch import watch_price_tool

_LOW_LABEL = "Prices are currently low — £56 cheaper than usual for your search"
_HISTORY = [
    [1787004000000, 36],
    [1787025600000, 295],
    [1792274400000, 26],
]


def _slot(
    code: object,
    *,
    lowest: object = 26,
    low: object = 25,
    high: object = 75,
    history: object = _HISTORY,
) -> list:
    return [
        code,
        [None, lowest],
        [None, 28],
        [None, 2],
        [None, low],
        [None, high],
        1,
        None,
        None,
        None,
        [history],
    ]


def _data(slot: object) -> list:
    return [None, None, None, None, None, slot]


def _heading(text: str | None, *, extra: str = "") -> str:
    body = ""
    if text is not None:
        body += f'<div role="heading" aria-level="4">{text}</div>'
    return f"<html><body>{body}{extra}</body></html>"


def _confirmed(slot: object, text: str | None, *, currency: str = "EUR", extra: str = ""):
    return price_insight_from_page(_heading(text, extra=extra), _data(slot), currency=currency)


class PriceInsightDecodeTests(unittest.TestCase):
    def test_codes_match_the_rendered_word(self) -> None:
        cases = (
            (1, "low", _LOW_LABEL),
            (2, "typical", "Prices are currently typical"),
            (3, "typical", "Prices are currently typical"),
            (4, "typical", "Prices are currently typical"),
            (5, "high", "Prices are currently high"),
        )
        for code, level, label in cases:
            with self.subTest(code=code):
                insight, error = _confirmed(_slot(code), label)
                self.assertIsNone(error)
                assert insight is not None
                self.assertEqual(insight.level, level)
                self.assertEqual(insight.page_label, label)
                self.assertEqual(insight.source, "google_flights")
                self.assertEqual(insight.currency, "EUR")

    def test_low_keeps_the_full_heading_and_a_range_above_the_lowest(self) -> None:
        insight, error = _confirmed(
            _slot(1, lowest=74, low=85, high=235), _LOW_LABEL, currency="GBP"
        )
        self.assertIsNone(error)
        assert insight is not None
        self.assertEqual(
            (insight.level, insight.lowest, insight.typical_range),
            ("low", 74.0, (85.0, 235.0)),
        )
        self.assertEqual(insight.page_label, _LOW_LABEL)

    def test_high_keeps_a_lowest_above_the_range(self) -> None:
        insight, error = _confirmed(
            _slot(5, lowest=394, low=190, high=290),
            "Prices are currently high",
            currency="USD",
        )
        self.assertIsNone(error)
        assert insight is not None
        self.assertEqual((insight.lowest, insight.typical_range), (394.0, (190.0, 290.0)))

    def test_a_different_heading_is_not_the_level(self) -> None:
        extra = '<div role="heading">Prices are unlikely to drop below $293</div>'
        insight, error = _confirmed(
            _slot(3, lowest=293, low=170, high=465),
            "Prices are currently typical",
            extra=extra,
        )
        self.assertIsNone(error)
        assert insight is not None
        self.assertEqual(insight.level, "typical")
        self.assertEqual(insight.page_label, "Prices are currently typical")

    def test_history_dates_are_the_utc_calendar_day(self) -> None:
        parsed = parse_price_insight(_data(_slot(2)), currency="EUR")
        assert parsed is not None and parsed.history is not None
        self.assertEqual(
            parsed.history,
            (("2026-08-17", 36.0), ("2026-08-18", 295.0), ("2026-10-17", 26.0)),
        )

    def test_nothing_on_the_page_is_null(self) -> None:
        self.assertEqual(
            price_insight_from_page(_heading(None), [None, None], currency="EUR"),
            (None, None),
        )
        self.assertEqual(_confirmed(_slot(True), None), (None, None))

    def test_disagreement_is_drift_and_does_not_raise(self) -> None:
        self.assertEqual(_confirmed(_slot(5), "Prices are currently typical")[1], "markup_drift")
        self.assertEqual(_confirmed(_slot(9), "Prices are currently typical")[1], "markup_drift")
        self.assertEqual(_confirmed(_slot(2), "Prices are currently unknown")[1], "markup_drift")
        disagree = (
            '<div role="heading">Prices are currently low</div>'
            '<div role="heading">Prices are currently high</div>'
        )
        self.assertEqual(
            price_insight_from_page(f"<html>{disagree}</html>", _data(_slot(1)), currency="EUR"),
            (None, "markup_drift"),
        )
        self.assertEqual(
            price_insight_from_page(None, _data(_slot(2)), currency="EUR"),  # type: ignore[arg-type]
            (None, "markup_drift"),
        )

    def test_bad_amounts_drop_the_figure_and_a_bad_history_drops_the_series(self) -> None:
        inverted, error = _confirmed(_slot(2, low=80, high=10), "Prices are currently typical")
        self.assertIsNone(error)
        assert inverted is not None
        self.assertIsNone(inverted.typical_range)
        self.assertEqual(inverted.lowest, 26.0)
        broken = parse_price_insight(_data(_slot(2, history=[[True, 10]])), currency="EUR")
        assert broken is not None
        self.assertIsNone(broken.history)
        negative = parse_price_insight(_data(_slot(2, lowest=-1)), currency="EUR")
        assert negative is not None
        self.assertIsNone(negative.lowest)
        self.assertIsNone(parse_price_insight(_data(_slot(2)), currency=""))


class PriceInsightSearchTests(unittest.TestCase):
    def _source(self, insight: PriceInsight | None, error: str | None = None):
        class Source:
            config = SimpleNamespace(html_lang="en", currency="USD", country=None)

            def fetch(self, _trip):
                return (RawFlightCard(None, None, None, None, None, None),)

            def metadata_for(self, _trip):
                return None, False

            def price_insight_for(self, _trip):
                return insight, error

            def reset(self):
                return None

        return Source()

    def _search(self, source, *, history: bool = False):
        return _run_search(
            [FlightQuery("JFK", "LHR", date(2026, 12, 10))],
            top=8,
            source=source,
            sleep=lambda _seconds: None,
            random_gen=random.Random(0),
            now=lambda: datetime(2026, 10, 18, tzinfo=timezone.utc),
            currency="USD",
            include_price_history=history,
        )

    def test_history_stays_off_unless_asked_and_a_missing_reader_is_null(self) -> None:
        insight = PriceInsight(
            level="typical",
            lowest=293.0,
            typical_range=(170.0, 465.0),
            currency="USD",
            page_label="Prices are currently typical",
            history=(("2026-10-17", 293.0),),
        )
        hidden = self._search(self._source(insight)).queries[0]
        assert isinstance(hidden, QuerySuccess) and hidden.price_insight is not None
        self.assertIsNone(hidden.price_insight.history)
        shown = self._search(self._source(insight), history=True).queries[0]
        assert isinstance(shown, QuerySuccess) and shown.price_insight is not None
        self.assertEqual(shown.price_insight.history, (("2026-10-17", 293.0),))
        bare = self._search(
            SimpleNamespace(
                config=SimpleNamespace(html_lang="en"),
                fetch=lambda _t: (),
                reset=lambda: None,
            )
        )
        self.assertIsNone(bare.queries[0].price_insight)

    def test_drift_is_stored_without_an_insight(self) -> None:
        result = self._search(self._source(None, "markup_drift")).queries[0]
        assert isinstance(result, QuerySuccess)
        self.assertIsNone(result.price_insight)
        self.assertEqual(result.price_insight_error, "markup_drift")


class PublicPageInsightTests(unittest.TestCase):
    @staticmethod
    def _page(slot: object, heading: str) -> str:
        flight = ["B6", ["JetBlue"], [[]], "JFK", None, [8, 40], "LHR", None, [20, 0], 80]
        data = [
            None,
            None,
            [[[flight, [[None, 293], "owned-token"]]], None, False],
            [[], 0, False],
            None,
            slot,
        ]
        controls = (
            '<button aria-label="Currency USD"></button>'
            '<div role="combobox">Economy</div>'
            '<div aria-label="Number of adult passengers">'
            '<span aria-live="polite">1</span></div>'
            '<div aria-label="Number of children aged 2 to 11">'
            '<span aria-live="polite">0</span></div>'
            '<div aria-label="Number of infants in their own seat">'
            '<span aria-live="polite">0</span></div>'
            '<div aria-label="Number of infants on lap">'
            '<span aria-live="polite">0</span></div>'
        )
        return (
            "<html><body>"
            + controls
            + f'<div role="heading">{heading}</div>'
            + "<script class=\"ds:1\">AF_initDataCallback({key:'ds:1',data:"
            + json.dumps(data)
            + "});</script></body></html>"
        )

    def test_the_outbound_board_keeps_its_insight(self) -> None:

        source = PublicGoogleFlightsHttpSource(currency="USD", client=object())
        trip = FlightQuery("JFK", "LHR", date(2026, 12, 10))
        url = "https://www.google.com/travel/flights?insight=1"
        page = self._page(_slot(2, lowest=293, low=170, high=465), "Prices are currently typical")
        cards = source._parse_response(
            SweepHttpResponse(200, page, url), url, trip, capture_insight=True
        )
        self.assertTrue(cards)
        insight, error = source.price_insight_for(trip)
        self.assertIsNone(error)
        assert insight is not None
        self.assertEqual(
            (insight.level, insight.lowest, insight.currency), ("typical", 293.0, "USD")
        )
        self.assertIsNotNone(insight.history)
        later = self._page(_slot(5, lowest=9, low=1, high=2), "Prices are currently high")
        source._parse_response(SweepHttpResponse(200, later, url), url, trip)
        kept = source.price_insight_for(trip)[0]
        assert kept is not None
        self.assertEqual(kept.level, "typical")
        bare = RoundTrip("JFK", "LHR", date(2026, 12, 10), date(2026, 12, 17))
        headed = self._page(None, "Prices are currently typical")
        found = source._parse_response(
            SweepHttpResponse(200, headed, url), url, bare, capture_insight=True
        )
        self.assertTrue(found)
        self.assertEqual(source.price_insight_for(bare), (None, "markup_drift"))
        quiet = self._page(None, "")
        source._parse_response(SweepHttpResponse(200, quiet, url), url, bare, capture_insight=True)
        self.assertEqual(source.price_insight_for(bare), (None, None))


class PriceInsightSurfaceTests(unittest.TestCase):
    def test_the_guide_quotes_the_label_and_history_defaults_off(self) -> None:
        flat = " ".join(GUIDE.split())
        self.assertIn(
            "`price_insight.level` is Google's comparison with its typical range, not a "
            "forecast. Quote it as Google's label. Never turn it into a promise that the "
            "price will drop.",
            flat,
        )
        self.assertIs(
            inspect.signature(search_flights_tool).parameters["include_price_history"].default,
            False,
        )

    def test_search_flights_forwards_include_price_history(self) -> None:
        day = date.today() + timedelta(days=40)
        report = SearchReport(
            searched_at=datetime(2026, 10, 18, tzinfo=timezone.utc),
            currency="USD",
            queries=(
                QuerySuccess(
                    query=FlightQuery("JFK", "LHR", day),
                    raw_count=0,
                    eligible_count=0,
                    offers=(),
                ),
            ),
        )
        with patch("viajante.mcp_handlers.search_flights", return_value=report) as search:
            payload = search_flights_tool(
                [f"JFK-LHR:{day.isoformat()}"], include_price_history=True
            )
        self.assertIs(search.call_args.kwargs["include_price_history"], True)
        self.assertIsNone(payload["queries"][0]["price_insight"])

    def test_watch_attaches_the_run_and_the_log_does_not(self) -> None:
        day = date(2026, 12, 10)
        insight = PriceInsight(
            level="low",
            lowest=74.0,
            typical_range=(85.0, 235.0),
            currency="USD",
            page_label=_LOW_LABEL,
        )
        offer = FlightOffer(
            airline="JetBlue",
            departure="08:40",
            arrival="20:00",
            price_text="293 USD",
            price=293.0,
            duration="7 hr",
            duration_hours=7.0,
            stops="Nonstop",
            stops_count=0,
            baggage_buffer=0,
            needs_bag_verify=False,
        )
        report = SearchReport(
            searched_at=datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
            currency="USD",
            fetch_backend="sweep",
            queries=(
                QuerySuccess(
                    query=FlightQuery("JFK", "LHR", day),
                    raw_count=1,
                    eligible_count=1,
                    offers=(offer,),
                    price_insight=insight,
                ),
            ),
        )
        with patch("viajante.flights._run_search", return_value=report):
            result = watch_price_tool(
                "jfk-lhr-insight",
                kind="flight",
                params={"routes": [f"JFK-LHR:{day.isoformat()}"], "currency": "USD"},
            )
        (row,) = result["results"]
        self.assertEqual(row["price_insight"]["level"], "low")
        self.assertEqual(row["price_insight"]["page_label"], _LOW_LABEL)
        self.assertNotIn("price_insight_error", row)
        (entry,) = read_observations()
        self.assertNotIn("price_insight", entry)
        self.assertNotIn("price_insight_error", entry)
