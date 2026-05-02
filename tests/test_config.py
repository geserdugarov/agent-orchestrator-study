from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch


class HitlHandleConfigTest(unittest.TestCase):
    def _load_config(self, hitl_handle: str):
        env = {
            "HITL_HANDLE": hitl_handle,
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-study-token-missing",
        }
        with patch.dict(os.environ, env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_formats_comma_separated_handles_as_mentions(self) -> None:
        config = self._load_config("alice,bob")

        self.assertEqual(config.HITL_HANDLES, ("alice", "bob"))
        self.assertEqual(config.HITL_HANDLE, "alice,bob")
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob")

    def test_strips_whitespace_at_signs_and_duplicates(self) -> None:
        config = self._load_config(" @alice, bob, ,alice,@carol ")

        self.assertEqual(config.HITL_HANDLES, ("alice", "bob", "carol"))
        self.assertEqual(config.HITL_MENTIONS, "@alice @bob @carol")

    def test_empty_config_keeps_existing_default(self) -> None:
        config = self._load_config("")

        self.assertEqual(config.HITL_HANDLES, ("geserdugarov",))
        self.assertEqual(config.HITL_MENTIONS, "@geserdugarov")


class AgentGitIdentityConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-study-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_defaults_to_orchestrator_identity(self) -> None:
        config = self._load_config()

        self.assertEqual(config.AGENT_GIT_NAME, "agent-orchestrator")
        self.assertEqual(
            config.AGENT_GIT_EMAIL,
            "agent-orchestrator@users.noreply.github.com",
        )

    def test_env_overrides_take_effect(self) -> None:
        config = self._load_config({
            "AGENT_GIT_NAME": "Custom Bot",
            "AGENT_GIT_EMAIL": "bot@example.com",
        })

        self.assertEqual(config.AGENT_GIT_NAME, "Custom Bot")
        self.assertEqual(config.AGENT_GIT_EMAIL, "bot@example.com")


class AgentBackendConfigTest(unittest.TestCase):
    """`DEV_AGENT` / `REVIEW_AGENT` are validated at import time so a typo
    aborts the process before the polling loop spins up."""

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-study-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_defaults_split_claude_dev_codex_review(self) -> None:
        config = self._load_config()
        self.assertEqual(config.DEV_AGENT, "claude")
        self.assertEqual(config.REVIEW_AGENT, "codex")

    def test_env_overrides_invert_split(self) -> None:
        config = self._load_config({
            "DEV_AGENT": "codex",
            "REVIEW_AGENT": "claude",
        })
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(config.REVIEW_AGENT, "claude")

    def test_case_and_whitespace_tolerated(self) -> None:
        config = self._load_config({
            "DEV_AGENT": "  CODEX ",
            "REVIEW_AGENT": "Claude",
        })
        self.assertEqual(config.DEV_AGENT, "codex")
        self.assertEqual(config.REVIEW_AGENT, "claude")

    def test_invalid_dev_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DEV_AGENT": "gemini"})
        self.assertIn("DEV_AGENT", str(cm.exception))
        self.assertIn("gemini", str(cm.exception))

    def test_invalid_review_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REVIEW_AGENT": "qwen"})
        self.assertIn("REVIEW_AGENT", str(cm.exception))


class MaxRetriesPerDayConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-study-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_three(self) -> None:
        config = self._load_config()
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 3)

    def test_env_override(self) -> None:
        config = self._load_config({"MAX_RETRIES_PER_DAY": "7"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 7)

    def test_zero_means_unbounded(self) -> None:
        config = self._load_config({"MAX_RETRIES_PER_DAY": "0"})
        self.assertEqual(config.MAX_RETRIES_PER_DAY, 0)


if __name__ == "__main__":
    unittest.main()
