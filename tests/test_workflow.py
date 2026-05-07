from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ORCHESTRATOR_SKIP_DOTENV", "1")

from orchestrator import config, workflow
from orchestrator.agents import AgentResult
from orchestrator.workflow import _parse_review_verdict

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeLabel,
    FakePR,
    FakePRRef,
    FakePRReview,
    FakeUser,
    make_issue,
)


_FAKE_WT = Path("/tmp/orchestrator-test-wt-doesnt-matter")
# Tests don't shell out (the worktree/git helpers are mocked), so the values
# only need to be plausible -- the slug/base reach `_build_review_prompt`,
# `_push_branch`, and the `find_open_pr` / `open_pr` call sites and are
# inspected by some assertions; nothing else cares.
_TEST_SPEC = config.RepoSpec(
    slug="geserdugarov/agent-orchestrator",
    target_root=Path("/tmp/orchestrator-test-target-root"),
    base_branch="main",
)


def _agent(
    *,
    session_id: str = "sess-1",
    last_message: str = "",
    timed_out: bool = False,
) -> AgentResult:
    return AgentResult(
        session_id=session_id,
        last_message=last_message,
        exit_code=-1 if timed_out else 0,
        timed_out=timed_out,
        stdout="",
        stderr="",
    )


def _as_mock(value_or_seq):
    from unittest.mock import MagicMock

    if callable(value_or_seq):
        return value_or_seq
    if isinstance(value_or_seq, (list, tuple)):
        m = MagicMock()
        m.side_effect = list(value_or_seq)
        return m
    m = MagicMock()
    m.return_value = value_or_seq
    return m


class _PatchedWorkflowMixin:
    """Helper that wires standard patches around a single test body."""

    def _run(
        self,
        callable_,
        *,
        run_agent,
        has_new_commits=False,
        dirty_files=(),
        push_branch=True,
        head_shas=("",),
        first_commit_subject="",
    ):
        from unittest.mock import MagicMock

        rc_mock = _as_mock(run_agent)
        hnc_seq = has_new_commits if isinstance(has_new_commits, (list, tuple)) else None
        hnc_mock = MagicMock()
        if hnc_seq is not None:
            hnc_mock.side_effect = list(hnc_seq)
        else:
            hnc_mock.return_value = bool(has_new_commits)

        df_mock = MagicMock(return_value=list(dirty_files))
        push_mock = MagicMock(return_value=bool(push_branch))
        head_mock = MagicMock(side_effect=list(head_shas))
        wt_mock = MagicMock(return_value=_FAKE_WT)
        # Decomposer worktree helpers run real `git` calls in production.
        # Mock them with the same _FAKE_WT so `_handle_decomposing` tests
        # don't shell out (and the cleanup helper is a no-op).
        decompose_wt_mock = MagicMock(return_value=_FAKE_WT)
        decompose_path_mock = MagicMock(return_value=_FAKE_WT)
        cleanup_decompose_mock = MagicMock()
        # `_on_commits` reads the worktree's first commit subject to derive
        # the PR title; mock it so tests don't shell out to git.
        first_subject_mock = MagicMock(return_value=first_commit_subject)
        cleanup_merged_mock = MagicMock()

        with patch.object(workflow, "run_agent", rc_mock), \
             patch.object(workflow, "_ensure_worktree", wt_mock), \
             patch.object(workflow, "_ensure_decompose_worktree", decompose_wt_mock), \
             patch.object(workflow, "_decompose_worktree_path", decompose_path_mock), \
             patch.object(workflow, "_cleanup_decompose_worktree", cleanup_decompose_mock), \
             patch.object(workflow, "_cleanup_merged_branch", cleanup_merged_mock), \
             patch.object(workflow, "_has_new_commits", hnc_mock), \
             patch.object(workflow, "_worktree_dirty_files", df_mock), \
             patch.object(workflow, "_push_branch", push_mock), \
             patch.object(workflow, "_head_sha", head_mock), \
             patch.object(workflow, "_first_commit_subject", first_subject_mock):
            callable_()

        return {
            "run_agent": rc_mock,
            "_ensure_worktree": wt_mock,
            "_ensure_decompose_worktree": decompose_wt_mock,
            "_decompose_worktree_path": decompose_path_mock,
            "_cleanup_decompose_worktree": cleanup_decompose_mock,
            "_cleanup_merged_branch": cleanup_merged_mock,
            "_has_new_commits": hnc_mock,
            "_worktree_dirty_files": df_mock,
            "_push_branch": push_mock,
            "_head_sha": head_mock,
            "_first_commit_subject": first_subject_mock,
        }


class ParseReviewVerdictTest(unittest.TestCase):
    def test_approved_alone_on_line(self) -> None:
        self.assertEqual(
            _parse_review_verdict("Looks good.\n\nVERDICT: APPROVED"),
            ("approved", "Looks good."),
        )

    def test_changes_requested_with_numbered_list(self) -> None:
        msg = "1. Fix typo in README\n2. Add a test for the empty case\n\nVERDICT: CHANGES_REQUESTED"
        verdict, body = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")
        self.assertIn("1. Fix typo in README", body)
        self.assertNotIn("VERDICT", body)

    def test_inline_marker_is_accepted(self) -> None:
        self.assertEqual(
            _parse_review_verdict("All good. VERDICT: APPROVED"),
            ("approved", "All good."),
        )

    def test_case_insensitive(self) -> None:
        verdict, _ = _parse_review_verdict("verdict: approved")
        self.assertEqual(verdict, "approved")

    def test_last_marker_wins(self) -> None:
        msg = "I considered VERDICT: APPROVED but a test fails.\nVERDICT: CHANGES_REQUESTED"
        verdict, _ = _parse_review_verdict(msg)
        self.assertEqual(verdict, "changes_requested")

    def test_no_marker_returns_unknown(self) -> None:
        self.assertEqual(
            _parse_review_verdict("looks fine to me"),
            ("unknown", "looks fine to me"),
        )

    def test_empty_message_returns_unknown(self) -> None:
        self.assertEqual(_parse_review_verdict(""), ("unknown", ""))


class PushBranchTest(unittest.TestCase):
    """`_push_branch` handles the divergence cases that bit issue-5.

    A self-restart can leave the local worktree on a different SHA than the
    one already pushed (e.g. codex `resume=False` rerun produced equivalent
    work with new committer dates). A plain push then fails non-fast-forward
    and parks the issue. The function uses ls-remote + --force-with-lease so
    the retry succeeds, and the lease still blocks unobserved updates.
    """

    @staticmethod
    def _ok(stdout: str = "", stderr: str = "") -> "object":
        from unittest.mock import MagicMock

        r = MagicMock()
        r.returncode = 0
        r.stdout = stdout
        r.stderr = stderr
        return r

    @staticmethod
    def _fail(stderr: str = "boom") -> "object":
        from unittest.mock import MagicMock

        r = MagicMock()
        r.returncode = 128
        r.stdout = ""
        r.stderr = stderr
        return r

    def _patch(self, run_results: list) -> "tuple":
        from unittest.mock import MagicMock

        run_mock = MagicMock(side_effect=run_results)
        token_patch = patch.object(
            workflow.config, "GITHUB_TOKEN", "ghp-test-secret"
        )
        run_patch = patch.object(workflow.subprocess, "run", run_mock)
        return run_mock, token_patch, run_patch

    def test_existing_remote_branch_force_with_lease_uses_observed_sha(
        self,
    ) -> None:
        # rewrite check (clean), ls-remote (returns sha), push (ok)
        sha = "87b2bc94b03a1729ef8b8145836d0959f433600e"
        ls_stdout = f"{sha}\trefs/heads/orchestrator/issue-5\n"
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=ls_stdout), self._ok()]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertTrue(ok)
        push_cmd = run_mock.call_args_list[2].args[0]
        self.assertIn("push", push_cmd)
        self.assertIn(
            f"--force-with-lease=refs/heads/orchestrator/issue-5:{sha}",
            push_cmd,
        )
        self.assertIn("HEAD:refs/heads/orchestrator/issue-5", push_cmd)

    def test_missing_remote_branch_uses_empty_lease(self) -> None:
        # First push ever for this branch -- ls-remote returns nothing, the
        # lease becomes "expect ref to not exist" so a concurrent create still
        # fails the lease.
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=""), self._ok()]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-9"
            )
        self.assertTrue(ok)
        push_cmd = run_mock.call_args_list[2].args[0]
        self.assertIn(
            "--force-with-lease=refs/heads/orchestrator/issue-9:",
            push_cmd,
        )

    def test_ls_remote_failure_aborts_without_pushing(self) -> None:
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._fail("network down")]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)
        # Only rewrite-check + ls-remote ran; the push subprocess.run was not
        # invoked.
        self.assertEqual(run_mock.call_count, 2)

    def test_push_failure_returns_false(self) -> None:
        ls_stdout = "abc123\trefs/heads/orchestrator/issue-5\n"
        run_mock, token_patch, run_patch = self._patch(
            [self._ok(), self._ok(stdout=ls_stdout), self._fail("rejected")]
        )
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)

    def test_url_rewrite_in_local_config_refuses_push(self) -> None:
        # Local .git/config carrying a url.<host>.insteadOf rewrite is the
        # exfil vector the security hardening guards against; ls-remote and
        # push must never run.
        from unittest.mock import MagicMock

        rewrite_hit = MagicMock()
        rewrite_hit.returncode = 0
        rewrite_hit.stdout = (
            "url.https://evil.example.com/.insteadof https://github.com/\n"
        )
        rewrite_hit.stderr = ""
        run_mock, token_patch, run_patch = self._patch([rewrite_hit])
        with token_patch, run_patch:
            ok = workflow._push_branch(
                _TEST_SPEC, _FAKE_WT, "orchestrator/issue-5"
            )
        self.assertFalse(ok)
        self.assertEqual(run_mock.call_count, 1)


class HandlePickupTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_pickup_with_decompose_off_routes_straight_to_implementing(
        self,
    ) -> None:
        # Legacy path retained behind the DECOMPOSE kill switch: an
        # unlabeled issue still goes straight to implementing without a
        # decomposer round, so operators can disable decomposition without
        # redeploying old binaries.
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message="need clarification"),
                has_new_commits=False,
            )

        self.assertTrue(
            any(":robot: orchestrator picking this up" in body
                for _, body in gh.posted_comments)
        )
        # Pickup flips the label to implementing; downstream handler may park
        # on awaiting_human but does not re-label.
        self.assertEqual(gh.label_history[0], (1, "implementing"))
        self.assertIn("created_at", gh.pinned_data(1))
        # _handle_implementing was actually entered (codex spawned).
        mocks["run_agent"].assert_called_once()


class HandleImplementingFreshRunTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, label="implementing"):
        gh = FakeGitHubClient()
        issue = make_issue(1, label=label)
        gh.add_issue(issue)
        # No prior pinned state; simulate just-after-pickup.
        return gh, issue

    def test_commits_clean_tree_opens_pr_and_flips_label(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="implemented"),
            # First call: not a recovered worktree -> codex runs.
            # Second call: codex produced commits -> push path.
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        self.assertEqual(len(gh.opened_prs), 1)
        opened = gh.opened_prs[0]
        self.assertTrue(any(
            f":sparkles: PR opened: #{opened.number}" in body
            for _, body in gh.posted_comments
        ))
        self.assertIn((1, "validating"), gh.label_history)
        data = gh.pinned_data(1)
        self.assertEqual(data["pr_number"], opened.number)
        self.assertEqual(data["branch"], "orchestrator/issue-1")
        # First fresh dev spawn writes the new keys; the legacy field is
        # deliberately not migrated.
        self.assertEqual(data["dev_agent"], config.DEV_AGENT)
        self.assertEqual(data["dev_session_id"], "sess-1")
        self.assertNotIn("codex_session_id", data)
        self.assertEqual(data["review_round"], 0)

    def test_commits_with_dirty_tree_parks_without_pushing(self) -> None:
        gh, issue = self._seeded()
        dirty = [f"file_{i}.py" for i in range(15)]
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="commit done but more work pending"),
            has_new_commits=[False, True],
            dirty_files=dirty,
            push_branch=True,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("file_0.py", last_comment)
        self.assertIn("file_9.py", last_comment)
        self.assertNotIn("file_10.py", last_comment)
        self.assertIn("… (5 more)", last_comment)

    def test_no_commits_with_message_parks_as_question(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="What database should I use?"),
            has_new_commits=False,
        )

        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("> What database should I use?", last_comment)
        self.assertIn("agent needs your input", last_comment)

    def test_codex_timeout_parks_with_timeout_message(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
            has_new_commits=False,
        )

        mocks["_push_branch"].assert_not_called()
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent timed out", last_comment)
        self.assertEqual(gh.opened_prs, [])

    def test_push_failure_parks_without_opening_pr(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=False,
        )

        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.opened_prs, [])
        self.assertTrue(gh.pinned_data(1).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)


class HandleImplementingAwaitingHumanTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_no_new_comments_returns_without_writing_state(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
        )
        before = gh.write_state_calls

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.write_state_calls, before)
        # Pinned data unchanged.
        self.assertTrue(gh.pinned_data(2).get("awaiting_human"))
        self.assertEqual(gh.pinned_data(2).get("codex_session_id"), "sess-old")

    def test_new_comments_resume_with_session_and_clear_awaiting(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(2, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            2,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            branch="orchestrator/issue-2",
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            # awaiting_human path skips the recovered-worktree probe; only
            # the post-codex commit check runs.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        # Legacy `codex_session_id` locks the resume to the codex backend
        # regardless of the current DEV_AGENT default.
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "sess-old")
        followup_arg = call.args[1]
        self.assertIn("please use sqlite", followup_arg)
        # Ran through to PR open.
        self.assertEqual(len(gh.opened_prs), 1)
        self.assertFalse(gh.pinned_data(2).get("awaiting_human"))


class HandleImplementingRecoveredWorktreeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_recovered_worktree_skips_codex_and_pushes(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(3, label="implementing")
        gh.add_issue(issue)
        gh.seed_state(3, codex_session_id="sess-prev")

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # Prior session id retained.
        self.assertEqual(gh.pinned_data(3).get("codex_session_id"), "sess-prev")


class OnCommitsPRReuseTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_existing_open_pr_is_reused(self) -> None:
        from tests.fakes import FakePR

        gh = FakeGitHubClient()
        issue = make_issue(4, label="implementing")
        gh.add_issue(issue)
        existing = FakePR(number=42, head_branch="orchestrator/issue-4")
        gh.existing_open_pr["orchestrator/issue-4"] = existing

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        # No new PR opened, no sparkles comment posted.
        self.assertEqual(gh.opened_prs, [])
        self.assertFalse(any(":sparkles: PR opened" in body
                             for _, body in gh.posted_comments))
        self.assertIn((4, "validating"), gh.label_history)
        self.assertEqual(gh.pinned_data(4).get("pr_number"), 42)


class ConventionalCommitPromptTest(unittest.TestCase):
    """Both implement and fix prompts must teach the agent the repo's
    Conventional-Commits convention (the prefixes and the `git log` step
    that lets the agent confirm the local style)."""

    def test_implement_prompt_mentions_conventional_commits(self) -> None:
        issue = make_issue(7, title="add a thing", body="please add a thing")
        prompt = workflow._build_implement_prompt(issue, comments_text="")

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        # The exact prefixes from the issue body must be listed so the agent
        # picks one rather than inventing a custom type.
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        # Subject-only commits, no extended body and no Co-Authored-By trailer.
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)

    def test_fix_prompt_mentions_conventional_commits(self) -> None:
        prompt = workflow._build_fix_prompt("please fix the typo")

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)

    def test_pr_comment_followup_mentions_conventional_commits(self) -> None:
        comments = [FakeComment(id=42, body="please rename foo to bar",
                                user=FakeUser("alice"))]
        prompt = workflow._build_pr_comment_followup(comments)

        self.assertIn("git log", prompt)
        self.assertIn("Conventional Commits", prompt)
        for prefix in ("feat:", "fix:", "chore:", "docs:", "refactor:", "test:"):
            self.assertIn(prefix, prompt)
        self.assertIn("subject line only", prompt)
        self.assertIn("Co-Authored-By", prompt)


class ConventionalPrTitleTest(unittest.TestCase, _PatchedWorkflowMixin):
    """`_on_commits` derives the PR title from the agent's first commit
    subject when it already follows the Conventional-Commits convention,
    and falls back to a `<type>: <issue title>` form otherwise."""

    def _seeded(self, *, issue_number: int = 30, label_name: str = "") -> tuple:
        gh = FakeGitHubClient()
        issue = make_issue(
            issue_number,
            label="implementing",
            title="add a sparkly thing",
        )
        if label_name:
            issue.labels.append(FakeLabel(label_name))
        gh.add_issue(issue)
        return gh, issue

    def test_pr_title_uses_conventional_first_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=30)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="feat: add a sparkly thing",
        )

        self.assertEqual(len(gh.opened_prs), 1)
        pr = gh.opened_prs[0]
        # First-commit subject is preserved verbatim, no extra prefix.
        self.assertEqual(pr.title, "feat: add a sparkly thing")
        # Traceability still in body.
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_pr_title_uses_scoped_conventional_first_commit_subject(self) -> None:
        gh, issue = self._seeded(issue_number=31)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            # Conventional Commits also allow `<type>(<scope>): ...` and
            # `<type>!:` for breaking changes; both must be accepted.
            first_commit_subject="fix(api)!: drop legacy endpoint",
        )

        self.assertEqual(gh.opened_prs[0].title, "fix(api)!: drop legacy endpoint")

    def test_pr_title_falls_back_to_feat_for_unconventional_commit(self) -> None:
        gh, issue = self._seeded(issue_number=32)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="updated stuff",
        )

        pr = gh.opened_prs[0]
        # Fallback uses `feat:` (no bug label) and the issue title.
        self.assertEqual(pr.title, "feat: add a sparkly thing")
        self.assertIn(f"Resolves #{issue.number}", pr.body)

    def test_pr_title_falls_back_to_fix_for_bug_labelled_issue(self) -> None:
        gh, issue = self._seeded(issue_number=33, label_name="bug")

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="fixed it",
        )

        # Bug label tips the fallback to `fix:`.
        self.assertEqual(gh.opened_prs[0].title, "fix: add a sparkly thing")

    def test_pr_title_fallback_when_no_commit_subject_available(self) -> None:
        gh, issue = self._seeded(issue_number=34)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="",
        )

        self.assertEqual(gh.opened_prs[0].title, "feat: add a sparkly thing")

    def test_pr_title_uses_conventional_issue_title_in_fallback(self) -> None:
        # Issue title already conventional -> use it directly so we don't
        # produce a doubled `feat: feat: ...` form.
        gh = FakeGitHubClient()
        issue = make_issue(
            35, label="implementing", title="docs: clarify the README"
        )
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
            first_commit_subject="some unconventional commit",
        )

        self.assertEqual(gh.opened_prs[0].title, "docs: clarify the README")


class ConventionalSubjectHelperTest(unittest.TestCase):
    """Direct coverage for the regex helper, since the convention list grew
    beyond what the prompts spell out."""

    def test_accepts_basic_types(self) -> None:
        for subject in (
            "feat: add thing",
            "fix: bug",
            "chore: bump dep",
            "docs: tweak",
            "refactor: rename foo",
            "test: cover edge case",
            "perf: speed it up",
            "ci: fix workflow",
        ):
            self.assertTrue(
                workflow._is_conventional_subject(subject),
                f"expected conventional: {subject!r}",
            )

    def test_accepts_scope_and_breaking(self) -> None:
        self.assertTrue(workflow._is_conventional_subject("feat(api): foo"))
        self.assertTrue(workflow._is_conventional_subject("fix!: bar"))
        self.assertTrue(workflow._is_conventional_subject("feat(api)!: baz"))

    def test_rejects_non_conventional(self) -> None:
        for subject in (
            "",
            "Add a thing",
            "wip: thing",
            "feat:",            # no subject after colon
            "feat:   ",         # whitespace-only subject
            "Feat: cap type",   # types must be lowercase
            "  feat: leading", # leading whitespace not accepted
        ):
            self.assertFalse(
                workflow._is_conventional_subject(subject),
                f"expected non-conventional: {subject!r}",
            )


class HandleValidatingFreshReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=11,
            branch="orchestrator/issue-5",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(5, **defaults)
        return gh, issue

    def test_approved_flips_label_and_does_not_resume(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn((5, "in_review"), gh.label_history)
        self.assertTrue(any(
            ":white_check_mark: codex review approved" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_changes_requested_resumes_dev_increments_round(self) -> None:
        gh, issue = self._seeded()
        review = _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            # 1: reviewed_sha snapshot before run_agent. 2: before_sha for the
            # dev-fix run. 3: after_sha to confirm the new commit.
            head_shas=["aaa", "aaa", "bbb"],
        )

        self.assertEqual(mocks["run_agent"].call_count, 2)
        # Second call (dev fix) must resume the developer session.
        _, second_kwargs = mocks["run_agent"].call_args_list[1]
        self.assertEqual(second_kwargs.get("resume_session_id"), "dev-sess")

        self.assertTrue(any(
            ":eyes: codex review (round 1/" in body and "Fix typo" in body
            for _, body in gh.posted_pr_comments
        ))
        mocks["_push_branch"].assert_called_once()
        self.assertEqual(gh.pinned_data(5).get("review_round"), 1)
        # Label NOT flipped to in_review here -- next tick re-reviews.
        self.assertNotIn((5, "in_review"), gh.label_history)

    def test_unknown_verdict_parks_with_quoted_message(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="I'm not sure what to think"),
        )

        self.assertTrue(gh.pinned_data(5).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("did not emit a VERDICT line", last_comment)
        self.assertIn("> I'm not sure what to think", last_comment)
        # Label stays validating: no in_review transition.
        self.assertNotIn((5, "in_review"), gh.label_history)

    def test_reviewer_timeout_parks(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(timed_out=True),
        )

        self.assertTrue(gh.pinned_data(5).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("reviewer timed out", last_comment)
        self.assertNotIn((5, "in_review"), gh.label_history)


class HandleValidatingFixLoopEdgeCasesTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(6, label="validating")
        gh.add_issue(issue)
        defaults = dict(
            pr_number=12,
            branch="orchestrator/issue-6",
            codex_session_id="dev-sess",
            review_round=0,
        )
        defaults.update(state)
        gh.seed_state(6, **defaults)
        return gh, issue

    def _changes_requested_review(self):
        return _agent(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )

    def test_dev_fix_no_new_commit_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="why?"),
            ],
            dirty_files=(),
            push_branch=True,
            # reviewed_sha + before_sha + after_sha (all "aaa" -> no commit).
            head_shas=["aaa", "aaa", "aaa"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent needs your input", last_comment)

    def test_dev_fix_dirty_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="partial"),
            ],
            dirty_files=["leftover.py"],
            push_branch=True,
            head_shas=["aaa", "aaa", "bbb"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("uncommitted change", last_comment)
        self.assertIn("leftover.py", last_comment)

    def test_dev_fix_push_fail_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=[
                self._changes_requested_review(),
                _agent(session_id="dev-sess", last_message="fixed"),
            ],
            dirty_files=(),
            push_branch=False,
            head_shas=["aaa", "aaa", "bbb"],
        )

        data = gh.pinned_data(6)
        self.assertEqual(data.get("review_round"), 0)
        self.assertTrue(data.get("awaiting_human"))
        # The transient `push_failed` tag is what lets the next tick's
        # recovery branch silently retry the push without needing a human
        # comment to unstick the issue.
        self.assertEqual(data.get("park_reason"), "push_failed")
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)

    def test_review_round_at_cap_parks_without_spawning_reviewer(self) -> None:
        gh, issue = self._seeded(review_round=config.MAX_REVIEW_ROUNDS)
        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("review still has comments", last_comment)


class HandleValidatingAwaitingHumanResumeTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_human_reply_resumes_dev_bumps_round_no_reviewer_this_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(7, label="validating")
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite please", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            7,
            awaiting_human=True,
            last_action_comment_id=950,
            codex_session_id="dev-sess",
            review_round=1,
            pr_number=13,
            branch="orchestrator/issue-7",
        )

        mocks = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Only the dev resume runs this tick; the reviewer fires on the next.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "codex")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        followup = call.args[1]
        self.assertIn("use sqlite please", followup)

        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(7)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 2)
        self.assertNotIn((7, "in_review"), gh.label_history)


class HandleImplementingRetryCapTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Bound the implementing loop with MAX_RETRIES_PER_DAY in pinned state.

    Resumes on human reply and recovered-worktree pushes are explicitly NOT
    counted; only fresh codex spawns consume the budget.
    """

    def _seeded(self, **state):
        gh = FakeGitHubClient()
        issue = make_issue(8, label="implementing")
        gh.add_issue(issue)
        if state:
            gh.seed_state(8, **state)
        return gh, issue

    def test_fourth_fresh_attempt_in_window_is_parked_before_codex(self) -> None:
        # Run three fresh attempts that each park as a question, then assert
        # the fourth tick parks before run_agent is called. Cap is 3/day.
        gh, issue = self._seeded()

        # First three ticks: codex returns no commits + a question, parking on
        # awaiting_human. Each tick consumes one retry from the budget.
        for tick in range(3):
            self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(last_message=f"q{tick}"),
                has_new_commits=False,
            )
            # Clear the awaiting-human flag manually so the next tick takes
            # the fresh-spawn branch again (simulating that the human answered
            # but the agent still failed to commit). We do NOT update
            # last_action_comment_id, but we also drop awaiting_human so the
            # else branch runs.
            data = gh._pinned[8].data
            data["awaiting_human"] = False

        self.assertEqual(gh.pinned_data(8).get("retry_count"), 3)
        self.assertIsNotNone(gh.pinned_data(8).get("retry_window_start"))

        # Fourth tick: must park before codex spawns.
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="should not run"),
            has_new_commits=False,
        )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(8).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("hit retry cap (3/day)", last_comment)
        self.assertIn("Window opened at", last_comment)

    def test_successful_on_commits_clears_retry_counter(self) -> None:
        # Pre-seed near-cap state, then run a successful tick (commits + clean
        # tree + push succeeds). The PR-open path must clear the budget.
        gh, issue = self._seeded(
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-1", last_message="done"),
            has_new_commits=[False, True],
            dirty_files=(),
            push_branch=True,
        )

        data = gh.pinned_data(8)
        self.assertEqual(data.get("retry_count"), 0)
        # window_start cleared back to falsy.
        self.assertFalse(data.get("retry_window_start"))
        self.assertEqual(len(gh.opened_prs), 1)

    def test_window_older_than_24h_resets_counter(self) -> None:
        # Cap exhausted but the window is 25h old: next fresh attempt opens a
        # new window with count=1 and codex actually spawns.
        gh, issue = self._seeded(
            retry_count=3,
            retry_window_start=_iso_hours_ago(25),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="ask again"),
            has_new_commits=False,
        )

        mocks["run_agent"].assert_called_once()
        data = gh.pinned_data(8)
        # Reset to 0 by the window-expired branch, then incremented to 1.
        self.assertEqual(data.get("retry_count"), 1)
        # Park message must NOT be the cap message.
        last_comment = gh.posted_comments[-1][1]
        self.assertNotIn("hit retry cap", last_comment)

    def test_awaiting_human_resume_does_not_increment_counter(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(9, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="please use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            9,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-old",
            retry_count=2,
            retry_window_start=_iso_hours_ago(1),
        )

        mocks = self._run(
            lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="sess-old", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # Resume happened (codex was called once with the followup comment).
        mocks["run_agent"].assert_called_once()
        # retry_count NOT incremented by the resume itself. The successful
        # _on_commits then clears it to 0.
        data = gh.pinned_data(9)
        self.assertEqual(data.get("retry_count"), 0)


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


class ConfigurableBackendTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The dev/review backends are picked from config, with the dev backend
    locked to whatever wrote `dev_session_id` (or legacy `codex_session_id`)
    so a config flip mid-flight does not break a resumable session.
    """

    def test_fresh_implementing_spawn_uses_dev_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="implementing")
        gh.add_issue(issue)

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-fresh", last_message="done"),
                has_new_commits=[False, True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "claude")
        data = gh.pinned_data(20)
        self.assertEqual(data["dev_agent"], "claude")
        self.assertEqual(data["dev_session_id"], "sess-fresh")
        self.assertNotIn("codex_session_id", data)

    def test_reviewer_spawn_uses_review_agent_config(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(21, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pr_number=21,
            branch="orchestrator/issue-21",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
        )

        with patch.object(config, "REVIEW_AGENT", "codex"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="rev-sess",
                    last_message="LGTM\n\nVERDICT: APPROVED",
                ),
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        data = gh.pinned_data(21)
        self.assertEqual(data["review_agent"], "codex")
        self.assertEqual(data["last_review_session_id"], "rev-sess")

    def test_dev_fix_uses_recorded_dev_backend_not_current_config(self) -> None:
        # Issue locked to codex via pinned state; even if config flips to
        # claude, the validating dev-fix call must stay on codex.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pr_number=22,
            branch="orchestrator/issue-22",
            dev_agent="codex",
            dev_session_id="dev-sess",
            review_round=0,
        )
        review = _agent(
            session_id="rev-sess",
            last_message="1. Tighten\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _agent(session_id="dev-sess", last_message="fixed")

        with patch.object(config, "DEV_AGENT", "claude"), \
             patch.object(config, "REVIEW_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=[review, dev_fix],
                dirty_files=(),
                push_branch=True,
                head_shas=["aaa", "aaa", "bbb"],
            )

        # Reviewer takes config; dev-fix takes pinned state.
        self.assertEqual(mocks["run_agent"].call_count, 2)
        self.assertEqual(mocks["run_agent"].call_args_list[0].args[0], "claude")
        self.assertEqual(mocks["run_agent"].call_args_list[1].args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args_list[1].kwargs.get("resume_session_id"),
            "dev-sess",
        )

    def test_legacy_codex_session_id_resumes_with_codex(self) -> None:
        # Pinned state predates the rollout: only `codex_session_id`. Resume
        # on human reply must stick with codex even when DEV_AGENT=claude.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="implementing")
        issue.comments.append(
            FakeComment(id=1100, body="use sqlite", user=FakeUser("alice"))
        )
        gh.add_issue(issue)
        gh.seed_state(
            23,
            awaiting_human=True,
            last_action_comment_id=900,
            codex_session_id="sess-legacy",
            branch="orchestrator/issue-23",
        )

        with patch.object(config, "DEV_AGENT", "claude"):
            mocks = self._run(
                lambda: workflow._handle_implementing(gh, _TEST_SPEC, issue),
                run_agent=_agent(session_id="sess-legacy", last_message="ok"),
                has_new_commits=[True],
                dirty_files=(),
                push_branch=True,
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "codex")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "sess-legacy",
        )
        # No proactive migration: legacy key stays put, no new keys written
        # by a resume (only fresh spawns write `dev_agent`/`dev_session_id`).
        data = gh.pinned_data(23)
        self.assertEqual(data.get("codex_session_id"), "sess-legacy")
        self.assertNotIn("dev_agent", data)
        self.assertNotIn("dev_session_id", data)


class HandleInReviewTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Drive the in_review handler through merged / closed-not-merged /
    open-PR (auto-merge gates and PR-comment debounce) branches against a
    seeded FakePR.
    """

    PR_NUMBER = 77
    BRANCH = "orchestrator/issue-30"

    def _seed(
        self,
        *,
        issue_number: int = 30,
        pr=None,
        with_pr_number: bool = True,
        extra_state=None,
    ):
        gh = FakeGitHubClient()
        issue = make_issue(issue_number, label="in_review")
        gh.add_issue(issue)
        if pr is not None:
            gh.add_pr(pr)
        state: dict = {
            "branch": self.BRANCH,
            "dev_agent": "claude",
            "dev_session_id": "dev-sess",
            "review_round": 1,
        }
        if with_pr_number and pr is not None:
            state["pr_number"] = pr.number
        if extra_state:
            state.update(extra_state)
        gh.seed_state(issue_number, **state)
        return gh, issue

    def _open_pr(self, **kwargs):
        defaults = dict(
            number=self.PR_NUMBER,
            head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
        )
        defaults.update(kwargs)
        return FakePR(**defaults)

    def test_in_review_pr_merged_externally(self) -> None:
        pr = self._open_pr(merged=True, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # Branch cleanup must fire for an external merge: the PR is gone, so
        # the per-issue worktree and the local + remote branches are dead
        # weight that should not survive past the `done` flip.
        mocks["_cleanup_merged_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
        )

    def test_in_review_pr_closed_unmerged(self) -> None:
        pr = self._open_pr(merged=False, state="closed")
        gh, issue = self._seed(pr=pr)

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((30, "rejected"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        self.assertEqual(gh.merge_calls, [])
        # Closed-without-merge is `rejected`, not `done`. The branch may
        # still be useful for reopening the PR or salvaging work, so we
        # leave it alone -- cleanup is gated to the merged paths.
        mocks["_cleanup_merged_branch"].assert_not_called()

    def test_in_review_pr_open_no_comments_no_auto_merge(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", False):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Pure no-op: no agent run, no merge, no label flip, no comment.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(issue.closed)

    def test_in_review_auto_merge_happy_path(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertIn((30, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(30))
        self.assertTrue(issue.closed)
        mocks["_cleanup_merged_branch"].assert_called_once_with(
            gh, _TEST_SPEC, 30,
        )

    def test_in_review_auto_merge_blocked_on_pending_checks(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="pending")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_auto_merge_blocked_on_no_approval(self) -> None:
        pr = self._open_pr(approved=False, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertNotIn("merged_at", gh.pinned_data(30))

    def test_in_review_auto_merge_blocked_on_failed_checks(self) -> None:
        pr = self._open_pr(approved=True, mergeable=True, check_state="failure")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("checks are 'failure'", last_comment)
        self.assertIn(f"PR #{self.PR_NUMBER}", last_comment)

    def test_in_review_auto_merge_blocked_on_unmergeable(self) -> None:
        pr = self._open_pr(approved=True, mergeable=False, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("not mergeable", last_comment)

    def test_in_review_auto_merge_mergeable_pending(self) -> None:
        # mergeable=None means GitHub is still computing. Don't merge, don't
        # park; the next tick re-checks once GitHub has decided.
        pr = self._open_pr(approved=True, mergeable=None, check_state="success")
        gh, issue = self._seed(pr=pr)

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertEqual(gh.posted_comments, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))

    def test_in_review_pr_comment_within_debounce(self) -> None:
        # A PR comment posted just now must NOT trigger a dev resume; the
        # human may still be typing more comments.
        now = datetime.now(timezone.utc)
        pr = self._open_pr(
            approved=True, mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=2000, body="please tighten the docstring",
                    user=FakeUser("alice"), created_at=now,
                ),
            ],
        )
        # Watermark just below the comment so it surfaces as fresh feedback.
        # An unset watermark would trip the legacy in_review migration and
        # mask this comment as already-consumed.
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Within debounce: no agent spawn, no merge, no label flip.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])

    def test_in_review_pr_comment_past_debounce(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = self._open_pr(
            issue_comments=[
                FakeComment(
                    id=2000, body="rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh, issue = self._seed(
            pr=pr, extra_state={"pr_last_comment_id": 1999}
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="renamed"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Dev resumed on the locked backend with the PR-comment text quoted
        # into the prompt; pushed; bounced back to validating with round=0.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "claude")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dev-sess")
        self.assertIn("rename foo to bar", call.args[1])

        mocks["_push_branch"].assert_called_once()
        self.assertIn((30, "validating"), gh.label_history)
        data = gh.pinned_data(30)
        self.assertEqual(data.get("review_round"), 0)
        self.assertEqual(data.get("pr_last_comment_id"), 2000)

    def test_in_review_sha_mismatch_on_merge(self) -> None:
        # merge_pr returning False (409 SHA mismatch / 405 / 422) leaves the
        # issue in_review for the next tick to retry; no park, no label flip.
        pr = self._open_pr(approved=True, mergeable=True, check_state="success")
        gh, issue = self._seed(pr=pr)
        gh.merge_returns_ok = False

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))
        self.assertNotIn("merged_at", gh.pinned_data(30))
        self.assertFalse(issue.closed)

    def test_in_review_pr_number_missing(self) -> None:
        # Manually-relabeled in_review without a pinned PR -- park once.
        gh, issue = self._seed(pr=None, with_pr_number=False)

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertTrue(gh.pinned_data(30).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("without a pinned `pr_number`", last_comment)

        # A second tick with awaiting_human set must NOT re-park (no second
        # comment posted; comment count stays at 1).
        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )
        self.assertEqual(len(gh.posted_comments), before)

    def test_in_review_agent_approval_unlocks_auto_merge(self) -> None:
        # The reviewer agent posts an issue comment, not a real PR review,
        # so pr_is_approved (which inspects pr.get_reviews()) is False even
        # after the agent emits VERDICT: APPROVED. The validating handler
        # persists `agent_approved_sha` for the head it reviewed; that key
        # is what the in_review auto-merge gate keys on.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            head=FakePRRef(sha="cafe1234"),
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={"agent_approved_sha": "cafe1234"},
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")])
        self.assertIn((30, "done"), gh.label_history)

    def test_in_review_stale_agent_approval_blocks_auto_merge(self) -> None:
        # If the head moved after the agent approved (e.g., a human force-
        # pushed) the snapshot SHA no longer matches and pr_is_approved is
        # also False -- nothing auto-merges. We don't park here either; the
        # next event (new comment / close / re-approval bouncing back
        # through validating) is what unsticks us.
        pr = self._open_pr(
            approved=False, mergeable=True, check_state="success",
            head=FakePRRef(sha="newhead99"),
        )
        gh, issue = self._seed(
            pr=pr,
            extra_state={"agent_approved_sha": "cafe1234"},
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(30).get("awaiting_human"))


class ValidatingToInReviewHandoffTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The validating -> in_review handoff has to seed two pinned-state keys
    so `_handle_in_review` behaves correctly on the next tick:

    * `agent_approved_sha` — the head SHA the reviewer agent OK'd. Without
      this, AUTO_MERGE never fires for the agent-driven flow because the
      agent posts an issue comment rather than a real PR review, so
      `pr_is_approved` returns False.
    * `pr_last_comment_id` — high-watermark seeded past every comment that
      already exists at handoff. Without this, the in_review handler sees
      the orchestrator's own ":robot: picking this up", ":sparkles: PR
      opened: #N", and ":white_check_mark: codex review approved" comments
      as fresh PR feedback once the debounce expires and resumes the dev
      session against them.
    """

    PR_NUMBER = 11
    BRANCH = "orchestrator/issue-5"

    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(5, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #11",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="newhead42"),
        )
        gh.add_pr(pr)
        gh.seed_state(
            5,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=0,
            # Pre-existing orchestrator comments are recognized by exact id,
            # not author login -- mirror what `_handle_pickup` / `_on_commits`
            # would have recorded as they posted these comments.
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_approved_seeds_agent_approved_sha_and_watermark(self) -> None:
        gh, issue, pr = self._setup()

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            # Local worktree HEAD == pr.head.sha; reviewed_sha snapshot
            # (the only _head_sha call on the approved path) returns it
            # so agent_approved_sha is persisted.
            head_shas=("newhead42",),
        )

        self.assertIn((5, "in_review"), gh.label_history)
        data = gh.pinned_data(5)
        self.assertEqual(data.get("agent_approved_sha"), "newhead42")
        # Watermark must be at least past the existing orchestrator
        # comments AND the approval comment validating just posted (which
        # FakeGitHubClient.pr_comment now appends to pr.issue_comments).
        approval_ids = [c.id for c in pr.issue_comments]
        self.assertTrue(approval_ids, "approval comment should be on PR")
        self.assertEqual(data.get("pr_last_comment_id"), max(approval_ids))
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 901)

    def test_in_review_after_approval_does_not_replay_existing_comments(self) -> None:
        # End-to-end: validating approves -> in_review tick auto-merges
        # without resuming the dev on the orchestrator's own automated
        # comments. This is the concrete bug guarded by both fixes
        # (watermark seeding + agent_approved_sha gate) acting together.
        gh, issue, pr = self._setup()

        # Step 1: validating approves. This posts a PR comment, seeds the
        # watermark and agent_approved_sha, and flips to in_review.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Backdate every existing comment so debounce would otherwise fire.
        for c in list(issue.comments) + list(pr.issue_comments):
            c.created_at = long_ago

        mocks_v = self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("newhead42",),
        )
        self.assertEqual(mocks_v["run_agent"].call_count, 1)

        # Backdate the approval comment that pr_comment just appended too,
        # so it would falsely fire the debounce-resume path if the
        # watermark were not seeded.
        for c in list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago

        # Step 2: relabel issue (FakeGitHubClient does this in step 1).
        # Step 3: pretend approved + green checks + mergeable so the
        # auto-merge gate is the thing under test.
        pr.approved = False  # only agent approved; no human review
        pr.mergeable = True
        pr.check_state = "success"
        # Re-label to in_review explicitly (set_workflow_label already did
        # this in step 1, but be defensive).
        from tests.fakes import FakeLabel
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks_r = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Critical assertion: NO dev resume on stale orchestrator comments.
        mocks_r["run_agent"].assert_not_called()
        # And the auto-merge unlocked because agent_approved_sha matches.
        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "newhead42", "squash")]
        )
        self.assertIn((5, "done"), gh.label_history)

    def test_second_handoff_ratchets_watermark(self) -> None:
        # An earlier in_review tick consumed a human PR comment (id 2000)
        # and bounced back to validating. The dev fixed it; the reviewer
        # approves again. _seed_watermark_past_self stops at the first
        # post-pickup human comment so its recomputed seed is BELOW the
        # already-stored watermark. Without max(), pr_last_comment_id
        # would regress and the next in_review tick would replay the same
        # already-fixed feedback as "new", looping forever.
        gh = FakeGitHubClient()
        issue = make_issue(99, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #50",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=50, head_branch="orchestrator/issue-99",
            head=FakePRRef(sha="cafe9999"),
            issue_comments=[
                FakeComment(
                    id=2000, body="rename foo to bar",
                    user=FakeUser("alice"),
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            99,
            pr_number=50,
            branch="orchestrator/issue-99",
            dev_agent="claude",
            dev_session_id="dev-sess",
            review_round=1,
            pr_last_comment_id=2000,
            pr_last_review_comment_id=4242,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )

        self.assertIn((99, "in_review"), gh.label_history)
        data = gh.pinned_data(99)
        wm = data.get("pr_last_comment_id")
        self.assertGreaterEqual(
            wm, 2000,
            f"watermark must not regress past consumed PR feedback (got {wm})",
        )
        self.assertEqual(data.get("pr_last_review_comment_id"), 4242)


class ListPollableIssuesTest(unittest.TestCase):
    """Closed-but-`in_review` issues must still be picked up so external
    manual merges (which auto-close the linked issue via "Resolves #N") get
    finalized to `done` instead of being silently dropped."""

    def test_open_only_when_no_in_review_closed(self) -> None:
        gh = FakeGitHubClient()
        gh.add_issue(make_issue(1, label="implementing"))
        gh.add_issue(make_issue(2, label="validating"))
        out = list(gh.list_pollable_issues())
        self.assertEqual({i.number for i in out}, {1, 2})

    def test_includes_closed_in_review_for_external_merge_finalization(self) -> None:
        gh = FakeGitHubClient()
        open_issue = make_issue(1, label="implementing")
        closed_in_review = make_issue(7, label="in_review")
        closed_in_review.closed = True
        # Closed but no in_review label: must be skipped (already finalized).
        closed_done = make_issue(8, label="done")
        closed_done.closed = True
        for i in (open_issue, closed_in_review, closed_done):
            gh.add_issue(i)
        out = {i.number for i in gh.list_pollable_issues()}
        self.assertEqual(out, {1, 7})


class HandleInReviewClosedIssueExternalMergeTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human merge with `Resolves #N` auto-closes issue N before the
    orchestrator ticks. The closed-in_review sweep yields the issue and
    `_handle_in_review` must still flip the label to `done` and stamp
    `merged_at` -- otherwise the issue stays closed-but-`in_review` forever.
    """

    def test_external_merge_on_closed_issue_finalizes_to_done(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(40, label="in_review")
        issue.closed = True  # Resolves #N has already auto-closed it.
        gh.add_issue(issue)
        pr = FakePR(
            number=99, head_branch="orchestrator/issue-40",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(40, pr_number=99, branch="orchestrator/issue-40")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((40, "done"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(40))


class StaleHumanApprovalAutoMergeTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human APPROVED review on an older head must NOT unlock auto-merge
    when a newer commit was pushed without re-approval. Otherwise a
    contributor could push code AFTER the human approval and have the
    orchestrator merge it unreviewed.
    """

    def test_stale_human_approval_blocks_auto_merge(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(50, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=88, head_branch="orchestrator/issue-50",
            head=FakePRRef(sha="newhead"),
            approved=True,                  # human approved
            approval_head_sha="oldhead",    # ...but on the previous commit
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(50, pr_number=88, branch="orchestrator/issue-50")

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No merge: stale approval is treated as missing.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(50).get("awaiting_human"))

    def test_current_head_human_approval_allows_auto_merge(self) -> None:
        # Same setup but approval IS for the current head -- merge proceeds.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=89, head_branch="orchestrator/issue-51",
            head=FakePRRef(sha="newhead"),
            approved=True, approval_head_sha="newhead",
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(51, pr_number=89, branch="orchestrator/issue-51")

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(89, "newhead", "squash")])
        self.assertIn((51, "done"), gh.label_history)


class InReviewParkWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A park inside `_handle_in_review` posts an issue comment. The watermark
    must be bumped past that comment so the next tick does not see the
    orchestrator's own HITL ping as fresh PR feedback and resume the dev
    agent against it.
    """

    def _setup_failed_checks(self):
        gh = FakeGitHubClient()
        issue = make_issue(60, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=70, head_branch="orchestrator/issue-60",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=True, check_state="failure",
        )
        gh.add_pr(pr)
        gh.seed_state(
            60, pr_number=70, branch="orchestrator/issue-60",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,  # an old watermark from validating handoff
        )
        return gh, issue

    def test_failed_checks_park_does_not_replay_on_next_tick(self) -> None:
        gh, issue = self._setup_failed_checks()

        with patch.object(config, "AUTO_MERGE", True):
            # Tick 1: fail-checks park.
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        self.assertTrue(gh.pinned_data(60).get("awaiting_human"))
        comments_after_park = len(gh.posted_comments)
        self.assertGreater(comments_after_park, 0)
        # Watermark must have been bumped past the park comment -- which
        # means it's at or above the latest comment id on the issue.
        latest_id = gh.latest_comment_id(issue)
        self.assertEqual(gh.pinned_data(60).get("pr_last_comment_id"), latest_id)

        with patch.object(config, "AUTO_MERGE", True):
            # Tick 2: nothing new; must NOT resume the dev agent.
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        mocks["run_agent"].assert_not_called()
        # No additional comments posted (no second park, no dev-resume ping).
        self.assertEqual(len(gh.posted_comments), comments_after_park)

    def test_unmergeable_park_does_not_replay_on_next_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(61, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=71, head_branch="orchestrator/issue-61",
            head=FakePRRef(sha="cafe1234"),
            approved=True, approval_head_sha="cafe1234",
            mergeable=False, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            61, pr_number=71, branch="orchestrator/issue-61",
            dev_agent="claude", dev_session_id="dev-sess",
            pr_last_comment_id=900,
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        self.assertTrue(gh.pinned_data(61).get("awaiting_human"))
        latest_id = gh.latest_comment_id(issue)
        self.assertEqual(gh.pinned_data(61).get("pr_last_comment_id"), latest_id)

        with patch.object(config, "AUTO_MERGE", True):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        mocks["run_agent"].assert_not_called()


class InReviewSplitWatermarkTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Issue comments and PR inline review comments live in different id
    namespaces in GitHub's REST API. The handler tracks them with two
    independent watermarks so a high id on one side cannot eclipse newer
    comments on the other.
    """

    BRANCH = "orchestrator/issue-65"
    PR_NUMBER = 95

    def _setup(self, *, issue_comments=(), review_comments=(), state_extra=None):
        gh = FakeGitHubClient()
        issue = make_issue(65, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            issue_comments=list(issue_comments),
            review_comments=list(review_comments),
        )
        gh.add_pr(pr)
        state = dict(
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        if state_extra:
            state.update(state_extra)
        gh.seed_state(65, **state)
        return gh, issue, pr

    def test_inline_review_comment_triggers_resume(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=42, body="line 12: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Inline-review watermark just below the comment id so it
            # surfaces as fresh feedback. An unset watermark would trip the
            # legacy in_review migration and treat id=42 as already-consumed.
            state_extra={"pr_last_review_comment_id": 41},
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="renamed"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn("rename foo to bar", mocks["run_agent"].call_args.args[1])
        self.assertIn((65, "validating"), gh.label_history)
        data = gh.pinned_data(65)
        self.assertEqual(data.get("pr_last_review_comment_id"), 42)
        # Issue-comment watermark stays at the legacy-migration default (0)
        # because no issue-side comment was consumed -- the two id spaces
        # ratchet independently. The migration always persists 0 instead of
        # leaving the watermark unset, so the next tick does not re-run the
        # migration past any newly-arrived first comment.
        self.assertEqual(data.get("pr_last_comment_id"), 0)

    def test_id_overlap_across_spaces_does_not_drop_comments(self) -> None:
        # Inline review comment id (5) is LOWER than the issue-comment
        # watermark (1000). With one merged-id watermark this comment would
        # be silently filtered out; with split watermarks it gets through.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup(
            review_comments=[
                FakeComment(
                    id=5, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            # Issue-side watermark high (1000), inline-review watermark low (4)
            # -- the two ratchet independently, and id=5 must still surface.
            state_extra={
                "pr_last_comment_id": 1000,
                "pr_last_review_comment_id": 4,
            },
        )

        mocks = self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dev-sess", last_message="added"),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # The inline comment is consumed even though id=5 < pr_last_comment_id=1000.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn("please add a docstring", mocks["run_agent"].call_args.args[1])
        self.assertEqual(gh.pinned_data(65).get("pr_last_review_comment_id"), 5)


class HumanChangesRequestedVetoTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human CHANGES_REQUESTED review on the PR's current head must veto
    auto-merge regardless of how the reviewer agent voted. Without the veto,
    the `agent_approved_sha == head_sha` short-circuit would let the
    orchestrator merge over a standing human objection on the same SHA.
    """

    def test_changes_requested_blocks_auto_merge_even_when_agent_approved(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(80, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=120, head_branch="orchestrator/issue-80",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            changes_requested=True,  # human vetoed the current head
        )
        gh.add_pr(pr)
        gh.seed_state(
            80, pr_number=120, branch="orchestrator/issue-80",
            agent_approved_sha="cafe1234",  # agent approved same head
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Veto wins over agent approval; no merge, no label flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        self.assertFalse(gh.pinned_data(80).get("awaiting_human"))

    def test_changes_requested_blocks_auto_merge_even_with_human_approval(self) -> None:
        # APPROVED + CHANGES_REQUESTED on the same head: GitHub considers
        # the PR not approved. pr_is_approved already filters this out, but
        # the orthogonal veto check is what guarantees the agent path can't
        # bypass it via agent_approved_sha.
        gh = FakeGitHubClient()
        issue = make_issue(81, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=121, head_branch="orchestrator/issue-81",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            approved=True, approval_head_sha="cafe1234",
            changes_requested=True,
        )
        gh.add_pr(pr)
        gh.seed_state(
            81, pr_number=121, branch="orchestrator/issue-81",
            agent_approved_sha="cafe1234",
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])

    def test_stale_changes_requested_does_not_block(self) -> None:
        # CHANGES_REQUESTED on an OLD head (force-pushed past) must not
        # block auto-merge: a stale veto on a no-longer-current SHA is
        # equivalent to no veto. Mirrors the stale-approval gating.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=122, head_branch="orchestrator/issue-82",
            head=FakePRRef(sha="newhead"),
            mergeable=True, check_state="success",
            changes_requested=True, changes_requested_head_sha="oldhead",
        )
        gh.add_pr(pr)
        gh.seed_state(
            82, pr_number=122, branch="orchestrator/issue-82",
            agent_approved_sha="newhead",
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [(122, "newhead", "squash")])
        self.assertIn((82, "done"), gh.label_history)


class ValidatingHandoffPreservesHumanFeedbackTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A human review comment posted while validating is still running must
    not be silently consumed when the validating handler approves and seeds
    the in_review watermarks. Otherwise auto-merge fires without the dev
    agent ever seeing the human's feedback.
    """

    PR_NUMBER = 22
    BRANCH = "orchestrator/issue-15"

    def _setup(self):
        gh = FakeGitHubClient()
        issue = make_issue(15, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #22",
                user=FakeUser("orchestrator"),
            ),
        ])
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            # Human posted a review comment during validating, BEFORE the
            # orchestrator's approval comment lands. Without the watermark
            # fix, the validating handler would seed pr_last_comment_id past
            # this comment and the next in_review tick would never see it.
            issue_comments=[
                FakeComment(
                    id=950, body="please add a docstring",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            15, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_pre_handoff_human_pr_comment_is_processed_in_in_review(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. The orchestrator's approval comment
        # lands AFTER the human's. With the fix, the watermark stops at
        # the first human comment instead of swallowing it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        self.assertIn((15, "in_review"), gh.label_history)
        wm = gh.pinned_data(15).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must stop before human comment id=950 (got {wm})",
        )

        # Step 2: in_review tick. With the fix, the human comment is visible
        # past the watermark, gets surfaced to the dev agent, and the issue
        # bounces back to validating. Without it, the auto-merge gate would
        # fire on the agent's approval and merge over the human's feedback.
        from tests.fakes import FakeLabel
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docstring added"
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev agent was resumed on the human's comment text.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please add a docstring",
            mocks["run_agent"].call_args.args[1],
        )
        # No merge happened; issue bounced back to validating.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((15, "validating"), gh.label_history)


class PrePickupChatterHandoffTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Pre-pickup human comments on the issue (the original discussion that
    landed in the dev agent's spawn context) must be advanced past at
    validating -> in_review handoff. If the watermark stops at the first
    non-self comment, those same already-consumed comments replay as fresh
    PR feedback once the in_review debounce expires -- an auto-merge
    candidate would instead bounce back through validating in a loop.
    """

    PR_NUMBER = 25
    BRANCH = "orchestrator/issue-20"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(20, label="validating", comments=[
            FakeComment(
                id=850,
                body="original issue clarification posted before pickup",
                user=FakeUser("alice"),
                created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #25",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            20, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_pickup_chatter_does_not_replay_at_in_review(self) -> None:
        gh, issue, pr, long_ago = self._setup()

        # Step 1: validating approves. Watermark must include id 850 so the
        # pre-pickup human comment is treated as consumed.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(20).get("pr_last_comment_id")
        self.assertIsNotNone(wm, "watermark must be seeded past pre-pickup")
        self.assertGreaterEqual(
            wm, 901,
            f"watermark must advance past pre-pickup chatter and self-run; "
            f"got {wm}",
        )

        # Backdate the approval comment too so debounce wouldn't filter it
        # out as a confound (it shouldn't matter because the watermark
        # already covers it, but be explicit).
        for c in list(pr.issue_comments):
            if c.created_at is None:
                c.created_at = long_ago

        # Step 2: in_review tick. With the fix, no comment is past the
        # watermark, so auto-merge proceeds. Without the fix, the human
        # comment id=850 surfaces as "new" and the dev gets resumed.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((20, "done"), gh.label_history)


class InReviewPRReviewSummaryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human can leave PR feedback either through inline review comments
    or through the *review summary* body (the textbox above the
    Approve / Request Changes / Comment buttons). The summary lives in the
    PullRequestReview id namespace, distinct from issue comments and inline
    review comments. Without surfacing it, a "Comment" review with body is
    silently auto-merged over and a CHANGES_REQUESTED summary blocks merge
    without the dev ever seeing the feedback.
    """

    PR_NUMBER = 130
    BRANCH = "orchestrator/issue-90"

    def _setup_with_review(self, review):
        gh = FakeGitHubClient()
        issue = make_issue(90, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            reviews=[review],
        )
        gh.add_pr(pr)
        gh.seed_state(
            90, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            # Watermarks below the seeded review id so the body surfaces as
            # fresh feedback. An unset summary watermark would trip the
            # legacy in_review migration and mask the review.
            pr_last_comment_id=999,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_changes_requested_with_body_resumes_dev(self) -> None:
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242,
            body="please rename foo to bar in the public API",
            state="CHANGES_REQUESTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
            commit_id="cafe1234",
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev resumed with the review body quoted into the prompt; pushed;
        # bounced to validating; summary watermark advanced past the review.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((90, "validating"), gh.label_history)
        self.assertEqual(gh.merge_calls, [])
        data = gh.pinned_data(90)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4242)
        self.assertEqual(data.get("review_round"), 0)

    def test_commented_review_with_body_resumes_dev(self) -> None:
        # A "Comment" review (state=COMMENTED) doesn't block via
        # pr_has_changes_requested, so without surfacing the body the
        # auto-merge gate would proceed and merge over the human's note.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4243,
            body="how about adding a smoke test for the empty-input case?",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="added test",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "smoke test for the empty-input case",
            mocks["run_agent"].call_args.args[1],
        )
        # Auto-merge did NOT fire over the human's comment.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((90, "validating"), gh.label_history)

    def test_approved_review_body_does_not_trigger_resume(self) -> None:
        # APPROVED reviews are excluded from the summary surface even when
        # they carry an informational body. The human approved the PR --
        # their note is not a request for changes.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4244, body="LGTM, ship it", state="APPROVED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        # APPROVED on the live head also satisfies the auto-merge gate
        # via pr_is_approved.
        pr.approved = True
        pr.approval_head_sha = "cafe1234"

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Auto-merge proceeds; the summary surface ignored the APPROVED body.
        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((90, "done"), gh.label_history)

    def test_empty_body_review_is_ignored(self) -> None:
        # A CHANGES_REQUESTED review with no body has nothing to forward to
        # the dev. pr_has_changes_requested still vetoes auto-merge (correct),
        # but no follow-up prompt is generated.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4245, body="", state="CHANGES_REQUESTED",
            user=FakeUser("alice"), submitted_at=long_ago,
        )
        gh, issue, pr = self._setup_with_review(review)
        # Mirror the pr_has_changes_requested veto path.
        pr.changes_requested = True
        pr.changes_requested_head_sha = "cafe1234"

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        # Veto blocked the merge; no label flip.
        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])


class SameAccountHumanFeedbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """Operators commonly run the orchestrator with a personal PAT and also
    review PRs by hand from that same GitHub account. The self-comment filter
    must not key on author login -- if it did, real human review feedback from
    that account would be dropped as bot noise and AUTO_MERGE could land a
    'please do not merge' comment.

    The fix tracks orchestrator-authored comments by exact id (recorded when
    the orchestrator posts them via `_post_issue_comment` /
    `_post_pr_comment`). A human comment from the PAT login carries an id the
    orchestrator never recorded, so it surfaces as fresh PR feedback and the
    auto-merge gate stays closed.
    """

    PR_NUMBER = 200
    BRANCH = "orchestrator/issue-100"

    def test_same_account_human_pr_comment_blocks_auto_merge(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(100, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The orchestrator's previous park message and the human's "please do
        # not merge yet" comment are both authored by FakeUser("orchestrator")
        # -- this models the operator's personal PAT being used both for the
        # bot and for the human review. Only the park id is in the recorded
        # set; the human comment must surface as fresh feedback.
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=3000, body="please do not merge yet",
                    user=FakeUser("orchestrator"),  # same login as PAT owner
                    created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            100,
            pr_number=self.PR_NUMBER,
            branch=self.BRANCH,
            dev_agent="claude",
            dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            # Watermark just past the orchestrator's earlier comments and the
            # human's id-3000 comment. Filter must drop only ids the
            # orchestrator actually recorded.
            pr_last_comment_id=2999,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="standing by"
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Auto-merge must not fire over the human's standing objection.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((100, "done"), gh.label_history)
        # The human comment is treated as fresh feedback: the dev session
        # is resumed on it and the issue bounces back to validating.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please do not merge yet",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((100, "validating"), gh.label_history)

    def test_same_account_human_issue_comment_at_handoff_is_preserved(self) -> None:
        # Validating-handoff variant: a human posts a review comment on the
        # issue thread (under the same account that owns the PAT) while
        # validating is still running. Without the id-based filter, the
        # handoff would advance the watermark past the human comment as if
        # it were the orchestrator's own self-run, then auto-merge over it.
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(101, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"),  # PAT-owner login
                created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #210",
                user=FakeUser("orchestrator"),
                created_at=long_ago,
            ),
            # Human review feedback posted from the same account during
            # validating. Login alone cannot distinguish this from the bot's
            # own messages; only the recorded-id set can.
            FakeComment(
                id=950, body="please add a docstring",
                user=FakeUser("orchestrator"),  # same login as PAT owner
                created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=210, head_branch="orchestrator/issue-101",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            101, pr_number=210, branch="orchestrator/issue-101",
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        # Step 1: validating approves; watermark seed must STOP at id=950.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        wm = gh.pinned_data(101).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must stop before same-account human comment id=950 "
            f"(got {wm})",
        )

        # Step 2: in_review tick. Human comment is still past the watermark
        # and the dev gets resumed -- not auto-merged.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docstring added"
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please add a docstring",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((101, "validating"), gh.label_history)


class LegacyInReviewWatermarkSeedTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An issue that reached `in_review` before validating started seeding
    watermarks (or that was manually relabeled, or whose handoff failed to
    snapshot the PR) sits on the in_review handler with all three watermarks
    unset. Without the first-tick migration, every historical comment --
    including the orchestrator's own pickup / PR-opened / approval messages
    -- would surface as fresh PR feedback once the debounce expired,
    resuming the dev and bouncing the PR back to validating.
    """

    PR_NUMBER = 300
    BRANCH = "orchestrator/issue-150"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Three historical orchestrator comments on the issue thread plus
        # one historical PR conversation comment (the validating handoff
        # approval) -- exactly the shape of an in-flight in_review issue
        # whose state was written before pr_last_comment_id existed.
        issue = make_issue(150, label="in_review", comments=[
            FakeComment(
                id=910, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=911, body=":sparkles: PR opened: #300",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=920,
                    body=":white_check_mark: codex review approved.",
                    user=FakeUser("orchestrator"),
                    created_at=long_ago,
                ),
            ],
            review_comments=[
                FakeComment(
                    id=30, body="line 5: drop the trailing newline",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
            reviews=[
                FakePRReview(
                    id=4000, body="please rename foo to bar",
                    state="CHANGES_REQUESTED",
                    user=FakeUser("alice"),
                    submitted_at=long_ago,
                    commit_id="cafe1234",
                ),
            ],
        )
        gh.add_pr(pr)
        # Legacy state: pr_number is set, but no watermarks AND no recorded
        # orchestrator_comment_ids. This is the state shape the migration
        # has to handle without replaying every historical comment.
        gh.seed_state(
            150, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
        )
        return gh, issue, pr

    def test_legacy_first_tick_does_not_replay_history(self) -> None:
        gh, issue, pr = self._legacy_setup()

        with patch.object(config, "AUTO_MERGE", False), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # No dev resume despite historical comments / inline review / review
        # summary all sitting visible: the migration seeded each watermark
        # past the latest visible id on its surface.
        mocks["run_agent"].assert_not_called()
        self.assertNotIn((150, "validating"), gh.label_history)
        # Watermarks were persisted so subsequent ticks see only newer ids.
        data = gh.pinned_data(150)
        self.assertGreaterEqual(data.get("pr_last_comment_id"), 920)
        self.assertEqual(data.get("pr_last_review_comment_id"), 30)
        self.assertEqual(data.get("pr_last_review_summary_id"), 4000)

    def test_legacy_first_tick_does_not_block_auto_merge(self) -> None:
        # AUTO_MERGE on with all gates passing: the migration must not park
        # or otherwise block the merge -- it only treats already-visible
        # comments as consumed.
        gh, issue, pr = self._legacy_setup()
        # Drop the historical review-summary so pr_has_changes_requested
        # doesn't veto via a separate path; the migration should still seed
        # the summary watermark past the inline review and then merge.
        pr.reviews = []

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((150, "done"), gh.label_history)


class CrossNamespaceFilterTest(unittest.TestCase, _PatchedWorkflowMixin):
    """orchestrator_comment_ids records ids from the IssueComment namespace
    only. Inline review comments and PR review summaries live in different
    id namespaces, where numeric collisions with recorded bot comment ids
    are possible -- and any human inline / summary feedback that happens to
    share an id must NOT be filtered out as self-authored.
    """

    def test_inline_review_with_colliding_id_still_surfaces(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(160, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=400, head_branch="orchestrator/issue-160",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=[
                FakeComment(
                    id=4242, body="rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        # Bot id 4242 was recorded in the issue-side namespace (e.g. the
        # validating handoff approval comment landed there with that id).
        # The same numeric id on the inline-review surface is a different
        # object -- the filter must ignore the namespace collision.
        gh.seed_state(
            160, pr_number=400, branch="orchestrator/issue-160",
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=4242,
            pr_last_review_comment_id=4241,
            pr_last_review_summary_id=0,
            orchestrator_comment_ids=[4242],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Inline review comment id=4242 surfaces despite colliding with the
        # recorded IssueComment id 4242; auto-merge does not fire.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((160, "validating"), gh.label_history)

    def test_review_summary_with_colliding_id_still_surfaces(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(161, label="in_review")
        gh.add_issue(issue)
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr = FakePR(
            number=401, head_branch="orchestrator/issue-161",
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            reviews=[
                FakePRReview(
                    id=5000, body="please tighten the spec",
                    state="COMMENTED",
                    user=FakeUser("alice"),
                    submitted_at=long_ago,
                    commit_id="cafe1234",
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            161, pr_number=401, branch="orchestrator/issue-161",
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=5000,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=4999,
            orchestrator_comment_ids=[5000],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="tightened",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "tighten the spec",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((161, "validating"), gh.label_history)


class TransientParkRecoveryTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An auto-merge candidate that parked on failed checks or unmergeability
    must auto-recover when the underlying GitHub state changes silently
    (CI rerun goes green, rebase resolves a conflict). Otherwise a human
    who fixes the transient condition without leaving a comment leaves the
    issue stuck in_review forever.
    """

    PR_NUMBER = 500
    BRANCH = "orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str, pr_kwargs: dict):
        gh = FakeGitHubClient()
        issue = make_issue(170, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            **pr_kwargs,
        )
        gh.add_pr(pr)
        gh.seed_state(
            170, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            awaiting_human=True,
            park_reason=park_reason,
            # Watermarks past everything visible -- mirrors what
            # _bump_in_review_watermarks set when the original park ran.
            pr_last_comment_id=10_000,
            pr_last_review_comment_id=10_000,
            pr_last_review_summary_id=10_000,
        )
        return gh, issue, pr

    def test_failed_checks_park_recovers_when_checks_go_green(self) -> None:
        gh, issue, pr = self._parked_issue(
            park_reason="failed_checks",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((170, "done"), gh.label_history)
        # Park flags cleared so subsequent ticks proceed normally.
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

    def test_unmergeable_park_recovers_when_pr_becomes_mergeable(self) -> None:
        gh, issue, pr = self._parked_issue(
            park_reason="unmergeable",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((170, "done"), gh.label_history)

    def test_failed_checks_park_stays_parked_when_checks_still_failing(
        self,
    ) -> None:
        # Recovery must not re-post the park message when the gate still
        # fails -- otherwise every poll would spam the issue.
        gh, issue, pr = self._parked_issue(
            park_reason="failed_checks",
            pr_kwargs=dict(mergeable=True, check_state="failure"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "failed_checks")

    def test_non_transient_park_stays_parked_even_when_gates_pass(self) -> None:
        # A park whose reason is not in the transient set (e.g. a missing
        # pr_number, a dev-fix failure) needs explicit human action and must
        # not recover from gate state alone.
        gh, issue, pr = self._parked_issue(
            park_reason="dev_fix_failed",
            pr_kwargs=dict(mergeable=True, check_state="success"),
        )

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(gh.merge_calls, [])
        self.assertEqual(gh.label_history, [])


class ValidatingTransientParkRecoveryTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A validating-side park whose underlying condition can self-resolve
    (a non-fast-forward push that the next --force-with-lease push will
    land) must auto-recover without needing a fresh issue-thread comment.
    Otherwise `_resume_developer_on_human_reply` -- which only fires on a
    new comment -- leaves the issue parked indefinitely even after the
    transient cause is gone.
    """

    BRANCH = "orchestrator/issue-170"

    def _parked_issue(self, *, park_reason: str):
        gh = FakeGitHubClient()
        # `last_action_comment_id` is well above any existing comment id, so
        # `comments_after` returns []. This mirrors the post-park watermark
        # set by `_park_awaiting_human` (it bumps to the latest comment id).
        issue = make_issue(170, label="validating")
        gh.add_issue(issue)
        gh.seed_state(
            170,
            pr_number=99, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=1,
            awaiting_human=True,
            park_reason=park_reason,
            last_action_comment_id=10_000,
        )
        return gh, issue

    def test_push_failed_park_recovers_when_push_succeeds(self) -> None:
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Force the worktree-existence check to pass; "/tmp" always exists
        # on Linux. The recovery only retries the push when the worktree
        # is still on disk (otherwise the dev's local commits are gone and
        # only a human relabel can unstick the issue).
        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        # Recovery must NOT spawn the agent or post any comment -- it is a
        # silent retry.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.posted_comments, [])
        self.assertEqual(gh.posted_pr_comments, [])
        # Push retried and succeeded: park flags cleared, review_round
        # incremented so the next tick runs the reviewer fresh.
        mocks["_push_branch"].assert_called_once()
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))
        self.assertEqual(data.get("review_round"), 2)
        # Stays in `validating` (no relabel); the next tick's reviewer will
        # decide whether to hand off.
        self.assertEqual(gh.label_history, [])

    def test_push_failed_park_stays_parked_when_push_still_fails(self) -> None:
        # Recovery must not re-post the park message when the push still
        # fails -- otherwise every poll would spam the issue.
        gh, issue = self._parked_issue(park_reason="push_failed")

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=False,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_called_once()
        # No new park comment posted on this tick.
        self.assertEqual(gh.posted_comments, [])
        # Park flags preserved for the next recovery attempt.
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")
        # review_round NOT bumped while still stuck.
        self.assertEqual(data.get("review_round"), 1)

    def test_push_failed_park_stays_parked_when_worktree_is_gone(self) -> None:
        # If the worktree was reaped between the original park and the
        # recovery tick, the dev's local commits are gone and there is
        # nothing to push. Stay parked so a human can intervene.
        gh, issue = self._parked_issue(park_reason="push_failed")

        # Path that will not exist on the test host.
        gone = Path("/tmp/orchestrator-test-recovery-no-such-worktree-xyz")
        with patch.object(workflow, "_worktree_path", return_value=gone):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("park_reason"), "push_failed")

    def test_non_transient_park_stays_parked_with_no_new_comments(self) -> None:
        # A park whose reason is not in the validating transient set (e.g.
        # a question or dirty-tree park) must NOT auto-recover. The
        # _resume_developer_on_human_reply path (no new comments) returns
        # without doing anything; recovery is the only other path and it
        # bails on park_reason.
        gh, issue = self._parked_issue(park_reason=None)

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
                push_branch=True,
            )

        mocks["run_agent"].assert_not_called()
        mocks["_push_branch"].assert_not_called()
        data = gh.pinned_data(170)
        self.assertTrue(data.get("awaiting_human"))
        self.assertEqual(data.get("review_round"), 1)

    def test_transient_park_with_new_comment_takes_resume_path(self) -> None:
        # A transient park is preempted by a fresh human comment: the
        # comment-driven resume path wins, the dev is spawned with the
        # human's feedback, and the recovery branch does not silently
        # retry the push. This ensures the human's reply is not dropped.
        gh, issue = self._parked_issue(park_reason="push_failed")
        issue.comments.append(
            FakeComment(
                id=10_500, body="please rebase first",
                user=FakeUser("alice"),
            )
        )

        with patch.object(workflow, "_worktree_path", return_value=Path("/tmp")):
            mocks = self._run(
                lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="rebased",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Dev was resumed with the human's feedback (recovery did NOT run).
        mocks["run_agent"].assert_called_once()
        followup = mocks["run_agent"].call_args.args[1]
        self.assertIn("please rebase first", followup)
        data = gh.pinned_data(170)
        self.assertFalse(data.get("awaiting_human"))


class ValidatingHandoffSeedsAllWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The validating -> in_review handoff has to seed every comment-surface
    watermark. The orchestrator never posts inline review comments or PR
    review summaries, so `_seed_watermark_past_self` returns None for those
    surfaces; without an explicit default seed, the in_review legacy
    migration would advance past human feedback submitted on those surfaces
    during validate (the COMMENTED PR review summary case is the worst:
    `pr_has_changes_requested` does not veto auto-merge, so AUTO_MERGE could
    land the PR over the human's note without surfacing it to the dev).
    """

    PR_NUMBER = 600
    BRANCH = "orchestrator/issue-200"

    def _setup(self, *, reviews=(), review_comments=()):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(200, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #600",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=list(review_comments),
            reviews=list(reviews),
        )
        gh.add_pr(pr)
        gh.seed_state(
            200, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr, long_ago

    def test_pre_handoff_review_summary_surfaces_in_in_review(self) -> None:
        # A "Comment" review without `CHANGES_REQUESTED` is the dangerous
        # case: it doesn't trip `pr_has_changes_requested` so AUTO_MERGE
        # would happily merge over it if the in_review tick advanced its
        # watermark past the body.
        long_ago_review = datetime.now(timezone.utc) - timedelta(hours=1)
        review = FakePRReview(
            id=4242, body="please tighten the docstring",
            state="COMMENTED",
            user=FakeUser("alice"),
            submitted_at=long_ago_review,
            commit_id="cafe1234",
        )
        gh, issue, pr, _ = self._setup(reviews=[review])

        # Step 1: validating approves. Handoff must seed
        # pr_last_review_summary_id so the legacy in_review migration cannot
        # accidentally advance past the human review.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_summary_id", data)
        # Seeded to 0 (or any value below the review id) -- not None and not
        # past the review.
        self.assertLess(data["pr_last_review_summary_id"], 4242)

        # Step 2: in_review tick. The summary surfaces and resumes the dev.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="tightened",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "tighten the docstring",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((200, "validating"), gh.label_history)

    def test_pre_handoff_inline_review_comment_surfaces(self) -> None:
        # Same shape, inline-review surface. The orchestrator never posts
        # there either, so handoff has to seed pr_last_review_comment_id
        # explicitly.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr, _ = self._setup(
            review_comments=[
                FakeComment(
                    id=77, body="line 4: rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(200)
        self.assertIn("pr_last_review_comment_id", data)
        self.assertLess(data["pr_last_review_comment_id"], 77)

        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])


class ManuallyClosedInReviewIssueTest(unittest.TestCase, _PatchedWorkflowMixin):
    """An open in_review issue closed manually by a human is a stop signal.
    The closed-in_review sweep yields the issue (so a Resolves-#N auto-close
    can finalize to `done`), but if the linked PR is still open the sweep
    has surfaced a manually-closed issue and `_handle_in_review` must mark
    it rejected before the auto-merge gates can run -- otherwise AUTO_MERGE
    can land the PR over the human's rejection.
    """

    PR_NUMBER = 700
    BRANCH = "orchestrator/issue-250"

    def _setup(self, **pr_kwargs):
        gh = FakeGitHubClient()
        issue = make_issue(250, label="in_review")
        issue.closed = True  # human closed the issue, PR still open
        gh.add_issue(issue)
        defaults = dict(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        defaults.update(pr_kwargs)
        pr = FakePR(**defaults)
        gh.add_pr(pr)
        gh.seed_state(
            250, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_manually_closed_with_open_pr_marks_rejected(self) -> None:
        gh, issue, pr = self._setup()

        with patch.object(config, "AUTO_MERGE", True):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # AUTO_MERGE must not fire over a manually-closed issue even though
        # every gate (approval, mergeable, success) would otherwise pass.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((250, "rejected"), gh.label_history)
        self.assertNotIn((250, "done"), gh.label_history)
        self.assertIn("closed_without_merge_at", gh.pinned_data(250))

    def test_manually_closed_does_not_resume_dev_on_new_comments(self) -> None:
        # Even with new PR feedback past the watermark, a manually-closed
        # issue should not spawn a dev fix -- the human closing the issue
        # superseded any open feedback.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        gh, issue, pr = self._setup()
        pr.issue_comments.append(
            FakeComment(
                id=2000, body="actually let's reconsider",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "AUTO_MERGE", False), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertIn((250, "rejected"), gh.label_history)

    def test_external_merge_with_closed_issue_still_finalizes_done(self) -> None:
        # The original closed-issue sweep purpose: a Resolves #N footer
        # auto-closes the issue when the PR merges. Issue closed AND PR
        # merged must still flip to `done`, not `rejected`.
        gh = FakeGitHubClient()
        issue = make_issue(251, label="in_review")
        issue.closed = True
        gh.add_issue(issue)
        pr = FakePR(
            number=701, head_branch="orchestrator/issue-251",
            head=FakePRRef(sha="cafe1234"),
            merged=True, state="closed",
        )
        gh.add_pr(pr)
        gh.seed_state(251, pr_number=701, branch="orchestrator/issue-251")

        self._run(
            lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        self.assertIn((251, "done"), gh.label_history)
        self.assertNotIn((251, "rejected"), gh.label_history)
        self.assertIn("merged_at", gh.pinned_data(251))


class HandoffInlineIdCollisionTest(unittest.TestCase, _PatchedWorkflowMixin):
    """orchestrator_comment_ids records IDs from the IssueComment namespace
    only. The validating handoff must NOT use that set to seed the inline
    review-comment watermark -- inline comments are PullRequestComment
    objects, with their own id space, where numeric collisions with bot
    issue/PR comment ids are possible. Otherwise a human inline comment
    whose id happens to match a recorded bot issue comment id would be
    treated as self-authored and consumed at handoff.
    """

    PR_NUMBER = 800
    BRANCH = "orchestrator/issue-300"

    def test_inline_comment_with_bot_issue_id_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(300, label="validating", comments=[
            FakeComment(
                id=4242, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            review_comments=[
                # Same numeric id as the bot's issue comment above, but a
                # different namespace (PullRequestComment). The handoff must
                # not treat this as self-authored.
                FakeComment(
                    id=4242, body="please rename foo to bar",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            300, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[4242],
            pickup_comment_id=4242,
        )

        # Step 1: validating handoff. The inline comment must NOT bump
        # pr_last_review_comment_id past 4242.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        data = gh.pinned_data(300)
        self.assertLess(
            data.get("pr_last_review_comment_id"), 4242,
            "id collision must not advance the inline-review watermark",
        )

        # Step 2: in_review tick. The human's inline comment surfaces and
        # the dev gets resumed -- not auto-merged.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((300, "validating"), gh.label_history)


class LegacyMigrationPersistsEmptyWatermarksTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """The legacy in_review migration runs on every tick where any of the
    three watermarks is unset. If the surface has no content yet, the
    migration would previously leave the watermark unset and re-fire next
    tick -- the FIRST human inline / summary review added in between would
    then be consumed by the migration before _handle_in_review built
    new_comments, allowing AUTO_MERGE to land the PR over that first
    review. The migration must persist 0 even on empty surfaces so the
    next tick scans new comments instead of re-migrating.
    """

    PR_NUMBER = 900
    BRANCH = "orchestrator/issue-400"

    def _legacy_setup(self):
        gh = FakeGitHubClient()
        # Make 'truly legacy': no watermarks at all on any surface, no
        # comments anywhere. This is the shape the reviewer flagged --
        # snapshot-failed handoff or pre-feature in_review state with an
        # empty PR.
        issue = make_issue(400, label="in_review")
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
        )
        return gh, issue, pr

    def test_first_inline_review_after_migration_surfaces(self) -> None:
        gh, issue, pr = self._legacy_setup()

        # Tick 1: legacy migration runs, surfaces have nothing to seed past.
        # The migration must persist 0 on every namespace anyway.
        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_comment_id"), 0)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)
        self.assertEqual(data.get("pr_last_comment_id"), 0)

        # Now a human posts the first inline review comment. With the fix,
        # the next tick sees pr_last_review_comment_id=0 (already set) and
        # surfaces id=42 instead of re-running migration past it.
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.review_comments.append(
            FakeComment(
                id=42, body="line 7: rename foo to bar",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="renamed",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # The first inline review comment after migration is treated as
        # fresh feedback and resumes the dev.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "rename foo to bar",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "validating"), gh.label_history)

    def test_first_review_summary_after_migration_surfaces(self) -> None:
        # Same shape on the review-summary surface. A COMMENTED summary
        # body is the dangerous case here: pr_has_changes_requested does
        # not veto and AUTO_MERGE could otherwise land the PR over it.
        gh, issue, pr = self._legacy_setup()
        # Need agent_approved_sha so the auto-merge path doesn't bail on
        # missing approval -- mirrors a freshly-handed-off issue.
        gh.seed_state(
            400, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
        )

        with patch.object(config, "AUTO_MERGE", False):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )
        data = gh.pinned_data(400)
        self.assertEqual(data.get("pr_last_review_summary_id"), 0)

        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        pr.reviews.append(
            FakePRReview(
                id=5050, body="please tighten the spec",
                state="COMMENTED",
                user=FakeUser("alice"),
                submitted_at=long_ago,
                commit_id="cafe1234",
            ),
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="tightened",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "tighten the spec",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((400, "validating"), gh.label_history)


class HandoffWithoutPickupIdLegacyStateTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """For an issue picked up under an older orchestrator version that did
    not record `pickup_comment_id`, the validating handoff cannot tell
    pre-pickup chatter (safe to skip) from human feedback posted during
    implementing/validating (must preserve). The seed-watermark function
    must refuse to advance past anything in that legacy state, defaulting
    pr_last_comment_id to 0; the orchestrator_comment_ids id-set filter in
    `_handle_in_review` then drops the recorded bot comments at scan time
    while leaving every human comment visible.
    """

    PR_NUMBER = 1000
    BRANCH = "orchestrator/issue-500"

    def test_legacy_human_during_implementing_survives_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Comment id ordering models a real legacy lifecycle: pre-pickup
        # chatter, then a pickup posted by the OLD orchestrator (id 900,
        # NOT recorded in orchestrator_comment_ids), then a human "do not
        # merge yet" posted while the dev was implementing, then a
        # PR-opened comment posted by the NEW orchestrator (id 960,
        # recorded). The human comment between the two bot posts is the
        # signal that must NOT be lost.
        issue = make_issue(500, label="validating", comments=[
            FakeComment(
                id=800, body="original issue clarification",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=950, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=960, body=":sparkles: PR opened: #1000",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        # Legacy state: PR-opened (960) is the FIRST recorded bot id;
        # pickup_comment_id is missing because pickup happened under the
        # old code. Validating handoff will then see only {960} as
        # orchestrator content; the seed-watermark function must NOT
        # falsely treat ids 800/900/950 as pre-pickup chatter.
        gh.seed_state(
            500, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[960],
        )

        # Step 1: validating approves. Handoff must NOT advance the
        # watermark past 950.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
        )
        wm = gh.pinned_data(500).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 950,
            f"watermark must not consume legacy human feedback at id 950 "
            f"(got {wm})",
        )

        # Step 2: in_review tick. AUTO_MERGE on, every gate passes -- the
        # only thing standing between the PR and a merge is the human's
        # "do not merge yet" comment, which the handler must surface.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="ack",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # Auto-merge must NOT fire.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((500, "done"), gh.label_history)
        # The "do not merge yet" comment surfaces as fresh PR feedback;
        # the dev session is resumed on it (alongside other legacy
        # comments the migration cannot reliably classify).
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "do not merge yet",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((500, "validating"), gh.label_history)


class GitHubClientClosedIssueSweepLabelTest(unittest.TestCase):
    """Real PyGithub's `Repository.get_issues(labels=...)` expects Label
    OBJECTS and reads `label.name`. The closed-issue sweep used to pass a
    raw string list, which raises a TypeError before the generator yields
    anything; because that exception escapes the per-issue try/except in
    `tick()`, every tick after open issues are processed would fail and
    externally-merged in_review issues would never finalize to `done`.

    This test pokes the real `GitHubClient.list_pollable_issues` against a
    mocked Repository to verify the call passes a Label object.
    """

    def test_closed_sweep_uses_label_object_from_get_label(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        # Bypass __init__: it would require a real PAT and Github client.
        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        # First get_issues call (open sweep) returns nothing; second call
        # (closed sweep) returns nothing too -- we only care about the
        # arguments PASSED to that second call.
        client.repo.get_issues.return_value = iter([])
        in_review_label = MagicMock(name="in_review_label")
        client.repo.get_label.return_value = in_review_label

        list(client.list_pollable_issues())

        # The label was looked up by name.
        client.repo.get_label.assert_called_once_with("in_review")
        # The closed sweep was invoked with the Label OBJECT, not a string.
        closed_call = next(
            (
                ca for ca in client.repo.get_issues.call_args_list
                if ca.kwargs.get("state") == "closed"
            ),
            None,
        )
        self.assertIsNotNone(closed_call, "closed sweep was not invoked")
        self.assertEqual(closed_call.kwargs["labels"], [in_review_label])

    def test_missing_label_skips_closed_sweep_without_raising(self) -> None:
        # If `get_label` raises (under-scoped PAT, label not yet bootstrapped)
        # the generator must complete the open-issue sweep AND swallow the
        # closed-issue branch -- otherwise `tick()` aborts mid-loop.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        client.repo.get_issues.return_value = iter([])
        client.repo.get_label.side_effect = GithubException(
            404, {"message": "Not Found"}, None
        )

        # Must not raise.
        out = list(client.list_pollable_issues())

        self.assertEqual(out, [])
        # Only the open sweep was invoked.
        states = [
            ca.kwargs.get("state")
            for ca in client.repo.get_issues.call_args_list
        ]
        self.assertEqual(states, ["open"])


class ZeroWatermarkSurvivesFallbackTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A legacy validating handoff stores `pr_last_comment_id = 0` to mean
    "scan all from the beginning". The in_review fallback to
    `last_action_comment_id` must not discard 0 in favor of a higher prior
    park-comment id; otherwise lower-id human feedback (e.g. an implementing-
    time "do not merge yet") sits below the watermark and AUTO_MERGE can
    land the PR over it.
    """

    PR_NUMBER = 1100
    BRANCH = "orchestrator/issue-600"

    def test_zero_watermark_does_not_fall_back_to_last_action(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # The implementing-time park comment (id 920) sits between a human
        # "do not merge yet" comment (id 910) and the validating-handoff
        # state. last_action_comment_id was set to 920 by the prior park.
        # If the in_review handler falls back to that for the watermark,
        # comment 910 is below it and gets dropped.
        issue = make_issue(600, label="in_review", comments=[
            FakeComment(
                id=910, body="please do not merge yet",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body=":robot: park message from a prior tick",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            600,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            # Legacy default: 0 means "scan everything".
            pr_last_comment_id=0,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # ALSO populated from the prior park; must NOT take precedence
            # over the legacy 0 watermark.
            last_action_comment_id=920,
            # Park the bot's own message id so the id-set filter drops it.
            orchestrator_comment_ids=[920],
        )

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="ack",
                ),
                push_branch=True,
                head_shas=["aaa", "bbb"],
            )

        # AUTO_MERGE must NOT fire over the human's id=910 comment.
        self.assertEqual(gh.merge_calls, [])
        self.assertNotIn((600, "done"), gh.label_history)
        # Dev resumed on the human comment.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "do not merge yet",
            mocks["run_agent"].call_args.args[1],
        )
        self.assertIn((600, "validating"), gh.label_history)


class StaleParkReasonClearedOnNewParkTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """A transient AUTO_MERGE park (failed_checks/unmergeable) followed by
    a comment-driven dev resume that itself parks (e.g. the dev asked a
    question, made no commit, or left a dirty worktree) must replace the
    stale `park_reason`. Otherwise the next tick's recovery branch sees a
    transient reason, re-checks gates, and merges over the dev's standing
    question or follow-up.
    """

    PR_NUMBER = 1200
    BRANCH = "orchestrator/issue-700"

    def test_stale_park_reason_cleared_after_question_park(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Tick 0 already parked for failed_checks; the human posted a
        # follow-up comment ("any update?") to nudge the orchestrator.
        issue = make_issue(700, label="in_review", comments=[
            FakeComment(
                id=3000, body="any update?",
                user=FakeUser("alice"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            700,
            pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            agent_approved_sha="cafe1234",
            pr_last_comment_id=2999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
            # Carryover from the original transient park.
            awaiting_human=True,
            park_reason="failed_checks",
        )

        # Tick A: the new comment arrives; dev gets resumed; the run
        # produces no commit (head SHA unchanged), which routes through
        # `_on_question`. That path must clear `park_reason`.
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess",
                    last_message="I cannot proceed without a clarification",
                ),
                push_branch=True,
                head_shas=["sha-before", "sha-before"],  # no new commit
            )
        data = gh.pinned_data(700)
        self.assertTrue(
            data.get("awaiting_human"),
            "should still be awaiting human after the question",
        )
        self.assertIsNone(
            data.get("park_reason"),
            "stale 'failed_checks' park reason must be cleared by the "
            "question park",
        )

        # Tick B: no new comments; gates still pass. Recovery must NOT
        # fire because park_reason is no longer transient.
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [],
            "auto-merge must not fire over the standing dev question",
        )
        self.assertNotIn((700, "done"), gh.label_history)
        data = gh.pinned_data(700)
        self.assertTrue(data.get("awaiting_human"))


class ReviewedShaBranchUpdateRaceTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The reviewer agent reads the LOCAL worktree; if the remote PR head
    moves between the review and the validating handoff (force-push, an
    out-of-band commit, a stale worktree), `pr.head.sha` no longer matches
    the commit the agent inspected. Persisting `pr.head.sha` as
    `agent_approved_sha` would mark an unreviewed commit as agent-approved
    and AUTO_MERGE could then land it once gates pass. Persist the local
    reviewed SHA instead; the auto-merge gate's existing
    `agent_approved_sha == head_sha` check then naturally rejects the
    race-introduced commit on the next in_review tick.
    """

    PR_NUMBER = 1300
    BRANCH = "orchestrator/issue-800"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(800, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #1300",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        # The remote PR head ("forced42") differs from what the reviewer
        # actually inspected on the local worktree ("reviewedAA"). Models
        # an out-of-band push that landed between the review and the
        # handoff -- the reviewer's verdict applies to "reviewedAA", not
        # to "forced42".
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="forced42"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            800, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )
        return gh, issue, pr

    def test_remote_head_moved_during_review_blocks_auto_merge(self) -> None:
        gh, issue, pr = self._setup()

        # Step 1: validating approves. The reviewer ran against the local
        # worktree at "reviewedAA". The remote PR shows "forced42".
        # `agent_approved_sha` must record what the agent actually saw.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("reviewedAA",),
        )

        data = gh.pinned_data(800)
        self.assertEqual(
            data.get("agent_approved_sha"), "reviewedAA",
            "agent_approved_sha must be the local reviewed SHA, not "
            "pr.head.sha at handoff time",
        )

        # Step 2: in_review tick. AUTO_MERGE on, all gates would otherwise
        # pass; the only reason the merge does NOT fire is the SHA
        # mismatch between agent_approved_sha (reviewedAA) and the live
        # head (forced42). Without this guard, AUTO_MERGE would land an
        # unreviewed commit.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(
            gh.merge_calls, [],
            "AUTO_MERGE must not land 'forced42' when only 'reviewedAA' "
            "was actually reviewed",
        )
        self.assertNotIn((800, "done"), gh.label_history)

    def test_remote_head_unchanged_lets_auto_merge_proceed(self) -> None:
        # Same setup, but the local reviewed SHA matches the remote PR
        # head: AUTO_MERGE proceeds normally. This is the happy path that
        # must keep working after the fix.
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(801, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #1301",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=1301, head_branch="orchestrator/issue-801",
            head=FakePRRef(sha="happyAA"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            801, pr_number=1301, branch="orchestrator/issue-801",
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
        )

        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("happyAA",),
        )

        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(1301, "happyAA", "squash")]
        )
        self.assertIn((801, "done"), gh.label_history)


class HandoffSkipsConsumedRepliesTest(unittest.TestCase, _PatchedWorkflowMixin):
    """A human reply consumed by `_resume_developer_on_human_reply` during
    implementing or validating must not re-surface as fresh PR feedback in
    in_review. The validating handoff watermark seed has to walk past such
    already-consumed comments; otherwise the next in_review tick re-resumes
    the dev on the same human input it has already addressed and can block
    AUTO_MERGE indefinitely.
    """

    PR_NUMBER = 1500
    BRANCH = "orchestrator/issue-900"

    def test_consumed_reply_does_not_replay_after_handoff(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> implementing dev asks question, parks
        # at 910 -> human replies "use sqlite" at 920 -> next tick resumes
        # the dev with that comment -> dev commits, _on_commits posts
        # PR-opened at 930 -> validating reviewer approves and posts
        # approval comment at 940. The reply at 920 was already fed to
        # the dev; in_review must NOT replay it.
        issue = make_issue(900, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1500",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        # `last_action_comment_id=920` reflects the post-resume bump --
        # the resume ate comments after the park (910) up through 920.
        gh.seed_state(
            900, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves. The handoff seed must walk PAST
        # comment 920 (already consumed) instead of stopping at it.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        wm = gh.pinned_data(900).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertGreaterEqual(
            wm, 930,
            f"watermark must advance past consumed reply (id 920); got {wm}",
        )

        # Step 2: in_review tick. AUTO_MERGE on; comment 920 must NOT
        # surface and the merge proceeds.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "cafe1234", "squash")]
        )
        self.assertIn((900, "done"), gh.label_history)

    def test_resume_bumps_last_action_comment_id_to_consumed_max(self) -> None:
        # Direct unit-level check on `_resume_developer_on_human_reply`:
        # after the resume runs, `last_action_comment_id` must reflect
        # the highest consumed id, not the prior park id.
        from orchestrator.github import PinnedState

        gh = FakeGitHubClient()
        issue = make_issue(901, label="implementing", comments=[
            FakeComment(id=910, body="park", user=FakeUser("orchestrator")),
            FakeComment(id=920, body="use sqlite", user=FakeUser("alice")),
            FakeComment(id=921, body="and add a test", user=FakeUser("alice")),
        ])
        gh.add_issue(issue)
        gh.seed_state(
            901, dev_agent="claude", dev_session_id="dev-sess",
            last_action_comment_id=910,
        )
        state = gh.read_pinned_state(issue)

        with patch.object(workflow, "_ensure_worktree", lambda spec, n: _FAKE_WT), \
             patch.object(workflow, "run_agent", lambda *a, **kw: _agent()):
            result = workflow._resume_developer_on_human_reply(
                gh, _TEST_SPEC, issue, state
            )

        self.assertIsNotNone(result)
        self.assertEqual(
            state.get("last_action_comment_id"), 921,
            "resume must bump last_action_comment_id to max(consumed)",
        )


class HandoffConsumedThroughIssueThreadOnlyTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`last_action_comment_id` only records issue-thread comments fed via
    `_resume_developer_on_human_reply`; PR-conversation comments are never
    consumed via that path. The validating handoff seed must NOT apply
    `consumed_through` to the PR-conversation surface, or a human PR comment
    whose id sits below a later-consumed issue-thread reply gets silently
    advanced past and AUTO_MERGE lands the PR over unread feedback.
    """

    PR_NUMBER = 1600
    BRANCH = "orchestrator/issue-800"

    def test_pr_conv_comment_below_consumed_through_is_preserved(self) -> None:
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        # Lifecycle: pickup (900) -> park asking question (910) -> human
        # leaves a PR-conv comment at 915 (the one that MUST surface) ->
        # human also replies on the issue thread at 920 -> resume consumes
        # the issue reply and bumps `last_action_comment_id` to 920 ->
        # PR-opened comment at 930 -> validating reviewer approves and
        # posts approval at 940. The PR-conv comment at 915 was never fed
        # to the dev (validating only watches the issue thread); without
        # the fix the seed walks past it because 915 <= consumed_through
        # (920) and AUTO_MERGE merges over it.
        issue = make_issue(800, label="validating", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=910, body="@hitl agent needs your input to proceed",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=920, body="use sqlite please",
                user=FakeUser("alice"), created_at=long_ago,
            ),
            FakeComment(
                id=930, body=":sparkles: PR opened: #1600",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="cafe1234"),
            mergeable=True, check_state="success",
            issue_comments=[
                FakeComment(
                    id=915, body="please add a docstring to the public class",
                    user=FakeUser("alice"), created_at=long_ago,
                ),
            ],
        )
        gh.add_pr(pr)
        gh.seed_state(
            800, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 910, 930],
            pickup_comment_id=900,
            last_action_comment_id=920,
        )

        # Step 1: validating approves and seeds in_review watermarks. The
        # seed must stop before 915 so the next in_review tick scans the
        # PR-conv surface and finds the human comment.
        self._run(
            lambda: workflow._handle_validating(gh, _TEST_SPEC, issue),
            run_agent=_agent(last_message="LGTM\n\nVERDICT: APPROVED"),
            head_shas=("cafe1234",),
        )
        self.assertIn((800, "in_review"), gh.label_history)
        wm = gh.pinned_data(800).get("pr_last_comment_id")
        self.assertIsNotNone(wm)
        self.assertLess(
            wm, 915,
            "watermark must stop before unread PR-conv comment id=915 "
            f"(consumed_through=920 must NOT apply across surfaces); got {wm}",
        )

        # Step 2: in_review tick. The PR-conv comment surfaces, the dev is
        # resumed on it, and the issue bounces to validating instead of
        # merging.
        if not any(l.name == "in_review" for l in issue.labels):
            issue.labels = [FakeLabel("in_review")]
        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            mocks = self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="docstring added",
                ),
                push_branch=True,
                head_shas=["cafe1234", "cafe5678"],
            )

        # Dev was resumed on the unread PR-conv text -- the safety guarantee.
        self.assertEqual(mocks["run_agent"].call_count, 1)
        self.assertIn(
            "please add a docstring",
            mocks["run_agent"].call_args.args[1],
        )
        # No auto-merge over unread feedback.
        self.assertEqual(gh.merge_calls, [])
        self.assertIn((800, "validating"), gh.label_history)


class CheckRunsForbiddenSurfacesScopeHintTest(unittest.TestCase):
    """A 403 from the check-runs endpoint almost always means the PAT is
    missing 'Checks: read'. Silently swallowing the exception leaves
    `pr_combined_check_state` at 'none' for Actions-only PRs and AUTO_MERGE
    parks forever. Promote the 403 to log.error with a specific message
    naming the scope.
    """

    def test_403_on_get_check_runs_logs_actionable_error(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()

        commit_obj = MagicMock()
        # Combined-status path returns nothing useful (Actions-only PR).
        combined = MagicMock(state="", total_count=0)
        commit_obj.get_combined_status.return_value = combined
        # Check-runs path raises 403.
        commit_obj.get_check_runs.side_effect = GithubException(
            403, {"message": "Resource not accessible"}, None,
        )
        client.repo.get_commit.return_value = commit_obj

        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="ERROR") as cm:
            state = client.pr_combined_check_state(pr)

        self.assertEqual(state, "none")
        joined = "\n".join(cm.output)
        self.assertIn("403", joined)
        self.assertIn("Checks: read", joined)
        self.assertIn("AUTO_MERGE", joined)

    def test_non_403_check_runs_failure_logs_warning_only(self) -> None:
        # 404, transient 5xx, etc. are logged at warning level and don't
        # need scope guidance. Avoid noisy ERROR for unrelated failures.
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state="", total_count=0
        )
        commit_obj.get_check_runs.side_effect = GithubException(
            500, {"message": "Internal Server Error"}, None,
        )
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"

        with self.assertLogs("orchestrator.github", level="WARNING") as cm:
            client.pr_combined_check_state(pr)

        # Filter to only WARNING records (assertLogs catches WARNING and above).
        warning_only = [r for r in cm.records if r.levelname == "WARNING"]
        self.assertTrue(warning_only, "should log a warning for non-403 errors")
        # No ERROR for non-403 failures.
        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(error_records, [])


class AutoMergeSHAShiftDuringMergeabilityCheckTest(
    unittest.TestCase, _PatchedWorkflowMixin
):
    """`gh.pr_is_mergeable(pr)` calls `pr.update()` when the cached
    mergeable is None, which can refresh `pr.head.sha`. The approval and
    changes-requested gates ran against the earlier head_sha, so a commit
    landing during that refresh must NOT slip through to the merge call:
    AUTO_MERGE must NOT merge the refreshed (unreviewed) head.
    """

    PR_NUMBER = 30
    BRANCH = "orchestrator/issue-7"

    def _setup(self):
        gh = FakeGitHubClient()
        long_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        issue = make_issue(7, label="in_review", comments=[
            FakeComment(
                id=900, body=":robot: orchestrator picking this up.",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
            FakeComment(
                id=901, body=":sparkles: PR opened: #30",
                user=FakeUser("orchestrator"), created_at=long_ago,
            ),
        ])
        gh.add_issue(issue)
        pr = FakePR(
            number=self.PR_NUMBER, head_branch=self.BRANCH,
            head=FakePRRef(sha="reviewedSHA"),
            mergeable=True, check_state="success",
        )
        gh.add_pr(pr)
        gh.seed_state(
            7, pr_number=self.PR_NUMBER, branch=self.BRANCH,
            dev_agent="claude", dev_session_id="dev-sess",
            review_round=0,
            orchestrator_comment_ids=[900, 901],
            pickup_comment_id=900,
            agent_approved_sha="reviewedSHA",
            pr_last_comment_id=999,
            pr_last_review_comment_id=0,
            pr_last_review_summary_id=0,
        )
        return gh, issue, pr

    def test_sha_shift_during_pr_is_mergeable_blocks_merge(self) -> None:
        gh, issue, pr = self._setup()

        # Simulate what GitHub's lazy `pr.update()` does inside
        # `pr_is_mergeable`: a commit landed between the gate checks and
        # the mergeability resolution, so the refresh moves pr.head.sha to
        # an UNREVIEWED commit. The approval gate already ran against
        # 'reviewedSHA'; the merge must NOT proceed against 'unreviewedSHA'.
        original_is_mergeable = gh.pr_is_mergeable

        def mergeable_with_refresh(pr_arg):
            pr_arg.head = FakePRRef(sha="unreviewedSHA")
            return True

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600), \
             patch.object(gh, "pr_is_mergeable", mergeable_with_refresh):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        # Critical: no merge happened. Without the SHA-shift bail (and the
        # head_sha pin on merge_pr), AUTO_MERGE would have called
        # merge_pr(pr, sha='unreviewedSHA') and merged the unreviewed head.
        self.assertEqual(
            gh.merge_calls, [],
            "merge must not fire when pr.head.sha shifted between the "
            "approval gate and the merge call",
        )
        # Issue stayed in_review; next tick will re-evaluate against the
        # new head SHA (which is not yet approved).
        self.assertNotIn((7, "done"), gh.label_history)

    def test_sha_unchanged_during_pr_is_mergeable_merges_normally(self) -> None:
        # Sanity check: the SHA-shift guard must not regress the happy path
        # when `pr_is_mergeable` does NOT refresh the head. Same setup but
        # without the head mutation.
        gh, issue, pr = self._setup()

        with patch.object(config, "AUTO_MERGE", True), \
             patch.object(config, "IN_REVIEW_DEBOUNCE_SECONDS", 600):
            self._run(
                lambda: workflow._handle_in_review(gh, _TEST_SPEC, issue),
                run_agent=_agent(),
            )

        self.assertEqual(
            gh.merge_calls, [(self.PR_NUMBER, "reviewedSHA", "squash")],
            "happy path must still merge against the gated head_sha",
        )
        self.assertIn((7, "done"), gh.label_history)


class PrCombinedCheckStatePartialReadFailsClosedTest(unittest.TestCase):
    """A read failure on one checks surface must NOT be masked by a
    'success' from the other surface. Otherwise a single green
    commit-status context plus failing or pending GitHub Actions check-runs
    that the PAT cannot read (403 from a missing 'Checks: read' scope, or a
    transient 5xx) would be reported as 'success' and AUTO_MERGE could land
    a PR over the unread failing checks.
    """

    def _client_with(self, *, combined_state, combined_total, check_runs_exc):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        commit_obj = MagicMock()
        commit_obj.get_combined_status.return_value = MagicMock(
            state=combined_state, total_count=combined_total,
        )
        commit_obj.get_check_runs.side_effect = check_runs_exc
        client.repo.get_commit.return_value = commit_obj
        pr = MagicMock()
        pr.head.sha = "deadbeef"
        return client, pr

    def test_combined_success_with_check_runs_403_returns_pending(self) -> None:
        # The dangerous case: legacy commit-status says 'success' but the
        # PAT cannot read check-runs. Without the partial-read guard,
        # AUTO_MERGE would land over failing/pending Actions runs.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "pending",
            "partial read with combined='success' must downgrade to "
            "'pending' to keep AUTO_MERGE from merging on half the picture",
        )

    def test_combined_success_with_check_runs_500_returns_pending(self) -> None:
        # A transient 5xx on check-runs has the same downgrade rule -- the
        # next tick may succeed and resolve to a real verdict, but until
        # then we cannot report success.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="success", combined_total=1,
            check_runs_exc=GithubException(
                500, {"message": "Internal Server Error"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="WARNING"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(state, "pending")

    def test_no_combined_signal_with_check_runs_403_still_returns_none(self) -> None:
        # Edge case: combined-status returned no usable signal AND
        # check-runs raised. We have NO signal at all; preserve the
        # existing 'none' return so the workflow's failed_checks branch
        # parks awaiting_human (visible to the operator) instead of
        # silently waiting forever on 'pending'.
        from github import GithubException

        client, pr = self._client_with(
            combined_state="", combined_total=0,
            check_runs_exc=GithubException(
                403, {"message": "Resource not accessible"}, None,
            ),
        )
        with self.assertLogs("orchestrator.github", level="ERROR"):
            state = client.pr_combined_check_state(pr)
        self.assertEqual(
            state, "none",
            "no signal on either surface must keep returning 'none' so "
            "the workflow parks awaiting_human instead of pending forever",
        )


def _manifest(payload: str) -> str:
    return f"```orchestrator-manifest\n{payload}\n```"


class ParseManifestTest(unittest.TestCase):
    def test_single_decision(self) -> None:
        msg = "I think this fits.\n\n" + _manifest(
            '{"decision": "single", "rationale": "small change"}'
        )
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertIsNotNone(data)
        self.assertEqual(data["decision"], "single")

    def test_split_decision_two_children(self) -> None:
        payload = (
            '{"decision": "split", "rationale": "too many surfaces", '
            '"children": ['
            '{"title": "A", "body": "do A", "depends_on": []},'
            '{"title": "B", "body": "do B", "depends_on": [0]}'
            ']}'
        )
        data, error = workflow._parse_manifest(_manifest(payload))
        self.assertIsNone(error)
        self.assertEqual(len(data["children"]), 2)
        self.assertEqual(data["children"][1]["depends_on"], [0])

    def test_no_fenced_block_returns_none_none(self) -> None:
        data, error = workflow._parse_manifest("just a question, no fence")
        self.assertIsNone(data)
        self.assertIsNone(error)

    def test_invalid_json_returns_error(self) -> None:
        data, error = workflow._parse_manifest(_manifest("{not json"))
        self.assertIsNone(data)
        self.assertIn("invalid JSON", error)

    def test_unknown_decision_rejected(self) -> None:
        data, error = workflow._parse_manifest(
            _manifest('{"decision": "maybe"}')
        )
        self.assertIsNone(data)
        self.assertIn("decision", error)

    def test_split_with_empty_children_rejected(self) -> None:
        data, error = workflow._parse_manifest(
            _manifest('{"decision": "split", "children": []}')
        )
        self.assertIsNone(data)
        self.assertIn("non-empty", error)

    def test_child_missing_title_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"body": "no title here"}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("title or body", error)

    def test_self_dependency_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "X", "body": "x", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("invalid dependency", error)

    def test_dep_cycle_rejected(self) -> None:
        # 0 -> 1 -> 0
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": [1]},'
            '{"title": "B", "body": "b", "depends_on": [0]}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("cycle", error)

    def test_too_many_children_rejected(self) -> None:
        children = ",".join(
            f'{{"title": "T{i}", "body": "b{i}"}}' for i in range(11)
        )
        data, error = workflow._parse_manifest(_manifest(
            f'{{"decision": "split", "children": [{children}]}}'
        ))
        self.assertIsNone(data)
        self.assertIn("too many", error)

    def test_non_string_title_rejected(self) -> None:
        # JSON-valid manifest with a non-string title (here a number)
        # must be rejected before any side effects. Truthiness alone
        # would let `42` pass, but `gh.create_child_issue` (`body.rstrip()`
        # plus the PyGithub call) blows up only AFTER
        # `expected_children_count` has been persisted, forcing the
        # half-finished-recovery path instead of the clean
        # invalid-manifest HITL/resume loop.
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": 42, "body": "x"}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("title or body", error)

    def test_non_string_body_rejected(self) -> None:
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "x", "body": ["a", "b"]}'
            ']}'
        ))
        self.assertIsNone(data)
        self.assertIn("title or body", error)

    def test_multiple_manifest_blocks_rejected(self) -> None:
        # The decompose prompt requires exactly one manifest. If the
        # decomposer quotes a sample/template manifest and then emits its
        # real one, `re.search` would silently take the first (sample)
        # block and the orchestrator would act on the wrong decision --
        # creating wrong child issues or marking a split parent as
        # `single`. Reject the message before any side effects.
        sample = _manifest('{"decision": "single", "rationale": "sample"}')
        real = _manifest(
            '{"decision": "split", "rationale": "real", "children": ['
            '{"title": "A", "body": "do A", "depends_on": []}'
            ']}'
        )
        msg = f"Here is the schema:\n\n{sample}\n\nMy answer:\n\n{real}"
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(data)
        self.assertIn("exactly one", error)
        self.assertIn("found 2", error)

    def test_content_after_manifest_rejected(self) -> None:
        # The prompt says "nothing else after" the manifest. Trailing
        # prose suggests the agent did not finish its final answer or
        # appended commentary that the orchestrator would ignore --
        # either way, surface to the human rather than silently act.
        msg = _manifest('{"decision": "single"}') + "\n\nP.S. hope this works"
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(data)
        self.assertIn("final block", error)

    def test_trailing_whitespace_after_manifest_accepted(self) -> None:
        # Pure whitespace (newlines/spaces) after the closing fence is a
        # benign formatting artifact and must NOT trip the "trailing
        # content" guard.
        msg = _manifest('{"decision": "single"}') + "\n\n   \n"
        data, error = workflow._parse_manifest(msg)
        self.assertIsNone(error)
        self.assertEqual(data["decision"], "single")

    def test_scalar_falsy_depends_on_rejected(self) -> None:
        # `child.get("depends_on") or []` previously collapsed every
        # falsy scalar (0, False, "") to [] before the list-type check.
        # A manifest like `"depends_on": 0` -- a clear malformed list,
        # not "no deps" -- would be silently accepted and child 1
        # activated before child 0 instead of waiting on it. Reject
        # any non-list, non-null value so the standard invalid-manifest
        # HITL/resume loop catches the typo.
        for raw in ("0", "false", '""', "0.0"):
            with self.subTest(raw=raw):
                data, error = workflow._parse_manifest(_manifest(
                    '{"decision": "split", "children": ['
                    '{"title": "A", "body": "a"},'
                    f'{{"title": "B", "body": "b", "depends_on": {raw}}}'
                    ']}'
                ))
                self.assertIsNone(data)
                self.assertIn("must be a list", error)

    def test_null_depends_on_treated_as_empty(self) -> None:
        # Explicit JSON null is treated the same as a missing key:
        # both signal "no dependencies". Only a non-list, non-null
        # value is a contract violation. This locks in the forgiving
        # behavior so a future tighten-up doesn't accidentally start
        # rejecting `"depends_on": null`.
        data, error = workflow._parse_manifest(_manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a", "depends_on": null}'
            ']}'
        ))
        self.assertIsNone(error)
        self.assertIsNotNone(data)

    def test_displayed_schema_example_is_valid_manifest(self) -> None:
        # A literal-minded decomposer that copies the schema verbatim
        # must produce a manifest that survives _parse_manifest. If the
        # displayed example uses union notation or any other
        # non-JSON sugar, prompt-compliant runs would park awaiting
        # human for a self-inflicted reason. Round-trip the example
        # through the same parser the orchestrator runs on agent
        # output to keep the prompt and parser in lockstep.
        prompt = workflow._build_decompose_prompt(
            make_issue(1, title="example", body="some body"), ""
        )
        m = workflow._MANIFEST_RE.search(prompt)
        self.assertIsNotNone(m, "prompt must contain a fenced example")
        data, error = workflow._parse_manifest(m.group(0))
        self.assertIsNone(
            error, f"displayed example failed to parse: {error}"
        )
        self.assertIsNotNone(data)


class HandleDecomposingTest(unittest.TestCase, _PatchedWorkflowMixin):
    """The decomposer drives the (no-label / `decomposing`) -> ready/blocked
    transitions. Single decision routes the parent to `ready`; split creates
    children with `ready`/`blocked` labels and parks the parent on `blocked`.
    Malformed or absent manifests park awaiting human.
    """

    def test_pickup_routes_to_decomposing(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(10)
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "trivial"}'
        )

        with patch.object(config, "DECOMPOSE", True):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess", last_message=manifest
                ),
            )

        # First label flip is to decomposing; the single-decision path then
        # flips it to ready on the same tick.
        self.assertEqual(gh.label_history[0], (10, "decomposing"))
        self.assertIn((10, "ready"), gh.label_history)
        self.assertTrue(any(
            "decomposing" in body
            for _, body in gh.posted_comments
        ))

    def test_decompose_decision_single_flips_to_ready(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(11, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "fits in one context"}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        self.assertIn((11, "ready"), gh.label_history)
        # No children created.
        self.assertEqual(gh.created_child_issues, [])
        data = gh.pinned_data(11)
        self.assertEqual(data.get("decomposer_agent"), config.DECOMPOSE_AGENT)
        self.assertEqual(data.get("decomposer_session_id"), "dec-sess")
        self.assertIn("decomposed_at", data)
        # Rationale surfaced in a comment.
        self.assertTrue(any(
            "fits in one context" in body for _, body in gh.posted_comments
        ))

    def test_decompose_decision_split_creates_children(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(12, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "rationale": "two pieces", "children": ['
            '{"title": "Add status subcommand", "body": "implement status", '
            '"depends_on": []},'
            '{"title": "Add pause subcommand", "body": "implement pause", '
            '"depends_on": []}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        # Parent is now blocked; both children created with `ready`.
        self.assertIn((12, "blocked"), gh.label_history)
        self.assertEqual(len(gh.created_child_issues), 2)
        for child in gh.created_child_issues:
            self.assertEqual(
                [l.name for l in child.labels], ["ready"],
            )
            self.assertIn(f"Parent: #{12}", child.body)

        data = gh.pinned_data(12)
        self.assertEqual(
            data.get("children"),
            [c.number for c in gh.created_child_issues],
        )
        # No deps -> dep_graph not persisted.
        self.assertNotIn("dep_graph", data)
        # Summary comment lists both child numbers.
        last_comment = next(
            body for n, body in gh.posted_comments if n == 12
            and ":bookmark_tabs:" in body
        )
        for child in gh.created_child_issues:
            self.assertIn(f"#{child.number}", last_comment)

    def test_decompose_split_with_deps_persists_dep_graph(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(13, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "First", "body": "do first", "depends_on": []},'
            '{"title": "Second", "body": "needs first", "depends_on": [0]}'
            ']}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        children = gh.created_child_issues
        self.assertEqual(len(children), 2)
        # child[0] has no deps -> ready; child[1] depends on [0] -> blocked.
        self.assertEqual([l.name for l in children[0].labels], ["ready"])
        self.assertEqual([l.name for l in children[1].labels], ["blocked"])

        data = gh.pinned_data(13)
        self.assertEqual(data.get("dep_graph"), {"1": [0]})
        # Each child's pinned state records the parent so the polling
        # loop's blocked-issue dispatch can recognize it as a child
        # rather than as an unattributed `blocked` parent.
        for child in children:
            self.assertEqual(
                gh.pinned_data(child.number).get("parent_number"), 13,
            )

    def test_decompose_parks_if_decomposer_left_commits(self) -> None:
        # The decomposer is supposed to be read-only. If it commits in the
        # parent's worktree, the implementer recovery path in
        # `_handle_implementing` would later see `_has_new_commits` -> True
        # and push decomposer-authored work as if it were implementation.
        # Defensive park is the surface that catches this.
        gh = FakeGitHubClient()
        issue = make_issue(40, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            has_new_commits=True,
        )

        data = gh.pinned_data(40)
        self.assertTrue(data.get("awaiting_human"))
        # Did NOT advance to ready -- the operator must clean up first.
        self.assertNotIn((40, "ready"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("read-only", last_comment)

    def test_decompose_parks_if_decomposer_left_dirty_files(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(41, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            dirty_files=("foo.py",),
        )

        data = gh.pinned_data(41)
        self.assertTrue(data.get("awaiting_human"))
        self.assertNotIn((41, "ready"), gh.label_history)
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("read-only", last_comment)

    def test_decompose_malformed_manifest_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(14, label="decomposing")
        gh.add_issue(issue)
        bad = _manifest("{not really json")

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=bad),
        )

        data = gh.pinned_data(14)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("manifest invalid", last_comment)
        # Last decomposer message quoted into the HITL ping so the human
        # can see what the agent actually emitted.
        self.assertIn("not really json", last_comment)
        # Decomposer session recorded so the resume on human reply uses
        # the right backend even if DECOMPOSE_AGENT flips between ticks.
        self.assertEqual(data.get("decomposer_session_id"), "dec-sess")

    def test_decompose_no_manifest_question_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(15, label="decomposing")
        gh.add_issue(issue)

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess",
                last_message="Should the new commands accept a --json flag?",
            ),
        )

        data = gh.pinned_data(15)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("needs your input", last_comment)
        self.assertIn("--json flag", last_comment)

    def test_decompose_resume_on_human_reply(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(16, label="decomposing")
        issue.comments.append(FakeComment(
            id=1100, body="please split into 2", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            16,
            awaiting_human=True,
            last_action_comment_id=900,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            ']}'
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dec-sess", last_message=manifest
            ),
        )

        # Resume happened with the human comment quoted, on the locked
        # backend.
        mocks["run_agent"].assert_called_once()
        call = mocks["run_agent"].call_args
        self.assertEqual(call.args[0], "claude")
        self.assertEqual(call.kwargs.get("resume_session_id"), "dec-sess")
        self.assertIn("please split into 2", call.args[1])

        self.assertIn((16, "blocked"), gh.label_history)
        self.assertEqual(len(gh.created_child_issues), 2)
        self.assertFalse(gh.pinned_data(16).get("awaiting_human"))

    def test_decompose_agent_locked_on_resume(self) -> None:
        # Pinned state recorded `decomposer_agent="claude"`. Even after
        # DECOMPOSE_AGENT flips to "codex", the resume must stick with
        # claude -- session ids do not bridge across backends.
        gh = FakeGitHubClient()
        issue = make_issue(17, label="decomposing")
        issue.comments.append(FakeComment(
            id=1100, body="any update?", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            17,
            awaiting_human=True,
            last_action_comment_id=900,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )
        manifest = _manifest(
            '{"decision": "single", "rationale": "trivial"}'
        )

        with patch.object(config, "DECOMPOSE_AGENT", "codex"):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dec-sess", last_message=manifest
                ),
            )

        self.assertEqual(mocks["run_agent"].call_args.args[0], "claude")
        self.assertEqual(
            mocks["run_agent"].call_args.kwargs.get("resume_session_id"),
            "dec-sess",
        )

    def test_decompose_retry_cap_parks(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(18, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            18,
            retry_count=config.MAX_RETRIES_PER_DAY,
            retry_window_start=_iso_hours_ago(1),
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertTrue(gh.pinned_data(18).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn(
            f"hit retry cap ({config.MAX_RETRIES_PER_DAY}/day) for decomposing",
            last_comment,
        )

    def test_decompose_off_falls_back_to_legacy_pickup(self) -> None:
        # End-to-end: with DECOMPOSE=off, the unlabeled issue must skip
        # the decomposer entirely and route straight to implementing
        # exactly as the bootstrap-milestone path did. No `decomposing`
        # label and no decomposer pinned-state keys are written.
        gh = FakeGitHubClient()
        issue = make_issue(19)
        gh.add_issue(issue)

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_pickup(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="done"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        self.assertNotIn(
            "decomposing", [lbl for _, lbl in gh.label_history],
        )
        self.assertIn((19, "implementing"), gh.label_history)
        self.assertEqual(gh.created_child_issues, [])
        data = gh.pinned_data(19)
        self.assertNotIn("decomposer_agent", data)
        self.assertNotIn("decomposer_session_id", data)

    def test_decompose_off_routes_decomposing_label_to_implementing(
        self,
    ) -> None:
        # The DECOMPOSE kill switch must apply to issues that were
        # already labeled `decomposing` (or parked there awaiting a
        # human) when the operator restarts with the flag off.
        # Without this, `_process_issue` still calls `_handle_decomposing`
        # for that label and the disabled rollout keeps spawning the
        # decomposer, producing manifests and child issues that the
        # operator explicitly disabled.
        gh = FakeGitHubClient()
        issue = make_issue(20, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            20,
            awaiting_human=True,
            park_reason="(test) decomposer asked a question",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
            last_action_comment_id=900,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # The agent that did run was the dev agent (legacy implementing
        # took over), not the decomposer.
        mocks["run_agent"].assert_called_once()
        self.assertEqual(
            mocks["run_agent"].call_args.args[0], config.DEV_AGENT,
            "kill switch must route to the dev backend, not decomposer",
        )

        # Label transitioned to implementing. Must never have routed
        # through `blocked` (that would have implied children created).
        labels = [lbl for _, lbl in gh.label_history]
        self.assertIn("implementing", labels)
        self.assertNotIn("blocked", labels)

        # Decomposer-side park state cleared so `_handle_implementing`'s
        # awaiting_human resume branch doesn't fire on stale state.
        data = gh.pinned_data(20)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))

        # Routing comment posted; no children created.
        self.assertTrue(any(
            "decomposition is disabled" in body
            for _, body in gh.posted_comments
        ))
        self.assertEqual(gh.created_child_issues, [])

    def test_decompose_off_ratchets_last_action_past_decomposing_comments(
        self,
    ) -> None:
        # When DECOMPOSE flips off mid-flight, decomposing-era human
        # comments newer than `last_action_comment_id` must be marked
        # consumed before falling into `_handle_implementing`. The
        # implementer reads the full thread via `_recent_comments_text`
        # at spawn, so the dev sees those comments at implementation
        # time. Without the ratchet, the validating->in_review
        # watermark seed later treats those same comments as fresh PR
        # feedback and bounces the dev unnecessarily -- exactly the
        # replay `_handle_ready` already prevents on the single-decision
        # happy path.
        gh = FakeGitHubClient()
        issue = make_issue(21, label="decomposing")
        # Decomposer-era HITL comments newer than the parked
        # last_action_comment_id (which is anchored on the original
        # pickup or an earlier decomposer round).
        issue.comments.append(FakeComment(
            id=950, body="please reconsider", user=FakeUser("alice"),
        ))
        issue.comments.append(FakeComment(
            id=960, body="the title is wrong", user=FakeUser("bob"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            21,
            awaiting_human=True,
            park_reason="(test) decomposer asked a question",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
            last_action_comment_id=900,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        data = gh.pinned_data(21)
        last_action = data.get("last_action_comment_id")
        # Must be past the highest decomposing-era comment so the
        # in_review watermark seed treats them as already-consumed.
        self.assertIsInstance(last_action, int)
        self.assertGreaterEqual(last_action, 960)

    def test_decompose_off_does_not_lower_last_action_comment_id(self) -> None:
        # The ratchet is one-way. If `last_action_comment_id` is
        # already past the latest visible comment (e.g. a prior tick
        # consumed everything and a later high-id comment hasn't been
        # posted yet), the kill-switch path must NOT lower it.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="decomposing")
        # One older comment; latest visible id is 500.
        issue.comments.append(FakeComment(
            id=500, body="early note", user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            22,
            awaiting_human=True,
            last_action_comment_id=10000,
            pickup_comment_id=100,
        )

        with patch.object(config, "DECOMPOSE", False):
            self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
                run_agent=_agent(
                    session_id="dev-sess", last_message="implemented"
                ),
                has_new_commits=[False, True],
                push_branch=True,
            )

        # Must not regress below the previously persisted high water mark.
        self.assertGreaterEqual(
            gh.pinned_data(22).get("last_action_comment_id"), 10000,
        )

    def test_decompose_off_still_finalizes_half_finished_split(self) -> None:
        # If a SIGKILL crashed a split between the parent's last
        # incremental `children` write and the parent label flip,
        # turning the kill switch on must NOT abandon the orphan
        # children -- they already exist on GitHub. Half-finished
        # recovery sits ABOVE the kill-switch bailout precisely so a
        # disabled rollout can still finalize the in-flight state to
        # `blocked` without spawning the decomposer.
        gh = FakeGitHubClient()
        parent = make_issue(50, label="decomposing")
        gh.add_issue(parent)
        for child_number in (101, 102):
            child = make_issue(child_number, label="blocked")
            gh.add_issue(child)
            gh.seed_state(
                child_number, parent_number=50,
                created_at="2026-05-03T00:00:00+00:00",
            )
        gh.seed_state(
            50,
            children=[101, 102],
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        with patch.object(config, "DECOMPOSE", False):
            mocks = self._run(
                lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
                run_agent=_agent(),
            )

        mocks["run_agent"].assert_not_called()
        labels = [lbl for _, lbl in gh.label_history]
        self.assertIn("blocked", labels)
        self.assertNotIn("implementing", labels)
        self.assertEqual(gh.created_child_issues, [])

    def test_decompose_persists_children_incrementally(self) -> None:
        # Each successful child creation must flush the parent's
        # `children` list before the next iteration starts. Without this,
        # a process kill (no exception) between iterations leaves the
        # parent without a `children` record, the next tick re-spawns the
        # decomposer, and duplicate child issues are created. We probe
        # the contract by snapshotting the parent's persisted `children`
        # list at the moment each child creation begins.
        gh = FakeGitHubClient()
        issue = make_issue(80, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"},'
            '{"title": "C", "body": "c"}'
            ']}'
        )

        snapshots: list[list] = []
        real_create = gh.create_child_issue

        def spy_create(**kwargs):
            snapshots.append(list(gh.pinned_data(80).get("children") or []))
            return real_create(**kwargs)

        gh.create_child_issue = spy_create

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        # iter 0: no children yet. iter 1: child[0] already persisted.
        # iter 2: child[0] + child[1] already persisted.
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(snapshots[0], [])
        self.assertEqual(len(snapshots[1]), 1)
        self.assertEqual(len(snapshots[2]), 2)
        self.assertEqual(
            len(gh.pinned_data(80).get("children") or []), 3,
        )

    def test_half_finished_recovery_flips_to_blocked(self) -> None:
        # Simulate: a prior tick created+persisted children but crashed
        # before flipping the parent label from `decomposing` to
        # `blocked`. The next tick must NOT re-spawn the decomposer
        # (would create duplicate children); it must finalize the parent
        # transition. The parent's `_handle_blocked` activates no-dep
        # children on a subsequent tick.
        gh = FakeGitHubClient()
        issue = make_issue(50, label="decomposing")
        gh.add_issue(issue)
        # Children already exist on GitHub with `parent_number` seeded --
        # the crash happened AFTER both child seeds, between the parent's
        # last incremental write and the parent label flip.
        for child_number in (101, 102):
            child = make_issue(child_number, label="blocked")
            gh.add_issue(child)
            gh.seed_state(
                child_number, parent_number=50,
                created_at="2026-05-03T00:00:00+00:00",
            )
        gh.seed_state(
            50,
            children=[101, 102],
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        # Decomposer was NOT respawned; no new children were created.
        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn((50, "blocked"), gh.label_history)
        # Children + decomposed_at preserved.
        data = gh.pinned_data(50)
        self.assertEqual(data.get("children"), [101, 102])

    def test_half_finished_recovery_with_awaiting_human_holds(self) -> None:
        # If the prior tick parked awaiting_human after partial child
        # creation, the recovery must NOT silently flip the parent to
        # `blocked`; the human's intervention is still required.
        gh = FakeGitHubClient()
        issue = make_issue(51, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            51,
            children=[201],
            awaiting_human=True,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Label NOT flipped; human still owns it.
        self.assertNotIn((51, "blocked"), gh.label_history)
        self.assertTrue(gh.pinned_data(51).get("awaiting_human"))

    def test_partial_children_recovery_parks(self) -> None:
        # SIGKILL between iterations leaves a partial `children` list
        # that the half-finished recovery used to silently treat as
        # complete -- stranding any un-created dependents and never
        # creating the missing children. With `expected_children_count`
        # persisted up-front, the recovery distinguishes partial from
        # complete and parks awaiting human.
        gh = FakeGitHubClient()
        issue = make_issue(52, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            52,
            children=[101],
            expected_children_count=3,
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        # Parked, not finalized to blocked.
        self.assertNotIn((52, "blocked"), gh.label_history)
        data = gh.pinned_data(52)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("1 of 3", last_comment)

    def test_orphan_child_recovery_parks_when_no_children_recorded(
        self,
    ) -> None:
        # SIGKILL between `create_child_issue` returning and the parent's
        # incremental `children` write leaves the parent with
        # `expected_children_count` set but zero recorded children, while
        # an orphan child issue exists on GitHub. The previous recovery
        # branch only fired when `state.get("children")` was truthy, so
        # this case fell through, the decomposer was respawned, and a
        # different manifest produced duplicate child issues alongside
        # the orphan.
        gh = FakeGitHubClient()
        issue = make_issue(53, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            53,
            expected_children_count=2,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertNotIn((53, "blocked"), gh.label_history)
        data = gh.pinned_data(53)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("crashed mid-way", last_comment)
        self.assertIn("0 of 2", last_comment)

    def test_recovery_seeds_missing_parent_number_on_orphan_child(self) -> None:
        # SIGKILL between the parent's child-record write and the child's
        # pinned-state seed for the LAST child satisfies
        # `len(children) == expected_children_count` but leaves that child
        # orphaned (label=blocked, no `parent_number`). A prior
        # `_handle_blocked` tick may have already parked the orphan as
        # "manual relabel suspected" with `awaiting_human=True`. Without
        # repair, recovery finalizes the parent to `blocked`, the parent's
        # walk later flips the orphan to `ready`, and
        # `_handle_implementing` reads the stale park and sits waiting on
        # a human reply that never comes.
        gh = FakeGitHubClient()
        parent = make_issue(60, label="decomposing")
        gh.add_issue(parent)
        # First child seeded normally; second is the orphan.
        child_a = make_issue(601, label="blocked")
        child_b = make_issue(602, label="blocked")
        gh.add_issue(child_a)
        gh.add_issue(child_b)
        gh.seed_state(
            601, parent_number=60, created_at="2026-05-03T00:00:00+00:00",
        )
        gh.seed_state(
            602,
            awaiting_human=True,
            park_reason=None,
            last_action_comment_id=999,
        )
        gh.seed_state(
            60,
            children=[601, 602],
            expected_children_count=2,
            decomposed_at="2026-05-03T00:00:00+00:00",
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        self.assertEqual(gh.created_child_issues, [])
        self.assertIn((60, "blocked"), gh.label_history)
        # Orphan got parent_number seeded and stale park cleared.
        orphan_state = gh.pinned_data(602)
        self.assertEqual(orphan_state.get("parent_number"), 60)
        self.assertFalse(orphan_state.get("awaiting_human"))
        # Healthy child untouched.
        healthy_state = gh.pinned_data(601)
        self.assertEqual(healthy_state.get("parent_number"), 60)

    def test_decompose_split_persists_expected_count_first(self) -> None:
        # `expected_children_count` MUST be on the parent before any
        # child is created on GitHub. Otherwise a SIGKILL after the
        # first child creation leaves `children=[#x]` without an
        # `expected_children_count`, and the recovery (legacy branch)
        # incorrectly finalizes to `blocked`.
        gh = FakeGitHubClient()
        issue = make_issue(82, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"},'
            '{"title": "B", "body": "b"}'
            ']}'
        )

        seen_expected: list[Optional[int]] = []
        real_create = gh.create_child_issue

        def spy_create(**kwargs):
            seen_expected.append(
                gh.pinned_data(82).get("expected_children_count")
            )
            return real_create(**kwargs)

        gh.create_child_issue = spy_create

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        self.assertEqual(seen_expected[0], 2)
        self.assertEqual(gh.pinned_data(82).get("expected_children_count"), 2)

    def test_parent_records_child_before_seeding_child_state(self) -> None:
        # Order matters: parent state records the new child BEFORE the
        # child's pinned state is seeded. Otherwise a SIGKILL between
        # `create_child_issue` returning and the parent write leaves
        # an orphan child (parent doesn't know about it), and the next
        # tick re-spawns the decomposer to create a duplicate.
        gh = FakeGitHubClient()
        issue = make_issue(83, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "split", "children": ['
            '{"title": "A", "body": "a"}'
            ']}'
        )

        # Wrap write_pinned_state so we can observe the order of writes
        # against parent vs child.
        seen_children_before_child_seed: list[list] = []
        real_write = gh.write_pinned_state

        def spy_write(target_issue, state):
            if target_issue.number != 83:
                # Child write -- parent state should already have the
                # child number recorded by now.
                seen_children_before_child_seed.append(
                    list(gh.pinned_data(83).get("children") or [])
                )
            return real_write(target_issue, state)

        gh.write_pinned_state = spy_write

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        # Exactly one child was created and its pinned state was seeded
        # AFTER the parent recorded the child number.
        self.assertEqual(len(seen_children_before_child_seed), 1)
        self.assertEqual(
            len(seen_children_before_child_seed[0]), 1,
            "parent must record the child number before the child's "
            "pinned state is seeded",
        )

    def test_decompose_uses_separate_worktree_from_implementer(self) -> None:
        # The decomposer must NOT taint the implementer's per-issue branch.
        # If it shared `_ensure_worktree`, a `split` decision would leave
        # the local `orchestrator/issue-<n>` branch anchored at the
        # origin/main snapshot the decomposer saw, and the parent's
        # eventual implementer (after children merged to main) would
        # commit on a stale base.
        gh = FakeGitHubClient()
        issue = make_issue(70, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": "fits"}'
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        mocks["_ensure_decompose_worktree"].assert_called_with(_TEST_SPEC, 70)
        mocks["_ensure_worktree"].assert_not_called()
        # Cleanup runs at function exit so the next consumer of issue 70
        # (here _handle_ready -> _handle_implementing on the next tick)
        # starts from a fresh checkout.
        mocks["_cleanup_decompose_worktree"].assert_called_with(_TEST_SPEC, 70)

    def test_decompose_skips_cleanup_on_dirty_park(self) -> None:
        # Operator inspection requires the decomposer's worktree to
        # outlive the dirty/commits park.
        gh = FakeGitHubClient()
        issue = make_issue(71, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest('{"decision": "single", "rationale": "fits"}')

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
            has_new_commits=True,
        )

        self.assertTrue(gh.pinned_data(71).get("awaiting_human"))
        mocks["_cleanup_decompose_worktree"].assert_not_called()

    def test_decompose_skips_cleanup_while_awaiting_human(self) -> None:
        # On the tick AFTER a dirty/commits park, awaiting_human is True
        # and no human reply has arrived yet. The handler must not clean
        # up the decomposer worktree -- the HITL message asks the operator
        # to inspect and reset it, and a subsequent-tick cleanup would
        # silently delete that state out from under them.
        gh = FakeGitHubClient()
        issue = make_issue(73, label="decomposing")
        gh.add_issue(issue)
        gh.seed_state(
            73,
            awaiting_human=True,
            last_action_comment_id=999,
            decomposer_agent="claude",
            decomposer_session_id="dec-sess",
        )

        mocks = self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(),
        )

        mocks["run_agent"].assert_not_called()
        mocks["_cleanup_decompose_worktree"].assert_not_called()

    def test_decompose_handles_non_string_rationale(self) -> None:
        # JSON-valid manifest with a non-string rationale (`[1,2,3]`,
        # `{}`, `42`) must not crash the handler at `.strip()` after
        # the agent already ran. Coerce to the placeholder.
        gh = FakeGitHubClient()
        issue = make_issue(72, label="decomposing")
        gh.add_issue(issue)
        manifest = _manifest(
            '{"decision": "single", "rationale": [1, 2, 3]}'
        )

        self._run(
            lambda: workflow._handle_decomposing(gh, _TEST_SPEC, issue),
            run_agent=_agent(session_id="dec-sess", last_message=manifest),
        )

        self.assertIn((72, "ready"), gh.label_history)
        self.assertFalse(gh.pinned_data(72).get("awaiting_human"))
        rationale_comment = next(
            body for n, body in gh.posted_comments
            if n == 72 and ":mag:" in body
        )
        self.assertIn("(no rationale provided)", rationale_comment)


class HandleReadyTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_handle_ready_routes_to_implementing_same_tick(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(20, label="ready")
        gh.add_issue(issue)

        mocks = self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="implemented"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # Label flips to implementing on the same tick; the dev agent ran
        # and a PR opened.
        self.assertEqual(gh.label_history[0], (20, "implementing"))
        mocks["run_agent"].assert_called_once()
        self.assertEqual(len(gh.opened_prs), 1)
        # pickup_comment_id seeded so the validating handoff can anchor
        # the in_review watermark seed on it.
        data = gh.pinned_data(20)
        self.assertIn("pickup_comment_id", data)
        self.assertIn("created_at", data)

    def test_handle_ready_keeps_existing_pickup_state(self) -> None:
        # If pickup state was already seeded (e.g. by a re-tick after the
        # legacy pickup path), don't double-post the picking-this-up
        # comment.
        gh = FakeGitHubClient()
        issue = make_issue(21, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            21,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        before = len(gh.posted_comments)
        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        # The "picking this up; starting implementation" comment was NOT
        # re-posted. (`_on_commits` still posts a `:sparkles:` comment.)
        new_comments = gh.posted_comments[before:]
        self.assertFalse(any(
            "picking this up" in body for _, body in new_comments
        ))

    def test_handle_ready_marks_pre_existing_comments_consumed(self) -> None:
        # A parent that came through `decomposing` -> `blocked` ->
        # all-children-done -> `ready` carries a `pickup_comment_id`
        # anchored on the original "decomposing" comment. Any human
        # feedback posted while children were resolving sits at a
        # comment id ABOVE pickup, so the in_review watermark seed
        # would classify it as post-pickup unconsumed PR feedback and
        # bounce the PR back to validating after the implementer has
        # already incorporated it. _handle_ready must bump
        # `last_action_comment_id` past the latest visible comment so
        # `_seed_watermark_past_self`'s `consumed_through` walk treats
        # those decomposing/blocked-era comments as already-fed-to-the-dev.
        gh = FakeGitHubClient()
        issue = make_issue(22, label="ready")
        # Decomposing-era human comment -- id well above the original
        # pickup comment id.
        issue.comments.append(FakeComment(
            id=2050, body="please use snake_case",
            user=FakeUser("alice"),
        ))
        gh.add_issue(issue)
        gh.seed_state(
            22,
            pickup_comment_id=500,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        data = gh.pinned_data(22)
        last_action = data.get("last_action_comment_id")
        self.assertIsNotNone(
            last_action,
            "last_action_comment_id must be set so the in_review "
            "handoff treats decomposing-era comments as consumed",
        )
        self.assertGreaterEqual(int(last_action), 2050)

    def test_handle_ready_does_not_lower_existing_last_action(self) -> None:
        # If a prior decomposing park already advanced
        # `last_action_comment_id` past everything, _handle_ready must
        # not regress it. Latest comment id might be smaller than the
        # park id when the latest is the orchestrator's own pinned-state
        # comment from a fresh seed (low id) and the prior park id was
        # higher.
        gh = FakeGitHubClient()
        issue = make_issue(23, label="ready")
        gh.add_issue(issue)
        gh.seed_state(
            23,
            pickup_comment_id=500,
            last_action_comment_id=9999,
            created_at="2026-05-03T00:00:00+00:00",
        )

        self._run(
            lambda: workflow._handle_ready(gh, _TEST_SPEC, issue),
            run_agent=_agent(
                session_id="dev-sess", last_message="done"
            ),
            has_new_commits=[False, True],
            push_branch=True,
        )

        data = gh.pinned_data(23)
        self.assertGreaterEqual(int(data["last_action_comment_id"]), 9999)


class HandleBlockedTest(unittest.TestCase, _PatchedWorkflowMixin):
    def _seed_parent_with_children(
        self,
        *,
        parent_number: int,
        child_labels: list[Optional[str]],
        dep_graph: Optional[dict] = None,
    ) -> tuple[FakeGitHubClient, FakeIssue, list[FakeIssue]]:
        gh = FakeGitHubClient()
        parent = make_issue(parent_number, label="blocked")
        gh.add_issue(parent)
        children: list[FakeIssue] = []
        for i, lbl in enumerate(child_labels):
            child = make_issue(parent_number * 10 + i + 1, label=lbl)
            gh.add_issue(child)
            children.append(child)
        seed = {
            "children": [c.number for c in children],
            "decomposer_agent": "claude",
            "decomposer_session_id": "dec-sess",
        }
        if dep_graph is not None:
            seed["dep_graph"] = dep_graph
        gh.seed_state(parent_number, **seed)
        return gh, parent, children

    def test_all_children_done_flips_parent_to_ready(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=30, child_labels=["done", "done"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((30, "ready"), gh.label_history)
        self.assertTrue(any(
            "all children resolved" in body
            for _, body in gh.posted_comments
        ))

    def test_some_children_in_progress_no_op(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=31,
            child_labels=["done", "implementing"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # No label flip on parent and no comment posted on the parent.
        self.assertNotIn((31, "ready"), gh.label_history)
        self.assertEqual(
            [b for n, b in gh.posted_comments if n == 31], [],
        )

    def test_rejected_child_parks_parent(self) -> None:
        gh, parent, children = self._seed_parent_with_children(
            parent_number=32,
            child_labels=["done", "rejected"],
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(32)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("rejected", last_comment)
        self.assertIn(f"#{children[1].number}", last_comment)

    def test_manually_closed_child_parks_parent(self) -> None:
        # A child closed manually (e.g. via the GitHub UI) before
        # reaching `in_review` is invisible to `list_pollable_issues`
        # (which only sweeps closed issues for `in_review`). Its
        # workflow label stays frozen, so without this branch the
        # parent reads the stale label, neither the rejected nor the
        # all-done branch fires, and the parent waits forever for a
        # child that is gone. Park it for human adjudication, exactly
        # like a rejected child.
        gh = FakeGitHubClient()
        parent = make_issue(40, label="blocked")
        gh.add_issue(parent)
        # children[0]: properly done -- closed with label `done`.
        done_child = make_issue(401, label="done")
        done_child.closed = True
        gh.add_issue(done_child)
        # children[1]: manually closed mid-implementation. Label stays
        # `implementing` because no orchestrator transition closed it.
        closed_child = make_issue(402, label="implementing")
        closed_child.closed = True
        gh.add_issue(closed_child)
        gh.seed_state(40, children=[401, 402])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(40)
        self.assertTrue(data.get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("closed without reaching", last_comment)
        self.assertIn("#402", last_comment)
        # Crucially: the parent must NOT have flipped to `ready`. With
        # only the all-done branch, the manually-closed child carrying
        # a non-"done" label correctly fails the `all(lbl == "done")`
        # check; but if a future change lowered that bar (e.g. "all
        # closed"), this assertion would catch the regression.
        self.assertNotIn((40, "ready"), gh.label_history)

    def test_closed_in_review_child_does_not_falsely_park_parent(
        self,
    ) -> None:
        # state=closed + label=in_review is the externally-merged
        # transient: the closed-in_review sweep in
        # `list_pollable_issues` picks the child up next tick and
        # `_handle_in_review` finalizes it to done/rejected. The
        # blocked parent must NOT pre-empt that finalization with a
        # manual-close park -- treating this as a manual override
        # would strand legitimately externally-merged children.
        gh = FakeGitHubClient()
        parent = make_issue(41, label="blocked")
        gh.add_issue(parent)
        in_review_child = make_issue(411, label="in_review")
        in_review_child.closed = True
        gh.add_issue(in_review_child)
        other_child = make_issue(412, label="implementing")
        gh.add_issue(other_child)
        gh.seed_state(41, children=[411, 412])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(41)
        self.assertFalse(data.get("awaiting_human"))
        # Parent stays `blocked`: no `ready` flip while other_child is
        # still implementing, and no manual-close park comment posted.
        self.assertNotIn((41, "ready"), gh.label_history)
        self.assertFalse(any(
            "closed without reaching" in body
            for n, body in gh.posted_comments if n == 41
        ))

    def test_manually_closed_child_with_no_label_parks_parent(self) -> None:
        # Defensive corner: a child with no workflow label at all
        # (e.g. a label was manually stripped before the issue was
        # closed) is also invisible to the closed-in_review sweep.
        # The "manually closed" branch must catch it -- otherwise the
        # parent would still wait forever.
        gh = FakeGitHubClient()
        parent = make_issue(42, label="blocked")
        gh.add_issue(parent)
        unlabeled_closed = make_issue(421, label=None)
        unlabeled_closed.closed = True
        gh.add_issue(unlabeled_closed)
        gh.seed_state(42, children=[421])

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(42)
        self.assertTrue(data.get("awaiting_human"))
        self.assertTrue(any(
            "closed without reaching" in body and "#421" in body
            for _, body in gh.posted_comments
        ))

    def test_unblocks_middle_child_when_dep_done(self) -> None:
        # children[0] is done; children[1] depends on [0] and is currently
        # blocked. Next blocked tick must relabel children[1] to `ready`.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=33,
            child_labels=["done", "blocked"],
            dep_graph={"1": [0]},
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # children[1] flipped to ready by the dep-graph walk; parent
        # stays blocked because children[1] is not yet done.
        flipped = [
            new for issue_n, new in gh.label_history
            if issue_n == children[1].number
        ]
        self.assertEqual(flipped, ["ready"])
        self.assertNotIn((33, "ready"), gh.label_history)

    def test_blocked_with_no_recorded_children_parks(self) -> None:
        gh = FakeGitHubClient()
        parent = make_issue(34, label="blocked")
        gh.add_issue(parent)
        # No children pinned.
        gh.seed_state(34, decomposer_agent="claude")

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        data = gh.pinned_data(34)
        self.assertTrue(data.get("awaiting_human"))

    def test_blocked_child_with_parent_number_is_noop(self) -> None:
        # A dependency-blocked child created by the decomposer carries
        # `parent_number` in its pinned state but no `children` of its
        # own. Polling routes it through `_handle_blocked`, which must
        # leave it alone -- the parent's dep-graph walk is what
        # eventually relabels it `ready`. Without the parent_number
        # branch this would park the child as "manual relabel suspected"
        # and leave `awaiting_human=True` behind, which would then
        # corrupt the implementation phase once the parent unblocks it.
        gh = FakeGitHubClient()
        child = make_issue(35, label="blocked")
        gh.add_issue(child)
        gh.seed_state(35, parent_number=30)

        before_comments = list(gh.posted_comments)
        before_labels = list(gh.label_history)

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, child),
            run_agent=_agent(),
        )

        data = gh.pinned_data(35)
        self.assertFalse(data.get("awaiting_human"))
        self.assertEqual(gh.posted_comments, before_comments)
        self.assertEqual(gh.label_history, before_labels)

    def test_no_dep_blocked_child_flipped_to_ready_by_walk(self) -> None:
        # Activation-recovery path: a no-dep child got stuck as `blocked`
        # because the decomposer's same-tick activation step crashed
        # (network blip etc.). The parent's `_handle_blocked` walk must
        # treat empty deps as deps-satisfied and flip the child to
        # `ready` so implementation can start.
        gh, parent, children = self._seed_parent_with_children(
            parent_number=36,
            child_labels=["blocked", "blocked"],
            # No dep_graph -- both children have no recorded deps.
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        # Both children flipped to `ready`. Parent stays `blocked`
        # because no children are `done` yet.
        for child in children:
            flipped = [
                new for issue_n, new in gh.label_history
                if issue_n == child.number
            ]
            self.assertEqual(flipped, ["ready"])
        self.assertNotIn((36, "ready"), gh.label_history)

    def test_blocked_clears_awaiting_human_after_all_done(self) -> None:
        # A prior tick parked the parent on `awaiting_human=True` because
        # one child was `rejected`. The operator fixed the rejection
        # off-band; eventually all children become `done`. The parent
        # flip to `ready` MUST clear the stale park so
        # `_handle_implementing` (next tick) starts a fresh implementer
        # run rather than routing through `_resume_developer_on_human_reply`
        # and either replaying long-stale comments or sitting silent.
        gh = FakeGitHubClient()
        parent = make_issue(38, label="blocked")
        gh.add_issue(parent)
        child_a = make_issue(381, label="done")
        child_b = make_issue(382, label="done")
        gh.add_issue(child_a)
        gh.add_issue(child_b)
        gh.seed_state(
            38,
            children=[381, 382],
            awaiting_human=True,
            park_reason="rejected_child",
            last_action_comment_id=999,
        )

        self._run(
            lambda: workflow._handle_blocked(gh, _TEST_SPEC, parent),
            run_agent=_agent(),
        )

        self.assertIn((38, "ready"), gh.label_history)
        data = gh.pinned_data(38)
        self.assertFalse(data.get("awaiting_human"))
        self.assertIsNone(data.get("park_reason"))


class CreateChildIssueAlwaysUsesParentRepoTest(unittest.TestCase):
    """`create_child_issue` is structurally bound to `self.repo` so a
    misuse cannot accidentally file a child against a different repo
    than the parent. Worth a regression test anyway.
    """

    def test_calls_self_repo_create_issue_with_parent_link(self) -> None:
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        sentinel = MagicMock(name="created_issue")
        client.repo.create_issue.return_value = sentinel

        out = client.create_child_issue(
            title="A", body="do A", parent_number=42, labels=["ready"],
        )

        self.assertIs(out, sentinel)
        client.repo.create_issue.assert_called_once()
        kwargs = client.repo.create_issue.call_args.kwargs
        self.assertEqual(kwargs["title"], "A")
        self.assertEqual(kwargs["labels"], ["ready"])
        # Parent link prepended via the helper (not by the caller) so the
        # workflow code can hand the agent's raw body straight in.
        self.assertIn("Parent: #42", kwargs["body"])


class WorktreePathSlugNamespaceTest(unittest.TestCase):
    """Two repos with the same issue number must produce distinct worktree
    paths, otherwise simultaneous orchestration of both would have them
    fighting over the same `WORKTREES_DIR/issue-N` checkout. The slug
    sanitizer also has to produce a single filesystem-safe segment
    (no `/`, no leading `.`) since it becomes a directory name.
    """

    def _spec(self, slug: str) -> config.RepoSpec:
        return config.RepoSpec(
            slug=slug,
            target_root=Path(f"/tmp/{workflow._sanitize_slug(slug)}-target"),
            base_branch="main",
        )

    def test_same_issue_number_different_slugs_no_collision(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        path_a = workflow._worktree_path(spec_a, 7)
        path_b = workflow._worktree_path(spec_b, 7)

        self.assertNotEqual(path_a, path_b)
        # Both must live under WORKTREES_DIR with the issue-N leaf.
        self.assertEqual(path_a.name, "issue-7")
        self.assertEqual(path_b.name, "issue-7")
        self.assertEqual(path_a.parent.parent, config.WORKTREES_DIR)
        self.assertEqual(path_b.parent.parent, config.WORKTREES_DIR)

    def test_decompose_path_also_namespaced_by_slug(self) -> None:
        spec_a = self._spec("alice/repo")
        spec_b = self._spec("bob/repo")
        self.assertNotEqual(
            workflow._decompose_worktree_path(spec_a, 7),
            workflow._decompose_worktree_path(spec_b, 7),
        )

    def test_implement_and_decompose_share_repo_namespace(self) -> None:
        # `WORKTREES_DIR/<slug>/issue-N` and `WORKTREES_DIR/<slug>/decompose-N`
        # share the per-repo subdirectory so cleanup on the parent dir
        # also reaps the decomposer scratch.
        spec = self._spec("owner/name")
        impl = workflow._worktree_path(spec, 11)
        dec = workflow._decompose_worktree_path(spec, 11)
        self.assertEqual(impl.parent, dec.parent)

    def test_sanitize_slug_replaces_owner_separator(self) -> None:
        self.assertEqual(workflow._sanitize_slug("owner/name"), "owner__name")

    def test_sanitize_slug_is_a_single_segment(self) -> None:
        # A directory name with `/` would split into nested directories,
        # defeating the point of namespacing.
        for raw in (
            "owner/name",
            "deep/owner/name",
            "name-only",
            "weird name with spaces",
        ):
            cleaned = workflow._sanitize_slug(raw)
            self.assertNotIn("/", cleaned, f"slug={raw!r} -> {cleaned!r}")

    def test_sanitize_slug_no_leading_dot(self) -> None:
        # Hidden directories (.foo) hide the worktree from a casual
        # operator inspection; escape leading dots.
        self.assertFalse(workflow._sanitize_slug(".dotfile/repo").startswith("."))
        self.assertFalse(workflow._sanitize_slug("./repo").startswith("."))

    def test_sanitize_slug_strips_unsafe_chars(self) -> None:
        cleaned = workflow._sanitize_slug("owner@#$/name with spaces")
        # No path separator, no shell-special chars; only [A-Za-z0-9_.-]
        for ch in cleaned:
            self.assertTrue(
                ch.isalnum() or ch in "_.-",
                f"unexpected char {ch!r} in {cleaned!r}",
            )

    def test_sanitize_slug_empty_input_falls_back(self) -> None:
        # Empty would collapse `WORKTREES_DIR/<slug>/issue-N` into
        # `WORKTREES_DIR/issue-N`, reintroducing the cross-repo collision.
        self.assertNotEqual(workflow._sanitize_slug(""), "")
        self.assertNotEqual(workflow._sanitize_slug(""), ".")

    def test_default_repo_spec_path_format(self) -> None:
        # Anchor the documented `<owner>__<name>/issue-N` layout.
        spec = config.RepoSpec(
            slug="geserdugarov/agent-orchestrator",
            target_root=Path("/tmp/x"),
            base_branch="main",
        )
        path = workflow._worktree_path(spec, 9)
        self.assertEqual(
            path,
            config.WORKTREES_DIR / "geserdugarov__agent-orchestrator" / "issue-9",
        )

class CleanupMergedBranchTest(unittest.TestCase):
    """Direct coverage of `_cleanup_merged_branch`. The handler-level tests
    patch this helper out so they only check it was invoked; here we run
    the real implementation with `_git` mocked to verify the worktree
    removal, local branch delete, and remote branch delete each fire (and
    that an absent worktree is silently skipped instead of erroring).
    """

    ISSUE_NUMBER = 99
    BRANCH = "orchestrator/issue-99"

    def _run_helper(
        self,
        *,
        worktree_exists: bool,
        local_branch_exists: bool,
    ):
        from unittest.mock import MagicMock
        gh = FakeGitHubClient()

        rev_parse_rc = 0 if local_branch_exists else 1

        def fake_git(*args, cwd):
            cmd = args[0]
            if cmd == "worktree":
                return MagicMock(returncode=0, stderr="", stdout="")
            if cmd == "rev-parse":
                return MagicMock(returncode=rev_parse_rc, stderr="", stdout="")
            if cmd == "branch":
                return MagicMock(returncode=0, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        git_mock = MagicMock(side_effect=fake_git)

        # `_worktree_path` returns a Path that may or may not exist on disk;
        # patch its existence check rather than touching the real filesystem.
        wt_path = MagicMock()
        wt_path.exists.return_value = worktree_exists
        wt_path.__str__ = lambda self: f"/tmp/issue-{CleanupMergedBranchTest.ISSUE_NUMBER}"

        with patch.object(workflow, "_git", git_mock), \
             patch.object(workflow, "_worktree_path", return_value=wt_path):
            workflow._cleanup_merged_branch(gh, _TEST_SPEC, self.ISSUE_NUMBER)
        return gh, git_mock

    def test_full_cleanup_runs_all_three_steps(self) -> None:
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=True,
        )

        # Worktree remove issued first, then rev-parse to probe the local
        # branch, then `branch -D`. The remote-side delete recorder confirms
        # gh.delete_remote_branch was called with the per-issue branch.
        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertEqual(
            cmds[:3],
            ["worktree", "rev-parse", "branch"],
        )
        # The branch -D invocation targets the per-issue branch by name.
        branch_call = next(
            c for c in git_mock.call_args_list if c.args[0] == "branch"
        )
        self.assertEqual(branch_call.args[1], "-D")
        self.assertEqual(branch_call.args[2], self.BRANCH)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_worktree_remove_when_worktree_absent(self) -> None:
        # Worktree may already be gone if the operator cleaned it up by hand
        # or a prior tick removed it. Helper should still drop the local
        # branch and request the remote delete instead of erroring out.
        gh, git_mock = self._run_helper(
            worktree_exists=False, local_branch_exists=True,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertNotIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])

    def test_skips_local_delete_when_branch_absent(self) -> None:
        # Branch may already be gone if a previous cleanup partly succeeded
        # or the operator pruned it. We must not run `branch -D` (it would
        # fail loudly), but must still request the remote delete.
        gh, git_mock = self._run_helper(
            worktree_exists=True, local_branch_exists=False,
        )

        cmds = [c.args[0] for c in git_mock.call_args_list]
        self.assertIn("worktree", cmds)
        self.assertIn("rev-parse", cmds)
        self.assertNotIn("branch", cmds)
        self.assertEqual(gh.deleted_remote_branches, [self.BRANCH])


class DeleteRemoteBranchTest(unittest.TestCase):
    """`GitHubClient.delete_remote_branch` is idempotent against a 404
    because the repo's "auto-delete head branches" setting may have
    already removed the ref as part of the merge. Other failures log
    and return False so the caller can keep going.
    """

    def _client_with_ref(self, *, raise_status):
        from unittest.mock import MagicMock
        from orchestrator.github import GitHubClient
        from github import GithubException

        client = GitHubClient.__new__(GitHubClient)
        client.repo = MagicMock()
        if raise_status is None:
            client.repo.get_git_ref.return_value = MagicMock()
        else:
            err = GithubException(status=raise_status, data={"message": "x"})
            client.repo.get_git_ref.return_value = MagicMock()
            client.repo.get_git_ref.return_value.delete.side_effect = err
        return client

    def test_success(self) -> None:
        client = self._client_with_ref(raise_status=None)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))
        client.repo.get_git_ref.assert_called_once_with(
            "heads/orchestrator/issue-1"
        )

    def test_404_treated_as_success(self) -> None:
        client = self._client_with_ref(raise_status=404)
        self.assertTrue(client.delete_remote_branch("orchestrator/issue-1"))

    def test_other_error_returns_false(self) -> None:
        client = self._client_with_ref(raise_status=403)
        self.assertFalse(client.delete_remote_branch("orchestrator/issue-1"))


if __name__ == "__main__":
    unittest.main()
