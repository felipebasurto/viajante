from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from viajante.bench import (
    LIVE_ENV,
    MIN_PARSED_CARDS,
    REQUIRED_BAGGAGE_BUFFER_EUR,
    REQUIRED_FIXTURE_NAMES,
    REQUIRED_FLIGHT_TOP,
    BenchIntegrityError,
    BenchReport,
    check_product_defaults,
    format_report,
    maybe_live_sweep,
    parse_corpus,
    repo_root,
    run_bench,
    validate_corpus,
)
from viajante.cli import main
from viajante.flights import DEFAULT_BAGGAGE_BUFFER_EUR, DEFAULT_TOP


def _copy_corpus(tmp: Path) -> Path:
    root = repo_root()
    dest = tmp / "checkout"
    (dest / "tests").mkdir(parents=True)
    shutil.copytree(root / "tests" / "bench", dest / "tests" / "bench")
    (dest / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return dest


class BenchCorpusTests(unittest.TestCase):
    def test_parse_corpus_uses_checked_in_owned_fixtures(self) -> None:
        stats = parse_corpus()
        self.assertGreaterEqual(stats.files, len(REQUIRED_FIXTURE_NAMES))
        self.assertGreaterEqual(stats.cards, MIN_PARSED_CARDS)
        self.assertGreater(stats.bytes_read, 0)
        names = {path.name for path in (repo_root() / "tests" / "bench").iterdir()}
        self.assertTrue(REQUIRED_FIXTURE_NAMES.issubset(names))

    def test_manifest_lists_only_required_owned_files(self) -> None:
        fixtures = validate_corpus()
        listed = {row["name"] for row in fixtures}
        self.assertEqual(listed, REQUIRED_FIXTURE_NAMES)
        for row in fixtures:
            self.assertGreater(row["bytes"], 0)

    def test_empty_fixture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_corpus(Path(tmp))
            target = checkout / "tests" / "bench" / "shopping-one-stop.wrb"
            target.write_text("", encoding="utf-8")
            with self.assertRaises(BenchIntegrityError) as ctx:
                validate_corpus(checkout)
            self.assertIn("empty fixture", str(ctx.exception))

    def test_dropped_manifest_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = _copy_corpus(Path(tmp))
            path = checkout / "tests" / "bench" / "manifest.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["fixtures"] = [row for row in data["fixtures"] if row["name"] != "cards-best.html"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(BenchIntegrityError) as ctx:
                validate_corpus(checkout)
            self.assertIn("dropped required fixtures", str(ctx.exception))

    def test_product_defaults_stay_pinned(self) -> None:
        check_product_defaults()
        self.assertEqual(DEFAULT_TOP, REQUIRED_FLIGHT_TOP)
        self.assertEqual(DEFAULT_BAGGAGE_BUFFER_EUR, REQUIRED_BAGGAGE_BUFFER_EUR)

    def test_committed_baseline_has_a_real_score(self) -> None:
        path = repo_root() / "bench-baseline.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data["git_sha"], str)
        self.assertGreaterEqual(len(data["git_sha"]), 7)
        self.assertIsInstance(data["tests_ms"], int)
        self.assertIsInstance(data["parse_ms"], int)
        self.assertIsInstance(data["score_ms"], int)
        self.assertGreater(data["tests_ms"], 0)
        self.assertGreaterEqual(data["parse_ms"], 0)
        self.assertEqual(data["score_ms"], data["tests_ms"] + data["parse_ms"])


class BenchReportTests(unittest.TestCase):
    def test_ok_block_is_machine_readable(self) -> None:
        text = format_report(BenchReport(gate="ok", tests_ms=531, parse_ms=12, score_ms=543))
        self.assertEqual(
            text,
            "gate: ok\ntests_ms: 531\nparse_ms: 12\nscore_ms: 543\n",
        )
        self.assertNotIn("sweep_ms", text)

    def test_ok_block_can_include_optional_sweep(self) -> None:
        text = format_report(
            BenchReport(
                gate="ok",
                tests_ms=531,
                parse_ms=12,
                score_ms=543,
                sweep_ms=1800,
            )
        )
        self.assertIn("sweep_ms: 1800\n", text)
        self.assertTrue(text.startswith("gate: ok\n"))

    def test_fail_block_has_no_score(self) -> None:
        text = format_report(BenchReport(gate="fail", detail="ruff check failed"))
        self.assertEqual(text, "gate: fail\n")
        self.assertNotIn("score_ms", text)
        self.assertNotIn("tests_ms", text)


class BenchCliTests(unittest.TestCase):
    def test_cli_wires_to_run_bench(self) -> None:
        with patch("viajante.cli.run_bench", return_value=0) as bench:
            code = main(["bench"])
        self.assertEqual(code, 0)
        bench.assert_called_once_with()

    def test_gate_fail_exits_nonzero_without_a_score(self) -> None:
        with patch(
            "viajante.bench.run_gate",
            return_value=(False, "ruff check failed (exit 1)\nbogus", 12),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = run_bench()
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "gate: fail\n")
        self.assertNotIn("score_ms", stdout.getvalue())
        self.assertIn("ruff check failed", stderr.getvalue())

    def test_integrity_fail_exits_nonzero_without_a_score(self) -> None:
        with patch(
            "viajante.bench.check_product_defaults",
            side_effect=BenchIntegrityError("DEFAULT_TOP must stay 8"),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = run_bench()
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "gate: fail\n")
        self.assertNotIn("score_ms", stdout.getvalue())

    def test_live_env_is_off_by_default(self) -> None:
        with (
            patch("viajante.bench.run_gate", return_value=(True, "", 10)),
            patch("viajante.bench.parse_corpus"),
            patch("viajante.bench.maybe_live_sweep") as live,
            patch.dict("os.environ", {LIVE_ENV: ""}),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = run_bench()
        self.assertEqual(code, 0)
        live.assert_not_called()
        self.assertIn("score_ms: 10\n", stdout.getvalue())
        self.assertNotIn("sweep_ms", stdout.getvalue())

    def test_maybe_live_sweep_does_not_touch_the_network_when_off(self) -> None:
        with (
            patch.dict("os.environ", {LIVE_ENV: ""}),
            patch("viajante.bench.search_flights") as search,
        ):
            self.assertIsNone(maybe_live_sweep())
            search.assert_not_called()

    def test_bench_help_lists_the_command(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            code = main(["bench", "--help"])
        self.assertEqual(code, 0)
        help_text = buffer.getvalue()
        self.assertIn("viajante bench", help_text)
        self.assertNotIn("--skip", help_text)
        self.assertNotIn("--top", help_text)


if __name__ == "__main__":
    unittest.main()
