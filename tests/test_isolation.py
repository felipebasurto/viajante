from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _isolate  # noqa: F401
from test_history import _flight_report
from viajante import bench
from viajante.history import (
    ENV_RECORD,
    HISTORY_FILE,
    read_observations,
    recorded_flights,
)
from viajante.storage import default_state_dir

ROOT = Path(__file__).resolve().parent.parent
CHILD = "VIAJANTE_ISOLATION_CHILD"


class SuiteIsolationTests(unittest.TestCase):
    def test_suite_runs_with_recording_off_and_a_private_state_dir(self) -> None:
        self.assertNotIn(ENV_RECORD, os.environ)
        self.assertIn("viajante-tests-", os.environ["VIAJANTE_STATE_DIR"])

    @unittest.skipIf(os.environ.get(CHILD) == "1", "child run of the isolation probe")
    def test_exported_opt_in_never_reaches_the_real_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as real:
            env = {
                **os.environ,
                CHILD: "1",
                ENV_RECORD: "1",
                "VIAJANTE_STATE_DIR": real,
                "XDG_STATE_HOME": real,
                "HOME": real,
            }
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_[hi]*.py",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(os.listdir(real), [])

    @unittest.skipUnless(os.environ.get(CHILD) == "1", "probe only runs inside the child")
    def test_probe_a_real_search_records_nothing_under_an_exported_opt_in(self) -> None:
        def fake(queries, *, top=3, sort="ranked", baggage_buffer=None, **rest):
            return _flight_report(99.0)

        report = _flight_report(99.0)
        recorded_flights(fake)(report.queries)
        self.assertEqual(read_observations(), [])
        self.assertNotEqual(str(default_state_dir()), os.environ.get("HOME"))
        self.assertFalse((default_state_dir() / HISTORY_FILE).exists())

    def test_bench_gate_strips_the_opt_in_from_its_subprocesses(self) -> None:
        seen = []

        def fake(argv, *, cwd, env):
            seen.append(dict(env))
            return subprocess.CompletedProcess(argv, 0, stdout="")

        with (
            patch.dict(os.environ, {ENV_RECORD: "1"}),
            patch.dict(os.environ),
            patch("viajante.bench._run_command", side_effect=fake),
        ):
            os.environ.pop(bench.BENCH_RUNNING_ENV, None)
            ok, _, _ = bench.run_gate(ROOT)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(ENV_RECORD not in env for env in seen))


if __name__ == "__main__":
    unittest.main()
