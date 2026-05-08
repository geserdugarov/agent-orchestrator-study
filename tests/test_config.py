# Copyright 2026 Geser Dugarov
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
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


class MaxConflictRoundsConfigTest(unittest.TestCase):
    """`MAX_CONFLICT_ROUNDS` parses identically to `MAX_REVIEW_ROUNDS`:
    integer, defaults to 3, env override wins.
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

    def test_default_is_three(self) -> None:
        config = self._load_config()
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 3)

    def test_env_override(self) -> None:
        config = self._load_config({"MAX_CONFLICT_ROUNDS": "7"})
        self.assertEqual(config.MAX_CONFLICT_ROUNDS, 7)


class MultiRepoConfigTest(unittest.TestCase):
    """`REPOS` parses N entries; when unset the legacy single-repo trio
    (`REPO` / `TARGET_REPO_ROOT` / `BASE_BRANCH`) keeps working."""

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

    def test_legacy_single_repo_fallback_when_repos_unset(self) -> None:
        config = self._load_config({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        })

        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].slug, "owner/legacy")
        self.assertEqual(specs[0].target_root, Path("/tmp"))
        self.assertEqual(specs[0].base_branch, "trunk")

    def test_multi_entry_parsing_newline_and_semicolon(self) -> None:
        # Mix newlines, ';', blank lines, and a comment to verify the parser
        # accepts both separators and ignores noise.
        with tempfile.TemporaryDirectory() as td:
            other = Path(td) / "other"
            other.mkdir()
            config = self._load_config({
                "REPOS": (
                    "# multi-repo example\n"
                    f"alpha/one|{td}|main\n"
                    "\n"
                    f"beta/two|{other}|develop;gamma/three|{td}|master"
                ),
            })

            specs = config.default_repo_specs()
            self.assertEqual([s.slug for s in specs],
                             ["alpha/one", "beta/two", "gamma/three"])
            self.assertEqual([s.base_branch for s in specs],
                             ["main", "develop", "master"])
            self.assertEqual(specs[1].target_root, other)
            # Returned list is a fresh copy so callers can't mutate the cache.
            specs.append("not-a-spec")  # type: ignore[arg-type]
            self.assertEqual(len(config.default_repo_specs()), 3)

    def test_repos_overrides_legacy_trio(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config = self._load_config({
                "REPO": "ignored/legacy",
                "TARGET_REPO_ROOT": "/nonexistent",
                "BASE_BRANCH": "ignored",
                "REPOS": f"alpha/one|{td}|main",
            })

            specs = config.default_repo_specs()
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0].slug, "alpha/one")
            self.assertEqual(specs[0].target_root, Path(td))
            self.assertEqual(specs[0].base_branch, "main")

    def test_duplicate_slug_aborts_at_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit) as cm:
                self._load_config({
                    "REPOS": (
                        f"alpha/one|{td}|main\n"
                        f"alpha/one|{td}|develop"
                    ),
                })
            msg = str(cm.exception)
            self.assertIn("duplicate slug", msg)
            self.assertIn("alpha/one", msg)

    def test_malformed_entry_aborts_at_import(self) -> None:
        # Wrong number of '|' segments.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo|/tmp"})
        self.assertIn("malformed", str(cm.exception))

    def test_empty_slug_aborts_at_import(self) -> None:
        # Slug must contain '/'.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "no-slash|/tmp|main"})
        self.assertIn("owner/name", str(cm.exception))

    def test_slug_with_empty_component_aborts_at_import(self) -> None:
        # `owner//repo` and `/repo` and `owner/` are all malformed even
        # though they contain `/`; require exactly two non-empty components.
        for bad in ("owner//repo", "/repo", "owner/", "//"):
            with self.subTest(slug=bad):
                with self.assertRaises(SystemExit) as cm:
                    self._load_config({"REPOS": f"{bad}|/tmp|main"})
                self.assertIn("owner/name", str(cm.exception))

    def test_slug_with_extra_path_segment_aborts_at_import(self) -> None:
        # `owner/repo/extra` looks plausible but PyGithub treats the slug
        # as the full repo identifier, so any extra `/` would resolve to
        # a wrong (or nonexistent) repo at runtime. Reject at import.
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo/extra|/tmp|main"})
        self.assertIn("owner/name", str(cm.exception))

    def test_empty_base_branch_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo|/tmp|"})
        self.assertIn("base_branch", str(cm.exception))

    def test_empty_target_root_aborts_at_import(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "owner/repo||main"})
        self.assertIn("target_root", str(cm.exception))

    def test_repos_with_only_comments_aborts(self) -> None:
        # `REPOS` set but yielding zero entries is a misconfiguration --
        # better to fail loudly than silently fall back to the legacy trio
        # (which the user explicitly opted out of by setting REPOS).
        with self.assertRaises(SystemExit) as cm:
            self._load_config({"REPOS": "# just a comment\n  \n"})
        self.assertIn("no valid entries", str(cm.exception))

    def test_missing_target_root_warns_but_does_not_abort(self) -> None:
        # Captures the stderr warning to confirm "warn loudly" semantics.
        import io
        from contextlib import redirect_stderr

        buf = io.StringIO()
        with redirect_stderr(buf):
            config = self._load_config(
                {"REPOS": "alpha/one|/this/path/does/not/exist|main"}
            )
        specs = config.default_repo_specs()
        self.assertEqual(len(specs), 1)
        self.assertIn("does not exist", buf.getvalue())
        self.assertIn("alpha/one", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
