from __future__ import annotations

import base64
import json
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlparse

from viajante.flights import _normalize_offer
from viajante.google_flights import (
    EMPTY_STATE_TEXT,
    SWEEP_RETRY_BACKOFF_SECONDS,
    GoogleFlightsBlocked,
    GoogleFlightsHttpSource,
    GoogleFlightsMarkupError,
    GoogleFlightsRejected,
    NoFlightsFound,
    SweepHttpResponse,
    build_itinerary_url,
    build_search_params,
    build_search_url,
    extract_main_html,
    google_flights_url,
    looks_blocked,
    parse_flight_cards,
    parse_http_flight_cards,
    reset_shared_chrome_sweep_client,
    shared_chrome_sweep_client,
)
from viajante.google_flights_rpc import (
    CompactParseMiss,
    EmptyShoppingResults,
    ShoppingRejected,
    build_search_constraints,
    build_shopping_inner,
    build_shopping_request,
    parse_shopping_body,
    shopping_stop_code,
)
from viajante.models import FlightLeg, FlightQuery, MultiCity, RoundTrip
from viajante.tfs import _encode_legs, encode_tfs

GOLDEN_TFS_DIRECT = "GhwSCjIwMjYtMTItMDQoAGoFEgNNQURyBRIDQkNOQgEBSAGYAQI="
GOLDEN_TFS_ONE_STOP = "GhwSCjIwMjYtMTItMDQoAWoFEgNNQURyBRIDQkNOQgEBSAGYAQI="
GOLDEN_TFS_TWO_STOP = "GhwSCjIwMjYtMTItMDQoAmoFEgNNQURyBRIDQkNOQgEBSAGYAQI="
GOLDEN_TFS_TWO_ADULTS = "GhwSCjIwMjYtMTItMDQoAGoFEgNNQURyBRIDQkNOQgIBAUgBmAEC"
GOLDEN_TFS_BUSINESS = "GhwSCjIwMjYtMTItMDQoAGoFEgNNQURyBRIDQkNOQgEBSAOYAQI="
GOLDEN_TFS_ROUND_TRIP = (
    "GhwSCjIwMjYtMTAtMDkoAWoFEgNNQURyBRIDT1BPGhwSCjIwMjYtMTAtMTIoAWoFEgNPUE9yBRIDTUFEQgEBSAGYAQE="
)
GOLDEN_URL_DIRECT = (
    "https://www.google.com/travel/flights?"
    "tfs=GhwSCjIwMjYtMTItMDQoAGoFEgNNQURyBRIDQkNOQgEBSAGYAQI%3D"
    "&hl=en&tfu=EgQIABABIgA&curr=EUR"
)


def _uvarint(buf: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7


def _proto_fields(buf: bytes) -> list[tuple[int, object]]:
    index = 0
    fields: list[tuple[int, object]] = []
    while index < len(buf):
        key, index = _uvarint(buf, index)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _uvarint(buf, index)
            fields.append((field, value))
        elif wire == 2:
            length, index = _uvarint(buf, index)
            fields.append((field, buf[index : index + length]))
            index += length
        else:
            raise AssertionError(f"unexpected tfs wire {wire}")
    return fields


def _tfs_leg(payload: object) -> tuple[str, str, str]:
    assert isinstance(payload, bytes)
    inner = dict(_proto_fields(payload))
    origin_msg = inner[13]
    destination_msg = inner[14]
    day = inner[2]
    assert isinstance(origin_msg, bytes)
    assert isinstance(destination_msg, bytes)
    assert isinstance(day, bytes)
    origin = dict(_proto_fields(origin_msg))[2]
    destination = dict(_proto_fields(destination_msg))[2]
    assert isinstance(origin, bytes)
    assert isinstance(destination, bytes)
    return origin.decode("ascii"), destination.decode("ascii"), day.decode("ascii")


def build_card(
    *,
    airline: str = "Iberia",
    departure: str = "08:40",
    arrival: str = "11:30",
    duration: str = "2 hr 50 min",
    stops: str = "Nonstop",
    price: str = "€129",
) -> str:
    return (
        "<li>"
        f'<div class="sSHqwe tPgKwe ogfYpf"><span>{airline}</span></div>'
        f'<span class="mv1WYe"><div>{departure}</div><div>{arrival}</div></span>'
        f'<div class="Ak5kof"><div>{duration}</div></div>'
        f'<div class="BbR8Ec"><div class="ogfYpf">{stops}</div></div>'
        f'<div class="YMlIz FpEdX"><span>{price}</span></div>'
        "</li>"
    )


def build_results_page(*cards: str, include_other: bool = False) -> str:
    best = f'<div jsname="IWWDBc"><ul class="Rk10dc">{"".join(cards)}</ul></div>'
    if not include_other:
        return best
    other = (
        '<div jsname="YdtKid"><ul class="Rk10dc">'
        + build_card(airline="Vueling", price="€99", stops="1 stop")
        + "<li><div>More flights</div></li>"
        + "</ul></div>"
    )
    return best + other


def build_empty_page() -> str:
    return f'<div class="QEk4oc BgYkof"><div>{EMPTY_STATE_TEXT}</div></div>'


class QueryEncodingTests(unittest.TestCase):
    def test_direct_query_matches_the_golden_url(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0)
        self.assertEqual(build_search_url(query), GOLDEN_URL_DIRECT)
        params = build_search_params(query)
        self.assertEqual(params["tfs"], GOLDEN_TFS_DIRECT)
        self.assertEqual(params["hl"], "en")
        self.assertEqual(params["curr"], "EUR")
        self.assertEqual(params["tfu"], "EgQIABABIgA")

    def test_one_stop_query_changes_only_the_max_stops_field(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=1)
        params = build_search_params(query)
        self.assertEqual(params["tfs"], GOLDEN_TFS_ONE_STOP)
        parsed = parse_qs(urlparse(build_search_url(query)).query)
        self.assertEqual(parsed["tfs"], [GOLDEN_TFS_ONE_STOP])
        self.assertEqual(parsed["hl"], ["en"])
        self.assertEqual(parsed["curr"], ["EUR"])
        self.assertEqual(parsed["tfu"], ["EgQIABABIgA"])

    def test_two_adults_query_matches_the_golden_tfs(self) -> None:
        params = build_search_params(
            FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0, adults=2)
        )
        self.assertEqual(params["tfs"], GOLDEN_TFS_TWO_ADULTS)
        self.assertEqual(params["hl"], "en")
        self.assertEqual(params["curr"], "EUR")

    def test_booking_token_builds_a_google_flights_url(self) -> None:
        url = build_itinerary_url("tok")
        parsed = parse_qs(urlparse(url).query)
        self.assertEqual(urlparse(url).path, "/travel/flights")
        self.assertEqual(parsed["hl"], ["en"])
        self.assertEqual(parsed["curr"], ["EUR"])
        self.assertEqual(parsed["booking_token"], ["tok"])
        with self.assertRaises(ValueError):
            build_itinerary_url("   ")

    def test_google_flights_url_uses_search_url_without_a_token(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0)
        url = google_flights_url(query)
        self.assertEqual(url, GOLDEN_URL_DIRECT)
        self.assertEqual(google_flights_url(query, booking_token="   "), GOLDEN_URL_DIRECT)

    def test_google_flights_url_prefers_owned_booking_token(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0)
        url = google_flights_url(query, booking_token="tok")
        parsed = parse_qs(urlparse(url).query)
        self.assertEqual(parsed["booking_token"], ["tok"])
        self.assertNotIn("tfs", parsed)

    def test_google_flights_url_encodes_multi_city_from_owned_tfs(self) -> None:
        trip = MultiCity(
            (
                FlightLeg("MAD", "BCN", date(2026, 9, 1)),
                FlightLeg("BCN", "FCO", date(2026, 9, 3)),
            )
        )
        url = google_flights_url(trip)
        self.assertEqual(url, build_search_url(trip))
        parsed = parse_qs(urlparse(url).query)
        self.assertEqual(parsed["tfs"], [encode_tfs(trip)])
        self.assertEqual(parsed["hl"], ["en"])
        token_url = google_flights_url(trip, booking_token="tok")
        self.assertIn("booking_token=tok", token_url)
        self.assertNotIn("tfs=", token_url)

    def test_google_flights_url_keeps_occupancy_and_currency(self) -> None:
        solo = google_flights_url(FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0))
        family = google_flights_url(
            FlightQuery(
                "MAD",
                "BCN",
                date(2026, 12, 4),
                max_stops=0,
                adults=2,
                children=1,
            ),
            currency="USD",
            country="US",
        )
        parsed = parse_qs(urlparse(family).query)
        self.assertEqual(parsed["hl"], ["en"])
        self.assertEqual(parsed["curr"], ["USD"])
        self.assertEqual(parsed["gl"], ["US"])
        self.assertNotEqual(parsed["tfs"], parse_qs(urlparse(solo).query)["tfs"])

    def test_business_cabin_query_matches_the_golden_tfs(self) -> None:
        params = build_search_params(
            FlightQuery(
                "MAD",
                "BCN",
                date(2026, 12, 4),
                max_stops=0,
                cabin="business",
            )
        )
        self.assertEqual(params["tfs"], GOLDEN_TFS_BUSINESS)
        self.assertEqual(params["hl"], "en")
        self.assertEqual(params["curr"], "EUR")

    def test_two_stop_one_way_uses_tfs_field_five_value_two(self) -> None:
        encoded = _encode_legs(
            (FlightLeg("MAD", "BCN", date(2026, 12, 4), max_stops=2),),
            adults=1,
            cabin="economy",
            trip_kind=2,
        )
        self.assertEqual(encoded, GOLDEN_TFS_TWO_STOP)
        self.assertIn(b"\x28\x02", base64.b64decode(encoded))

    def test_child_and_lap_infant_use_distinct_passenger_types(self) -> None:
        encoded = _encode_legs(
            (FlightLeg("MAD", "BCN", date(2026, 12, 4), max_stops=0),),
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
            cabin="economy",
            trip_kind=2,
        )
        packed = base64.b64decode(encoded)
        # Field 8 packed enums: adult=1, child=2, infant in seat=3, infant on lap=4.
        self.assertIn(b"\x42\x05\x01\x01\x02\x03\x04", packed)
        two_adults = base64.b64decode(GOLDEN_TFS_TWO_ADULTS)
        self.assertIn(b"\x42\x02\x01\x01", two_adults)
        self.assertNotIn(b"\x02\x03\x04", two_adults)

    def test_round_trip_repeats_flight_data_and_sets_trip_kind(self) -> None:
        trip = RoundTrip("MAD", "OPO", date(2026, 10, 9), date(2026, 10, 12), max_stops=1)
        self.assertEqual(encode_tfs(trip), GOLDEN_TFS_ROUND_TRIP)
        self.assertEqual(build_search_params(trip)["tfs"], GOLDEN_TFS_ROUND_TRIP)

    def test_multi_city_tfs_repeats_legs_and_sets_trip_kind(self) -> None:
        trip = MultiCity(
            (
                FlightLeg("MAD", "BCN", date(2026, 9, 1)),
                FlightLeg("BCN", "FCO", date(2026, 9, 3)),
            )
        )
        encoded = encode_tfs(trip)
        fields = _proto_fields(base64.b64decode(encoded))
        flights = [payload for field, payload in fields if field == 3]
        self.assertEqual(len(flights), 2)
        self.assertEqual(_tfs_leg(flights[0]), ("MAD", "BCN", "2026-09-01"))
        self.assertEqual(_tfs_leg(flights[1]), ("BCN", "FCO", "2026-09-03"))
        self.assertEqual([value for field, value in fields if field == 19], [3])
        self.assertEqual(
            encoded,
            _encode_legs(trip.legs, adults=1, cabin="economy", trip_kind=3),
        )

    def test_open_jaw_yvr_lhr_lgw_encodes_tfs_and_shopping(self) -> None:
        trip = MultiCity(
            (
                FlightLeg("YVR", "LHR", date(2026, 10, 9)),
                FlightLeg("LGW", "YVR", date(2026, 10, 13)),
            )
        )
        encoded = encode_tfs(trip)
        fields = _proto_fields(base64.b64decode(encoded))
        flights = [payload for field, payload in fields if field == 3]
        self.assertEqual(
            [_tfs_leg(payload) for payload in flights],
            [("YVR", "LHR", "2026-10-09"), ("LGW", "YVR", "2026-10-13")],
        )
        self.assertEqual([value for field, value in fields if field == 19], [3])
        params = build_search_params(trip)
        self.assertEqual(params["tfs"], encoded)
        self.assertEqual(params["hl"], "en")
        parsed = parse_qs(urlparse(build_search_url(trip)).query)
        self.assertEqual(parsed["tfs"], [encoded])
        self.assertEqual(parsed["hl"], ["en"])

        inner = build_shopping_inner(trip)
        self.assertEqual(inner[1][2], 3)
        outbound, inbound = inner[1][13]
        self.assertEqual(outbound[0], [[["YVR", 0]]])
        self.assertEqual(outbound[1], [[["LHR", 0]]])
        self.assertEqual(outbound[6], "2026-10-09")
        self.assertEqual(inbound[0], [[["LGW", 0]]])
        self.assertEqual(inbound[1], [[["YVR", 0]]])
        self.assertEqual(inbound[6], "2026-10-13")
        self.assertEqual(outbound[14], 3)
        self.assertEqual(inbound[14], 3)
        url, body = build_shopping_request(trip)
        self.assertEqual(parse_qs(urlparse(url).query)["hl"], ["en"])
        self.assertTrue(body.startswith("f.req="))

    def test_html_lang_and_currency_args_reach_url_params(self) -> None:
        params = build_search_params(
            FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0),
            html_lang="en",
            currency="USD",
        )
        self.assertEqual(params["hl"], "en")
        self.assertEqual(params["curr"], "USD")
        self.assertNotIn("gl", params)

    def test_country_arg_reaches_url_params(self) -> None:
        params = build_search_params(
            FlightQuery("MAD", "BCN", date(2026, 12, 4), max_stops=0),
            html_lang="en",
            currency="GBP",
            country="GB",
        )
        self.assertEqual(params["hl"], "en")
        self.assertEqual(params["curr"], "GBP")
        self.assertEqual(params["gl"], "GB")


