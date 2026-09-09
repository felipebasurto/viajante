"""Lazy Chromium session shared by Google Flights and Booking.com."""

from __future__ import annotations

import atexit
import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})


def _sync_playwright():
    # Detail-only: sweep must import this module without a Playwright install.
    from playwright.sync_api import sync_playwright

    return sync_playwright()


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class BrowserSessionConfig:
    state_filename: str
    locale: str
    html_lang: str
    currency: str = "EUR"
    country: Optional[str] = None
    viewport: Optional[Mapping[str, int]] = None
    user_agent: Optional[str] = None
    blocked_resource_types: Optional[frozenset[str]] = None

    def blocked_types(self) -> frozenset[str]:
        if self.blocked_resource_types is None:
            return BLOCKED_RESOURCE_TYPES
        return frozenset(self.blocked_resource_types)


class ChromiumSession:
    def __init__(self, state_dir: Path, config: BrowserSessionConfig) -> None:
        self._state_dir = state_dir
        self._config = config
        self._state_path = state_dir / config.state_filename
        self._pw = None
        self._browser = None
        self._context = None
        self._atexit_registered = False

    def new_page(self) -> object:
        return self._ensure_context().new_page()

    def reset(self) -> None:
        with contextlib.suppress(Exception):
            self._persist_state()
        self._teardown()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._persist_state()
        self._teardown()

    def _ensure_context(self) -> object:
        if self._context is None:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._pw = _sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            options: dict[str, object] = {
                "locale": self._config.locale,
                "storage_state": str(self._state_path) if self._state_path.exists() else None,
            }
            if self._config.viewport is not None:
                options["viewport"] = dict(self._config.viewport)
            if self._config.user_agent is not None:
                options["user_agent"] = self._config.user_agent
            self._context = self._browser.new_context(**options)
            self._context.route("**/*", self._block_heavy_resources)
            if not self._atexit_registered:
                atexit.register(self.close)
                self._atexit_registered = True
        return self._context

    def _block_heavy_resources(self, route) -> None:
        if route.request.resource_type in self._config.blocked_types():
            route.abort()
        else:
            route.continue_()

    def _persist_state(self) -> None:
        if self._context is None:
            return
        self._state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".json.tmp")
        try:
            self._context.storage_state(path=str(temporary))
            os.replace(temporary, self._state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def _teardown(self) -> None:
        for handle in (self._context, self._browser):
            if handle is not None:
                with contextlib.suppress(Exception):
                    handle.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                self._pw.stop()
        self._pw = self._browser = self._context = None
