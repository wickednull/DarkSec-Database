#!/usr/bin/env bash
# Launch the Electron desktop shell. Requires `npm install` once.
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x node_modules/.bin/electron ]]; then
  echo "Electron is not installed. Run: npm install" >&2
  exit 1
fi
exec node_modules/.bin/electron . "$@"
