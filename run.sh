#!/usr/bin/env bash
# Self-restarting orchestrator wrapper. Exits cleanly when the orchestrator
# detects a self-modifying merge so the new code is picked up on next loop.
set -uo pipefail
cd "$(dirname "$0")"
git pull --ff-only origin main || true
while true; do
    .venv/bin/python -m orchestrator.main "$@"
    rc=$?
    echo "[$(date -Iseconds)] orchestrator exited with code $rc; restarting in 1s..."
    sleep 1
    git pull --ff-only origin main || true
done
