"""Configuration loaded from .env / process environment.

Secrets are deliberately NOT loaded from REPO_ROOT/.env. The implementer agent
runs in a sibling worktree with sandbox bypass, so anything readable inside
REPO_ROOT (including .env) is recoverable by a prompt-injected agent via a
relative-path read like `cat ../agent-orchestrator-study/.env`. GITHUB_TOKEN is
only read from the process environment or from a token file outside REPO_ROOT
(default `~/.config/agent-orchestrator-study/token`, override with
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
                f"~/.config/agent-orchestrator-study/token or export "
                f"{key} before launching.",
                file=sys.stderr,
            )
            continue
        os.environ.setdefault(key, value)


def _resolve_github_token() -> str:
    """Resolve GITHUB_TOKEN from process env or a file outside REPO_ROOT.

    Returns "" when neither is set; GitHubClient surfaces the actionable error.
    """
    env_val = os.environ.get("GITHUB_TOKEN", "").strip()
    if env_val:
        return env_val
    default_path = Path.home() / ".config" / "agent-orchestrator-study" / "token"
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

GITHUB_TOKEN: str = _resolve_github_token()
REPO: str = os.environ.get("REPO", "geserdugarov/agent-orchestrator-study")
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))
HITL_HANDLES: tuple[str, ...] = (
    _parse_hitl_handles(os.environ.get("HITL_HANDLE", "geserdugarov"))
    or ("geserdugarov",)
)
HITL_HANDLE: str = ",".join(HITL_HANDLES)
HITL_MENTIONS: str = " ".join(f"@{handle}" for handle in HITL_HANDLES)
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")

WORKTREES_DIR: Path = Path(
    os.environ.get("WORKTREES_DIR", str(REPO_ROOT.parent / "wt-orchestrator"))
)

BASE_BRANCH: str = os.environ.get("BASE_BRANCH", "main")
