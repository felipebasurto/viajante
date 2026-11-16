"""Keep the whole suite off the developer's real state dir and price history.

Every ``tests/test_*.py`` imports this for its side effect, so isolation holds
however a test is run (a single module with unittest, ``discover``, or pytest).
``conftest.py`` imports it too. A new test module must do the same;
``test_isolation.py`` fails if one does not.

It also freezes ``datetime.date.today()``. The fixtures carry fixed future dates, and the
code rejects past ones, so a moving "today" would fail the suite as those dates pass. The
freeze runs before anything imports ``viajante``, so every ``from datetime import date``
sees it. Move ``FROZEN_TODAY`` forward only together with the fixture dates.
"""

import atexit
import datetime as _datetime
import os
import shutil
import tempfile
import unittest

FROZEN_TODAY = (2026, 11, 1)
_real_date = _datetime.date


class _FrozenDateMeta(type(_real_date)):
    # Dates built by datetime (``dt.date()``) are still real dates to isinstance checks.
    def __instancecheck__(cls, obj):
        return isinstance(obj, _real_date)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _real_date)


class _FrozenDate(_real_date, metaclass=_FrozenDateMeta):
    @classmethod
    def today(cls):
        return cls(*FROZEN_TODAY)


if _datetime.date is _real_date:
    _datetime.date = _FrozenDate

from viajante.ratelimit import reset_throttle_signals  # noqa: E402

_MARK = "viajante-tests-"

# The status-13 burst counter lives beside the cooldown file, with a memory
# fallback. Clear both before every test so one module's frames do not cool the next.
_run = unittest.TestCase.run


def _run_isolated(self, result=None):
    reset_throttle_signals()
    return _run(self, result)


unittest.TestCase.run = _run_isolated

os.environ.pop("VIAJANTE_PRICE_HISTORY", None)
os.environ.pop("VIAJANTE_SWEEP_MODE", None)
os.environ.pop("VIAJANTE_MCP_DEADLINE_SECONDS", None)
if _MARK not in os.environ.get("VIAJANTE_STATE_DIR", ""):
    _STATE = tempfile.mkdtemp(prefix=_MARK)
    os.environ["VIAJANTE_STATE_DIR"] = _STATE
    atexit.register(shutil.rmtree, _STATE, ignore_errors=True)
