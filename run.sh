#!/bin/sh
# Sets up a local venv (Pillow, certifi, anthropic, numpy, pywebview, Vision OCR) and keeps it
# in sync with requirements.txt, then starts the app at http://127.0.0.1:8765.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  if ! command -v python3 >/dev/null 2>&1 || \
     ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    echo "Webtoon Downloader needs Python 3.10 or newer."
    echo "Install it from https://www.python.org/downloads/ (the macOS installer), then run this again."
    exit 1
  fi
  echo "First run: setting up (takes a minute)..."
  python3 -m venv .venv || exit 1
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing requirements..."
  .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt && touch .venv/.installed || exit 1
fi
exec .venv/bin/python app.py "$@"
