from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from viajante.bench import repo_root
from viajante.cli import main
from viajante.prompt_bench import (
    DEFAULT_JUDGE_MODEL,
    JUDGE_ENV,
    JUDGE_KEY_ENV,
    JUDGE_KEY_OVERRIDE_ENV,
    JUDGE_MODEL_ENV,
    JUDGE_MODEL_OVERRIDE_ENV,
    JUDGE_SYSTEM_PROMPT,
    JUDGE_URL,
    LIVE_ENV,
    MIN_INSANE,
    MIN_PROMPT_CASES,
    MIN_TIER_CASES,
    MIN_UNIQUE_ORIGINS,
    PROMPTS_ENV,
    REQUIRED_PROMPT_IDS,
    JudgeResult,
    PromptCase,
    PromptCorpusError,
    PromptRunResult,
    _evaluate_case,
    _format_case_line,
    _judge_model,
    compact_plan_dict,
    invention_reason,
    judge_case,
    load_holdout_cases,
    load_prompt_cases,
    parse_judge_verdict,
    parse_score_1_100,
    rt_split_reason,
    run_prompt_bench,
    validate_prompt_corpus,
)
from viajante.prompt_plan import plan_prompt


class _FakeJudgeResponse:
    def __init__(self, score: int, reason: str) -> None:
        payload = {
            "choices": [
                {"message": {"content": json.dumps({"score_1_100": score, "reason": reason})}}
            ]
        }
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeJudgeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def _copy_prompts(tmp: Path) -> Path:
    root = repo_root()
    dest = tmp / "checkout"
    (dest / "tests").mkdir(parents=True)
    shutil.copytree(root / "tests" / "prompts", dest / "tests" / "prompts")
    shutil.copytree(root / "tests" / "bench", dest / "tests" / "bench")
    (dest / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return dest


class PromptCorpusIntegrityTests(unittest.TestCase):
    def test_corpus_meets_floors(self) -> None:
        cases = validate_prompt_corpus()
        self.assertGreaterEqual(len(cases), MIN_PROMPT_CASES)
        ids = {row.id for row in cases}
        self.assertTrue(REQUIRED_PROMPT_IDS.issubset(ids))
        insane = [row for row in cases if row.tier == "insane"]
        self.assertGreaterEqual(len(insane), MIN_INSANE)
        brutal = [row for row in cases if row.tier == "brutal"]
        self.assertGreaterEqual(len(brutal), MIN_TIER_CASES["brutal"])

    def test_no_empty_prompts_or_blank_ids(self) -> None:
        for row in load_prompt_cases():
            self.assertTrue(row.id.strip())
            self.assertTrue(row.prompt.strip())
            self.assertIn(row.tier, {"smoke", "easy", "medium", "hard", "insane", "brutal"})
            self.assertIn(row.judge, {"deterministic", "llm"})

    def test_deterministic_flight_cases_have_expected_iata(self) -> None:
        checked = 0
        for row in load_prompt_cases():
            if row.judge != "deterministic":
                continue
            intent = row.expect.get("intent")
            if intent not in {"flights", "dates", "explore"}:
                continue
            origin = row.expect.get("origin")
            self.assertIsInstance(origin, str)
            self.assertEqual(len(origin), 3)
            self.assertEqual(origin, origin.upper())
            checked += 1
        self.assertGreaterEqual(checked, 30)

    def test_required_fiji_case_is_present(self) -> None:
        cases = {row.id: row for row in load_prompt_cases()}
        row = cases["insane-yhz-fiji-via-continents"]
        self.assertEqual(row.tier, "insane")
        self.assertIn("Halifax", row.prompt)
        self.assertIn("Fiji", row.prompt)
        self.assertIn("European", row.prompt)
        lowered = row.prompt.casefold()
        self.assertIn("sub-saharan", lowered)
        self.assertIn("indian", lowered)
        self.assertIn("chinese", lowered)
        self.assertIn("new zealand", lowered)
        self.assertEqual(row.expect.get("origin"), "YHZ")
        self.assertEqual(row.expect.get("destination"), "NAN")

    def test_brutal_impossible_slice_is_present(self) -> None:
        rows = [row for row in load_prompt_cases() if row.id.startswith("brutal-impossible-")]
        self.assertGreaterEqual(len(rows), 10)
        for row in rows:
            self.assertEqual(row.tier, "brutal", row.id)
            self.assertEqual(row.judge, "llm", row.id)
            self.assertEqual(row.lang, "en", row.id)
            notes = str(row.expect.get("notes") or "")
            self.assertIn("Unsatisfiable", notes)
            self.assertNotRegex(notes, r"\d+\s*€")
            self.assertNotRegex(row.prompt, r"\bMAD\b")

    def test_empty_prompt_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_prompts(Path(tmp))
            target = checkout / "tests" / "prompts" / "smoke.jsonl"
            target.write_text("", encoding="utf-8")
            with self.assertRaises(PromptCorpusError) as ctx:
                validate_prompt_corpus(checkout)
            self.assertIn("empty", str(ctx.exception).casefold())

    def test_dropped_prompt_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_prompts(Path(tmp))
            (checkout / "tests" / "prompts" / "insane.jsonl").unlink()
            with self.assertRaises(PromptCorpusError) as ctx:
                validate_prompt_corpus(checkout)
            self.assertIn("dropped", str(ctx.exception).casefold())

    def test_dropping_the_fiji_case_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_prompts(Path(tmp))
            path = checkout / "tests" / "prompts" / "insane.jsonl"
            kept = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line).get("id") != "insane-yhz-fiji-via-continents"
            ]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            with self.assertRaises(PromptCorpusError) as ctx:
                validate_prompt_corpus(checkout)
            self.assertIn("insane-yhz-fiji-via-continents", str(ctx.exception))

    def test_corpus_is_english_and_international(self) -> None:
        cases = load_prompt_cases()
        spanish = (
            r"\b(vuelos?|habitaci[oó]n|destinos|explora|calendario|"
            r"tercermundista|fiyi|quiero|ida y vuelta|reserva ya|"
            r"alquiler de coche)\b"
        )
        origins: list[str] = []
        for row in cases:
            self.assertEqual(row.lang, "en", row.id)
            self.assertRegex(row.prompt, r"[A-Za-z]")
            self.assertNotRegex(row.prompt, spanish)
            origin = row.expect.get("origin")
            if isinstance(origin, str):
                origins.append(origin)
        unique = set(origins)
        self.assertGreaterEqual(len(unique), MIN_UNIQUE_ORIGINS)
        self.assertLessEqual(origins.count("MAD"), 2)
        self.assertTrue({"BOS", "NRT", "GRU", "YHZ", "SIN"} & unique)

    def test_corpus_is_ordered_easy_to_insane(self) -> None:
        cases = load_prompt_cases()
        order = {"smoke": 0, "easy": 1, "medium": 2, "hard": 3, "insane": 4, "brutal": 5}
        ranks = [order[row.tier] for row in cases]
        self.assertEqual(ranks, sorted(ranks))


