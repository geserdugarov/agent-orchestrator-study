from __future__ import annotations

import unittest

from orchestrator.workflow import _parse_review_verdict


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
        # Real codex output isn't always disciplined about putting the marker
        # on its own line; a trailing inline marker should still resolve.
        self.assertEqual(
            _parse_review_verdict("All good. VERDICT: APPROVED"),
            ("approved", "All good."),
        )

    def test_case_insensitive(self) -> None:
        verdict, _ = _parse_review_verdict("verdict: approved")
        self.assertEqual(verdict, "approved")

    def test_last_marker_wins(self) -> None:
        # Reviewer wavered, then concluded with CHANGES_REQUESTED -- the final
        # word is what counts.
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


if __name__ == "__main__":
    unittest.main()
