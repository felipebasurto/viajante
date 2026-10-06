from __future__ import annotations

import json
import random
import unittest
from datetime import date, datetime, timedelta
from typing import Optional
from unittest.mock import patch

from test_flights import FakeSource, card
from test_trip import _card as trip_card
from test_trip import _search_trip_cards
from viajante.envelope import stamp_search
from viajante.flights import _recommend, _run_search, parse_offer_filters
from viajante.mcp_guide import GUIDE
from viajante.mcp_handlers import search_flights_tool
from viajante.models import FlightOffer, FlightQuery, OfferEvidence, QuerySuccess, RoundTrip
from viajante.recommend import (
    RELAX_ORDER,
    SCORE_WEIGHTS,
    Requirements,
    departure_slot,
    recommend_offers,
)


def make(
    *,
    airline: Optional[str] = "Air",
    dep: Optional[str] = "08:00",
    arr: Optional[str] = "15:00",
    price: float = 600.0,
    hours: Optional[float] = 7.0,
    stops: Optional[int] = 0,
    layover: Optional[tuple[str, Optional[float]]] = None,
    bags: Optional[int] = None,
    carry_on: Optional[int] = None,
    buffer: int = 0,
    currency: Optional[str] = None,
) -> FlightOffer:
    text = {None: None, 0: "Nonstop", 1: "1 stop"}.get(stops, f"{stops} stops")
    duration = None if hours is None else f"{int(hours)} hr {round(hours % 1 * 60)} min"
    evidence = None
    if currency:
        evidence = OfferEvidence(
            evidence_id=f"gf_{currency}_{price}_{dep}",
            query={"trip": "one-way"},
            currency=currency,
            retrieved_at=datetime(2026, 8, 10),
            fetch_backend="sweep",
            query_url=None,
            offer_url=None,
            url_kind="none",
        )
    return FlightOffer(
        airline=airline,
        departure=dep,
        arrival=arr,
        price_text=f"${price:.0f}",
        price=price,
        duration=duration,
        duration_hours=hours,
        stops=text,
        stops_count=stops,
        baggage_buffer=buffer,
        needs_bag_verify=buffer > 0,
        layover_city=layover[0] if layover else None,
        layover_hours=layover[1] if layover else None,
        checked_bags=bags,
        carry_on=carry_on,
        evidence=evidence,
    )


def day() -> list[FlightOffer]:
    """One owned day: three nonstop slots and two one-stops, single currency."""
    return [
        make(airline="Delta", dep="08:00", arr="15:00", price=620, hours=7.0, stops=0),
        make(airline="United", dep="07:15", arr="14:15", price=640, hours=7.0, stops=0),
        make(airline="Aer", dep="14:00", arr="21:30", price=700, hours=7.5, stops=0),
        make(
            airline="Iberia",
            dep="19:30",
            arr="07:30",
            price=480,
            hours=10.0,
            stops=1,
            layover=("DUB", 2.0),
        ),
        make(
            airline="Icelandair",
            dep="06:30",
            arr="19:30",
            price=450,
            hours=12.0,
            stops=1,
            layover=("KEF", 1.5),
        ),
    ]


def recommend(offers, requirements=None, **kwargs):
    kwargs.setdefault("currency", "USD")
    return recommend_offers(offers, requirements, **kwargs)


def signatures(rec) -> list[tuple]:
    return [(e.offer.stops_count, departure_slot(e.offer)) for e in rec.entries]


class RequirementsMetTests(unittest.TestCase):
    def test_all_requirements_met_reports_no_relaxation(self) -> None:
        rec = recommend(
            day(), Requirements(max_stops=0, depart_window=(360, 720), max_duration=8.0)
        )
        self.assertEqual(rec.relaxed_requirements, ())
        top = rec.entries[0]
        self.assertEqual(top.labels[0], "recommended")
        self.assertEqual(
            dict(top.requirements),
            {"depart_window": "met", "max_duration": "met", "max_stops": "met"},
        )
        self.assertEqual(top.offer.stops_count, 0)
        self.assertEqual(departure_slot(top.offer), "morning")
        for entry in rec.entries:
            self.assertNotIn("unmet", entry.requirements.values())

    def test_no_stated_requirements_still_recommends(self) -> None:
        rec = recommend(day())
        self.assertEqual(rec.relaxed_requirements, ())
        self.assertEqual(dict(rec.requirements.to_dict()), {})
        self.assertIn("recommended", rec.entries[0].labels)

    def test_no_offers_no_recommendation(self) -> None:
        self.assertIsNone(recommend([]))