class OwnedCardParserTests(unittest.TestCase):
    def test_nonstop_survives_direct_only_search(self) -> None:
        card = parse_flight_cards(build_results_page(build_card(stops="Nonstop")))[0]
        offer = _normalize_offer(card, max_stops=0)
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.stops, "Nonstop")
        self.assertEqual(offer.stops_count, 0)

    def test_every_priced_card_is_kept(self) -> None:
        cards_html = [build_card(price=f"€{eur}", stops="Nonstop") for eur in (129, 154, 188)]
        cards = parse_flight_cards(build_results_page(*cards_html))
        self.assertEqual(len(cards), 3)
        kept = [
            offer
            for offer in (_normalize_offer(card, max_stops=0) for card in cards)
            if offer is not None
        ]
        self.assertEqual(len(kept), 3)

    def test_secondary_group_keeps_priced_cards_and_skips_structural_rows(self) -> None:
        cards = parse_flight_cards(build_results_page(build_card(price="€129"), include_other=True))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1].airline, "Vueling")
        self.assertEqual(cards[1].price, "€99")

    def test_one_stop_is_filtered_by_max_stops_but_kept_otherwise(self) -> None:
        card = parse_flight_cards(build_results_page(build_card(stops="1 stop")))[0]
        self.assertIsNone(_normalize_offer(card, max_stops=0))
        offer = _normalize_offer(card, max_stops=1)
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.stops, "1 stop")
        self.assertEqual(offer.stops_count, 1)

    def test_raw_price_and_duration_reach_the_offer(self) -> None:
        card = parse_flight_cards(
            build_results_page(build_card(price="€1,234.56", duration="2 hr 50 min"))
        )[0]
        self.assertEqual(card.price, "€1,234.56")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.price, "€1,234.56")
        self.assertEqual(offer.price_eur, 1234.56)
        self.assertAlmostEqual(offer.duration_hours or 0, 2 + 50 / 60)

    def test_spanish_stop_labels_are_raw_only(self) -> None:
        card = parse_flight_cards(
            build_results_page(build_card(stops="Sin escalas", price="1.234,56 €"))
        )[0]
        self.assertEqual(card.stops, "Sin escalas")
        self.assertEqual(card.price, "1.234,56 €")
        skipped = _normalize_offer(card, max_stops=0)
        self.assertIsNone(skipped)
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.stops, "Sin escalas")
        self.assertIsNone(offer.stops_count)
        self.assertEqual(offer.price_eur, 1234.56)

    def test_cards_beat_stale_empty_state_markup(self) -> None:
        html = build_empty_page() + build_results_page(build_card(price="€88"))
        cards = parse_flight_cards(html)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].price, "€88")

    def test_explicit_empty_state_raises_no_flights_found(self) -> None:
        with self.assertRaises(NoFlightsFound) as ctx:
            parse_flight_cards(build_empty_page())
        self.assertEqual(ctx.exception.observed_text, EMPTY_STATE_TEXT)

    def test_empty_card_list_is_no_flights_found(self) -> None:
        with self.assertRaises(NoFlightsFound):
            parse_flight_cards('<div jsname="IWWDBc"><ul class="Rk10dc"></ul></div>')

    def test_unknown_markup_raises_markup_error(self) -> None:
        with self.assertRaises(GoogleFlightsMarkupError):
            parse_flight_cards("<div>completely unrelated page</div>")


