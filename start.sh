#!/usr/bin/env bash
# One-command start for NeverTwice (macOS / Linux):  ./start.sh
# Creates the virtual environment, installs dependencies, checks your keys and starts the app.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
if [ -z "$PY" ]; then
  echo "NeverTwice needs Python 3.10 or newer. On macOS: brew install python@3.12"; exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating virtual environment with $PY ..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env — add your keys, then run ./start.sh again:"
  echo "  HINDSIGHT_API_KEY  -> https://ui.hindsight.vectorize.io"
  echo "  LLM_API_KEY        -> https://console.groq.com/keys"
  exit 1
fi

python scripts/check_setup.py || {
  echo
  echo "Fix the items above. If the memory bank is empty, run:  source .venv/bin/activate && python scripts/seed_memory.py"
  exit 1
}
exec python run.py
