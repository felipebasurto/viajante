from __future__ import annotations

import unittest
from datetime import date

from viajante import evidence
from viajante.mcp_handlers import plan_stay_blocks_tool, split_stay_costs_tool
from viajante.stays import plan_stay_blocks, split_stay_costs

CORE = ["Felipe", "Mer"]
ROSTER = {
    "2026-12-03": [*CORE, "Jimena", "Angelita", "Chuspi"],
    "2026-12-04": [*CORE, "Jimena", "Angelita", "Chuspi", "Alvaro"],
    "2026-12-05": [*CORE, "Jimena", "Angelita", "Chuspi", "Alvaro", "Dela"],
    "2026-12-06": [*CORE, "Alvaro", "Dela"],
    "2026-12-07": [*CORE, "Alvaro", "Dela"],
    "2026-12-08": [*CORE, "Alvaro"],
}
STAYS = [
    {"name": "Best Spot", "check_in": "2026-12-03", "check_out": "2026-12-06", "total": 391.68},
    {"name": "Hotel Victor", "check_in": "2026-12-06", "check_out": "2026-12-09", "total": 196},
]


class PlanStayBlocksTests(unittest.TestCase):
    def test_roster_becomes_blocks_of_the_same_people(self) -> None:
        report = plan_stay_blocks(ROSTER)
        rows = [(b.check_in, b.check_out, b.headcount) for b in report.blocks]
        self.assertEqual(
            rows,
            [
                (date(2026, 12, 3), date(2026, 12, 4), 5),
                (date(2026, 12, 4), date(2026, 12, 5), 6),
                (date(2026, 12, 5), date(2026, 12, 6), 7),
                (date(2026, 12, 6), date(2026, 12, 8), 4),
                (date(2026, 12, 8), date(2026, 12, 9), 3),
            ],
        )
        self.assertEqual(report.person_nights, 29)
        self.assertEqual(report.people[0], "Felipe")
        self.assertEqual(report.to_dict()["nights"], 6)

    def test_same_headcount_with_different_people_is_a_new_block(self) -> None:
        report = plan_stay_blocks({"2026-12-01": ["A", "B"], "2026-12-02": ["A", "C"]})
        self.assertEqual(len(report.blocks), 2)

    def test_order_and_case_do_not_split_a_block(self) -> None:
        report = plan_stay_blocks({"2026-12-01": ["Ana", "Luis"], "2026-12-02": ["luis", "ANA"]})
        self.assertEqual(len(report.blocks), 1)
        self.assertEqual(report.blocks[0].nights, 2)

    def test_bad_rosters_fail_loudly(self) -> None:
        cases = {
            "empty": {},
            "gap": {"2026-12-01": ["A"], "2026-12-03": ["A"]},
            "nobody": {"2026-12-01": []},
            "duplicate": {"2026-12-01": ["A", "a"]},
            "bad date": {"soon": ["A"]},
            "blank": {"2026-12-01": ["A", " "]},
        }
        for label, roster in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    plan_stay_blocks(roster)


class SplitStayCostsTests(unittest.TestCase):
    def split(self, **kwargs):
        return split_stay_costs(STAYS, ROSTER, currency="EUR", fee_per_person_night=2, **kwargs)

    def test_each_stay_is_split_only_among_the_people_who_sleep_there(self) -> None:
        report = self.split()
        by_name = {p.name: p for p in report.people}
        # Chuspi never sleeps at Hotel Victor, so she owes nothing toward it.
        self.assertEqual([s.stay for s in by_name["Chuspi"].shares], ["Best Spot"])
        self.assertEqual(by_name["Chuspi"].total, 71.28)
        self.assertEqual(by_name["Felipe"].total, by_name["Mer"].total)
        self.assertEqual(by_name["Felipe"].total, 130.73)
        self.assertEqual(by_name["Dela"].total, 63.40)
        self.assertEqual(
            {s.name: s.rate_per_person_night for s in report.stays},
            {
                "Best Spot": 21.76,
                "Hotel Victor": 17.8182,
            },
        )

    def test_every_stay_and_the_grand_total_sum_exactly(self) -> None:
        report = self.split()
        for stay in report.stays:
            paid = sum(
                share.total
                for person in report.people
                for share in person.shares
                if share.stay == stay.name
            )
            self.assertEqual(round(paid, 2), stay.total)
        self.assertEqual(round(sum(p.total for p in report.people), 2), report.total)
        self.assertEqual(report.total, 645.68)
        self.assertEqual(report.fee_per_person_night, 2.0)

    def test_fee_is_per_person_and_night(self) -> None:
        by_name = {p.name: p for p in self.split().people}
        self.assertEqual((by_name["Felipe"].fee, by_name["Dela"].fee), (12.0, 6.0))

    def test_without_a_fee_it_is_zero_and_unstamped(self) -> None:
        report = split_stay_costs(STAYS, ROSTER, currency="EUR")
        self.assertIsNone(report.fee_per_person_night)
        self.assertEqual(sum(p.fee for p in report.people), 0)
        self.assertEqual(report.total, 587.68)

    def test_cents_that_do_not_divide_evenly_still_sum_to_the_stay(self) -> None:
        roster = {"2026-12-01": ["A", "B", "C"]}
        stays = [{"name": "S", "check_in": "2026-12-01", "check_out": "2026-12-02", "total": 100}]
        report = split_stay_costs(stays, roster, currency="EUR")
        self.assertEqual(sorted(p.total for p in report.people), [33.33, 33.33, 33.34])
        self.assertEqual(round(sum(p.total for p in report.people), 2), 100.0)

    def test_nights_no_stay_covers_are_reported_not_hidden(self) -> None:
        report = split_stay_costs(STAYS[:1], ROSTER, currency="EUR")
        self.assertEqual(
            [d.isoformat() for d in report.unallocated_nights],
            ["2026-12-06", "2026-12-07", "2026-12-08"],
        )

    def test_invalid_inputs_fail_before_any_arithmetic(self) -> None:
        def stay(**over):
            return [{**STAYS[0], **over}]

        cases = {
            "overlap": (list(STAYS) + [{**STAYS[1], "name": "X", "check_in": "2026-12-05"}], "EUR"),
            "night outside roster": (stay(check_out="2026-12-12"), "EUR"),
            "reversed": (stay(check_in="2026-12-06", check_out="2026-12-03"), "EUR"),
            "negative total": (stay(total=-5), "EUR"),
            "text total": (stay(total="lots"), "EUR"),
            "no name": (stay(name=" "), "EUR"),
            "no stays": ([], "EUR"),
            "bad currency": (STAYS, "euros please"),
        }
        for label, (stays, currency) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    split_stay_costs(stays, ROSTER, currency=currency)


class StayToolTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence.clear()
        self.addCleanup(evidence.clear)

    def test_blocks_tool_returns_plain_json(self) -> None:
        payload = plan_stay_blocks_tool(ROSTER)
        self.assertEqual(payload["person_nights"], 29)
        self.assertEqual([b["headcount"] for b in payload["blocks"]], [5, 6, 7, 4, 3])

    def test_split_output_is_recorded_so_verify_answer_owns_the_shares(self) -> None:
        split_stay_costs_tool(STAYS, ROSTER, "EUR", fee_per_person_night=2)
        verdict = evidence.verify_answer("Chuspi pays EUR 71.28 and the group EUR 645.68.")
        self.assertTrue(verdict["ok"], verdict)
        invented = evidence.verify_answer("Chuspi pays EUR 99.99.")
        self.assertFalse(invented["ok"])


if __name__ == "__main__":
    unittest.main()
