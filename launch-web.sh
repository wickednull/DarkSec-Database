#!/usr/bin/env bash
# Launch the field console in the default browser (no Electron required).
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m darksec --open "$@"
