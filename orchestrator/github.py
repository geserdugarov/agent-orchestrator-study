"""Thin GitHub client built on PyGithub.

Per-issue state is stored in a single 'pinned' comment whose body matches
PINNED_STATE_RE. The orchestrator owns this comment and only edits it from
write_pinned_state.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from github import Auth, Github, GithubException
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.PullRequest import PullRequest
from github.Repository import Repository

from . import config

log = logging.getLogger(__name__)

PINNED_STATE_MARKER = "<!--orchestrator-state"
PINNED_STATE_RE = re.compile(r"<!--orchestrator-state\s+(\{.*?\})\s*-->", re.DOTALL)
PINNED_STATE_TEMPLATE = "<!--orchestrator-state {payload}-->"

# (name, hex color, description) for each workflow label. Order = lifecycle.
WORKFLOW_LABEL_SPECS: tuple[tuple[str, str, str], ...] = (
    ("decomposing", "fbca04", "Orchestrator is breaking this issue into sub-issues"),
    ("ready", "0e8a16", "Decomposed and ready for implementation"),
    ("blocked", "b60205", "Blocked on another issue"),
    ("implementing", "1d76db", "A coding agent is working on this"),
    ("validating", "8a2be2", "Automated review/tests are running"),
    ("in_review", "d93f0b", "PR is open, awaiting human review"),
    ("done", "cccccc", "Merged to main"),
    ("rejected", "5c0000", "Issue rejected / closed without merge"),
)
WORKFLOW_LABELS = frozenset(name for name, _, _ in WORKFLOW_LABEL_SPECS)


@dataclass
class PinnedState:
    comment_id: Optional[int] = None
    data: dict = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class GitHubClient:
    def __init__(
        self,
        token: Optional[str] = None,
        repo_slug: Optional[str] = None,
        repo_spec: Optional["config.RepoSpec"] = None,
    ):
        # `repo_spec` wins when both are passed -- the multi-repo caller in
        # main.py threads a spec; legacy callers (and tests) still use the
        # `repo_slug` shortcut against the single-repo default.
        if repo_spec is not None:
            slug = repo_spec.slug
        else:
            slug = repo_slug or config.REPO
        token = token or config.GITHUB_TOKEN
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{slug}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file."
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(slug)

    def list_pollable_issues(self, since: Optional[datetime] = None) -> Iterable[Issue]:
        """Open issues plus closed issues still labeled `in_review`.

        The closed-in_review sweep is what makes the manual-merge path work:
        when a human merges a PR with a `Resolves #N` footer, GitHub closes
        the linked issue automatically. Without this sweep the next tick would
        not see issue #N at all and `_handle_in_review` could never flip the
        label to `done`. Once flipped the issue no longer carries `in_review`
        so the sweep stays bounded in steady state.
        """
        seen: set[int] = set()

        kwargs: dict[str, Any] = {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            kwargs["since"] = since
        for issue in self.repo.get_issues(**kwargs):
            if issue.pull_request is None and issue.number not in seen:
                seen.add(issue.number)
                yield issue

        # PyGithub's Repository.get_issues(labels=...) expects Label OBJECTS
        # and reads `label.name`; passing a raw string list raises a
        # TypeError before the sweep yields anything. Because that exception
        # propagates out of this generator on the second `for` -- past the
        # per-issue try/except in `tick()` -- it would silently break every
        # tick after open issues processed and leave externally-merged
        # in_review issues stuck closed-but-`in_review` forever. Look up
        # the Label once per call; treat a missing label as "nothing to
        # sweep" and skip rather than raising.
        try:
            in_review_label = self.repo.get_label("in_review")
        except GithubException as e:
            log.warning(
                "could not look up 'in_review' label for closed-issue "
                "sweep (HTTP %s); skipping. Externally-merged in_review "
                "issues will not finalize to `done` until the label exists.",
                e.status,
            )
            return
        closed_kwargs: dict[str, Any] = {
            "state": "closed",
            "labels": [in_review_label],
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            closed_kwargs["since"] = since
        for issue in self.repo.get_issues(**closed_kwargs):
            if issue.pull_request is None and issue.number not in seen:
                seen.add(issue.number)
                yield issue

    @staticmethod
    def workflow_label(issue: Issue) -> Optional[str]:
        for lbl in issue.labels:
            if lbl.name in WORKFLOW_LABELS:
                return lbl.name
        return None

    def set_workflow_label(self, issue: Issue, new_label: Optional[str]) -> None:
        keep = [l.name for l in issue.labels if l.name not in WORKFLOW_LABELS]
        if new_label:
            keep.append(new_label)
        issue.set_labels(*keep)

    def comment(self, issue: Issue, body: str) -> IssueComment:
        return issue.create_comment(body)

    def get_issue(self, number: int) -> Issue:
        return self.repo.get_issue(number)

    def create_child_issue(
        self,
        *,
        title: str,
        body: str,
        parent_number: int,
        labels: list[str],
    ) -> Issue:
        """Create a sub-issue in the same repo, with a `Parent: #<n>` link.

        Deliberately does NOT use a `Resolves #<parent>` keyword: GitHub
        would auto-close the parent the moment the child PR merges (when
        the parent has only this one open child reference), bypassing
        `_handle_blocked`'s aggregation across siblings. A plain
        `Parent: #<n>` line keeps the parent open until every child
        resolves and `_handle_blocked` flips the parent to `ready`.
        """
        full_body = f"{(body or '').rstrip()}\n\nParent: #{parent_number}"
        return self.repo.create_issue(title=title, body=full_body, labels=labels)

    def read_pinned_state(self, issue: Issue) -> PinnedState:
        for c in issue.get_comments():
            body = c.body or ""
            if PINNED_STATE_MARKER not in body:
                continue
            m = PINNED_STATE_RE.search(body)
            if m:
                try:
                    return PinnedState(comment_id=c.id, data=json.loads(m.group(1)))
                except json.JSONDecodeError:
                    log.warning("issue=#%s pinned state JSON unparseable", issue.number)
                    return PinnedState(comment_id=c.id, data={})
        return PinnedState()

    def write_pinned_state(self, issue: Issue, state: PinnedState) -> PinnedState:
        body = PINNED_STATE_TEMPLATE.format(
            payload=json.dumps(state.data, sort_keys=True)
        )
        if state.comment_id is None:
            created = issue.create_comment(body)
            state.comment_id = created.id
            return state
        for c in issue.get_comments():
            if c.id == state.comment_id:
                c.edit(body)
                return state
        # Pinned comment was deleted out from under us; recreate.
        created = issue.create_comment(body)
        state.comment_id = created.id
        return state

    def comments_after(
        self, issue: Issue, after_id: Optional[int]
    ) -> list[IssueComment]:
        result: list[IssueComment] = []
        for c in issue.get_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                result.append(c)
        return result

    def latest_comment_id(self, issue: Issue) -> Optional[int]:
        latest: Optional[int] = None
        for c in issue.get_comments():
            if latest is None or c.id > latest:
                latest = c.id
        return latest

    def open_pr(
        self, *, branch: str, base: str, title: str, body: str
    ) -> PullRequest:
        return self.repo.create_pull(title=title, body=body, head=branch, base=base)

    def pr_comment(self, pr_number: int, body: str) -> IssueComment:
        return self.repo.get_pull(pr_number).create_issue_comment(body)

    def find_open_pr(self, *, branch: str, base: str) -> Optional[PullRequest]:
        """Return an open PR with the given head branch, or None.

        Used to recover after a crash between create_pull and relabeling:
        a duplicate create_pull would 422 and trap the issue in implementing.
        """
        head = f"{self.repo.owner.login}:{branch}"
        for pr in self.repo.get_pulls(state="open", head=head, base=base):
            return pr
        return None

    def get_pr(self, pr_number: int) -> PullRequest:
        return self.repo.get_pull(pr_number)

    @staticmethod
    def pr_state(pr: PullRequest) -> str:
        """Return one of 'merged', 'closed', 'open'."""
        if pr.merged:
            return "merged"
        if pr.state == "closed":
            return "closed"
        return "open"

    @staticmethod
    def pr_is_mergeable(pr: PullRequest) -> Optional[bool]:
        """`pr.mergeable` is computed lazily by GitHub. None means "not yet",
        not "no" -- callers should wait a tick rather than treating it as a
        hard failure. We refresh once if the cached value is None.
        """
        if pr.mergeable is None:
            try:
                pr.update()
            except GithubException:
                return None
        return pr.mergeable

    def pr_combined_check_state(self, pr: PullRequest) -> str:
        """Return one of 'success', 'pending', 'failure', 'none'.

        Combines the legacy combined-status API (commit statuses) with the
        check-runs API (GitHub Actions, third-party Apps). Either source is
        sufficient to mark the head 'success'; either failing is failure;
        a pending in either source pends the whole. 'none' means there are
        no checks configured at all (ambiguous -- caller refuses to merge).

        Fails closed on a partial read: when one surface returned a usable
        signal but the other surface raised, the unread surface is treated
        as 'pending' so the result downgrades from 'success' to 'pending'.
        Without that, a single green commit-status context plus failing or
        pending GitHub Actions check-runs that the PAT cannot read (403 on
        check-runs from a missing 'Checks: read' scope, or a transient 5xx)
        would be reported as 'success' and AUTO_MERGE could land the PR
        over the unread failing checks.
        """
        head_sha = pr.head.sha
        states: list[str] = []
        read_failed = False

        try:
            combined = self.repo.get_commit(head_sha).get_combined_status()
            cs = combined.state
            if cs and cs != "":
                # 'success' / 'pending' / 'failure'/'error', plus 'pending'
                # when there are statuses but none have completed yet.
                if combined.total_count or cs != "pending":
                    states.append("failure" if cs == "error" else cs)
        except GithubException as e:
            log.warning(
                "could not read combined status for %s (HTTP %s); ignoring",
                head_sha, e.status,
            )
            read_failed = True

        try:
            check_runs = list(self.repo.get_commit(head_sha).get_check_runs())
            if check_runs:
                conclusions = [cr.conclusion for cr in check_runs]
                if any(c is None for c in conclusions):
                    states.append("pending")
                elif any(
                    c in ("failure", "timed_out", "action_required", "cancelled")
                    for c in conclusions
                ):
                    states.append("failure")
                elif all(
                    c in ("success", "neutral", "skipped")
                    for c in conclusions
                ):
                    states.append("success")
                else:
                    # Unknown conclusion shape -- fail safe.
                    states.append("failure")
        except GithubException as e:
            # 403 here almost always means the fine-grained PAT is missing
            # 'Checks: read'. For Actions-only PRs (no commit statuses,
            # only check-runs), swallowing this silently leaves
            # `pr_combined_check_state` at 'none' and AUTO_MERGE parks
            # forever despite the PR actually being green; surface the
            # remediation prominently so an operator can fix the scope.
            if e.status == 403:
                log.error(
                    "could not read check-runs for %s (HTTP 403). The "
                    "orchestrator PAT needs 'Checks: read' to evaluate "
                    "GitHub Actions PRs. Without it, AUTO_MERGE may "
                    "report check_state='none' and park indefinitely on "
                    "Actions-only PRs. Add the permission and restart.",
                    head_sha,
                )
            else:
                log.warning(
                    "could not read check-runs for %s (HTTP %s); ignoring",
                    head_sha, e.status,
                )
            read_failed = True

        # Partial read: at least one surface returned a usable signal but
        # the other surface raised. Treat the unread side as 'pending' so
        # an unread failing/pending check on that side cannot be masked by
        # the readable side's 'success'. AUTO_MERGE then waits (or parks
        # via the failed_checks branch on a sustained partial read) instead
        # of merging on half the picture. When BOTH surfaces failed the
        # branch is skipped and we return 'none' below, which the workflow
        # treats as ambiguous and parks awaiting_human -- visible to the
        # operator instead of silently waiting forever.
        if states and read_failed:
            states.append("pending")

        if not states:
            return "none"
        if "failure" in states:
            return "failure"
        if "pending" in states:
            return "pending"
        return "success"

    @staticmethod
    def _latest_review_states_for_head(
        pr: PullRequest, *, head_sha: str
    ) -> list[str]:
        """Latest review state per reviewer, restricted to `head_sha`.

        Approvals on older commits are treated as stale -- a commit pushed
        after a human approval must not auto-merge unless the human re-reviews
        the new head. GitHub's branch protection can enforce this server-side
        ("dismiss stale reviews") but we do not require that to be configured;
        the orchestrator gates auto-merge conservatively on its own.
        """
        if not head_sha:
            return []
        latest_per_user: dict[str, tuple[int, str]] = {}
        for review in pr.get_reviews():
            if (getattr(review, "commit_id", "") or "") != head_sha:
                continue
            state = (review.state or "").upper()
            if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            login = review.user.login if review.user else ""
            if not login:
                continue
            sub_id = getattr(review, "id", 0) or 0
            prev = latest_per_user.get(login)
            if prev is None or sub_id > prev[0]:
                latest_per_user[login] = (sub_id, state)
        return [s for _, s in latest_per_user.values()]

    @classmethod
    def pr_has_changes_requested(
        cls, pr: PullRequest, *, head_sha: str
    ) -> bool:
        """True if any reviewer's latest review on `head_sha` is
        CHANGES_REQUESTED. Independent of agent approval: a human veto on the
        current head must block auto-merge even if the reviewer agent OK'd
        the same SHA, otherwise `agent_approved_sha == head_sha` would let
        the orchestrator merge over a human's standing objection.
        """
        return any(
            s == "CHANGES_REQUESTED"
            for s in cls._latest_review_states_for_head(pr, head_sha=head_sha)
        )

    @classmethod
    def pr_is_approved(cls, pr: PullRequest, *, head_sha: str) -> bool:
        """True iff at least one APPROVED review exists for `head_sha` and no
        review on `head_sha` says CHANGES_REQUESTED.
        """
        states = cls._latest_review_states_for_head(pr, head_sha=head_sha)
        if not states:
            return False
        if any(s == "CHANGES_REQUESTED" for s in states):
            return False
        return any(s == "APPROVED" for s in states)

    def delete_remote_branch(self, branch: str) -> bool:
        """Delete the remote `<branch>` ref from the repo.

        Idempotent: a 404 (ref already gone) is treated as success because
        the repo's "Automatically delete head branches" setting may have
        removed the branch as part of the merge call. Other failures are
        logged and swallowed so a tidy-up step never raises out of the
        merge handler.
        """
        try:
            self.repo.get_git_ref(f"heads/{branch}").delete()
            return True
        except GithubException as e:
            if e.status == 404:
                return True
            log.warning(
                "could not delete remote branch %r (HTTP %s): %s",
                branch, e.status, e.data,
            )
            return False

    def merge_pr(
        self, pr: PullRequest, *, sha: str, method: str = "squash"
    ) -> bool:
        """SHA-pinned merge so a commit landing between our checks and the
        merge call cannot slip through unreviewed. PyGithub returns 409 if the
        head moved; we treat 405 (not mergeable) / 409 (sha mismatch) /
        422 (conflicts) as 'wait a tick' rather than retrying blind.
        """
        try:
            pr.merge(sha=sha, merge_method=method)
            return True
        except GithubException as e:
            log.warning(
                "merge failed for PR #%s (HTTP %s): %s",
                pr.number, e.status, e.data,
            )
            return False

    def pr_conversation_comments_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list[IssueComment]:
        """PR conversation comments (the `/issues/N/comments` resource) newer
        than `after_id`. These share the IssueComment id space with
        `issue.get_comments()`, so callers may use a single watermark across
        both. Inline review comments live in a separate id space and need
        `pr_inline_comments_after`.
        """
        out: list[IssueComment] = []
        for c in pr.get_issue_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
        return out

    def pr_inline_comments_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list:
        """Inline PR review comments (`/pulls/N/comments`) newer than
        `after_id`. These are PullRequestComment objects with their own id
        space, distinct from IssueComment ids -- mixing the two namespaces
        under one watermark drops or replays comments, so this method takes
        a separate watermark from the issue-comment side.
        """
        out: list = []
        for c in pr.get_review_comments():
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
        return out

    def pr_reviews_after(
        self, pr: PullRequest, after_id: Optional[int]
    ) -> list:
        """PR review summaries (`pr.get_reviews()`) newer than `after_id`,
        filtered to states whose body is actionable feedback for the dev:
        CHANGES_REQUESTED and COMMENTED. APPROVED is excluded -- the human
        approved, the body is informational. DISMISSED / PENDING never count.
        Empty bodies are dropped because there is nothing to forward.

        These objects live in the PullRequestReview id namespace, distinct
        from the IssueComment and PullRequestComment id spaces -- the
        in_review handler tracks `pr_last_review_summary_id` separately.

        Without this surface, a 'Comment' review with a request in the body
        is silently ignored and may be auto-merged over, and a
        CHANGES_REQUESTED review with body but no inline comments only
        blocks merge via `pr_has_changes_requested` without ever reaching
        the dev agent.
        """
        out: list = []
        for review in pr.get_reviews():
            state = (review.state or "").upper()
            if state not in ("CHANGES_REQUESTED", "COMMENTED"):
                continue
            body = (review.body or "").strip()
            if not body:
                continue
            if after_id is None or review.id > after_id:
                out.append(review)
        out.sort(key=lambda r: r.id)
        return out

    def ensure_workflow_labels(self) -> None:
        """Create any missing workflow labels on the repo. Idempotent.

        Best-effort: a 403 (under-scoped PAT) logs a clear instruction and
        returns without raising, so the polling loop keeps running. The user
        can fix the PAT scopes without restarting.
        """
        try:
            existing = {l.name for l in self.repo.get_labels()}
        except GithubException as e:
            log.warning(
                "could not list labels (HTTP %s); skipping label bootstrap. "
                "Grant the PAT 'Issues: Read and write' to enable.",
                e.status,
            )
            return
        for name, color, description in WORKFLOW_LABEL_SPECS:
            if name in existing:
                continue
            try:
                self.repo.create_label(name=name, color=color, description=description)
                log.info("created label %r", name)
            except GithubException as e:
                log.error(
                    "could not create label %r (HTTP %s). "
                    "Fine-grained PAT needs 'Issues: Read and write'. "
                    "Skipping remaining label bootstrap; orchestrator will keep "
                    "running and may retry on the next restart.",
                    name, e.status,
                )
                return
