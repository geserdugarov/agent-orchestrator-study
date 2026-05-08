# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Configuration loaded from .env / process environment.

Secrets are deliberately NOT loaded from REPO_ROOT/.env. The implementer agent
runs in a sibling worktree with sandbox bypass, so anything readable inside
REPO_ROOT (including .env) is recoverable by a prompt-injected agent via a
relative-path read like `cat ../agent-orchestrator/.env`. GITHUB_TOKEN is
only read from the process environment or from a token file outside REPO_ROOT
(default `~/.config/<owner>/<repo>/token` derived from REPO, override with
ORCHESTRATOR_TOKEN_FILE).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Keys whose values must never be loaded from REPO_ROOT/.env. The agent has
# read access to that file via the orchestrator checkout; secrets belong in
# process env or in a file outside REPO_ROOT.
_SECRET_KEYS = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
})


def _load_dotenv() -> None:
    if os.environ.get("ORCHESTRATOR_SKIP_DOTENV", "").strip().lower() in (
        "1", "true", "on", "yes",
    ):
        return
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in _SECRET_KEYS:
            print(
                f"orchestrator: ignoring {key} in {env_path}; the implementer "
                f"agent can read this file. Move the token to "
                f"~/.config/<owner>/<repo>/token (path derived from REPO) "
                f"or export {key} before launching.",
                file=sys.stderr,
            )
            continue
        os.environ.setdefault(key, value)


def _resolve_github_token(repo: str) -> str:
    """Resolve GITHUB_TOKEN from process env or a file outside REPO_ROOT.

    Default file path is `~/.config/<owner>/<repo>/token`, derived from REPO so
    a single host can drive multiple repos without colliding token files.
    Returns "" when neither is set; GitHubClient surfaces the actionable error.
    """
    env_val = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_val:
        return env_val
    default_path = Path.home() / ".config" / repo / "token"
    token_file = Path(os.environ.get("ORCHESTRATOR_TOKEN_FILE", str(default_path)))
    try:
        return token_file.read_text().strip()
    except FileNotFoundError:
        return ""
    except OSError as e:
        print(
            f"orchestrator: could not read token file {token_file}: {e}",
            file=sys.stderr,
        )
        return ""


_load_dotenv()


def _parse_hitl_handles(raw: str) -> tuple[str, ...]:
    handles: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        handle = part.strip().lstrip("@").strip()
        if not handle or handle in seen:
            continue
        handles.append(handle)
        seen.add(handle)
    return tuple(handles)

REPO: str = os.environ.get("REPO", "geserdugarov/agent-orchestrator")
GITHUB_TOKEN: str = _resolve_github_token(REPO)
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))

# Persistent log location. main.py attaches a FileHandler here in addition to
# the existing stderr stream, so post-mortems don't depend on the terminal
# `run.sh` was started in. Already covered by the `*.log` .gitignore rule.
LOG_DIR: Path = Path(os.environ.get("LOG_DIR", str(REPO_ROOT / "logs")))
REVIEW_TIMEOUT: int = int(os.environ.get("REVIEW_TIMEOUT", str(AGENT_TIMEOUT)))
MAX_REVIEW_ROUNDS: int = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
# Cap on how many auto-conflict-resolution attempts one PR can use before
# `_handle_resolving_conflict` parks awaiting human. Mirrors the
# `MAX_REVIEW_ROUNDS` shape so a stuck rebase loop cannot burn tokens
# indefinitely.
MAX_CONFLICT_ROUNDS: int = int(os.environ.get("MAX_CONFLICT_ROUNDS", "3"))
# Cap on how many fresh implementing-codex spawns one issue can use within a
# 24h window opened at the first counted attempt. The window resets once 24h
# elapses since that start. Resumes on human reply do not count. 0 = unbounded
# (matches MAX_REVIEW_ROUNDS's implied semantics).
MAX_RETRIES_PER_DAY: int = int(os.environ.get("MAX_RETRIES_PER_DAY", "3"))
HITL_HANDLES: tuple[str, ...] = (
    _parse_hitl_handles(os.environ.get("HITL_HANDLE", "geserdugarov"))
    or ("geserdugarov",)
)
HITL_HANDLE: str = ",".join(HITL_HANDLES)
HITL_MENTIONS: str = " ".join(f"@{handle}" for handle in HITL_HANDLES)
# Comma-separated GitHub logins whose unlabeled issues the orchestrator is
# willing to auto-pick-up. Empty (the default) disables the allowlist and
# preserves the legacy "anyone can trigger" behavior. Set this on a public
# repo to keep random users from spending the orchestrator's compute budget
# on useless tasks. The guard only fires at pickup; if a maintainer manually
# labels an outsider's issue (e.g. `implementing`) the workflow still drives
# it to completion, so this is purely a triage filter.
ALLOWED_ISSUE_AUTHORS: tuple[str, ...] = _parse_hitl_handles(
    os.environ.get("ALLOWED_ISSUE_AUTHORS", "")
)
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")


