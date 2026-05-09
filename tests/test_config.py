# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch


class HitlHandleConfigTest(unittest.TestCase):
    def _load_config(self, hitl_handle: str):
        env = {
            "HITL_HANDLE": hitl_handle,
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
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
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
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
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
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

    def test_default_decompose_agent_is_claude(self) -> None:
        config = self._load_config()
        self.assertEqual(config.DECOMPOSE_AGENT, "claude")

    def test_decompose_agent_env_override(self) -> None:
        config = self._load_config({"DECOMPOSE_AGENT": "codex"})
        self.assertEqual(config.DECOMPOSE_AGENT, "codex")

    def test_invalid_decompose_agent_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"DECOMPOSE_AGENT": "gemini"})
        self.assertIn("DECOMPOSE_AGENT", str(cm.exception))

    def test_decompose_agent_validated_even_when_decompose_off(self) -> None:
        # Toggling DECOMPOSE back on later must not surface a fresh
        # "that env var was always invalid" failure.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({
                "DECOMPOSE": "off",
                "DECOMPOSE_AGENT": "gemini",
            })
        self.assertIn("DECOMPOSE_AGENT", str(cm.exception))


class DecomposeKillSwitchConfigTest(unittest.TestCase):
    """The DECOMPOSE kill switch defaults on; truthy spellings keep it on,
    explicit off / typos disable it. Mirrors AUTO_MERGE's strict parser
    semantics so a typo doesn't silently flip the user's intent.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_on(self) -> None:
        config = self._load_config()
        self.assertTrue(config.DECOMPOSE)

    def test_explicit_off(self) -> None:
        config = self._load_config({"DECOMPOSE": "off"})
        self.assertFalse(config.DECOMPOSE)

    def test_truthy_spellings_keep_on(self) -> None:
        for value in ("on", "ON", " on ", "1", "true", "True", "yes"):
            with self.subTest(value=value):
                config = self._load_config({"DECOMPOSE": value})
                self.assertTrue(config.DECOMPOSE)

    def test_falsy_spellings_disable(self) -> None:
        for value in ("0", "false", "no", "off"):
            with self.subTest(value=value):
                config = self._load_config({"DECOMPOSE": value})
                self.assertFalse(config.DECOMPOSE)

    def test_typo_defaults_to_off(self) -> None:
        # Strict parser: any unrecognized value disables decomposition.
        config = self._load_config({"DECOMPOSE": "enabled"})
        self.assertFalse(config.DECOMPOSE)


class AutoMergeConfigTest(unittest.TestCase):
    """Default off; only an explicit truthy spelling flips it on. A typo
    silently defaulting to on would let the orchestrator merge against the
    user's intent, so the parser is deliberately strict.
    """

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_off(self) -> None:
        config = self._load_config()
        self.assertFalse(config.AUTO_MERGE)

    def test_explicit_off(self) -> None:
        config = self._load_config({"AUTO_MERGE": "off"})
        self.assertFalse(config.AUTO_MERGE)

    def test_truthy_spellings_enable(self) -> None:
        for value in ("on", "ON", " on ", "1", "true", "True", "yes"):
            with self.subTest(value=value):
                config = self._load_config({"AUTO_MERGE": value})
                self.assertTrue(
                    config.AUTO_MERGE, f"{value!r} should enable AUTO_MERGE"
                )

    def test_falsy_spellings_disable(self) -> None:
        for value in ("0", "false", "no", ""):
            with self.subTest(value=value):
                config = self._load_config({"AUTO_MERGE": value})
                self.assertFalse(
                    config.AUTO_MERGE, f"{value!r} should leave AUTO_MERGE off"
                )

    def test_typo_defaults_to_off(self) -> None:
        # The whole point of off-by-default + strict-truthy parsing: a typo
        # cannot silently turn on auto-merge.
        config = self._load_config({"AUTO_MERGE": "enabled"})
        self.assertFalse(config.AUTO_MERGE)


class InReviewDebounceConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_ten_minutes(self) -> None:
        # Matches the "10 минут (debounce)" in docs/workflow.md:142.
        config = self._load_config()
        self.assertEqual(config.IN_REVIEW_DEBOUNCE_SECONDS, 600)

    def test_env_override(self) -> None:
        config = self._load_config({"IN_REVIEW_DEBOUNCE_SECONDS": "120"})
        self.assertEqual(config.IN_REVIEW_DEBOUNCE_SECONDS, 120)


class MaxRetriesPerDayConfigTest(unittest.TestCase):
    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
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


class AllowedIssueAuthorsConfigTest(unittest.TestCase):
    """Author-allowlist for unlabeled-issue pickup. Empty (default) disables
    the filter so existing single-user setups keep working; a populated list
    guards against random users on public repos triggering agent runs."""

    def _load_config(self, env: dict[str, str] | None = None):
        full_env = {
            "ORCHESTRATOR_SKIP_DOTENV": "1",
            "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        }
        if env:
            full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            sys.modules.pop("orchestrator.config", None)
            import orchestrator.config as config

            return config

    def test_default_is_empty_tuple(self) -> None:
        config = self._load_config()
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ())

    def test_parses_comma_separated(self) -> None:
        config = self._load_config({"ALLOWED_ISSUE_AUTHORS": "alice,bob"})
        self.assertEqual(config.ALLOWED_ISSUE_AUTHORS, ("alice", "bob"))

    def test_strips_whitespace_at_signs_and_duplicates(self) -> None:
        config = self._load_config(
            {"ALLOWED_ISSUE_AUTHORS": " @alice, bob, ,alice,@carol "}
        )
        self.assertEqual(
            config.ALLOWED_ISSUE_AUTHORS, ("alice", "bob", "carol")
        )


if __name__ == "__main__":
    unittest.main()
