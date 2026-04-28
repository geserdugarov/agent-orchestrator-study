# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to local `codex` CLI subprocesses running in isolated git worktrees.

## Top-level layout

```
orchestrator/
  main.py      — entry point, polling loop, self-restart guard
  config.py    — env loading, secrets handling
  github.py    — PyGithub wrapper, label bootstrap, pinned-state comment
  agents.py    — codex CLI subprocess runner
  workflow.py  — state machine over labels
```

## Process model

There is **only one long-lived process**: `python -m orchestrator.main`. It is wrapped by `run.sh` so the loop can self-exit and be restarted with new code.

- **Trigger**: started manually (or by a wrapper). Optional `--once` for a single tick.
- **Tick cadence**: every `POLL_INTERVAL` seconds (default 60).
- **Self-restart guard** (`main.py:46`): each tick fetches `origin/main`; if it advanced past the process's startup SHA *and* the new commits touch `orchestrator/`, the loop exits 0 so the wrapper can re-exec the new code.
- **Signals**: SIGINT/SIGTERM set a flag; the current tick finishes, then the loop exits.

The codex agent runs as a **transient child subprocess**, not a daemon — spawned per tick when work is needed.

## Per-tick flow (`workflow.tick`)

For every open non-PR issue:

1. Read its workflow label (one of `decomposing/ready/blocked/implementing/validating/in_review/done/rejected`).
2. Dispatch by label. v0 implements only the unlabeled → `implementing` → `validating` → `in_review` happy path; other labels are logged and skipped.

Per-issue durable state lives in a single **"pinned" comment** on the issue (`<!--orchestrator-state {...json...}-->`), holding `codex_session_id`, `branch`, `pr_number`, `review_round`, `awaiting_human`, `last_action_comment_id`, etc. (`github.py:99`).

## Stage handlers

### `_handle_pickup` (no label → `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments.
- **Action**: posts a "picking this up" comment, sets label `implementing`, writes initial pinned state, then falls through into `_handle_implementing`.

### `_handle_implementing` (label `implementing`)
- **Trigger**: each tick while the label is `implementing`.
- **Input**: issue + comments + pinned state (`codex_session_id`, etc.).
- **Internal flow**:
  1. If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, **resume** the codex session with that text. If none, return.
  2. Otherwise: ensure a per-issue worktree at `<WORKTREES_DIR>/issue-<n>` on branch `orchestrator/issue-<n>`. Worktrees with unpushed commits are reused (crash recovery); otherwise force-removed and recreated from `origin/main`.
  3. If the worktree already has commits (recovered), skip codex and go straight to push.
  4. Else build the **implementer prompt** (issue body + recent comments + "commit, do not push") and `run_codex(...)`.
  5. Branch on result:
     - `timed_out` → park awaiting human (`@HITL_HANDLE`).
     - new commits + clean tree → `_on_commits`: push branch, open PR (or reuse an existing open one), comment `:sparkles: PR opened: #N`, set label `validating`, reset `review_round=0`.
     - new commits + dirty files → `_on_dirty_worktree`: park; refuse to publish a partial branch.
     - no new commits → `_on_question`: post the agent's last message as a HITL question, park.
- **Output**: a pushed branch + open PR + label moved to `validating`, OR a HITL park.

### `_handle_validating` (label `validating`)
- **Trigger**: each tick while label is `validating` (set after PR opens).
- **Input**: PR #, branch, `codex_session_id` (dev), pinned state, `review_round`.
- **Internal flow**:
  1. Awaiting-human path: same resume mechanic as implementing; on a successful pushed fix, bump `review_round` and stay in `validating` so the reviewer runs next tick.
  2. If `review_round >= MAX_REVIEW_ROUNDS` (default 3), park awaiting human.
  3. Otherwise spawn a **fresh codex session** with the **reviewer prompt** (read-only: `git log` / `git diff origin/main...HEAD`, must end with `VERDICT: APPROVED` or `VERDICT: CHANGES_REQUESTED`).
  4. Parse last `VERDICT:` marker (`_parse_review_verdict`):
     - `approved` → comment `:white_check_mark:` on the PR, set label `in_review`.
     - `unknown` (no marker) → park.
     - `changes_requested` → post the feedback to the PR, then **resume the developer's codex session** with the fix prompt; if it produces a new commit on a clean tree, push and increment `review_round` for next tick.
