"""State machine: drive issues through the orchestrator workflow.

(no label) -> implementing -> validating -> in_review -> done|rejected.
Validating runs a fresh reviewer session; on changes-requested the dev session
is resumed, the fix pushed, and the review rerun until APPROVED or
MAX_REVIEW_ROUNDS is hit. In_review reacts to PR state (merged/closed) and PR
comments (debounced) and, when AUTO_MERGE is on, merges PRs that the reviewer
approved and that GitHub considers mergeable with green checks. Other labels
are observed and logged as not-yet-implemented.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

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


# Cap on `orchestrator_comment_ids`. The watermark always advances, so older
# ids are no longer in any `comments_after` window -- the cap exists only to
# bound list growth on long-lived issues, not for correctness.
_ORCH_COMMENT_ID_CAP = 500


def _orchestrator_ids(state: PinnedState) -> set[int]:
    """Set of comment ids the orchestrator itself posted on this issue/PR.
    Used to filter the orchestrator's own messages out of "new feedback"
    scans without falling back to author-login matching -- a PAT shared
    with a human reviewer's GitHub account would otherwise have its real
    review comments swallowed as bot noise (and auto-merged over).
    """
    raw = state.get("orchestrator_comment_ids") or []
    return {int(x) for x in raw}


def _track_orchestrator_comment(state: PinnedState, comment_id: int) -> None:
    raw = state.get("orchestrator_comment_ids")
    ids = list(raw) if isinstance(raw, list) else []
    ids.append(int(comment_id))
    if len(ids) > _ORCH_COMMENT_ID_CAP:
        ids = ids[-_ORCH_COMMENT_ID_CAP:]
    state.set("orchestrator_comment_ids", ids)


def _post_issue_comment(
    gh: GitHubClient, issue: Issue, state: PinnedState, body: str,
):
    """Post an issue comment AND record its id in pinned state so future
    `_handle_in_review` ticks recognize it as orchestrator-authored even when
    the PAT login is shared with a human reviewer. Caller is still responsible
    for `gh.write_pinned_state` -- this only mutates the in-memory state.
    """
    c = gh.comment(issue, body)
    cid = getattr(c, "id", None)
    if cid is not None:
        _track_orchestrator_comment(state, int(cid))
    return c


def _post_pr_comment(
    gh: GitHubClient, pr_number: int, state: PinnedState, body: str,
):
    """PR-conversation comment counterpart to `_post_issue_comment`. Both
    surfaces share the IssueComment id namespace, so a single id list covers
    them. Inline review comments and PR review summaries live in different id
    spaces but the orchestrator never posts to those, so they need no entry.
    """
    c = gh.pr_comment(pr_number, body)
    cid = getattr(c, "id", None)
    if cid is not None:
        _track_orchestrator_comment(state, int(cid))
    return c


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
    for issue in gh.list_pollable_issues():
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
        _handle_in_review(gh, issue)
    else:
        log.warning(
            "issue=#%s label=%r not implemented yet; leaving alone",
            issue.number, label,
        )


def _handle_pickup(gh: GitHubClient, issue: Issue) -> None:
    state = PinnedState()
    state.set("created_at", _now_iso())
    pickup = _post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up. Decomposition stage is not yet "
        "wired; going straight to implementation.",
    )
    # Anchor the validating-handoff seed-watermark on the exact pickup
    # comment id. Without this, an issue that started under an older
    # version of the orchestrator (where bot ids were not tracked) would
    # have its first recorded bot id be a much later comment (PR-opened or
    # approval), causing `_seed_watermark_past_self` to silently advance
    # past every issue/PR comment in between -- including any human
    # "do not merge yet" posted during implementing.
    pickup_id = getattr(pickup, "id", None)
    if pickup_id is not None:
        state.set("pickup_comment_id", int(pickup_id))
    gh.set_workflow_label(issue, "implementing")
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, issue)


def _park_awaiting_human(
    gh: GitHubClient, issue: Issue, state: PinnedState, message: str
) -> None:
    """Post `message` and mark the issue as awaiting a human reply.

    Caller is responsible for `gh.write_pinned_state` afterwards (mirrors the
    existing _on_question / _on_dirty_worktree contract). Clears any stale
    `park_reason` -- a transient AUTO_MERGE park (failed_checks/unmergeable)
    followed by a follow-up question/timeout park would otherwise leave
    the transient reason behind and let the in_review recovery branch
    auto-merge over the dev's standing question on the next tick. Callers
    that re-park for a transient reason (the AUTO_MERGE failed-checks /
    unmergeable paths) re-set `park_reason` immediately after this call.
    """
    _post_issue_comment(gh, issue, state, message)
    state.set("awaiting_human", True)
    state.set("park_reason", None)
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


def _resume_dev_with_text(
    gh: GitHubClient, issue: Issue, state: PinnedState, followup_text: str
) -> Tuple[Path, AgentResult]:
    """Resume the dev's locked-backend session with the given prompt text.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist. Clears the
    `awaiting_human` flag because the caller is reacting to a fresh human
    signal (issue or PR comment) by spawning the agent.
    """
    wt = _worktree_path(issue.number)
    if not wt.exists():
        wt = _ensure_worktree(issue.number)
    dev_agent, dev_sid = _read_dev_session(state)
    result = run_agent(dev_agent, followup_text, wt, resume_session_id=dev_sid)
    state.set("awaiting_human", False)
    return wt, result


def _resume_developer_on_human_reply(
    gh: GitHubClient, issue: Issue, state: PinnedState
) -> Optional[Tuple[Path, AgentResult]]:
    """Resume the developer's agent session with new issue-level comments.

    Returns (worktree, agent_result) on resume, or None if there are no new
    comments since the last park (caller should return without writing state).

    Used by `implementing` and `validating` -- both deliberately watch only
    the issue's comment thread, not the PR's. The `in_review` handler watches
    PR comments too via `_resume_dev_with_text` directly.

    Bumps `last_action_comment_id` to the highest consumed comment id BEFORE
    spawning the agent. Without this, a successful resume during implementing
    or validating leaves `last_action_comment_id` at the prior park id, so
    the validating->in_review handoff treats the just-consumed human reply
    as fresh PR feedback and re-resumes the dev on input it has already
    handled. This pre-resume bump is also robust to mid-resume failures:
    if the agent crashes or times out, those comments are still recorded
    as consumed (the dev DID see them via the resume prompt), and the
    failure is surfaced via the timeout/dirty/question paths instead.
    """
    last_action_id = state.get("last_action_comment_id")
    new_comments = gh.comments_after(issue, last_action_id)
    if not new_comments:
        return None
    consumed_max = max(c.id for c in new_comments)
    state.set("last_action_comment_id", consumed_max)
    followup = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body}"
        for c in new_comments if c.body
    )
    return _resume_dev_with_text(gh, issue, state, followup)


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
    # The reviewer reads the local worktree's HEAD; remember which commit
    # that is so the in_review handoff can persist the SHA the agent
    # actually inspected. Setting `agent_approved_sha = pr.head.sha`
    # instead would mark the REMOTE head at handoff time as agent-approved,
    # which lets AUTO_MERGE land an unreviewed commit if the branch was
    # force-pushed or otherwise updated between the review and the handoff.
    reviewed_sha = _head_sha(wt)
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
                _post_pr_comment(
                    gh, int(pr_number), state,
                    ":white_check_mark: codex review approved.",
                )
            except Exception:
                log.exception(
                    "issue=#%s could not post approval to PR #%s",
                    issue.number, pr_number,
                )
        if pr_number is not None:
            # Snapshot what the reviewer agent approved and seed the
            # in_review comment watermark. Without these, `_handle_in_review`
            # would (a) refuse to auto-merge -- the agent posts an issue
            # comment, not a real PR review, so pr_is_approved alone is
            # always False for the agent flow -- and (b) replay the
            # orchestrator's own automated comments ("picking this up",
            # "PR opened", the approval just posted) as fresh PR feedback
            # once the debounce expires.
            try:
                pr = gh.get_pr(int(pr_number))
            except Exception as e:
                # Recoverable: AUTO_MERGE will simply not fire for this
                # issue, and the in_review handler will fall back to its
                # legacy `last_action_comment_id` watermark. Surface the
                # failure but skip the traceback -- it adds no signal.
                log.warning(
                    "issue=#%s could not snapshot PR #%s for in_review "
                    "handoff: %s", issue.number, pr_number, e,
                )
            else:
                # Persist the local SHA the reviewer ran against, not the
                # current remote head. The auto-merge gate's existing
                # `agent_approved_sha == head_sha` check then naturally
                # rejects the branch-update race: if pr.head.sha has moved
                # past `reviewed_sha`, agent_approved_sha won't match the
                # new head and AUTO_MERGE waits for a fresh review round.
                if reviewed_sha:
                    state.set("agent_approved_sha", reviewed_sha)
                issue_wm, review_wm = _latest_pr_comment_ids(
                    gh, issue, pr, state
                )
                # Ratchet: a previous in_review tick may have already
                # advanced these watermarks past PR feedback the dev has
                # since fixed. _seed_watermark_past_self stops at the first
                # post-pickup human comment, so without max() that consumed
                # comment would replay as "new" on the next in_review tick.
                prev_issue_wm = state.get("pr_last_comment_id")
                if isinstance(prev_issue_wm, int):
                    issue_wm = (
                        prev_issue_wm if issue_wm is None
                        else max(issue_wm, prev_issue_wm)
                    )
                # Default to 0 ("scan all from the beginning") when the
                # seed-past-self logic returned None and no prior watermark
                # exists. That happens for legacy state without a recorded
                # pickup id; setting 0 stops the in_review legacy migration
                # from then advancing past historical content (including
                # human feedback posted during implementing/validating)
                # while still letting `orchestrator_comment_ids` filter
                # recorded bot comments out of the next tick's scan.
                if issue_wm is None:
                    issue_wm = 0
                state.set("pr_last_comment_id", issue_wm)
                # Inline review comments and review summaries live in
                # namespaces the orchestrator never posts on, so the
                # seed-past-self logic always returns None for those
                # surfaces. Default each to 0 ("scan all from beginning")
                # so the in_review legacy migration sees them as already
                # seeded and does NOT advance past human feedback the
                # human submitted on those surfaces during validate. Ratchet
                # past anything a prior in_review tick already consumed.
                prev_review_wm = state.get("pr_last_review_comment_id")
                if isinstance(prev_review_wm, int):
                    review_wm = (
                        prev_review_wm if review_wm is None
                        else max(review_wm, prev_review_wm)
                    )
                if review_wm is None:
                    review_wm = 0
                state.set("pr_last_review_comment_id", review_wm)
                prev_summary_wm = state.get("pr_last_review_summary_id")
                summary_wm = (
                    prev_summary_wm
                    if isinstance(prev_summary_wm, int)
                    else 0
                )
                state.set("pr_last_review_summary_id", summary_wm)
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
            _post_pr_comment(
                gh, int(pr_number), state,
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


def _build_pr_comment_followup(comments: list) -> str:
    """Compose a dev-fix prompt from new PR-side comments.

    The dev session has not seen any PR comment before (those live on a
    different surface than the issue thread it was fed at spawn time), so a
    short preamble is needed to frame the request -- otherwise a comment like
    "rename foo to bar" reads as freeform chatter without context.
    """
    body = "\n\n".join(
        f"@{c.user.login if c.user else 'user'}: {c.body or ''}"
        for c in comments
    )
    quoted = "> " + body.replace("\n", "\n> ")
    return (
        "New comments arrived on the open PR for this issue. Address each item, "
        "then COMMIT the fix in your current worktree. Do NOT push -- the "
        "orchestrator pushes and re-runs the reviewer.\n\n"
        f"PR comments:\n\n{quoted}\n\n"
        "If you genuinely disagree with a point, end your final message with a "
        "question for the human and leave that item un-fixed; the orchestrator "
        "will park the issue for human review."
    )


def _seed_watermark_past_self(
    issue_thread_comments: list,
    pr_conversation_comments: list,
    orchestrator_ids: set[int],
    pickup_comment_id: Optional[int],
    consumed_through: Optional[int] = None,
) -> Optional[int]:
    """Seed the in_review handoff watermark.

    Walk comments oldest-to-newest across both surfaces (issue thread and
    PR conversation share the IssueComment id space, so a single watermark
    covers both). The pickup comment is the boundary: everything before
    `pickup_comment_id` is pre-pickup chatter the dev agent already saw at
    spawn, so it can be advanced past. From the pickup forward, advance
    through the contiguous run of orchestrator-authored comments AND
    through any ISSUE-THREAD comment with id <= `consumed_through` (already
    fed to the dev agent via a prior `_resume_developer_on_human_reply`
    call during implementing/validating), stopping at the first
    not-yet-consumed non-orchestrator comment. This preserves human
    feedback posted during validating that the dev has not yet seen while
    NOT replaying feedback the dev has already consumed.

    `consumed_through` is intentionally NOT applied to PR-conversation
    comments. `last_action_comment_id` only records issue-thread ids fed
    via `_resume_developer_on_human_reply` (validating/implementing watch
    the issue thread only); a PR-conversation comment whose id happens to
    be <= a later-consumed issue-thread reply has NOT been seen by the dev
    and must surface on the next in_review tick. Folding both surfaces
    under one `c.id <= consumed_through` check would let AUTO_MERGE land
    the PR over unread PR-conversation feedback.

    Identification of orchestrator-authored content is by exact comment id
    (recorded when the orchestrator posted the comment) rather than author
    login. The login-based check would also drop comments authored by a
    human reviewer who shares the PAT's GitHub account -- a common
    deployment shape -- causing real review feedback to be auto-merged over.

    Returns None when the pickup id is unknown (legacy state from a deploy
    that pre-dates pickup-id tracking, or a manually-relabeled issue) or
    when the surface has no orchestrator-authored content. The caller then
    defaults the watermark to 0 so the in_review legacy migration cannot
    advance past historical content; the orchestrator_comment_ids id-set
    filter in `_handle_in_review` drops recorded bot comments at scan time.
    """
    if pickup_comment_id is None:
        # Legacy state without a pickup anchor: refuse to advance. We
        # cannot tell pre-pickup chatter (safe to skip) from human feedback
        # posted during implementing/validating (must preserve), and
        # dropping a human comment is the unsafe direction.
        return None
    # Tag each comment with its surface so the walk below can apply
    # `consumed_through` to the issue thread only.
    sorted_pairs: list[Tuple[Any, bool]] = sorted(
        [(c, True) for c in issue_thread_comments]
        + [(c, False) for c in pr_conversation_comments],
        key=lambda p: p[0].id,
    )
    if not any(c.id in orchestrator_ids for c, _ in sorted_pairs):
        return None
    watermark: Optional[int] = None
    seen_self = False
    for c, is_issue_thread in sorted_pairs:
        is_self = c.id in orchestrator_ids
        already_consumed = (
            is_issue_thread
            and consumed_through is not None
            and c.id <= consumed_through
        )
        if is_self:
            watermark = c.id
            seen_self = True
        elif not seen_self and c.id < pickup_comment_id:
            # Pre-pickup chatter -- already in the dev agent's spawn context.
            watermark = c.id
        elif already_consumed:
            # Fed to the dev via a prior implementing/validating resume.
            # Replaying it as fresh PR feedback would re-spawn the dev on
            # input it has already handled.
            watermark = c.id
        else:
            # Post-pickup human comment that has NOT been consumed yet.
            # Stop and preserve for the next in_review tick.
            break
    return watermark


def _latest_pr_comment_ids(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> Tuple[Optional[int], Optional[int]]:
    """Return (issue-comment watermark, review-comment watermark) seeded only
    past leading orchestrator-authored comments on the issue thread + PR.

    The second value is always None: the orchestrator never posts inline PR
    review comments, so there is no leading self-run to advance past on
    that surface, and `orchestrator_comment_ids` records IDs in the
    IssueComment namespace only -- feeding it to `_seed_watermark_past_self`
    against the PullRequestComment namespace would falsely treat a human
    inline comment whose numeric id collides with a recorded bot id as
    self-authored, advancing the watermark past the human's feedback. The
    `_handle_validating` caller defaults the inline-review watermark to 0
    when this returns None so the in_review legacy migration cannot then
    advance past human inline feedback either.
    """
    orchestrator_ids = _orchestrator_ids(state)
    pickup_id_raw = state.get("pickup_comment_id")
    pickup_id = pickup_id_raw if isinstance(pickup_id_raw, int) else None
    # `last_action_comment_id` doubles as a "consumed through" marker:
    # both park comments and post-resume bumps land here, so any issue
    # comment with id <= this value has either been posted by the
    # orchestrator (filtered by `orchestrator_comment_ids`) or already
    # been fed to the dev session (must not replay).
    consumed_raw = state.get("last_action_comment_id")
    consumed_through = (
        consumed_raw if isinstance(consumed_raw, int) else None
    )
    # Keep the surfaces separate -- `consumed_through` only applies to the
    # issue thread (the surface `_resume_developer_on_human_reply` watches
    # during implementing/validating). Folding both into one list and
    # applying `c.id <= consumed_through` uniformly would silently advance
    # the watermark past unread PR-conversation feedback whose id happens
    # to be lower than a later-consumed issue-thread reply, letting
    # AUTO_MERGE land the PR over the human's PR comment.
    issue_thread = list(gh.comments_after(issue, None))
    pr_conversation = list(gh.pr_conversation_comments_after(pr, None))
    return (
        _seed_watermark_past_self(
            issue_thread, pr_conversation,
            orchestrator_ids, pickup_id,
            consumed_through=consumed_through,
        ),
        None,
    )


def _bump_in_review_watermarks(
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    issue_space_new: Optional[list] = None,
    review_space_new: Optional[list] = None,
    review_summary_new: Optional[list] = None,
) -> None:
    """Push the in_review watermarks past anything we've seen so far AND past
    any park comment we just wrote on the issue thread.

    Without this, a park-and-write at in_review (failed checks, unmergeable,
    failed dev fix) leaves `pr_last_comment_id` lagging behind the orchestrator
    park message it just posted; the next tick scans the issue thread from the
    older watermark and resumes the dev agent on the orchestrator's own HITL
    ping. The ratchet is one-way (only ever increases) so callers can pass
    just-consumed comments or omit them and let `latest_comment_id` carry it.
    """
    candidates: list[int] = []
    cur_issue_wm = state.get("pr_last_comment_id")
    if isinstance(cur_issue_wm, int):
        candidates.append(cur_issue_wm)
    last_action = state.get("last_action_comment_id")
    if isinstance(last_action, int):
        candidates.append(last_action)
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        candidates.append(latest)
    if issue_space_new:
        candidates.extend(c.id for c in issue_space_new)
    if candidates:
        state.set("pr_last_comment_id", max(candidates))

    review_candidates: list[int] = []
    cur_review_wm = state.get("pr_last_review_comment_id")
    if isinstance(cur_review_wm, int):
        review_candidates.append(cur_review_wm)
    if review_space_new:
        review_candidates.extend(c.id for c in review_space_new)
    if review_candidates:
        state.set("pr_last_review_comment_id", max(review_candidates))

    summary_candidates: list[int] = []
    cur_summary_wm = state.get("pr_last_review_summary_id")
    if isinstance(cur_summary_wm, int):
        summary_candidates.append(cur_summary_wm)
    if review_summary_new:
        summary_candidates.extend(r.id for r in review_summary_new)
    if summary_candidates:
        state.set("pr_last_review_summary_id", max(summary_candidates))


def _comment_created_at(comment) -> Optional[datetime]:
    """Return a tz-aware UTC datetime for a comment, or None if unavailable.

    Real PyGithub `IssueComment.created_at` is always set, but the fakes used
    in tests can leave it None when the test doesn't care about debounce.
    PullRequestReview surfaces its timestamp as `submitted_at` rather than
    `created_at`, so the in_review debounce reads either. Naive datetimes are
    interpreted as UTC (PyGithub returns naive UTC).
    """
    ca = getattr(comment, "created_at", None)
    if ca is None:
        ca = getattr(comment, "submitted_at", None)
    if ca is None:
        return None
    if ca.tzinfo is None:
        return ca.replace(tzinfo=timezone.utc)
    return ca


# Park reasons that auto-resolve when the underlying GitHub state changes
# (CI rerun goes green, rebase resolves a conflict, branch protection drops
# a stale required review). Other parks (`missing_pr_number`, dev-fix
# failures) need explicit human action to unstick.
_TRANSIENT_PARK_REASONS = frozenset({"failed_checks", "unmergeable"})


def _auto_merge_gates_pass(
    gh: GitHubClient, pr, state: PinnedState
) -> bool:
    """All conditions required for auto-merge, evaluated quietly (no parking,
    no PR comments, no state writes).

    Used by the transient-park recovery path: when an awaiting_human issue
    re-enters `_handle_in_review` with no new comments, we want to detect a
    silently-resolved condition (CI now green, rebase made the PR mergeable)
    and unstick the merge without re-posting the park message every tick.
    Mirrors the inline gate sequence in `_handle_in_review` exactly so the
    two cannot drift.
    """
    head_sha = pr.head.sha
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return False
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return False
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None or not mergeable:
        return False
    return gh.pr_combined_check_state(pr) == "success"


def _seed_legacy_in_review_watermarks(
    gh: GitHubClient, issue: Issue, pr, state: PinnedState
) -> None:
    """First-tick migration: seed any missing in_review watermark past every
    comment currently visible on its surface, and record the seed in pinned
    state immediately.

    Issues that reached `in_review` before the validating handoff started
    seeding watermarks (or that were manually relabeled, or whose handoff
    failed to snapshot the PR) sit on `_handle_in_review` with
    `pr_last_comment_id`/`pr_last_review_comment_id`/`pr_last_review_summary_id`
    all unset. Without this seed, the next tick would call
    `comments_after(..., None)` on each surface and treat every historical
    comment -- including the orchestrator's own pickup / PR-opened / approval
    messages -- as fresh PR feedback once the debounce expires, resuming the
    dev and bouncing the PR back to validating even with `AUTO_MERGE` off.

    Tests that want to drive `_handle_in_review` against pre-existing comments
    seed the relevant watermark explicitly so this helper is a no-op for them.
    """
    # Each missing watermark is persisted on this tick -- 0 if the surface
    # currently has no content, otherwise the latest visible id. Persisting
    # 0 in the empty case is what stops the migration from re-firing on the
    # next tick: if we left the watermark unset, the FIRST human inline /
    # summary review added afterward would be consumed by a re-run of this
    # seed before `_handle_in_review` builds `new_comments`, so AUTO_MERGE
    # could land the PR over that first review.
    seeded = False
    if (
        state.get("pr_last_comment_id") is None
        and state.get("last_action_comment_id") is None
    ):
        candidates: list[int] = []
        issue_latest = gh.latest_comment_id(issue)
        if isinstance(issue_latest, int):
            candidates.append(issue_latest)
        pr_conv = list(gh.pr_conversation_comments_after(pr, None))
        if pr_conv:
            candidates.append(max(c.id for c in pr_conv))
        state.set("pr_last_comment_id", max(candidates) if candidates else 0)
        seeded = True

    if state.get("pr_last_review_comment_id") is None:
        inline = list(gh.pr_inline_comments_after(pr, None))
        state.set(
            "pr_last_review_comment_id",
            max(c.id for c in inline) if inline else 0,
        )
        seeded = True

    if state.get("pr_last_review_summary_id") is None:
        summaries = list(gh.pr_reviews_after(pr, None))
        state.set(
            "pr_last_review_summary_id",
            max(r.id for r in summaries) if summaries else 0,
        )
        seeded = True

    if seeded:
        gh.write_pinned_state(issue, state)


def _handle_in_review(gh: GitHubClient, issue: Issue) -> None:
    """Drive an in_review issue toward done / rejected, or back to validating
    on a new PR comment.

    The handler always re-checks PR state (merged/closed) first so an external
    human merge wins over any orchestrator-side logic. A PR comment newer than
    the debounce window resumes the dev's locked-backend session and bounces
    the issue back to `validating` so the reviewer agent re-runs on the fix.
    Auto-merge is gated by `AUTO_MERGE` (default off); without it, the loop
    only handles state transitions and comment-driven re-fixes -- humans still
    click Merge.
    """
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    if pr_number is None:
        # Manual relabel from outside the validating path. We don't try to
        # infer the PR -- park once and let the human relabel back.
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `in_review` without a pinned `pr_number`; "
            "manual relabeling suspected. Set the workflow label back to "
            "`validating` (or `implementing`) after fixing.",
        )
        gh.write_pinned_state(issue, state)
        return

    pr = gh.get_pr(int(pr_number))
    pr_status = gh.pr_state(pr)

    if pr_status == "merged":
        state.set("merged_at", _now_iso())
        gh.set_workflow_label(issue, "done")
        gh.write_pinned_state(issue, state)
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after merge", issue.number,
            )
        return

    if pr_status == "closed":  # closed without merge
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        try:
            issue.edit(state="closed")
        except Exception:
            log.exception(
                "issue=#%s could not close after reject", issue.number,
            )
        return

    # PR is open BUT the issue was closed manually (the closed-in_review sweep
    # in `list_pollable_issues` yielded it). Closing the issue while its PR
    # is still open is a human stop signal -- without this branch, AUTO_MERGE
    # could otherwise land the PR and flip the issue to `done` over the
    # human's rejection. The closed-with-merged-PR path (Resolves #N
    # auto-close) is already handled by the `pr_status == "merged"` branch
    # above, so by the time we reach here a closed issue means the human
    # closed it directly.
    if getattr(issue, "state", "open") == "closed":
        state.set("closed_without_merge_at", _now_iso())
        gh.set_workflow_label(issue, "rejected")
        gh.write_pinned_state(issue, state)
        return

    # PR is open. Look for new human activity. Three watermarks because the
    # three comment surfaces live in distinct id namespaces in GitHub's REST
    # API: issue/PR-conversation comments share the IssueComment id space,
    # inline review comments live in the PullRequestComment id space, and
    # PR review summaries (the body posted alongside an APPROVE / REQUEST
    # CHANGES / COMMENT submission) live in the PullRequestReview id space.
    # Mixing any two under one int would silently drop or replay one side.
    # Orchestrator-authored items are filtered by exact id (recorded when
    # we posted them); we cannot key this on author login because a PAT
    # shared with a human reviewer's GitHub account is a normal deployment
    # shape, and login-matching would silently drop that human's feedback.
    # The id-set filter is restricted to the IssueComment namespace -- the
    # only surface the orchestrator posts on -- so a human inline review
    # comment or PR review summary that happens to share a numeric id with
    # a recorded bot comment is not falsely dropped.
    _seed_legacy_in_review_watermarks(gh, issue, pr, state)
    # `or` would discard a legacy default of `pr_last_comment_id == 0` and
    # fall back to `last_action_comment_id` (the id of a prior park
    # comment), which sits ABOVE any human "do not merge yet" comment
    # posted earlier during implementing/validating; that human comment
    # would then never surface and AUTO_MERGE could land the PR over it.
    # Treat 0 as a valid "scan from the beginning" watermark.
    issue_wm = state.get("pr_last_comment_id")
    if issue_wm is None:
        issue_wm = state.get("last_action_comment_id")
    review_wm = state.get("pr_last_review_comment_id")
    review_summary_wm = state.get("pr_last_review_summary_id")
    orchestrator_ids = _orchestrator_ids(state)
    new_issue_side = [
        c for c in gh.comments_after(issue, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_conv = [
        c for c in gh.pr_conversation_comments_after(pr, issue_wm)
        if c.id not in orchestrator_ids
    ]
    new_pr_inline = list(gh.pr_inline_comments_after(pr, review_wm))
    new_pr_reviews = list(gh.pr_reviews_after(pr, review_summary_wm))
    issue_space_new = sorted(
        list(new_issue_side) + list(new_pr_conv), key=lambda c: c.id
    )
    review_space_new = sorted(new_pr_inline, key=lambda c: c.id)
    review_summary_new = sorted(new_pr_reviews, key=lambda r: r.id)
    new_comments = issue_space_new + review_space_new + review_summary_new

    # If a previous tick already parked on an unrecoverable state and
    # nothing changed since, do nothing -- the human action that unsticks
    # us is a comment, a relabel, or closing/merging the PR. The first two
    # land in `new_comments`; the last two are caught by the `pr_status`
    # branches above.
    #
    # Exception: when the park reason is transient (failed checks or PR not
    # yet mergeable) and `AUTO_MERGE` is on, re-evaluate the gates here. A
    # human who reruns CI green or rebases the branch without leaving a
    # comment would otherwise leave the issue stuck in_review forever.
    if state.get("awaiting_human") and not new_comments:
        if not (
            config.AUTO_MERGE
            and state.get("park_reason") in _TRANSIENT_PARK_REASONS
        ):
            return
        if not _auto_merge_gates_pass(gh, pr, state):
            return  # still stuck, do not re-post the park comment
        # Conditions resolved: clear the park flags and fall through to the
        # auto-merge block, which re-checks the same gates and merges.
        state.set("awaiting_human", False)
        state.set("park_reason", None)

    if new_comments:
        timestamps = [
            ts for ts in (_comment_created_at(c) for c in new_comments)
            if ts is not None
        ]
        if timestamps:
            newest_ts = max(timestamps)
            elapsed = (datetime.now(timezone.utc) - newest_ts).total_seconds()
            if elapsed < config.IN_REVIEW_DEBOUNCE_SECONDS:
                return  # human may still be typing; wait a tick

        followup = _build_pr_comment_followup(new_comments)
        wt = _worktree_path(issue.number)
        if not wt.exists():
            wt = _ensure_worktree(issue.number)
        before_sha = _head_sha(wt)
        wt, dev_result = _resume_dev_with_text(gh, issue, state, followup)
        state.set("last_agent_action_at", _now_iso())
        if not _handle_dev_fix_result(
            gh, issue, state, wt, dev_result, before_sha
        ):
            # Park has updated last_action_comment_id; bump the in_review
            # watermarks past anything we just consumed so the next tick does
            # not replay these comments OR the orchestrator's own park
            # message as fresh PR feedback.
            _bump_in_review_watermarks(
                gh, issue, state,
                issue_space_new=issue_space_new,
                review_space_new=review_space_new,
                review_summary_new=review_summary_new,
            )
            gh.write_pinned_state(issue, state)
            return
        # Successful fix pushed -- bounce back to validating so the reviewer
        # re-runs on the next tick. Reset round counter; this is a new diff.
        if issue_space_new:
            state.set(
                "pr_last_comment_id", max(c.id for c in issue_space_new)
            )
        if review_space_new:
            state.set(
                "pr_last_review_comment_id",
                max(c.id for c in review_space_new),
            )
        if review_summary_new:
            state.set(
                "pr_last_review_summary_id",
                max(r.id for r in review_summary_new),
            )
        state.set("review_round", 0)
        gh.set_workflow_label(issue, "validating")
        gh.write_pinned_state(issue, state)
        return

    # No new comments -- consider auto-merging.
    if not config.AUTO_MERGE:
        return
    head_sha = pr.head.sha
    # A human CHANGES_REQUESTED on the current head vetoes auto-merge
    # regardless of how the reviewer agent voted. Without this check, the
    # `agent_approved_sha == head_sha` short-circuit below would let the
    # orchestrator merge over a standing human objection on the same SHA.
    if gh.pr_has_changes_requested(pr, head_sha=head_sha):
        return
    # Approval can come from either side: the reviewer agent persists
    # `agent_approved_sha` for the head it OK'd (the agent posts an issue
    # comment, not a real PR review, so pr_is_approved alone would never be
    # True for the agent flow), OR a human/bot submitted a real APPROVED
    # review on the *current* head SHA. Stale human approvals on older
    # commits do NOT count -- a commit pushed after a human approval must
    # not auto-merge unless the human re-approves.
    approved_for_head = (
        state.get("agent_approved_sha") == head_sha
        or gh.pr_is_approved(pr, head_sha=head_sha)
    )
    if not approved_for_head:
        return
    mergeable = gh.pr_is_mergeable(pr)
    if mergeable is None:
        return  # GitHub still computing; try next tick
    if pr.head.sha != head_sha:
        # `pr_is_mergeable` calls `pr.update()` to resolve a `None`
        # mergeable, which refreshes `pr.head.sha`. The approval and
        # changes-requested gates above ran against the earlier head_sha,
        # so a commit landing during the refresh would otherwise let the
        # subsequent failed-checks branch park on the WRONG sha or, worse,
        # let an unreviewed head reach the merge call. Bail and re-evaluate
        # all gates against the new head on the next tick.
        return
    if not mergeable:
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} PR #{pr_number} is not mergeable "
            "(branch protection, conflicts, or out-of-date base); manual "
            "merge needed.",
        )
        state.set("park_reason", "unmergeable")
        _bump_in_review_watermarks(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return
    check = gh.pr_combined_check_state(pr)
    if check == "pending":
        return
    if check in ("failure", "none"):
        # 'none' means no checks at all -- ambiguous, refuse to merge.
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} PR #{pr_number} checks are {check!r}; "
            "refusing to auto-merge.",
        )
        state.set("park_reason", "failed_checks")
        _bump_in_review_watermarks(gh, issue, state)
        gh.write_pinned_state(issue, state)
        return

    # Approved + mergeable + green: SHA-pinned merge to the head we GATED
    # on, NOT the (possibly-refreshed) `pr.head.sha`. `pr_is_mergeable`
    # may have refreshed `pr.head.sha` above; using that value here would
    # let a commit landing during the refresh slip through past the
    # approval and changes-requested gates. The SHA-shift check above
    # already bails when this happens, but pinning to `head_sha` here is
    # belt-and-suspenders: GitHub returns 409 for a SHA mismatch so a
    # missed shift cannot merge an unreviewed head.
    if not gh.merge_pr(pr, sha=head_sha):
        # 405/409/422 -- next tick will re-evaluate; if it still won't merge,
        # the GH UI shows why.
        return
    state.set("merged_at", _now_iso())
    gh.set_workflow_label(issue, "done")
    gh.write_pinned_state(issue, state)
    try:
        issue.edit(state="closed")
    except Exception:
        log.exception(
            "issue=#%s could not close after auto-merge", issue.number,
        )


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
        _post_issue_comment(gh, issue, state, f":sparkles: PR opened: #{pr.number}")
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
    _post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
    )
    state.set("awaiting_human", True)
    # Question parks are not transient: they need a human reply before the
    # auto-merge gates should run again. Clear any stale `park_reason`
    # left behind by a prior AUTO_MERGE failed_checks/unmergeable park.
    state.set("park_reason", None)
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
    _post_issue_comment(
        gh, issue, state,
        f"{config.HITL_MENTIONS} agent committed but left {len(dirty)} "
        f"uncommitted change(s); refusing to push an incomplete branch. "
        f"Reply with guidance and the orchestrator will resume the session.\n\n"
        f"{files_md}{tail}",
    )
    state.set("awaiting_human", True)
    # Mirror `_on_question`: not transient, clear any stale `park_reason`
    # so a prior AUTO_MERGE transient park does not auto-recover over the
    # standing dirty-worktree question.
    state.set("park_reason", None)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
