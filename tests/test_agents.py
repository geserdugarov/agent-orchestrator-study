# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agents import (
    _claude_last_message,
    _run_claude,
    _run_codex,
    parse_session_id,
    run_agent,
)


_CWD = Path("/tmp/agent-orchestrator-test-cwd-doesnt-matter")


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    # _run_subprocess uses Popen + communicate(timeout=...). The mock returns
    # (stdout, stderr) from communicate and exposes .returncode -- enough to
    # let tests assert on argv passed to Popen without spawning anything.
    proc = MagicMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    proc.pid = 12345
    return proc


class ParseSessionIdTest(unittest.TestCase):
    def test_codex_jsonl_session_id(self) -> None:
        # Codex's --json output has session_id at varied paths; the walker
        # picks any UUID at a known key, anywhere in the tree.
        line = json.dumps({
            "type": "task_started",
            "session_id": "11111111-2222-3333-4444-555555555555",
        })
        self.assertEqual(
            parse_session_id(line),
            "11111111-2222-3333-4444-555555555555",
        )

    def test_claude_stream_json_session_id(self) -> None:
        # Claude's stream-json puts session_id on the system/init event and
        # on most subsequent events; a top-level UUID at session_id is the
        # documented surface.
        events = [
            json.dumps({
                "type": "system",
                "subtype": "init",
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "tools": [],
            }),
            json.dumps({
                "type": "assistant",
                "session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "message": {"role": "assistant", "content": []},
            }),
        ]
        self.assertEqual(
            parse_session_id("\n".join(events)),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )

    def test_no_uuid_returns_none(self) -> None:
        self.assertIsNone(parse_session_id('{"type":"banner","msg":"hello"}'))

    def test_skips_unparseable_lines(self) -> None:
        out = (
            "not-json\n"
            + json.dumps({"session_id": "12341234-1234-1234-1234-123412341234"})
        )
        self.assertEqual(
            parse_session_id(out),
            "12341234-1234-1234-1234-123412341234",
        )


class ClaudeLastMessageTest(unittest.TestCase):
    def test_prefers_terminal_result_event(self) -> None:
        events = [
            json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": "thinking..."}],
            }}),
            json.dumps({
                "type": "result",
                "subtype": "success",
                "result": "final answer",
            }),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "final answer")

    def test_falls_back_to_assistant_text_when_no_result(self) -> None:
        events = [
            json.dumps({"type": "assistant", "message": {
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ],
            }}),
        ]
        self.assertEqual(_claude_last_message("\n".join(events)), "hello world")

    def test_returns_empty_when_no_recognizable_events(self) -> None:
        self.assertEqual(_claude_last_message(""), "")
        self.assertEqual(
            _claude_last_message('{"type":"system","subtype":"init"}'),
            "",
        )


class RunAgentDispatchTest(unittest.TestCase):
    def test_unknown_backend_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as cm:
            run_agent("gemini", "prompt", _CWD)
        self.assertIn("gemini", str(cm.exception))

    def test_dispatches_to_codex(self) -> None:
        # Use stream-json-shaped output so parse_session_id has something to
        # find; the codex runner doesn't care about claude shape.
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(stdout=json.dumps({"session_id": sid})),
        ) as run_mock:
            result = run_agent("codex", "p", _CWD)
        self.assertEqual(result.session_id, sid)
        self.assertEqual(result.exit_code, 0)
        argv = run_mock.call_args.args[0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(argv[1], "exec")

    def test_dispatches_to_claude(self) -> None:
        sid = "cafe1234-5678-90ab-cdef-1234567890ab"
        events = [
            json.dumps({"type": "system", "session_id": sid}),
            json.dumps({"type": "result", "result": "shipped"}),
        ]
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(stdout="\n".join(events)),
        ) as run_mock:
            result = run_agent("claude", "p", _CWD)
        self.assertEqual(result.session_id, sid)
        self.assertEqual(result.last_message, "shipped")
        argv = run_mock.call_args.args[0]
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("-p", argv)
        self.assertIn("--output-format", argv)


class RunCodexEnvScrubTest(unittest.TestCase):
    def test_github_credentials_are_stripped(self) -> None:
        # The agent must never see GITHUB_TOKEN (or any synonym); the
        # orchestrator owns all GitHub writes. Provider auth keys
        # (ANTHROPIC_API_KEY, OPENAI_*) must NOT be stripped -- those are how
        # the agent talks to its own model.
        env = {
            "GITHUB_TOKEN": "ghp_secret",
            "GH_TOKEN": "ghp_alt",
            "ANTHROPIC_API_KEY": "sk-keep-me",
            "PATH": "/usr/bin",
        }
        with patch.dict("os.environ", env, clear=True), patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", _CWD)
        passed_env = run_mock.call_args.kwargs["env"]
        self.assertNotIn("GITHUB_TOKEN", passed_env)
        self.assertNotIn("GH_TOKEN", passed_env)
        self.assertEqual(passed_env.get("ANTHROPIC_API_KEY"), "sk-keep-me")


class RunCodexCwdTest(unittest.TestCase):
    def test_dash_C_receives_absolute_path_for_relative_cwd(self) -> None:
        # codex applies `-C` AFTER it has already chdir'd into the subprocess
        # cwd, so a relative path resolves twice and codex hits "No such file
        # or directory (os error 2)". Pinning this guarantees the path passed
        # to `-C` is absolute even when WORKTREES_DIR (and the worktree path
        # derived from it) is relative.
        rel_cwd = Path("../wt-orchestrator/foo/issue-1")
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_codex("p", rel_cwd)
        argv = run_mock.call_args.args[0]
        c_value = argv[argv.index("-C") + 1]
        self.assertTrue(
            Path(c_value).is_absolute(),
            f"-C path should be absolute, got {c_value!r}",
        )
        self.assertEqual(Path(c_value), rel_cwd.resolve())


class RunClaudeResumeTest(unittest.TestCase):
    def test_resume_passes_resume_session_id_arg(self) -> None:
        sid = "deadbeef-1234-1234-1234-1234deadbeef"
        with patch(
            "orchestrator.agents.subprocess.Popen",
            return_value=_completed(),
        ) as run_mock:
            _run_claude("followup", _CWD, resume_session_id=sid)
        argv = run_mock.call_args.args[0]
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)


if __name__ == "__main__":
    unittest.main()
