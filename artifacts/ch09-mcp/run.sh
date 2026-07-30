#!/usr/bin/env bash
# The Chapter 9 artifact, over either transport.
#
#   ./run.sh stdio              # client spawns the server as a subprocess
#   ./run.sh http               # one endpoint, token required
#   ./run.sh http --no-token    # expect 401 plus the discovery hint
#   ./run.sh                    # everything, including the negative cases
#
# Offline, stdlib only, no credentials. The "subprocess" and the endpoint
# are both in-process mocks; see the README for what that does and does not
# model.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"

if [ -n "${PYTHON:-}" ]; then
  PY="${PYTHON}"
elif [ -x "${ROOT}/.venv/bin/python" ]; then
  PY="${ROOT}/.venv/bin/python"
else
  PY="python3"
fi

MODE="${1:-all}"
case "${MODE}" in
  stdio|http|all) ;;
  -h|--help)
    sed -n '2,10p' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "usage: ./run.sh [stdio|http|all] [--no-token]" >&2
    exit 2
    ;;
esac
shift || true

exec "${PY}" "${HERE}/demo.py" "${MODE}" "$@"