def _http_page(inner: str) -> str:
    return (
        f'<html><body><div role="main">{inner}</div>'
        "<footer>chrome chrome chrome</footer></body></html>"
    )


class HttpSweepParseTests(unittest.TestCase):
    def test_http_body_yields_the_same_raw_cards(self) -> None:
        html = _http_page(build_results_page(build_card(price="€39", airline="Vueling")))
        cards = parse_http_flight_cards(html)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].airline, "Vueling")
        self.assertEqual(cards[0].price, "€39")
        self.assertEqual(cards[0].stops, "Nonstop")
        offer = _normalize_offer(cards[0], max_stops=1)
        assert offer is not None
        self.assertEqual(offer.price, "€39")
        self.assertEqual(offer.price_eur, 39.0)

    def test_extract_main_drops_chrome_outside_main(self) -> None:
        inner = build_results_page(build_card(price="€88"))
        extracted = extract_main_html(_http_page(inner))
        self.assertIn("€88", extracted)
        self.assertNotIn("chrome chrome chrome", extracted)

    def test_http_empty_state_is_no_flights(self) -> None:
        with self.assertRaises(NoFlightsFound):
            parse_http_flight_cards(_http_page(build_empty_page()))

    def test_http_unknown_shell_is_markup_error(self) -> None:
        with self.assertRaises(GoogleFlightsMarkupError):
            parse_http_flight_cards(_http_page("<div>completely unrelated page</div>"))

    def test_consent_and_sorry_urls_are_blocks(self) -> None:
        self.assertTrue(looks_blocked("<html></html>", "https://consent.google.com/ml"))
        self.assertTrue(looks_blocked("<html></html>", "https://www.google.com/sorry/index"))
        self.assertTrue(looks_blocked("Our systems have detected unusual traffic", ""))
        self.assertFalse(looks_blocked(_http_page(build_results_page(build_card())), ""))

    def test_http_source_uses_fixture_body_not_the_network(self) -> None:
        html = _http_page(build_results_page(build_card(price="€131", airline="Iberia")))

        class _Resp:
            def __init__(self) -> None:
                self.headers = {"Content-Encoding": ""}
                self.status = 200

            def read(self) -> bytes:
                return html.encode("utf-8")

            def geturl(self) -> str:
                return "https://www.google.com/travel/flights?hl=en"

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        class _Opener:
            def open(self, request: object, timeout: float = 0) -> _Resp:
                return _Resp()

        source = GoogleFlightsHttpSource(opener=_Opener())
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(cards[0].price, "€131")

    def test_http_source_raises_blocked_on_sorry_redirect(self) -> None:
        class _Resp:
            headers = {"Content-Encoding": ""}
            status = 200

            def read(self) -> bytes:
                return b"<html>sorry</html>"

            def geturl(self) -> str:
                return "https://www.google.com/sorry/index?continue=flights"

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        class _Opener:
            def open(self, request: object, timeout: float = 0) -> _Resp:
                return _Resp()

        source = GoogleFlightsHttpSource(opener=_Opener())
        with self.assertRaises(GoogleFlightsBlocked):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))


def _itinerary(
    *,
    airline: str = "Iberia",
    code: str = "IB",
    dep: tuple[int, int] = (8, 40),
    arr: tuple[int, int] = (11, 30),
    minutes: int = 170,
    price: int = 129,
    legs: int = 1,
    bags: tuple[int, int] | None = None,
) -> list[object]:
    flight = [
        code,
        [airline],
        [[] for _ in range(legs)],
        "MAD",
        None,
        list(dep),
        "BCN",
        None,
        list(arr),
        minutes,
    ]
    fare: list[object] = [[None, price], "tok"]
    if bags is not None:
        fare.append(list(bags))
    return [flight, fare]


def _compact_body(*itineraries: list[object], other: tuple[list[object], ...] = ()) -> str:
    data: list[object] = [
        None,
        None,
        [list(itineraries), None, False],
        [list(other), len(other), False],
    ]
    wrb = [["wrb.fr", None, json.dumps(data, separators=(",", ":"))]]
    raw = json.dumps(wrb, separators=(",", ":"))
    return f")]}}'\n\n{len(raw)}\n{raw}"


class _FakeSweepClient:
    def __init__(
        self,
        *,
        post_text: str = "",
        post_status: int = 200,
        post_url: str = "https://www.google.com/_/FlightsFrontendUi/data/shopping",
        get_text: str = "",
        get_status: int = 200,
        get_url: str = "https://www.google.com/travel/flights?hl=en",
        post_replies: tuple[SweepHttpResponse, ...] = (),
        get_replies: tuple[SweepHttpResponse, ...] = (),
    ) -> None:
        self.post_text = post_text
        self.post_status = post_status
        self.post_url = post_url
        self.get_text = get_text
        self.get_status = get_status
        self.get_url = get_url
        self._post_replies = list(post_replies)
        self._get_replies = list(get_replies)
        self.posts: list[str] = []
        self.gets: list[str] = []

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: object,
        timeout: float,
    ) -> SweepHttpResponse:
        self.posts.append(url)
        self.last_post_data = data
        if self._post_replies:
            return self._post_replies.pop(0)
        return SweepHttpResponse(self.post_status, self.post_text, self.post_url)

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        self.gets.append(url)
        if self._get_replies:
            return self._get_replies.pop(0)
        return SweepHttpResponse(self.get_status, self.get_text, self.get_url)

    def close(self) -> None:
        return None


