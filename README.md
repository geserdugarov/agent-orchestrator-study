# Agent orchestration

Orchestrator for automatic issues resolving utilizing agents.

The orchestrator watches GitHub Issues, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) to implement them and open PRs. State lives in GitHub Issues themselves (one workflow label + one pinned JSON comment), so the orchestrator stays stateless and progress is observable on github.com.

For the design and stage definitions, see [`docs/workflow.md`](docs/workflow.md) (in Russian).
For the implementation roadmap, see [`plans/roadmap.md`](plans/roadmap.md).

## Requirements

### System

- Linux (developed and tested on Ubuntu 24.04 / WSL2)
- Git
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — Python package and venv manager (alternative: `python3-venv` + `pip`)

### CLI agents

The orchestrator spawns these as subprocesses; both must be installed and authenticated on the host before the orchestrator starts. Roles are configurable via `DEV_AGENT` / `REVIEW_AGENT` (default: `claude` implements, `codex` reviews).

- [`codex`](https://github.com/openai/codex) — invoked with `--dangerously-bypass-approvals-and-sandbox`. Run `codex login` once. The host is the sandbox boundary.
- [`claude`](https://docs.anthropic.com/en/docs/claude-code) — invoked with `--dangerously-skip-permissions`. Authenticate via `claude` once.

### GitHub

- A repository the orchestrator will manage (default: this one).
- A **fine-grained Personal Access Token** scoped to that repository, with these repository permissions:
  - **Contents**: Read and write — push branches
  - **Issues**: Read and write — read issues, post comments, set/create labels
  - **Pull requests**: Read and write — open PRs
  - **Checks**: Read-only — required for `AUTO_MERGE` to evaluate Actions-only PRs (without it, the orchestrator sees `check_state='none'` and parks waiting for a human even when CI is green)
  - **Metadata**: Read-only — required and forced on

  Generate at <https://github.com/settings/personal-access-tokens>.

### Python dependencies

Pinned in [`pyproject.toml`](pyproject.toml):

- `PyGithub >= 2.1`

## Quick start

1. **Clone and enter the repo**

   ```sh
   git clone https://github.com/geserdugarov/agent-orchestrator.git
   cd agent-orchestrator
   ```

2. **Create a venv and install dependencies**

   ```sh
   uv venv --python 3.12
   uv pip install PyGithub
   ```

3. **Configure environment**

   ```sh
   cp .env.example .env
   ```

   Edit `.env` and set at minimum:
   - `HITL_HANDLE` — comma-separated GitHub logins (the users the orchestrator @-mentions on questions)
   - `REPO` — leave default unless pointing at a different repo

   Then store the PAT **outside** the repo so the implementer agent (which runs
   in a sibling worktree with sandbox bypass) cannot read it via a relative
   path:

   The default token path is derived from `REPO` (`~/.config/<owner>/<repo>/token`).
   For the default repo:

   ```sh
   install -d -m 700 ~/.config/geserdugarov/agent-orchestrator
   printf %s "$YOUR_PAT" > ~/.config/geserdugarov/agent-orchestrator/token
   chmod 600 ~/.config/geserdugarov/agent-orchestrator/token
   ```

   Or export `GITHUB_TOKEN` in the orchestrator's launch environment. Putting
   the PAT in `.env` is rejected at startup. Override the file path with
   `ORCHESTRATOR_TOKEN_FILE` if you want a different location — pick one the
   agent worktree cannot reach via known relatives.

4. **Verify the agents are authenticated**

   ```sh
   codex --version
   claude --version
   ```

   If a backend is not logged in, run its `login` flow (`codex login` / `claude /login`). Only the backends you actually route to via `DEV_AGENT` / `REVIEW_AGENT` need to be authenticated, but the defaults use both.

5. **Run**

   ```sh
   ./run.sh
   ```

   The wrapper does `git pull --ff-only origin main` and re-launches the orchestrator after each clean exit (so a self-modifying merge picks up the new code automatically).

   On first start the orchestrator creates the 9 workflow labels on the repo and begins polling open issues every 60 seconds.

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within ~1 minute the orchestrator should comment "picking this up", label the issue `implementing`, run the dev agent (`DEV_AGENT`, default `claude`) in a fresh worktree at `../wt-orchestrator/issue-N`, push the branch, open a PR, label the issue `validating`, run a fresh reviewer session (`REVIEW_AGENT`, default `codex`) against the diff, and on `VERDICT: APPROVED` move the issue to `in_review`. With `AUTO_MERGE=on`, the orchestrator then merges the PR itself once GitHub reports it mergeable with green checks, flips the label to `done`, and closes the issue. With `AUTO_MERGE=off` (the default), review and merge the PR manually.

## Run modes

- `./run.sh` — production: continuous polling with auto-restart on self-modifying merges
- `python -m orchestrator.main --once` — single tick then exit, useful for testing
- `python -m orchestrator.main --log-level DEBUG` — verbose logs

## Configuration reference

All settings load from `.env` (or process environment). See [`.env.example`](.env.example) for the full list with defaults. Key knobs:

| Variable                  | Default                                       | Purpose                                                   |
| ------------------------- | --------------------------------------------- | --------------------------------------------------------- |
| `GITHUB_TOKEN`            | _(required, env-only — not read from `.env`)_ | fine-grained PAT                                          |
| `ORCHESTRATOR_TOKEN_FILE` | `~/.config/<owner>/<repo>/token` (from `REPO`) | path to PAT file (used when `GITHUB_TOKEN` is not in env) |
| `REPO`                    | `geserdugarov/agent-orchestrator`  | `owner/name` of the repo to manage                        |
| `POLL_INTERVAL`           | `60`                                          | seconds between polling ticks                             |
| `AGENT_TIMEOUT`           | `1800`                                        | wall-clock cap per agent invocation, seconds              |
| `REVIEW_TIMEOUT`          | (= `AGENT_TIMEOUT`)                           | wall-clock cap per reviewer invocation, seconds           |
| `MAX_REVIEW_ROUNDS`       | `3`                                           | review/fix iterations before parking on `awaiting_human` |
| `MAX_RETRIES_PER_DAY`     | `3`                                           | fresh implementer spawns per issue per 24h window (`0` = unbounded) |
| `DEV_AGENT`               | `claude`                                      | implementer backend; one of `codex` / `claude`            |
| `REVIEW_AGENT`            | `codex`                                       | reviewer backend; one of `codex` / `claude`               |
| `DECOMPOSE_AGENT`         | `claude`                                      | decomposer backend; one of `codex` / `claude` (validated even when `DECOMPOSE=off`) |
| `DECOMPOSE`               | `on`                                          | enable the `decomposing` stage; `off` reverts to the legacy "no label → implementing" pickup |
| `HITL_HANDLE`             | `geserdugarov`                                | comma-separated GitHub logins to @-mention when a human is needed |
| `WORKTREES_DIR`           | `../wt-orchestrator`                          | where per-issue git worktrees are created; per-repo subdir keeps them isolated, so the on-disk layout is `WORKTREES_DIR/<owner>__<name>/issue-N` |
| `CODEX_BIN`               | `codex`                                       | override only if `codex` is not on `$PATH`                |
| `CLAUDE_BIN`              | `claude`                                      | override only if `claude` is not on `$PATH`               |
| `AGENT_GIT_NAME`          | `agent-orchestrator`                          | `GIT_AUTHOR_NAME`/`GIT_COMMITTER_NAME` injected into agent spawns |
| `AGENT_GIT_EMAIL`         | `agent-orchestrator@users.noreply.github.com` | `GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_EMAIL` injected into agent spawns |
| `BASE_BRANCH`             | `main`                                        | branch PRs target                                         |
| `AUTO_MERGE`              | `off`                                         | merge approved PRs (green CI + mergeable) from `in_review`; flip to `on` once dogfooded |
| `IN_REVIEW_DEBOUNCE_SECONDS` | `600`                                       | quiet window after the latest PR/issue comment before resuming the dev session |

## Current scope

The orchestrator currently drives (no label) → `decomposing` → `ready`/`blocked` → `implementing` → `validating` → `in_review` → `done`/`rejected`, with configurable dev/review/decompose backend splits, a per-issue retry budget (`MAX_RETRIES_PER_DAY`), a review/fix loop capped by `MAX_REVIEW_ROUNDS`, and a debounced PR-comment-resume loop in `in_review`. The decomposer asks the agent for a fenced `orchestrator-manifest` JSON block; on `decision=single` the parent flips straight to `ready`, on `decision=split` it creates child issues, persists the dep graph, and parks on `blocked` until `_handle_blocked` walks the children. Auto-merge on approve+green-CI is gated by `AUTO_MERGE` (default `off`); enable it once dogfooded. The decomposer can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup; the same flag also routes any issue already labeled `decomposing` (e.g. parked there awaiting a human) to `implementing` on the next tick, so the kill switch applies to in-flight issues, not just new ones. See [`plans/roadmap.md`](plans/roadmap.md).
