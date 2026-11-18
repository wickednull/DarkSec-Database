#!/usr/bin/env bash
# Wrapper for launching the installer from a file manager or desktop shortcut.
# It keeps the terminal open afterwards so errors can be read.
set -u
cd "$(dirname "$0")"
bash install.sh "$@"
status=$?
echo
if [ "$status" -ne 0 ]; then
  echo "Installer exited with status $status."
fi
read -r -p "Press Enter to close..." _
