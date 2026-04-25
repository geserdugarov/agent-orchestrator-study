# Agent Orchestrator MVP — Implementation Plan

## Context

The repo currently contains only `README.md` and `docs/workflow.md` (a Russian-language design spec). No code yet.

The goal documented in `docs/workflow.md` is an "orchestrator": a long-running process that watches GitHub Issues, drives them through a fixed 4-stage workflow (Decompose → Implement → Validate → Accept), and uses local AI coding-agent CLIs (`codex`, `claude`) to do the actual work. State lives in GitHub Issues themselves (one label per issue, plus pinned JSON state in a comment) so the orchestrator stays stateless and the user can watch progress on github.com.

The driver of this plan is the user's twin constraint: **2-week total budget** and "switch to self-development as soon as possible" — i.e. the orchestrator has to become useful for resolving issues in *its own repo* well before the 2 weeks are up, so the rest of the build can itself be done by the orchestrator (compiler-bootstrap principle). The intended outcome is a v0 by **Day 3** that handles the (no-label → implementing → in_review) happy path end-to-end against this very repo, with the documented `decomposing` and `validating` stages added in the second week.

User-confirmed decisions: **aggressive scope cut** for v0, **Python 3.12**, **fine-grained PAT scoped to this repo only** for GitHub auth.

## v0 scope (Day 1–3, self-bootstrap milestone)

Ship only the critical path; everything else is iteration:

- New issue with no label → orchestrator picks it up → labels `implementing` → spawns `codex` in a worktree → pushes branch → opens PR → labels `in_review`. **`decomposing` and `validating` stages are skipped entirely in v0.**
- Human reviews PR on github.com and merges manually. No auto-merge in v0.
- HITL: when codex output indicates it's blocked / needs input, the orchestrator posts the question as an issue comment, leaves the issue at `implementing`, and waits for a fresh human comment before resuming the codex session.
- Concurrency: **one agent at a time** (a `Lock` in `main.py`). Issues queue.

Defer to Week 2 (Day 6–14): `validating` stage with claude PR review, auto-merge on approve+green-CI, comment debounce, `decomposing`, `blocked`/`rejected` flows, parallel agents, container isolation, VPS deploy, GitHub App migration.

## Tech stack

- **Python 3.12** (already on host).
- One dependency: **PyGithub**. No `gh` CLI install; `requests`/`httpx`/`octokit` not needed.
- Standard library `subprocess`, `pathlib`, `json`, `logging`, `signal`, `time`. No `python-dotenv` — read `.env` manually in `config.py`.
- Tests: stdlib `unittest` against a faked `Github` object. No mocking of `codex`/`claude` — those are integration-tested via the bootstrap issue.

## File layout

Flat package, ~5 files for v0. No premature abstraction.

```
/home/geserdugarov/git/agent-orchestrator-study/
├── README.md                       (existing)
├── docs/workflow.md                (existing — source of truth for label/stage semantics)
├── orchestrator/
│   ├── __init__.py
│   ├── main.py                     # entry point, polling loop, signal handling, --once flag
│   ├── workflow.py                 # state machine: label → handler dispatch (heart of v0, <300 LoC)
│   ├── github.py                   # PyGithub wrapper: list issues, label r/w, comment, open PR, PR state, pinned-state-comment helpers
│   ├── agents.py                   # spawn codex/claude as subprocess, capture session ID, timeout, parse blocked/done signal
│   └── config.py                   # env-var loader: GITHUB_TOKEN, REPO, POLL_INTERVAL, AGENT_TIMEOUT, agent CLI paths
├── pyproject.toml                  # PEP 621, deps = ["PyGithub"]
├── .env.example                    # GITHUB_TOKEN=, REPO=podlodka-ai-club/spark-gap, POLL_INTERVAL=60
├── .gitignore                      # .env, __pycache__, *.pyc, .venv/, ../wt-issue-*
└── tests/
    └── test_workflow.py            # state-machine transitions with a fake GitHub
```

## State machine (v0)

| From label | Trigger | To label | Handler |
|---|---|---|---|
| (none) | issue is open & unlabeled | `implementing` | `handle_pickup`: post "starting work" comment, create branch `orchestrator/issue-<N>` in a fresh worktree at `../wt-issue-<N>`, set label, hand off to `handle_implement` |
| `implementing` | no agent currently running for this issue & no "awaiting human" marker | (stays) | `handle_implement`: spawn `codex exec` with issue title+body+comments, on success push branch and open PR, persist `codex_session_id` + `branch` + `pr_number` into pinned-state JSON comment |
| `implementing` | codex returned a blocked/question signal | (stays) | post the question as a normal comment, write `awaiting_human=true` into pinned-state, do nothing further this tick |
| `implementing` (awaiting_human) | new human comment arrived after agent's last action | (stays) | `codex resume --session <id>` with the new comment text, clear `awaiting_human` |
| `implementing` | PR opened successfully | `in_review` | flip label, post comment with PR link |
| `in_review` | (v0) | (terminal) | wait for human to merge or close manually; orchestrator does nothing |