class RelaxationTests(unittest.TestCase):
    def test_fewest_relaxed_then_fixed_order_on_ties(self) -> None:
        offers = [o for o in day() if o.departure != "22:00"]
        # Nonstops depart before 18:00 and the only evening flight has a stop.
        # Relaxing depart_after or max_stops each suffices; depart_after is earlier in the order.
        rec = recommend(offers, Requirements(max_stops=0, depart_after=18 * 60))
        self.assertEqual(rec.relaxed_requirements, ("depart_after",))
        self.assertEqual(rec.entries[0].offer.stops_count, 0)
        self.assertEqual(rec.entries[0].requirements["depart_after"], "unmet")
        self.assertEqual(rec.entries[0].requirements["max_stops"], "met")
        self.assertIn("depart_after", rec.notes[0])

    def test_relaxed_requirement_is_visible_on_every_entry(self) -> None:
        offers = [make(stops=1, layover=("DUB", 2.0), bags=0), make(stops=1, dep="12:00", bags=0)]
        rec = recommend(offers, Requirements(max_stops=0, bags=1))
        self.assertEqual(rec.relaxed_requirements, ("bags", "max_stops"))
        self.assertEqual(
            [name for name in RELAX_ORDER if name in rec.relaxed_requirements],
            list(rec.relaxed_requirements),
        )
        for entry in rec.entries:
            self.assertEqual(entry.requirements["max_stops"], "unmet")
            self.assertEqual(entry.requirements["bags"], "unmet")
            self.assertIn("Does not meet requested max_stops 0", entry.tradeoffs)
            self.assertIn("Does not meet requested bags 1", entry.tradeoffs)

    def test_relax_disabled_returns_none_when_unmet(self) -> None:
        offers = [make(stops=1)]
        self.assertIsNone(recommend(offers, Requirements(max_stops=0), relax=False))

    def test_unknown_stop_count_cannot_prove_direct_only(self) -> None:
        rec = recommend([make(stops=None)], Requirements(max_stops=0))
        self.assertEqual(rec.relaxed_requirements, ("max_stops",))

    def test_unknown_bag_is_unconfirmed_not_unmet(self) -> None:
        rec = recommend([make(bags=None), make(bags=0, dep="12:00")], Requirements(bags=1))
        self.assertEqual(rec.relaxed_requirements, ())
        entry = rec.entries[0]
        self.assertEqual(entry.requirements["bags"], "unknown")
        self.assertIn("Requested bags 1 is not confirmed on this card", entry.tradeoffs)
        self.assertTrue(all(e.requirements["bags"] != "unmet" for e in rec.entries))

    def test_known_bag_shortfall_is_unmet(self) -> None:
        offers = [make(bags=2, price=700), make(bags=0, price=400, dep="12:00")]
        rec = recommend(offers, Requirements(bags=1))
        self.assertEqual(rec.entries[0].offer.checked_bags, 2)
        self.assertEqual(rec.compared, 1)


