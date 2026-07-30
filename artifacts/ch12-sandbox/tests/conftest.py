"""Path bootstrap for the tests one directory below the artifact.

The artifact's own ``conftest.py`` one level up already inserts the
chapter directory on ``sys.path`` and holds every fixture these tests
use. This exists so that ``pytest artifacts/ch12-sandbox/tests`` resolves
``import egress`` the same way ``python artifacts/ch12-sandbox/demo.py``
does, without any chapter module having to be imported as a package.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