class ShoppingRpcTests(unittest.TestCase):
    def test_inner_payload_keeps_owned_airport_nesting(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1, adults=2, cabin="business")
        inner = build_shopping_inner(query)
        flight = inner[1][13][0]
        self.assertEqual(flight[0], [[["MAD", 0]]])
        self.assertEqual(flight[1], [[["BCN", 0]]])
        self.assertEqual(flight[3], 2)
        self.assertEqual(flight[6], "2026-09-01")
        self.assertEqual(inner[1][5], 3)
        self.assertEqual(inner[1][6], [2, 0, 0, 0])
        self.assertIsNone(inner[1][7])
        self.assertIsNone(flight[7])

    def test_occupancy_slot_is_adults_children_seat_lap(self) -> None:
        query = FlightQuery(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            adults=2,
            children=1,
            infants_in_seat=1,
            infants_on_lap=1,
        )
        inner = build_shopping_inner(query)
        self.assertEqual(inner[1][6], [2, 1, 1, 1])
        default = build_shopping_inner(FlightQuery("MAD", "BCN", date(2026, 9, 1)))
        self.assertEqual(default[1][6], [1, 0, 0, 0])

    def test_bags_pair_fills_constraints_index_10(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), bags=1, carry_on=1)
        inner = build_shopping_inner(query)
        self.assertEqual(inner[1][6], [1, 0, 0, 0])
        self.assertIsNone(inner[1][7])
        self.assertIsNone(inner[1][8])
        self.assertIsNone(inner[1][9])
        self.assertEqual(inner[1][10], [1, 1])
        checked_only = build_shopping_inner(FlightQuery("MAD", "BCN", date(2026, 9, 1), bags=2))
        self.assertEqual(checked_only[1][10], [2, 0])
        self.assertIsNone(checked_only[1][7])
        carry_only = build_shopping_inner(FlightQuery("MAD", "BCN", date(2026, 9, 1), carry_on=1))
        self.assertEqual(carry_only[1][10], [0, 1])
        default = build_shopping_inner(FlightQuery("MAD", "BCN", date(2026, 9, 1)))
        self.assertIsNone(default[1][7])
        self.assertIsNone(default[1][10])

    def test_airline_include_fills_segment_index_7(self) -> None:
        query = FlightQuery(
            "MAD",
            "BCN",
            date(2026, 9, 1),
            airlines=("BA", "KL"),
        )
        inner = build_shopping_inner(query)
        flight = inner[1][13][0]
        self.assertEqual(flight[7], [None, [["BA"], ["KL"]]])
        self.assertIsNone(inner[1][7])
        self.assertIsNone(inner[1][10])

    def test_airline_exclude_fills_segment_index_7(self) -> None:
        query = FlightQuery("MAD", "BCN", date(2026, 9, 1), exclude_airlines=("DL",))
        flight = build_shopping_inner(query)[1][13][0]
        self.assertEqual(flight[7], [1, [["DL"]]])

    def test_alliance_include_and_exclude_use_iata_designators(self) -> None:
        oneworld = FlightQuery("MAD", "LHR", date(2026, 9, 1), alliances=("oneworld",))
        self.assertEqual(
            build_shopping_inner(oneworld)[1][13][0][7],
            [None, [["*O"]]],
        )
        not_star = FlightQuery("MAD", "FRA", date(2026, 9, 1), exclude_alliances=("star",))
        self.assertEqual(
            build_shopping_inner(not_star)[1][13][0][7],
            [1, [["*A"]]],
        )
        mixed = FlightQuery(
            "MAD",
            "JFK",
            date(2026, 9, 1),
            airlines=("BA",),
            exclude_alliances=("star",),
        )
        self.assertEqual(
            build_shopping_inner(mixed)[1][13][0][7],
            [None, [["BA"]], [["*A"]]],
        )

    def test_round_trip_carrier_filter_applies_to_both_segments(self) -> None:
        trip = RoundTrip(
            "MAD",
            "LHR",
            date(2026, 10, 9),
            date(2026, 10, 12),
            airlines=("BA",),
        )
        outbound, inbound = build_shopping_inner(trip)[1][13]
        self.assertEqual(outbound[7], [None, [["BA"]]])
        self.assertEqual(inbound[7], [None, [["BA"]]])

    def test_shopping_stop_table_is_not_the_tfs_integer(self) -> None:
        self.assertEqual(shopping_stop_code(0), 1)
        self.assertEqual(shopping_stop_code(1), 2)
        self.assertEqual(shopping_stop_code(2), 3)
        nonstop = build_shopping_inner(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=0))
        self.assertEqual(nonstop[1][13][0][3], 1)
        two_stop = build_shopping_inner(
            RoundTrip("MAD", "NRT", date(2026, 10, 1), date(2026, 10, 20), max_stops=2)
        )
        self.assertEqual(two_stop[1][13][0][3], 3)
        self.assertEqual(two_stop[1][13][1][3], 3)
        self.assertEqual(
            FlightLeg("MAD", "NRT", date(2026, 10, 1), max_stops=2).max_stops,
            2,
        )

    def test_round_trip_shopping_sets_kind_and_return_classifier(self) -> None:
        trip = RoundTrip("MAD", "OPO", date(2026, 10, 9), date(2026, 10, 12), max_stops=1)
        inner = build_shopping_inner(trip)
        self.assertEqual(inner[1][2], 1)
        outbound, inbound = inner[1][13]
        self.assertEqual(outbound[14], 3)
        self.assertEqual(inbound[14], 1)
        self.assertEqual(outbound[6], "2026-10-09")
        self.assertEqual(inbound[6], "2026-10-12")

    def test_multi_city_shopping_kind_keeps_outbound_classifier(self) -> None:
        trip = MultiCity(
            (
                FlightLeg("MAD", "BCN", date(2026, 9, 1)),
                FlightLeg("BCN", "FCO", date(2026, 9, 3)),
                FlightLeg("FCO", "MAD", date(2026, 9, 6)),
            )
        )
        constraints = build_search_constraints(trip)
        self.assertEqual(constraints[2], 3)
        self.assertEqual([segment[14] for segment in constraints[13]], [3, 3, 3])

    def test_selected_flight_lands_on_the_first_segment(self) -> None:
        pinned = ["tok"]
        constraints = build_search_constraints(
            FlightQuery("MAD", "BCN", date(2026, 9, 1)),
            selected_flight=pinned,
        )
        self.assertEqual(constraints[13][0][8], pinned)

    def test_request_body_is_f_req_envelope(self) -> None:
        query = FlightQuery("MAD", "OPO", date(2026, 10, 9), max_stops=0)
        url, body = build_shopping_request(query)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(params["hl"], ["en"])
        self.assertEqual(params["curr"], ["EUR"])
        self.assertNotIn("gl", params)
        self.assertEqual(params["rt"], ["c"])
        self.assertTrue(body.startswith("f.req="))
        envelope = json.loads(unquote(body[len("f.req=") :]))
        self.assertIsNone(envelope[0])
        inner = json.loads(envelope[1])
        self.assertEqual(inner[1][13][0][1], [[["OPO", 0]]])

    def test_currency_and_country_reach_rpc_params(self) -> None:
        query = FlightQuery("MAD", "OPO", date(2026, 10, 9), max_stops=0)
        url, _body = build_shopping_request(query, html_lang="en", currency="USD", country="US")
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["hl"], ["en"])
        self.assertEqual(params["curr"], ["USD"])
        self.assertEqual(params["gl"], ["US"])

    def test_journey_list_keeps_outbound_clocks_and_package_price(self) -> None:
        outbound = _itinerary(airline="Iberia", dep=(8, 0), arr=(9, 10), minutes=70, price=40)[0]
        inbound = _itinerary(airline="Iberia", dep=(18, 0), arr=(19, 20), minutes=80, price=40)[0]
        item = [[outbound, inbound], [[None, 199], "tok"]]
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(card.departure, "08:00")
        self.assertEqual(card.arrival, "09:10")
        self.assertEqual(card.price, "€199")
        self.assertEqual(card.booking_token, "tok")
        self.assertEqual(len(card.legs), 2)
        self.assertEqual(card.legs[1].departure, "18:00")
        self.assertEqual(card.legs[1].arrival, "19:20")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(len(offer.legs), 2)
        self.assertEqual(offer.legs[1].departure, "18:00")
        self.assertEqual(offer.to_dict()["legs"][1]["arrival"], "19:20")

    def test_wrapped_round_trip_pair_keeps_return_leg(self) -> None:
        outbound = _itinerary(airline="Iberia", dep=(8, 0), arr=(9, 10), minutes=70, price=40)[0]
        inbound = _itinerary(airline="TAP", dep=(18, 0), arr=(19, 20), minutes=80, price=40)[0]
        inbound[3] = "OPO"
        inbound[6] = "MAD"
        item = [[[outbound], [inbound]], [[None, 199], "tok"]]
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(len(card.legs), 2)
        self.assertEqual(card.legs[0].departure, "08:00")
        self.assertEqual(card.legs[1].departure, "18:00")
        self.assertEqual(card.legs[1].arrival, "19:20")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        data = offer.to_dict()
        self.assertEqual(len(data["legs"]), 2)
        self.assertEqual(data["legs"][1]["departure"], "18:00")
        self.assertEqual(data["legs"][1]["arrival"], "19:20")

    def test_sibling_return_flight_keeps_return_leg(self) -> None:
        outbound = _itinerary(airline="Iberia", dep=(8, 0), arr=(9, 10), minutes=70, price=40)[0]
        inbound = _itinerary(airline="Iberia", dep=(18, 0), arr=(19, 20), minutes=80, price=40)[0]
        inbound[3] = "OPO"
        inbound[6] = "MAD"
        item = [outbound, [[None, 199], "tok"], inbound]
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(len(card.legs), 2)
        self.assertEqual(card.legs[1].arrival, "19:20")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(len(offer.to_dict()["legs"]), 2)

    def test_live_shaped_round_trip_keeps_return_airports(self) -> None:
        day_out = [2026, 10, 9]
        day_back = [2026, 10, 12]
        outbound = _live_flight(
            code="TP",
            airline="Tap Air Portugal",
            legs=[
                _live_leg(
                    origin="MAD",
                    origin_name="Adolfo Suárez Madrid-Barajas Airport",
                    dest="OPO",
                    dest_name="Francisco Sá Carneiro Airport",
                    dep=[8, 0],
                    arr=[9, 10],
                    minutes=70,
                    dep_date=day_out,
                    arr_date=day_out,
                )
            ],
            origin="MAD",
            dest="OPO",
            dep_date=day_out,
            dep=[8, 0],
            arr_date=day_out,
            arr=[9, 10],
            minutes=70,
        )
        inbound = _live_flight(
            code="TP",
            airline="Tap Air Portugal",
            legs=[
                _live_leg(
                    origin="OPO",
                    origin_name="Francisco Sá Carneiro Airport",
                    dest="MAD",
                    dest_name="Adolfo Suárez Madrid-Barajas Airport",
                    dep=[18, 0],
                    arr=[19, 20],
                    minutes=80,
                    dep_date=day_back,
                    arr_date=day_back,
                )
            ],
            origin="OPO",
            dest="MAD",
            dep_date=day_back,
            dep=[18, 0],
            arr_date=day_back,
            arr=[19, 20],
            minutes=80,
        )
        item = [[outbound, inbound], [[None, 199], "tok"]]
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(len(card.legs), 2)
        self.assertEqual(card.legs[0].departure, "08:00")
        self.assertEqual(card.legs[1].departure, "18:00")
        self.assertEqual(card.legs[1].arrival, "19:20")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        data = offer.to_dict()
        self.assertEqual(len(data["legs"]), 2)
        self.assertEqual(data["legs"][1]["arrival"], "19:20")

    def test_outbound_only_compact_does_not_invent_a_return_leg(self) -> None:
        # Packaged --trip rt still encodes return_date on the query. If the
        # shopping body only has the outbound flight, we must not fake a return.
        card = parse_shopping_body(_compact_body(_iberia_late_nonstop()))[0]
        self.assertEqual(len(card.legs), 1)
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(len(offer.to_dict()["legs"]), 1)

    def test_compact_body_yields_raw_card_fields(self) -> None:
        body = _compact_body(
            _itinerary(
                airline="Air Europa",
                dep=(17, 5),
                arr=(21, 10),
                minutes=245,
                price=83,
                legs=2,
            )
        )
        cards = parse_shopping_body(body)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].airline, "Air Europa")
        self.assertEqual(cards[0].departure, "17:05")
        self.assertEqual(cards[0].arrival, "21:10")
        self.assertEqual(cards[0].duration, "4 hr 5 min")
        self.assertEqual(cards[0].stops, "1 stop")
        self.assertEqual(cards[0].price, "€83")
        offer = _normalize_offer(cards[0], max_stops=1)
        assert offer is not None
        self.assertEqual(offer.price_eur, 83.0)
        self.assertEqual(offer.stops_count, 1)
        self.assertEqual(offer.duration_hours, 4 + 5 / 60)
        self.assertIsNone(cards[0].checked_bags)
        self.assertIsNone(cards[0].carry_on)
        self.assertNotIn("checked_bags", offer.to_dict())
        self.assertNotIn("carry_on", offer.to_dict())

    def test_fare_bags_pair_is_parsed_and_otherwise_omitted(self) -> None:
        with_bags = parse_shopping_body(_compact_body(_itinerary(price=91, bags=(1, 1))))[0]
        self.assertEqual(with_bags.checked_bags, 1)
        self.assertEqual(with_bags.carry_on, 1)
        offer = _normalize_offer(with_bags, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.checked_bags, 1)
        self.assertEqual(offer.carry_on, 1)
        self.assertEqual(offer.to_dict()["checked_bags"], 1)
        self.assertFalse(offer.needs_bag_verify)
        self.assertEqual(offer.baggage_buffer_eur, 0)
        missing = parse_shopping_body(_compact_body(_itinerary(price=91)))[0]
        self.assertIsNone(missing.checked_bags)
        self.assertIsNone(missing.carry_on)

    def test_owned_bags_fixture_parses_the_fare_pair(self) -> None:
        body = (Path(__file__).resolve().parent / "bench" / "shopping-bags.wrb").read_text(
            encoding="utf-8"
        )
        card = parse_shopping_body(body)[0]
        self.assertEqual(card.airline, "Ryanair")
        self.assertEqual(card.checked_bags, 1)
        self.assertEqual(card.carry_on, 1)
        self.assertEqual(card.price, "€64")

    def test_missing_itinerary_arrival_uses_last_leg(self) -> None:
        item = _itinerary(dep=(13, 40), arr=(16, 20), minutes=160, price=74, legs=2)
        item[0][8] = None
        first = [None] * 11
        first[8] = [13, 40]
        first[10] = [14, 30]
        last = [None] * 11
        last[8] = [15, 10]
        last[10] = [16, 20]
        item[0][2] = [first, last]
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(card.departure, "13:40")
        self.assertEqual(card.arrival, "16:20")

    def test_midnight_and_noon_clocks(self) -> None:
        body = _compact_body(
            _itinerary(dep=(0, 10), arr=(12, 0), minutes=60, price=40, legs=1),
        )
        card = parse_shopping_body(body)[0]
        self.assertEqual(card.departure, "00:10")
        self.assertEqual(card.arrival, "12:00")
        self.assertEqual(card.duration, "1 hr")
        self.assertEqual(card.stops, "Nonstop")

    def test_empty_itinerary_slots_are_no_flights(self) -> None:
        with self.assertRaises(EmptyShoppingResults):
            parse_shopping_body(_compact_body())

    def test_unreadable_body_is_compact_miss(self) -> None:
        with self.assertRaises(CompactParseMiss):
            parse_shopping_body("not a shopping payload")

    def test_source_uses_compact_post_not_html(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(post_text=_compact_body(_itinerary(price=88, airline="Iberia")))
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(cards[0].price, "€88")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [])

    def test_source_falls_back_to_html_when_compact_misses(self) -> None:
        sleeps: list[float] = []
        html = _http_page(build_results_page(build_card(price="€39", airline="Vueling")))
        client = _FakeSweepClient(post_text="totally unrelated", get_text=html)
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].airline, "Vueling")
        self.assertEqual(cards[0].price, "€39")
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(len(client.gets), 1)
        self.assertEqual(sleeps, [])

    def test_empty_compact_does_not_download_html(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(post_text=_compact_body())
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(NoFlightsFound):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_source_raises_blocked_on_shopping_403(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(post_status=403, post_text="no")
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(GoogleFlightsBlocked):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [])


class HttpSweepRetryTests(unittest.TestCase):
    def test_retry_backoff_is_under_200ms_not_an_anti_bot_pause(self) -> None:
        self.assertGreater(SWEEP_RETRY_BACKOFF_SECONDS, 0.0)
        self.assertLess(SWEEP_RETRY_BACKOFF_SECONDS, 0.2)

    def test_tiny_html_drift_replays_once_and_keeps_compact_cards(self) -> None:
        sleeps: list[float] = []
        tiny = _http_page("<div>loading</div>")
        client = _FakeSweepClient(
            post_replies=(
                SweepHttpResponse(200, "not shopping"),
                SweepHttpResponse(200, _compact_body(_itinerary(price=77, airline="Iberia"))),
            ),
            get_replies=(SweepHttpResponse(200, tiny),),
        )
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(cards[0].price, "€77")
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(len(client.gets), 1)
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_drift_twice_is_still_markup_error_after_one_retry(self) -> None:
        sleeps: list[float] = []
        tiny = _http_page("<div>loading</div>")
        client = _FakeSweepClient(post_text="not shopping", get_text=tiny)
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(GoogleFlightsMarkupError):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(len(client.gets), 2)
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_empty_compact_then_cards_replays_on_the_same_client(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(
            post_replies=(
                SweepHttpResponse(200, _compact_body()),
                SweepHttpResponse(200, _compact_body(_itinerary(price=64, airline="Ryanair"))),
            )
        )
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].airline, "Ryanair")
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_shopping_503_then_cards_retries_once(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(
            post_replies=(
                SweepHttpResponse(503, "upstream"),
                SweepHttpResponse(200, _compact_body(_itinerary(price=91, airline="Iberia"))),
            )
        )
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        cards = source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(cards[0].price, "€91")
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_shopping_500_retries_once_then_stays_blocked(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(post_status=500, post_text="no")
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(GoogleFlightsBlocked) as caught:
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [SWEEP_RETRY_BACKOFF_SECONDS])

    def test_shopping_reject_is_not_retried(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(post_text=_error_response_body())
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(GoogleFlightsRejected):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.gets, [])
        self.assertEqual(sleeps, [])

    def test_consent_block_is_not_retried(self) -> None:
        sleeps: list[float] = []
        client = _FakeSweepClient(
            post_text="not shopping",
            get_url="https://consent.google.com/ml",
            get_text="<html></html>",
        )
        source = GoogleFlightsHttpSource(client=client, sleep=sleeps.append)
        with self.assertRaises(GoogleFlightsBlocked) as caught:
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertIsNone(caught.exception.status)
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(len(client.gets), 1)
        self.assertEqual(sleeps, [])


def _live_leg(
    *,
    origin: str,
    origin_name: str,
    dest: str,
    dest_name: str,
    dep: list[object],
    arr: list[object],
    minutes: int,
    dep_date: list[int],
    arr_date: list[int],
    code: str = "TP",
    number: str = "1013",
    airline: str = "Tap Air Portugal",
    operated_by: str | None = None,
    dest_day_offset: int | None = None,
    arr_day_offset: int | None = None,
) -> list[object]:
    # Live shopping legs are ~33 slots; clocks may omit a zero minute.
    return [
        None,
        None,
        operated_by,
        origin,
        origin_name,
        dest_name,
        dest,
        dest_day_offset,
        dep,
        arr_day_offset,
        arr,
        minutes,
        [],
        1,
        "28 in",
        None,
        1,
        "Airbus A321neo",
        None,
        False,
        dep_date,
        arr_date,
        [code, number, None, airline],
        None,
        None,
        1,
        None,
        None,
        None,
        None,
        "28 inches",
        54000,
        1,
    ]


def _live_flight(
    *,
    code: str,
    airline: str,
    legs: list[object],
    origin: str,
    dest: str,
    dep_date: list[int],
    dep: list[object],
    arr_date: list[int],
    arr: list[object],
    minutes: int,
    stops: int | None = None,
    layover: list[object] | None = None,
) -> list[object]:
    flight: list[object] = [None] * 25
    flight[0] = code
    flight[1] = [airline]
    flight[2] = legs
    flight[3] = origin
    flight[4] = dep_date
    flight[5] = dep
    flight[6] = dest
    flight[7] = arr_date
    flight[8] = arr
    flight[9] = minutes
    flight[10] = stops
    flight[12] = False
    flight[13] = layover
    return flight


def _priced(flight: list[object], price: int, bags: tuple[int, int] | None = None) -> list[object]:
    fare: list[object] = [[None, price], "tok"]
    if bags is not None:
        fare.append(list(bags))
    return [flight, fare]


def _tap_long_layover() -> list[object]:
    dep_date = [2026, 10, 9]
    arr_date = [2026, 10, 10]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="LIS",
            dest_name="Humberto Delgado Airport",
            dep=[13, 40],
            arr=[14, 5],
            minutes=85,
            dep_date=dep_date,
            arr_date=dep_date,
            number="1013",
        ),
        _live_leg(
            origin="LIS",
            origin_name="Humberto Delgado Airport",
            dest="OPO",
            dest_name="Francisco Sá Carneiro Airport",
            dep=[8, 5],
            arr=[9],
            minutes=55,
            dep_date=arr_date,
            arr_date=arr_date,
            number="1922",
            dest_day_offset=1,
        ),
    ]
    return _priced(
        _live_flight(
            code="TP",
            airline="Tap Air Portugal",
            legs=legs,
            origin="MAD",
            dest="OPO",
            dep_date=dep_date,
            dep=[13, 40],
            arr_date=arr_date,
            arr=[9],
            minutes=1220,
            stops=1,
            layover=[
                [
                    1080,
                    "LIS",
                    "LIS",
                    None,
                    "Humberto Delgado Airport",
                    "Lisbon",
                    "Humberto Delgado Airport",
                    "Lisbon",
                ]
            ],
        ),
        74,
    )


