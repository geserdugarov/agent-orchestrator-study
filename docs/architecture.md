# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees. The dev/review backends are picked independently via `DEV_AGENT` / `REVIEW_AGENT` (default: claude implements, codex reviews) and validated at config load.

## Top-level layout

```
orchestrator/
  main.py      — entry point, polling loop, self-restart guard
  config.py    — env loading, secrets handling, backend validation
  github.py    — PyGithub wrapper, label bootstrap, pinned-state comment
  agents.py    — coding-agent subprocess runner (codex/claude dispatch)
  workflow.py  — state machine over labels
```

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main.py:46`): each tick fetches `origin/main`; if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits.

The coding agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

For every open non-PR issue:

1. Read its workflow label (one of `decomposing/ready/blocked/implementing/validating/in_review/done/rejected`).
2. Dispatch by label. v0 implements only the unlabeled → `implementing` → `validating` → `in_review` happy path; other labels are logged and skipped.

Per-issue durable state lives in a single **"pinned" comment** on the issue (`<!--orchestrator-state {...json...}-->`), holding `dev_agent` + `dev_session_id` (the backend that handled this issue and its session), `review_agent`, `branch`, `pr_number`, `review_round`, `retry_window_start` + `retry_count` (per-issue 24h fresh-spawn budget), `awaiting_human`, `last_action_comment_id`, etc. (`github.py:99`). The legacy `codex_session_id` key written before the configurable-backend rollout is still honored on read and treated as codex.

## Stage handlers

### `_handle_pickup` (no label → `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments.
- **Action**: posts a "picking this up" comment, sets label `implementing`, writes initial pinned state, then falls through into `_handle_implementing`.

### `_handle_implementing` (label `implementing`)
- **Trigger**: each tick while the label is `implementing`.
- **Input**: issue + comments + pinned state (`dev_agent`/`dev_session_id`, retry-budget keys, etc.).
- **Internal flow**:
  1. If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, **resume** the dev session via `run_agent(dev_agent, ...)` with that text. The backend is locked to whichever wrote `dev_session_id` (or the legacy `codex_session_id`) for this issue — flipping `DEV_AGENT` does not migrate in-flight issues. If no new comments, return.
  2. Otherwise: ensure a per-issue worktree at `<WORKTREES_DIR>/issue-<n>` on branch `orchestrator/issue-<n>`. Worktrees with unpushed commits are reused (crash recovery); otherwise force-removed and recreated from `origin/main`.
  3. If the worktree already has commits (recovered), skip the agent and go straight to push.
  4. Else gate the run on the **per-issue retry budget** (`MAX_RETRIES_PER_DAY`, default 3): a 24h window opens at the first counted spawn and resets after 24h; only fresh spawns count, not human-resume runs or recovered-worktree pushes. If the cap is exhausted, park awaiting human and return.
  5. Else build the **implementer prompt** (issue body + recent comments + "commit, do not push") and `run_agent(config.DEV_AGENT, ...)`. On a new session id, persist `dev_agent` + `dev_session_id`.
  6. Branch on result:
     - `timed_out` → park awaiting human (`@HITL_HANDLE`).
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating`, reset `review_round=0` and `retry_count=0` (next bounce back into implementing starts fresh).
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: a pushed branch + open PR + label moved to `validating`, OR a HITL park.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating` (set after PR opens).
- **Input**: PR #, branch, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), pinned state, `review_round`.
- **Internal flow**:
  1. Awaiting-human path: same resume mechanic as implementing (resume on the dev's locked backend); on a successful pushed fix, bump `review_round` and stay in `validating` so the reviewer runs next tick.
  2. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park awaiting human.
  3. Otherwise spawn a **fresh reviewer session** via `run_agent(config.REVIEW_AGENT, ...)` with the **reviewer prompt** (read-only: `git log` / `git diff origin/main...HEAD`, must end with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`); persist `review_agent` for traceability.
  4. Parse last `VERDICT:` marker (`_parse_review_verdict`):
     - `approved` → comment `:white_check_mark:` on the PR, set label `in_review`.
     - `unknown` (no marker) → park.
     - `changes_requested` → post the feedback to the PR, then **resume the developer's session** on its locked backend with the fix prompt; if it produces a new commit on a clean tree, push and increment `review_round` for next tick.
- **Output**: label moved to `in_review` (approval) OR a new fix commit + bumped round OR a HITL park.

### `_handle_in_review`
No-op in v0 — humans own the PR after the reviewer approves.

## Agent subprocess (`agents.run_agent`)

`run_agent(backend, prompt, cwd, ...)` dispatches to the per-backend runner (`_run_codex` / `_run_claude`); `backend` is one of `"codex"` / `"claude"` and is re-validated at call time so a misuse fails loudly. Both runners return a unified `AgentResult(session_id, last_message, exit_code, timed_out, stdout, stderr)`. `CodexResult` is kept as a transitional alias for one release.

- **Trigger**: called by handlers with a backend name + prompt + worktree path.
- **Codex command**: `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o .codex-last-message.txt <prompt>`. `last_message` is read from the `-o` file.
- **Claude command**: `claude -p --dangerously-skip-permissions --output-format stream-json --include-partial-messages --verbose <prompt>` (with `--resume <sid>` when resuming). `last_message` is parsed from the stream-json: prefers the terminal `{"type":"result","result":...}` event, falls back to the last `assistant`/`message` text content for schema-drift forward-compat.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT`/`REVIEW_TIMEOUT`).
- **Environment**:
  - GitHub-token-bearing env vars are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc.) so a prompt-injected agent cannot push or call the GitHub API. Provider auth (`ANTHROPIC_API_KEY`, OpenAI keychain, etc.) is intentionally left intact — that is how the agent reaches its own model.
  - `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` are injected from `AGENT_GIT_NAME`/`AGENT_GIT_EMAIL` (default `agent-orchestrator <agent-orchestrator@users.noreply.github.com>`) so agent commits are stamped with the orchestrator's identity, regardless of the host's `~/.gitconfig`.
