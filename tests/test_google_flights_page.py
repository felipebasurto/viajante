from __future__ import annotations

import base64
import json
import unittest
from datetime import date

import _isolate  # noqa: F401
from viajante.google_flights_page import extract_ds1_data, parse_shopping_page
from viajante.google_flights_rpc import (
    CompactParseMiss,
    EmptyShoppingResults,
    ShoppingErrorFrame,
    error_frame_status,
    raw_rpc_error_status,
    rpc_error_status,
)
from viajante.models import RawJourneyLeg, RawSegment, RoundTrip
from viajante.tfs import encode_tfs_selected_outbound


def _itinerary() -> list[object]:
    flight = ["IB", ["Iberia"], [[]], "MAD", None, [8, 40], "BCN", None, [10, 0], 80]
    return [flight, [[None, 129], "owned-token"]]


def _page(data: object, *, props: str = "") -> str:
    script = f"AF_initDataCallback({{key:'ds:1',{props}data:{json.dumps(data)}}});"
    return (
        f'<html><script class="other">{script}</script>'
        f'<script class="ds:1">{script}</script></html>'
    )


class GoogleFlightsPageTests(unittest.TestCase):
    def test_extracts_balanced_ds1_array_without_evaluating_javascript(self) -> None:
        data = [None, None, ["quoted ] ) } text", 1]]
        html = _page(data, props="hash:'brace } and comma, data: nope', nested:{data:[0]},")
        self.assertEqual(extract_ds1_data(html), data)

    def test_public_page_data_uses_the_owned_shopping_decoder(self) -> None:
        data = [None, None, [[_itinerary()], None, False], [[], 0, False]]
        cards = parse_shopping_page(_page(data), currency="EUR")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].airline, "Iberia")
        self.assertEqual(cards[0].price, "€129")
        self.assertEqual(cards[0].booking_token, "owned-token")

    def test_segments_carry_the_operating_carrier_and_their_own_duration(self) -> None:
        # Live shape, 2026-11-04 (JFK-BOS): leg[2] is the operating carrier's text, only
        # when it differs from the marketing carrier; leg[11] is the segment in minutes.
        def leg(operated_by: object, minutes: object) -> list[object]:
            row: list[object] = [None] * 23
            row[2] = operated_by
            row[3] = "JFK"
            row[6] = "BOS"
            row[8] = [8, 0]
            row[10] = [9, 35]
            row[11] = minutes
            row[22] = ["AA", "4618", None, "American"]
            return row

        def page_for(*legs: list[object]) -> tuple:
            flight: list[object] = ["AA", ["American"], list(legs), "JFK", None, [8, 0], "BOS"]
            flight += [None, [9, 35], 95]
            data = [None, None, [[[flight, [[None, 129], "tok"]]], None, False], [[], 0, False]]
            return parse_shopping_page(_page(data), currency="USD")

        (card,) = page_for(leg("Republic Airways as American Eagle", 95))
        segment = card.legs[0].segments[0]
        self.assertEqual(segment.operated_by, "Republic Airways as American Eagle")
        self.assertEqual(segment.duration_minutes, 95)
        self.assertEqual(segment.to_dict()["operated_by"], "Republic Airways as American Eagle")
        self.assertEqual(segment.to_dict()["duration_minutes"], 95)

        (card,) = page_for(leg(None, None))
        segment = card.legs[0].segments[0]
        self.assertIsNone(segment.operated_by)
        self.assertIsNone(segment.duration_minutes)
        self.assertNotIn("operated_by", segment.to_dict())
        self.assertNotIn("duration_minutes", segment.to_dict())

        (card,) = page_for(leg("  ", True))
        self.assertIsNone(card.legs[0].segments[0].operated_by)
        self.assertIsNone(card.legs[0].segments[0].duration_minutes)

    def test_recognized_empty_slots_remain_empty_not_markup_drift(self) -> None:
        empty = [None, None, [[], None, False], [[], 0, False]]
        with self.assertRaises(EmptyShoppingResults):
            parse_shopping_page(_page(empty), currency="EUR")

    def test_rendered_page_with_null_itinerary_slots_is_empty_not_markup_drift(self) -> None:
        # Live shape for a nonstop search on a route with no nonstop flights: the page
        # echoes the route and the filter catalog, and both itinerary slots are null.
        header = [None, [[1, 2, 3], None, None, None, None, [[1]]], 0, "req"]
        route_echo = [[[[["BCN", 0], "Barcelona"]], [[["NRT", 0], "Narita"]]]]
        catalog = [[[None, 496], [None, 4725]], [[["ONEWORLD", "Oneworld"]]]]
        empty = [header, route_echo, None, None, None, None, None, catalog] + [None] * 16
        with self.assertRaises(EmptyShoppingResults):
            parse_shopping_page(_page(empty), currency="EUR")

    def test_null_itinerary_slots_without_a_rendered_frame_stay_markup_drift(self) -> None:
        for data in (
            [None, None, None, None],
            [[None], None, None, None, None, None, None, [[]]],
            [[None], [[]], None, None, None, None, None, None],
        ):
            with self.subTest(data=data), self.assertRaises(CompactParseMiss):
                parse_shopping_page(_page(data), currency="EUR")

    def test_provider_error_frame_is_an_error_frame_not_markup_drift(self) -> None:
        # Live shape, captured 2026-10-16: the page answers a burst of day shops
        # with ds:1 = [13, null, [[...ErrorResponse...]]] instead of results.
        frame = [
            13,
            None,
            [
                [
                    "type.googleapis.com/travel.frontend.flights.ErrorResponse",
                    [[None, [[1, 2, 3], None, None, None, None, [[0]]], 0, "x", "y"], 0],
                ]
            ],
        ]
        with self.assertRaises(ShoppingErrorFrame) as caught:
            parse_shopping_page(_page(frame), currency="EUR")
        self.assertEqual(caught.exception.status, 13)
        self.assertIsInstance(caught.exception, CompactParseMiss)

    def test_error_frame_status_requires_the_exact_frame_shape(self) -> None:
        # Captured shape: the ErrorResponse type string sits at data[2][0][0].
        payload = [["type.googleapis.com/travel.frontend.flights.ErrorResponse", []]]
        self.assertEqual(error_frame_status([13, None, payload]), 13)
        self.assertEqual(error_frame_status([3, None, payload]), 3)
        for data in (
            [13, None, None],  # no payload
            [13, None, [["other.type", []]]],  # no ErrorResponse marker
            [13, "x", payload],  # separator slot not null
            [True, None, payload],  # bool is not a status int
            [None, None, payload],  # no status
            [13, None, payload, None],  # four items is not the frame
            [None, None, [[], None, False], [[], 0, False]],  # empty results frame
        ):
            with self.subTest(data=data):
                self.assertIsNone(error_frame_status(data))

    def test_raw_rpc_status_keeps_errorresponse_visible(self) -> None:
        error_response = ')]}\'\n\n[["wrb.fr",null,null,null,null,[13],"generic"]]'
        self.assertEqual(raw_rpc_error_status(error_response), 13)
        rejected = ")]}'\n\n" + json.dumps(
            [
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
                                [
                                    [None, [[1, 2, 3], None, None, None, None, [[0]]], 0, "x", "y"],
                                    0,
                                ],
                            ]
                        ],
                    ],
                ]
            ]
        )
        self.assertEqual(raw_rpc_error_status(rejected), 3)
        self.assertIsNone(rpc_error_status(rejected))

    def test_missing_or_malformed_ds1_is_a_parse_miss(self) -> None:
        for html in (
            "<html><script class='other'>AF_initDataCallback({key:'ds:1',data:[]})</script></html>",
            "<script class='ds:1'>AF_initDataCallback({key:'ds:1',data:[1,})</script>",
            "<script class='ds:1'>AF_initDataCallback({key:'ds:1',data:'[]'})</script>",
        ):
            with self.subTest(html=html), self.assertRaises(CompactParseMiss):
                parse_shopping_page(html, currency="EUR")


