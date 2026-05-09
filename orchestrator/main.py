# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Polling-loop entry point.

Run with `python -m orchestrator.main` (or `--once` for a single tick).

The loop self-exits when it detects a merge to origin/main that touches its
own source files, so the wrapper script can pick up the new code.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import subprocess
import sys
import time
from typing import Optional

from . import config, workflow
from .github import GitHubClient

log = logging.getLogger("orchestrator")

_running = True


def _shutdown(signum, _frame) -> None:
    global _running
    log.info("signal %s received; will stop after this tick", signum)
    _running = False


def _configure_logging(level: str) -> None:
    # stderr stays for live tailing in `run.sh`'s terminal; the file handler
    # is what survives terminal close. RotatingFileHandler caps disk use
    # without needing logrotate on the host.
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.LOG_DIR / "orchestrator.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    except OSError as e:
        # Don't refuse to start just because the log dir is unwritable;
        # stderr alone keeps the loop usable. Surface the reason once.
        logging.basicConfig(level=level, format=fmt, handlers=handlers)
        logging.getLogger("orchestrator").warning(
            "file logging disabled: %s (%s)", config.LOG_DIR, e
        )
        return
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(config.REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _own_head_sha() -> Optional[str]:
    r = _git("rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _self_modifying_merge_happened(start_sha: str) -> bool:
    """Detect that origin/<orchestrator-base> has moved FORWARD from start_sha
    and the new commits touch orchestrator/. Watches the orchestrator's own
    repo (REPO_ROOT), not the target repo, so a separately-configured target
    branch (e.g. `master`) does not interfere with self-update detection.
    """
    _git("fetch", "--quiet", "origin", config.ORCHESTRATOR_BASE_BRANCH)
    cur = _git("rev-parse", f"origin/{config.ORCHESTRATOR_BASE_BRANCH}").stdout.strip()
    if not cur or cur == start_sha:
        return False
    # start_sha must be an ancestor of origin/main for this to be a merge that
    # advanced the upstream ref past where we started.
    if _git("merge-base", "--is-ancestor", start_sha, cur).returncode != 0:
        return False
    diff = _git("diff", "--name-only", start_sha, cur).stdout
    return any(line.startswith("orchestrator/") for line in diff.splitlines())


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Agent orchestrator polling loop.")
    p.add_argument("--once", action="store_true", help="Run a single tick and exit.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    _configure_logging(args.log_level)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Single-repo today; the helper returns a one-element list so the next
    # multi-repo child can extend `default_repo_specs()` without touching
    # main.py's loop shape.
    specs = config.default_repo_specs()
    clients: list[tuple[config.RepoSpec, GitHubClient]] = []
    for spec in specs:
        gh = GitHubClient(repo_spec=spec)
        log.info("connected: repo=%s", spec.slug)
        gh.ensure_workflow_labels()
        clients.append((spec, gh))

    if args.once:
        for spec, gh in clients:
            workflow.tick(gh, spec)
        return 0

    own_sha = _own_head_sha()
    log.info("own HEAD=%s", own_sha)

    while _running:
        if own_sha and _self_modifying_merge_happened(own_sha):
            log.info("self-modifying merge detected; exiting for restart")
            return 0
        for spec, gh in clients:
            log.info("tick: repo=%s", spec.slug)
            try:
                workflow.tick(gh, spec)
            except Exception:
                log.exception("tick failed for repo=%s; continuing", spec.slug)
        for _ in range(config.POLL_INTERVAL):
            if not _running:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