class DiversityTests(unittest.TestCase):
    def test_top_three_differ_in_stops_or_departure_slot(self) -> None:
        rec = recommend(day())
        self.assertEqual(len(rec.entries), 3)
        sigs = signatures(rec)
        self.assertEqual(len(set(sigs)), 3)
        labels = {label for e in rec.entries for label in e.labels}
        self.assertIn("recommended", labels)
        self.assertIn("cheapest", labels)
        self.assertIn("fastest", labels)
        cheapest = next(e for e in rec.entries if "cheapest" in e.labels)
        self.assertEqual(cheapest.offer.price, 450)
        fastest = next(e for e in rec.entries if "fastest" in e.labels)
        self.assertEqual(fastest.offer.duration_hours, 7.0)

    def test_near_identical_schedule_is_not_listed_twice(self) -> None:
        # Delta 08:00 and United 07:15 are both morning nonstops: one shortlist entry.
        rec = recommend(day())
        morning_nonstops = [s for s in signatures(rec) if s == (0, "morning")]
        self.assertEqual(len(morning_nonstops), 1)

    def test_distinct_label_when_global_best_shares_a_signature(self) -> None:
        offers = [
            make(airline="A", dep="08:00", price=500, hours=7.0, stops=0),
            make(airline="B", dep="09:00", price=400, hours=7.5, stops=0),
            make(airline="C", dep="19:00", price=450, hours=7.2, stops=0),
        ]
        rec = recommend(offers)
        self.assertEqual(
            [(e.offer.airline, e.labels) for e in rec.entries],
            [("B", ("recommended", "cheapest")), ("C", ("fastest_distinct",))],
        )
        self.assertEqual(len(set(signatures(rec))), len(rec.entries))
        self.assertEqual(len(rec.notes), 1)
        self.assertIn("shortest duration compared", rec.notes[0])
        self.assertIn("not listed separately", rec.notes[0])

    def test_fewer_distinct_signatures_means_shorter_shortlist(self) -> None:
        offers = [make(dep="08:00", price=500), make(airline="B", dep="09:00", price=450)]
        rec = recommend(offers)
        self.assertEqual(len(rec.entries), 1)

    def test_third_slot_fills_with_next_distinct_alternative(self) -> None:
        offers = [
            make(airline="A", dep="08:00", price=400, hours=6.0, stops=0),
            make(airline="B", dep="14:00", price=900, hours=9.0, stops=0),
            make(airline="C", dep="20:00", price=800, hours=9.5, stops=0),
        ]
        rec = recommend(offers)
        self.assertEqual(len(rec.entries), 3)
        self.assertEqual(rec.entries[0].offer.airline, "A")
        self.assertEqual(set(rec.entries[0].labels), {"recommended", "cheapest", "fastest"})
        self.assertEqual([e.labels for e in rec.entries[1:]], [("alternative",), ("alternative",)])

    def test_departure_slot_boundaries(self) -> None:
        self.assertEqual(departure_slot(make(dep="05:59")), "night")
        self.assertEqual(departure_slot(make(dep="06:00")), "morning")
        self.assertEqual(departure_slot(make(dep="12:00")), "afternoon")
        self.assertEqual(departure_slot(make(dep="18:00")), "evening")
        self.assertIsNone(departure_slot(make(dep=None)))


class DedupeTests(unittest.TestCase):
    def test_same_flight_fare_variants_keep_the_cheaper(self) -> None:
        offers = [
            make(airline="Delta", price=700),
            make(airline="Delta", price=620),
            make(airline="United", dep="14:00", price=650),
        ]
        rec = recommend(offers)
        self.assertEqual(rec.duplicates_removed, 1)
        self.assertEqual(rec.compared, 2)
        prices = sorted(e.offer.price for e in rec.entries)
        self.assertNotIn(700, prices)

    def test_same_clocks_other_carrier_is_not_a_duplicate(self) -> None:
        rec = recommend([make(airline="Delta"), make(airline="United", price=610)])
        self.assertEqual(rec.duplicates_removed, 0)


class UnknownFieldWordingTests(unittest.TestCase):
    def test_missing_provider_fields_are_worded_as_unknown(self) -> None:
        bare = make(
            airline=None, dep=None, arr=None, hours=None, stops=None, bags=None, carry_on=None
        )
        rec = recommend([bare])
        entry = rec.entries[0]
        for text in (
            "Stop count not shown",
            "Duration not shown",
            "Departure or arrival time not shown",
            "Carrier not shown",
            "Checked bag fee unknown",
            "Carry-on not shown",
            "Fare rules (refund, change) not shown",
        ):
            self.assertIn(text, entry.tradeoffs)
        self.assertEqual(entry.breakdown["duration"], 1.0)
        self.assertEqual(entry.breakdown["stops"], 1.0)

    def test_connection_without_layover_facts_says_so(self) -> None:
        rec = recommend([make(stops=1)])
        self.assertIn("1 stop, layover details not shown", rec.entries[0].tradeoffs)
        rec = recommend([make(stops=1, layover=("DUB", None))])
        self.assertIn("1 stop, via DUB (length not shown)", rec.entries[0].tradeoffs)

    def test_known_facts_are_stated_plainly(self) -> None:
        rec = recommend([make(stops=1, layover=("DUB", 2.5), bags=1, carry_on=0, airline="Iberia")])
        entry = rec.entries[0]
        self.assertIn("1 stop, via DUB (2.5 h)", entry.tradeoffs)
        self.assertIn("1 checked bag included", entry.highlights)
        self.assertIn("No carry-on included", entry.tradeoffs)
        self.assertIn("Carrier: Iberia", entry.highlights)
        self.assertIn("Departs 08:00, arrives 15:00", entry.highlights)
        self.assertIn("Fare $600", entry.highlights)

    def test_no_reference_prices_or_savings_claims(self) -> None:
        rec = recommend(day())
        text = " ".join(t for e in rec.entries for t in (*e.highlights, *e.tradeoffs)).lower()
        for word in ("save", "saving", "typical", "deal", "discount", "cheaper than"):
            self.assertNotIn(word, text)

    def test_baggage_buffer_is_named_not_invented(self) -> None:
        rec = recommend([make(buffer=40)])
        self.assertIn(
            "Ranked with a caller-named baggage buffer; the bag fee itself is not verified",
            rec.entries[0].tradeoffs,
        )


