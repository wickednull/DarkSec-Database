#!/usr/bin/env bash
# Launch the field console in the default browser (no Electron required).
set -euo pipefail
cd "$(dirname "$0")"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "Python 3 is not installed. Install python3 (or python) and run ./install.sh." >&2
  exit 1
fi

# Surface missing document/OCR tooling instead of failing silently later.
if ! "$PY" tools/doctor.py >/dev/null 2>&1; then
  echo "DarkSec dependency warning:" >&2
  "$PY" tools/doctor.py 2>&1 | sed 's/^/  /' >&2
  echo "Run ./install.sh to install the missing packages. Starting anyway." >&2
fi

exec "$PY" -m darksec --open "$@"
