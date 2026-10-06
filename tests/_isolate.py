"""Keep the whole suite off the developer's real state dir and price history.

Imported for its side effect by ``conftest.py`` (pytest) and ``test_isolation.py``
(unittest discovery imports every test module before running any test).
"""

import atexit
import os
import shutil
import tempfile

os.environ.pop("VIAJANTE_PRICE_HISTORY", None)
_STATE = tempfile.mkdtemp(prefix="viajante-tests-")
os.environ["VIAJANTE_STATE_DIR"] = _STATE
atexit.register(shutil.rmtree, _STATE, ignore_errors=True)
