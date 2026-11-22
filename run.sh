#!/bin/sh
# Sets up a local venv (Pillow, certifi, anthropic, numpy, pywebview) and keeps it in sync with
# requirements.txt, then starts the app. Translation needs ANTHROPIC_API_KEY set.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating .venv..."
  python3 -m venv .venv || exit 1
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  echo "Installing requirements..."
  .venv/bin/pip install -q -r requirements.txt && touch .venv/.installed || exit 1
fi
exec .venv/bin/python app.py "$@"
