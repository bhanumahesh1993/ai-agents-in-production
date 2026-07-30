"""Make the hyphenated per-chapter artifact directories importable.

The book prints paths like ``artifacts/ch01-first-agent/``. A hyphen is not
valid in a Python module name, so these directories are not packages; each is
put on ``sys.path`` instead, which is also how the printed ``python
artifacts/chNN-slug/demo.py`` commands resolve their imports.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
for _d in sorted((ROOT / "artifacts").glob("ch*-*")):
    if _d.is_dir():
        sys.path.insert(0, str(_d))
