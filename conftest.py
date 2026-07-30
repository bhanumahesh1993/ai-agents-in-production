"""Root pytest configuration.

Chapter suites are *not* collected into this process. They share module
basenames that the book prints -- ``router.py`` in ch04 and ch25, ``demo.py``
everywhere, ch15's ``detectors.py`` against ch16's ``detectors/`` package -- so
each runs in its own interpreter, driven from
``tests/test_chapter_suites.py``. That file explains why cache and path
manipulation was tried and abandoned.

Running a chapter directly still works and is the normal way to read one:

    pytest artifacts/ch04-patterns
    python artifacts/ch04-patterns/demo.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