- **Output**: label moved to `in_review` (approval) OR a new fix commit + bumped round OR a HITL park.

### `_handle_in_review`
No-op in v0 — humans own the PR after codex approval.

## Codex subprocess (`agents.run_codex`)

- **Trigger**: called by handlers with a prompt + worktree path.
- **Command**: `codex exec [-C cwd | resume <sid>] --dangerously-bypass-approvals-and-sandbox --json -o .codex-last-message.txt <prompt>`.
- **Input**: prompt string; optional resume session id; timeout (`AGENT_TIMEOUT`/`REVIEW_TIMEOUT`).
- **Environment**: GitHub-token-bearing env vars are stripped (`GITHUB_TOKEN`, `GH_TOKEN`, etc.) so a prompt-injected agent cannot push or call the GitHub API.
- **Output**: `CodexResult(session_id, last_message, exit_code, timed_out, stdout, stderr)`. `session_id` is harvested by walking the JSONL events for any UUID-shaped value at `session_id`/`conversation_id`/etc.; `last_message` is read from the `-o` file.

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
| codex implementer | subprocess | `_handle_implementing` (no commits yet) or HITL resume | one shot per tick when needed |
| codex reviewer | subprocess (fresh session) | `_handle_validating`, round < max | one shot per tick |
| codex dev-fix | subprocess (resumed dev session) | reviewer says CHANGES_REQUESTED | one shot per tick |
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
   │                       ├─ run_codex(implement prompt)  ◄────┼──┐    │
   │                       ├─ commits+clean? push, open PR,     │  │    │
   │                       │     label=validating               │  │    │
   │                       ├─ dirty?  ─► park awaiting human ───┤  │    │
   │                       ├─ no commit? ─► park (question) ────┤  │    │
   │                       └─ timeout? ─► park ─────────────────┤  │    │
   │                                                            │  │    │
   │     validating ──► _handle_validating                      │  │    │
   │                       │                                    │  │    │
   │                       ├─ run_codex(review prompt, fresh)   │  │    │
   │                       │     parse VERDICT marker           │  │    │
   │                       │       APPROVED ─► label=in_review  │  │    │
   │                       │       CHANGES_REQUESTED:           │  │    │
   │                       │         post feedback on PR        │  │    │
   │                       │         run_codex(fix, resume) ────┘  │    │
   │                       │         push, ++review_round          │    │
   │                       │       UNKNOWN ─► park                 │    │
   │                       └─ round ≥ MAX_REVIEW_ROUNDS ─► park    │    │
   │                                                                │    │
   │     in_review ──► no-op (human owns the PR)                   │    │
   │                                                                │    │
   │   awaiting_human + new comment ─► resume dev codex ────────────┘    │
   │                                                                     │
   └─────────┬───────────────────────────────────────┬───────────────────┘
             │ subprocess                            │ subprocess (hardened)
             ▼                                       ▼
   ┌─────────────────────────────┐         ┌─────────────────────────────┐
   │  codex CLI                  │         │  git push                   │
   │  (per-issue worktree)       │         │  ─ GIT_ASKPASS tempfile     │
   │  ─ env: token vars stripped │         │  ─ no global/system config  │
   │  ─ --bypass sandbox         │         │  ─ hooks/helper disabled    │
   │  ─ JSONL → session_id       │         │  ─ refuses url-rewrite      │
   │  ─ -o .codex-last-message   │         └──────────────┬──────────────┘
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
| **workflow.py** | label-driven state machine, codex orchestration, push/PR |
| **agents.py** | spawn codex subprocess, capture session id + last message |
| **github.py** | issues, comments, labels, pinned state, PR open/comment |
| **config.py** | env + token loading (token kept outside REPO_ROOT) |
| **codex** | the only thing that writes code; runs in isolated worktree |

### State transition (label lifecycle, v0)

```
   (none) ──► implementing ──► validating ──► in_review
                  ▲                │
                  │   CHANGES_     │
                  └── REQUESTED ───┘   (until APPROVED or MAX_REVIEW_ROUNDS)

   any stage ──► [park: awaiting_human=true]  (timeout, dirty tree,
                       │                       question, push fail,
                       ▼                       unknown verdict, max rounds)
                 wait for new human comment ──► resume codex
```
