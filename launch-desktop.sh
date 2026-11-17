#!/usr/bin/env bash
# Launch the Electron desktop shell. Requires `npm install` once.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x node_modules/.bin/electron ]]; then
  echo "Electron is not installed. Run: ./install.sh (or npm install)" >&2
  exit 1
fi
if [[ -x ".venv/bin/python" ]]; then
  export DARKSEC_PYTHON="${DARKSEC_PYTHON:-$PWD/.venv/bin/python}"
fi
exec node_modules/.bin/electron . "$@"
