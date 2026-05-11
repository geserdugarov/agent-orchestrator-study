# Repository guide for AI agents

This file is the entry point for AI coding agents (Codex, Claude, etc.) working on this repository. `CLAUDE.md` is a symlink to this file, so both conventions resolve to the same content.

## What this project is

`agent-orchestrator` is a GitHub-Issue-driven workflow that watches issues on configured repos, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) in per-issue git worktrees to implement them and open PRs. State lives entirely in GitHub (one workflow label + one pinned JSON comment per issue), so the orchestrator process is stateless.

The user-facing overview is in [`README.md`](README.md). The design and stage definitions are in [`docs/workflow.md`](docs/workflow.md) (Russian) and [`docs/architecture.md`](docs/architecture.md). The roadmap lives at [`plans/roadmap.md`](plans/roadmap.md). Read these before making non-trivial changes to the state machine.

## Repository layout

- `orchestrator/` — the Python package
  - `main.py` — entry point; polling loop and `--once` mode
  - `workflow.py` — the label state machine and per-stage handlers (large; the bulk of the logic)
  - `agents.py` — `codex` / `claude` subprocess invocation
  - `github.py` — `GitHubClient` wrapper around PyGithub
  - `config.py` — env parsing, `REPOS` multi-repo spec, validation
- `tests/` — pytest suite; `fakes.py` holds in-memory fakes used across tests
- `docs/` — workflow and architecture documentation
- `plans/roadmap.md` — implementation roadmap
- `run.sh` — production launcher that auto-restarts after self-modifying merges
- `.env.example` — annotated configuration reference

## Running and testing

The repo targets Python 3.12+. Local development uses [`uv`](https://github.com/astral-sh/uv) for the venv.

```sh
uv venv --python 3.12
uv pip install PyGithub
.venv/bin/python -m pytest          # run the test suite
.venv/bin/python -m orchestrator.main --once  # one polling tick then exit
.venv/bin/python -m orchestrator.main --log-level DEBUG
```

Tests are the primary correctness gate. Add or update tests for any behavioral change to `workflow.py`, `agents.py`, `github.py`, or `config.py`.

## Code conventions

- **License headers.** Every source file (`*.py`, `*.sh`, `pyproject.toml`) starts with:
  ```
  # Copyright 2026 Geser Dugarov
  # SPDX-License-Identifier: Apache-2.0
  ```
  Match the existing style of neighbouring files when adding new ones.
- **Commits.** Conventional Commits: `<type>: <subject>` with types `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Subject line only — no body, no `Co-Authored-By` trailer. Imperative mood, short.
- **No comments unless the *why* is non-obvious.** The codebase keeps comments sparse; don't narrate what well-named code already says. Hidden constraints, race-window reasoning, or workarounds for specific GitHub quirks are worth a line.
- **Don't introduce dependencies casually.** `pyproject.toml` pins only `PyGithub`. Anything else needs justification.
- **Secrets.** `GITHUB_TOKEN` is deliberately *not* loaded from `.env` (the implementer agent can read the worktree). Tokens live in `~/.config/<owner>/<repo>/token` or the process environment. Don't change this without reading the rationale in `.env.example`.

## When working on the state machine

- Labels and stage names are part of the public contract — issues in flight carry them. Renaming or repurposing a label is a migration, not a refactor.
- The pinned JSON state comment is the only durable per-issue state. Schema changes need to stay backward-compatible with comments already on live issues.
- `workflow.py` is large and has many stage handlers (`_handle_decomposing`, `_handle_implementing`, `_handle_validating`, `_handle_in_review`, `_handle_resolving_conflict`, `_handle_blocked`, …). Find the handler for the stage you're touching before changing dispatcher routing.
- Tests in `tests/test_workflow.py` exercise stages against in-memory fakes (`tests/fakes.py`). Prefer extending these fakes over mocking PyGithub directly.

## When working on agent invocation

- `codex` is invoked with `--dangerously-bypass-approvals-and-sandbox`; `claude` with `--dangerously-skip-permissions`. The host is the sandbox boundary, which is why secrets are kept off the worktree.
- Agent stdout/stderr handling matters: empty-output and timeout cases are deliberately distinguished (`park_reason`s are tagged transient vs. terminal). Look at `agents.py` and the `_handle_*_timeout` recovery paths in `workflow.py` before adjusting subprocess plumbing.

## Out of scope without explicit ask

- Adding new external dependencies, frameworks, or services.
- Reformatting unrelated files or churning whitespace.
- "Future-proofing" abstractions for hypothetical features. The roadmap drives feature work; design for what's on it, not what might be next.
