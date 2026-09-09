"""Shared pacing, backoff, and failure classification for both providers."""

from __future__ import annotations

from viajante.models import SearchError, SearchErrorCode

REQUEST_DELAY_SECONDS = 4.5
REQUEST_JITTER_SECONDS = 1.5
# HTTP sweep is not a visible browser scrape; do not inherit the 4.5s pace.
SWEEP_DELAY_SECONDS = 0.0
SWEEP_JITTER_SECONDS = 0.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 8.0
BACKOFF_JITTER_SECONDS = 3.0

BROWSER_UNAVAILABLE_MARKERS = (
    "executable doesn't exist",
    "playwright install",
    "browsertype.launch",
    "no module named 'playwright'",
)

NON_RETRIABLE_CODES = frozenset(
    {
        SearchErrorCode.NO_RESULTS,
        SearchErrorCode.REJECTED,
        SearchErrorCode.BLOCKED,
        SearchErrorCode.MARKUP_DRIFT,
        SearchErrorCode.BROWSER_UNAVAILABLE,
    }
)

ERROR_MESSAGE_MAX_CHARS = 500
BROWSER_INSTALL_HINT = (
    "Chromium is not available to Playwright. "
    "Install viajante[browser] and run 'playwright install chromium'."
)


def _clip_error_message(text: str, *, limit: int = ERROR_MESSAGE_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def classify_failure(exc: BaseException) -> SearchError:
    text = f"{type(exc).__name__}: {exc}".strip()
    lowered = text.casefold()
    if any(marker in lowered for marker in BROWSER_UNAVAILABLE_MARKERS):
        return SearchError(
            code=SearchErrorCode.BROWSER_UNAVAILABLE,
            message=BROWSER_INSTALL_HINT,
        )
    return SearchError(code=SearchErrorCode.FETCH_FAILED, message=_clip_error_message(text))


def retry_backoff_seconds(attempt: int, random_gen) -> float:
    return BACKOFF_BASE_SECONDS * (2**attempt) + random_gen.uniform(0, BACKOFF_JITTER_SECONDS)


def inter_query_delay_seconds(random_gen) -> float:
    return REQUEST_DELAY_SECONDS + random_gen.uniform(0, REQUEST_JITTER_SECONDS)


def sweep_inter_query_delay_seconds(random_gen) -> float:
    return SWEEP_DELAY_SECONDS + random_gen.uniform(0, SWEEP_JITTER_SECONDS)
