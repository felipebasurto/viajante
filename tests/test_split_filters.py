from __future__ import annotations

import unittest
from dataclasses import replace

import _isolate  # noqa: F401
from test_split import DAY, NEXT, FakeSearch, _hub_table, _offer, _packaged_via, _segment
from viajante.flight_filters import OfferFilters
from viajante.models import FlightQuery, RawJourneyLeg
from viajante.split import SplitItinerary, SplitPart, _rank, search_split_tickets
from viajante.split_filters import SplitFilters, passes

# The hub table: JFK-LAX 08:00-11:30, a 3.5 h connection at LAX, LAX-NRT 15:00-23:00. Every
# fixture ticket is 5 h long (the helper fixes duration_hours).


def _split(filters: SplitFilters | None = None, **kwargs) -> tuple[object, int]:
    report = search_split_tickets(
        FlightQuery("JFK", "NRT", DAY),
        packaged=_packaged_via("LAX"),
        via=["LAX"],
        search=FakeSearch(_hub_table()),
        filters=filters,
        **kwargs,
    )
    return report, report.rejected.get("filter", 0)


def _kept(filters: SplitFilters | None) -> int:
    report, _ = _split(filters)
    return len(report.itineraries)


class HubFilterTests(unittest.TestCase):
    def test_no_filter_keeps_the_pair(self) -> None:
        self.assertEqual(_kept(None), 1)
        self.assertEqual(_kept(SplitFilters()), 1)

    def test_departure_window_and_depart_after(self) -> None:
        self.assertEqual(_kept(SplitFilters(OfferFilters(depart_window=(420, 540)))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(depart_window=(720, 840)))), 0)
        self.assertEqual(_kept(SplitFilters(OfferFilters(depart_after=7 * 60))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(depart_after=9 * 60))), 0)

    def test_arrive_before_reads_the_last_arrival(self) -> None:
        self.assertEqual(_kept(SplitFilters(OfferFilters(arrive_before=23 * 60 + 30))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(arrive_before=22 * 60))), 0)

    def test_max_duration_applies_to_each_ticket(self) -> None:
        # Each fixture ticket is 5 h: a 4.5 h cap drops the pair and a 5 h cap keeps it.
        self.assertEqual(_kept(SplitFilters(OfferFilters(max_duration_hours=4.5))), 0)
        self.assertEqual(_kept(SplitFilters(OfferFilters(max_duration_hours=5))), 1)

    def test_layover_bounds_read_the_hub_connection(self) -> None:
        # The connection is 3.5 h.
        self.assertEqual(_kept(SplitFilters(OfferFilters(min_layover_hours=4))), 0)
        self.assertEqual(_kept(SplitFilters(OfferFilters(max_layover_hours=3))), 0)
        self.assertEqual(
            _kept(SplitFilters(OfferFilters(min_layover_hours=3, max_layover_hours=4))), 1
        )

    def test_via_and_exclude_via_name_the_hub(self) -> None:
        self.assertEqual(_kept(SplitFilters(OfferFilters(via=("LAX",)))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(via=("SFO",)))), 0)
        self.assertEqual(_kept(SplitFilters(OfferFilters(exclude_via=("LAX",)))), 0)
        self.assertEqual(_kept(SplitFilters(OfferFilters(exclude_via=("SFO",)))), 1)

    def test_overnight_lists_name_the_hub(self) -> None:
        # The hub connection is the same day, so no overnight at LAX.
        self.assertEqual(_kept(SplitFilters(OfferFilters(no_overnight=("LAX",)))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(require_overnight=("LAX",)))), 0)

    def test_any_overnight_matches_every_hub(self) -> None:
        # The same-day LAX connection has no overnight stop, so "any" means no overnight here.
        self.assertEqual(_kept(SplitFilters(OfferFilters(no_overnight=("any",)))), 1)
        self.assertEqual(_kept(SplitFilters(OfferFilters(require_overnight=("any",)))), 0)

    def test_any_overnight_drops_a_real_overnight_hub_connection(self) -> None:
        table = _hub_table()
        table[("JFK", "LAX", DAY)] = [
            _offer(200.0, (_segment("JFK", "LAX", "18:00", "21:30"),), airline="A")
        ]
        table[("LAX", "NRT", NEXT)] = [
            _offer(450.0, (_segment("LAX", "NRT", "11:00", "19:00", on=NEXT),), airline="C")
        ]

        def kept(filters: SplitFilters) -> int:
            report = search_split_tickets(
                FlightQuery("JFK", "NRT", DAY),
                packaged=_packaged_via("LAX"),
                allow_overnight=True,
                search=FakeSearch(table),
                filters=filters,
            )
            return len(report.itineraries)

        self.assertEqual(kept(SplitFilters()), 1)
        self.assertEqual(kept(SplitFilters(OfferFilters(no_overnight=("any",)))), 0)
        self.assertEqual(kept(SplitFilters(OfferFilters(require_overnight=("any",)))), 1)

    def test_airports_exclude_any_named_airport_and_include_the_destination(self) -> None:
        self.assertEqual(_kept(SplitFilters(exclude_airports=("LAX",))), 0)
        self.assertEqual(_kept(SplitFilters(exclude_airports=("SFO",))), 1)
        self.assertEqual(_kept(SplitFilters(include_airports=("NRT",))), 1)
        self.assertEqual(_kept(SplitFilters(include_airports=("HND",))), 0)

    def test_dropped_pairs_are_counted_as_filtered(self) -> None:
        report, filtered = _split(SplitFilters(OfferFilters(via=("SFO",))))
        self.assertEqual(report.itineraries, ())
        self.assertEqual(filtered, 1)


