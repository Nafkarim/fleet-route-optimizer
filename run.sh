#!/usr/bin/env bash
# One-command launcher: sets up Python deps (first run only), then starts the dashboard.
# The first start runs the optimizer (~1 minute) if no saved plan exists.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python environment (first run only)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
( sleep 2 && open "http://localhost:${PORT}" 2>/dev/null || true ) &
exec .venv/bin/uvicorn server.app:app --port "${PORT}"
