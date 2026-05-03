# Architecture of the Current Implementation

Single-process **polling orchestrator** that drives GitHub issues through a label-based state machine, delegating the actual coding work to a configurable coding-agent CLI (`codex` or `claude`) running as a subprocess in isolated git worktrees. The dev/review/decompose backends are picked independently via `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` (default: claude decomposes, claude implements, codex reviews) and validated at config load. New unlabeled issues route through a `decomposing` stage that asks the decomposer agent for a structured manifest: `decision=single` flips the issue to `ready` and the implementer takes over; `decision=split` creates child issues, persists the dep graph, and parks the parent on `blocked` until `_handle_blocked` walks the children. Once the reviewer approves and the PR is mergeable with green CI, the orchestrator can merge it itself (gated by `AUTO_MERGE`, default off) and close the issue with `done`; PRs closed without merge land on `rejected`. Decomposition can be disabled with `DECOMPOSE=off`, which reverts to the legacy direct-to-`implementing` pickup.

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

Each tick, `gh.list_pollable_issues()` yields all open non-PR issues plus closed non-PR issues still labeled `in_review`. The closed-`in_review` sweep is what makes the manual-merge path land cleanly: a human-merged PR with a `Resolves #N` footer auto-closes issue N before the orchestrator can flip the label, and without the sweep `_handle_in_review` would never run on it.

For every yielded issue:

1. Read its workflow label (one of `decomposing/ready/blocked/implementing/validating/in_review/done/rejected`).
2. Dispatch by label. The full lifecycle (no label → `decomposing` → `ready`/`blocked` → `implementing` → `validating` → `in_review` → `done`/`rejected`) is implemented; `done` and `rejected` are terminal no-ops, every other label routes to its handler.

Per-issue durable state lives in a single **"pinned" comment** on the issue (`<!--orchestrator-state {...json...}-->`), holding `dev_agent` + `dev_session_id` (the backend that handled this issue and its session), `review_agent`, `decomposer_agent` + `decomposer_session_id` (parents only; same lock-on-first-spawn semantics as `dev_agent`), `children` (parents only — child issue numbers, used by `_handle_blocked`), `dep_graph` (parents only — `{child_idx_str: [child_idx, ...]}` because GitHub has no first-class blocks-issue relation), `decomposed_at`, `pickup_comment_id`, `branch`, `pr_number`, `review_round`, `retry_window_start` + `retry_count` (per-issue 24h fresh-spawn budget; shared between implementing and decomposing), `awaiting_human`, `last_action_comment_id`, `pr_last_comment_id` (in_review high-watermark across the issue thread + PR conversation comments, which share the IssueComment id space; seeded at validating → in_review handoff so the orchestrator's own automated comments don't replay as fresh feedback, and bumped past any park comment so an HITL ping doesn't replay either), `pr_last_review_comment_id` (separate watermark for inline PR review comments, which live in their own id space), `agent_approved_sha` (the head SHA the reviewer agent OK'd; `_handle_in_review` keys AUTO_MERGE on this since the agent posts an issue comment, not a real PR review), `merged_at` / `closed_without_merge_at` (terminal stamps), etc. (`github.py:99`). The legacy `codex_session_id` key written before the configurable-backend rollout is still honored on read and treated as codex.

## Stage handlers

### `_handle_pickup` (no label → `decomposing` or `implementing`)
- **Trigger**: open issue with no workflow label.
- **Input**: issue title/body/comments; `config.DECOMPOSE` (default on).
- **Action**: posts a "picking this up" comment, anchors `pickup_comment_id` for the in_review legacy migration, then routes:
  - `DECOMPOSE=on` → label `decomposing`, fall into `_handle_decomposing`.
  - `DECOMPOSE=off` → label `implementing`, fall into `_handle_implementing` (legacy bootstrap path).

