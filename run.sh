#!/usr/bin/env bash
# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
# Self-restarting orchestrator wrapper. Exits cleanly when the orchestrator
# detects a self-modifying merge so the new code is picked up on next loop.
set -uo pipefail
cd "$(dirname "$0")"

# Read ORCHESTRATOR_BASE_BRANCH from .env so the wrapper pulls the orchestrator
# repo's own branch (REPO_ROOT) for self-update -- not BASE_BRANCH, which is
# the *target* repo's base branch and may differ (e.g. target=`master` while
# the orchestrator itself ships from `main`).
base_branch="${ORCHESTRATOR_BASE_BRANCH:-}"
if [ -z "$base_branch" ] && [ -f .env ]; then
    base_branch=$(sed -n 's/^[[:space:]]*ORCHESTRATOR_BASE_BRANCH[[:space:]]*=[[:space:]]*//p' .env \
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