class MixedCurrencyTests(unittest.TestCase):
    def test_different_currencies_are_never_price_compared(self) -> None:
        offers = [
            make(airline="A", dep="08:00", price=500, hours=7.0, currency="USD"),
            make(airline="B", dep="19:00", price=400, hours=9.0, stops=1, currency="GBP"),
            make(airline="C", dep="14:00", price=300, hours=8.0, stops=0, currency="JPY"),
        ]
        rec = recommend(offers)
        self.assertEqual(rec.price_comparison, "skipped_mixed_currency")
        self.assertEqual(rec.weights["price"], 0.0)
        self.assertAlmostEqual(sum(rec.weights.values()), 1.0, places=3)
        labels = {label for e in rec.entries for label in e.labels}
        self.assertNotIn("cheapest", labels)
        for entry in rec.entries:
            self.assertIsNone(entry.breakdown["price"])
            self.assertFalse(
                any("lowest" in t.lower() for t in (*entry.highlights, *entry.tradeoffs))
            )
        self.assertIn("fares are not compared", rec.notes[-1])
        # The fastest nonstop wins on duration and stops alone, whatever its number says.
        self.assertEqual(rec.entries[0].offer.airline, "A")

    def test_mixed_currency_ranking_ignores_amounts(self) -> None:
        a = make(airline="A", price=1, currency="USD")
        b = make(airline="A", dep="09:00", price=999999, currency="JPY")
        first = recommend([a, b])
        second = recommend([b, a])
        self.assertEqual(
            [e.offer.departure for e in first.entries], [e.offer.departure for e in second.entries]
        )

    def test_same_flight_in_two_currencies_is_not_deduplicated(self) -> None:
        offers = [make(price=500, currency="USD"), make(price=400, currency="GBP")]
        rec = recommend(offers)
        self.assertEqual(rec.duplicates_removed, 0)

    def test_unproven_currency_skips_price(self) -> None:
        rec = recommend([make(price=500), make(dep="19:00", price=400)], currency=None)
        self.assertEqual(rec.price_comparison, "skipped_unknown_currency")

    def test_named_quote_currency_proves_a_single_currency(self) -> None:
        rec = recommend([make(price=500), make(dep="19:00", price=400)], currency="USD")
        self.assertEqual(rec.price_comparison, "compared")


