# Agent Orchestrator — Roadmap

## Status as of 2026-05-11

The full label lifecycle (no label → `decomposing` → `ready` / `blocked` /
`umbrella` → `implementing` → `validating` → `in_review` → `resolving_conflict`
optional detour → `done` / `rejected`) is wired end-to-end. The orchestrator
runs as a single long-lived Python process (`python -m orchestrator.main`,
wrapped by `run.sh` for self-restart), polls one or more configured repos,
and delegates the actual coding to `codex` / `claude` CLI subprocesses
running in per-issue git worktrees. State lives in GitHub Issues themselves
(one workflow label plus one pinned JSON comment), so the loop stays
stateless and progress is observable on github.com.

See `docs/workflow.md` for the design and stage semantics and
`docs/architecture.md` for the implementation walk-through. This file
tracks what shipped, what is intentionally deferred, and what is still
open.

## Shipped

**Bootstrap path.** Polling loop with `--once`, SIGINT/SIGTERM-clean
shutdown, and ancestry-aware self-update detection (exit on the wrapper's
behalf when `origin/<ORCHESTRATOR_BASE_BRANCH>` advances past the running
HEAD with changes under `orchestrator/`). `run.sh` self-restart wrapper
fast-forwards the same branch on every restart. `GitHubClient` thin
PyGithub wrapper handles issues, labels, pinned-state JSON comments, PR
open / find / merge, idempotent workflow-label bootstrap (graceful on
under-scoped PATs).

**Agent invocation.** `agents.run_agent(backend, ...)` dispatches to
`_run_codex` or `_run_claude`, both returning a unified `AgentResult`.
Codex via `codex exec [-C cwd | resume <sid>] --dangerously-bypass-
approvals-and-sandbox --json -o <tempfile>`. Claude via `claude -p
--dangerously-skip-permissions --output-format stream-json
--include-partial-messages --verbose` with `--resume <sid>` for resumes.
Session id is harvested by walking JSONL events for any UUID-shaped value
at `session_id` / `conversation_id` / etc. (shared between both backends).
`DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` independently configurable
and validated at import; default split is claude implements + codex
reviews + claude decomposes. The backend for an in-flight issue is locked
in pinned state (`dev_agent` / `dev_session_id`, with legacy
`codex_session_id` falling back to codex), so flipping `DEV_AGENT` does
not migrate in-flight work. `AGENT_TIMEOUT` / `REVIEW_TIMEOUT` hard
wall-clock caps; reaper kills agent grandchildren on timeout.
`MAX_RETRIES_PER_DAY` (default 3, 0 = unbounded) per-issue fresh-spawn
budget over a 24h window shared between implementing and decomposing.

**Security hardening.** PAT never reaches the agent: `agents._agent_env`
strips `GITHUB_TOKEN` / `GH_TOKEN` / `GIT_TOKEN` / `GITHUB_PAT` /
`GH_ENTERPRISE_TOKEN` / `GITHUB_ENTERPRISE_TOKEN` / `GH_HOST` from the
inherited environment. PAT is rejected if found in `REPO_ROOT/.env`
(`config._load_dotenv`); the token must come from the process environment
or a file outside `REPO_ROOT` (default `~/.config/<owner>/<repo>/token`
derived from `REPO`, overridable via `ORCHESTRATOR_TOKEN_FILE`).
Hardened `git push`: token via `GIT_ASKPASS` tempfile (never argv),
`core.hooksPath=/dev/null`, `credential.helper=`, `core.fsmonitor=`,
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, refuses to push when the local config carries
`url.*.insteadOf` / `pushInsteadOf` rewrites, pushes via explicit refspec
`HEAD:refs/heads/<branch>` (no upstream stored). `_authed_fetch` and
`_git_hardened` reuse the same envelope for fetches and merges inside
agent-writable worktrees. Agent commit identity is stamped via
`GIT_AUTHOR_*` / `GIT_COMMITTER_*` env vars (`AGENT_GIT_NAME` /
`AGENT_GIT_EMAIL`, default `agent-orchestrator
<agent-orchestrator@users.noreply.github.com>`), overriding any host
`~/.gitconfig` without touching it.

**Decomposing stage.** `_handle_decomposing` drives a fresh decomposer
session on `DECOMPOSE_AGENT` (default `claude`). The agent emits a fenced
` ```orchestrator-manifest ` JSON block; `_parse_manifest` accepts
`decision=single` (parent flips to `ready` with the rationale surfaced as
a comment) or `decision=split` with up to 10 children, structurally
validated for shape, dependency indexes, self-deps, and DFS-detected
cycles. The optional `umbrella` boolean on a `split` decision routes the
parent to `umbrella` instead of `blocked`; an umbrella parent has no
implementation of its own and `_handle_umbrella` closes it to `done` once
every child resolves. Invalid manifests park awaiting human; absent
manifests park as a question. `_handle_blocked` aggregates child labels
per tick: all-done → parent `ready`, any rejected → park HITL, otherwise
the dep-graph walk unblocks middle children. `gh.create_child_issue`
prepends `Parent: #<n>` and deliberately avoids `Resolves` keywords so a
merged child PR does not auto-close the parent. `DECOMPOSE=off` reverts
to the legacy direct-to-`implementing` pickup and applies mid-flight to
issues already labeled `decomposing`. Pickup is also gated by
`ALLOWED_ISSUE_AUTHORS` (comma-separated logins): when set, unlabeled
issues from outside the list are silently skipped.