Defer to Week 2 transitions: `(none) → decomposing`, `decomposing → ready/blocked`, `ready → implementing` (split out from pickup), `implementing → validating`, `validating → in_review` / `validating → ready`, `in_review → done` (auto-merge), `in_review → rejected`.

Pinned-state comment shape (one per issue, the orchestrator searches for the first comment containing `<!-- orchestrator-state -->`):

```
<!-- orchestrator-state -->
```json
{"codex_session_id": "...", "branch": "orchestrator/issue-7", "pr_number": 42, "awaiting_human": false, "last_seen_comment_id": 1234567}
```

## Polling loop

**Polling, not webhooks.** Single Linux/WSL2 host has no public endpoint; polling is one process, no inbound networking, easy to debug. Cost is negligible (~1 GET per minute, well under PyGithub's rate limit handling).

Tick (every 60s, configurable via `POLL_INTERVAL`):

1. `repo.get_issues(state="open", since=last_tick - 5min, sort="updated", direction="desc")` — only changed issues.
2. For each issue, read its current label, dispatch via `workflow.py` to the matching handler. Each handler re-reads the issue + pinned state immediately before acting (read-modify-write inside one tick) to avoid races with human comments.
3. `last_tick = now`; sleep `POLL_INTERVAL`.

`main.py` exposes `--once` (single tick then exit, used in dev/tests) and traps `SIGTERM`/`SIGINT` so the loop can shut down cleanly between ticks.

## Agent invocation

`agents.py` exposes one function for v0: `run_codex(prompt: str, cwd: Path, resume_session_id: str | None) -> CodexResult`.

```
codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --cd <worktree path> \
  --json \
  "<prompt>"        # or: codex resume --session <id> "<follow-up>"