class ScoringTests(unittest.TestCase):
    def test_weights_are_documented_and_sum_to_one(self) -> None:
        self.assertEqual(set(SCORE_WEIGHTS), {"price", "duration", "stops"})
        self.assertAlmostEqual(sum(SCORE_WEIGHTS.values()), 1.0)

    def test_score_is_deterministic_and_order_independent(self) -> None:
        offers = day()
        shuffled = offers[:]
        random.Random(7).shuffle(shuffled)
        first = recommend(offers, currency="USD").to_dict("USD")
        second = recommend(shuffled, currency="USD").to_dict("USD")
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_best_in_pool_on_every_dimension_scores_100(self) -> None:
        best = make(price=100, hours=5.0, stops=0)
        worse = make(dep="19:00", price=900, hours=9.0, stops=1)
        rec = recommend([best, worse], currency="USD")
        self.assertEqual(rec.entries[0].score, 100.0)
        self.assertEqual(rec.entries[0].breakdown, {"price": 0.0, "duration": 0.0, "stops": 0.0})

    def test_small_duration_gap_does_not_beat_a_large_fare_gap(self) -> None:
        offers = [
            make(airline="A", dep="18:25", price=422, hours=6 + 55 / 60),
            make(airline="B", dep="19:01", price=325, hours=7 + 14 / 60),
            make(airline="C", dep="19:30", price=900, hours=12.0, stops=0),
        ]
        rec = recommend(offers)
        self.assertEqual(rec.entries[0].offer.airline, "B")
        self.assertEqual(rec.entries[0].breakdown["price"], 0.0)

    def test_penalties_are_relative_to_the_best_and_capped(self) -> None:
        offers = [
            make(airline="A", dep="08:00", price=100, hours=5.0, stops=0),
            make(airline="B", dep="19:00", price=150, hours=6.0, stops=1),
            make(airline="C", dep="14:00", price=900, hours=9.0, stops=2),
        ]
        by_airline = {e.offer.airline: e for e in recommend(offers).entries}
        self.assertEqual(by_airline["B"].breakdown, {"price": 0.5, "duration": 0.2, "stops": 0.5})
        self.assertEqual(by_airline["C"].breakdown, {"price": 1.0, "duration": 0.8, "stops": 1.0})

    def test_slow_connections_are_hidden_from_the_comparison(self) -> None:
        offers = [
            make(dep="08:00", price=600, hours=7.0, stops=0),
            make(dep="19:00", price=300, hours=30.0, stops=1, layover=("DUB", 20.0)),
        ]
        rec = recommend(offers, currency="USD")
        self.assertEqual(rec.slow_connections_hidden, 1)
        self.assertEqual(rec.compared, 1)

    def test_buffer_changes_ranking_cost_and_is_labelled(self) -> None:
        offers = [
            make(airline="Ryanair", dep="08:00", price=100, buffer=50, hours=5.0),
            make(airline="Iberia", dep="19:00", price=120, hours=5.0, stops=0),
        ]
        rec = recommend(offers, currency="EUR")
        cheapest = next(e for e in rec.entries if "cheapest" in e.labels)
        self.assertEqual(cheapest.offer.airline, "Iberia")
        self.assertTrue(
            any(
                "ranked cost (fare plus named baggage buffer)" in t
                for e in rec.entries
                for t in (*e.highlights, *e.tradeoffs)
            )
        )


class PipelineTests(unittest.TestCase):
    def _search(self, cards, *, query: FlightQuery, filters=None):
        source = FakeSource(
            {(query.origin, query.destination, "2026-09-01", query.max_stops): cards}
        )
        kwargs = {"filters": filters} if filters is not None else {}
        report = _run_search(
            (query,),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=random.Random(0),
            now=lambda: datetime(2026, 8, 10),
            currency="USD",
            **kwargs,
        )
        return report

    def test_relaxes_a_hard_filter_that_emptied_the_offer_list(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=0)
        cards = (
            card(
                airline="Iberia",
                stops="1 stop",
                price="$480",
                duration="10 hr",
                layover_city="DUB",
                layover_hours=2.0,
            ),
            card(
                airline="Icelandair",
                departure="06:30",
                arrival="19:30",
                stops="1 stop",
                price="$450",
                duration="12 hr",
                layover_city="KEF",
                layover_hours=1.5,
            ),
        )
        # Direct-only drops both one-stop cards from `offers`; the recommendation still shows them.
        report = self._search(cards, query=query)
        result = report.queries[0]
        self.assertIsInstance(result, QuerySuccess)
        self.assertEqual(result.offers, ())
        rec = result.recommendation
        self.assertEqual(rec.relaxed_requirements, ("max_stops",))
        payload = report.to_dict()["queries"][0]
        self.assertEqual(payload["offers"], [])
        shortlist = payload["recommendation"]["shortlist"]
        self.assertEqual(payload["recommendation"]["relaxed_requirements"], ["max_stops"])
        self.assertEqual(shortlist[0]["requirements"], {"max_stops": "unmet"})
        self.assertTrue(shortlist[0]["offer"]["evidence"]["evidence_id"].startswith("gf_"))
        self.assertTrue(shortlist[0]["offer"]["google_flights_url"])

    def test_offers_key_and_order_are_unchanged_and_recommendation_is_additive(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=1)
        cards = (
            card(
                airline="Delta", price="$620", duration="7 hr", departure="08:00", arrival="15:00"
            ),
            card(
                airline="Iberia",
                stops="1 stop",
                price="$480",
                duration="10 hr",
                departure="19:30",
                arrival="07:30",
                layover_city="DUB",
                layover_hours=2.0,
            ),
        )
        with_rec = self._search(cards, query=query).to_dict()["queries"][0]
        self.assertEqual([o["airline"] for o in with_rec["offers"]], ["Iberia", "Delta"])
        self.assertEqual(with_rec["recommendation"]["relaxed_requirements"], [])
        self.assertEqual(
            {
                "status",
                "query",
                "raw_count",
                "eligible_count",
                "offers",
                "stops_compare",
                "recommendation",
            },
            set(with_rec),
        )
        recommended = with_rec["recommendation"]["shortlist"][0]["offer"]
        shown = next(o for o in with_rec["offers"] if o["airline"] == recommended["airline"])
        self.assertEqual(recommended["evidence"]["evidence_id"], shown["evidence"]["evidence_id"])

    def test_clock_requirements_come_from_the_named_filters(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=1)
        filters = parse_offer_filters(depart_window=(360, 720))
        cards = (card(airline="Late", departure="21:00", arrival="23:59", price="$300"),)
        result = self._search(cards, query=query, filters=filters).queries[0]
        self.assertEqual(result.offers, ())
        self.assertEqual(result.recommendation.relaxed_requirements, ("depart_window",))
        self.assertEqual(
            result.recommendation.requirements.to_dict()["depart_window"], "06:00-12:00"
        )

    def test_non_requirement_filters_are_never_relaxed(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=1, price_cap=100)
        cards = (card(airline="Pricey", price="$300"),)
        result = self._search(cards, query=query).queries[0]
        self.assertIsNone(result.recommendation)

    def test_packaged_trip_notes_that_it_cannot_relax(self) -> None:
        trip = RoundTrip("JFK", "LHR", date(2026, 9, 1), date(2026, 9, 8), max_stops=1)
        rec = _recommend(
            trip,
            [make(currency="USD")],
            parse_offer_filters(),
            currency="USD",
            packaged=True,
        )
        self.assertIn("cannot be relaxed", rec.notes[-1])


