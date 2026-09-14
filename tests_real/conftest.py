"""Real-Hummingbot integration tests (no ``sys.modules`` stubs).

Run these on their own so the stubs in ``tests/conftest.py`` are never injected
into the process::

    pytest tests_real/ -v

They are skipped automatically when Hummingbot is not importable (the package
is not on PyPI; see the README for the conda/compile procedure).
"""

import sys
from pathlib import Path

_src = str(Path(__file__).parent.parent / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
