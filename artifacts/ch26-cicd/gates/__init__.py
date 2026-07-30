"""The two release gates the CI workflow runs.

Both are invoked as modules, from this directory::

    python -m gates.reliability --scenarios critical --k 5 \
        --min-pass-k 0.90 --baseline baselines/main.json \
        --max-regression 0.02
    python -m gates.trajectory \
        --forbid "issue_refund before get_policy" \
        --forbid "issue_refund without idempotency_key" \
        --require-state-grader

Both exit non-zero when they block, print why, and take their comparison
from a stored baseline rather than from an absolute score. A gate that
compares against a fixed number tells you whether the agent is good; a
gate that compares against the version it replaces tells you whether the
change made it worse, which is the question a release actually asks.
"""

from __future__ import annotations

__all__ = ["reliability", "trajectory"]