def _iberia_late_nonstop() -> list[object]:
    day = [2026, 9, 15]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="PMI",
            dest_name="Palma de Mallorca Airport",
            dep=[23, 10],
            arr=[0, 30],
            minutes=80,
            dep_date=day,
            arr_date=[2026, 9, 16],
            code="I2",
            number="3915",
            airline="Iberia Express",
            dest_day_offset=None,
            arr_day_offset=1,
        )
    ]
    return _priced(
        _live_flight(
            code="I2",
            airline="Iberia Express",
            legs=legs,
            origin="MAD",
            dest="PMI",
            dep_date=day,
            dep=[23, 10],
            arr_date=[2026, 9, 16],
            arr=[0, 30],
            minutes=80,
        ),
        88,
    )


def _iberia_fco_late_evening() -> list[object]:
    day = [2026, 9, 15]
    next_day = [2026, 9, 16]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="FCO",
            dest_name="Leonardo da Vinci International Airport",
            dep=[21, 50],
            arr=[24, 5],
            minutes=135,
            dep_date=day,
            arr_date=next_day,
            code="IB",
            number="3234",
            airline="Iberia",
            arr_day_offset=1,
        )
    ]
    return _priced(
        _live_flight(
            code="IB",
            airline="Iberia",
            legs=legs,
            origin="MAD",
            dest="FCO",
            dep_date=day,
            dep=[21, 50],
            arr_date=next_day,
            arr=[24, 5],
            minutes=135,
        ),
        79,
    )


