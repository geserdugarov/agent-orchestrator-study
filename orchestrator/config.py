"""Configuration loaded from .env / process environment.

Secrets are deliberately NOT loaded from REPO_ROOT/.env. The implementer agent
runs in a sibling worktree with sandbox bypass, so anything readable inside
REPO_ROOT (including .env) is recoverable by a prompt-injected agent via a
relative-path read like `cat ../agent-orchestrator-study/.env`. GITHUB_TOKEN is
only read from the process environment or from a token file outside REPO_ROOT
(default `~/.config/<owner>/<repo>/token` derived from REPO, override with
ORCHESTRATOR_TOKEN_FILE).
"""
from __future__ import annotations

import os
import sys
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

REPO: str = os.environ.get("REPO", "podlodka-ai-club/spark-gap")
GITHUB_TOKEN: str = _resolve_github_token(REPO)
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))
REVIEW_TIMEOUT: int = int(os.environ.get("REVIEW_TIMEOUT", str(AGENT_TIMEOUT)))
MAX_REVIEW_ROUNDS: int = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
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
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")

# git identity injected into each codex spawn via GIT_AUTHOR_*/GIT_COMMITTER_*
# env vars (see agents.run_codex). Env vars take precedence over user.name and
# user.email from any config scope, so agent commits are attributable to the
# orchestrator without touching the host's git config or the shared repo
# config. The default email uses the GitHub-recognized noreply form so it
# won't bounce and won't link to a real user account.
AGENT_GIT_NAME: str = os.environ.get("AGENT_GIT_NAME", "agent-orchestrator")
AGENT_GIT_EMAIL: str = os.environ.get(
    "AGENT_GIT_EMAIL", "agent-orchestrator@users.noreply.github.com"
)

WORKTREES_DIR: Path = Path(
    os.environ.get("WORKTREES_DIR", str(REPO_ROOT.parent / "wt-orchestrator"))
)

BASE_BRANCH: str = os.environ.get("BASE_BRANCH", "main")
