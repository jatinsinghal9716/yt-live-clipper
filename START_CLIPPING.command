#!/usr/bin/env bash
# Double-click on a Mac: opens the clipper page in your browser.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/ytclip ]; then
  echo "Run install_mac_linux.sh first:  bash install_mac_linux.sh"; read -r -p "Press Enter to close"; exit 1
fi
. .venv/bin/activate
echo "Keep this window open while clipping. Close it to quit."
exec ytclip web
