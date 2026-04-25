"""Spawn the local codex CLI as a subprocess.

Codex's --json output emits JSONL events. We don't pin its event-shape
contract; instead we walk the parsed JSON looking for any UUID-shaped value
at common keys (session_id, conversation_id, ...). If the format drifts, the
unit test on parse_session_id will fail loudly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config

log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PRIORITY_KEYS = ("session_id", "conversation_id", "thread_id", "session", "id")

# Strip GitHub credentials from the agent's environment. Issue/comment text is
# untrusted and codex runs with sandbox bypass, so a prompt injection that
# inherits these would let the agent push directly or call the API as us.
# The orchestrator owns all GitHub writes; the agent must never see them.
_FORBIDDEN_AGENT_ENV = frozenset({
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GIT_TOKEN",
    "GH_HOST",
})


@dataclass
class CodexResult:
    session_id: Optional[str]
    last_message: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str


def _walk_for_uuid(obj: Any) -> Optional[str]:
    if isinstance(obj, str):
        return obj if _UUID_RE.match(obj) else None
    if isinstance(obj, dict):
        for key in _PRIORITY_KEYS:
            if key in obj:
                found = _walk_for_uuid(obj[key])
                if found:
                    return found
        for value in obj.values():
            found = _walk_for_uuid(value)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _walk_for_uuid(item)
            if found:
                return found
    return None


def parse_session_id(jsonl_output: str) -> Optional[str]:
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = _walk_for_uuid(obj)
        if sid:
            return sid
    return None


def run_codex(
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> CodexResult:
    timeout = timeout or config.AGENT_TIMEOUT
    last_msg_path = cwd / ".codex-last-message.txt"
    if last_msg_path.exists():
        last_msg_path.unlink()

    # `codex exec resume` does not accept -C; we rely on subprocess cwd for it.
    common = [
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "-o", str(last_msg_path),
    ]
    if resume_session_id:
        cmd = [config.CODEX_BIN, "exec", "resume", *common, resume_session_id, prompt]
    else:
        cmd = [config.CODEX_BIN, "exec", "-C", str(cwd), *common, prompt]

    env = {k: v for k, v in os.environ.items() if k not in _FORBIDDEN_AGENT_ENV}
    if extra_env:
        env.update(extra_env)
    log.info(
        "codex spawn: cwd=%s resume=%s timeout=%ss",
        cwd, bool(resume_session_id), timeout,
    )

    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        exit_code = -1

    sid = resume_session_id or parse_session_id(stdout)
    last_msg = ""
    if last_msg_path.exists():
        try:
            last_msg = last_msg_path.read_text(errors="replace")
        except OSError:
            last_msg = ""

    return CodexResult(
        session_id=sid,
        last_message=last_msg,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )
