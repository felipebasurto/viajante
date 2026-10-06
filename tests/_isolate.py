"""Keep the whole suite off the developer's real state dir and price history.

Every ``tests/test_*.py`` imports this for its side effect, so isolation holds
however a test is run (a single module with unittest, ``discover``, or pytest).
``conftest.py`` imports it too. A new test module must do the same;
``test_isolation.py`` fails if one does not.
"""

import atexit
import os
import shutil
import tempfile

_MARK = "viajante-tests-"

os.environ.pop("VIAJANTE_PRICE_HISTORY", None)
if _MARK not in os.environ.get("VIAJANTE_STATE_DIR", ""):
    _STATE = tempfile.mkdtemp(prefix=_MARK)
    os.environ["VIAJANTE_STATE_DIR"] = _STATE
    atexit.register(shutil.rmtree, _STATE, ignore_errors=True)