- **Output**: `AgentResult(...)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at `session_id`/`conversation_id`/etc. (shared between both backends).

## Push path (`workflow._push_branch`)

The orchestrator (not the agent) pushes. The push is hardened against the agent-controlled worktree:
- Token delivered via `GIT_ASKPASS` tempfile, never argv.
- Detaches from `~/.gitconfig` and `/etc/gitconfig` (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`).
- Disables `core.hooksPath`, `credential.helper`, `core.fsmonitor`.
- Refuses to push if the worktree's local config has any `url.*.insteadOf`/`pushInsteadOf` rewrite.
- Pushes via explicit refspec `HEAD:refs/heads/<branch>` (no upstream stored).

## Summary of "what runs when"

| Component | Type | Trigger | Cadence |
|---|---|---|---|
| `main` polling loop | long-lived Python process | manual start (or wrapper) | every `POLL_INTERVAL`s |
| `workflow.tick` | function call | each loop iteration | once per tick |
| `_handle_*` per issue | function call | issue's workflow label | once per tick per open issue |
| implementer agent (`DEV_AGENT`) | subprocess | `_handle_implementing` (no commits yet, retry budget OK) or HITL resume | one shot per tick when needed |
| reviewer agent (`REVIEW_AGENT`) | subprocess (fresh session) | `_handle_validating`, round < max | one shot per tick |
| dev-fix agent | subprocess (resumed dev session, locked backend) | reviewer says CHANGES_REQUESTED | one shot per tick |
| `git push` | subprocess | after dev produces clean commits | per fix |
| self-restart check | git fetch + diff | start of each tick | every tick |

## Architecture schema

