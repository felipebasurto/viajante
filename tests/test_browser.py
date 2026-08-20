from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from viajante.browser import (
    BLOCKED_RESOURCE_TYPES,
    BrowserSessionConfig,
    ChromiumSession,
)


class FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class FakeRoute:
    def __init__(self, resource_type: str) -> None:
        self.request = FakeRequest(resource_type)
        self.aborted = False
        self.continued = False

    def abort(self) -> None:
        self.aborted = True

    def continue_(self) -> None:
        self.continued = True


class FakeContext:
    def __init__(self, page=None, *, storage_error: Exception | None = None) -> None:
        self.page = page
        self.route_calls: list[tuple] = []
        self.storage_paths: list[str] = []
        self.storage_error = storage_error
        self.closed = False

    def route(self, pattern, handler) -> None:
        self.route_calls.append((pattern, handler))

    def new_page(self):
        return self.page

    def storage_state(self, *, path: str) -> None:
        self.storage_paths.append(path)
        if self.storage_error is not None:
            raise self.storage_error
        Path(path).write_text('{"cookies": []}', encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context: FakeContext | None = None) -> None:
        self.context = context or FakeContext()
        self.context_options: list[dict] = []
        self.closed = False

    def new_context(self, **options):
        self.context_options.append(options)
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.launch_options: list[dict] = []

    def launch(self, **options):
        self.launch_options.append(options)
        return self.browser


class FakePlaywright:
    def __init__(self, chromium: FakeChromium | None = None) -> None:
        self.chromium = chromium or FakeChromium(FakeBrowser())
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright
        self.start_calls = 0

    def start(self):
        self.start_calls += 1
        return self.playwright


class ChromiumSessionTests(unittest.TestCase):
    def test_lazy_start_configures_context_and_registers_atexit_once(self) -> None:
        context = FakeContext()
        browser = FakeBrowser(context)
        chromium = FakeChromium(browser)
        playwright = FakePlaywright(chromium)
        starter = FakePlaywrightStarter(playwright)

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            state_path = state_dir / "pw_state_google.json"
            state_path.write_text('{"cookies": []}', encoding="utf-8")
            session = ChromiumSession(
                state_dir,
                BrowserSessionConfig(
                    state_filename="pw_state_google.json",
                    locale="en-US",
                    html_lang="en",
                ),
            )

            with (
                patch("viajante.browser._sync_playwright", return_value=starter),
                patch("viajante.browser.atexit.register") as register,
            ):
                first = session._ensure_context()
                second = session._ensure_context()

            self.assertIs(first, context)
            self.assertIs(second, context)
            self.assertEqual(starter.start_calls, 1)
            self.assertEqual(chromium.launch_options, [{"headless": True}])
            self.assertEqual(
                browser.context_options,
                [
                    {
                        "locale": "en-US",
                        "storage_state": str(state_path),
                    }
                ],
            )
            self.assertEqual(context.route_calls[0][0], "**/*")
            register.assert_called_once_with(session.close)
            session.close()

    def test_optional_viewport_and_user_agent_are_passed_through(self) -> None:
        context = FakeContext()
        browser = FakeBrowser(context)
        chromium = FakeChromium(browser)
        playwright = FakePlaywright(chromium)
        starter = FakePlaywrightStarter(playwright)

        with tempfile.TemporaryDirectory() as tmp:
            session = ChromiumSession(
                Path(tmp),
                BrowserSessionConfig(
                    state_filename="pw_state_booking.json",
                    locale="de-DE",
                    html_lang="de",
                    viewport={"width": 1280, "height": 900},
                    user_agent="TestAgent/1.0",
                ),
            )
            with patch("viajante.browser._sync_playwright", return_value=starter):
                session._ensure_context()

        self.assertEqual(
            browser.context_options,
            [
                {
                    "locale": "de-DE",
                    "storage_state": None,
                    "viewport": {"width": 1280, "height": 900},
                    "user_agent": "TestAgent/1.0",
                }
            ],
        )

    def test_resource_blocking_aborts_heavy_types(self) -> None:
        self.assertEqual(BLOCKED_RESOURCE_TYPES, frozenset({"image", "media", "font"}))
        with tempfile.TemporaryDirectory() as tmp:
            session = ChromiumSession(
                Path(tmp),
                BrowserSessionConfig(
                    state_filename="pw_state_google.json",
                    locale="en-US",
                    html_lang="en",
                ),
            )
            image_route = FakeRoute("image")
            script_route = FakeRoute("script")
            session._block_heavy_resources(image_route)
            session._block_heavy_resources(script_route)
            self.assertTrue(image_route.aborted)
            self.assertTrue(script_route.continued)

    def test_custom_blocked_types_can_allow_fonts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = ChromiumSession(
                Path(tmp),
                BrowserSessionConfig(
                    state_filename="pw_state_booking.json",
                    locale="de-DE",
                    html_lang="de",
                    blocked_resource_types=frozenset({"image", "media"}),
                ),
            )
            font_route = FakeRoute("font")
            image_route = FakeRoute("image")
            session._block_heavy_resources(font_route)
            session._block_heavy_resources(image_route)
            self.assertTrue(font_route.continued)
            self.assertTrue(image_route.aborted)

    def test_persist_state_is_atomic_and_cleans_tmp_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            destination = state_dir / "pw_state_google.json"
            destination.write_text('{"cookies": ["old"]}', encoding="utf-8")
            context = FakeContext(storage_error=RuntimeError("write failed"))
            session = ChromiumSession(
                state_dir,
                BrowserSessionConfig(
                    state_filename="pw_state_google.json",
                    locale="en-US",
                    html_lang="en",
                ),
            )
            session._context = context

            with self.assertRaisesRegex(RuntimeError, "write failed"):
                session._persist_state()

            self.assertEqual(destination.read_text(encoding="utf-8"), '{"cookies": ["old"]}')
            self.assertFalse(destination.with_suffix(".json.tmp").exists())

    def test_close_persists_state_and_tears_down(self) -> None:
        context = FakeContext()
        browser = FakeBrowser()
        playwright = FakePlaywright()

        with tempfile.TemporaryDirectory() as tmp:
            session = ChromiumSession(
                Path(tmp),
                BrowserSessionConfig(
                    state_filename="pw_state_google.json",
                    locale="en-US",
                    html_lang="en",
                ),
            )
            session._context = context
            session._browser = browser
            session._pw = playwright
            session.close()

            state_path = Path(tmp) / "pw_state_google.json"
            self.assertEqual(state_path.read_text(encoding="utf-8"), '{"cookies": []}')
            self.assertFalse(state_path.with_suffix(".json.tmp").exists())

        self.assertTrue(context.closed)
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)
        self.assertIsNone(session._context)


if __name__ == "__main__":
    unittest.main()