def _ryanair_fco_late_evening() -> list[object]:
    day = [2026, 9, 15]
    next_day = [2026, 9, 16]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="FCO",
            dest_name="Leonardo da Vinci International Airport",
            dep=[22, 15],
            arr=[24, 30],
            minutes=135,
            dep_date=day,
            arr_date=next_day,
            code="FR",
            number="5994",
            airline="Ryanair",
            arr_day_offset=1,
        )
    ]
    flight = _live_flight(
        code="FR",
        airline="Ryanair",
        legs=legs,
        origin="MAD",
        dest="FCO",
        dep_date=day,
        dep=[22, 15],
        arr_date=next_day,
        arr=next_day,
        minutes=135,
    )
    return _priced(flight, 41)


def _iberia_fco_omitted_midnight_hour() -> list[object]:
    day = [2026, 9, 15]
    next_day = [2026, 9, 16]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="FCO",
            dest_name="Leonardo da Vinci International Airport",
            dep=[21, 50],
            arr=[None, 5],
            minutes=135,
            dep_date=day,
            arr_date=next_day,
            code="IB",
            number="3234",
            airline="Iberia",
            arr_day_offset=1,
        )
    ]
    return _priced(
        _live_flight(
            code="IB",
            airline="Iberia",
            legs=legs,
            origin="MAD",
            dest="FCO",
            dep_date=day,
            dep=[21, 50],
            arr_date=next_day,
            arr=[None, 5],
            minutes=135,
        ),
        79,
    )


def _ryanair_fco_omitted_midnight_hour() -> list[object]:
    day = [2026, 9, 15]
    next_day = [2026, 9, 16]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="FCO",
            dest_name="Leonardo da Vinci International Airport",
            dep=[22, 15],
            arr=[None, 30],
            minutes=135,
            dep_date=day,
            arr_date=next_day,
            code="FR",
            number="5994",
            airline="Ryanair",
            arr_day_offset=1,
        )
    ]
    flight = _live_flight(
        code="FR",
        airline="Ryanair",
        legs=legs,
        origin="MAD",
        dest="FCO",
        dep_date=day,
        dep=[22, 15],
        arr_date=next_day,
        arr=next_day,
        minutes=135,
    )
    return _priced(flight, 41)


def _two_stop_mad_icn() -> list[object]:
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="HEL",
            dest_name="Helsinki Airport",
            dep=[11, 0],
            arr=[16, 20],
            minutes=260,
            dep_date=[2026, 9, 22],
            arr_date=[2026, 9, 22],
            code="AY",
            number="1662",
            airline="Finnair",
        ),
        _live_leg(
            origin="HEL",
            origin_name="Helsinki Airport",
            dest="NRT",
            dest_name="Narita International Airport",
            dep=[17, 50],
            arr=[10, 15],
            minutes=565,
            dep_date=[2026, 9, 22],
            arr_date=[2026, 9, 23],
            code="AY",
            number="61",
            airline="Finnair",
            arr_day_offset=1,
        ),
        _live_leg(
            origin="NRT",
            origin_name="Narita International Airport",
            dest="ICN",
            dest_name="Incheon International Airport",
            dep=[12, 0],
            arr=[14, 30],
            minutes=150,
            dep_date=[2026, 9, 23],
            arr_date=[2026, 9, 23],
            code="JL",
            number="5237",
            airline="Japan Airlines",
        ),
    ]
    return _priced(
        _live_flight(
            code="AY",
            airline="Finnair",
            legs=legs,
            origin="MAD",
            dest="ICN",
            dep_date=[2026, 9, 22],
            dep=[11, 0],
            arr_date=[2026, 9, 23],
            arr=[14, 30],
            minutes=1290,
            stops=2,
            layover=[
                [
                    90,
                    "HEL",
                    "HEL",
                    None,
                    "Helsinki Airport",
                    "Helsinki",
                    "Helsinki Airport",
                    "Helsinki",
                ],
                [
                    105,
                    "NRT",
                    "NRT",
                    None,
                    "Narita International Airport",
                    "Tokyo",
                    "Narita International Airport",
                    "Tokyo",
                ],
            ],
        ),
        520,
    )