def _parse_backend(name: str, value: str) -> str:
    v = (value or "").strip().lower()
    if v not in ("codex", "claude"):
        raise SystemExit(
            f"orchestrator: {name}={value!r} is invalid; "
            "expected 'codex' or 'claude'"
        )
    return v


# Default split: claude implements, codex reviews. Validated at import so a
# typo in the deployment env aborts the process before the first GitHub call.
DEV_AGENT: str = _parse_backend("DEV_AGENT", os.environ.get("DEV_AGENT", "claude"))
REVIEW_AGENT: str = _parse_backend("REVIEW_AGENT", os.environ.get("REVIEW_AGENT", "codex"))
# Decomposer is a separate role from implementing/reviewing -- it reads the
# issue and produces a structured manifest. Parsed at import time even when
# DECOMPOSE=off so flipping the kill switch back on does not introduce a
# fresh "that env var was always invalid" failure.
DECOMPOSE_AGENT: str = _parse_backend(
    "DECOMPOSE_AGENT", os.environ.get("DECOMPOSE_AGENT", "claude")
)

# git identity injected into each agent spawn via GIT_AUTHOR_*/GIT_COMMITTER_*
# env vars (see agents._agent_env). Env vars take precedence over user.name
# and user.email from any config scope, so agent commits are attributable to
# the orchestrator without touching the host's git config or the shared repo
# config. The default email uses the GitHub-recognized noreply form so it
# won't bounce and won't link to a real user account.
AGENT_GIT_NAME: str = os.environ.get("AGENT_GIT_NAME", "agent-orchestrator")
AGENT_GIT_EMAIL: str = os.environ.get(
    "AGENT_GIT_EMAIL", "agent-orchestrator@users.noreply.github.com"
)

# The repository whose issues / PRs this orchestrator manages. Defaults to
# REPO_ROOT (self-bootstrap: orchestrator manages its own repo). Override when
# the orchestrator code is installed in one clone but drives PRs into another.
# Worktrees are `git worktree add`-ed from this path, so commits land on its
# git history -- not the orchestrator's own.
TARGET_REPO_ROOT: Path = Path(
    os.environ.get("TARGET_REPO_ROOT", str(REPO_ROOT))
)

WORKTREES_DIR: Path = Path(
    os.environ.get("WORKTREES_DIR", str(TARGET_REPO_ROOT.parent / "wt-orchestrator"))
)

# Base branch in the *target* repo: where worktrees branch from and where PRs
# are opened against.
BASE_BRANCH: str = os.environ.get("BASE_BRANCH", "main")


@dataclass(frozen=True)
class RepoSpec:
    """Per-repo identity threaded through the workflow.

    Replaces the global `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH` reads
    inside workflow.py so a future multi-repo loop can drive several repos
    from one orchestrator process without touching module-level state.
    """

    slug: str
    target_root: Path
    base_branch: str


