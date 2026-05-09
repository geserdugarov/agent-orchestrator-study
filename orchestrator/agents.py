# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
"""Spawn a local coding-agent CLI (codex or claude) as a subprocess.

Both backends emit JSONL events on stdout. We don't pin their event-shape
contracts; instead `parse_session_id` walks the parsed JSON looking for any
UUID-shaped value at common keys (session_id, conversation_id, ...). If a
format drifts, the unit tests on parse_session_id and the claude
last-message walker will fail loudly.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
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
# untrusted and the agent runs with sandbox bypass, so a prompt injection that
# inherits these would let the agent push directly or call the API as us.
# The orchestrator owns all GitHub writes; the agent must never see them.
#
# Scope is intentionally GitHub-only: the agent's own provider auth
# (ANTHROPIC_API_KEY for claude, OpenAI keychain for codex, etc.) belongs to
# the user's pre-existing CLI login on the host and MUST be left intact.
# Do not add ANTHROPIC_API_KEY / OPENAI_API_KEY here "for symmetry" -- they
# are how the agent talks to its own model and stripping them breaks the run.
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
class AgentResult:
    session_id: Optional[str]
    last_message: str
    exit_code: int
    timed_out: bool
    stdout: str
    stderr: str


# Transitional alias for one release so external imports (debugging scripts,
# downstream tests) keep working while call sites migrate to AgentResult.
CodexResult = AgentResult


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


def _agent_env(extra_env: Optional[dict[str, str]]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _FORBIDDEN_AGENT_ENV}
    # Stamp agent commits with the orchestrator's identity. Env vars take
    # precedence over user.name/user.email from any config scope, so the
    # host's git config is untouched and no per-worktree config is needed.
    env["GIT_AUTHOR_NAME"] = config.AGENT_GIT_NAME
    env["GIT_AUTHOR_EMAIL"] = config.AGENT_GIT_EMAIL
    env["GIT_COMMITTER_NAME"] = config.AGENT_GIT_NAME
    env["GIT_COMMITTER_EMAIL"] = config.AGENT_GIT_EMAIL
    if extra_env:
        env.update(extra_env)
    return env


def _run_subprocess(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> tuple[str, str, int, bool]:
    # Spawn the agent in its own process group (start_new_session=True =>
    # setsid). On timeout we send SIGTERM to the whole group, not just the
    # direct child, so that grandchildren the agent forked (Maven, gradle,
    # JVM test runners, ...) are also reaped. Without this, a 30-min build
    # the agent kicked off keeps running for hours after the agent itself
    # was killed -- we hit exactly that with a hudi-spark scalatest sweep.
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout or "", stderr or "", proc.returncode, False
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return stdout or "", stderr or "", -1, True


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole process group, then SIGKILL after a grace window.

    ProcessLookupError races are expected (the leader may have exited between
    the Python-side timeout firing and our killpg call) -- swallow them.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_codex(
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> AgentResult:
    timeout = timeout or config.AGENT_TIMEOUT
    # The -o file lives outside the worktree (per-spawn tempfile) so the
    # target repo's `git status` never sees it as untracked. Putting it
    # inside cwd worked when the orchestrator managed its own repo (whose
    # .gitignore covers `.codex-*`), but broke `_worktree_dirty_files` on
    # any target repo without that rule -- the orchestrator would park
    # awaiting_human on its own scratch on every codex review pass.
    fd, last_msg_path_str = tempfile.mkstemp(prefix="codex-last-", suffix=".txt")
    os.close(fd)
    last_msg_path = Path(last_msg_path_str)
    # codex applies `-C` AFTER it has already chdir'd into the subprocess cwd,
    # so a relative path resolves twice (once by Popen, once by codex) and
    # codex hits "No such file or directory (os error 2)". Pass an absolute
    # path so the second resolution is a no-op. WORKTREES_DIR=../wt-...
    # in .env is the common shape that triggers this.
    cwd_abs = Path(cwd).resolve()
    try:
        # `codex exec resume` does not accept -C; we rely on subprocess cwd for it.
        common = [
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "-o", str(last_msg_path),
        ]
        if resume_session_id:
            cmd = [config.CODEX_BIN, "exec", "resume", *common, resume_session_id, prompt]
        else:
            cmd = [config.CODEX_BIN, "exec", "-C", str(cwd_abs), *common, prompt]

        env = _agent_env(extra_env)
        log.info(
            "codex spawn: cwd=%s resume=%s timeout=%ss",
            cwd, bool(resume_session_id), timeout,
        )

        stdout, stderr, exit_code, timed_out = _run_subprocess(cmd, cwd, env, timeout)

        sid = resume_session_id or parse_session_id(stdout)
        last_msg = ""
        if last_msg_path.exists():
            try:
                last_msg = last_msg_path.read_text(errors="replace")
            except OSError:
                last_msg = ""

        return AgentResult(
            session_id=sid,
            last_message=last_msg,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        try:
            last_msg_path.unlink()
        except FileNotFoundError:
            pass


def _claude_last_message(jsonl_output: str) -> str:
    """Pull the final assistant text out of claude's stream-json output.

    Prefers the terminal `{"type":"result", "result": "..."}` event, which is
    the documented final-message channel. Falls back to the last `assistant`
    or `message` event's text content for forward-compat with schema drift.
    Returns "" on total absence; the question/timeout paths in workflow.py
    already accept an empty last_message.
    """
    last_result: Optional[str] = None
    last_assistant_text: Optional[str] = None
    for line in jsonl_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        ev_type = obj.get("type")
        if ev_type == "result":
            res = obj.get("result")
            if isinstance(res, str):
                last_result = res
        elif ev_type in ("assistant", "message"):
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            content = msg.get("content")
            if isinstance(content, list):
                texts: list[str] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            texts.append(text)
                if texts:
                    last_assistant_text = "".join(texts)
            elif isinstance(content, str):
                last_assistant_text = content
    if last_result is not None:
        return last_result
    return last_assistant_text or ""


def _run_claude(
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> AgentResult:
    timeout = timeout or config.AGENT_TIMEOUT

    cmd = [
        config.CLAUDE_BIN,
        "-p",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd.append(prompt)

    env = _agent_env(extra_env)
    log.info(
        "claude spawn: cwd=%s resume=%s timeout=%ss",
        cwd, bool(resume_session_id), timeout,
    )

    stdout, stderr, exit_code, timed_out = _run_subprocess(cmd, cwd, env, timeout)

    sid = resume_session_id or parse_session_id(stdout)
    last_msg = _claude_last_message(stdout)

    return AgentResult(
        session_id=sid,
        last_message=last_msg,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )


def run_agent(
    backend: str,
    prompt: str,
    cwd: Path,
    *,
    resume_session_id: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> AgentResult:
    """Dispatch to the per-backend runner. Config validates `backend` at
    import time, but we re-check here so a misuse from non-config call sites
    fails loudly instead of silently no-opping.
    """
    if backend == "codex":
        runner = _run_codex
    elif backend == "claude":
        runner = _run_claude
    else:
        raise ValueError(
            f"unknown agent backend {backend!r}; expected 'codex' or 'claude'"
        )
    return runner(
        prompt,
        cwd,
        resume_session_id=resume_session_id,
        extra_env=extra_env,
        timeout=timeout,
    )
