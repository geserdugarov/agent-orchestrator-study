"""Minimal in-memory fakes for the orchestrator's GitHub surface.

Only the methods workflow.py actually calls are implemented. State lives in
plain dicts/lists on the fake so tests can assert on it directly without
needing extra recorder objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
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

    def get_comments(self) -> Iterable[FakeComment]:
        return list(self.comments)


@dataclass
class FakePR:
    number: int
    head_branch: str = ""
    base_branch: str = "main"
    title: str = ""
    body: str = ""


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
        # Recorders for assertions.
        self.posted_comments: list[tuple[int, str]] = []
        self.posted_pr_comments: list[tuple[int, str]] = []
        self.label_history: list[tuple[int, Optional[str]]] = []
        self.opened_prs: list[FakePR] = []
        self.write_state_calls: int = 0
        # Configurable: what find_open_pr returns (per-branch).
        self.existing_open_pr: dict[str, FakePR] = {}

    def seed_state(self, issue_number: int, **data: Any) -> None:
        """Pre-populate pinned state for an issue. The next read_pinned_state
        returns a wrapper around this dict (with a synthetic comment_id)."""
        self._pinned[issue_number] = PinnedState(
            comment_id=next(self._comment_id), data=dict(data)
        )

    def add_issue(self, issue: FakeIssue) -> None:
        self._issues[issue.number] = issue

    def list_open_issues(self) -> Iterable[FakeIssue]:
        return list(self._issues.values())

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
        self.posted_pr_comments.append((pr_number, body))
        return FakeComment(id=next(self._comment_id), body=body)

    def find_open_pr(self, *, branch: str, base: str) -> Optional[FakePR]:
        return self.existing_open_pr.get(branch)