def _iberia_hour_only_arrival() -> list[object]:
    day = [2026, 10, 9]
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="OPO",
            dest_name="Francisco Sá Carneiro Airport",
            dep=[19, 40],
            arr=[20],
            minutes=80,
            dep_date=day,
            arr_date=day,
            code="IB",
            number="1153",
            airline="Iberia",
            operated_by="Air Nostrum for Iberia",
        )
    ]
    return _priced(
        _live_flight(
            code="IB",
            airline="Iberia",
            legs=legs,
            origin="MAD",
            dest="OPO",
            dep_date=day,
            dep=[19, 40],
            arr_date=day,
            arr=[20],
            minutes=80,
        ),
        95,
    )


def _longhaul_cz_hour_only_dep() -> list[object]:
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="CAN",
            dest_name="Guangzhou Baiyun International Airport",
            dep=[21],
            arr=[16, 10],
            minutes=790,
            dep_date=[2026, 9, 22],
            arr_date=[2026, 9, 23],
            code="CZ",
            number="378",
            airline="China Southern",
            arr_day_offset=1,
        ),
        _live_leg(
            origin="CAN",
            origin_name="Guangzhou Baiyun International Airport",
            dest="ICN",
            dest_name="Incheon International Airport",
            dep=[17, 20],
            arr=[21, 50],
            minutes=210,
            dep_date=[2026, 9, 23],
            arr_date=[2026, 9, 23],
            code="CZ",
            number="339",
            airline="China Southern",
            dest_day_offset=1,
        ),
    ]
    return _priced(
        _live_flight(
            code="CZ",
            airline="China Southern",
            legs=legs,
            origin="MAD",
            dest="ICN",
            dep_date=[2026, 9, 22],
            dep=[21],
            arr_date=[2026, 9, 23],
            arr=[21, 50],
            minutes=1070,
            stops=1,
            layover=[
                [
                    70,
                    "CAN",
                    "CAN",
                    None,
                    "Guangzhou Baiyun International Airport",
                    "Guangzhou",
                    "Guangzhou Baiyun International Airport",
                    "Guangzhou",
                ]
            ],
        ),
        314,
    )


def _longhaul_etihad() -> list[object]:
    legs = [
        _live_leg(
            origin="MAD",
            origin_name="Adolfo Suárez Madrid-Barajas Airport",
            dest="AUH",
            dest_name="Zayed International Airport",
            dep=[10, 45],
            arr=[19, 40],
            minutes=415,
            dep_date=[2026, 9, 22],
            arr_date=[2026, 9, 22],
            code="EY",
            number="102",
            airline="Etihad",
        ),
        _live_leg(
            origin="AUH",
            origin_name="Zayed International Airport",
            dest="ICN",
            dest_name="Incheon International Airport",
            dep=[21, 15],
            arr=[10, 55],
            minutes=520,
            dep_date=[2026, 9, 22],
            arr_date=[2026, 9, 23],
            code="EY",
            number="822",
            airline="Etihad",
            arr_day_offset=1,
        ),
    ]
    return _priced(
        _live_flight(
            code="EY",
            airline="Etihad",
            legs=legs,
            origin="MAD",
            dest="ICN",
            dep_date=[2026, 9, 22],
            dep=[10, 45],
            arr_date=[2026, 9, 23],
            arr=[10, 55],
            minutes=1030,
            stops=1,
            layover=[
                [
                    95,
                    "AUH",
                    "AUH",
                    None,
                    "Zayed International Airport",
                    "Abu Dhabi",
                    "Zayed International Airport",
                    "Abu Dhabi",
                ]
            ],
        ),
        420,
    )


def _error_response_body() -> str:
    wrb = [
        [
            "wrb.fr",
            None,
            None,
            None,
            None,
            [
                3,
                None,
                [
                    [
                        "type.googleapis.com/travel.frontend.flights.ErrorResponse",
                        [[None, [[1, 2, 3], None, None, None, None, [[0]]], 0, "x", "y"], 0],
                    ]
                ],
            ],
        ]
    ]
    raw = json.dumps(wrb, separators=(",", ":"))
    return f")]}}'\n\n{len(raw)}\n{raw}"


class LiveShapedCompactTests(unittest.TestCase):
    def test_tap_one_stop_hour_only_arrival_and_layover(self) -> None:
        card = parse_shopping_body(_compact_body(_tap_long_layover()))[0]
        self.assertEqual(card.airline, "Tap Air Portugal")
        self.assertEqual(card.departure, "13:40")
        self.assertEqual(card.arrival, "09:00")
        self.assertEqual(card.duration, "20 hr 20 min")
        self.assertEqual(card.stops, "1 stop")
        self.assertEqual(card.layover_city, "Lisbon")
        self.assertEqual(card.layover_hours, 18.0)
        self.assertEqual(card.flight_numbers, ("TP1013", "TP1922"))
        self.assertEqual(card.airline_codes, ("TP",))
        self.assertEqual(card.booking_token, "tok")
        offer = _normalize_offer(card, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.arrival, "09:00")
        self.assertEqual(offer.layover_city, "Lisbon")
        self.assertEqual(offer.layover_hours, 18.0)
        self.assertEqual(offer.flight_numbers, ("TP1013", "TP1922"))
        self.assertEqual(offer.booking_token, "tok")

    def test_layover_from_legs_when_itinerary_block_is_missing(self) -> None:
        item = _tap_long_layover()
        item[0][13] = None
        card = parse_shopping_body(_compact_body(item))[0]
        self.assertEqual(card.arrival, "09:00")
        self.assertEqual(card.layover_city, "LIS")
        self.assertEqual(card.layover_hours, 18.0)

    def test_iberia_hour_only_arrival_is_not_null(self) -> None:
        card = parse_shopping_body(_compact_body(_iberia_hour_only_arrival()))[0]
        self.assertEqual(card.departure, "19:40")
        self.assertEqual(card.arrival, "20:00")
        self.assertIsNone(card.layover_city)
        self.assertIsNone(card.layover_hours)

    def test_late_iberia_nonstop_keeps_next_day_arrival(self) -> None:
        card = parse_shopping_body(_compact_body(_iberia_late_nonstop()))[0]
        self.assertEqual(card.departure, "23:10")
        self.assertEqual(card.arrival, "00:30")
        self.assertEqual(card.stops, "Nonstop")

    def test_late_evening_mad_fco_arrivals_are_not_null(self) -> None:
        iberia = parse_shopping_body(_compact_body(_iberia_fco_late_evening()))[0]
        self.assertEqual(iberia.airline, "Iberia")
        self.assertEqual(iberia.departure, "21:50")
        self.assertEqual(iberia.arrival, "00:05")
        ryanair = parse_shopping_body(_compact_body(_ryanair_fco_late_evening()))[0]
        self.assertEqual(ryanair.airline, "Ryanair")
        self.assertEqual(ryanair.departure, "22:15")
        self.assertEqual(ryanair.arrival, "00:30")

    def test_fco_late_bench_fixtures_keep_arrivals_in_json(self) -> None:
        root = Path(__file__).resolve().parent / "bench"
        cases = (
            ("shopping-iberia-fco-late.wrb", "Iberia", "21:50", "00:05"),
            ("shopping-ryanair-fco-late.wrb", "Ryanair", "22:15", "00:30"),
        )
        for name, airline, dep, arr in cases:
            with self.subTest(name=name):
                card = parse_shopping_body((root / name).read_text(encoding="utf-8"))[0]
                self.assertEqual(card.airline, airline)
                self.assertEqual(card.departure, dep)
                self.assertEqual(card.arrival, arr)
                offer = _normalize_offer(card, max_stops=1)
                assert offer is not None
                data = offer.to_dict()
                self.assertEqual(data["departure"], dep)
                self.assertEqual(data["arrival"], arr)
                self.assertEqual(data["legs"][0]["arrival"], arr)

    def test_omitted_midnight_hour_is_not_null(self) -> None:
        iberia = parse_shopping_body(_compact_body(_iberia_fco_omitted_midnight_hour()))[0]
        self.assertEqual(iberia.departure, "21:50")
        self.assertEqual(iberia.arrival, "00:05")
        ryanair = parse_shopping_body(_compact_body(_ryanair_fco_omitted_midnight_hour()))[0]
        self.assertEqual(ryanair.departure, "22:15")
        self.assertEqual(ryanair.arrival, "00:30")
        offer = _normalize_offer(ryanair, max_stops=1)
        assert offer is not None
        self.assertEqual(offer.to_dict()["arrival"], "00:30")

    def test_two_stop_card_keeps_both_layover_cities(self) -> None:
        card = parse_shopping_body(_compact_body(_two_stop_mad_icn()))[0]
        self.assertEqual(card.stops, "2 stops")
        offer = _normalize_offer(card, max_stops=2)
        assert offer is not None
        self.assertEqual(offer.stops_count, 2)
        cities = [row.city for row in offer.legs[0].layovers]
        self.assertEqual(cities, ["Helsinki", "Tokyo"])
        data = offer.to_dict()
        self.assertIsNone(data["layover_city"])
        self.assertEqual(
            [row["city"] for row in data["legs"][0]["layovers"]], ["Helsinki", "Tokyo"]
        )

    def test_longhaul_group_starting_with_hour_only_departure_parses(self) -> None:
        body = _compact_body(_longhaul_cz_hour_only_dep(), other=(_longhaul_etihad(),))
        cards = parse_shopping_body(body)
        self.assertEqual(len(cards), 2)
        by_airline = {card.airline: card for card in cards}
        cz = by_airline["China Southern"]
        self.assertEqual(cz.departure, "21:00")
        self.assertEqual(cz.arrival, "21:50")
        self.assertEqual(cz.duration, "17 hr 50 min")
        self.assertEqual(cz.stops, "1 stop")
        self.assertEqual(cz.price, "€314")
        self.assertEqual(cz.layover_city, "Guangzhou")
        self.assertAlmostEqual(cz.layover_hours or 0, 70 / 60)
        ey = by_airline["Etihad"]
        self.assertEqual(ey.departure, "10:45")
        self.assertEqual(ey.arrival, "10:55")
        self.assertEqual(ey.layover_city, "Abu Dhabi")

    def test_hour_only_departure_as_only_best_itinerary_is_not_empty(self) -> None:
        cards = parse_shopping_body(_compact_body(_longhaul_cz_hour_only_dep()))
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].airline, "China Southern")
        self.assertEqual(cards[0].departure, "21:00")

    def test_shopping_error_response_is_rejected_not_a_compact_miss(self) -> None:
        with self.assertRaises(ShoppingRejected):
            parse_shopping_body(_error_response_body())

    def test_source_does_not_download_html_after_shopping_reject(self) -> None:
        client = _FakeSweepClient(post_text=_error_response_body())
        source = GoogleFlightsHttpSource(client=client)
        with self.assertRaises(GoogleFlightsRejected):
            source.fetch(FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1))
        self.assertEqual(client.gets, [])


