"""Minimal in-memory fakes for the orchestrator's GitHub surface.

Only the methods workflow.py actually calls are implemented. State lives in
plain dicts/lists on the fake so tests can assert on it directly without
needing extra recorder objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any, Iterable, Optional

from orchestrator.github import (
    PINNED_STATE_MARKER,
    PinnedState,
    WORKFLOW_LABELS,
)


@dataclass
class FakeUser:
    login: str = "human"


@dataclass
class FakeComment:
    id: int
    body: str
    user: FakeUser = field(default_factory=FakeUser)
    # Real PyGithub `IssueComment.created_at` is always set; tests that don't
    # exercise the in_review debounce can leave this None.
    created_at: Optional[datetime] = None


@dataclass
class FakeLabel:
    name: str


@dataclass
class FakeIssue:
    number: int
    title: str = "test issue"
    body: str = "test body"
    labels: list[FakeLabel] = field(default_factory=list)
    comments: list[FakeComment] = field(default_factory=list)
    closed: bool = False

    @property
    def state(self) -> str:
        """Mirror PyGithub's Issue.state so workflow code can read it
        without branching on whether the issue object is a fake or real."""
        return "closed" if self.closed else "open"

    def get_comments(self) -> Iterable[FakeComment]:
        return list(self.comments)

    def edit(self, *, state: Optional[str] = None) -> None:
        if state == "closed":
            self.closed = True


@dataclass
class FakePRRef:
    sha: str = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    ref: str = ""


@dataclass
class FakePRReview:
    """Stand-in for a PullRequestReview object.

    Body + state model the "Comment" / "Request changes" submission flow.
    `submitted_at` mirrors PyGithub's field name (PullRequestReview exposes
    `submitted_at`, not `created_at`); the workflow's `_comment_created_at`
    helper falls back to it for debounce timestamping.
    """

    id: int
    body: str
    state: str = "COMMENTED"
    user: FakeUser = field(default_factory=lambda: FakeUser("alice"))
    submitted_at: Optional[datetime] = None
    commit_id: str = ""


@dataclass
class FakePR:
    number: int
    head_branch: str = ""
    base_branch: str = "main"
    title: str = ""
    body: str = ""
    # State surface used by `_handle_in_review`. Defaults match a freshly
    # opened PR: open, no merge, no approval, no checks configured.
    merged: bool = False
    state: str = "open"  # "open" or "closed"
    mergeable: Optional[bool] = True
    head: FakePRRef = field(default_factory=FakePRRef)
    approved: bool = False
    check_state: str = "none"  # one of success/pending/failure/none
    issue_comments: list[FakeComment] = field(default_factory=list)
    review_comments: list[FakeComment] = field(default_factory=list)
    # PR review summaries (the body posted alongside an APPROVE / REQUEST
    # CHANGES / COMMENT submission). Distinct id namespace from
    # issue_comments and review_comments; `_handle_in_review` tracks them
    # with `pr_last_review_summary_id`.
    reviews: list[FakePRReview] = field(default_factory=list)
    # When `approved` is True, model the SHA the approval was submitted on.
    # None means "the current head" (the common case for happy-path tests);
    # set to an older SHA to model a stale human approval after a force-push.
    approval_head_sha: Optional[str] = None
    # Human/bot CHANGES_REQUESTED review on the modeled head. Independent
    # from `approved` because a single PR can carry both an APPROVED review
    # from one user and a CHANGES_REQUESTED review from another; the veto
    # path needs to fire regardless of any APPROVED state.
    changes_requested: bool = False
    changes_requested_head_sha: Optional[str] = None


def make_issue(
    number: int,
    label: Optional[str] = None,
    comments: Iterable[FakeComment] = (),
    title: str = "test issue",
    body: str = "test body",
) -> FakeIssue:
    labels = [FakeLabel(label)] if label else []
    return FakeIssue(
        number=number,
        title=title,
        body=body,
        labels=labels,
        comments=list(comments),
    )


class FakeGitHubClient:
    """In-memory stand-in for orchestrator.github.GitHubClient.

    Behavior mirrors the real client's read/write semantics for pinned state
    and workflow labels, but state lives in dicts on this object so tests can
    inspect it directly.
    """

    def __init__(self, issues: Iterable[FakeIssue] = ()) -> None:
        self._issues: dict[int, FakeIssue] = {i.number: i for i in issues}
        self._pinned: dict[int, PinnedState] = {}
        self._comment_id = count(start=1000)
        self._pr_id = count(start=1)
        # New issues created via `create_child_issue` get sequential numbers
        # well above any number the test seeded so collisions are impossible.
        self._next_issue_number = count(
            start=max((i.number for i in self._issues.values()), default=0) + 100
        )
        # Recorders for assertions.
        self.posted_comments: list[tuple[int, str]] = []
        self.posted_pr_comments: list[tuple[int, str]] = []
        self.label_history: list[tuple[int, Optional[str]]] = []
        self.opened_prs: list[FakePR] = []
        self.created_child_issues: list[FakeIssue] = []
        self.write_state_calls: int = 0
        # Configurable: what find_open_pr returns (per-branch).
        self.existing_open_pr: dict[str, FakePR] = {}
        # PR-state surface for _handle_in_review. Tests pre-seed pulls and
        # toggle merge_returns_ok to exercise the sha-mismatch retry path.
        self.pulls: dict[int, FakePR] = {}
        self.merge_calls: list[tuple[int, str, str]] = []
        self.merge_returns_ok: bool = True
        # Branches the orchestrator asked us to delete remotely after merge.
        # Tests assert on this list to verify the post-merge cleanup hook
        # actually fires.
        self.deleted_remote_branches: list[str] = []
        self.delete_remote_branch_returns_ok: bool = True

    def seed_state(self, issue_number: int, **data: Any) -> None:
        """Pre-populate pinned state for an issue. The next read_pinned_state
        returns a wrapper around this dict (with a synthetic comment_id)."""
        self._pinned[issue_number] = PinnedState(
            comment_id=next(self._comment_id), data=dict(data)
        )

    def add_issue(self, issue: FakeIssue) -> None:
        self._issues[issue.number] = issue

    def list_pollable_issues(self) -> Iterable[FakeIssue]:
        """Mirror the real client: open issues plus closed-but-`in_review`
        issues. The closed-in_review sweep is what catches an external manual
        merge -- the linked issue auto-closes via "Resolves #N" before the
        orchestrator can flip its label to `done`.
        """
        out: list[FakeIssue] = []
        seen: set[int] = set()
        for issue in self._issues.values():
            if issue.closed:
                continue
            seen.add(issue.number)
            out.append(issue)
        for issue in self._issues.values():
            if not issue.closed or issue.number in seen:
                continue
            if any(l.name == "in_review" for l in issue.labels):
                seen.add(issue.number)
                out.append(issue)
        return out

    @staticmethod
    def workflow_label(issue: FakeIssue) -> Optional[str]:
        for lbl in issue.labels:
            if lbl.name in WORKFLOW_LABELS:
                return lbl.name
        return None

    def set_workflow_label(
        self, issue: FakeIssue, new_label: Optional[str]
    ) -> None:
        keep = [l for l in issue.labels if l.name not in WORKFLOW_LABELS]
        if new_label:
            keep.append(FakeLabel(new_label))
        issue.labels = keep
        self.label_history.append((issue.number, new_label))

    def comment(self, issue: FakeIssue, body: str) -> FakeComment:
        c = FakeComment(id=next(self._comment_id), body=body)
        issue.comments.append(c)
        self.posted_comments.append((issue.number, body))
        return c

    def get_issue(self, number: int) -> FakeIssue:
        return self._issues[int(number)]

    def create_child_issue(
        self,
        *,
        title: str,
        body: str,
        parent_number: int,
        labels: list[str],
    ) -> FakeIssue:
        full_body = f"{(body or '').rstrip()}\n\nParent: #{parent_number}"
        child = FakeIssue(
            number=next(self._next_issue_number),
            title=title,
            body=full_body,
            labels=[FakeLabel(l) for l in labels],
        )
        self._issues[child.number] = child
        self.created_child_issues.append(child)
        return child

    def read_pinned_state(self, issue: FakeIssue) -> PinnedState:
        existing = self._pinned.get(issue.number)
        if existing is None:
            return PinnedState()
        # Return a fresh wrapper around the same dict so handlers can mutate
        # state without us needing to deepcopy. Mirrors the real client's
        # behavior closely enough for the transitions under test.
        return PinnedState(comment_id=existing.comment_id, data=dict(existing.data))

    def write_pinned_state(
        self, issue: FakeIssue, state: PinnedState
    ) -> PinnedState:
        self.write_state_calls += 1
        if state.comment_id is None:
            state.comment_id = next(self._comment_id)
            issue.comments.append(
                FakeComment(
                    id=state.comment_id,
                    body=f"{PINNED_STATE_MARKER} ... -->",
                )
            )
        self._pinned[issue.number] = PinnedState(
            comment_id=state.comment_id, data=dict(state.data)
        )
        return state

    def pinned_data(self, issue_number: int) -> dict[str, Any]:
        """Convenience for tests: the last-written state dict for an issue."""
        st = self._pinned.get(issue_number)
        return dict(st.data) if st is not None else {}

    def comments_after(
        self, issue: FakeIssue, after_id: Optional[int]
    ) -> list[FakeComment]:
        out: list[FakeComment] = []
        for c in issue.comments:
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        return out

    def latest_comment_id(self, issue: FakeIssue) -> Optional[int]:
        latest: Optional[int] = None
        for c in issue.comments:
            if latest is None or c.id > latest:
                latest = c.id
        return latest

    def open_pr(
        self, *, branch: str, base: str, title: str, body: str
    ) -> FakePR:
        pr = FakePR(
            number=next(self._pr_id),
            head_branch=branch,
            base_branch=base,
            title=title,
            body=body,
        )
        self.opened_prs.append(pr)
        return pr

    def pr_comment(self, pr_number: int, body: str) -> FakeComment:
        c = FakeComment(
            id=next(self._comment_id),
            body=body,
            user=FakeUser("orchestrator"),
        )
        self.posted_pr_comments.append((pr_number, body))
        # Real GitHub PR conversation comments show up on subsequent
        # `pr.get_issue_comments()` calls. Mirror that so the watermark
        # initialized at validating -> in_review handoff includes the
        # approval comment we just posted.
        pr = self.pulls.get(pr_number)
        if pr is not None:
            pr.issue_comments.append(c)
        return c

    def find_open_pr(self, *, branch: str, base: str) -> Optional[FakePR]:
        return self.existing_open_pr.get(branch)

    def add_pr(self, pr: FakePR) -> None:
        """Pre-seed a PR for `_handle_in_review` to read. Tests usually pair
        this with `seed_state(..., pr_number=pr.number)`."""
        self.pulls[pr.number] = pr

    def get_pr(self, pr_number: int) -> FakePR:
        return self.pulls[pr_number]

    @staticmethod
    def pr_state(pr: FakePR) -> str:
        if pr.merged:
            return "merged"
        if pr.state == "closed":
            return "closed"
        return "open"

    @staticmethod
    def pr_is_mergeable(pr: FakePR) -> Optional[bool]:
        return pr.mergeable

    @staticmethod
    def pr_is_approved(pr: FakePR, *, head_sha: str) -> bool:
        if not pr.approved:
            return False
        sha = pr.approval_head_sha if pr.approval_head_sha is not None else pr.head.sha
        return sha == head_sha

    @staticmethod
    def pr_has_changes_requested(pr: FakePR, *, head_sha: str) -> bool:
        if not pr.changes_requested:
            return False
        sha = (
            pr.changes_requested_head_sha
            if pr.changes_requested_head_sha is not None
            else pr.head.sha
        )
        return sha == head_sha

    @staticmethod
    def pr_combined_check_state(pr: FakePR) -> str:
        return pr.check_state

    def merge_pr(
        self, pr: FakePR, *, sha: str, method: str = "squash"
    ) -> bool:
        self.merge_calls.append((pr.number, sha, method))
        if not self.merge_returns_ok:
            return False
        pr.merged = True
        pr.state = "closed"
        return True

    def delete_remote_branch(self, branch: str) -> bool:
        self.deleted_remote_branches.append(branch)
        return self.delete_remote_branch_returns_ok

    def pr_conversation_comments_after(
        self, pr: FakePR, after_id: Optional[int]
    ) -> list[FakeComment]:
        """PR conversation comments only (shares id space with
        `comments_after(issue, ...)`)."""
        out: list[FakeComment] = []
        for c in pr.issue_comments:
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
        return out

    def pr_inline_comments_after(
        self, pr: FakePR, after_id: Optional[int]
    ) -> list[FakeComment]:
        """Inline review comments only (separate id space)."""
        out: list[FakeComment] = []
        for c in pr.review_comments:
            if PINNED_STATE_MARKER in (c.body or ""):
                continue
            if after_id is None or c.id > after_id:
                out.append(c)
        out.sort(key=lambda c: c.id)
        return out

    def pr_reviews_after(
        self, pr: FakePR, after_id: Optional[int]
    ) -> list[FakePRReview]:
        """PR review summaries with non-empty body in CHANGES_REQUESTED or
        COMMENTED state, mirroring the real client's filter."""
        out: list[FakePRReview] = []
        for r in pr.reviews:
            state = (r.state or "").upper()
            if state not in ("CHANGES_REQUESTED", "COMMENTED"):
                continue
            body = (r.body or "").strip()
            if not body:
                continue
            if after_id is None or r.id > after_id:
                out.append(r)
        out.sort(key=lambda r: r.id)
        return out
