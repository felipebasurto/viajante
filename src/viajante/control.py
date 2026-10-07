"""Cooperative cancel and deadline for one search.

A search checks its ``SearchControl`` between queries, between retries, and in every
sleep. Cancel raises ``SearchCancelled`` (a ``BaseException``, so no ``except Exception``
retry loop can swallow it). A deadline raises ``SearchDeadline`` (an ``Exception``) at the
next checkpoint, so each unfinished query is classified like any other failed query, with
code ``deadline``, and the rest of the report is kept.

The active control is ambient per thread: library entry points decorated with
``controlled`` accept optional ``cancel`` / ``deadline_seconds`` keywords and inherit an
enclosing control when none is named. Without either, nothing here changes behavior.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import inspect
import math
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Callable, Iterator, Optional

POLL_SECONDS = 0.05
DEADLINE_MESSAGE = (
    "deadline_seconds elapsed before this query finished. It was not loaded; "
    "that is not proof the provider has nothing."
)


class SearchCancelled(BaseException):
    """The caller cancelled the search. Nothing is returned, cached, or recorded."""


class SearchDeadline(Exception):
    """deadline_seconds elapsed. Raised at a checkpoint, classified as code ``deadline``."""

    def __init__(self, message: str = DEADLINE_MESSAGE) -> None:
        super().__init__(message)


def validate_deadline_seconds(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("deadline_seconds must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("deadline_seconds must be a positive, finite number of seconds")
    return float(value)


class SearchControl:
    def __init__(
        self,
        cancel: Optional[threading.Event] = None,
        deadline_seconds: Optional[float] = None,
        *,
        progress: Optional[Callable[[str], None]] = None,
        deadline_at: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
        parent: Optional["SearchControl"] = None,
    ) -> None:
        seconds = validate_deadline_seconds(deadline_seconds)
        self.cancel = cancel if cancel is not None else threading.Event()
        self.progress = progress
        self.clock = clock
        self.cut = False
        self._parent = parent
        own = None if seconds is None else clock() + seconds
        self.deadline_at = min((t for t in (own, deadline_at) if t is not None), default=None)

    def mark_cut(self) -> None:
        """A deadline cut part of this search; it and any enclosing search are not complete."""
        control: Optional[SearchControl] = self
        while control is not None:
            control.cut = True
            control = control._parent

    def expired(self) -> bool:
        return self.deadline_at is not None and self.clock() >= self.deadline_at

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise SearchCancelled()

    def checkpoint(self) -> None:
        self.check_cancelled()
        if self.expired():
            self.mark_cut()
            raise SearchDeadline()

    def wait(self, seconds: float) -> None:
        """Sleep up to ``seconds``. Cancel raises; the deadline only cuts the sleep short."""
        limit = max(0.0, seconds)
        if self.deadline_at is not None:
            left = max(0.0, self.deadline_at - self.clock())
            if left < limit:
                self.mark_cut()
            limit = min(limit, left)
        if self.cancel.wait(limit):
            raise SearchCancelled()

    def result(self, future: Future, timeout: float) -> Any:
        """``future.result(timeout)`` that gives up on cancel or deadline and drops the work."""
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            try:
                return future.result(timeout=max(0.0, min(POLL_SECONDS, left)))
            except FutureTimeout:
                if left <= POLL_SECONDS:
                    raise
            try:
                self.checkpoint()
            except BaseException:
                future.cancel()
                raise


_ambient = threading.local()


def current_control() -> Optional[SearchControl]:
    return getattr(_ambient, "control", None)


@contextlib.contextmanager
def active(control: Optional[SearchControl]) -> Iterator[Optional[SearchControl]]:
    previous = current_control()
    _ambient.control = control
    try:
        yield control
    finally:
        _ambient.control = previous


def note_cut() -> None:
    """Record that a deadline cost this search something even though no error escaped."""
    control = current_control()
    if control is not None:
        control.mark_cut()


def cut_by_deadline(result: Any) -> Any:
    """A finished report that a deadline touched must not read as complete."""
    if hasattr(result, "deadline_cut"):
        return dataclasses.replace(result, deadline_cut=True)
    coverage = getattr(result, "coverage", None)
    if coverage is None or coverage.stopping_reason == "deadline":
        return result
    note = "deadline_seconds cut part of this search; that evidence was not loaded"
    unsearched = f"{coverage.unsearched}; {note}" if coverage.unsearched else note
    return dataclasses.replace(
        result,
        coverage=dataclasses.replace(
            coverage, complete=False, stopping_reason="deadline", unsearched=unsearched
        ),
    )


def checkpoint() -> None:
    control = current_control()
    if control is not None:
        control.checkpoint()


def check_cancelled() -> None:
    """Discard a cancelled result without cutting evidence completed at a deadline."""
    control = current_control()
    if control is not None:
        control.check_cancelled()


def interruptible_sleep(seconds: float) -> None:
    control = current_control()
    if control is None:
        time.sleep(seconds)
    else:
        control.wait(seconds)


def wait_for_future(future: Future, timeout: float) -> Any:
    control = current_control()
    if control is None:
        return future.result(timeout=timeout)
    return control.result(future, timeout)


def controlled(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Run a public search inside a control built from its ``cancel`` / ``deadline_seconds``.

    With neither named, an enclosing control (the MCP worker's) stays in force and its
    progress callback fills a missing ``progress``.
    """
    takes_progress = "progress" in inspect.signature(fn).parameters

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        parent = current_control()
        control = parent
        if kwargs.get("cancel") is not None or kwargs.get("deadline_seconds") is not None:
            control = SearchControl(
                kwargs.get("cancel") or (parent.cancel if parent else None),
                kwargs.get("deadline_seconds"),
                progress=parent.progress if parent else None,
                deadline_at=parent.deadline_at if parent else None,
                parent=parent,
            )
        if takes_progress and kwargs.get("progress") is None and control is not None:
            kwargs["progress"] = control.progress
        with active(control):
            result = fn(*args, **kwargs)
            check_cancelled()
        if control is not None and control.cut:
            result = cut_by_deadline(result)
        return result

    return wrapper