**Implementing stage.** `_handle_implementing` ensures a per-issue
worktree at `<WORKTREES_DIR>/<owner>__<name>/issue-<n>` from
`origin/<spec.base_branch>` in `spec.target_root` (slug subdir keeps two
repos with the same issue number isolated on disk; worktrees with
unpushed commits are reused for crash recovery). Branches: timeout →
park; new commits + clean tree → push, open PR (or reuse via
`find_open_pr`), comment `:sparkles: PR opened: #N`, set label
`validating`, reset `review_round=0` and `retry_count=0`; new commits +
dirty tree → park (refuse partial branch); no new commits → park as a
question. Awaiting-human reply branch resumes the dev session on its
locked backend with the new comment text. PR titles and commit messages
follow Conventional Commits: the implementer prompt instructs the agent
to inspect `git log --oneline -20` and emit subject-only commits with no
`Co-Authored-By:` trailer; `_pr_title_from_commit_or_issue` reuses the
agent's first commit subject when conformant, otherwise falls back to
`<type>: <issue title>` (`fix` for bug-labeled issues, `feat` everywhere
else).

**Validating stage.** `_handle_validating` spawns a fresh reviewer
session on `REVIEW_AGENT` against `git diff origin/<base>...HEAD` and
parses the last `VERDICT:` marker (`_parse_review_verdict`). On
`APPROVED`, snapshot `agent_approved_sha`, optionally squash the dev's
commits into one (`_squash_and_force_push`, gated by
`SQUASH_ON_APPROVAL`, default `on`; subject reuses the first commit when
already conventional-commit-shaped, otherwise `feat: <issue title>`; body
lists the original subjects; force-pushed with `--force-with-lease`
against the pre-squash SHA) and flip the label to `in_review`. On
`CHANGES_REQUESTED`, post the feedback to the PR, resume the dev's
locked-backend session with the fix prompt, push, and increment
`review_round` for the next tick. `MAX_REVIEW_ROUNDS` (default 3) caps
review/fix iterations before parking. Reviewer-side human nudges route
to the reviewer; silent reviewer crashes are tagged transient so the
next tick retries instead of parking on stale state.

**In-review terminals and auto-merge.** `_handle_in_review` covers:
PR merged externally → `done` (issue closed, `merged_at` stamped,
`_cleanup_merged_branch` removes worktree + local + remote branch);
PR closed without merge → `rejected` (issue closed,
`closed_without_merge_at` stamped); PR open with new comments past the
`IN_REVIEW_DEBOUNCE_SECONDS` (default 600s) quiet window → resume the
dev's locked-backend session on the quoted comments, push, bounce back
to `validating` with `review_round=0`; PR open with no comments +
`AUTO_MERGE=on` + (agent-approved-on-current-head OR
`pr_is_approved(head_sha=)`) + no standing human `CHANGES_REQUESTED` veto
+ `pr_is_mergeable=True` + green CI → SHA-pinned `gh.merge_pr(pr,
sha=head_sha)` → `done`, close, cleanup branch. PR-feedback is read from
four sources tracked under three independent watermarks
(`pr_last_comment_id`, `pr_last_review_comment_id`,
`pr_last_review_summary_id`) so the IssueComment / PullRequestComment /
PullRequestReview id namespaces never bleed into each other. Park
branches bump the watermarks past the orchestrator's own park comment via
`_bump_in_review_watermarks` so an HITL ping does not replay as fresh
feedback.

**Conflict resolution stage.** Under `AUTO_MERGE=on` an approved-but-
unmergeable PR routes to `resolving_conflict` instead of parking.
`_handle_resolving_conflict` refreshes `origin/<branch>` and
`origin/<base>` via `_authed_fetch`, runs `git merge --no-edit
origin/<base>` under `_git_hardened`, and flips back to `validating` on
either an already-up-to-date no-op or a clean merge (push first if HEAD
moved). Real conflicts resume the dev session on the locked backend with
a conflict-resolution prompt that names up to 20 conflicted paths;
on a clean resolved commit, push and flip. `MAX_CONFLICT_ROUNDS`
(default 3) caps auto-resolution attempts; the counter increments on
every clean push and every no-op already-up-to-date merge (so a PR that
is unmergeable purely due to branch protection cannot ping-pong forever).
Merge over rebase by design so the stored `agent_approved_sha` snapshot
stays valid. Awaiting-human resume mirrors the implementing pattern; a
diverged worktree (`behind > 0`) parks rather than risk clobbering the
PR head. Closed-`resolving_conflict` issues are swept the same way
closed-`in_review` ones are.

