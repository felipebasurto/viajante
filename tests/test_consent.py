from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _isolate  # noqa: F401
from viajante.consent import (
    CONSENT_FILE,
    CONSENT_MAX_AGE_SECONDS,
    is_reject_form,
    restore_consent_cookies,
    save_consent_cookies,
)
from viajante.google_flights import ChromeSweepClient

NOW = 1_800_000_000.0
INTERSTITIAL = "https://consent.google.com/m?continue=https://www.google.com/travel/flights"
RESULTS = "https://www.google.com/travel/flights?tfs=x"


class FakeCookies:
    """Stands in for the curl_cffi cookie jar: set() and a .jar of cookie records."""

    def __init__(self) -> None:
        self.jar: list[SimpleNamespace] = []

    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        self.jar.append(
            SimpleNamespace(name=name, value=value, domain=domain, path=path, expires=None)
        )


def _form(set_eom: str, set_sc: str) -> str:
    return (
        '<form action="https://consent.google.com/save">'
        f'<input name="set_eom" value="{set_eom}"><input name="set_sc" value="{set_sc}">'
        f'<input name="continue" value="{RESULTS}"></form>'
    )


class _Response(SimpleNamespace):
    pass


class _ConsentSession:
    """Serves the consent interstitial until a form is posted, then the results page."""

    def __init__(self, server_cookies: list[tuple[str, str, str]]) -> None:
        self.cookies = FakeCookies()
        self._server_cookies = server_cookies
        self.posts: list[str] = []

    async def post(self, url: str, **_: object) -> _Response:
        self.posts.append(url)
        for name, value, domain in self._server_cookies:
            self.cookies.set(name, value, domain=domain)
        return _Response(url=RESULTS, text="")


class ConsentStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {"VIAJANTE_STATE_DIR": self._dir.name})
        self._env.start()
        self.path = Path(self._dir.name) / CONSENT_FILE

    def tearDown(self) -> None:
        self._env.stop()
        self._dir.cleanup()

    def _write(self, payload: object) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def _cookie(self, name: str = "SOCS", **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "name": name,
            "value": "reject-value",
            "domain": ".google.com",
            "path": "/",
            "expires": None,
        }
        record.update(overrides)
        return record

    def test_reject_form_is_recognised_and_accept_form_is_not(self) -> None:
        self.assertTrue(is_reject_form({"set_eom": "true", "set_sc": "false"}))
        self.assertFalse(is_reject_form({"set_eom": "true", "set_sc": "true"}))
        self.assertFalse(is_reject_form({"set_sc": "true"}))

    def test_save_keeps_only_the_consent_cookie(self) -> None:
        session = SimpleNamespace(cookies=FakeCookies())
        session.cookies.set("SOCS", "v1", domain=".google.com")
        session.cookies.set("__Secure-ENID", "v2", domain=".google.com")
        session.cookies.set("CONSENT", "v3", domain="consent.google.com")
        session.cookies.set("tracker", "v4", domain="ads.example.net")
        session.cookies.set("SOCS", "v5", domain="evil-google.com")

        self.assertEqual(save_consent_cookies(session, now=NOW), 1)

        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["stored_at"], NOW)
        self.assertEqual([(c["name"], c["value"]) for c in saved["cookies"]], [("SOCS", "v1")])

    def test_restore_sets_saved_cookies_on_a_fresh_session(self) -> None:
        self._write({"stored_at": NOW, "cookies": [self._cookie()]})
        session = SimpleNamespace(cookies=FakeCookies())

        self.assertEqual(restore_consent_cookies(session, now=NOW + 60), 1)
        self.assertEqual(session.cookies.jar[0].name, "SOCS")
        self.assertEqual(session.cookies.jar[0].domain, ".google.com")

    def test_restore_ignores_missing_corrupt_stale_expired_and_foreign_entries(self) -> None:
        session = SimpleNamespace(cookies=FakeCookies())
        self.assertEqual(restore_consent_cookies(session, now=NOW), 0)

        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(restore_consent_cookies(session, now=NOW), 0)

        self._write({"stored_at": NOW, "cookies": [self._cookie()]})
        too_old = NOW + CONSENT_MAX_AGE_SECONDS + 1
        self.assertEqual(restore_consent_cookies(session, now=too_old), 0)

        self._write(
            {
                "stored_at": NOW,
                "cookies": [
                    self._cookie(expires=NOW - 10),
                    self._cookie(domain="ads.example.net"),
                    self._cookie(domain="evil-google.com"),
                    self._cookie(value=None),
                    self._cookie("__Secure-ENID"),
                    self._cookie(),
                ],
            }
        )
        self.assertEqual(restore_consent_cookies(session, now=NOW), 1)
        self.assertEqual(
            [(c.name, c.domain) for c in session.cookies.jar], [("SOCS", ".google.com")]
        )

    def test_save_failure_is_not_fatal(self) -> None:
        session = SimpleNamespace(cookies=FakeCookies())
        session.cookies.set("SOCS", "v1", domain=".google.com")
        with patch("viajante.consent.write_json_atomic", side_effect=OSError("disk full")):
            self.assertEqual(save_consent_cookies(session, now=NOW), 0)

    def test_a_later_interstitial_is_dismissed_again(self) -> None:
        reject_page = _Response(url=INTERSTITIAL, text=_form(set_eom="true", set_sc="false"))
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._consent_lock = asyncio.Lock()
        client._session = _ConsentSession([("SOCS", "reject-value", ".google.com")])
        with patch("viajante.consent.time") as clock:
            clock.time.return_value = NOW
            self.assertTrue(asyncio.run(client._dismiss_consent(reject_page, timeout=5)))
            self.assertTrue(asyncio.run(client._dismiss_consent(reject_page, timeout=5)))
        # The first dismissal does not cover a second interstitial later in the process.
        self.assertEqual(len(client._session.posts), 2)

    def test_declined_consent_is_persisted_and_reused_by_a_new_client(self) -> None:
        reject_page = _Response(url=INTERSTITIAL, text=_form(set_eom="true", set_sc="false"))
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._consent_lock = asyncio.Lock()
        client._session = _ConsentSession([("SOCS", "reject-value", ".google.com")])

        with patch("viajante.consent.time") as clock:
            clock.time.return_value = NOW
            self.assertTrue(asyncio.run(client._dismiss_consent(reject_page, timeout=5)))
        self.assertEqual(client._session.posts, ["https://consent.google.com/save"])
        self.assertTrue(self.path.exists())

        # A new process restores the cookie and never sees the interstitial.
        fresh = SimpleNamespace(cookies=FakeCookies())
        self.assertEqual(restore_consent_cookies(fresh, now=NOW), 1)
        self.assertEqual(fresh.cookies.jar[0].value, "reject-value")

    def test_accepted_consent_is_never_persisted(self) -> None:
        accept_page = _Response(url=INTERSTITIAL, text=_form(set_eom="true", set_sc="true"))
        client = ChromeSweepClient.__new__(ChromeSweepClient)
        client._consent_lock = asyncio.Lock()
        client._session = _ConsentSession([("SOCS", "accept-value", ".google.com")])

        self.assertTrue(asyncio.run(client._dismiss_consent(accept_page, timeout=5)))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
