#!/usr/bin/env bash
# Launch the field console in the default browser (no Electron required).
set -euo pipefail
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 is not installed. Install python3 (or python) and retry." >&2
  exit 1
fi
exec "$PY" -m darksec --open "$@"