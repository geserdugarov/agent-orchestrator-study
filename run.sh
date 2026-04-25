#!/usr/bin/env bash
# Self-restarting orchestrator wrapper. Exits cleanly when the orchestrator
# detects a self-modifying merge so the new code is picked up on next loop.
set -uo pipefail
cd "$(dirname "$0")"

# Read BASE_BRANCH from .env so the wrapper pulls the same branch the Python
# code uses for worktrees and self-update detection. Without this, configuring
# BASE_BRANCH=foo would still pull origin main here and merge stale or wrong
# code on restart.
base_branch="${BASE_BRANCH:-}"
if [ -z "$base_branch" ] && [ -f .env ]; then
    base_branch=$(sed -n 's/^[[:space:]]*BASE_BRANCH[[:space:]]*=[[:space:]]*//p' .env \
        | head -n1 | tr -d '"' | tr -d "'")
fi
base_branch="${base_branch:-main}"

git pull --ff-only origin "$base_branch" || true
while true; do
    .venv/bin/python -m orchestrator.main "$@"
    rc=$?
    echo "[$(date -Iseconds)] orchestrator exited with code $rc; restarting in 1s..."
    sleep 1
    git pull --ff-only origin "$base_branch" || true
done
