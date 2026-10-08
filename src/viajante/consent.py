"""Google consent cookies a new process may reuse: reject-only, consent cookie only, capped."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from viajante.storage import default_state_dir, write_json_atomic

CONSENT_FILE = "google-consent.json"
CONSENT_MAX_AGE_SECONDS = 30 * 24 * 3600
# The consent record only. Other cookies Google sets on the reject flow are identifiers
# and are not needed to skip the interstitial, so they are never written to disk.
CONSENT_COOKIE_NAMES = frozenset({"SOCS"})


def is_reject_form(fields: Mapping[str, str]) -> bool:
    """True for the consent form that declines non-essential cookies (never accept-all)."""
    return fields.get("set_eom") == "true" and fields.get("set_sc") != "true"


def _google_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == "google.com" or host.endswith(".google.com")


def _consent_path() -> Path:
    return default_state_dir() / CONSENT_FILE


def restore_consent_cookies(session: Any, *, now: Optional[float] = None) -> int:
    """Load saved reject-consent cookies into a session jar. A bad file is ignored."""
    try:
        state = json.loads(_consent_path().read_text(encoding="utf-8"))
        stored_at = float(state["stored_at"])
        cookies = state["cookies"]
    except (OSError, ValueError, TypeError, KeyError):
        return 0
    clock = time.time() if now is None else now
    if not isinstance(cookies, list) or clock - stored_at > CONSENT_MAX_AGE_SECONDS:
        return 0
    loaded = 0
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name, value = item.get("name"), item.get("value")
        domain, path = item.get("domain"), item.get("path") or "/"
        expires = item.get("expires")
        if not (
            isinstance(name, str)
            and isinstance(value, str)
            and isinstance(domain, str)
            and isinstance(path, str)
        ):
            continue
        if name not in CONSENT_COOKIE_NAMES or not _google_domain(domain):
            continue
        if isinstance(expires, (int, float)) and expires <= clock:
            continue
        session.cookies.set(name, value, domain=domain, path=path)
        loaded += 1
    return loaded


def save_consent_cookies(session: Any, *, now: Optional[float] = None) -> int:
    """Persist the consent cookie a reject left in the jar, and nothing else."""
    clock = time.time() if now is None else now
    cookies = [
        {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "expires": cookie.expires,
        }
        for cookie in session.cookies.jar
        if cookie.name in CONSENT_COOKIE_NAMES
        and isinstance(cookie.value, str)
        and _google_domain(cookie.domain or "")
    ]
    if not cookies:
        return 0
    try:
        write_json_atomic({"stored_at": clock, "cookies": cookies}, _consent_path())
    except OSError:
        return 0
    return len(cookies)
