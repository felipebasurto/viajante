from __future__ import annotations

import _isolate  # noqa: F401
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_each_module_alone_never_reaches_the_real_state_dir(self) -> None:
        for module in (
            "test_flights.py",
            "test_trip.py",
            "test_hotel_evidence_context.py",
            "test_skiplagged_hotels.py",
            "test_isolation.py",
        ):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as real:
                env = {
                    **os.environ,
                    CHILD: "1",
                    ENV_RECORD: "1",
                    "VIAJANTE_STATE_DIR": real,
                    "XDG_STATE_HOME": real,
                    "HOME": real,
                }
                result = subprocess.run(
                    [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", module],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(os.listdir(real), [])

    def test_every_test_module_imports_the_isolation(self) -> None:
        missing = [
            path.name
            for path in sorted(Path(__file__).parent.glob("test_*.py"))
            if not re.search(r"^import _isolate\b", path.read_text(encoding="utf-8"), re.M)
        ]
        self.assertEqual(missing, [], "add `import _isolate  # noqa: F401` to these test modules")

    def test_isolation_comes_before_any_datetime_import(self) -> None:
        late = []
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            isolate = re.search(r"^import _isolate\b", text, re.M)
            clock = re.search(r"^(from datetime import|import datetime)\b", text, re.M)
            if isolate and clock and clock.start() < isolate.start():
                late.append(path.name)
        self.assertEqual(
            late,
            [],
            "`import _isolate` must precede `from datetime import`, or the module keeps the "
            "real `date` and its fixtures drift from the frozen today",
        )

    def test_today_is_frozen_for_the_tests_and_the_package(self) -> None:
        from datetime import date, datetime

        from viajante import dates, hotels

        frozen = date(*_isolate.FROZEN_TODAY)
        self.assertEqual(date.today(), frozen)
        self.assertEqual(dates.date.today(), frozen)
        self.assertEqual(hotels.date.today(), frozen)
        # A date built by datetime is still a date to isinstance checks on either side.
        self.assertIsInstance(datetime(2027, 1, 2).date(), dates.date)
        self.assertIsInstance(date(2027, 1, 2), dates.date)

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
