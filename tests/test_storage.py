from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from viajante.flights import write_report_atomic
from viajante.models import SearchReport
from viajante.storage import default_state_dir, write_json_atomic, write_text_atomic


class DefaultStateDirTests(unittest.TestCase):
    def test_viajante_state_dir_wins(self) -> None:
        with patch.dict(
            os.environ,
            {
                "VIAJANTE_STATE_DIR": "/custom/viajante",
                "XDG_STATE_HOME": "/xdg/state",
            },
            clear=False,
        ):
            self.assertEqual(default_state_dir(), Path("/custom/viajante"))

    def test_xdg_state_home_when_no_viajante(self) -> None:
        env = os.environ.copy()
        env.pop("VIAJANTE_STATE_DIR", None)
        env["XDG_STATE_HOME"] = "/xdg/state"
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_state_dir(), Path("/xdg/state/viajante"))

    def test_default_home_local_state(self) -> None:
        env = os.environ.copy()
        env.pop("VIAJANTE_STATE_DIR", None)
        env.pop("XDG_STATE_HOME", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("viajante.storage.Path.home", return_value=Path("/home/user")):
                self.assertEqual(
                    default_state_dir(),
                    Path("/home/user/.local/state/viajante"),
                )


class WriteJsonAtomicTests(unittest.TestCase):
    def test_creates_parents_and_writes_utf8_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "data.json"
            write_json_atomic({"schema_version": 1, "value": "café"}, path)
            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["value"], "café")
            self.assertIn("\n", path.read_text(encoding="utf-8"))

    def test_write_text_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dump.html"
            write_text_atomic("<html>ok</html>", path)
            self.assertEqual(path.read_text(encoding="utf-8"), "<html>ok</html>")
            self.assertFalse(path.with_suffix(".html.tmp").exists())

    def test_replaces_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            path.write_text('{"old": true}', encoding="utf-8")
            write_json_atomic({"new": True}, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": True})


class WriteReportAtomicTests(unittest.TestCase):
    def test_write_report_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "out.json"
            report = SearchReport(
                searched_at=datetime(2026, 8, 10, 9, 0, 0),
                queries=(),
            )
            write_report_atomic(report, path)
            self.assertTrue(path.exists())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
