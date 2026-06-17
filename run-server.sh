#!/usr/bin/env bash
# Start the Flowboard server (FastAPI + worker, serves API + built SPA on one
# port). Used both manually and by the launchd service. Ensures Postgres is up
# first, then runs uvicorn in the foreground (launchd KeepAlive restarts it).
set -euo pipefail

# launchd runs with a minimal PATH — add Homebrew/Docker locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT/agent"

# Bring up Postgres in case the container is stopped (e.g. after a reboot).
docker compose up -d >/dev/null 2>&1 || true

exec .venv/bin/uvicorn flowboard.main:app \
  --host 0.0.0.0 --port "${FLOWBOARD_HTTP_PORT:-8101}"