class BaggageBufferRankingTests(unittest.TestCase):
    def test_the_buffer_ranks_a_pair_but_never_changes_its_total(self) -> None:
        base, _ = _split()
        plain = base.itineraries[0]
        cheaper = replace(plain, parts=(plain.parts[0], _second_at(plain.parts[1], 490.0, 30)))
        self.assertEqual(cheaper.total, 690.0)
        self.assertEqual(cheaper.baggage_buffer_total, 30)
        ranked, _ = _rank([cheaper, plain], "USD", 2)
        self.assertIs(ranked[0], plain)
        ranked, _ = _rank([cheaper], "USD", 2)
        self.assertEqual(ranked[0].total, 690.0)


def _second_at(part: SplitPart, price: float, buffer: int) -> SplitPart:
    offer = replace(
        part.offer,
        price=price,
        price_text=f"{price:.0f}",
        baggage_buffer=buffer,
        needs_bag_verify=True,
    )
    return replace(part, offer=offer)


class PassesDirectTests(unittest.TestCase):
    def _hub_itinerary(self, **overrides) -> SplitItinerary:
        report, _ = _split()
        return replace(report.itineraries[0], **overrides)

    def test_unknown_clock_cannot_prove_a_named_departure(self) -> None:
        itinerary = self._hub_itinerary()
        blank = (RawJourneyLeg(departure=None, arrival=None, duration=None, stops=None),)
        first = replace(itinerary.parts[0], offer=replace(itinerary.parts[0].offer, legs=blank))
        broken = replace(itinerary, parts=(first, itinerary.parts[1]))
        self.assertTrue(passes(itinerary, SplitFilters()))
        self.assertFalse(passes(broken, SplitFilters(OfferFilters(depart_after=0))))
        self.assertFalse(passes(broken, SplitFilters(OfferFilters(depart_window=(0, 1439)))))

    def test_connection_filters_drop_every_mixed_pair(self) -> None:
        query = FlightQuery("JFK", "NRT", DAY)
        offer = _offer(300.0, (_segment("JFK", "NRT", "08:00", "20:00"),))
        mixed = SplitItinerary("mixed_one_ways", (SplitPart("outbound", query, offer, "USD"),))
        self.assertTrue(passes(mixed, SplitFilters()))
        self.assertFalse(passes(mixed, SplitFilters(OfferFilters(min_layover_hours=1))))
        self.assertFalse(passes(mixed, SplitFilters(OfferFilters(via=("LAX",)))))
        self.assertTrue(passes(mixed, SplitFilters(OfferFilters(depart_after=7 * 60))))


if __name__ == "__main__":
    unittest.main()
