"""Configuration loaded from .env / process environment."""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        os.environ.setdefault(key, value)


_load_dotenv()

GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
REPO: str = os.environ.get("REPO", "geserdugarov/agent-orchestrator-study")
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "60"))
AGENT_TIMEOUT: int = int(os.environ.get("AGENT_TIMEOUT", "1800"))
HITL_HANDLE: str = os.environ.get("HITL_HANDLE", "geserdugarov")
CODEX_BIN: str = os.environ.get("CODEX_BIN", "codex")
CLAUDE_BIN: str = os.environ.get("CLAUDE_BIN", "claude")

WORKTREES_DIR: Path = Path(
    os.environ.get("WORKTREES_DIR", str(REPO_ROOT.parent / "wt-orchestrator"))
)

BASE_BRANCH: str = os.environ.get("BASE_BRANCH", "main")
