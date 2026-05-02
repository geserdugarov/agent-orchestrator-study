"""State machine: drive issues through the orchestrator workflow.

v0 implements (no label) -> implementing -> validating -> in_review. Validating
runs a fresh codex session as a reviewer; if it requests changes, the dev
session is resumed with the feedback, the fix is pushed, and the review reruns
until APPROVED or MAX_REVIEW_ROUNDS is hit. Other labels are observed and
logged as not-yet-implemented.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from github.Issue import Issue

from . import config
from .agents import AgentResult, run_agent
from .github import GitHubClient, PinnedState

log = logging.getLogger(__name__)

# Disable git's /dev/tty fallback prompts in any subprocess we spawn.
_GIT_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

# The reviewer prompt asks for the marker alone on its own line, but real
# codex output isn't always that disciplined: prefixes like "Final verdict:"
# or trailing punctuation appear in practice. Match anywhere and take the
# last occurrence, so a stray reference earlier in the text loses to the
# concluding one.
_VERDICT_RE = re.compile(
    r"VERDICT:\s*(APPROVED|CHANGES_REQUESTED)\b",
    re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_dev_session(state: PinnedState) -> Tuple[str, Optional[str]]:
    """Return (dev_agent, dev_session_id) for an issue.

    Prefers the new `dev_agent`/`dev_session_id` keys. Falls back to the
    legacy `codex_session_id` (which is always codex by definition) so
    in-flight issues written before the configurable-backend rollout keep
    using codex even if `DEV_AGENT` flips to claude on the next restart.
    Returns (config.DEV_AGENT, None) when the issue has never been spawned.
    """
    if state.get("dev_agent"):
        sid = state.get("dev_session_id")
        return str(state.get("dev_agent")), str(sid) if sid is not None else None
    legacy = state.get("codex_session_id")
    if legacy is not None:
        return "codex", str(legacy)
    return config.DEV_AGENT, None


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


def _head_sha(worktree: Path) -> str:
    """HEAD commit SHA of the worktree, or '' if it cannot be read.

    Used by the validating handler to detect whether a dev-fix codex run
    produced a new commit. _has_new_commits compares against origin/<base>,
    which is already true throughout validating, so we need an absolute SHA
    snapshot instead.
    """
    r = _git("rev-parse", "HEAD", cwd=worktree)
    if r.returncode != 0:
        return ""
    return (r.stdout or "").strip()


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


def _build_review_prompt(issue: Issue, comments_text: str) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    return (
        f"You are an automated code reviewer for GitHub issue #{issue.number}: {issue.title!r}. "
        "A separate codex session has implemented this issue and committed to the current "
        f"branch. The base branch is `origin/{config.BASE_BRANCH}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        "Inspect the change with:\n"
        f"  git log --oneline origin/{config.BASE_BRANCH}..HEAD\n"
        f"  git diff origin/{config.BASE_BRANCH}...HEAD\n\n"
        "Review the change against the issue requirements. Flag correctness bugs, missing "
        "tests, scope creep, obvious style issues, and anything that would block a human "
        "approver. Do NOT edit or commit anything -- you are a reviewer only.\n\n"
        "Your final message MUST end with exactly one of these markers, alone on its own line:\n"
        "  VERDICT: APPROVED\n"
        "  VERDICT: CHANGES_REQUESTED\n\n"
        "If CHANGES_REQUESTED, list the specific items above the verdict line as a numbered "
        "list so the implementer can address them one by one. If the change is acceptable as "
        "is, write VERDICT: APPROVED with a one-line justification above it."
    )


def _build_fix_prompt(review_feedback: str) -> str:
    feedback = review_feedback.strip() or "(reviewer left no detail)"
    quoted = "> " + feedback.replace("\n", "\n> ")
    return (
        "An automated reviewer requested changes on your implementation. Address each item "
        "below, then COMMIT the fix in your current worktree. Do NOT push -- the orchestrator "
        "pushes and re-runs the review.\n\n"
        f"Review feedback:\n\n{quoted}\n\n"
        "If you genuinely disagree with a point, end your final message with a question for "
        "the human and leave that item un-fixed; the orchestrator will park the issue for "
        "human review. Otherwise, fix all items (a single commit is fine)."
    )


def _parse_review_verdict(last_message: str) -> Tuple[str, str]:
    """Find the last 'VERDICT: APPROVED|CHANGES_REQUESTED' marker.

    Returns (verdict, body_above_marker). verdict is one of "approved",
    "changes_requested", or "unknown" (no marker found). body_above_marker is
    the slice of last_message before the marker, used as PR-comment text for
    the changes-requested case.
    """
    if not last_message:
        return "unknown", ""
    matches = list(_VERDICT_RE.finditer(last_message))
    if not matches:
        return "unknown", last_message
    last = matches[-1]
    word = last.group(1).upper()
    verdict = "approved" if word == "APPROVED" else "changes_requested"
    body = last_message[: last.start()].rstrip()
    return verdict, body


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
    elif label == "validating":
        _handle_validating(gh, issue)
    elif label == "in_review":
        return  # v0: human owns the PR after codex review approves
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


def _park_awaiting_human(
    gh: GitHubClient, issue: Issue, state: PinnedState, message: str
) -> None:
    """Post `message` and mark the issue as awaiting a human reply.

    Caller is responsible for `gh.write_pinned_state` afterwards (mirrors the
    existing _on_question / _on_dirty_worktree contract).
    """
    gh.comment(issue, message)
    state.set("awaiting_human", True)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)


def _check_and_increment_retry_budget(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> bool:
    """Gate fresh implementing-codex spawns by a per-issue 24h retry cap.

    The window starts at the first counted attempt and resets once 24h after
    that start has elapsed -- a fixed window per issue, not a true rolling
    window, but enough to stop a stuck issue from burning tokens for a day.

    Returns True if the spawn is allowed (and the budget was incremented);
    False if the cap is exhausted (and the issue was parked on awaiting_human).

    Only fresh spawns count. Resumes on human reply and recovered-worktree
    pushes are explicit unblock signals or carry-over work, not retries.
    Caller writes pinned state after this returns; on the False branch we have
    already parked, so caller's pinned-state write commits the park.
    """
    cap = config.MAX_RETRIES_PER_DAY
    if cap <= 0:
        return True

    now = datetime.now(timezone.utc)
    window_start_raw = state.get("retry_window_start")
    window_start: Optional[datetime] = None
    if window_start_raw:
        try:
            window_start = datetime.fromisoformat(window_start_raw)
        except (TypeError, ValueError):
            window_start = None

    if window_start is None or now - window_start > timedelta(hours=24):
        # Window absent/corrupt/expired: open a new one.
        state.set("retry_window_start", _now_iso())
        state.set("retry_count", 0)
        window_start_raw = state.get("retry_window_start")

    count = int(state.get("retry_count") or 0)
    if count >= cap:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} hit retry cap ({cap}/day) for "
            f"implementing; manual intervention needed. "
            f"Window opened at {window_start_raw}.",
        )
        return False

    state.set("retry_count", count + 1)
    return True


def _resume_developer_on_human_reply(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> Optional[Tuple[Path, AgentResult]]:
    """Resume the developer's agent session with new human comments.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist.

    Returns (worktree, agent_result) on resume, or None if there are no new
    comments since the last park (caller should return without writing state).
    """
    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    wt = _worktree_path(issue.number)
    if not wt.exists():
        wt = _ensure_worktree(issue.number)
    dev_agent, dev_sid = _read_dev_session(state)
    result = run_agent(dev_agent, followup, wt, resume_session_id=dev_sid)
    state.set("awaiting_human", False)
    return wt, result


def _handle_implementing(gh: GitHubClient, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    if state.get("awaiting_human"):
        resumed = _resume_developer_on_human_reply(gh, issue, state)
        if resumed is None:
            return
        wt, result = resumed
    else:
        wt = _ensure_worktree(issue.number)
        if _has_new_commits(wt):
            # Recovered worktree: the dev agent already committed on a
            # previous tick; skip a fresh run and go straight to push.
            log.info(
                "issue=#%d skipping agent; worktree already has commits",
                issue.number,
            )
            _, dev_sid = _read_dev_session(state)
            result = AgentResult(
                session_id=dev_sid,
                last_message="(orchestrator restart: pushing previously committed work)",
                exit_code=0,
                timed_out=False,
                stdout="",
                stderr="",
            )
        else:
            if not _check_and_increment_retry_budget(gh, issue, state):
                gh.write_pinned_state(issue, state)
                return
            dev_agent, _ = _read_dev_session(state)
            prompt = _build_implement_prompt(issue, _recent_comments_text(issue))
            result = run_agent(dev_agent, prompt, wt)
            if result.session_id:
                state.set("dev_agent", dev_agent)
                state.set("dev_session_id", result.session_id)
        state.set("branch", _branch_name(issue.number))

    state.set("last_agent_action_at", _now_iso())

    if result.timed_out:
        # Park on awaiting_human so the next tick doesn't restart codex or
        # push partial commits left in the worktree. The HITL reply acts as
        # the unblock signal, identical to the question path.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
        )
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


def _handle_dev_fix_result(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    wt: Path,
    result: AgentResult,
    before_sha: str,
) -> bool:
    """Post-agent handling for a dev fix during validating.

    Returns True if a fix was committed, pushed, and the loop should re-review
    on the next tick. Returns False if the run produced no fix (timeout,
    no-new-commit, dirty tree, or push failure); caller should write state and
    return.
    """
    if result.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent timed out after {config.AGENT_TIMEOUT}s, "
            "manual intervention needed.",
        )
        return False

    after_sha = _head_sha(wt)
    if after_sha == before_sha or not after_sha:
        # No new commit: dev asked a question or did nothing.
        _on_question(gh, issue, state, result)
        return False

    dirty = _worktree_dirty_files(wt)
    if dirty:
        _on_dirty_worktree(gh, issue, state, result, dirty)
        return False

    branch = _branch_name(issue.number)
    if not _push_branch(wt, branch):
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        )
        return False

    return True


def _handle_validating(gh: GitHubClient, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # Awaiting-human path: human replied after a park; resume the developer
    # codex with their feedback. Identical mechanic to implementing's resume,
    # but on success we stay in validating and bump the round so the reviewer
    # runs again on the next tick.
    if state.get("awaiting_human"):
        wt = _worktree_path(issue.number)
        if not wt.exists():
            wt = _ensure_worktree(issue.number)
        before_sha = _head_sha(wt)
        resumed = _resume_developer_on_human_reply(gh, issue, state)
        if resumed is None:
            return
        wt, result = resumed
        state.set("last_agent_action_at", _now_iso())
        if not _handle_dev_fix_result(gh, issue, state, wt, result, before_sha):
            gh.write_pinned_state(issue, state)
            return
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        gh.write_pinned_state(issue, state)
        return

    round_n = int(state.get("review_round") or 0)
    if round_n >= config.MAX_REVIEW_ROUNDS:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} review still has comments after "
            f"{round_n} round(s); manual intervention needed.",
        )
        gh.write_pinned_state(issue, state)
        return

    wt = _ensure_worktree(issue.number)
    review_prompt = _build_review_prompt(issue, _recent_comments_text(issue))
    review = run_agent(
        config.REVIEW_AGENT, review_prompt, wt, timeout=config.REVIEW_TIMEOUT
    )
    state.set("review_agent", config.REVIEW_AGENT)
    if review.session_id:
        state.set("last_review_session_id", review.session_id)
    state.set("last_review_at", _now_iso())

    if review.timed_out:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer timed out after "
            f"{config.REVIEW_TIMEOUT}s; manual intervention needed.",
        )
        gh.write_pinned_state(issue, state)
        return

    verdict, body = _parse_review_verdict(review.last_message)

    if verdict == "approved":
        if pr_number is not None:
            try:
                gh.pr_comment(
                    int(pr_number),
                    ":white_check_mark: codex review approved.",
                )
            except Exception:
                log.exception(
                    "issue=#%s could not post approval to PR #%s",
                    issue.number, pr_number,
                )
        gh.set_workflow_label(issue, "in_review")
        gh.write_pinned_state(issue, state)
        return

    if verdict == "unknown":
        raw = (review.last_message or "").strip() or "(reviewer produced no final message)"
        quoted = "> " + raw.replace("\n", "\n> ")
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} reviewer did not emit a VERDICT line; "
            f"manual adjudication needed.\n\n_Last reviewer message:_\n\n{quoted}",
        )
        gh.write_pinned_state(issue, state)
        return

    # CHANGES_REQUESTED -- post the feedback on the PR, then resume the dev.
    feedback = body.strip() or (review.last_message or "").strip()
    if pr_number is not None:
        try:
            gh.pr_comment(
                int(pr_number),
                f":eyes: codex review (round {round_n + 1}/"
                f"{config.MAX_REVIEW_ROUNDS}) requested changes:\n\n{feedback}",
            )
        except Exception:
            log.exception(
                "issue=#%s could not post review to PR #%s",
                issue.number, pr_number,
            )

    fix_prompt = _build_fix_prompt(feedback)
    before_sha = _head_sha(wt)
    dev_agent, dev_sid = _read_dev_session(state)
    dev_result = run_agent(
        dev_agent, fix_prompt, wt, resume_session_id=dev_sid
    )
    state.set("last_agent_action_at", _now_iso())

    if not _handle_dev_fix_result(gh, issue, state, wt, dev_result, before_sha):
        gh.write_pinned_state(issue, state)
        return

    state.set("review_round", round_n + 1)
    gh.write_pinned_state(issue, state)


def _on_commits(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: AgentResult
) -> None:
    wt = _worktree_path(issue.number)
    branch = _branch_name(issue.number)
    if not _push_branch(wt, branch):
        # Park on awaiting_human like the timeout/question paths. Otherwise the
        # worktree's commits keep _has_new_commits() true, so every poll would
        # re-enter _on_commits() and re-comment indefinitely until a human acts.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        )
        # _handle_implementing writes pinned state after we return.
        return
    # Recover gracefully if a previous tick crashed between open_pr and the
    # relabel: reuse the existing open PR instead of 422-ing on duplicate.
    pr = gh.find_open_pr(branch=branch, base=config.BASE_BRANCH)
    if pr is None:
        title = f"#{issue.number}: {issue.title}"
        dev_agent, dev_sid = _read_dev_session(state)
        body_parts = [
            f"Resolves #{issue.number}",
            "",
            f"Generated by orchestrator ({dev_agent} session `{dev_sid or '?'}`).",
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
    # Reset the review counter every time we (re-)open a PR so the validating
    # handler starts fresh on the new branch state.
    state.set("review_round", 0)
    # Issue moved forward; reset the implementing retry budget so any future
    # bounce back into implementing (e.g. validating -> implementing in a
    # later stage) starts with a fresh window.
    state.set("retry_count", 0)
    state.set("retry_window_start", None)
    gh.set_workflow_label(issue, "validating")


def _on_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: AgentResult
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
    result: AgentResult,
    dirty: list[str],
) -> None:
    """Park instead of pushing when the agent left uncommitted changes.

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