class PromptBenchRunnerTests(unittest.TestCase):
    def test_prompts_run_is_offline_and_skips_judge_by_default(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {JUDGE_ENV: "", LIVE_ENV: "", PROMPTS_ENV: ""}),
            patch("viajante.prompt_bench.judge_case") as judge,
            patch("viajante.flights.search_flights") as search,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_prompt_bench()
        self.assertEqual(code, 0)
        judge.assert_not_called()
        search.assert_not_called()
        text = stdout.getvalue()
        err = stderr.getvalue()
        self.assertIn("prompts:", text)
        self.assertIn("judge: skip", text)
        self.assertIn("scored: 0", text)
        self.assertNotIn("score_ms", text)
        self.assertNotIn("score_1_100", text)
        self.assertRegex(text, r"wall_ms: \d+")
        pass_lines = [ln for ln in err.splitlines() if "  pass  " in ln]
        self.assertTrue(pass_lines)
        for ln in pass_lines:
            self.assertNotIn("plan=", ln)
        self.assertNotIn("holdout-", err)

    def test_judge_without_key_prints_skip_not_a_score(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict(
                "os.environ",
                {
                    JUDGE_ENV: "1",
                    JUDGE_KEY_ENV: "",
                    JUDGE_KEY_OVERRIDE_ENV: "",
                    "OPENAI_API_KEY": "sk-not-used",
                },
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_prompt_bench()
        self.assertEqual(code, 0)
        urlopen.assert_not_called()
        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertIn("judge: skip", out)
        self.assertIn("scored: 0", out)
        self.assertNotIn("score_ms", out)
        self.assertNotIn("score_1_100=", err)
        self.assertNotIn("sk-not-used", out)
        self.assertNotIn("sk-not-used", err)

    def test_deterministic_corpus_matches_the_planner(self) -> None:
        failures = []
        for row in load_prompt_cases():
            if row.judge != "deterministic":
                continue
            plan = plan_prompt(row.prompt)
            ok, reason = plan.matches(row.expect)
            if not ok:
                failures.append(f"{row.id}: {reason}")
        self.assertEqual(failures, [])

    def test_cli_prompts_flag_wires_to_runner(self) -> None:
        with patch("viajante.cli.run_prompt_bench", return_value=0) as runner:
            code = main(["bench", "--prompts"])
        self.assertEqual(code, 0)
        runner.assert_called_once_with()

    def test_cli_env_wires_to_runner(self) -> None:
        with (
            patch.dict("os.environ", {PROMPTS_ENV: "1"}),
            patch("viajante.cli.run_prompt_bench", return_value=0) as runner,
            patch("viajante.cli.run_bench") as speed,
        ):
            code = main(["bench"])
        self.assertEqual(code, 0)
        runner.assert_called_once_with()
        speed.assert_not_called()

    def test_default_bench_does_not_run_prompts(self) -> None:
        with (
            patch.dict("os.environ", {PROMPTS_ENV: "", JUDGE_ENV: ""}),
            patch("viajante.cli.run_bench", return_value=0) as speed,
            patch("viajante.cli.run_prompt_bench") as prompts,
        ):
            code = main(["bench"])
        self.assertEqual(code, 0)
        speed.assert_called_once_with()
        prompts.assert_not_called()

    def test_bench_help_lists_prompts(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["bench", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("--prompts", help_text)
        self.assertIn("--holdout", help_text)
        self.assertIn("DEEPSEEK_API_KEY", help_text)
        self.assertNotIn("--skip", help_text)
        self.assertNotIn("--top", help_text)

    def test_cli_holdout_flag_wires_to_runner(self) -> None:
        with patch("viajante.cli.run_prompt_bench", return_value=0) as runner:
            code = main(["bench", "--prompts", "--holdout"])
        self.assertEqual(code, 0)
        runner.assert_called_once_with(holdout=True)


class JudgeScoreTests(unittest.TestCase):
    def test_default_model_is_deepseek_chat(self) -> None:
        self.assertEqual(DEFAULT_JUDGE_MODEL, "deepseek-chat")
        self.assertEqual(JUDGE_KEY_ENV, "DEEPSEEK_API_KEY")
        self.assertEqual(JUDGE_KEY_OVERRIDE_ENV, "VIAJANTE_JUDGE_KEY")
        self.assertEqual(JUDGE_MODEL_OVERRIDE_ENV, "VIAJANTE_JUDGE_MODEL")
        self.assertEqual(JUDGE_URL, "https://api.deepseek.com/v1/chat/completions")
        with patch.dict(
            "os.environ",
            {JUDGE_MODEL_ENV: "", JUDGE_MODEL_OVERRIDE_ENV: ""},
            clear=False,
        ):
            self.assertEqual(_judge_model(), "deepseek-chat")
        with patch.dict("os.environ", {JUDGE_MODEL_ENV: "deepseek-chat"}, clear=False):
            self.assertEqual(_judge_model(), "deepseek-chat")

    def test_parse_score_rejects_pass_fail_and_out_of_range(self) -> None:
        self.assertEqual(parse_score_1_100(1), 1)
        self.assertEqual(parse_score_1_100(100), 100)
        self.assertEqual(parse_score_1_100("81"), 81)
        self.assertIsNone(parse_score_1_100(0))
        self.assertIsNone(parse_score_1_100(101))
        self.assertIsNone(parse_score_1_100(True))
        self.assertIsNone(parse_score_1_100(False))
        self.assertIsNone(parse_score_1_100({"pass": True}))
        self.assertIsNone(parse_score_1_100(None))

    def test_verdict_requires_score_and_reason(self) -> None:
        scored = parse_judge_verdict(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"score_1_100": 81, "reason": "Honest split packages"}
                            )
                        }
                    }
                ]
            }
        )
        self.assertEqual(scored.score_1_100, 81)
        self.assertEqual(scored.reason, "Honest split packages")
        self.assertFalse(scored.skipped)

        old_pass_fail = parse_judge_verdict(
            {"choices": [{"message": {"content": json.dumps({"pass": True, "reason": "ok"})}}]}
        )
        self.assertTrue(old_pass_fail.skipped)
        self.assertIsNone(old_pass_fail.score_1_100)

        missing_reason = parse_judge_verdict(
            {"choices": [{"message": {"content": json.dumps({"score_1_100": 50})}}]}
        )
        self.assertTrue(missing_reason.skipped)

    def test_judge_instructions_do_not_punish_omitted_prices(self) -> None:
        prompt = JUDGE_SYSTEM_PROMPT
        self.assertIn("NO single correct answer", prompt)
        self.assertIn("AUTOMATIC 0", prompt)
        self.assertIn("Not a 15-point ding", prompt)
        self.assertIn("Inventing a price, fare, EUR amount, booking", prompt)
        self.assertIn("Extra route_specs / city pairs", prompt)
        self.assertIn("Omitting prices", prompt)
        self.assertIn("must not lower the score", prompt)
        self.assertIn("Do not deduct for lacking explicit pricing", prompt)
        self.assertIn("optional, not required for a score of 90 or above", prompt)
        self.assertIn(
            "Do not deduct for not stating that viajante does not book",
            prompt,
        )
        self.assertIn("Madrid is not the implied home hub", prompt)
        self.assertIn("You write a 1–100 only", prompt)
        self.assertIn("split a packaged RT", prompt)
        self.assertIn("two one-ways / without --trip / separate tickets", prompt)
        self.assertIn("one-way prices as if they were the RT package", prompt)
        self.assertIn('"score_1_100"', prompt)
        self.assertIn('"reason"', prompt)
        self.assertNotIn("LARGE penalty", prompt)

    def test_compact_plan_dict_drops_empty_fields(self) -> None:
        cases = {row.id: row for row in load_prompt_cases()}
        plan = plan_prompt(cases["smoke-bos-lhr-one-date"].prompt)
        compact = compact_plan_dict(plan)
        self.assertEqual(compact.get("intent"), "flights")
        self.assertEqual(compact.get("origin"), "BOS")
        self.assertEqual(compact.get("destination"), "LHR")
        self.assertNotIn("notes", compact)
        self.assertNotIn("refuse", compact)
        for value in compact.values():
            self.assertIsNotNone(value)
            self.assertNotEqual(value, "")
            self.assertNotEqual(value, [])
            self.assertIsNot(value, False)

    def test_scored_line_records_score_reason_and_plan(self) -> None:
        case = next(row for row in load_prompt_cases() if row.is_llm)
        plan = plan_prompt(case.prompt)
        line = _format_case_line(
            PromptRunResult(
                case=case,
                status="scored",
                elapsed_ms=12,
                reason="Honest about --trip multi cap",
                score_1_100=81,
                plan=plan,
            )
        )
        self.assertIn("score_1_100=81", line)
        self.assertIn("Honest about --trip multi cap", line)
        self.assertIn("scored", line)
        self.assertNotIn(" pass ", f" {line} ")
        self.assertIn("plan=", line)
        dumped = json.loads(line.split("plan=", 1)[1])
        self.assertEqual(dumped, compact_plan_dict(plan))
        self.assertIn("intent", dumped)

    def test_fail_line_dumps_compact_plan_pass_does_not(self) -> None:
        real = next(
            row
            for row in load_prompt_cases()
            if row.judge == "deterministic" and row.expect.get("intent") == "flights"
        )
        broken = PromptCase(
            id="fail-plan-dump",
            tier=real.tier,
            prompt=real.prompt,
            judge="deterministic",
            expect={**real.expect, "origin": "ZZZ"},
            lang=real.lang,
        )
        failed = _evaluate_case(broken)
        self.assertEqual(failed.status, "fail")
        fail_line = _format_case_line(failed)
        self.assertIn("plan=", fail_line)
        dumped = json.loads(fail_line.split("plan=", 1)[1])
        self.assertIn("intent", dumped)
        self.assertNotEqual(dumped.get("origin"), "ZZZ")

        passed = _format_case_line(
            PromptRunResult(
                case=real,
                status="pass",
                elapsed_ms=3,
                reason="ok",
                plan=plan_prompt(real.prompt),
            )
        )
        self.assertNotIn("plan=", passed)

    def test_owned_plans_are_not_invention(self) -> None:
        failures = []
        for row in load_prompt_cases():
            reason = invention_reason(row.prompt, plan_prompt(row.prompt))
            if reason is not None:
                failures.append(f"{row.id}: {reason}")
        self.assertEqual(failures, [])

    def test_owned_plans_are_not_rt_splits(self) -> None:
        failures = []
        for row in load_prompt_cases():
            reason = rt_split_reason(row.prompt, plan_prompt(row.prompt))
            if reason is not None:
                failures.append(f"{row.id}: {reason}")
        self.assertEqual(failures, [])

    def test_invented_fare_is_automatic_zero_without_llm(self) -> None:
        case = next(row for row in load_prompt_cases() if row.is_llm)
        honest = plan_prompt(case.prompt)
        self.assertIsNone(invention_reason(case.prompt, honest))
        fake = replace(honest, notes="cheapest 87 EUR nonstop")
        reason = invention_reason(case.prompt, fake)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("invented", reason.casefold())
        self.assertIn("87", reason)

        with (
            patch.dict(
                "os.environ",
                {JUDGE_ENV: "1", JUDGE_KEY_ENV: "test-deepseek-key"},
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
        ):
            judged = judge_case(case, fake)
        self.assertEqual(judged.score_1_100, 0)
        self.assertIn("invented", judged.reason.casefold())
        urlopen.assert_not_called()

        line = _format_case_line(
            PromptRunResult(
                case=case,
                status="scored",
                elapsed_ms=4,
                reason=judged.reason,
                score_1_100=0,
                plan=fake,
            )
        )
        self.assertIn("score_1_100=0", line)
        self.assertIn("invented", line)
        self.assertIn("plan=", line)
        dumped = json.loads(line.split("plan=", 1)[1])
        self.assertIn("intent", dumped)

    def test_invented_city_pair_is_automatic_zero_without_llm(self) -> None:
        prompt = "Flights BOS-LHR on 2026-09-01"
        honest = plan_prompt(prompt)
        self.assertIsNone(invention_reason(prompt, honest))
        fake = replace(
            honest,
            destination="CDG",
            route_specs=honest.route_specs + ("BOS-CDG:2026-09-01",),
        )
        reason = invention_reason(prompt, fake)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("invented", reason.casefold())
        self.assertIn("CDG", reason)

        case = PromptCase(
            id="invented-city-pair",
            tier="insane",
            prompt=prompt,
            judge="llm",
            expect={"intent": "flights"},
            lang="en",
        )
        with (
            patch.dict(
                "os.environ",
                {JUDGE_ENV: "1", JUDGE_KEY_ENV: "test-deepseek-key"},
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
        ):
            judged = judge_case(case, fake)
        self.assertEqual(judged.score_1_100, 0)
        self.assertIn("invented", judged.reason.casefold())
        urlopen.assert_not_called()

    def test_rt_split_reason_named_return_split_is_fatal(self) -> None:
        prompt = "LAX-NRT on 2026-11-03 returning 2026-11-12"
        honest = plan_prompt(prompt)
        self.assertEqual(honest.trip, "rt")
        self.assertIsNone(rt_split_reason(prompt, honest))
        split = replace(
            honest,
            trip="one-way",
            route_specs=("LAX-NRT:2026-11-03", "NRT-LAX:2026-11-12"),
        )
        self.assertEqual(rt_split_reason(prompt, split), "plan split a packaged RT")
        missing_trip = replace(honest, trip="one-way")
        self.assertEqual(rt_split_reason(prompt, missing_trip), "plan split a packaged RT")
        sugar = replace(
            honest,
            trip="one-way",
            route_specs=("LAX-NRT:2026-11-03:2026-11-12",),
        )
        self.assertEqual(rt_split_reason(prompt, sugar), "plan split a packaged RT")

        case = PromptCase(
            id="rt-split-named-return",
            tier="insane",
            prompt=prompt,
            judge="llm",
            expect={"intent": "flights"},
            lang="en",
        )
        with (
            patch.dict(
                "os.environ",
                {JUDGE_ENV: "1", JUDGE_KEY_ENV: "test-deepseek-key"},
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
        ):
            judged = judge_case(case, split)
        self.assertEqual(judged.score_1_100, 0)
        self.assertEqual(judged.reason, "plan split a packaged RT")
        urlopen.assert_not_called()

        with patch("viajante.prompt_bench.plan_prompt", return_value=split):
            evaluated = _evaluate_case(case)
        self.assertEqual(evaluated.status, "scored")
        self.assertEqual(evaluated.score_1_100, 0)
        self.assertEqual(evaluated.reason, "plan split a packaged RT")

    def test_rt_split_reason_explicit_two_one_ways_packaged_is_fatal(self) -> None:
        prompt = "BOM-DXB on 2026-09-25 returning 2026-09-28 as two one-way, without --trip rt"
        honest = plan_prompt(prompt)
        self.assertEqual(honest.trip, "one-way")
        self.assertIsNone(rt_split_reason(prompt, honest))
        packaged = replace(
            honest,
            trip="rt",
            route_specs=("BOM-DXB:2026-09-25:2026-09-28",),
        )
        self.assertEqual(
            rt_split_reason(prompt, packaged),
            "plan packaged a two one-way request as --trip rt",
        )

        case = PromptCase(
            id="rt-split-two-one-ways",
            tier="insane",
            prompt=prompt,
            judge="llm",
            expect={"intent": "flights"},
            lang="en",
        )
        with (
            patch.dict(
                "os.environ",
                {JUDGE_ENV: "1", JUDGE_KEY_ENV: "test-deepseek-key"},
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
        ):
            judged = judge_case(case, packaged)
        self.assertEqual(judged.score_1_100, 0)
        self.assertEqual(
            judged.reason,
            "plan packaged a two one-way request as --trip rt",
        )
        urlopen.assert_not_called()

    def test_stated_price_cap_is_not_invention(self) -> None:
        prompt = "SIN to everywhere under 200€ next month (September 2026)"
        plan = plan_prompt(prompt)
        self.assertEqual(plan.price_cap_eur, 200)
        self.assertIsNone(invention_reason(prompt, plan))

    def test_low_score_is_scored_not_fail(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def fake_judge(case: object, plan: object) -> JudgeResult:
            return JudgeResult(12, "Weak routing but no invented booking")

        with (
            patch.dict(
                "os.environ",
                {JUDGE_ENV: "1", JUDGE_KEY_ENV: "test-deepseek-key"},
                clear=False,
            ),
            patch("viajante.prompt_bench.judge_case", side_effect=fake_judge),
            patch("viajante.prompt_bench._judge_urlopen") as urlopen,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_prompt_bench()
        self.assertEqual(code, 0)
        urlopen.assert_not_called()
        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertIn("judge: ran", out)
        self.assertRegex(out, r"fail: 0")
        self.assertRegex(out, r"scored: [1-9]")
        self.assertIn("score_1_100=12", err)
        self.assertIn("Weak routing but no invented booking", err)
        self.assertNotIn("score_ms", out)
        self.assertNotIn("test-deepseek-key", out)
        self.assertNotIn("test-deepseek-key", err)
        scored_lines = [ln for ln in err.splitlines() if "score_1_100=" in ln]
        self.assertTrue(scored_lines)
        for ln in scored_lines:
            self.assertIn("plan=", ln)
            dumped = json.loads(ln.split("plan=", 1)[1])
            self.assertIsInstance(dumped, dict)
            self.assertIn("intent", dumped)

    def test_judge_posts_to_deepseek_not_openai(self) -> None:
        case = next(row for row in load_prompt_cases() if row.is_llm)
        plan = plan_prompt(case.prompt)
        captured: list[object] = []

        def fake_urlopen(request: object, timeout: int = 30) -> _FakeJudgeResponse:
            captured.append(request)
            return _FakeJudgeResponse(81, "Honest multi-hop split")

        with (
            patch.dict(
                "os.environ",
                {
                    JUDGE_ENV: "1",
                    JUDGE_KEY_ENV: "test-deepseek-key",
                    JUDGE_KEY_OVERRIDE_ENV: "",
                    JUDGE_MODEL_ENV: "",
                    JUDGE_MODEL_OVERRIDE_ENV: "",
                },
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen", fake_urlopen),
        ):
            result = judge_case(case, plan)
        self.assertEqual(result.score_1_100, 81)
        self.assertEqual(result.reason, "Honest multi-hop split")
        request = captured[0]
        self.assertEqual(request.full_url, JUDGE_URL)
        self.assertIn("/v1/chat/completions", request.full_url)
        self.assertNotIn("openai.com", request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["messages"][0]["content"], JUDGE_SYSTEM_PROMPT)
        self.assertIn("NO single correct answer", body["messages"][0]["content"])
        self.assertIn("must not lower the score", body["messages"][0]["content"])
        self.assertIn("AUTOMATIC 0", body["messages"][0]["content"])
        self.assertIn(
            "optional, not required for a score of 90 or above",
            body["messages"][0]["content"],
        )
        user_payload = json.loads(body["messages"][1]["content"])
        self.assertIn("rt_contract", user_payload)
        self.assertNotIn("gpt-4o", json.dumps(body))

    def test_viajante_judge_key_overrides_deepseek_key(self) -> None:
        case = next(row for row in load_prompt_cases() if row.is_llm)
        plan = plan_prompt(case.prompt)
        captured: list[object] = []

        def fake_urlopen(request: object, timeout: int = 30) -> _FakeJudgeResponse:
            captured.append(request)
            return _FakeJudgeResponse(70, "Sane routing")

        with (
            patch.dict(
                "os.environ",
                {
                    JUDGE_ENV: "1",
                    JUDGE_KEY_ENV: "primary-key",
                    JUDGE_KEY_OVERRIDE_ENV: "override-key",
                    JUDGE_MODEL_ENV: "ignored-model",
                    JUDGE_MODEL_OVERRIDE_ENV: "deepseek-chat",
                },
                clear=False,
            ),
            patch("viajante.prompt_bench._judge_urlopen", fake_urlopen),
        ):
            result = judge_case(case, plan)
        self.assertEqual(result.score_1_100, 70)
        request = captured[0]
        auth = request.headers.get("Authorization")
        self.assertEqual(auth, "Bearer override-key")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-chat")
        self.assertNotIn("primary-key", json.dumps(body))


class PromptCorpusDropTests(unittest.TestCase):
    def test_shrinking_below_the_floor_fails_the_prompts_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_prompts(Path(tmp))
            easy = checkout / "tests" / "prompts" / "easy.jsonl"
            first = next(
                line for line in easy.read_text(encoding="utf-8").splitlines() if line.strip()
            )
            easy.write_text(first + "\n", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = run_prompt_bench(root=checkout)
            self.assertEqual(code, 1)
            self.assertNotIn("score_ms", stdout.getvalue())


class HoldoutCorpusTests(unittest.TestCase):
    def test_holdout_is_not_in_weekday_manifest(self) -> None:
        data = json.loads(
            (repo_root() / "tests" / "prompts" / "manifest.json").read_text(encoding="utf-8")
        )
        names = [row["name"] for row in data["files"]]
        self.assertNotIn("holdout.jsonl", names)
        self.assertEqual(
            names,
            [
                "smoke.jsonl",
                "easy.jsonl",
                "medium.jsonl",
                "hard.jsonl",
                "insane.jsonl",
                "brutal.jsonl",
            ],
        )

    def test_weekday_load_skips_holdout(self) -> None:
        cases = load_prompt_cases()
        self.assertFalse(any(row.id.startswith("holdout-") for row in cases))
        self.assertFalse(any(row.tier == "holdout" for row in cases))

    def test_holdout_file_schema(self) -> None:
        cases = load_holdout_cases()
        self.assertGreaterEqual(len(cases), 8)
        self.assertLessEqual(len(cases), 12)
        origins: list[str] = []
        judges = set()
        for row in cases:
            self.assertTrue(row.id.startswith("holdout-"), row.id)
            self.assertEqual(row.tier, "holdout")
            self.assertEqual(row.lang, "en")
            self.assertIn(row.judge, {"deterministic", "llm"})
            self.assertTrue(row.prompt.strip())
            self.assertTrue(row.expect)
            judges.add(row.judge)
            origin = row.expect.get("origin")
            if isinstance(origin, str):
                origins.append(origin)
        self.assertEqual(judges, {"deterministic", "llm"})
        self.assertGreaterEqual(len(set(origins)), 6)
        self.assertNotIn("MAD", origins)

    def test_holdout_run_stays_offline(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.dict("os.environ", {JUDGE_ENV: "", LIVE_ENV: "", PROMPTS_ENV: ""}),
            patch("viajante.prompt_bench.judge_case") as judge,
            patch("viajante.flights.search_flights") as search,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_prompt_bench(holdout=True)
        self.assertEqual(code, 0)
        judge.assert_not_called()
        search.assert_not_called()
        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertIn("prompts: ", out)
        self.assertNotIn("score_ms", out)
        lines = err.splitlines()
        self.assertTrue(any(ln.startswith("holdout-") for ln in lines))
        self.assertFalse(any(ln.startswith("smoke-") for ln in lines))

    def test_manifest_listing_holdout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_prompts(Path(tmp))
            path = checkout / "tests" / "prompts" / "manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["files"].append({"name": "holdout.jsonl", "tier": "holdout"})
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(PromptCorpusError) as ctx:
                validate_prompt_corpus(checkout)
            self.assertIn("holdout", str(ctx.exception).casefold())


if __name__ == "__main__":
    unittest.main()
