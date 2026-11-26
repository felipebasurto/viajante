"""A page that fails a Google markup check is kept in the state dir for diagnosis. Offline."""

from __future__ import annotations

import _isolate  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from viajante import google_flights_public as public


class MarkupCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"
        patcher = patch.object(public, "default_state_dir", return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keeps_the_page_and_the_failed_check(self) -> None:
        public.keep_markup_failure(
            "<html>page</html>", "occupancy echo: Number of adult passengers"
        )
        html = self.state / public.MARKUP_FAILURE_HTML
        note = self.state / public.MARKUP_FAILURE_NOTE
        self.assertEqual(html.read_text(encoding="utf-8"), "<html>page</html>")
        self.assertIn("Number of adult passengers", note.read_text(encoding="utf-8"))

    def test_a_newer_failure_replaces_the_older_one(self) -> None:
        public.keep_markup_failure("<html>old</html>", "a")
        public.keep_markup_failure("<html>new</html>", "b")
        self.assertEqual(
            (self.state / public.MARKUP_FAILURE_HTML).read_text(encoding="utf-8"),
            "<html>new</html>",
        )

    def test_a_write_error_never_changes_the_search(self) -> None:
        with patch.object(public, "write_text_atomic", side_effect=OSError("disk full")):
            public.keep_markup_failure("<html/>", "x")  # must not raise


if __name__ == "__main__":
    unittest.main()
