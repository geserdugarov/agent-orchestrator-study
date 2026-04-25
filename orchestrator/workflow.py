"""State machine: drive issues through the orchestrator workflow.

v0 only implements (no label) -> implementing -> in_review.
Other labels are observed and logged as not-yet-implemented.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from github.Issue import Issue

from . import config
from .agents import CodexResult, run_codex
from .github import GitHubClient, PinnedState

log = logging.getLogger(__name__)

# Disable git's /dev/tty fallback prompts in any subprocess we spawn.
_GIT_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _branch_name(issue_number: int) -> str:
    return f"orchestrator/issue-{issue_number}"


def _worktree_path(issue_number: int) -> Path:
    return config.WORKTREES_DIR / f"issue-{issue_number}"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NO_PROMPT_ENV},
    )


def _ensure_worktree(issue_number: int) -> Path:
    """Return a worktree on a per-issue branch, reusing one with unpushed work.

    The reuse is what lets the orchestrator survive a crash between codex
    committing and the orchestrator pushing -- without it, the next tick would
    wipe the worktree and we'd burn another codex run on the same prompt.
    """
    config.WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    wt = _worktree_path(issue_number)
    branch = _branch_name(issue_number)

    if wt.exists():
        if _has_new_commits(wt):
            log.info("issue=#%d worktree has unpushed commits; reusing", issue_number)
            return wt
        _git("worktree", "remove", "--force", str(wt), cwd=config.REPO_ROOT)

    _git("fetch", "--quiet", "origin", config.BASE_BRANCH, cwd=config.REPO_ROOT)

    have_branch = _git(
        "rev-parse", "--verify", branch, cwd=config.REPO_ROOT
    ).returncode == 0
    if have_branch:
        result = _git("worktree", "add", str(wt), branch, cwd=config.REPO_ROOT)
    else:
        result = _git(
            "worktree", "add", "-b", branch, str(wt),
            f"origin/{config.BASE_BRANCH}",
            cwd=config.REPO_ROOT,
        )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return wt


def _has_new_commits(worktree: Path) -> bool:
    r = _git(
        "rev-list", "--count", f"origin/{config.BASE_BRANCH}..HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return False
    return int((r.stdout or "0").strip() or "0") > 0


def _worktree_dirty_files(worktree: Path) -> list[str]:
    """Paths git considers modified or untracked in the worktree.

    Used to refuse opening a PR when codex committed only part of its work and
    left other modifications behind -- the push would publish an incomplete
    branch. Ignored files are excluded by default in porcelain mode, so the
    orchestrator scratch (`.codex-last-message.txt`, matched by `.codex-*` in
    .gitignore) does not surface here.
    """
    r = _git("status", "--porcelain", cwd=worktree)
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for line in (r.stdout or "").splitlines():
        if len(line) < 4:
            continue
        # porcelain v1: "XY <path>" with optional " -> dest" for renames.
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        path = rest.strip().strip('"')
        if path:
            paths.append(path)
    return paths


def _push_branch(worktree: Path, branch: str) -> bool:
    """Push via GIT_ASKPASS so the token never appears in argv.

    The push target URL carries only the username (`x-access-token`); the
    token itself is read from the GIT_TOKEN env var by a tempfile askpass
    script. This keeps the PAT out of `/proc/<pid>/cmdline`, which is
    world-readable on Linux. We also use an explicit `HEAD:refs/heads/<branch>`
    refspec so no upstream is set and no remote URL is stored in .git/config.

    The worktree is shared with the codex agent, so anything in `.git/hooks/`
    or `.git/config` is attacker-controlled. The agent also writes as the same
    OS user, so it can plant `~/.gitconfig` (or anything pointed at by
    XDG_CONFIG_HOME) before we push. We harden the push so a planted pre-push
    hook, credential helper, fsmonitor, or url-rewrite rule cannot observe
    GIT_TOKEN or redirect the push to an attacker-controlled host:
      * `core.hooksPath=/dev/null` disables `.git/hooks/*` and any hooksPath
        override the agent set in the local config.
      * `credential.helper=` (empty) clears all inherited credential helpers
        so a repo-local helper script never executes with GIT_TOKEN in env.
      * `core.fsmonitor=` disables any fsmonitor program git would otherwise
        spawn for index-touching operations.
      * `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_SYSTEM=/dev/null` block
        global/system config entirely, so url.<host>.insteadOf or
        pushInsteadOf rules planted in `~/.gitconfig` (or `/etc/gitconfig`)
        cannot rewrite our auth URL and exfiltrate the askpass token.
      * We also refuse to push if the local config contains any url
        insteadOf/pushInsteadOf rewrite, since those rewrite our auth URL
        and would deliver the token to whatever host the agent picked.
    """
    if not config.GITHUB_TOKEN:
        log.error("GITHUB_TOKEN missing; cannot push")
        return False
    rewrite = subprocess.run(
        ["git", "config", "--local", "--get-regexp",
         r"^url\..*\.(insteadof|pushinsteadof)$"],
        cwd=str(worktree), capture_output=True, text=True,
    )
    if rewrite.returncode == 0 and rewrite.stdout.strip():
        log.error(
            "refusing to push %s: worktree .git/config has url rewrite rules: %s",
            branch, rewrite.stdout.strip(),
        )
        return False
    auth_url = f"https://x-access-token@github.com/{config.REPO}.git"
    with tempfile.TemporaryDirectory(prefix="orch-askpass-") as td:
        askpass = Path(td) / "askpass.sh"
        askpass.write_text('#!/bin/sh\nprintf %s "$GIT_TOKEN"\n')
        askpass.chmod(0o700)
        env = {
            **os.environ,
            **_GIT_NO_PROMPT_ENV,
            "GIT_ASKPASS": str(askpass),
            "GIT_TOKEN": config.GITHUB_TOKEN,
            # Detach from any agent-writable global/system git config; the
            # only config that applies is the local worktree config (already
            # checked above) plus our explicit -c overrides below.
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        r = subprocess.run(
            [
                "git",
                "-c", "core.hooksPath=/dev/null",
                "-c", "credential.helper=",
                "-c", "core.fsmonitor=",
                "push", auth_url, f"HEAD:refs/heads/{branch}",
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=env,
        )
    if r.returncode != 0:
        # Scrub the token out of any error output before logging.
        scrubbed = (r.stderr or "").replace(config.GITHUB_TOKEN, "***")
        log.error("git push failed for %s: %s", branch, scrubbed)
        return False
    return True


def _build_implement_prompt(issue: Issue, comments_text: str) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    return (
        f"You are the implementer for GitHub issue #{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        "Implement the change in the current working directory (a fresh git worktree on a "
        "new branch). When done, COMMIT your changes with a clear message. Do NOT push - "
        "the orchestrator pushes and opens the PR.\n\n"
        "If you cannot proceed because of missing information, leave the working tree "
        "uncommitted (no commits) and end your response with a clear question for the human."
    )


def _recent_comments_text(issue: Issue, max_chars: int = 4000) -> str:
    chunks: list[str] = []
    for c in issue.get_comments():
        body = c.body or ""
        if "<!--orchestrator-state" in body:
            continue
        login = c.user.login if c.user else "user"
        chunks.append(f"@{login}: {body}")
    text = "\n\n".join(chunks)
    return text[-max_chars:] if len(text) > max_chars else text


def tick(gh: GitHubClient) -> None:
    for issue in gh.list_open_issues():
        try:
            _process_issue(gh, issue)
        except Exception:
            log.exception("issue=#%s processing failed", issue.number)


def _process_issue(gh: GitHubClient, issue: Issue) -> None:
    label = gh.workflow_label(issue)
    log.info("issue=#%s label=%r", issue.number, label)
    if label is None:
        _handle_pickup(gh, issue)
    elif label == "implementing":
        _handle_implementing(gh, issue)
    elif label == "in_review":
        return  # v0: human owns the PR
    else:
        log.warning(
            "issue=#%s label=%r not implemented in v0; leaving alone",
            issue.number, label,
        )


def _handle_pickup(gh: GitHubClient, issue: Issue) -> None:
    gh.comment(
        issue,
        ":robot: orchestrator picking this up. v0 skips decomposition and goes "
        "straight to implementation.",
    )
    gh.set_workflow_label(issue, "implementing")
    state = PinnedState()
    state.set("created_at", _now_iso())
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, issue)


def _handle_implementing(gh: GitHubClient, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    if state.get("awaiting_human"):
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        if not new_comments:
            return
        followup = "\n\n".join(
            f"@{c.user.login if c.user else 'user'}: {c.body}"
            for c in new_comments if c.body
        )
        wt = _worktree_path(issue.number)
        if not wt.exists():
            wt = _ensure_worktree(issue.number)
        result = run_codex(
            followup, wt, resume_session_id=state.get("codex_session_id")
        )
        state.set("awaiting_human", False)
    else:
        wt = _ensure_worktree(issue.number)
        if _has_new_commits(wt):
            # Recovered worktree: codex already committed on a previous tick;
            # skip a fresh run and go straight to push to save tokens.
            log.info(
                "issue=#%d skipping codex; worktree already has commits",
                issue.number,
            )
            result = CodexResult(
                session_id=state.get("codex_session_id"),
                last_message="(orchestrator restart: pushing previously committed work)",
                exit_code=0,
                timed_out=False,
                stdout="",
                stderr="",
            )
        else:
            prompt = _build_implement_prompt(issue, _recent_comments_text(issue))
            result = run_codex(prompt, wt)
            if result.session_id:
                state.set("codex_session_id", result.session_id)
        state.set("branch", _branch_name(issue.number))

    state.set("last_agent_action_at", _now_iso())

    if result.timed_out:
        gh.comment(
            issue,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
        )
        # Park the issue on awaiting_human so the next tick doesn't restart
        # codex or push partial commits left in the worktree. The HITL reply
        # acts as the unblock signal, identical to the question path.
        state.set("awaiting_human", True)
        latest = gh.latest_comment_id(issue)
        if latest is not None:
            state.set("last_action_comment_id", latest)
        gh.write_pinned_state(issue, state)
        return

    wt = _worktree_path(issue.number)
    if _has_new_commits(wt):
        dirty = _worktree_dirty_files(wt)
        if dirty:
            _on_dirty_worktree(gh, issue, state, result, dirty)
        else:
            _on_commits(gh, issue, state, result)
    else:
        _on_question(gh, issue, state, result)

    gh.write_pinned_state(issue, state)


def _on_commits(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: CodexResult
) -> None:
    wt = _worktree_path(issue.number)
    branch = _branch_name(issue.number)
    if not _push_branch(wt, branch):
        # Park on awaiting_human like the timeout/question paths. Otherwise the
        # worktree's commits keep _has_new_commits() true, so every poll would
        # re-enter _on_commits() and re-comment indefinitely until a human acts.
        gh.comment(
            issue,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        )
        state.set("awaiting_human", True)
        latest = gh.latest_comment_id(issue)
        if latest is not None:
            state.set("last_action_comment_id", latest)
        # _handle_implementing writes pinned state after we return.
        return
    # Recover gracefully if a previous tick crashed between open_pr and the
    # relabel: reuse the existing open PR instead of 422-ing on duplicate.
    pr = gh.find_open_pr(branch=branch, base=config.BASE_BRANCH)
    if pr is None:
        title = f"#{issue.number}: {issue.title}"
        body_parts = [
            f"Resolves #{issue.number}",
            "",
            f"Generated by orchestrator (codex session `{state.get('codex_session_id', '?')}`).",
        ]
        if result.last_message.strip():
            body_parts += ["", "---", "_Last agent message:_", "", result.last_message[:2000]]
        pr = gh.open_pr(
            branch=branch, base=config.BASE_BRANCH, title=title, body="\n".join(body_parts)
        )
        gh.comment(issue, f":sparkles: PR opened: #{pr.number}")
    else:
        log.info("issue=#%s reusing existing PR #%d for %s", issue.number, pr.number, branch)
    state.set("pr_number", pr.number)
    gh.set_workflow_label(issue, "in_review")


def _on_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: CodexResult
) -> None:
    raw = result.last_message.strip()
    question = raw or "(agent did not produce a final message)"
    quoted = "> " + question.replace("\n", "\n> ")
    gh.comment(
        issue,
        f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
    )
    state.set("awaiting_human", True)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)


def _on_dirty_worktree(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    result: CodexResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when codex left uncommitted changes.

    Pushing here would publish a branch that omits the dirty files, so the PR
    would not match what the agent actually produced. We surface the situation
    to the human and resume the codex session on their reply, identical to the
    question path.
    """
    shown = dirty[:10]
    files_md = "\n".join(f"- `{p}`" for p in shown)
    if len(dirty) > len(shown):
        files_md += f"\n- … ({len(dirty) - len(shown)} more)"
    last_msg = result.last_message.strip()
    tail = ""
    if last_msg:
        quoted = "> " + last_msg.replace("\n", "\n> ")
        tail = f"\n\n_Last agent message:_\n\n{quoted}"
    gh.comment(
        issue,
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}",
    )
    state.set("awaiting_human", True)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