def _calendar_rpc_body(rows: list[list[object]]) -> str:
    data = [None, rows]
    wrb = [["wrb.fr", None, json.dumps(data, separators=(",", ":"))]]
    raw = json.dumps(wrb, separators=(",", ":"))
    return f")]}}'\n\n{len(raw)}\n{raw}"


class _MuxFakeSweepClient:
    """Owned transport: optional post_many with one RTT for the whole batch."""

    def __init__(
        self,
        *,
        shop_text: str,
        calendar_text: str = "not-calendar",
        rtt: float = 0.04,
    ) -> None:
        self.shop_text = shop_text
        self.calendar_text = calendar_text
        self.rtt = rtt
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.post_many_calls = 0

    def _response(self, url: str) -> SweepHttpResponse:
        if "GetCalendarGrid" in url:
            return SweepHttpResponse(200, self.calendar_text, url)
        return SweepHttpResponse(200, self.shop_text, url)

    def post(
        self,
        url: str,
        *,
        data: str,
        headers: object,
        timeout: float,
    ) -> SweepHttpResponse:
        time.sleep(self.rtt)
        self.posts.append(url)
        return self._response(url)

    def post_many(self, jobs, *, timeout: float) -> list[SweepHttpResponse]:
        time.sleep(self.rtt)
        self.post_many_calls += 1
        for job in jobs:
            self.posts.append(job.url)
        return [self._response(job.url) for job in jobs]

    def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
        self.gets.append(url)
        return SweepHttpResponse(200, "<html></html>", url)

    def close(self) -> None:
        return None


class SweepClientShapeTests(unittest.TestCase):
    def test_fetch_with_calendar_uses_one_multiplex_round(self) -> None:
        shop = _compact_body(_itinerary(price=88, airline="Iberia"))
        calendar = _calendar_rpc_body(
            [
                ["2026-09-01", None, [[None, 80], "tok"], 1],
                ["2026-09-02", None, [[None, 90], "tok"], 1],
                ["2026-09-03", None, [[None, 100], "tok"], 1],
            ]
        )
        client = _MuxFakeSweepClient(shop_text=shop, calendar_text=calendar, rtt=0.04)
        source = GoogleFlightsHttpSource(client=client)
        started = time.perf_counter()
        cards, days = source.fetch_with_calendar(
            FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1),
            date(2026, 9, 1),
            date(2026, 9, 3),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"sweep_client_ms={elapsed_ms:.1f}")
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(len(days), 3)
        self.assertEqual(client.post_many_calls, 1)
        self.assertEqual(len(client.posts), 2)
        self.assertLess(elapsed_ms, 70)

    def test_fetch_many_multiplexes_calendar_day_fanout(self) -> None:
        shop = _compact_body(_itinerary(price=45, airline="Vueling"))
        client = _MuxFakeSweepClient(shop_text=shop, rtt=0.04)
        source = GoogleFlightsHttpSource(client=client)
        trips = tuple(
            FlightQuery("MAD", "BCN", date(2026, 9, day), max_stops=1) for day in range(1, 8)
        )
        started = time.perf_counter()
        results = source.fetch_many(trips)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"sweep_client_fanout_ms={elapsed_ms:.1f}")
        self.assertEqual(len(results), 7)
        self.assertTrue(all(not isinstance(item, BaseException) for item in results))
        self.assertEqual(results[0][0].airline, "Vueling")
        self.assertEqual(client.post_many_calls, 1)
        self.assertEqual(len(client.posts), 7)
        self.assertLess(elapsed_ms, 70)

    def test_fetch_many_with_calendar_uses_one_multiplex_round(self) -> None:
        shop = _compact_body(_itinerary(price=88, airline="Iberia"))
        calendar = _calendar_rpc_body(
            [
                ["2026-09-01", None, [[None, 80], "tok"], 1],
                ["2026-09-02", None, [[None, 90], "tok"], 1],
                ["2026-09-03", None, [[None, 100], "tok"], 1],
            ]
        )
        client = _MuxFakeSweepClient(shop_text=shop, calendar_text=calendar, rtt=0.04)
        source = GoogleFlightsHttpSource(client=client)
        jobs = tuple(
            (
                FlightQuery("MAD", dest, date(2026, 9, 1), max_stops=1),
                date(2026, 9, 1),
                date(2026, 9, 3),
            )
            for dest in ("BCN", "LHR", "CDG")
        )
        started = time.perf_counter()
        results = source.fetch_many_with_calendar(jobs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"sweep_client_batch_ms={elapsed_ms:.1f}")
        self.assertEqual(len(results), 3)
        cards, days = results[0]
        self.assertFalse(isinstance(cards, BaseException))
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(len(days), 3)
        self.assertEqual(client.post_many_calls, 1)
        self.assertEqual(len(client.posts), 6)
        self.assertLess(elapsed_ms, 70)

    def test_http_sources_reuse_the_process_tls_session(self) -> None:
        created: list[object] = []

        class FakeChrome:
            def __init__(self) -> None:
                created.append(self)

            def close(self) -> None:
                return None

            def post(
                self,
                url: str,
                *,
                data: str,
                headers: object,
                timeout: float,
            ) -> SweepHttpResponse:
                return SweepHttpResponse(200, _compact_body(_itinerary(price=10)), url)

            def get(self, url: str, *, timeout: float) -> SweepHttpResponse:
                return SweepHttpResponse(200, "<html></html>", url)

        with patch("viajante.google_flights.ChromeSweepClient", FakeChrome):
            reset_shared_chrome_sweep_client()
            first = GoogleFlightsHttpSource()
            second = GoogleFlightsHttpSource()
            query = FlightQuery("MAD", "BCN", date(2026, 9, 1), max_stops=1)
            first.fetch(query)
            second.fetch(query)
            self.assertIs(shared_chrome_sweep_client(), created[0])
            self.assertEqual(len(created), 1)
            first.close()
            second.close()
            third = GoogleFlightsHttpSource()
            third.fetch(query)
            self.assertEqual(len(created), 1)
            reset_shared_chrome_sweep_client()


if __name__ == "__main__":
    unittest.main()
