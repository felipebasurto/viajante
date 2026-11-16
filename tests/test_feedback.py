from __future__ import annotations

import _isolate  # noqa: F401
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from viajante.cli import main
from viajante.feedback import MESSAGE_LIMIT, NEW_ISSUE_URL, REMINDER, TITLE_LIMIT, issue_url
from viajante.runtime import package_version


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class IssueUrlTests(unittest.TestCase):
    def test_link_opens_the_new_issue_form(self) -> None:
        url = issue_url()
        self.assertTrue(url.startswith(NEW_ISSUE_URL + "?"))

    def test_empty_message_prefills_only_the_facts(self) -> None:
        params = _params(issue_url())
        self.assertEqual(params["title"], [""])
        body = params["body"][0]
        self.assertIn(f"Viajante {package_version()}", body)
        self.assertIn("Python ", body)
        self.assertIn("Platform ", body)
        self.assertIn(REMINDER, body)

    def test_message_becomes_title_and_body_and_round_trips(self) -> None:
        message = "Line one & more = ? # ✈\nSecond line"
        params = _params(issue_url(message))
        self.assertEqual(params["title"], ["Line one & more = ? # ✈"])
        self.assertTrue(params["body"][0].startswith(message))

    def test_title_is_the_first_line_truncated(self) -> None:
        params = _params(issue_url("x" * (TITLE_LIMIT + 40)))
        self.assertEqual(params["title"], ["x" * TITLE_LIMIT])

    def test_surrounding_whitespace_is_dropped(self) -> None:
        self.assertEqual(_params(issue_url("  hi  "))["title"], ["hi"])


class FeedbackCommandTests(unittest.TestCase):
    def test_default_prints_the_link_and_opens_it(self) -> None:
        out = io.StringIO()
        with patch("viajante.feedback.webbrowser.open") as opened:
            with redirect_stdout(out):
                code = main(["feedback", "Booking total is off"])
        self.assertEqual(code, 0)
        url = out.getvalue().strip()
        self.assertTrue(url.startswith(NEW_ISSUE_URL))
        opened.assert_called_once_with(url)

    def test_no_browser_only_prints_the_link(self) -> None:
        out = io.StringIO()
        with patch("viajante.feedback.webbrowser.open") as opened:
            with redirect_stdout(out):
                code = main(["feedback", "--no-browser"])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith(NEW_ISSUE_URL))
        opened.assert_not_called()

    def test_message_over_the_limit_is_refused_before_any_link(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with patch("viajante.feedback.webbrowser.open") as opened:
            with redirect_stdout(out), redirect_stderr(err):
                code = main(["feedback", "x" * (MESSAGE_LIMIT + 1), "--no-browser"])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn(f"over {MESSAGE_LIMIT} characters", err.getvalue())
        opened.assert_not_called()

    def test_message_at_the_limit_is_accepted(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["feedback", "x" * MESSAGE_LIMIT, "--no-browser"])
        self.assertEqual(code, 0)
        self.assertTrue(out.getvalue().startswith(NEW_ISSUE_URL))


if __name__ == "__main__":
    unittest.main()
