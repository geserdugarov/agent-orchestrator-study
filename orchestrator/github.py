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
    def __init__(self, token: Optional[str] = None, repo_slug: Optional[str] = None):
        token = token or config.GITHUB_TOKEN
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is empty. Export it in the orchestrator's "
                "environment or write it to "
                f"~/.config/{config.REPO}/token "
                "(override path with ORCHESTRATOR_TOKEN_FILE). "
                "Do NOT put it in REPO_ROOT/.env -- the implementer agent "
                "can read that file."
            )
        self._gh = Github(auth=Auth.Token(token))
        self.repo: Repository = self._gh.get_repo(repo_slug or config.REPO)

    def list_open_issues(self, since: Optional[datetime] = None) -> Iterable[Issue]:
        kwargs: dict[str, Any] = {
            "state": "open",
            "sort": "updated",
            "direction": "desc",
        }
        if since is not None:
            kwargs["since"] = since
        for issue in self.repo.get_issues(**kwargs):
            if issue.pull_request is None:
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
