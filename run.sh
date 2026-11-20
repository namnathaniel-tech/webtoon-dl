#!/bin/sh
# Sets up a local venv (Pillow for PDF, certifi for HTTPS) on first run, then starts the app.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv and installing requirements..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
exec .venv/bin/python app.py "$@"
