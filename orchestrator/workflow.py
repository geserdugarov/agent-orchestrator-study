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

import json
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
from .config import RepoSpec
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

# Conventional Commits subject: `<type>[(scope)][!]: <subject>`. The type
# allowlist matches the ones the implement/fix prompts teach plus the broader
# Conventional Commits set, so an agent that picks `perf:` or `ci:` still
# gets credited as conventional.
_CONVENTIONAL_RE = re.compile(
    r"^(?:feat|fix|chore|docs|refactor|test|perf|build|ci|style|revert)"
    r"(?:\([^)]+\))?!?:\s+\S",
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


# Allowed characters in a worktree directory segment: alphanumerics plus
# `_`, `.`, `-`. `/` is excluded so the slug stays a single path segment;
# anything else is replaced with `_`. A leading `.` is also escaped so the
# per-repo subdir is never a hidden directory.
_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_slug(slug: str) -> str:
    """Turn an `owner/name` repo slug into a single filesystem-safe segment.

    `/` collapses to `__` so two repos with the same issue number cannot
    share a worktree path. Any other character outside `[A-Za-z0-9_.-]`
    becomes `_`. A leading `.` is escaped to `_.` so the per-repo subdir
    is never a dotfile-hidden directory. An empty/all-stripped input
    falls back to `_` rather than collapsing into the bare WORKTREES_DIR.
    """
    cleaned = _SLUG_SAFE_RE.sub("_", (slug or "").replace("/", "__"))
    if not cleaned:
        return "_"
    if cleaned.startswith("."):
        cleaned = "_" + cleaned
    return cleaned


def _repo_worktrees_root(spec: RepoSpec) -> Path:
    """Per-repo subdirectory under WORKTREES_DIR for this spec.

    Two specs with the same issue number must not collide on disk, so the
    issue-N / decompose-N segments live inside a sanitized-slug parent
    instead of directly under WORKTREES_DIR.
    """
    return config.WORKTREES_DIR / _sanitize_slug(spec.slug)


def _worktree_path(spec: RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"issue-{issue_number}"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_NO_PROMPT_ENV},
    )


