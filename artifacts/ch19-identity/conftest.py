"""Make ``import broker`` mean *this* chapter's broker.

The root ``conftest.py`` puts every ``artifacts/chNN-slug/`` directory on
``sys.path``, which is what makes the printed ``python
artifacts/chNN-slug/demo.py`` commands resolve their imports. Under one
``pytest`` run over all of ``artifacts/`` that has a consequence: several
chapters ship a ``policy.py`` and every chapter ships a ``demo.py``, so
whichever chapter's tests ran first would own the plain module name for
the rest of the session.

Evicting modules that were imported out of a *sibling* artifact directory
fixes that without renaming anything the book prints. ``conftest`` modules
are left alone; those belong to pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE.parent

sys.path.insert(0, str(HERE))

for _name, _module in list(sys.modules.items()):
    _file = getattr(_module, "__file__", None)
    if not _file:
        continue
    _path = Path(_file).resolve()
    if _path.name == "conftest.py" or ARTIFACTS not in _path.parents:
        continue
    if not _path.is_relative_to(HERE):
        del sys.modules[_name]