class SurfaceTests(unittest.TestCase):
    def test_mcp_search_flights_payload_carries_json_safe_recommendation(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=1)
        source = FakeSource({("JFK", "LHR", "2026-09-01", 1): (card(price="$620"),)})
        report = _run_search(
            (query,),
            top=8,
            source=source,
            sleep=lambda _: None,
            random_gen=random.Random(0),
            now=lambda: datetime(2026, 8, 10),
            currency="USD",
        )
        future = (date.today() + timedelta(days=30)).isoformat()
        with patch("viajante.mcp_handlers.search_flights", return_value=report):
            payload = search_flights_tool([f"JFK-LHR:{future}"], currency="USD")
        recommendation = payload["queries"][0]["recommendation"]
        self.assertEqual(recommendation["shortlist"][0]["labels"][0], "recommended")
        self.assertEqual(recommendation["scoring"]["weights"], dict(SCORE_WEIGHTS))
        json.dumps(payload)

    def test_relaxed_pick_sits_next_to_a_filtered_out_envelope(self) -> None:
        query = FlightQuery("JFK", "LHR", date(2026, 9, 1), max_stops=0)
        cards = (card(airline="Iberia", stops="1 stop", price="$480", duration="10 hr"),)
        report = _run_search(
            (query,),
            top=8,
            source=FakeSource({("JFK", "LHR", "2026-09-01", 0): cards}),
            sleep=lambda _: None,
            random_gen=random.Random(0),
            now=lambda: datetime(2026, 8, 10),
            currency="USD",
        )
        payload = stamp_search(dict(report.to_dict()))
        row = payload["queries"][0]
        self.assertEqual(payload["status"], "no_results")
        self.assertEqual(row["empty_reason"], "filtered_out")
        self.assertEqual(row["offers"], [])
        self.assertEqual(row["recommendation"]["relaxed_requirements"], ["max_stops"])

    def test_guide_explains_filtered_out_with_a_pick(self) -> None:
        flat = " ".join(GUIDE.split())
        self.assertIn("filtered_out (envelope status no_results) and a recommendation", flat)
        self.assertIn("a relaxed pick, not an exact match", flat)

    def test_search_trip_flights_report_carries_recommendation(self) -> None:
        report, _ = _search_trip_cards(trip_card())
        payload = report.to_dict()["flights"]["queries"][0]
        self.assertIn("recommendation", payload)
        self.assertEqual(payload["recommendation"]["relaxed_requirements"], [])


if __name__ == "__main__":
    unittest.main()