**Multi-repo support.** `RepoSpec(slug, target_root, base_branch)` is
threaded through every workflow handler. `REPOS` env var
(`owner/name|target_root|base_branch`, `;`- or newline-separated) drives
the multi-repo loop; legacy single-repo mode collapses to a
one-element list when `REPOS` is unset. Validation runs at import:
malformed entries, slugs that are not exactly `owner/name`, empty
`target_root` / `base_branch`, duplicate slugs, and a `REPOS` value that
yields zero entries all abort startup with `SystemExit`. A `target_root`
that does not exist is warned to stderr but does not abort. Per-issue
worktrees are namespaced by repo slug
(`WORKTREES_DIR/<owner>__<name>/issue-N`) so two repos with the same
issue number cannot collide. Each tick `main._run_tick` iterates every
`(spec, GitHubClient)` pair; a per-repo exception is logged and skipped
so one wedged repo cannot stop the others from advancing. Per-slug token
resolution: `GitHubClient` reads `GITHUB_TOKEN` from env first, then
falls back to `~/.config/<owner>/<repo>/token` derived from the spec's
slug. `ORCHESTRATOR_BASE_BRANCH` is decoupled from `BASE_BRANCH` so the
target repo can have a different default branch (e.g. `master`) without
breaking self-update detection on `orchestrator/`. `TARGET_REPO_ROOT`
decouples the orchestrator's own checkout from the target repo's clone.

**Tests.** `tests/test_workflow.py` is large (≈400k) and covers every
stage handler, the manifest parser, the watermark / debounce logic, the
auto-merge gate sequence, the squash-on-approval path, the
resolving-conflict suite (merge / push / cap / resume / recovery /
dirty-tree / fetch-fail / diverged-worktree / closed-issue), the
umbrella handler, the multi-repo dispatcher fan-out, and the
park-comment-replay-prevention path. `tests/fakes.py` exposes an
in-memory `FakeGitHubClient` plus `FakePR` / `FakePRRef` / `FakeIssue`.
`tests/test_config.py` covers env parsing for every knob.
`tests/test_agents.py` covers per-backend dispatch and the unified
`AgentResult`. `tests/test_main.py` covers per-repo fan-out, exception
isolation, and the legacy single-repo fallback.

## Known gaps

Behaviors `docs/workflow.md` prescribes that the code does not yet do:

1. **Project tests/linters during `validating`.** `_handle_validating`
   only spawns the reviewer agent; there is no `pytest` / `ruff` /
   `mypy` / project-script invocation before the `validating → in_review`
   flip. Project-level checks happen externally via PR CI and are only
   consulted at the AUTO_MERGE gate (`pr_combined_check_state`), not as
   a precondition for the transition.

## Future work

- **Dockerfile / systemd / GitHub App migration.** The current deployment
  is a `run.sh` wrapper around `python -m orchestrator.main` on a single
  host. The design doc flags container / VM isolation as an open
  question. Moving to a long-running VPS deployment also lets `systemd
  Restart=always` replace the `run.sh` self-restart wrapper, and the
  GitHub App migration lets the orchestrator drop the per-repo PAT in
  favor of an installation token.
- **Parallel implementers and pick-best / merge.** `docs/workflow.md`
  flags this as Week-2 / future: spawn several agents on the same issue,
  pick the best of N solutions, or merge them together. Out of scope
  for the first version (one solution per issue).
- **Architectural review at `validating`.** `docs/workflow.md` flags
  this as optional: a reviewer pass that flags structural issues (e.g.
  oversized files that should be split). Not yet implemented.
- **Documentation stage.** `docs/workflow.md` lists this under "Next
  steps": an extra stage that keeps `docs/` in sync as code changes
  land.
- **Dynamic workflow.** `docs/workflow.md` lists this under
  "Alternatives": a planner agent ahead of execution that picks the
  stages a given issue needs (extra architectural exploration, skip
  acceptance for trivial fixes, etc.). Judged excessive for the original
  2-week budget; revisit once the static flow is fully dogfooded.

## Risks

- **R1 — Codex/Claude CLI output format drift.** Isolated in
  `agents.parse_session_id()` and the per-backend last-message capture;
  failure modes surface as `session_id=None` (logged, agent still runs)
  or empty `last_message` (the orchestrator parks with the agent's
  stderr quoted via `_format_stderr_diagnostics`).
- **R2 — Self-mutation while running.** Mitigated by per-issue worktrees
  + ancestry-aware self-update detection in
  `main._self_modifying_merge_happened` + the `run.sh` self-restart
  wrapper.
- **R3 — Runaway agent loops / token cost.** Wall-clock timeouts
  (`AGENT_TIMEOUT`, `REVIEW_TIMEOUT`), per-issue retry budget
  (`MAX_RETRIES_PER_DAY`), review/fix cap (`MAX_REVIEW_ROUNDS`), and
  conflict-resolution cap (`MAX_CONFLICT_ROUNDS`).
- **R4 — GitHub rate limits.** PyGithub handles backoff; 60s ticks are
  well under the 5000 req/hr limit.
- **R5 — Race between human comments and orchestrator action.**
  Re-fetch issue + pinned-state immediately before each transition; any
  comment newer than the recorded watermark is treated as a pause signal
  that drives the awaiting-human resume branch.
