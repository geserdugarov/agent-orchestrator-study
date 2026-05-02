from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestrator import config, workflow
from orchestrator.agents import CodexResult
from orchestrator.workflow import _parse_review_verdict

from tests.fakes import (
    FakeComment,
    FakeGitHubClient,
    FakeUser,
    make_issue,
)


_FAKE_WT = Path("/tmp/orchestrator-test-wt-doesnt-matter")


def _codex(
    *,
    session_id: str = "sess-1",
    last_message: str = "",
    timed_out: bool = False,
) -> CodexResult:
    return CodexResult(
        session_id=session_id,
        last_message=last_message,
        exit_code=-1 if timed_out else 0,
        timed_out=timed_out,
        stdout="",
        stderr="",
    )


def _patch_workflow(
    *,
    run_codex,
    has_new_commits=False,
    dirty_files=(),
    push_branch=True,
    head_shas=("",),
):
    """Apply the standard set of monkeypatches to orchestrator.workflow.

    `run_codex` and `head_shas` accept either a single value or an iterable
    used as a side_effect sequence (for tests that span multiple codex spawns
    or before/after SHA reads in one tick).
    """
    return patch.multiple(
        workflow,
        run_codex=_as_mock(run_codex),
        _ensure_worktree=patch.DEFAULT,
        _has_new_commits=patch.DEFAULT,
        _worktree_dirty_files=patch.DEFAULT,
        _push_branch=patch.DEFAULT,
        _head_sha=patch.DEFAULT,
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
        run_codex,
        has_new_commits=False,
        dirty_files=(),
        push_branch=True,
        head_shas=("",),
    ):
        from unittest.mock import MagicMock

        rc_mock = _as_mock(run_codex)
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

        with patch.object(workflow, "run_codex", rc_mock), \
             patch.object(workflow, "_ensure_worktree", wt_mock), \
             patch.object(workflow, "_has_new_commits", hnc_mock), \
             patch.object(workflow, "_worktree_dirty_files", df_mock), \
             patch.object(workflow, "_push_branch", push_mock), \
             patch.object(workflow, "_head_sha", head_mock):
            callable_()

        return {
            "run_codex": rc_mock,
            "_ensure_worktree": wt_mock,
            "_has_new_commits": hnc_mock,
            "_worktree_dirty_files": df_mock,
            "_push_branch": push_mock,
            "_head_sha": head_mock,
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


class HandlePickupTest(unittest.TestCase, _PatchedWorkflowMixin):
    def test_pickup_posts_comment_sets_label_writes_state_then_implements(self) -> None:
        gh = FakeGitHubClient()
        issue = make_issue(1)
        gh.add_issue(issue)

        mocks = self._run(
            lambda: workflow._handle_pickup(gh, issue),
            run_codex=_codex(last_message="need clarification"),
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
        mocks["run_codex"].assert_called_once()


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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-1", last_message="implemented"),
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
        self.assertEqual(data["codex_session_id"], "sess-1")
        self.assertEqual(data["review_round"], 0)

    def test_commits_with_dirty_tree_parks_without_pushing(self) -> None:
        gh, issue = self._seeded()
        dirty = [f"file_{i}.py" for i in range(15)]
        mocks = self._run(
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(last_message="commit done but more work pending"),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(last_message="What database should I use?"),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(timed_out=True),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-1", last_message="done"),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(),
        )

        mocks["run_codex"].assert_not_called()
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-old", last_message="ok"),
            # awaiting_human path skips the recovered-worktree probe; only
            # the post-codex commit check runs.
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_codex"].assert_called_once()
        _, kwargs = mocks["run_codex"].call_args
        self.assertEqual(kwargs.get("resume_session_id"), "sess-old")
        followup_arg = mocks["run_codex"].call_args.args[0]
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(),
            has_new_commits=True,
            dirty_files=(),
            push_branch=True,
        )

        mocks["run_codex"].assert_not_called()
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-1", last_message="done"),
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
            lambda: workflow._handle_validating(gh, issue),
            run_codex=_codex(last_message="LGTM\n\nVERDICT: APPROVED"),
        )

        self.assertEqual(mocks["run_codex"].call_count, 1)
        self.assertIn((5, "in_review"), gh.label_history)
        self.assertTrue(any(
            ":white_check_mark: codex review approved" in body
            for _, body in gh.posted_pr_comments
        ))

    def test_changes_requested_resumes_dev_increments_round(self) -> None:
        gh, issue = self._seeded()
        review = _codex(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )
        dev_fix = _codex(session_id="dev-sess", last_message="fixed")

        mocks = self._run(
            lambda: workflow._handle_validating(gh, issue),
            run_codex=[review, dev_fix],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        self.assertEqual(mocks["run_codex"].call_count, 2)
        # Second call (dev fix) must resume the developer session.
        _, second_kwargs = mocks["run_codex"].call_args_list[1]
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
            lambda: workflow._handle_validating(gh, issue),
            run_codex=_codex(last_message="I'm not sure what to think"),
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
            lambda: workflow._handle_validating(gh, issue),
            run_codex=_codex(timed_out=True),
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
        return _codex(
            session_id="rev-sess",
            last_message="1. Fix typo\n\nVERDICT: CHANGES_REQUESTED",
        )

    def test_dev_fix_no_new_commit_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, issue),
            run_codex=[
                self._changes_requested_review(),
                _codex(session_id="dev-sess", last_message="why?"),
            ],
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "aaa"],
        )

        mocks["_push_branch"].assert_not_called()
        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("agent needs your input", last_comment)

    def test_dev_fix_dirty_parks_round_unchanged(self) -> None:
        gh, issue = self._seeded()
        mocks = self._run(
            lambda: workflow._handle_validating(gh, issue),
            run_codex=[
                self._changes_requested_review(),
                _codex(session_id="dev-sess", last_message="partial"),
            ],
            dirty_files=["leftover.py"],
            push_branch=True,
            head_shas=["aaa", "bbb"],
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
            lambda: workflow._handle_validating(gh, issue),
            run_codex=[
                self._changes_requested_review(),
                _codex(session_id="dev-sess", last_message="fixed"),
            ],
            dirty_files=(),
            push_branch=False,
            head_shas=["aaa", "bbb"],
        )

        self.assertEqual(gh.pinned_data(6).get("review_round"), 0)
        self.assertTrue(gh.pinned_data(6).get("awaiting_human"))
        last_comment = gh.posted_comments[-1][1]
        self.assertIn("git push failed", last_comment)

    def test_review_round_at_cap_parks_without_spawning_reviewer(self) -> None:
        gh, issue = self._seeded(review_round=config.MAX_REVIEW_ROUNDS)
        mocks = self._run(
            lambda: workflow._handle_validating(gh, issue),
            run_codex=_codex(),
        )

        mocks["run_codex"].assert_not_called()
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
            lambda: workflow._handle_validating(gh, issue),
            run_codex=_codex(session_id="dev-sess", last_message="fixed"),
            dirty_files=(),
            push_branch=True,
            head_shas=["aaa", "bbb"],
        )

        # Only the dev resume runs this tick; the reviewer fires on the next.
        self.assertEqual(mocks["run_codex"].call_count, 1)
        _, kwargs = mocks["run_codex"].call_args
        self.assertEqual(kwargs.get("resume_session_id"), "dev-sess")
        followup = mocks["run_codex"].call_args.args[0]
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
        # the fourth tick parks before run_codex is called. Cap is 3/day.
        gh, issue = self._seeded()

        # First three ticks: codex returns no commits + a question, parking on
        # awaiting_human. Each tick consumes one retry from the budget.
        for tick in range(3):
            self._run(
                lambda: workflow._handle_implementing(gh, issue),
                run_codex=_codex(last_message=f"q{tick}"),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(last_message="should not run"),
            has_new_commits=False,
        )

        mocks["run_codex"].assert_not_called()
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-1", last_message="done"),
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(last_message="ask again"),
            has_new_commits=False,
        )

        mocks["run_codex"].assert_called_once()
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
            lambda: workflow._handle_implementing(gh, issue),
            run_codex=_codex(session_id="sess-old", last_message="ok"),
            has_new_commits=[True],
            dirty_files=(),
            push_branch=True,
        )

        # Resume happened (codex was called once with the followup comment).
        mocks["run_codex"].assert_called_once()
        # retry_count NOT incremented by the resume itself. The successful
        # _on_commits then clears it to 0.
        data = gh.pinned_data(9)
        self.assertEqual(data.get("retry_count"), 0)


def _iso_hours_ago(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


if __name__ == "__main__":
    unittest.main()