### `_handle_decomposing` (label `decomposing`)
- **Trigger**: each tick while the label is `decomposing`.
- **Input**: issue + comments + pinned state (`decomposer_agent`/`decomposer_session_id`, retry-budget keys).
- **Internal flow**:
  1. If `awaiting_human`: re-check for new human comments since `last_action_comment_id`; if any, **resume** the decomposer session via `run_agent(decomposer_agent, ...)` with that text. The backend is locked to whichever wrote `decomposer_session_id` for this issue. If no new comments, return.
  2. Otherwise: gate on the **per-issue retry budget** (shared with `implementing` — both consume the same daily counter on purpose). If exhausted, park awaiting human.
  3. Ensure a per-issue worktree (read-only — the decomposer never commits, but the agent still wants `git ls-files` / `wc -l` context).
  4. Build the **decomposer prompt** (issue body + recent comments + sizing rule of thumb + the manifest schema) and `run_agent(config.DECOMPOSE_AGENT, ...)`. On a new session id, persist `decomposer_agent` + `decomposer_session_id`.
  5. **Read-only check**: if the worktree now has new commits or dirty files, park awaiting human. The decomposer is supposed to be read-only; otherwise the implementer recovery path in `_handle_implementing` would later see the leftover commits and push decomposer-authored work as if it were implementation.
  6. Parse the manifest from `result.last_message` via `_parse_manifest` (regex captures the fenced ` ```orchestrator-manifest ` block; structural validation rejects unknown decisions, bad child shape, self-deps, cycles, and >10 children):
     - **invalid manifest** → park awaiting human with the parse error and the agent's last message quoted (same recovery as a malformed reviewer verdict).
     - **no fenced block** → treat as a question; park with the message quoted (mirrors `_on_question` from implementing).
     - **decision == "single"** → post a one-line "fits in one context" comment with the rationale, set label `ready`, stamp `decomposed_at`. `_handle_ready` picks it up next tick.
     - **decision == "split"** → crash-safe creation in three phases. (a) For each child call `gh.create_child_issue(...)` (which prepends `Parent: #<n>` to the body, no auto-close keyword) with label `blocked` regardless of dependencies, and seed the child's pinned state with `parent_number`; child-state seeding is mandatory — failure persists the partial `children` list and parks awaiting human (no orphan child is left runnable). (b) Persist `children` and `dep_graph` (`{child_idx_str: [child_idx, ...]}`) on the parent, post the summary comment, set parent label `blocked`, stamp `decomposed_at`. (c) Activate no-dep children by flipping their label `blocked` → `ready`; this is best-effort because `_handle_blocked`'s walk also treats no-dep children as deps-satisfied, so a crashed activation step is recovered on the next tick.
- **Pre-flight (half-finished recovery)**: if `children` is already set on the parent but the label is still `decomposing`, a prior tick crashed between child creation and the parent label flip. Re-running the decomposer would create duplicates, so the handler short-circuits: when not awaiting_human, flip the parent to `blocked` and let `_handle_blocked` activate children; when awaiting_human (parent state was parked mid-creation), hold and require manual intervention.
- **Pre-flight (DECOMPOSE kill switch, mid-flight)**: if `config.DECOMPOSE` is off when this handler runs (operator restarted with the rollout disabled while the issue was already labeled `decomposing` or parked there), bail out before any decomposer spawn: post a routing comment, clear the decomposer-side `awaiting_human`/`park_reason` so the legacy implementing flow doesn't trip its resume branch on stale state, flip the label to `implementing`, and fall into `_handle_implementing`. The half-finished recovery above runs first and is unaffected — abandoning orphan children that already exist on GitHub just because new decompositions are now disabled is not what a kill switch should do.
- **Output**: parent label moved to `ready` / `blocked`, OR a HITL park.

### `_handle_ready` (label `ready` → `implementing`)
- **Trigger**: each tick while the label is `ready`. Reached by either a `single`-decision parent or by a freshly-created child.
- **Action**: if `pickup_comment_id` is unset (the common path for auto-created children), post a "picking this up; starting implementation" comment and seed `created_at` + `pickup_comment_id` so the in_review legacy migration has its anchor. Bump `last_action_comment_id` to the latest visible comment id (one-way ratchet) so any human comments posted while the parent was `decomposing` / `blocked` are marked consumed — the implementer reads them at spawn via `_recent_comments_text`, so they must NOT later resurface as fresh PR feedback in `_handle_in_review`'s watermark seed (which would bounce the PR back to validating after merge readiness). Then flip the label to `implementing` and fall through into `_handle_implementing` on the same tick.

### `_handle_blocked` (label `blocked`)
- **Trigger**: each tick while the label is `blocked`.
- **Input**: pinned `children` (parent only), optional `dep_graph` (parent only — `{child_idx_str: [child_idx, ...]}`), `parent_number` (child only — seeded by the decomposer at child-creation time).
- **Internal flow**:
  1. If no `children` recorded but `parent_number` is set → no-op. The parent's `_handle_blocked` walks the dep graph and flips this child to `ready` when its dependencies finish; this tick has nothing to do.
  2. If no `children` and no `parent_number` (manual relabel suspected), park awaiting human.
  3. Read each child's current workflow label via `gh.get_issue(n)` + `gh.workflow_label(child)`.
  4. If any child is `rejected` → park parent awaiting human (the human decides whether to re-decompose or close).
  5. If any child is closed (`state=="closed"`) but its label is not `done`, `rejected`, or `in_review` → park parent awaiting human. A child closed manually (e.g. via the GitHub UI) before reaching `in_review` is invisible to `list_pollable_issues` (which only sweeps closed-but-`in_review` for the externally-merged path), so its workflow label stays frozen and the parent would otherwise wait forever for it. `in_review` is intentionally excluded — the closed-`in_review` sweep finalizes that transient on the next tick.
  6. If every child is `done` → post a summary comment, flip parent → `ready`. The next tick `_handle_ready` picks it up and the implementer takes over.
  7. Otherwise walk children: any `blocked` child whose recorded dependencies are all `done` gets relabeled `ready`. A child with no recorded deps is also flipped (vacuous all-done over an empty list) — this recovers no-dep children that the decomposer's same-tick activation step left as `blocked`. This walk both unblocks middle-of-the-graph children and rescues stuck activations without waiting on the parent.
- **Output**: parent → `ready` (all done), OR a sibling unblocked, OR a HITL park (rejected child, manually-closed child, or unattributed `blocked`), OR a no-op for a child still waiting on its dependencies.

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

### `_handle_in_review` (label `in_review`)
- **Trigger**: each tick while label is `in_review` (set by `_handle_validating` after `VERDICT: APPROVED`). Also runs on closed-`in_review` issues yielded by the closed-issue sweep, so an external manual merge gets finalized to `done` even when `Resolves #N` already closed the issue.
- **Input**: pinned `pr_number`, `branch`, `dev_agent`/`dev_session_id` (or legacy `codex_session_id`), and two watermarks: `pr_last_comment_id` (issue thread + PR conversation, shared IssueComment id space; falls back to `last_action_comment_id` for back-compat) and `pr_last_review_comment_id` (separate id space for inline review comments).
- **Internal flow**:
  1. If `pr_number` is missing (manual relabel suspected), park awaiting human and return; subsequent ticks no-op until the human relabels.
  2. Read the PR via `gh.get_pr`. Branch on `gh.pr_state(pr)`:
     - `merged` → set label `done`, stamp `merged_at`, write pinned state, then `issue.edit(state="closed")`. (Pinned-state write before close so PyGithub caching cannot serve a stale issue body to the writer.)
     - `closed` (without merge) → set label `rejected`, stamp `closed_without_merge_at`, write state, close.
     - `open` → fall through.
  3. **PR-comment debounce → dev resume → bounce back to validating.** Read three sources independently: `gh.comments_after(issue, pr_last_comment_id)` (issue thread), `gh.pr_conversation_comments_after(pr, pr_last_comment_id)` (PR conversation; shares id space with the issue thread, so one watermark suffices), `gh.pr_inline_comments_after(pr, pr_last_review_comment_id)` (inline review comments, separate id space). If any are newer than their watermark and the most recent one is older than `IN_REVIEW_DEBOUNCE_SECONDS` (default 600s, matches `docs/workflow.md:142`), build a follow-up prompt that quotes them and call `_resume_dev_with_text` on the dev's locked backend. On a successful pushed commit (clean tree + push ok), bump each watermark to the newest seen in its own id space, reset `review_round=0`, and flip the label back to `validating` so the reviewer agent re-runs on the new diff next tick. If still inside the debounce window, return — the human may still be typing.
  4. **Auto-merge gate** (only reached when there are no new comments to act on). Off unless `AUTO_MERGE=on`. Sequence: approval check (either `agent_approved_sha == pr.head.sha`, snapshotted by validating when the reviewer agent emitted `VERDICT: APPROVED`, OR `gh.pr_is_approved(pr, head_sha=pr.head.sha)` — only counts human/bot reviews submitted on the *current* head SHA, so a stale APPROVED from before a later push does not unlock auto-merge); `pr_is_mergeable` (`None` means GitHub still computing — try next tick; `False` parks awaiting human for branch-protection / conflict / out-of-date base); `pr_combined_check_state` (`success` proceeds; `pending` waits; `failure`/`none` parks awaiting human — `none` means no checks at all, ambiguous). Finally `gh.merge_pr(pr, sha=pr.head.sha)` — SHA-pinned so a commit landing between our checks and the merge cannot slip through unreviewed; PyGithub's 405/409/422 are returned as `False` and the next tick retries.
  5. On a successful merge, set label `done`, stamp `merged_at`, write pinned state, close the issue.
  6. Every park inside this handler bumps the in_review watermarks past the orchestrator's own park comment via `_bump_in_review_watermarks`, so the next tick does not see the HITL ping as fresh PR feedback and resume the dev agent against it.
- **Output**: label moved to `done` / `rejected` (terminal) OR a fix push and label bounce to `validating` OR a HITL park OR a no-op tick.

The "back to validating on a new PR comment" arc is intentional: validating is the stage that re-runs the reviewer after a fix is pushed. Staying in `in_review` would skip the automated re-review and rely on humans alone, contradicting the validating loop. `_park_awaiting_human` posts on the issue (not the PR) so the HITL ping appears alongside the rest of orchestrator state. The PR comment that triggers a resume is the human signal; awaiting-human is reserved for *unrecoverable* states (not mergeable / failed checks / push fail / missing pr_number).

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
| decomposer agent (`DECOMPOSE_AGENT`) | subprocess (fresh or resumed, locked backend) | `_handle_decomposing` (retry budget OK) or HITL resume | one shot per tick when needed |
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
   │     in_review ──► _handle_in_review                           │    │
   │                       │                                       │    │
   │                       ├─ pr merged externally ─► label=done,  │    │
   │                       │     stamp merged_at, close issue      │    │
   │                       ├─ pr closed unmerged ─► label=rejected,│    │
   │                       │     stamp closed_without_merge_at,    │    │
   │                       │     close issue                       │    │
   │                       ├─ new PR/issue comment past debounce:  │    │
   │                       │     resume dev (locked backend) ──────┘    │
   │                       │     push, ++pr_last_comment_id,            │
   │                       │     label=validating, review_round=0       │
   │                       └─ AUTO_MERGE on, approved, mergeable,       │
   │                           green checks ─► merge_pr (sha pin),      │
   │                           label=done, close                        │
   │                          unmergeable / failed checks ─► park       │
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

### State transition (label lifecycle)

```
                         single
                       ┌─────────────────────────────┐
   (none) ──► decomposing ──► ready ──► implementing ──► validating ──► in_review ──► done | rejected
                  │                          ▲                  │              ▲ │
                  │ split                    │ all children     │              │ │  PR comment past
                  ▼                          │ done             │              │ │  debounce ─► resume
                blocked ──► (children created) ──┐              │              │ │  dev, push, label
                  ▲                              │              │              │ │  back to validating
                  └─ child rejected ─► park HITL │   CHANGES_   │              │ │
                                                 │   REQUESTED  │              │ │
                                                 │              │              └─┘
                                                 └──────────────┘
                                  (APPROVED or MAX_REVIEW_ROUNDS)

   decomposing flavors:
     decision='single'  ─► label=ready  (parent itself implements)
     decision='split'   ─► create children, parent=blocked,
                           child[i] = ready if no deps else blocked
     manifest invalid / question / timeout ─► park HITL

   blocked transitions (per tick):
     all children = done ─► parent=ready
     any child = rejected ─► park HITL on parent
     dep_graph walk: any blocked child with all deps=done ─► child=ready

   in_review terminals:
     pr merged (externally or by AUTO_MERGE) ─► done   (issue closed)
     pr closed without merge                  ─► rejected (issue closed)

   any stage ──► [park: awaiting_human=true]  (timeout, dirty tree,
                       │                       question, push fail,
                       │                       unknown verdict, max rounds,
                       │                       retry budget exhausted,
                       │                       not mergeable, failed checks,
                       ▼                       invalid manifest)
                 wait for new human comment ──► resume agent (locked backend)
```
