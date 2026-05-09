"""Tests for the polling-loop entry point.

The multi-repo polling loop must call `workflow.tick(gh, spec)` for every
configured spec on every tick. A per-repo exception in `tick` must not
prevent the remaining specs from running -- the orchestrator's whole point
is to keep advancing other repos when one is stuck.
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch


@contextmanager
def _reload_main(env: dict[str, str]):
    """Reload `orchestrator.config` + `orchestrator.main` with `env` patched
    over the process environment, so module-level `REPOS` parsing actually
    sees the test value. Yields the freshly imported `main` module.

    `importlib.import_module` is used instead of `from orchestrator import
    main` because the latter falls back to the parent package's cached
    `main` attribute even after the submodule is popped from `sys.modules`,
    which leaks state across tests.
    """
    full_env = {
        "ORCHESTRATOR_SKIP_DOTENV": "1",
        "ORCHESTRATOR_TOKEN_FILE": "/tmp/agent-orchestrator-token-missing",
        "GITHUB_TOKEN": "ghp-test-secret",
    }
    full_env.update(env)
    with patch.dict(os.environ, full_env, clear=True):
        sys.modules.pop("orchestrator.config", None)
        sys.modules.pop("orchestrator.main", None)
        # Force config to re-run module-level REPOS parsing first, then
        # main, so main_mod.config is the freshly imported module.
        importlib.import_module("orchestrator.config")
        main_mod = importlib.import_module("orchestrator.main")
        # Skip signal-handler registration and the file-handler setup so
        # the test does not touch shared process state or filesystem.
        with patch.object(main_mod, "_configure_logging"), \
             patch.object(main_mod.signal, "signal"):
            yield main_mod


class PollingLoopFanOutTest(unittest.TestCase):
    def test_once_calls_tick_for_every_configured_spec(self) -> None:
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop"
            ),
        }) as main_mod:
            tick_calls: list[tuple[str, str]] = []

            def fake_tick(gh, spec):
                # Record the spec slug + whichever client main.py paired it
                # with, so a regression that crossed wires (spec for alpha
                # paired with beta's gh) would surface here.
                tick_calls.append((spec.slug, gh.slug))

            clients_by_slug: dict[str, MagicMock] = {}

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                clients_by_slug[repo_spec.slug] = m
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(
                tick_calls,
                [("alpha/one", "alpha/one"), ("beta/two", "beta/two")],
            )
            for slug in ("alpha/one", "beta/two"):
                clients_by_slug[slug].ensure_workflow_labels.assert_called_once()

    def test_per_repo_tick_exception_does_not_block_other_repos(self) -> None:
        # The whole point of catching per-repo failures: one repo wedged in
        # an unhandled error must not stop the others from advancing.
        with tempfile.TemporaryDirectory() as td, _reload_main({
            "REPOS": (
                f"alpha/one|{td}|main\n"
                f"beta/two|{td}|develop\n"
                f"gamma/three|{td}|main"
            ),
        }) as main_mod:
            ticked: list[str] = []

            def fake_tick(gh, spec):
                ticked.append(spec.slug)
                if spec.slug == "alpha/one":
                    raise RuntimeError("simulated alpha failure")

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            # Returned 0 (loop swallowed the per-repo exception) and every
            # spec was attempted in declared order even though the first
            # one raised.
            self.assertEqual(rc, 0)
            self.assertEqual(ticked, ["alpha/one", "beta/two", "gamma/three"])

    def test_legacy_single_repo_still_works(self) -> None:
        # No REPOS set: main.py must still run a single tick using the
        # legacy REPO/TARGET_REPO_ROOT/BASE_BRANCH trio.
        with _reload_main({
            "REPO": "owner/legacy",
            "TARGET_REPO_ROOT": "/tmp",
            "BASE_BRANCH": "trunk",
        }) as main_mod:
            tick_calls: list[str] = []

            def fake_tick(gh, spec):
                tick_calls.append(spec.slug)

            def fake_client(*, repo_spec):
                m = MagicMock()
                m.slug = repo_spec.slug
                return m

            with patch.object(main_mod, "GitHubClient", side_effect=fake_client), \
                 patch.object(main_mod.workflow, "tick", side_effect=fake_tick):
                rc = main_mod.main(["--once"])

            self.assertEqual(rc, 0)
            self.assertEqual(tick_calls, ["owner/legacy"])


if __name__ == "__main__":
    unittest.main()