```
                     ┌──────────────────────────────────────┐
                     │   GitHub repo (REPO)                 │
                     │   ─ issues (with workflow labels)    │
                     │   ─ pinned state comment per issue   │
                     │   ─ branches / PRs                   │
                     └──────────────┬───────────────────────┘
                                    │ PyGithub (token)
                                    │
   ┌────────────────────────────────┴───────────────────────────────────┐
   │  orchestrator process  (python -m orchestrator.main)               │
   │  ───────────────────────────────────────────────────               │
   │   main.py                                                          │
   │     loop every POLL_INTERVAL s:                                    │
   │       1. self-restart check (origin/main moved & touches orch/?)   │
   │       2. workflow.tick(gh)                                         │
   │                    │                                               │
   │                    ▼                                               │
   │   workflow.tick → for each open issue → dispatch by label:         │
   │                                                                    │
   │     (no label) ──► _handle_pickup ──► label=implementing ──┐       │
   │                                                            │       │
   │     implementing ──► _handle_implementing ─────────────────┤       │
   │                       │                                    │       │
   │                       ├─ ensure worktree                   │       │
   │                       ├─ retry budget? ─► park if exhausted│       │
   │                       ├─ run_agent(DEV_AGENT, prompt) ◄────┼──┐    │
   │                       ├─ commits+clean? push, open PR,     │  │    │
   │                       │     label=validating               │  │    │
   │                       ├─ dirty?  ─► park awaiting human ───┤  │    │
   │                       ├─ no commit? ─► park (question) ────┤  │    │
   │                       └─ timeout? ─► park ─────────────────┤  │    │
   │                                                            │  │    │
   │     validating ──► _handle_validating                      │  │    │
   │                       │                                    │  │    │
   │                       ├─ run_agent(REVIEW_AGENT, fresh)    │  │    │
   │                       │     parse VERDICT marker           │  │    │
   │                       │       APPROVED ─► label=in_review  │  │    │
   │                       │       CHANGES_REQUESTED:           │  │    │
   │                       │         post feedback on PR        │  │    │
   │                       │         run_agent(dev, fix, resume) ─┘  │    │
   │                       │         push, ++review_round          │    │
   │                       │       UNKNOWN ─► park                 │    │
   │                       └─ round ≥ MAX_REVIEW_ROUNDS ─► park    │    │
   │                                                                │    │
   │     in_review ──► no-op (human owns the PR)                   │    │
   │                                                                │    │
   │   awaiting_human + new comment ─► resume dev (locked backend) ─┘    │
   │                                                                     │
   └─────────┬───────────────────────────────────────┬───────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  coding-agent CLI           │         │  git push                   │
   │  (codex or claude,          │         │  ─ GIT_ASKPASS tempfile     │
   │   per-issue worktree)       │         │  ─ no global/system config  │
   │  ─ env: GH tokens stripped  │         │  ─ hooks/helper disabled    │
   │  ─ env: GIT_AUTHOR/COMMITTER│         │  ─ refuses url-rewrite      │
   │     stamped (orchestrator)  │         └──────────────┬──────────────┘
   │  ─ provider auth left alone │                        │
   │  ─ --bypass / --skip perms  │                        │
   │  ─ JSONL → session_id       │                        │
   │  ─ last_message: -o (codex) │                        │
   │     or stream-json (claude) │                        │
   └──────────────┬──────────────┘                        │
                  │ commits to                            │ pushes branch to
                  ▼                                       ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │  git worktree:  <WORKTREES_DIR>/issue-<n>                           │
   │  branch:        orchestrator/issue-<n>                              │
   │  ─ created from origin/main (or reused if has unpushed commits)     │
   └─────────────────────────────────────────────────────────────────────┘
```

### Roles in one line

| Component | Role |
|---|---|
| **main.py** | polling loop + signal handling + self-restart |
| **workflow.py** | label-driven state machine, agent orchestration, push/PR |
| **agents.py** | dispatch + spawn codex/claude subprocess, capture session id + last message |
| **github.py** | issues, comments, labels, pinned state, PR open/comment |
| **config.py** | env + token loading (token kept outside REPO_ROOT), backend validation |
| **codex / claude** | the only things that write code; run in isolated worktree |

### State transition (label lifecycle, v0)

```
   (none) ──► implementing ──► validating ──► in_review
                  ▲                │
                  │   CHANGES_     │
                  └── REQUESTED ───┘   (until APPROVED or MAX_REVIEW_ROUNDS)

   any stage ──► [park: awaiting_human=true]  (timeout, dirty tree,
                       │                       question, push fail,
                       │                       unknown verdict, max rounds,
                       ▼                       retry budget exhausted)
                 wait for new human comment ──► resume dev (locked backend)
```