```

Implementation:
- `subprocess.run(..., timeout=AGENT_TIMEOUT)` with `AGENT_TIMEOUT=1800` (30 min hard cap).
- Parse JSON-lines output to capture the session ID. **Day 1 task**: run `codex exec --help` on this host to confirm the exact flag name and output format before writing this module — the doc references `--dangerously-skip-permissions` but `codex` may use a slightly different flag name.
- Detect "blocked / needs human input" by a simple heuristic: agent finishes without committing changes AND its final message contains a question. Refine the heuristic only if it misfires.
- On timeout: kill the subprocess, post `@geserdugarov agent timed out, manual intervention needed`, leave label as-is, do not retry on this tick.
- Per-issue retry counter in pinned-state, hard cap 3/day; over the cap → ping the user and stop.

**Worktrees** are mandatory for self-bootstrap safety: `git worktree add ../wt-issue-<N> -b orchestrator/issue-<N> origin/main`. The orchestrator's own checkout (which is also the running process's source code) is never touched while codex edits files. After PR open, the worktree can be removed lazily on next pickup of the same issue, or kept until merge.

## Self-modification safety (R2 in agent's risk list)

Because the orchestrator is editing its own code, when a self-touching PR merges to `main` the running process is stale. v0 mitigation:

- Detect "self-touching merge" by checking if the merged PR modified any file under `orchestrator/`.
- On such a merge being detected at the start of a tick, log "exiting for self-update" and `sys.exit(0)`.
- Run the orchestrator under a shell wrapper: `while true; do python -m orchestrator.main; sleep 1; done`. Replace with `systemd Restart=always` when moving to VPS in Week 3.

## GitHub auth

- **Fine-grained PAT scoped to `podlodka-ai-club/spark-gap` only**, with read/write on Contents, Issues, Pull requests, Metadata.
- Stored in `.env` as `GITHUB_TOKEN`. `.env` is in `.gitignore`.
- `config.py` reads `.env` manually (5 lines, no dep).
- The token is passed to agent subprocesses via `env={"GH_TOKEN": token, ...}` so they can `git push`. PR opening is done by the orchestrator (PyGithub), not by the agent — narrower agent surface.
- Agent API keys (Anthropic / OpenAI): the orchestrator does **not** hold these. It relies on the user's existing global `claude` and `codex` CLI logins on this host.

## Phased rollout

| Days | Milestone | Done when |
|---|---|---|
| **Day 1** | Scaffold + read-only GitHub | `python -m orchestrator.main --once` lists open issues and prints their labels. `pyproject.toml`, `orchestrator/{__init__,main,github,config}.py`, `.env.example`, `.gitignore` all exist. PAT created and tested. |
| **Day 2** | Agent invocation works | `agents.run_codex(...)` against a throwaway worktree successfully edits a file, captures the session ID, and the orchestrator pushes the branch and opens a PR via PyGithub. Codex flag name verified. |
| **Day 3** | **Self-bootstrap milestone.** Polling loop end-to-end. | The bootstrap test issue (§Verification) is filed, orchestrator started, walked away from for 10 min, and a PR appears that the user can manually merge. Self-update wrapper script in place. |
| **Day 4–5** | HITL + harden | Codex-asks-question detection, resume on human reply, pinned-state comment, retry cap, `tests/test_workflow.py` for state transitions. |
| **Day 6–8** | `validating` stage | `claude` reviews each PR, posts review summary, flips `implementing → validating → in_review` or `validating → ready`. |
| **Day 9–10** | Auto-merge + `rejected` | Auto-merge on approve + green CI. PR-comment-resume during `in_review` with 10-min debounce. `rejected` flow on PR close. |
| **Day 11–12** | `decomposing` stage | Codex with a decomposition prompt; sub-issues created via PyGithub when the LLM judges the issue too large for one context. `blocked` label + dependency linking. |
| **Day 13** | VPS prep | Dockerfile, systemd unit, GitHub App migration (replaces fine-grained PAT), structured logging, `--status` CLI flag listing in-flight issues. |
| **Day 14** | Buffer / dogfood / docs | Update `docs/workflow.md` to reflect what actually shipped vs what's still future work. |

## Verification

**Bootstrap test issue** (file by hand on Day 3 morning):

> **Title:** Add a `hello()` function to the orchestrator package
> **Body:** Add `hello()` to `orchestrator/__init__.py` returning the literal string `"hello, world"`. Add `tests/test_hello.py` asserting the return value. Don't change anything else.

This exercises the entire v0 path: pickup → branch → codex run → push → PR → human merge. It edits the orchestrator's own code (true self-bootstrap), is too small to need decomposition, and is trivially verifiable.

**End-to-end test sequence:**

1. **Day 3 acceptance:** file the hello issue, run `python -m orchestrator.main`, walk away. Within 10 minutes: issue label transitions to `implementing` then `in_review`; a PR is open with the function + passing test; merging the PR works without breaking the running orchestrator (or it self-restarts cleanly via the wrapper). Pass criterion: PR exists and is mergeable.
2. **Day 5 acceptance:** file an issue that intentionally requires clarification ("Add a CLI flag — let me know what to name it"). Pass criterion: orchestrator posts the question as a comment, waits, and on a follow-up comment ("call it `--quiet`") completes the work and opens the PR.
3. **Day 9 acceptance:** file a "rename `hello()` to `greet()`" issue. Pass criterion: orchestrator opens PR and *auto-merges* once the user clicks Approve (no manual merge needed).
4. **Day 12 acceptance:** file a deliberately oversized issue ("Add `status`, `pause`, `resume` CLI subcommands"). Pass criterion: orchestrator creates 3 sub-issues linked to the parent and labels them `ready` / parent `blocked`.

**Unit tests** (Day 4): `tests/test_workflow.py` drives every state transition against an in-memory fake `Github`; no real network. Asserts label changes, comment posts, and pinned-state JSON shape.

## Open items to resolve on Day 1 (before writing code)

1. **Codex flag name & JSON output shape.** Run `codex exec --help` and a one-line dry run to confirm `--dangerously-bypass-approvals-and-sandbox` (or whatever the current flag is) and the location of the session ID in `--json` output.
2. **Commit identity for agent commits.** Suggest configuring `user.name = "agent-orchestrator"`, `user.email = "noreply+orchestrator@geserdugarov.dev"` in each agent worktree. Confirm the email you want.
3. **HITL @mention handle.** Suggest `@geserdugarov`. Confirm.

These are 15-minute checks, not blockers — they sit at the start of Day 1.

## Risks (carry-over from agent design)

- **R1 — Codex/Claude CLI output format drift.** Isolate parsing in `agents.parse_session_id()` with a fixture-backed unit test; fail loudly with a clear error if the shape changes.
- **R2 — Self-mutation while running.** Mitigated by the worktree + self-update wrapper above.
- **R3 — Runaway agent loops / token cost.** 30-min wall-clock timeout per invocation; max 3 retries per issue per day in pinned-state.
- **R4 — Host sleep on WSL2.** Acceptable for Week 1; Day 13 moves to VPS.
- **R5 — GitHub rate limits.** PyGithub handles backoff; 60s ticks are well under 5000 req/hr.
- **R6 — Race between human comments and orchestrator action.** Re-fetch issue + pinned-state immediately before each transition; treat any human comment newer than agent's last action as a pause signal.
- **R7 — Decomposition criteria unsolved in the design doc.** Don't try to solve in v0. Day 11–12 uses an "ask the LLM, take its word" heuristic.