def _varint(data: bytes, index: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, index
        shift += 7


def _fields(data: bytes) -> dict[int, list[object]]:
    result: dict[int, list[object]] = {}
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(data, index)
        else:
            length, index = _varint(data, index)
            value = data[index : index + length]
            index += length
        result.setdefault(field, []).append(value)
    return result


class SelectedOutboundTfsTests(unittest.TestCase):
    def test_encodes_observed_physical_segments_and_return_route(self) -> None:
        trip = RoundTrip("MAD", "SIN", date(2026, 11, 14), date(2026, 11, 25), adults=2)
        outbound = RawJourneyLeg(
            "08:00",
            "23:00",
            segments=(
                RawSegment(
                    "MAD",
                    "DOH",
                    flight_number="TR533",
                    departure_date=date(2026, 11, 14),
                    carrier="TR",
                ),
                RawSegment(
                    "DOH",
                    "SIN",
                    flight_number="TR539",
                    departure_date=date(2026, 11, 14),
                    carrier="TR",
                ),
            ),
        )
        fields = _fields(base64.b64decode(encode_tfs_selected_outbound(trip, outbound)))
        self.assertEqual(len(fields[3]), 2)
        self.assertEqual(fields[19], [1])
        first_leg = _fields(fields[3][0])
        selected = [_fields(raw) for raw in first_leg[4]]
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[0][1], [b"MAD"])
        self.assertEqual(selected[0][2], [b"2026-11-14"])
        self.assertEqual(selected[0][3], [b"DOH"])
        self.assertEqual(selected[0][5], [b"TR"])
        self.assertEqual(selected[0][6], [b"533"])

    def test_rejects_non_contiguous_or_undated_selected_routes(self) -> None:
        trip = RoundTrip("MAD", "SIN", date(2026, 11, 14), date(2026, 11, 25))
        broken = RawJourneyLeg(
            "08:00",
            "23:00",
            segments=(
                RawSegment(
                    "MAD",
                    "DOH",
                    flight_number="TR533",
                    departure_date=date(2026, 11, 14),
                    carrier="TR",
                ),
                RawSegment(
                    "DXB",
                    "SIN",
                    flight_number="TR539",
                    departure_date=date(2026, 11, 14),
                    carrier="TR",
                ),
            ),
        )
        with self.assertRaisesRegex(ValueError, "not contiguous"):
            encode_tfs_selected_outbound(trip, broken)
        undated = RawJourneyLeg("08:00", "23:00", segments=(RawSegment("MAD", "SIN"),))
        with self.assertRaisesRegex(ValueError, "lacks route or departure date"):
            encode_tfs_selected_outbound(trip, undated)


if __name__ == "__main__":
    unittest.main()