def _ensure_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Return a worktree on a per-issue branch, reusing one with unpushed work.

    The reuse is what lets the orchestrator survive a crash between codex
    committing and the orchestrator pushing -- without it, the next tick would
    wipe the worktree and we'd burn another codex run on the same prompt.
    """
    _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
    wt = _worktree_path(spec, issue_number)
    branch = _branch_name(issue_number)

    if wt.exists():
        if _has_new_commits(spec, wt):
            log.info("issue=#%d worktree has unpushed commits; reusing", issue_number)
            return wt
        _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)

    _git("fetch", "--quiet", "origin", spec.base_branch, cwd=spec.target_root)

    have_branch = _git(
        "rev-parse", "--verify", branch, cwd=spec.target_root
    ).returncode == 0
    if have_branch:
        result = _git("worktree", "add", str(wt), branch, cwd=spec.target_root)
    else:
        result = _git(
            "worktree", "add", "-b", branch, str(wt),
            f"origin/{spec.base_branch}",
            cwd=spec.target_root,
        )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return wt


def _has_new_commits(spec: RepoSpec, worktree: Path) -> bool:
    r = _git(
        "rev-list", "--count", f"origin/{spec.base_branch}..HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return False
    return int((r.stdout or "0").strip() or "0") > 0


def _first_commit_subject(worktree: Path) -> str:
    """Subject line of the oldest commit in `origin/<base>..HEAD`, or ''.

    Used by `_on_commits` to derive a Conventional-Commits PR title from what
    the agent actually wrote, so the PR title matches the commit history
    when the agent followed the convention.
    """
    r = _git(
        "log", "--reverse", "--format=%s",
        f"origin/{config.BASE_BRANCH}..HEAD",
        cwd=worktree,
    )
    if r.returncode != 0:
        return ""
    lines = (r.stdout or "").splitlines()
    return lines[0].strip() if lines else ""


def _is_conventional_subject(subject: str) -> bool:
    return bool(_CONVENTIONAL_RE.match(subject or ""))


def _pr_title_from_commit_or_issue(issue: Issue, first_subject: str) -> str:
    """Pick a Conventional-Commits PR title.

    Prefer the agent's first commit subject when it already follows the
    convention (so the PR title matches the commit history). Otherwise
    fall back to `<type>: <issue title>`, choosing `fix` for bug-labelled
    issues and `feat` everywhere else. Traceability is preserved by the
    `Resolves #<n>` line in the PR body, so the title stays clean.
    """
    subject = (first_subject or "").strip()
    if _is_conventional_subject(subject):
        return subject
    issue_title = (issue.title or "").strip()
    if _is_conventional_subject(issue_title):
        return issue_title
    label_names = {(getattr(l, "name", "") or "").lower() for l in (issue.labels or [])}
    if {"bug", "fix"} & label_names:
        ctype = "fix"
    else:
        ctype = "feat"
    body = issue_title or f"address issue #{issue.number}"
    return f"{ctype}: {body}"


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
    branch. The orchestrator's own scratch (codex's `-o` file) lives outside
    the worktree (a per-spawn tempfile in `_run_codex`), so it never surfaces
    here regardless of the target repo's .gitignore.
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


# The decomposer needs a working directory to run `git ls-files` / `wc -l`
# against, but it must never touch the implementer's per-issue branch. If
# it shared `_worktree_path(issue)`, the local `orchestrator/issue-<n>`
# branch would get anchored at whatever `origin/<base>` snapshot the
# decomposer saw -- and a `split` decision parks the parent on `blocked`
# for the duration of its children's lifecycle. By the time the parent
# flips to `ready` and the implementer takes over, `origin/<base>` has
# advanced (children's PRs merged), but `_ensure_worktree` would re-add
# the worktree pointing at that stale branch and the implementer would
# commit on the old base. A separate detached-HEAD checkout sidesteps the
# problem entirely: the implementer's `_ensure_worktree` always sees a
# fresh per-issue branch created from the current `origin/<base>`.
def _decompose_worktree_path(spec: RepoSpec, issue_number: int) -> Path:
    return _repo_worktrees_root(spec) / f"decompose-{issue_number}"


def _ensure_decompose_worktree(spec: RepoSpec, issue_number: int) -> Path:
    """Create the decomposer's worktree fresh from current origin/<base>.

    Force-removes any existing decomposer worktree first; the decomposer
    is read-only and stateless across runs, so we always want it to see
    the current base, not whatever was left over from a prior run.
    """
    _repo_worktrees_root(spec).mkdir(parents=True, exist_ok=True)
    wt = _decompose_worktree_path(spec, issue_number)
    if wt.exists():
        _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)
    _git("fetch", "--quiet", "origin", spec.base_branch, cwd=spec.target_root)
    result = _git(
        "worktree", "add", "--detach", str(wt),
        f"origin/{spec.base_branch}",
        cwd=spec.target_root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return wt


def _cleanup_decompose_worktree(spec: RepoSpec, issue_number: int) -> None:
    """Remove the decomposer's worktree if it exists.

    Called at every `_handle_decomposing` exit except the dirty/commits
    park (where the operator may want to inspect before resuming). Failures
    are logged but never raised -- cleanup must not mask the real exit.
    """
    try:
        wt = _decompose_worktree_path(spec, issue_number)
        if wt.exists():
            _git("worktree", "remove", "--force", str(wt), cwd=spec.target_root)
    except Exception:
        log.exception(
            "issue=#%d failed to clean up decomposer worktree", issue_number,
        )


def _cleanup_merged_branch(
    gh: GitHubClient, spec: RepoSpec, issue_number: int
) -> None:
    """Remove the per-issue worktree and delete the local + remote branches.

    Called after the PR for `issue_number` has merged (either via AUTO_MERGE
    or an external human merge). Best-effort: each step swallows its own
    error so a leftover worktree or branch never raises out of the merge
    handler -- by the time we reach here the issue has already flipped to
    `done`, and a stale ref is tidiness, not correctness.

    Order matters: the worktree must come down before `git branch -D`,
    because git refuses to delete a branch that's still checked out in a
    worktree. Remote delete is last so a local-side failure does not block
    cleaning up the GitHub side (which is what the operator actually sees
    in the repo's branch list). All local `_git` calls run from
    `spec.target_root` so the multi-repo loop tidies the right clone.
    """
    branch = _branch_name(issue_number)
    wt = _worktree_path(spec, issue_number)
    if wt.exists():
        r = _git(
            "worktree", "remove", "--force", str(wt),
            cwd=spec.target_root,
        )
        if r.returncode != 0:
            log.warning(
                "issue=#%d worktree remove failed: %s",
                issue_number, (r.stderr or "").strip(),
            )

    have_local = _git(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
        cwd=spec.target_root,
    ).returncode == 0
    if have_local:
        r = _git("branch", "-D", branch, cwd=spec.target_root)
        if r.returncode != 0:
            log.warning(
                "issue=#%d local branch %r delete failed: %s",
                issue_number, branch, (r.stderr or "").strip(),
            )

    try:
        gh.delete_remote_branch(branch)
    except Exception:
        log.exception(
            "issue=#%d remote branch %r delete raised", issue_number, branch,
        )


def _push_branch(
    spec: RepoSpec, worktree: Path, branch: str,
    *,
    force_with_lease: Optional[str] = None,
) -> bool:
    """Push via GIT_ASKPASS so the token never appears in argv.

    `force_with_lease`, when provided, is the SHA the caller expects the
    remote ref to be at. The push then uses
    `--force-with-lease=refs/heads/<branch>:<sha>` against that exact SHA,
    so a concurrent update to the remote rejects the push instead of being
    silently clobbered. This is the squash/rewrite path: pinning the lease
    to the caller-supplied pre-rewrite HEAD (rather than reading it from
    the live remote) prevents the "out-of-band update happened in the
    window between approval and push" race -- a fresh `ls-remote` would
    treat the unexpected new remote SHA as the lease value and silently
    overwrite it.

    When `force_with_lease` is None (the default), the function reads the
    current remote SHA via `ls-remote` and uses that as the lease. This is
    the normal-push path: the orchestrator owns the
    `orchestrator/issue-<n>` namespace, but a self-restart between commit
    and push can leave the worktree on a different SHA than what was
    already pushed -- e.g. a `resume=False` rerun of codex amending
    equivalent work. A plain push then fails non-fast-forward and parks
    the issue. The lease lets the retry succeed while still refusing to
    clobber a concurrent foreign update (the lease check compares against
    what we observed, not a stale remote-tracking ref).

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
    auth_url = f"https://x-access-token@github.com/{spec.slug}.git"
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
        git_prefix = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "credential.helper=",
            "-c", "core.fsmonitor=",
        ]
        ref = f"refs/heads/{branch}"
        if force_with_lease is not None:
            remote_sha = force_with_lease
        else:
            ls = subprocess.run(
                [*git_prefix, "ls-remote", auth_url, ref],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                env=env,
            )
            if ls.returncode != 0:
                scrubbed = (ls.stderr or "").replace(config.GITHUB_TOKEN, "***")
                log.error("git ls-remote failed for %s: %s", branch, scrubbed)
                return False
            remote_sha = ""
            for line in (ls.stdout or "").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == ref:
                    remote_sha = parts[0]
                    break
        # An empty <expected> in --force-with-lease means "expect the ref to
        # not exist", which is the right lease for the create-branch case.
        r = subprocess.run(
            [
                *git_prefix,
                "push",
                f"--force-with-lease={ref}:{remote_sha}",
                auth_url, f"HEAD:{ref}",
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


def _squash_and_force_push(
    spec: RepoSpec, worktree: Path, branch: str, issue: Issue,
) -> Tuple[bool, Optional[str], int, Optional[str]]:
    """Squash all commits since `origin/<base>` into one, force-push with lease.

    Returns `(success, new_head_sha, squashed_count, error_message)`:
      * `(True, sha, 0, None)` — nothing to squash (zero or one commit on top
        of base). Caller should leave state alone; agent_approved_sha keeps
        pointing at the SHA the reviewer ran against.
      * `(True, sha, N, None)` — squashed N>1 commits into one. `sha` is the
        new local HEAD; the remote was force-pushed to match.
      * `(False, _, _, error)` — squash or push failed. Caller parks
        awaiting_human; the original commits remain on the local branch
        (we abort before resetting if any check fails) and the remote was
        not updated.

    The squash commit subject reuses the first commit's subject when it
    already matches conventional-commit form; otherwise it builds one from
    the issue title with a `feat:` prefix. Body aggregates the prior
    subjects so reviewers see what landed in the squash. The commit is
    authored under the AGENT_GIT_* identity (via env vars) so attribution
    matches the per-step commits this squash replaces.
    """
    base_ref = f"origin/{spec.base_branch}"
    mb = _git("merge-base", base_ref, "HEAD", cwd=worktree)
    if mb.returncode != 0:
        return False, None, 0, f"merge-base failed: {(mb.stderr or '').strip()}"
    base_sha = (mb.stdout or "").strip()
    if not base_sha:
        return False, None, 0, "merge-base returned empty"

    # Snapshot the original HEAD BEFORE any destructive step. Every
    # post-reset failure path below restores the branch to this SHA so
    # the original commits are still on the branch (as the issue spec
    # requires), and we use it as the pinned lease value for the
    # force-push (the remote was last set to this SHA by the dev's plain
    # push, so a remote drift between then and now means an out-of-band
    # update we must NOT clobber).
    original_head = _head_sha(worktree)
    if not original_head:
        return False, None, 0, "could not read original HEAD"

    log_r = _git(
        "log", "--reverse", "--pretty=%s", f"{base_sha}..HEAD",
        cwd=worktree,
    )
    if log_r.returncode != 0:
        return (
            False, None, 0,
            f"git log failed: {(log_r.stderr or '').strip()}",
        )
    subjects = [
        line for line in (log_r.stdout or "").splitlines() if line.strip()
    ]
    if len(subjects) <= 1:
        # Nothing to squash; the caller can still record original_head
        # as agent_approved_sha if it wants.
        return True, original_head, 0, None

    if _worktree_dirty_files(worktree):
        return False, None, 0, "worktree has uncommitted changes"

    if _is_conventional_subject(subjects[0]):
        subject = subjects[0]
    else:
        title = (issue.title or "").strip() or f"resolve issue #{issue.number}"
        subject = f"feat: {title}"

    body_lines = [
        "Squashed commits:",
        *(f"- {s}" for s in subjects),
    ]
    message = subject + "\n\n" + "\n".join(body_lines) + "\n"

    reset_r = _git("reset", "--soft", base_sha, cwd=worktree)
    if reset_r.returncode != 0:
        return (
            False, None, 0,
            f"reset --soft failed: {(reset_r.stderr or '').strip()}",
        )

    def _rollback(reason: str) -> None:
        """Restore the branch to original_head after a post-reset failure.
        Best-effort: a rollback failure leaves the worktree in an
        inconsistent state; logged loudly so an operator notices.
        """
        rb = _git("reset", "--hard", original_head, cwd=worktree)
        if rb.returncode != 0:
            log.error(
                "issue=#%s rollback to %s after %s failed; worktree may be "
                "in an inconsistent state: %s",
                issue.number, original_head, reason,
                (rb.stderr or "").strip(),
            )

    # Hardening for the orchestrator-owned squash commit. The agent has
    # write access to .git/hooks, .git/config (templatedir), and any
    # global/system git config the host user owns. Without these flags a
    # planted pre-commit hook or commit-msg hook would run during this
    # commit and could exfiltrate secrets we hold (no GIT_TOKEN here, but
    # ANTHROPIC_API_KEY etc. live in os.environ for the agent to use).
    # Mirrors the same hardening _push_branch applies.
    commit_env = {
        **os.environ,
        **_GIT_NO_PROMPT_ENV,
        "GIT_AUTHOR_NAME": config.AGENT_GIT_NAME,
        "GIT_AUTHOR_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_COMMITTER_NAME": config.AGENT_GIT_NAME,
        "GIT_COMMITTER_EMAIL": config.AGENT_GIT_EMAIL,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    commit_r = subprocess.run(
        [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=",
            "-c", "commit.gpgsign=false",
            "commit", "-m", message,
        ],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        env=commit_env,
    )
    if commit_r.returncode != 0:
        _rollback("squash commit")
        return (
            False, None, 0,
            f"squash commit failed: {(commit_r.stderr or '').strip()}",
        )

    new_sha = _head_sha(worktree)
    if not new_sha:
        _rollback("post-commit head read")
        return False, None, 0, "could not read new HEAD after squash"

    if not _push_branch(spec, worktree, branch, force_with_lease=original_head):
        _rollback("force-push")
        return False, None, 0, (
            "force-push with lease rejected (concurrent update on the "
            "remote, or lease violation); see orchestrator logs"
        )

    return True, new_sha, len(subjects), None


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
        "Before committing, run `git log --oneline -20` to see how recent commit subjects "
        "are formatted, and follow the same convention. This repo uses Conventional Commits "
        "of the form `<type>: <subject>` (e.g. `feat:`, `fix:`, `chore:`, `docs:`, "
        "`refactor:`, `test:`); pick the type that best fits your change and keep the "
        "subject short and imperative.\n\n"
        "The commit message MUST be the subject line only -- no extended description / "
        "body and no `Co-Authored-By:` (or other) trailer. Use `git commit -m \"<type>: "
        "<subject>\"` with a single `-m`.\n\n"
        "If you cannot proceed because of missing information, leave the working tree "
        "uncommitted (no commits) and end your response with a clear question for the human."
    )


def _build_review_prompt(spec: RepoSpec, issue: Issue, comments_text: str) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    return (
        f"You are an automated code reviewer for GitHub issue #{issue.number}: {issue.title!r}. "
        "A separate codex session has implemented this issue and committed to the current "
        f"branch. The base branch is `origin/{spec.base_branch}`.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        "Inspect the change with:\n"
        f"  git log --oneline origin/{spec.base_branch}..HEAD\n"
        f"  git diff origin/{spec.base_branch}...HEAD\n\n"
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
        "Before committing, run `git log --oneline -20` to see how recent commit subjects "
        "are formatted, and follow the same convention. This repo uses Conventional Commits "
        "of the form `<type>: <subject>` (e.g. `feat:`, `fix:`, `chore:`, `docs:`, "
        "`refactor:`, `test:`); for a review fix `fix:` is usually the right type.\n\n"
        "The commit message MUST be the subject line only -- no extended description / "
        "body and no `Co-Authored-By:` (or other) trailer. Use `git commit -m \"<type>: "
        "<subject>\"` with a single `-m`.\n\n"
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


def tick(gh: GitHubClient, spec: RepoSpec) -> None:
    for issue in gh.list_pollable_issues():
        try:
            _process_issue(gh, spec, issue)
        except Exception:
            log.exception(
                "repo=%s issue=#%s processing failed", spec.slug, issue.number,
            )


def _process_issue(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    label = gh.workflow_label(issue)
    log.info("repo=%s issue=#%s label=%r", spec.slug, issue.number, label)
    if label is None:
        _handle_pickup(gh, spec, issue)
    elif label == "decomposing":
        _handle_decomposing(gh, spec, issue)
    elif label == "ready":
        _handle_ready(gh, spec, issue)
    elif label == "blocked":
        _handle_blocked(gh, spec, issue)
    elif label == "implementing":
        _handle_implementing(gh, spec, issue)
    elif label == "validating":
        _handle_validating(gh, spec, issue)
    elif label == "in_review":
        _handle_in_review(gh, spec, issue)
    elif label == "resolving_conflict":
        _handle_resolving_conflict(gh, spec, issue)
    elif label in ("done", "rejected"):
        return
    else:
        log.warning(
            "repo=%s issue=#%s label=%r not implemented yet; leaving alone",
            spec.slug, issue.number, label,
        )


def _handle_pickup(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = PinnedState()
    state.set("created_at", _now_iso())
    if config.DECOMPOSE:
        pickup = _post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; decomposing.",
        )
        # Anchor the validating-handoff seed-watermark on the exact pickup
        # comment id (see legacy branch comment).
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
        gh.set_workflow_label(issue, "decomposing")
        gh.write_pinned_state(issue, state)
        _handle_decomposing(gh, spec, issue)
        return
    # Legacy path with DECOMPOSE=off: skip decomposition entirely and route
    # the unlabeled issue straight to implementing, exactly as the
    # bootstrap-milestone code did.
    pickup = _post_issue_comment(
        gh, issue, state,
        ":robot: orchestrator picking this up. Decomposition stage is "
        "disabled; going straight to implementation.",
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
    _handle_implementing(gh, spec, issue)


# Captures the JSON payload between a fenced ```orchestrator-manifest block.
# We deliberately match everything up to the next ``` rather than trying to
# bound braces in the regex itself: nested objects in the JSON body would
# trip a `\{.*?\}` non-greedy match without rescuing well, while a fence
# delimiter is a single token that the agent prompt forces it to emit.
_MANIFEST_RE = re.compile(
    r"```orchestrator-manifest\s*\n(.*?)\n```",
    re.DOTALL,
)
# Hard cap on children per parent. A buggy decomposer that emits 100 children
# would otherwise create 100 GitHub issues before anyone notices. Configurable
# later if needed; not surfaced as an env var initially.
_MAX_CHILDREN = 10


def _parse_manifest(
    last_message: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Parse a fenced `orchestrator-manifest` block.

    Returns `(manifest, error_reason)`:
      * `(dict, None)` -- a valid manifest. `decision` is `"single"` or
        `"split"`; for `"split"`, `children` is non-empty and each entry has
        `title`/`body` and a structurally-valid `depends_on` index list.
      * `(None, error)` -- a fence was present but the payload was invalid.
        `error` is a short human-readable reason (used in the HITL park
        message).
      * `(None, None)` -- no fenced block at all. The caller treats this as
        "agent ended without a manifest" and parks as a question.
    """
    if not last_message:
        return None, None
    matches = list(_MANIFEST_RE.finditer(last_message))
    if not matches:
        return None, None
    # The decompose prompt mandates "EXACTLY ONE fenced JSON block ...
    # and nothing else after it". `re.search` would silently accept the
    # first fence and ignore the rest, so a decomposer that quotes a
    # sample/template manifest before its real final answer would have
    # the orchestrator act on the sample -- creating wrong child issues
    # or routing the parent on a stale decision. Reject multiple fences
    # and require the accepted one to be the final block (whitespace
    # after the closing fence only).
    if len(matches) > 1:
        return None, (
            f"expected exactly one orchestrator-manifest block, "
            f"found {len(matches)}"
        )
    m = matches[0]
    if last_message[m.end():].strip():
        return None, (
            "orchestrator-manifest must be the final block; "
            "found content after the closing fence"
        )
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e.msg}"
    if not isinstance(data, dict):
        return None, "manifest is not a JSON object"
    decision = data.get("decision")
    if decision not in ("single", "split"):
        return None, "decision must be 'single' or 'split'"
    if decision == "single":
        return data, None
    children = data.get("children")
    if not isinstance(children, list) or not children:
        return None, "split decision requires non-empty children list"
    if len(children) > _MAX_CHILDREN:
        return None, (
            f"too many children ({len(children)} > {_MAX_CHILDREN})"
        )
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            return None, f"child {idx} is not an object"
        title = child.get("title")
        body = child.get("body")
        # Truthiness alone is not enough: `"body": 42` is truthy but
        # would later blow up `create_child_issue` (which calls
        # `body.rstrip()`) AFTER `expected_children_count` is persisted,
        # forcing the half-finished-recovery path. Reject non-string
        # values up front so the standard "invalid manifest" HITL/resume
        # loop handles it cleanly.
        if (
            not isinstance(title, str) or not title
            or not isinstance(body, str) or not body
        ):
            return None, f"child {idx} missing title or body"
        # Treat missing key and explicit JSON null as "no dependencies"
        # (same intent), but reject any other non-list value. The
        # earlier `child.get("depends_on") or []` collapsed every
        # falsy scalar (0, False, "") to [] before the list-type
        # check, so a manifest like `{"depends_on": 0}` -- a clear
        # malformed list -- was silently accepted as no-deps and the
        # child activated out of dependency order.
        deps = child.get("depends_on")
        if deps is None:
            deps = []
        elif not isinstance(deps, list):
            return None, f"child {idx} depends_on must be a list"
        for d in deps:
            if (
                not isinstance(d, int)
                or isinstance(d, bool)
                or d < 0
                or d >= len(children)
                or d == idx
            ):
                return None, f"child {idx} has invalid dependency {d!r}"
    if _has_dep_cycle(children):
        return None, "dependency graph has a cycle"
    return data, None


def _has_dep_cycle(children: list[dict]) -> bool:
    """DFS for back-edges in the children dep graph (white/gray/black)."""
    n = len(children)
    color = [0] * n  # 0=unvisited, 1=on-stack, 2=finished

    def visit(u: int) -> bool:
        color[u] = 1
        for v in (children[u].get("depends_on") or []):
            if color[v] == 1:
                return True
            if color[v] == 0 and visit(v):
                return True
        color[u] = 2
        return False

    for u in range(n):
        if color[u] == 0 and visit(u):
            return True
    return False


def _build_decompose_prompt(issue: Issue, comments_text: str) -> str:
    body = issue.body or "(no body)"
    convo = comments_text or "(no prior comments)"
    return (
        f"You are the decomposer for GitHub issue #{issue.number}: {issue.title!r}.\n\n"
        f"Issue body:\n{body}\n\n"
        f"Conversation so far:\n{convo}\n\n"
        "Decide whether this issue can be implemented in ONE coding-agent "
        "context window. If yes, return decision='single'. If no, propose a "
        "list of smaller child issues each one-shottable on its own.\n\n"
        "Sizing rule of thumb: if the change touches more than ~5 files or "
        "needs more than one logical commit, propose splitting; otherwise "
        "keep it as a single child. Use `git ls-files`, `wc -l`, or other "
        "read-only commands to inspect the codebase. You MUST NOT commit, "
        "push, or modify any file -- you are read-only.\n\n"
        "If you genuinely need a clarification, end your message with a "
        "question for the human and DO NOT emit a manifest. Otherwise, end "
        "your final message with EXACTLY ONE fenced JSON block in this "
        "format (and nothing else after it):\n\n"
        "```orchestrator-manifest\n"
        "{\n"
        "  \"decision\": \"split\",\n"
        "  \"rationale\": \"<<= 2 sentences why>\",\n"
        "  \"children\": [\n"
        "    {\"title\": \"...\", \"body\": \"...\", \"depends_on\": []}\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "The block must be valid JSON parseable by `json.loads`. The "
        "`decision` value must be exactly the string `\"single\"` or "
        "`\"split\"` (no other values, no union syntax). On `\"single\"`, "
        "omit the `children` field entirely.\n\n"
        "Rules for the children list (omit entirely on 'single'):\n"
        f"- At most {_MAX_CHILDREN} children.\n"
        "- `depends_on` is a list of 0-based indexes into THIS children "
        "array (not GitHub issue numbers; the orchestrator allocates those).\n"
        "- Self-dependencies and cycles are rejected.\n"
        "- Each child must be small enough to implement in one context "
        "(do not propose a child that itself needs decomposition)."
    )


def _read_decomposer_session(
    state: PinnedState,
) -> Tuple[str, Optional[str]]:
    """Return (decomposer_agent, decomposer_session_id) for an issue.

    Mirrors `_read_dev_session`: once `decomposer_agent` is written by a
    fresh spawn, the backend is locked for any future resumes on this issue
    so flipping `DECOMPOSE_AGENT` mid-flight cannot strand the session.
    """
    if state.get("decomposer_agent"):
        sid = state.get("decomposer_session_id")
        return (
            str(state.get("decomposer_agent")),
            str(sid) if sid is not None else None,
        )
    return config.DECOMPOSE_AGENT, None


def _resume_decomposer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
) -> Optional[AgentResult]:
    """Resume the decomposer's locked-backend session with new comments.

    Returns the agent result, or None if there are no new comments since
    the last park (caller should return without writing state).

    Mirrors `_resume_developer_on_human_reply` but on the decomposer
    session. The backend is locked to whichever wrote
    `decomposer_session_id`; resuming across backends would need an
    inter-backend session bridge that does not exist.
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
    wt = _decompose_worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _ensure_decompose_worktree(spec, issue.number)
    decomposer_agent, decomposer_sid = _read_decomposer_session(state)
    result = run_agent(
        decomposer_agent, followup, wt, resume_session_id=decomposer_sid
    )
    state.set("awaiting_human", False)
    return result


def _handle_decomposing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    # Track whether to keep the decomposer worktree past this tick. Set
    # True only in the dirty/commits park, where the operator may want to
    # inspect what the agent did. Every other exit (success or park)
    # cleans up via the finally below so the next consumer of this issue
    # number starts from current `origin/<base>`.
    keep_worktree = False
    try:
        # Half-finished decomposition recovery. Two persistent markers
        # signal a prior tick crashed mid-split:
        #   * `expected_children_count` is written BEFORE any child is
        #     created, so a SIGKILL after `create_child_issue` returns
        #     but before the parent records the new child number leaves
        #     the parent with this marker AND zero recorded children
        #     while an orphan child issue exists on GitHub. Re-running
        #     the decomposer here would emit a different manifest and
        #     create duplicate children alongside the orphan.
        #   * `children` is written incrementally after each successful
        #     create + parent-state flush. Its presence covers a crash
        #     after at least one child was recorded.
        # Either marker present without the parent label having flipped
        # to `blocked` means we cannot safely respawn the decomposer.
        # Branch by whether the recorded count matches expectations:
        # equal -> finalize to `blocked`; less -> park awaiting human.
        # Legacy state from a deploy that pre-dates
        # `expected_children_count` still routes through the
        # `children`-only branch and finalizes.
        expected_raw = state.get("expected_children_count")
        children_recorded = state.get("children") or []
        if expected_raw is not None or children_recorded:
            if state.get("awaiting_human"):
                return
            if expected_raw is not None and len(children_recorded) < int(
                expected_raw
            ):
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposition crashed mid-way: "
                    f"{len(children_recorded)} of {expected_raw} children "
                    "recorded (an orphan child issue may exist on GitHub if "
                    "the crash landed between `create_child_issue` returning "
                    "and the parent state write); manual intervention needed "
                    "(close any partial children and re-decompose, or finish "
                    "creating the missing ones).",
                )
                gh.write_pinned_state(issue, state)
                return
            # Before finalizing to `blocked`, repair any child whose pinned
            # state was never seeded. A SIGKILL between the parent's
            # incremental `children` write and the child-state write at
            # the LAST child satisfies `len(children) == expected_children_count`
            # but leaves that child orphaned: no `parent_number`, and likely
            # already parked with `awaiting_human=True` by a prior
            # `_handle_blocked` tick that saw it as "unattributed blocked".
            # Without repair, the parent's later walk flips the orphan to
            # `ready`, but `_handle_implementing` reads the stale park and
            # sits waiting for a human reply that never comes.
            for child_number in children_recorded:
                try:
                    child_issue = gh.get_issue(int(child_number))
                    child_state = gh.read_pinned_state(child_issue)
                    if not child_state.get("parent_number"):
                        child_state.set("parent_number", issue.number)
                        if not child_state.get("created_at"):
                            child_state.set("created_at", _now_iso())
                        child_state.set("awaiting_human", False)
                        child_state.set("park_reason", None)
                        gh.write_pinned_state(child_issue, child_state)
                except Exception:
                    log.exception(
                        "issue=#%s could not repair orphan child #%s during "
                        "decomposition recovery", issue.number, child_number,
                    )
                    _park_awaiting_human(
                        gh, issue, state,
                        f"{config.HITL_MENTIONS} could not repair child "
                        f"#{child_number} during decomposition recovery "
                        "(seed `parent_number` on its pinned state); manual "
                        "intervention needed (check orchestrator logs).",
                    )
                    gh.write_pinned_state(issue, state)
                    return
            gh.set_workflow_label(issue, "blocked")
            gh.write_pinned_state(issue, state)
            return

        # DECOMPOSE kill-switch bailout. Every path below this point
        # spawns the decomposer (fresh or via the awaiting_human
        # resume), so an operator who restarts with DECOMPOSE=off after
        # `_handle_pickup` already labeled the issue `decomposing` --
        # or while it is parked there awaiting a human -- would still
        # see the disabled rollout create manifests and child issues.
        # Drop into the legacy implementing flow exactly as
        # `_handle_pickup` does on a freshly unlabeled issue. The
        # half-finished recovery above must keep running regardless of
        # the flag: abandoning orphan children (already on GitHub)
        # because new decompositions are now disabled would strand
        # work, which is not what a kill switch should do.
        if not config.DECOMPOSE:
            _post_issue_comment(
                gh, issue, state,
                ":robot: decomposition is disabled; routing this issue "
                "to implementation.",
            )
            # Clear decomposer-side park state. Without this,
            # `_handle_implementing` reads `awaiting_human=True` and
            # tries to resume a dev session that was never spawned --
            # at best it stalls on `comments_after`, at worst the
            # follow-up text becomes the sole prompt instead of the
            # real implement prompt.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            # Mark every comment visible at this transition as
            # "already consumed", mirroring `_handle_ready`'s ratchet.
            # `_handle_implementing` will read the full issue thread
            # via `_recent_comments_text` when it builds the implement
            # prompt, so the dev sees any decomposing-era human
            # feedback at spawn. Without this bump, the
            # validating->in_review watermark seed later sees those
            # same comments as fresh PR feedback (because they sit
            # AFTER the now-stale `last_action_comment_id` from the
            # decomposer-era park) and bounces the dev unnecessarily.
            # One-way ratchet so we never lower a higher prior value.
            latest = gh.latest_comment_id(issue)
            if isinstance(latest, int):
                prior = state.get("last_action_comment_id")
                if not isinstance(prior, int) or latest > prior:
                    state.set("last_action_comment_id", latest)
            gh.set_workflow_label(issue, "implementing")
            gh.write_pinned_state(issue, state)
            _handle_implementing(gh, spec, issue)
            return

        if state.get("awaiting_human"):
            result = _resume_decomposer_on_human_reply(gh, spec, issue, state)
            if result is None:
                # No human reply yet. Keep the worktree intact -- if a
                # prior tick parked on the dirty/commits reason, the
                # HITL message explicitly asks the operator to inspect
                # and reset it before resuming, and cleanup here would
                # silently delete that state on every subsequent poll.
                keep_worktree = True
                return
        else:
            if not _check_and_increment_retry_budget(
                gh, issue, state, stage="decomposing"
            ):
                gh.write_pinned_state(issue, state)
                return
            wt = _ensure_decompose_worktree(spec, issue.number)
            decomposer_agent, _ = _read_decomposer_session(state)
            prompt = _build_decompose_prompt(issue, _recent_comments_text(issue))
            result = run_agent(decomposer_agent, prompt, wt)
            if result.session_id:
                state.set("decomposer_agent", decomposer_agent)
                state.set("decomposer_session_id", result.session_id)

        state.set("last_agent_action_at", _now_iso())

        if result.timed_out:
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer timed out after "
                f"{config.AGENT_TIMEOUT}s, manual intervention needed.",
            )
            gh.write_pinned_state(issue, state)
            return

        # The decomposer is supposed to be read-only. If it committed or
        # left uncommitted changes, something has gone wrong (prompt
        # ignored, agent misbehaving, operator scratch). Park awaiting
        # human and KEEP the worktree past this tick so the operator can
        # inspect what the decomposer actually produced before resetting.
        wt = _decompose_worktree_path(spec, issue.number)
        if _has_new_commits(spec, wt) or _worktree_dirty_files(wt):
            keep_worktree = True
            _park_awaiting_human(
                gh, issue, state,
                f"{config.HITL_MENTIONS} decomposer left commits or uncommitted "
                "changes in the worktree, but it must be read-only. Reset the "
                "worktree before resuming.",
            )
            gh.write_pinned_state(issue, state)
            return

        last_msg = result.last_message or ""
        parsed, error = _parse_manifest(last_msg)

        if parsed is None:
            # Either malformed manifest OR no manifest at all (question /
            # silence). Both park awaiting human; resume on the next
            # comment runs through the awaiting_human branch above.
            if error is not None:
                quoted = "> " + last_msg.strip().replace("\n", "\n> ")
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposer manifest invalid "
                    f"({error}); manual adjudication needed.\n\n"
                    f"_Last decomposer message:_\n\n{quoted}",
                )
            else:
                raw = last_msg.strip() or "(decomposer produced no final message)"
                quoted = "> " + raw.replace("\n", "\n> ")
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} decomposer needs your input to "
                    f"proceed:\n\n{quoted}",
                )
            gh.write_pinned_state(issue, state)
            return

        if parsed["decision"] == "single":
            # `_parse_manifest` only checks the decision string for the
            # single branch, so `rationale` may be any JSON value (or
            # missing). Coerce non-strings to the placeholder rather than
            # crashing the handler at `.strip()` after the agent already ran.
            raw_rationale = parsed.get("rationale")
            if not isinstance(raw_rationale, str):
                raw_rationale = ""
            rationale = raw_rationale.strip() or "(no rationale provided)"
            _post_issue_comment(
                gh, issue, state,
                f":mag: decomposer says this fits one context: {rationale}",
            )
            state.set("decomposed_at", _now_iso())
            gh.set_workflow_label(issue, "ready")
            gh.write_pinned_state(issue, state)
            return

        # decision == "split". Crash-safe sequence:
        #   1. Persist `expected_children_count` BEFORE creating any
        #      child. The half-finished recovery uses this to tell a
        #      partial loop apart from a completed one.
        #   2. For each child: create the GitHub issue, then
        #      IMMEDIATELY record its number in parent state (before
        #      any further non-idempotent work). A SIGKILL between
        #      these two steps is unavoidable; persisting first means
        #      the worst case is an orphan child without seeded
        #      `parent_number`, not a duplicate child created by a
        #      decomposer respawn.
        #   3. Seed child pinned state. Failure here parks but parent
        #      state already records the child, so no respawn happens.
        #   4. After the loop: post the summary, label parent
        #      `blocked`. Activation (children blocked -> ready) only
        #      runs AFTER this final write, so a crash here cannot
        #      leave a runnable orphan child against a
        #      `decomposing`-labeled parent.
        children_manifest = parsed["children"]
        created: list[Tuple[int, dict]] = []
        dep_graph: dict[str, list[int]] = {}
        state.set("expected_children_count", len(children_manifest))
        gh.write_pinned_state(issue, state)
        for idx, child in enumerate(children_manifest):
            depends_on = list(child.get("depends_on") or [])
            try:
                new_issue = gh.create_child_issue(
                    title=child["title"],
                    body=child["body"],
                    parent_number=issue.number,
                    labels=["blocked"],
                )
            except Exception:
                log.exception(
                    "issue=#%s could not create child %d (%r)",
                    issue.number, idx, child.get("title"),
                )
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} could not create child issue "
                    f"index={idx} ({child.get('title')!r}); manual intervention "
                    "needed (check orchestrator logs).",
                )
                gh.write_pinned_state(issue, state)
                return

            # Persist the child number on the parent BEFORE doing any
            # further work for this child. A SIGKILL between
            # `create_child_issue` returning and this write would leave
            # an orphan child on GitHub that the parent does not know
            # about; the next tick would re-spawn the decomposer and
            # create duplicates.
            created.append((new_issue.number, child))
            if depends_on:
                dep_graph[str(idx)] = depends_on
            state.set("children", [n for n, _ in created])
            if dep_graph:
                state.set("dep_graph", dep_graph)
            state.set("decomposed_at", _now_iso())
            gh.write_pinned_state(issue, state)

            # Seed `parent_number` on the child. Mandatory: without
            # it `_handle_blocked` parks the child as "manual relabel
            # suspected" and that park leaves `awaiting_human=True`
            # behind even after the parent later flips the child's
            # label to `ready` -- the child's `_handle_implementing`
            # would then sit waiting for a human comment instead of
            # starting work.
            try:
                child_state = PinnedState()
                child_state.set("parent_number", issue.number)
                child_state.set("created_at", _now_iso())
                gh.write_pinned_state(new_issue, child_state)
            except Exception:
                log.exception(
                    "issue=#%s could not seed pinned state on child #%d",
                    issue.number, new_issue.number,
                )
                # Parent already records the child (no duplicate
                # risk). Park so a human can either seed the child
                # manually or close it.
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} created child #{new_issue.number} "
                    f"({child.get('title')!r}) but could not seed its pinned "
                    "state with `parent_number`; manual intervention needed "
                    "(seed parent_number on the child or close it).",
                )
                gh.write_pinned_state(issue, state)
                return

        # children/dep_graph/decomposed_at are already durable from the
        # incremental writes in the loop above. Post the summary, flip
        # the parent label to `blocked`, and persist the new
        # orchestrator_comment_id. Activation (children blocked -> ready)
        # only runs AFTER this final write, so a crash here cannot leave
        # a runnable orphan child against a `decomposing`-labeled parent.
        summary = "\n".join(
            f"- #{n}: {child['title']}" for n, child in created
        )
        _post_issue_comment(
            gh, issue, state,
            f":bookmark_tabs: decomposer split this into {len(created)} "
            f"child issue(s):\n\n{summary}",
        )
        gh.set_workflow_label(issue, "blocked")
        gh.write_pinned_state(issue, state)

        # Activation: flip no-dep children from `blocked` to `ready`.
        # Best-effort -- if any flip fails the parent's `_handle_blocked`
        # walk handles it on the next tick (the walk treats a child with
        # no recorded deps as deps-satisfied).
        for idx, (child_number, _) in enumerate(created):
            if str(idx) in dep_graph:
                continue
            try:
                child_issue = gh.get_issue(child_number)
                gh.set_workflow_label(child_issue, "ready")
            except Exception:
                log.exception(
                    "issue=#%s could not flip child #%d to ready; the parent's "
                    "_handle_blocked walk will retry on the next tick",
                    issue.number, child_number,
                )
    finally:
        if not keep_worktree:
            _cleanup_decompose_worktree(spec, issue.number)


