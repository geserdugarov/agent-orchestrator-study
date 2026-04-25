# agent-orchestrator-study

Orchestrator for automatic issues resolving utilizing agents.

The orchestrator watches GitHub Issues, drives them through a label-based state machine, and spawns local CLI agents (`codex`, `claude`) to implement them and open PRs. State lives in GitHub Issues themselves (one workflow label + one pinned JSON comment), so the orchestrator stays stateless and progress is observable on github.com.

For the design and stage definitions, see [`docs/workflow.md`](docs/workflow.md) (in Russian).
For the implementation roadmap and v0 scope cut, see [`plans/roadmap.md`](plans/roadmap.md).

## Requirements

### System

- Linux (developed and tested on Ubuntu 24.04 / WSL2)
- Git
- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) — Python package and venv manager (alternative: `python3-venv` + `pip`)

### CLI agents

The orchestrator spawns these as subprocesses; both must be installed and authenticated on the host before the orchestrator starts.

- [`codex`](https://github.com/openai/codex) — implementer. Run `codex login` once. The orchestrator invokes it with `--dangerously-bypass-approvals-and-sandbox`, so the host should be considered the sandbox boundary.
- [`claude`](https://docs.anthropic.com/en/docs/claude-code) — validator (Week 2 stage; **not used in v0**). Authenticate when needed.

### GitHub

- A repository the orchestrator will manage (default: this one).
- A **fine-grained Personal Access Token** scoped to that repository, with these repository permissions:
  - **Contents**: Read and write — push branches
  - **Issues**: Read and write — read issues, post comments, set/create labels
  - **Pull requests**: Read and write — open PRs
  - **Metadata**: Read-only — required and forced on

  Generate at <https://github.com/settings/personal-access-tokens>.

### Python dependencies

Pinned in [`pyproject.toml`](pyproject.toml):

- `PyGithub >= 2.1`

## Quick start

1. **Clone and enter the repo**

   ```sh
   git clone https://github.com/podlodka-ai-club/spark-gap.git
   cd spark-gap
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
   install -d -m 700 ~/.config/podlodka-ai-club/spark-gap
   printf %s "$YOUR_PAT" > ~/.config/podlodka-ai-club/spark-gap/token
   chmod 600 ~/.config/podlodka-ai-club/spark-gap/token
   ```

   Or export `GITHUB_TOKEN` in the orchestrator's launch environment. Putting
   the PAT in `.env` is rejected at startup. Override the file path with
   `ORCHESTRATOR_TOKEN_FILE` if you want a different location — pick one the
   agent worktree cannot reach via known relatives.

4. **Verify the agents are authenticated**

   ```sh
   codex --version
   claude --version    # only required for Week 2 validate stage
   ```

   If `codex` is not logged in, run `codex login`.

5. **Run**

   ```sh
   ./run.sh
   ```

   The wrapper does `git pull --ff-only origin main` and re-launches the orchestrator after each clean exit (so a self-modifying merge picks up the new code automatically).

   On first start the orchestrator creates the 8 workflow labels on the repo and begins polling open issues every 60 seconds.

6. **File a bootstrap test issue** to verify the path works end-to-end:

   > **Title:** Add a `hello()` function to the orchestrator package
   > **Body:** Add `hello()` to `orchestrator/__init__.py` returning the string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

   Within ~1 minute the orchestrator should comment "picking this up", label the issue `implementing`, run codex in a fresh worktree at `../wt-orchestrator/issue-N`, push the branch, open a PR, and label the issue `in_review`. Review the PR and merge manually (auto-merge is a Week 2 feature).

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
| `REPO`                    | `podlodka-ai-club/spark-gap`       | `owner/name` of the repo to manage                        |
| `POLL_INTERVAL`           | `60`                                          | seconds between polling ticks                             |
| `AGENT_TIMEOUT`           | `1800`                                        | wall-clock cap per agent invocation, seconds              |
| `HITL_HANDLE`             | `geserdugarov`                                | comma-separated GitHub logins to @-mention when a human is needed |
| `WORKTREES_DIR`           | `../wt-orchestrator`                          | where per-issue git worktrees are created                 |
| `CODEX_BIN`               | `codex`                                       | override only if `codex` is not on `$PATH`                |
| `CLAUDE_BIN`              | `claude`                                      | override only if `claude` is not on `$PATH`               |
| `BASE_BRANCH`             | `main`                                        | branch PRs target                                         |

## v0 scope

The current MVP implements only the (no label) → `implementing` → `in_review` path. The full 4-stage workflow (`decomposing`, `validating`, auto-merge, `blocked`/`rejected`) is scoped for Week 2; see [`plans/roadmap.md`](plans/roadmap.md).
