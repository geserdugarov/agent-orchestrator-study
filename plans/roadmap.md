# Agent Orchestrator MVP — Implementation Plan

## Status as of 2026-05-03

**v0 self-bootstrap path is shipped.** The scaffold, polling loop, codex invocation, hardened push, PR open, and the (no label → `implementing` → `validating` → `in_review`) state machine all exist on `main` (commits `eb87246` … `06c7ea2`, plus the codex-review cycle). The orchestrator can be pointed at `podlodka-ai-club/spark-gap` and run end-to-end against the bootstrap test issue.

Done:

- `orchestrator/{__init__,main,workflow,github,agents,config}.py`, `pyproject.toml`, `.env.example`, `.gitignore`, `run.sh` — all in place.
- Polling loop with `--once`, `SIGTERM`/`SIGINT`-clean shutdown, ancestry-aware self-update detection (`main.py` exits when `origin/<BASE_BRANCH>` advances past the running HEAD with changes under `orchestrator/`).
- `run.sh` self-restart wrapper that pulls the same `BASE_BRANCH` the Python code uses.
- `GitHubClient`: list issues, workflow-label r/w, post comment, pinned-state JSON r/w, open/find PR, idempotent label bootstrap (graceful on under-scoped PAT).
- Pinned-state JSON comment with `<!--orchestrator-state ...-->` marker (note: differs slightly from the original plan's `<!-- orchestrator-state -->` plus fenced JSON — the marker is now inline with the JSON payload).
- `run_codex` against `codex exec` and `codex exec resume`, `--dangerously-bypass-approvals-and-sandbox`, `--json`, `-o <last-message-file>`. Session ID parsed by walking JSONL events for any UUID-shaped value at `session_id`/`conversation_id`/etc.
- `_handle_implementing` covers: fresh run, resume on human follow-up, timeout → park on `awaiting_human`, no-commits-but-message → park as question, commits + clean tree → push + open PR + flip to `in_review`, commits + dirty tree → park (refuse to push partial branch), push failure → park.
- Worktrees at `WORKTREES_DIR/issue-<N>` (default `../wt-orchestrator/issue-<N>`), reused when prior commits remain unpushed so a crash between commit and push doesn't burn another codex run.
- Multi-handle HITL mentions via comma-separated `HITL_HANDLE` (commits `34853f9`, `b8e5fb2`).
- `tests/test_config.py` covers HITL handle parsing.

Done beyond the original plan (security hardening from review iterations):

- **PAT never leaves orchestrator-controlled surfaces.** The agent's environment is scrubbed of `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_TOKEN`/`GITHUB_PAT`/`GH_ENTERPRISE_TOKEN`/`GITHUB_ENTERPRISE_TOKEN`/`GH_HOST` (`agents.py`). The orchestrator owns all GitHub writes; the agent has no path to push or call the API as us.
- **PAT cannot live in `REPO_ROOT/.env`** (which is agent-readable via relative path from the worktree). `config._load_dotenv` actively rejects secret keys found there with a clear stderr message. Token must come from the process environment or a file outside `REPO_ROOT` — default `~/.config/<owner>/<repo>/token`, derived from `REPO` (commit `06c7ea2`).
- **Hardened `git push`**: askpass tempscript reads token from env (token never in argv / `/proc/<pid>/cmdline`); `core.hooksPath=/dev/null`, `credential.helper=`, `core.fsmonitor=`, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `GIT_CONFIG_NOSYSTEM=1` to defeat agent-planted hooks, helpers, fsmonitor programs, and `~/.gitconfig` `url.insteadOf` rewrites that could redirect the auth URL. Also refuses to push when the local config carries `url.*.insteadOf`/`pushInsteadOf` rules. Push errors are logged with the token scrubbed (commits `c9f1bb1`, `26d9a1f`).
- **Refuse incomplete branches**: `_worktree_dirty_files` blocks the push when codex committed only part of its work, parks on `awaiting_human` instead of publishing a misleading PR.
- **Idempotent PR open**: `find_open_pr` recovers when a previous tick crashed between `create_pull` and the relabel — reuses the existing open PR rather than 422-ing.
- **Idempotent label bootstrap**: `ensure_workflow_labels` swallows 403s with an actionable message so the loop keeps running while the PAT is being fixed.

Open items from the Day-1 checklist:

1. **Codex flag name & JSON output shape** — resolved during Day 2 (`--dangerously-bypass-approvals-and-sandbox`, `--json`, last-message-via-`-o`, UUID walker for session ID).
2. **Commit identity for agent commits** — resolved in `7f9c6e2`. `agents._agent_env` injects `GIT_AUTHOR_*` / `GIT_COMMITTER_*` from `AGENT_GIT_NAME` / `AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) into every spawn, overriding any `~/.gitconfig` without touching it.
3. **HITL @mention handle** — resolved as a configurable list (`HITL_HANDLE`, default `geserdugarov,and-semakin,garudainfo55`).

Done in Day 4–5 (post-original-plan, shipped together as `7f9c6e2`):

- **Per-issue retry budget.** `MAX_RETRIES_PER_DAY` (default 3, `0` = unbounded) caps fresh implementing-codex spawns within a 24h window opened at the first counted attempt; resumes on human reply and recovered-worktree pushes don't count. Pinned state grew `retry_window_start` + `retry_count`. Forward progress (`_on_commits`) resets the budget.
- **Agent commit identity stamped via env.** See Day-1 item 2 above.
- **Fake-`Github` test harness landed.** `tests/fakes.py` (199 lines) plus a 700-line expansion of `tests/test_workflow.py` cover state transitions, the resume-on-human-reply path, retry-budget gating, and the new agent-identity env stamping. The original-plan TODO for `tests/test_workflow.py` is no longer outstanding.

Done in Day 6 (validating stage):

- `validating` stage as a review loop. Every PR opened by the implementer enters `validating`. A fresh reviewer-agent session (`run_agent(config.REVIEW_AGENT, ...)`) reviews `git diff origin/<base>...HEAD` against the issue and emits `VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED`. On approval the label flips to `in_review` and humans take over. On changes requested the dev session is resumed (on whichever backend started the issue) with the feedback, the fix is pushed, and the review re-runs. Capped at `MAX_REVIEW_ROUNDS` (default 3) before parking on `awaiting_human`.
- Review feedback and approval comments go to the **PR** via `pr.create_issue_comment` (`gh.pr_comment`). HITL pings (timeouts, cap reached, malformed verdict) stay on the issue.
- Pinned state gained `review_round`, `last_review_session_id`, `last_review_at`, plus `review_agent` (which backend ran the review) added with the configurable-backend rollout in `8f91df5`.
- `tests/test_workflow.py` covers `_parse_review_verdict` against APPROVED / CHANGES_REQUESTED / inline marker / case-insensitive / last-marker-wins / missing-marker / empty input.
- `_park_awaiting_human` extracted from the existing inline parking blocks; `_resume_developer_on_human_reply` extracted so both `implementing` and `validating` share the human-reply resume path.
- **Configurable dev/review backends (`8f91df5`).** `DEV_AGENT` (default `claude`) and `REVIEW_AGENT` (default `codex`) route each spawn through `run_agent` to either `_run_codex` or `_run_claude`; both backends return a unified `AgentResult` (with `CodexResult` kept as a one-release alias). Both values are validated at config load — a typo aborts startup. `CLAUDE_BIN` is no longer dormant. Pinned state grew `dev_agent` + `dev_session_id` (replacing `codex_session_id`); the legacy key is still honored on read and treated as codex, so in-flight issues stay locked to whichever backend started them across a `DEV_AGENT` flip.

Not yet done:

- Auto-merge on approve+green-CI, comment debounce, `decomposing`, `blocked`/`rejected` flows, Dockerfile / systemd / GitHub App migration.

## Context

The goal documented in `docs/workflow.md` is an "orchestrator": a long-running process that watches GitHub Issues, drives them through a fixed 4-stage workflow (Decompose → Implement → Validate → Accept), and uses local AI coding-agent CLIs (`codex`, `claude`) to do the actual work. State lives in GitHub Issues themselves (one label per issue, plus pinned JSON state in a comment) so the orchestrator stays stateless and the user can watch progress on github.com.

The driver of this plan is the user's twin constraint: **2-week total budget** and "switch to self-development as soon as possible" — i.e. the orchestrator has to become useful for resolving issues in *its own repo* well before the 2 weeks are up, so the rest of the build can itself be done by the orchestrator (compiler-bootstrap principle). The intended outcome is a v0 by **Day 3** that handles the (no-label → implementing → in_review) happy path end-to-end against this very repo, with the documented `decomposing` and `validating` stages added in the second week.

User-confirmed decisions: **aggressive scope cut** for v0, **Python 3.12**, **fine-grained PAT scoped to this repo only** for GitHub auth.

## v0 scope (Day 1–3, self-bootstrap milestone)

Ship only the critical path; everything else is iteration:

- New issue with no label → orchestrator picks it up → labels `implementing` → spawns `codex` in a worktree → pushes branch → opens PR → labels `in_review`. **`decomposing` and `validating` stages are skipped entirely in v0.**
- Human reviews PR on github.com and merges manually. No auto-merge in v0.
- HITL: when codex output indicates it's blocked / needs input, the orchestrator posts the question as an issue comment, leaves the issue at `implementing`, and waits for a fresh human comment before resuming the codex session.
- Concurrency: **one agent at a time** (a `Lock` in `main.py`). Issues queue.

Defer to Week 2 (Day 6–14): ~~`validating` stage with claude PR review~~ (now done as a codex-on-codex loop), auto-merge on approve+green-CI, comment debounce, `decomposing`, `blocked`/`rejected` flows, parallel agents, container isolation, VPS deploy, GitHub App migration.

## Tech stack

- **Python 3.12** (already on host).
- One dependency: **PyGithub**. No `gh` CLI install; `requests`/`httpx`/`octokit` not needed.
- Standard library `subprocess`, `pathlib`, `json`, `logging`, `signal`, `time`. No `python-dotenv` — read `.env` manually in `config.py`.
- Tests: stdlib `unittest` against a faked `Github` object. No mocking of `codex`/`claude` — those are integration-tested via the bootstrap issue.

## File layout

Flat package, ~5 files for v0. No premature abstraction. Current shape on disk:

```
/home/geserdugarov/git/agent-orchestrator-study/
├── README.md
├── docs/workflow.md                (Russian-language source of truth for label/stage semantics)
├── orchestrator/
│   ├── __init__.py
│   ├── main.py                     # polling loop, --once, --log-level, SIGTERM/SIGINT, ancestry-aware self-update detection
│   ├── workflow.py                 # state machine + worktree mgmt + hardened push (heart of v0)
│   ├── github.py                   # PyGithub wrapper: issues, labels, comments, pinned-state JSON, open/find PR, label bootstrap
│   ├── agents.py                   # codex spawn/resume, session-ID walker, env scrub, last-message capture
│   └── config.py                   # .env loader (rejects secrets), token resolution from env or ~/.config/<owner>/<repo>/token, HITL parsing
├── pyproject.toml                  # PEP 621, deps = ["PyGithub>=2.1"]
├── run.sh                          # self-restart wrapper, BASE_BRANCH-aware pull
├── .env.example                    # REPO, POLL_INTERVAL, AGENT_TIMEOUT, HITL_HANDLE, *_BIN (no GITHUB_TOKEN — banned from .env)
├── .gitignore                      # .env, __pycache__, .venv, .codex/, .claude/, …
└── tests/
    ├── __init__.py
    └── test_config.py              # HITL handle parsing
                                    # test_workflow.py — TODO (Day 4)
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

Pinned-state comment shape (one per issue, found by the marker `<!--orchestrator-state` and parsed via `PINNED_STATE_RE`):

```
<!--orchestrator-state {"dev_agent":"claude","dev_session_id":"…","review_agent":"codex","last_review_session_id":"…","last_review_at":"…","branch":"orchestrator/issue-7","pr_number":42,"review_round":0,"retry_window_start":"…","retry_count":1,"awaiting_human":false,"last_action_comment_id":1234567,"created_at":"…","last_agent_action_at":"…"}-->
```

The orchestrator-owned keys today: `dev_agent`, `dev_session_id`, `review_agent`, `last_review_session_id`, `last_review_at`, `branch`, `pr_number`, `review_round`, `retry_window_start`, `retry_count`, `awaiting_human`, `last_action_comment_id`, `created_at`, `last_agent_action_at`. Issues created before the configurable-backend rollout still carry the legacy `codex_session_id`; readers fall back to it and treat it as a codex session.

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

Implementation (current state in `agents.py` / `workflow.py`):
- `subprocess.run(..., timeout=AGENT_TIMEOUT)` with `AGENT_TIMEOUT=1800` (30 min hard cap). **Done.**
- Parse JSON-lines output to capture the session ID. **Done** — `parse_session_id` walks JSONL events for any UUID at `session_id`/`conversation_id`/`thread_id`/`session`/`id` (or anywhere nested).
- Detect "blocked / needs human input" by a simple heuristic: agent finishes without committing changes. **Done** — implemented as `not _has_new_commits(wt)` after the codex run. The final message captured via `-o <last-message-file>` is quoted into the HITL comment as the question text.
- On timeout: kill the subprocess, post `<HITL mention> agent timed out…`, park on `awaiting_human=true`, do not retry until a human comments. **Done.**
- Per-issue retry budget in pinned state, hard cap `MAX_RETRIES_PER_DAY`/day (default 3); over the cap → park on `awaiting_human` with a HITL ping. **Done in `7f9c6e2`** (24h fixed window per issue, opened on first counted attempt; resumes don't count; `_on_commits` resets it).
- Agent commit identity. **Done in `7f9c6e2`** via `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env injection from `AGENT_GIT_NAME`/`AGENT_GIT_EMAIL` on every spawn — overrides any `git config user.{name,email}` without needing per-worktree config and leaves the host `~/.gitconfig` untouched.

**Worktrees** are mandatory for self-bootstrap safety: `git worktree add ../wt-issue-<N> -b orchestrator/issue-<N> origin/main`. The orchestrator's own checkout (which is also the running process's source code) is never touched while codex edits files. After PR open, the worktree can be removed lazily on next pickup of the same issue, or kept until merge.

## Self-modification safety (R2 in agent's risk list)

Because the orchestrator is editing its own code, when a self-touching PR merges to `main` the running process is stale. v0 mitigation:

- Detect "self-touching merge" by checking if the merged PR modified any file under `orchestrator/`.
- On such a merge being detected at the start of a tick, log "exiting for self-update" and `sys.exit(0)`.
- Run the orchestrator under a shell wrapper: `while true; do python -m orchestrator.main; sleep 1; done`. Replace with `systemd Restart=always` when moving to VPS in Week 3.

## GitHub auth

- **Fine-grained PAT scoped to `podlodka-ai-club/spark-gap` only**, with read/write on Contents, Issues, Pull requests, Metadata.
- **Token storage (revised from original plan).** The PAT is **not** stored in `.env` — that file is reachable from the agent's worktree via relative path. It must come from either the orchestrator's process environment (`GITHUB_TOKEN=…` exported before launch) or a file outside `REPO_ROOT`. Default file path is `~/.config/<owner>/<repo>/token`, derived from `REPO`; override with `ORCHESTRATOR_TOKEN_FILE`. `config._load_dotenv` actively rejects `GITHUB_TOKEN`/`GH_TOKEN`/`GIT_TOKEN`/etc. found in `.env` with a clear stderr message.
- `config.py` reads `.env` manually (no dep). The agent process never receives `GITHUB_TOKEN` (or any `GH_*` / `GIT_TOKEN` synonym) — `agents.run_codex` strips them from the inherited environment.
- The orchestrator does the `git push` itself via an askpass tempscript (token in env, never in argv) and does the PR open via PyGithub. The agent only edits files and commits inside its worktree. See "Done beyond the original plan" above for the full list of push hardening.
- Agent API keys (Anthropic / OpenAI): the orchestrator does **not** hold these. It relies on the user's existing global `claude` and `codex` CLI logins on this host.

## Phased rollout

| Days | Milestone | Status |
|---|---|---|
| **Day 1** | Scaffold + read-only GitHub | ✅ Done. `pyproject.toml`, `orchestrator/{__init__,main,github,config}.py`, `.env.example`, `.gitignore`, PAT all in place. |
| **Day 2** | Agent invocation works | ✅ Done. `agents.run_codex(...)` confirmed, codex flags verified, askpass-based push and PyGithub PR open both wired up. |
| **Day 3** | **Self-bootstrap milestone.** Polling loop end-to-end. | ✅ Done. Polling loop, signal handling, ancestry-aware self-update detection, and `run.sh` wrapper all merged (eb87246, 9e5eac6). |
| **Day 4–5** | HITL + harden | 🟢 Done. Question detection (no-commits heuristic), resume on human follow-up, pinned-state JSON, dirty-tree refusal, push-failure parking, comprehensive HITL mention plumbing all in place; per-issue retry budget (`MAX_RETRIES_PER_DAY`, 24h window) and agent commit-identity stamping landed in `7f9c6e2`; `tests/fakes.py` + an expanded `tests/test_workflow.py` now cover state transitions against an in-memory fake `Github`. |
| **Day 6–8** | `validating` stage | 🟢 Done. `_handle_validating` runs a fresh review, posts feedback to the PR, resumes the dev session for fixes, re-reviews, and caps at `MAX_REVIEW_ROUNDS` rounds before parking on `awaiting_human`. Transitions to `in_review` on `VERDICT: APPROVED`. The dev/review backend split is now config-driven (`DEV_AGENT` / `REVIEW_AGENT`), defaults to claude implements + codex reviews, and `CLAUDE_BIN` is no longer dormant. The dev backend for an in-flight issue is locked in pinned state (`dev_agent`/`dev_session_id`, with legacy `codex_session_id` falling back to codex). |
| **Day 9–10** | Auto-merge + `rejected` | ⬜ Not started. Add `in_review` handler that watches PR state + check runs, auto-merges on approve+green, transitions to `done`. Add `rejected` on PR close-without-merge. PR-comment-resume during `in_review` with 10-min debounce. |
| **Day 11–12** | `decomposing` stage | ⬜ Not started. New `_handle_decomposing` driving codex with a decomposition prompt; sub-issues created via PyGithub; `blocked` label + dependency linking when sub-issues exist. |
| **Day 13** | VPS prep | ⬜ Not started. Dockerfile, systemd unit (`Restart=always` replaces `run.sh`), GitHub App migration to drop the PAT, structured logging, `--status` CLI flag listing in-flight issues. |
| **Day 14** | Buffer / dogfood / docs | ⬜ Not started. Update `docs/workflow.md` to reflect what actually shipped (incl. the inline pinned-state marker change and the new token-storage rules). |

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

**Unit tests** (Day 4 — **done in `7f9c6e2`**): `tests/test_workflow.py` drives state transitions against an in-memory fake `Github` (`tests/fakes.py`, ~200 lines); covers pickup, implementing-with-resume, retry-budget gating, agent-identity env stamping, validating round-trips, and pinned-state JSON shape. `tests/test_config.py` covers HITL handle parsing, retry-cap parsing, and agent-identity env defaults. `tests/test_agents.py` covers the per-backend dispatch added in `8f91df5`.

## Open items from Day-1 checklist

1. **Codex flag name & JSON output shape.** ✅ Resolved during Day 2: `codex exec [-C <cwd>] --dangerously-bypass-approvals-and-sandbox --json -o <last-message-file> "<prompt>"` (resume variant: `codex exec resume <session-id> "<follow-up>"` — does **not** accept `-C`, so we rely on `subprocess` cwd).
2. **Commit identity for agent commits.** ✅ Resolved in `7f9c6e2` via `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env injection from `AGENT_GIT_NAME`/`AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`). Env vars beat `git config user.{name,email}` at every scope, so the host's `~/.gitconfig` and the per-worktree config are both left untouched.
3. **HITL @mention handle.** ✅ Resolved as a configurable comma-separated list (`HITL_HANDLE`); current default is `geserdugarov,and-semakin,garudainfo55`.

## Risks (carry-over from agent design)

- **R1 — Codex/Claude CLI output format drift.** Isolate parsing in `agents.parse_session_id()` with a fixture-backed unit test; fail loudly with a clear error if the shape changes.
- **R2 — Self-mutation while running.** Mitigated by the worktree + self-update wrapper above.
- **R3 — Runaway agent loops / token cost.** 30-min wall-clock timeout per invocation; max 3 retries per issue per day in pinned-state.
- **R4 — Host sleep on WSL2.** Acceptable for Week 1; Day 13 moves to VPS.
- **R5 — GitHub rate limits.** PyGithub handles backoff; 60s ticks are well under 5000 req/hr.
- **R6 — Race between human comments and orchestrator action.** Re-fetch issue + pinned-state immediately before each transition; treat any human comment newer than agent's last action as a pause signal.
- **R7 — Decomposition criteria unsolved in the design doc.** Don't try to solve in v0. Day 11–12 uses an "ask the LLM, take its word" heuristic.
