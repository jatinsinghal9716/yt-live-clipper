#!/usr/bin/env bash
# YT Live Clipper - macOS / Linux installer (everything is free)
set -e
cd "$(dirname "$0")"

if [ "$(uname)" = "Darwin" ]; then
  command -v brew >/dev/null || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # faster-whisper has no wheels for brand-new Pythons yet -> use 3.12
  command -v python3.12 >/dev/null || brew install python@3.12
  # Homebrew's own ffmpeg has no libass (needed to burn captions) -> use the full build
  if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass "; then
    brew list ffmpeg >/dev/null 2>&1 && brew uninstall ffmpeg
    brew tap homebrew-ffmpeg/ffmpeg
    brew install homebrew-ffmpeg/ffmpeg/ffmpeg
  fi
elif ! command -v ffmpeg >/dev/null || ! command -v python3 >/dev/null; then
  if command -v apt-get >/dev/null; then
    sudo apt-get update && sudo apt-get install -y python3 python3-venv ffmpeg
  elif command -v dnf >/dev/null; then
    sudo dnf install -y python3 ffmpeg
  else
    echo "Please install python3 and ffmpeg with your package manager, then re-run."; exit 1
  fi
fi

# first Python that faster-whisper supports
for PY in python3.12 python3.13 python3.11 python3.10 python3; do command -v $PY >/dev/null && break; done
[ -d .venv ] || $PY -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -e .
ytclip doctor
echo
echo "Done! Start clipping with:"
echo "  source .venv/bin/activate"
echo "  ytclip live \"https://www.youtube.com/@ChannelName/live\" --wait"