def _parse_repos_env(raw: str) -> list[RepoSpec]:
    """Parse the REPOS env value into a list of RepoSpecs.

    Format: one entry per line, ``owner/name|target_root|base_branch``.
    Blank lines and lines starting with ``#`` are skipped. ``;`` is also
    accepted as an entry separator so the value fits on a single line in a
    ``.env`` file (the simple parser in `_load_dotenv` cannot represent
    multi-line values). Aborts (SystemExit) on malformed entries or
    duplicate slugs; a missing ``target_root`` is warned to stderr but not
    fatal so a freshly-cloned host can still start the orchestrator and
    notice the problem on the first tick rather than at import.
    """
    specs: list[RepoSpec] = []
    seen: set[str] = set()
    # ';' accepted in addition to '\n' so the value can be one line in .env.
    for entry_no, raw_line in enumerate(
        raw.replace(";", "\n").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 3:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} is malformed "
                f"(expected 'owner/name|target_root|base_branch'): {line!r}"
            )
        slug, target_root, base_branch = (p.strip() for p in parts)
        if (
            not slug
            or "/" not in slug
            or slug.startswith("/")
            or slug.endswith("/")
        ):
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has invalid "
                f"owner/name {slug!r}; expected 'owner/name'"
            )
        if not target_root:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has empty target_root"
            )
        if not base_branch:
            raise SystemExit(
                f"orchestrator: REPOS entry #{entry_no} has empty base_branch"
            )
        if slug in seen:
            raise SystemExit(
                f"orchestrator: REPOS lists duplicate slug {slug!r}; "
                "each repo can appear only once"
            )
        seen.add(slug)
        target_path = Path(target_root)
        if not target_path.exists():
            print(
                f"orchestrator: REPOS entry {slug!r} target_root "
                f"{target_path} does not exist; worktree creation will fail",
                file=sys.stderr,
            )
        specs.append(
            RepoSpec(
                slug=slug, target_root=target_path, base_branch=base_branch
            )
        )
    if not specs:
        raise SystemExit(
            "orchestrator: REPOS is set but contains no valid entries; "
            "either unset it or provide at least one "
            "'owner/name|target_root|base_branch' entry"
        )
    return specs


_REPOS_RAW: str = os.environ.get("REPOS", "")
_REPO_SPECS: list[RepoSpec] = (
    _parse_repos_env(_REPOS_RAW)
    if _REPOS_RAW.strip()
    else [
        RepoSpec(
            slug=REPO,
            target_root=TARGET_REPO_ROOT,
            base_branch=BASE_BRANCH,
        )
    ]
)


def default_repo_specs() -> list[RepoSpec]:
    """The configured RepoSpecs (validated at import).

    A single element built from `REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`
    when `REPOS` is unset (so existing single-repo deployments keep working
    unchanged); otherwise one element per `REPOS` entry. Returns a fresh
    list copy so callers cannot mutate the cached result.
    """
    return list(_REPO_SPECS)

# Base branch of the orchestrator's *own* repo (REPO_ROOT). Used only by the
# self-update path: `_self_modifying_merge_happened` watches `origin/<this>`
# for new commits under `orchestrator/`, and `run.sh` fast-forwards to it on
# every restart. Decoupled from BASE_BRANCH so the target repo can have a
# different default branch (e.g. `master`) without breaking self-update.
ORCHESTRATOR_BASE_BRANCH: str = os.environ.get("ORCHESTRATOR_BASE_BRANCH", "main")

# When `in_review` and the reviewer (and any branch protections GitHub knows
# about) are happy, the orchestrator can merge the PR itself. Default off so
# the legacy "humans merge" behavior keeps working until users opt in.
AUTO_MERGE: bool = os.environ.get("AUTO_MERGE", "off").strip().lower() in (
    "1", "true", "on", "yes",
)
# Quiet window after the most recent PR/issue comment before resuming the dev
# session in `in_review`. Matches the 10-minute target in docs/workflow.md.
IN_REVIEW_DEBOUNCE_SECONDS: int = int(
    os.environ.get("IN_REVIEW_DEBOUNCE_SECONDS", "600")
)

# Kill switch for the entire `decomposing` stage. off -> revert to the
# legacy "no label -> implementing" pickup, no children, no manifest. The
# rollout safety valve so the user can disable decomposition if manifest
# output proves unreliable, without redeploying old binaries.
DECOMPOSE: bool = os.environ.get("DECOMPOSE", "on").strip().lower() in (
    "1", "true", "on", "yes",
)

# After the reviewer agent emits VERDICT: APPROVED, squash the dev's commits
# on the PR branch into a single conventional-commit-shaped commit and
# force-push (with lease). Default on -- a one-commit PR is what humans
# expect on merge. Off restores the legacy "leave the dev's commit history
# as-is" behavior; useful if a workflow downstream (changelog generation,
# bisect tooling) depends on the per-step commit history.
SQUASH_ON_APPROVAL: bool = os.environ.get(
    "SQUASH_ON_APPROVAL", "on"
).strip().lower() in ("1", "true", "on", "yes")