def _handle_ready(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """`ready` is the entry point for an auto-created child or for a parent
    whose decomposer voted `single`. Both cases need the same pickup-state
    seeding the legacy `_handle_pickup` did before flipping to
    `implementing`, so the validating handoff watermark and the in_review
    legacy migration have an anchor comment they can key on.
    """
    state = gh.read_pinned_state(issue)
    if state.get("pickup_comment_id") is None:
        if not state.get("created_at"):
            state.set("created_at", _now_iso())
        pickup = _post_issue_comment(
            gh, issue, state,
            ":robot: orchestrator picking this up; starting implementation.",
        )
        pickup_id = getattr(pickup, "id", None)
        if pickup_id is not None:
            state.set("pickup_comment_id", int(pickup_id))
    # Mark every comment visible right now as "already consumed". For a
    # parent that came through `decomposing` / `blocked`, `pickup_comment_id`
    # was anchored on the original "decomposing" comment, so any human
    # feedback posted while children were resolving sits AFTER pickup and
    # would be classified as post-pickup, unconsumed feedback by the
    # in_review watermark seed. The implementer reads the full thread via
    # `_recent_comments_text` at spawn, so by the time the PR reaches
    # `in_review` those comments have been incorporated; replaying them
    # would resume the dev and bounce the PR back to validating instead
    # of allowing merge. Bumping `last_action_comment_id` lets
    # `_seed_watermark_past_self`'s `consumed_through` walk advance past
    # them. The next park (or the validating handoff) will overwrite this
    # value, so it's a transient marker for the in-progress handoff only.
    latest = gh.latest_comment_id(issue)
    if isinstance(latest, int):
        prior = state.get("last_action_comment_id")
        if not isinstance(prior, int) or latest > prior:
            state.set("last_action_comment_id", latest)
    gh.set_workflow_label(issue, "implementing")
    gh.write_pinned_state(issue, state)
    _handle_implementing(gh, spec, issue)


def _handle_blocked(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    """Poll children to decide whether the parent unblocks (or one of the
    children unblocks).

    Workers run sequentially in the polling loop today, so the child's
    `in_review -> done` label flip and this tick cannot truly race; we read
    each child's current label fresh here.
    """
    state = gh.read_pinned_state(issue)
    children = state.get("children") or []
    if not children:
        # A blocked issue with `parent_number` recorded is a child waiting
        # on a sibling. The parent's `_handle_blocked` walks the dep graph
        # and flips the child to `ready` when its dependencies finish; this
        # tick has nothing to do. Without this branch the polling loop
        # would route every `blocked` child here, treat it as a parent
        # missing its `children` list, and park it as "manual relabel
        # suspected" -- leaving `awaiting_human=True` on the child even
        # after the parent later relabels it `ready`.
        if state.get("parent_number"):
            return
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} `blocked` without recorded children; "
            "manual relabel suspected.",
        )
        gh.write_pinned_state(issue, state)
        return

    child_labels: dict[int, Optional[str]] = {}
    child_issues: dict[int, Issue] = {}
    for child_number in children:
        try:
            child_issue = gh.get_issue(int(child_number))
        except Exception:
            log.exception(
                "issue=#%s could not read child #%d", issue.number, child_number,
            )
            return
        child_issues[int(child_number)] = child_issue
        child_labels[int(child_number)] = gh.workflow_label(child_issue)

    rejected = [n for n, lbl in child_labels.items() if lbl == "rejected"]
    if rejected:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) rejected: "
            f"{', '.join(f'#{n}' for n in rejected)}; "
            "decide whether to re-decompose or close.",
        )
        gh.write_pinned_state(issue, state)
        return

    # A child closed manually (e.g. via the GitHub UI) before reaching
    # `in_review` is invisible to `list_pollable_issues`, which only
    # sweeps closed issues for `in_review` (the externally-merged
    # path). Its workflow label stays frozen at whatever it was at
    # close -- ready/blocked/implementing/validating, or none at all
    # -- so without this branch the parent would read the stale label,
    # neither the rejected nor the all-done branch would fire, and the
    # parent would wait forever for a child that is gone. Treat it
    # like a rejected child so the operator can adjudicate. `in_review`
    # is intentionally allowed: a state=closed/label=in_review child is
    # the externally-merged transient that the closed-in_review sweep
    # finalizes on the next tick, NOT a manual override.
    manually_closed = [
        n for n, ci in child_issues.items()
        if getattr(ci, "state", "open") == "closed"
        and child_labels.get(n) not in ("done", "rejected", "in_review")
    ]
    if manually_closed:
        if state.get("awaiting_human"):
            return
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} child issue(s) closed without reaching "
            f"`done` or `rejected`: "
            f"{', '.join(f'#{n}' for n in manually_closed)}; "
            "decide whether to re-decompose or close.",
        )
        gh.write_pinned_state(issue, state)
        return

    if all(lbl == "done" for lbl in child_labels.values()):
        _post_issue_comment(
            gh, issue, state,
            ":white_check_mark: all children resolved; ready for "
            "implementation.",
        )
        # Clear any stale park left by a prior `rejected`-child tick: the
        # operator may have re-implemented the rejected child since, and
        # the parent now reaches `ready` legitimately. Without this clear,
        # `awaiting_human=True` survives into `_handle_implementing`,
        # which would route through `_resume_developer_on_human_reply`
        # and either replay long-stale comments or sit silent until a new
        # human reply arrives -- instead of just starting the parent's
        # implementation.
        state.set("awaiting_human", False)
        state.set("park_reason", None)
        gh.set_workflow_label(issue, "ready")
        gh.write_pinned_state(issue, state)
        return

    # Walk children: any `blocked` child whose recorded dependencies are
    # all `done` gets relabeled `ready`. A child with no recorded deps
    # also flips (vacuous all-done over an empty list) -- this recovers
    # any no-dep child that the decomposer's same-tick activation step
    # left as `blocked` (network blip, label-flip failure, etc.).
    dep_graph = state.get("dep_graph") or {}
    relabeled = False
    for idx, child_number in enumerate(children):
        cn = int(child_number)
        if child_labels.get(cn) != "blocked":
            continue
        deps = dep_graph.get(str(idx), [])
        dep_numbers = [
            int(children[int(d)]) for d in deps if int(d) < len(children)
        ]
        if all(child_labels.get(dn) == "done" for dn in dep_numbers):
            gh.set_workflow_label(child_issues[cn], "ready")
            relabeled = True
    if relabeled:
        gh.write_pinned_state(issue, state)


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
    gh: GitHubClient,
    issue: Issue,
    state: PinnedState,
    *,
    stage: str = "implementing",
) -> bool:
    """Gate fresh agent spawns by a per-issue 24h retry cap.

    The window starts at the first counted attempt and resets once 24h after
    that start has elapsed -- a fixed window per issue, not a true rolling
    window, but enough to stop a stuck issue from burning tokens for a day.
    Implementing and decomposing share the same per-issue counter on
    purpose: both consume the issue's daily spawn budget.

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
            f"{stage}; manual intervention needed. "
            f"Window opened at {window_start_raw}.",
        )
        return False

    state.set("retry_count", count + 1)
    return True


# After this many consecutive `agent_silent` parks on the same
# `dev_session_id`, `_resume_dev_with_text` drops the session id and starts
# a fresh spawn. Two strikes (rather than one) tolerates a transient
# single-call blip while still preventing the resume loop from burning every
# fresh-spawn retry slot on a poisoned session that's not coming back.
_SILENT_PARKS_BEFORE_FRESH_SESSION = 2


def _resume_dev_with_text(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    followup_text: str,
) -> Tuple[Path, AgentResult]:
    """Resume the dev's locked-backend session with the given prompt text.

    The backend is locked to whatever wrote `dev_session_id` (or the legacy
    `codex_session_id`) for this issue -- resuming across backends would need
    an inter-backend session bridge that does not exist. Clears the
    `awaiting_human` flag because the caller is reacting to a fresh human
    signal (issue or PR comment) by spawning the agent.

    After `_SILENT_PARKS_BEFORE_FRESH_SESSION` consecutive `agent_silent`
    parks on the current `dev_session_id`, the resume drops the session id
    and starts a fresh spawn instead. Sessions killed mid-stream (e.g. by a
    Claude rate limit) consistently return empty results on every subsequent
    resume; without this fallback every human "retry" comment burns another
    fresh-spawn retry slot on the same poisoned session.
    """
    wt = _worktree_path(spec, issue.number)
    if not wt.exists():
        wt = _ensure_worktree(spec, issue.number)
    dev_agent, dev_sid = _read_dev_session(state)
    silent_count = int(state.get("silent_park_count") or 0)
    fresh_spawn = (
        dev_sid is not None
        and silent_count >= _SILENT_PARKS_BEFORE_FRESH_SESSION
    )
    if fresh_spawn:
        log.info(
            "issue=#%d dropping poisoned dev session %r after %d "
            "consecutive silent parks; starting fresh",
            issue.number, dev_sid, silent_count,
        )
        dev_sid = None
        state.set("silent_park_count", 0)
        # Clear the poisoned session from pinned state BEFORE the spawn.
        # If the fresh spawn returns no `session_id` (or its persistence
        # is racy), the next tick must see a cleared session -- not the
        # old poisoned id, which `_read_dev_session` would otherwise
        # return again and burn another retry. Writing `dev_agent` here
        # also overrides the legacy `codex_session_id` fallback path:
        # `_read_dev_session` returns `(dev_agent, dev_session_id)` once
        # `dev_agent` is set, ignoring the legacy field. Clear the legacy
        # field too so the dropped session leaves no trace anywhere.
        state.set("dev_agent", dev_agent)
        state.set("dev_session_id", None)
        state.set("codex_session_id", None)
    result = run_agent(dev_agent, followup_text, wt, resume_session_id=dev_sid)
    if fresh_spawn and result.session_id:
        # Fresh spawn produced a session id -- record it so subsequent
        # resumes pick up the live session. Mirrors the persistence done
        # in `_handle_implementing`'s fresh-spawn branch.
        state.set("dev_session_id", result.session_id)
    state.set("awaiting_human", False)
    return wt, result


def _resume_developer_on_human_reply(
    gh: GitHubClient, spec: RepoSpec, issue: Issue, state: PinnedState
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
    return _resume_dev_with_text(gh, spec, issue, state, followup)


def _handle_implementing(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)

    if state.get("awaiting_human"):
        resumed = _resume_developer_on_human_reply(gh, spec, issue, state)
        if resumed is None:
            return
        wt, result = resumed
    else:
        wt = _ensure_worktree(spec, issue.number)
        if _has_new_commits(spec, wt):
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

    wt = _worktree_path(spec, issue.number)
    if _has_new_commits(spec, wt):
        dirty = _worktree_dirty_files(wt)
        if dirty:
            _on_dirty_worktree(gh, issue, state, result, dirty)
        else:
            _on_commits(gh, spec, issue, state, result)
    else:
        _on_question(gh, issue, state, result)

    gh.write_pinned_state(issue, state)


def _handle_dev_fix_result(
    gh: GitHubClient,
    spec: RepoSpec,
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
        # Tag as transient: a stuck dev fix-loop (validating CHANGES_REQUESTED
        # or comment-driven resume) clears on the next tick when the validating
        # recovery branch lets the reviewer re-run; the in_review fix-loop
        # leaves it tagged but stays parked because in_review's transient set
        # does not include this reason.
        state.set("park_reason", "agent_timeout")
        # Persist the pre-agent SHA so the recovery branch can tell whether
        # the timeout actually produced a new commit. `_has_new_commits()`
        # would say yes for any normal PR worktree (the dev's earlier fixes
        # are ahead of `origin/<base>` even when this run did nothing), so
        # without this watermark the recovery would force-push a stale
        # local HEAD and bump the round on every tick.
        state.set("pre_dev_fix_sha", before_sha or "")
        return False

    after_sha = _head_sha(wt)
    if after_sha == before_sha or not after_sha:
        # No new commit: dev asked a question or did nothing.
        _on_question(gh, issue, state, result)
        return False

    # A new commit landed -- the session is alive and producing output, so
    # the silent-park streak must reset here. Otherwise a single later
    # empty resume would tip a healthy session past the fresh-session
    # threshold (`silent_park_count` is meant to count *consecutive*
    # silent parks, not lifetime silent parks). Covers the validating /
    # in_review fix paths whose success exit (`return True` below) bypasses
    # `_on_commits` / `_on_dirty_worktree`, which would otherwise be the
    # only resetters on this branch.
    state.set("silent_park_count", 0)

    dirty = _worktree_dirty_files(wt)
    if dirty:
        _on_dirty_worktree(gh, issue, state, result, dirty)
        return False

    branch = _branch_name(issue.number)
    if not _push_branch(spec, wt, branch):
        _park_awaiting_human(
            gh, issue, state,
            f"{config.HITL_MENTIONS} git push failed; see orchestrator logs.",
        )
        # Tag as transient so a self-resolving condition (the next push
        # succeeds under --force-with-lease once the remote settles) can
        # silently recover the issue without needing a human comment.
        state.set("park_reason", "push_failed")
        return False

    return True


def _handle_validating(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
    state = gh.read_pinned_state(issue)
    pr_number = state.get("pr_number")

    # Awaiting-human path: human replied after a park; resume the developer
    # codex with their feedback. Identical mechanic to implementing's resume,
    # but on success we stay in validating and bump the round so the reviewer
    # runs again on the next tick.
    if state.get("awaiting_human"):
        # Transient-park recovery: when the original park reason is something
        # that can resolve without a human comment (a push race that the
        # next --force-with-lease push will land, or an agent timeout that
        # the next tick can simply rerun past), re-attempt silently. This
        # mirrors the in_review recovery branch -- without it, the issue
        # would sit forever, because `_resume_developer_on_human_reply`
        # only fires on new issue-thread comments and the human action
        # that unstuck the underlying condition typically does not include
        # one.
        last_action_id = state.get("last_action_comment_id")
        new_comments = gh.comments_after(issue, last_action_id)
        park_reason = state.get("park_reason")
        if (
            not new_comments
            and park_reason in _VALIDATING_TRANSIENT_PARK_REASONS
        ):
            if not _try_recover_validating_transient_park(
                spec, issue, state
            ):
                return  # still stuck, do not re-post the park comment
            # Conditions resolved: clear the park flags. The recovery
            # helper has already bumped review_round when a fix landed
            # (push_failed, or agent_timeout that finished its push), so
            # we only handle the flag clear here. The handoff watermark
            # `last_action_comment_id` was already advanced by the
            # original `_park_awaiting_human` call, so the in_review
            # handler will not re-feed any already-consumed comments.
            state.set("awaiting_human", False)
            state.set("park_reason", None)
            gh.write_pinned_state(issue, state)
            return
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        resumed = _resume_developer_on_human_reply(gh, spec, issue, state)
        if resumed is None:
            return
        wt, result = resumed
        state.set("last_agent_action_at", _now_iso())
        if not _handle_dev_fix_result(
            gh, spec, issue, state, wt, result, before_sha
        ):
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

    wt = _ensure_worktree(spec, issue.number)
    # The reviewer reads the local worktree's HEAD; remember which commit
    # that is so the in_review handoff can persist the SHA the agent
    # actually inspected. Setting `agent_approved_sha = pr.head.sha`
    # instead would mark the REMOTE head at handoff time as agent-approved,
    # which lets AUTO_MERGE land an unreviewed commit if the branch was
    # force-pushed or otherwise updated between the review and the handoff.
    reviewed_sha = _head_sha(wt)
    review_prompt = _build_review_prompt(spec, issue, _recent_comments_text(issue))
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
        # Tag as transient so the next tick re-spawns the reviewer instead
        # of waiting for a human comment that the timeout itself does not
        # produce.
        state.set("park_reason", "reviewer_timeout")
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

        # Squash before seeding the in_review handoff. If the squash or
        # force-push fails we park awaiting_human and STAY in `validating`
        # (no relabel), so the original commits remain on the branch and a
        # human can adjudicate. On success the new local HEAD becomes the
        # SHA AUTO_MERGE will gate on; the existing
        # `agent_approved_sha == pr.head.sha` invariant then keeps holding
        # because the remote also points at the new SHA after the
        # force-push, and `_latest_review_states_for_head` naturally
        # invalidates any stale GitHub review (its `commit_id` no longer
        # matches the new head), leaving the agent's `agent_approved_sha`
        # as the only thing keeping AUTO_MERGE viable -- which is exactly
        # the point.
        new_head_sha = reviewed_sha
        squashed_count = 0
        if config.SQUASH_ON_APPROVAL:
            success, sha_after, n_squashed, err = _squash_and_force_push(
                spec, wt, _branch_name(issue.number), issue,
            )
            if not success:
                _park_awaiting_human(
                    gh, issue, state,
                    f"{config.HITL_MENTIONS} squash-on-approval failed "
                    f"({err}); the original commits are still on the "
                    "branch and the PR was not relabeled. Manual "
                    "intervention needed (squash + force-push by hand, "
                    "or set `SQUASH_ON_APPROVAL=off` and re-run the "
                    "reviewer).",
                )
                gh.write_pinned_state(issue, state)
                return
            if sha_after:
                new_head_sha = sha_after
            squashed_count = n_squashed

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
                # Post the squash PR comment BEFORE seeding watermarks so
                # the seed walks past it (its id lands in
                # `orchestrator_comment_ids` via `_post_pr_comment`).
                # Without that ordering, the next in_review tick treats
                # the squash comment as fresh PR feedback once the
                # debounce expires and resumes the dev session over an
                # informational orchestrator post.
                if squashed_count > 1:
                    try:
                        _post_pr_comment(
                            gh, int(pr_number), state,
                            f":package: squashed {squashed_count} commits "
                            "to 1 after approval",
                        )
                    except Exception:
                        log.exception(
                            "issue=#%s could not post squash notice to "
                            "PR #%s", issue.number, pr_number,
                        )
                # Persist the local SHA the reviewer (or the squash)
                # produced, not the current remote head. The auto-merge
                # gate's existing `agent_approved_sha == head_sha` check
                # then naturally rejects the branch-update race: if the
                # remote moves past this SHA, agent_approved_sha won't
                # match and AUTO_MERGE waits for a fresh review round.
                if new_head_sha:
                    state.set("agent_approved_sha", new_head_sha)
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

    if not _handle_dev_fix_result(
        gh, spec, issue, state, wt, dev_result, before_sha
    ):
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
        "Before committing, run `git log --oneline -20` to see how recent commit "
        "subjects are formatted, and follow the same convention. This repo uses "
        "Conventional Commits of the form `<type>: <subject>` (e.g. `feat:`, "
        "`fix:`, `chore:`, `docs:`, `refactor:`, `test:`); for a review fix "
        "`fix:` is usually the right type.\n\n"
        "The commit message MUST be the subject line only -- no extended "
        "description / body and no `Co-Authored-By:` (or other) trailer. Use "
        "`git commit -m \"<type>: <subject>\"` with a single `-m`.\n\n"
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

# Validating-side counterpart: park reasons whose underlying condition can
# resolve without any human comment. Without this, a transient validating
# failure would leave the issue parked forever -- `_resume_developer_on_human_reply`
# only fires on a new issue-thread comment, and the human action that
# unstuck the underlying condition (a flake clears, CI settles, the remote
# accepts the next push) typically does not include one.
#
#   `push_failed`     - non-fast-forward push; retried under --force-with-lease.
#   `agent_timeout`   - dev-fix agent timed out; let the next tick re-run the
#                       reviewer (which will spawn the dev again if changes
#                       are still requested).
#   `reviewer_timeout`- reviewer agent timed out; let the next tick re-run it.
#
# Reasons that need human content (a question, a dirty worktree, a verdict
# the agent could not produce) stay parked until a comment arrives.
_VALIDATING_TRANSIENT_PARK_REASONS = frozenset(
    {"push_failed", "agent_timeout", "reviewer_timeout"}
)


def _try_recover_validating_transient_park(
    spec: RepoSpec, issue: Issue, state: PinnedState
) -> bool:
    """Quietly attempt to clear a transient validating park.

    Returns True if the underlying condition has resolved (caller should
    clear the park flags and progress); False to stay parked. Must not
    spawn the agent or post issue/PR comments -- the caller owns the
    visible side of the recovery so a still-stuck tick produces no churn.

    The helper IS allowed to update review-round bookkeeping when a fix
    landed during recovery (e.g. an agent_timeout where the dev had
    actually committed before timing out, and we finish the push here).
    Callers should not mutate the round themselves; this is the only
    write path while the park flags are still set.
    """
    park_reason = state.get("park_reason")
    if park_reason == "push_failed":
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            # Worktree was reaped; the dev's local commits are gone, so
            # there is nothing to push. A human has to intervene (relabel
            # back to implementing) -- that's the unblocking signal.
            return False
        if not _push_branch(spec, wt, _branch_name(issue.number)):
            return False
        # The dev's fix is now landed; bump the round so the cap reflects
        # the completed fix cycle.
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    if park_reason == "reviewer_timeout":
        # Reviewer agent only reads the worktree; nothing to reconcile
        # locally. Clear flags so the next tick re-spawns the reviewer
        # with a fresh budget.
        return True
    if park_reason == "agent_timeout":
        # The dev agent could have committed or left uncommitted edits
        # before the timeout killed it. Recovery cannot just clear flags
        # -- the next tick's reviewer would inspect the LOCAL worktree
        # and could approve a SHA that is not on the PR, seeding
        # `agent_approved_sha` to an unpushed commit and stalling
        # in_review. Reconcile the worktree explicitly here.
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            return False
        if _worktree_dirty_files(wt):
            # The dev left edits that were never committed. We cannot
            # safely push, review, or auto-merge in this state; stay
            # parked until a human or a fresh comment-driven resume
            # sorts it out. A reviewer that ignored the dirty index
            # would vote on the committed head while the leftover edits
            # are silently dropped on the next push.
            return False
        # The pre-agent SHA was persisted when the timeout park ran.
        # Compare against the current worktree HEAD instead of
        # `_has_new_commits()`, which only checks against
        # `origin/<base>` and would always say "yes" for a PR worktree
        # whose earlier fixes already shipped.
        pre_sha = state.get("pre_dev_fix_sha")
        if not isinstance(pre_sha, str):
            # Defensive: the timeout-tagging path always persists this,
            # so a missing watermark means foreign state we cannot
            # reason about. Stay parked rather than risk force-pushing
            # an out-of-date HEAD over the remote.
            return False
        now_sha = _head_sha(wt)
        if not now_sha or now_sha == pre_sha:
            # The timeout produced no new commit. Clear flags but do
            # not bump the round or push -- nothing landed.
            state.set("pre_dev_fix_sha", None)
            return True
        # The dev committed before timing out. Finish what it started
        # by pushing the new SHA; on success the fix is now landed and
        # we bump the round just like the push_failed branch.
        if not _push_branch(spec, wt, _branch_name(issue.number)):
            return False
        state.set("pre_dev_fix_sha", None)
        round_n = int(state.get("review_round") or 0)
        state.set("review_round", round_n + 1)
        return True
    return False


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


def _handle_in_review(gh: GitHubClient, spec: RepoSpec, issue: Issue) -> None:
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
        _cleanup_merged_branch(gh, spec, issue.number)
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
        wt = _worktree_path(spec, issue.number)
        if not wt.exists():
            wt = _ensure_worktree(spec, issue.number)
        before_sha = _head_sha(wt)
        wt, dev_result = _resume_dev_with_text(gh, spec, issue, state, followup)
        state.set("last_agent_action_at", _now_iso())
        if not _handle_dev_fix_result(
            gh, spec, issue, state, wt, dev_result, before_sha
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
    _cleanup_merged_branch(gh, spec, issue.number)


def _handle_resolving_conflict(
    gh: GitHubClient, spec: RepoSpec, issue: Issue
) -> None:
    """Placeholder for the conflict-resolution stage. No behavior yet -- the
    label and dispatcher branch exist so a follow-up child can wire in the
    actual rebase/retry logic without another label-rollout step.
    """
    log.info(
        "repo=%s issue=#%s resolving_conflict (no-op placeholder)",
        spec.slug, issue.number,
    )


def _on_commits(
    gh: GitHubClient,
    spec: RepoSpec,
    issue: Issue,
    state: PinnedState,
    result: AgentResult,
) -> None:
    wt = _worktree_path(spec, issue.number)
    branch = _branch_name(issue.number)
    if not _push_branch(spec, wt, branch):
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
    pr = gh.find_open_pr(branch=branch, base=spec.base_branch)
    if pr is None:
        title = _pr_title_from_commit_or_issue(issue, _first_commit_subject(wt))
        dev_agent, dev_sid = _read_dev_session(state)
        body_parts = [
            f"Resolves #{issue.number}",
            "",
            f"Generated by orchestrator ({dev_agent} session `{dev_sid or '?'}`).",
        ]
        if result.last_message.strip():
            body_parts += ["", "---", "_Last agent message:_", "", result.last_message[:2000]]
        pr = gh.open_pr(
            branch=branch, base=spec.base_branch, title=title, body="\n".join(body_parts)
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
    # The session just produced commits, so it isn't poisoned -- reset the
    # silent-park streak so a future blip doesn't tip an otherwise-healthy
    # session past the fresh-session threshold.
    state.set("silent_park_count", 0)
    gh.set_workflow_label(issue, "validating")


def _on_question(
    gh: GitHubClient, issue: Issue, state: PinnedState, result: AgentResult
) -> None:
    raw = result.last_message.strip()
    if raw:
        quoted = "> " + raw.replace("\n", "\n> ")
        _post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent needs your input to proceed:\n\n{quoted}",
        )
        state.set("awaiting_human", True)
        # Real question parks are not transient: they need a human reply
        # before the auto-merge gates should run again. Clear any stale
        # `park_reason` left behind by a prior AUTO_MERGE failed_checks /
        # unmergeable park, and reset the silent-park streak.
        state.set("park_reason", None)
        state.set("silent_park_count", 0)
    else:
        # No commits AND no final message -- the agent produced literally
        # nothing. Callers only invoke `_on_question` when the worktree has
        # no new commits, so an empty `last_message` here is a silent
        # failure, not a content question. The most common cause is a
        # poisoned resume of a session previously killed mid-stream (e.g.
        # by a Claude rate limit). Tag the park with a distinct reason so
        # `_resume_dev_with_text` can drop the dev session id after enough
        # consecutive silent parks, and surface the situation accurately
        # to the operator instead of impersonating a real "agent has a
        # question" park.
        _post_issue_comment(
            gh, issue, state,
            f"{config.HITL_MENTIONS} agent produced no output (likely a "
            "session-resume failure); manual intervention needed.",
        )
        state.set("awaiting_human", True)
        state.set("park_reason", "agent_silent")
        state.set(
            "silent_park_count",
            int(state.get("silent_park_count") or 0) + 1,
        )
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
    # standing dirty-worktree question. Clear the silent-park streak too:
    # the agent produced output, so the session is not poisoned.
    state.set("park_reason", None)
    state.set("silent_park_count", 0)
    latest = gh.latest_comment_id(issue)
    if latest is not None:
        state.set("last_action_comment_id", latest)
