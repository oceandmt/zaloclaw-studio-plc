#!/usr/bin/env bash
# Start zaloclaw-studio web panel (single process, role via ZS_ROLE).
# Usage: scripts/run_webapp.sh [HOST] [PORT]
#   defaults: HOST=${ZS_HOST:-127.0.0.1}  PORT=${ZS_PORT:-18090}
#   (bind a Tailscale IP by passing it as HOST, e.g. `tailscale ip -4`)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-${ZS_HOST:-127.0.0.1}}"
PORT="${2:-${ZS_PORT:-18090}}"
cd "$ROOT/webapp"
mkdir -p ../data
exec .venv/bin/python -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level warning
