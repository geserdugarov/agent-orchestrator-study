# Features Proposal — Cross-Orchestrator Survey

## Purpose

Closes issue #50. Surveys 13 sibling agent-orchestrator
implementations from `podlodka-ai-club` and proposes the features most worth
adopting into `agent-orchestrator`, ranked by added usability / pipeline
autonomy.

The intent is to seed roadmap discussion, not to commit to a build order.
Each proposal lists the source repos that demonstrate it, an implementation
sketch keyed to our handlers in `orchestrator/workflow.py`, and the
expected impact relative to today's behaviour.

## Method

Surveyed READMEs, architecture docs, and source layout of:

| Repo | Language | State | Defining trait |
| --- | --- | --- | --- |
| `the-foundry` | Python | SQLite + worktrees | Two-tier validation (deterministic commands then LLM reviewer), `repo_memory`, per-stage model overrides, `SAFE_AGENT_MODE` default-on |
| `X15` (`packages/backlog-orchestrator`, branch `dev`) | TypeScript / Bun | Postgres `remote_agent_backlog_orchestrator_runs` | Library-style `reconcileOnce()` driven by a host scheduler; **routing-label workflow dispatch** (`archon-workflow:<name>` picks the agent prompt); **area-lock concurrency policy** (`area:*` labels block parallel runs); per-issue `archon:auto-merge` opt-in label; parallel-specialist reviewer with synthesizer; explicit allowlist for workflows that legitimately produce no PR |
| `iron-press` | Node + Claude Agent SDK | `.runs/<id>/` per-run dir | Workflow as DAG, per-node permission profiles, structured `outputSchema`, label-selected workflow |
| `heavy-lifting` (branch `master` — `main` holds only a placeholder `readme`) | Python + Flask + Postgres | Postgres | Triage stage with Story-Point routing matrix, content-hash re-triage, composite-cursor event ingestion, economics dashboard |
| `the-furnace` | TypeScript + Temporal | Temporal | Spec-first split (tests-as-contract), persona reviewers, devcontainer-per-attempt, activity-level rate limiting |
| `gear-grinders` | Python CLI | `.gg/runs/<id>/` | Parallel candidates with verifier-only scoring, agent-pattern static linter, `ResultEvaluation` stage |
| `boiler-room` | Python CLI | `.agent-runs/<id>/` | GitHub Copilot CLI as a third backend, draft-project-item support, end-to-end test harness |
| `steam-hammer` | Python core + Go wrapper | Comment + `.orchestrator/workers/` | `scope_check` pre-stage, image/attachment ingestion, preset-based escalation, detached daemon mode with fresh per-worker clones |
| `night-shift` | TypeScript + Temporal | Temporal | Specify-phase with operator approval gate, inline Escalation Manager, board-status HITL signals, replay/eval harness |
| `the-smelters` | Python + Agno | SQLite | `PREVIOUS_ITERATION_DIFF` feedback, intentional-bug fixture as orchestrator regression test, structured `_checker_passed` signal |
| `blast-furnace` | TypeScript + Fastify + BullMQ | Redis + JSONL ledger | Stop-hook in-session quality gate, append-only handoff JSONL, label-only rework trigger |
| `the-anvils` | Python + FastAPI + Postgres | Postgres | Language-agnostic Quality Gate protocol, decision-gate pre-filter, per-plan USD budgets, `.importlinter` arch enforcement, per-PR resilient polling |
| `drop-forge` | Go + Linear | Filesystem per run | Cross-agent review (opposite-model reviewer), closed-enum review schema, idempotency markers on PR reviews, ELK shipping |

Conclusions are drawn from the union of three independent sub-agent reads.
A few file references are reproduced inline so a reader can verify the
provenance of each proposal.

## How the proposals are ranked

Priority is "expected delta to autonomy / usability per unit of work."
Anything that closes an explicit roadmap gap in `plans/roadmap.md` or that
shows up in more than half of the surveyed orchestrators is treated as
Tier 1. Tier 2 is high-impact but design-heavy. Tier 3 is opportunistic
polish.

For each proposal: the problem in our current code, what other
orchestrators do, a concrete sketch of where the change lands, and the
trade-off.

---

## Tier 1 — high impact, closes known gaps

### 1. Local quality gate before opening the PR

**Problem.** `_handle_implementing` pushes a branch the moment the agent
produces a clean commit. `_handle_validating` then spawns a reviewer
agent against `git diff origin/<base>...HEAD`. Project tests, linters,
and type checkers never run inside the orchestrator's loop — we wait for
PR CI and only consult it inside the auto-merge gate
(`pr_combined_check_state`). `plans/roadmap.md` already lists this as
the single concrete known gap.

**What others do.** Four of the five most mature orchestrators wire
deterministic checks into the local loop before opening (or before
declaring "done") the PR:

- `the-foundry/src/foundry/stages/verify.py` auto-detects
  `verify_commands` from `pyproject.toml` (ruff + pytest),
  `package.json` (`npm test`), `Cargo.toml` (cargo test / clippy /
  fmt), `go.mod` (go test / vet / fmt). VERIFY short-circuits on
  nonzero return code; the LLM reviewer only runs when commands pass.
- `the-anvils/whilly/quality/` defines a `QualityGate` Protocol with
  concrete implementations per language, selected by marker files. ADR-016
  states the promise: "whilly never opens a PR it wouldn't accept from a
  human contributor."
- `gear-grinders/src/gg/orchestrator/verification.py` treats verification
  as part of candidate validity, not a postscript. A candidate that
  mutates the worktree during verification, breaks a mandatory command,
  or violates policy is *invalidated* before scoring.
- `steam-hammer` has `workflow_checks` as a first-class state that blocks
  the `ready-for-review` transition on failure.
- `X15`'s `.archon/commands/defaults/archon-validate.md` is the runbook
  form of the same idea: a sequential pipeline of typecheck → lint
  (auto-fix) → format (auto-fix) → tests → build, **fix-in-place rather
  than delegate**, fail-fast at first unfixable step. The auto-fix on
  lint/format passes is the variant our proposal should adopt: if
  `ruff check --fix` or `prettier --write` resolves the failure, commit
  the auto-fix and keep going instead of bouncing back to the dev agent.

**Proposed change.** Insert a new stage `verifying` between `implementing`
and `validating`:

```
implementing → verifying → validating → in_review → …
```

`_handle_verifying`:
1. Auto-detect commands from marker files in the worktree
   (`pyproject.toml` → `ruff check . && pytest`; `package.json` →
   read `scripts.test` / `scripts.lint`; `Cargo.toml` → `cargo test`
   + `cargo clippy -- -D warnings`; `go.mod` → `go test ./...` +
   `go vet ./...`). Auto-detection is overridable per repo by
   `VERIFY_COMMANDS` env in `RepoSpec` style.
2. Run each command in the worktree under a wall-clock cap
   (`VERIFY_TIMEOUT`, default 600s). Run order: lint → typecheck →
   tests, fastest first so a quick lint catches the easy fixes.
3. On a fixable failure (lint / format passes that accept `--fix` /
   `--write` flags), run the auto-fix variant first, commit the result
   under a `chore: auto-fix lint` message, and re-run the gate. Borrowed
   from `X15`'s `archon-validate.md` — auto-fix cheap classes of failure
   before paying for a dev-agent spawn.
4. On all-pass: push branch, open PR, label `validating`. The reviewer
   then sees a diff that we already know lints and passes tests.
5. On unfixable failure: rebuild a "fix-the-failing-checks" prompt that
   quotes the failed command, the stderr tail
   (`_format_stderr_diagnostics`), and the changed files. Resume the dev
   session on its locked backend, bump a new `verify_round` counter
   capped by `MAX_VERIFY_ROUNDS` (default 3). On exhaustion: park
   awaiting human, mirroring the `MAX_REVIEW_ROUNDS` pattern in
   `_handle_validating`.

**Impact.** Removes the "agent declares victory, CI says no" round-trip
that today costs at least one push + GitHub Actions roundtrip + reviewer
spawn. On a stubborn issue with three failing tests this is the
difference between one tick and four. Closes the only acknowledged gap
in `plans/roadmap.md`.

**Cost.** ~300 lines in `workflow.py` + a `verify_commands.py` detector
module, plus tests against the fakes. No new dependencies — `subprocess`
is enough. Label addition is a one-line migration via the existing
`ensure_workflow_labels`.

**Trade-off.** Adds wall-clock time per implementation cycle. Mitigated
by tight `VERIFY_TIMEOUT` and by skipping verify entirely when no marker
file is detected (issue still routes through `validating` as today).

---

### 2. In-session stop-hook quality gate

**Problem.** Even with proposal #1, the dev agent decides on its own when
to stop. If the agent commits work that lints fine but breaks tests, we
pay for a whole new agent spawn (or at best a resume) to fix it.

**What others do.** `blast-furnace`'s
[`2026-04-27-stop-hook-quality-gate-for-develop`](https://github.com/podlodka-ai-club/blast-furnace)
openspec change wires `pytest` to a Codex *stop hook*: when the agent
declares it's done, the hook runs the test command, and on failure the
agent's stop is **blocked** and failures are fed back into the **same
Codex session**. Capped at three cycles (block, block, allow-stop-with-
terminal-failure). The agent never leaves the warm context in which it
just wrote the change.

**Proposed change.** Both supported CLI agents expose this primitive:

- Claude Code supports `Stop` hooks via `~/.claude/settings.json`
  hooks config; the hook is a script that reads the transcript path
  from stdin, runs the checks, and returns a non-zero JSON
  `{"decision":"block","reason":"..."}` to refuse the stop.
- Codex CLI supports `--hooks` with the same shape.

`_handle_implementing` writes a temporary `settings.json` into the
worktree pointing at a generated hook script that runs the same commands
proposal #1 auto-detects. Cap by `STOP_HOOK_MAX_CYCLES` (default 2).
On exhaustion the hook lets the stop go through and the orchestrator
falls through to `verifying`, which catches the terminal failure as
usual.

**Impact.** Closes the loop *within one warm session*. Empirically the
single biggest delta in `blast-furnace`'s own dogfooding: failure
context never leaves the model's working memory. Combined with #1 this
makes our `validating` reviewer agent see a diff that already passes
local checks, so the reviewer's job collapses to "is the design sound"
rather than "did the obvious things work."

**Cost.** ~150 lines: one hook-script generator, two backend-specific
wirings in `agents.py`, an integration test that runs against a fake
Bash hook. No new dependencies.

**Trade-off.** Hooks are a relatively new surface in both CLI agents;
schema drift risk applies. Mitigated by treating hook absence /
schema-drift errors as soft failures — fall through to proposal #1's
verifying stage, which catches the same failure with an extra
agent-spawn cost.

---

### 3. Structured reviewer schema with closed-enum severities and `fix_prompt`

**Problem.** `_parse_review_verdict` keys on a single `VERDICT: APPROVED |
CHANGES_REQUESTED` marker in the reviewer agent's last message. The
remaining text is freeform. When the reviewer says
"CHANGES_REQUESTED — change `_handle_pickup` to take a `RepoSpec`," we
forward that prose to the dev as the fix prompt. Quality of the next
round is bottlenecked on how well the reviewer happened to phrase its
ask, and on the dev agent's ability to extract actionable items from
prose.

**What others do.** `drop-forge`'s `internal/reviewrunner/` requires the
reviewer to emit a strict JSON envelope:

```json
{
  "summary": { "verdict": "ship-ready | needs-work | blocked", "...": "..." },
  "findings": [
    {
      "id": "F1",
      "category": "<closed enum per stage>",
      "severity": "blocker | major | minor | nit",
      "fix_prompt": "<self-contained, copy-paste-ready instruction>"
    }
  ]
}
```

Each finding's `fix_prompt` is mandated to be self-contained: a future
agent (the same model or a different one) can act on it without seeing
the rest of the review. Categories are closed enums and differ per stage
(proposal categories ≠ apply categories ≠ archive categories) — that
keeps the reviewer from inventing taxonomy on the fly.

Two more useful nuances from `drop-forge` and `night-shift`:

- **Idempotency marker.** Inline PR review comments carry an HTML marker
  `<!-- reviewer=codex stage=apply sha=abc123 -->`. Re-running the
  reviewer against the same HEAD is a no-op, so the orchestrator can
  retry safely without spamming the PR. `X15` corroborates with a
  different mechanism: the orchestrator persists a `comment_keys_json`
  set on the run row (`StoredOrchestratorRun.comment_keys_json` in
  `packages/backlog-orchestrator/src/db/store.ts`), so duplicates are
  caught client-side without needing a marker on every comment.
- **Producer trailer + opposite-model reviewer.** Each commit carries a
  trailer `Produced-By: claude / Produced-Model: <model>`. The reviewer
  router reads it and picks the opposite backend. Today we have
  `DEV_AGENT=claude / REVIEW_AGENT=codex` as a static pair; the trailer
  makes the routing rule legible to humans reading the git log and
  resilient to operator-driven backend swaps.

**Proposed change.** Replace the freeform reviewer prompt with a fenced
JSON envelope analogous to the existing `orchestrator-manifest`:

````
```orchestrator-review
{ "verdict": "...", "findings": [ ... ] }
```
````

`_parse_review_envelope` validates structure (similar to
`_parse_manifest`). Each finding's `fix_prompt` becomes one line in the
fix prompt fed to the dev resume; severities `blocker | major` block the
flip to `in_review`, `minor | nit` are forwarded as advisory and do not
block. Findings are posted as a single GitHub Reviews API call (atomic,
not one comment per finding) with an HTML idempotency marker keyed on
`(reviewer_agent, head_sha)`.

Add a `Produced-By:` trailer to dev-agent commits via the existing
`GIT_AUTHOR_*` env wiring (extend `agents.py` to write the trailer
through `git commit -m "..." -m "Produced-By: <backend>\nProduced-Model:
<model>"`). The reviewer can then read the trailer with
`git log -1 --format=%B` and confirm the backend matches.

**Impact.** Sharper fix loop: the dev sees a structured list of
actionable items rather than freeform prose. Idempotency markers make
retries safe; today a flaky reviewer that gets retried can re-spam the
PR with the same feedback.

**Cost.** ~200 lines: schema, parser, posting helper, prompt rewrite.
The existing CHANGES_REQUESTED prose path stays as a fallback when the
envelope is missing (mirrors how `_parse_manifest` handles absent
manifests as a question). Trailers are one extra `-m` per commit.

**Trade-off.** Schema-drift risk on the reviewer prompt. Mitigated by
the same fenced-block + soft-fallback pattern that `_parse_manifest`
already uses.

---

### 4. Per-role permission profiles and per-stage tool allow/deny lists

**Problem.** Today every agent invocation passes
`--dangerously-skip-permissions` (Claude) or
`--dangerously-bypass-approvals-and-sandbox` (Codex). The decomposer can
theoretically `rm -rf /` even though its job is read-only. The reviewer
can theoretically rewrite the diff it is reviewing. We rely on the host
being the sandbox boundary and on agent good-citizenship to not abuse
those flags. This is fine for an MVP and explicitly chosen in our
`README.md`, but the *blast radius* of a prompt-injected agent is wider
than it has to be.

**What others do.**

- `iron-press/src/sdk/workflow/permission-profiles.ts` defines four
  profiles — `view-only / read-only / engineer / safe-write` — and each
  workflow node pins one. The BA persona is `view-only` (Read/Grep/Glob/
  WebFetch only; physically cannot Edit/Write/Bash). The engineer is
  `safe-write`. Each node also has a per-node `allowedTools` /
  `disallowedTools` list, a `budgetUsd`, and a `maxTurns`.
- `the-foundry/src/foundry/security.py` defaults `SAFE_AGENT_MODE=true`,
  which strips the dangerous flags entirely. The README explicitly
  contrasts this with the more permissive default we use today.
  `BASE_ENV_ALLOWLIST` + `BACKEND_SECRET_ALLOWLIST` scrub the agent's
  env down to `PATH`, `HOME`, locale, and the one backend secret it
  needs.
- `steam-hammer` enforces destructive-action policy: merge, force-push,
  and branch-delete each need an explicit policy entry to be invoked
  by an agent.

**Proposed change.** Add per-stage permission profiles to
`config.py`. Each backend invocation in `agents.py` consults the
profile for the calling stage and translates it into the CLI's
allow/deny flags:

| Stage | Profile | Rationale |
| --- | --- | --- |
| `decomposing` | `view-only` | The decomposer is supposed to be read-only. We already detect dirty worktrees post-hoc; the profile blocks the failure ex ante. |
| `validating` (reviewer) | `read-only` | The reviewer should only `git diff` / `git log` / `Read`. Edits or Bash writes are off-policy. |
| `implementing` | `engineer` | Full local write access, no destructive git ops (`force-push`, `reset --hard` outside the worktree). |
| `resolving_conflict` | `engineer` | Same as implementing. |

Translate the profiles to:
- Claude: `--allowed-tools "Read,Grep,Glob,Edit,Write,Bash"` etc.
- Codex: `--config-toml` keys (see Codex docs for the exact knobs).

Layer a cumulative spend cap on top (`ORCH_OVER_BUDGET_USD`): if the
running session's reported cost from the JSONL stream crosses the cap,
kill the subprocess and park awaiting human. `iron-press`'s
`ORCH_OVER_BUDGET` shows this is one flag away once you read the cost
events.

**Impact.** Reduces blast radius without rewriting the sandbox model.
Catches "decomposer accidentally edits files" as a profile-violation
error rather than a downstream worktree-dirty park. Operator confidence
in raising `MAX_RETRIES_PER_DAY` goes up because each individual run is
provably contained.

**Cost.** ~250 lines: profile definitions, per-backend flag translation
in `agents.py`, cumulative-cost tracker, config wiring. Tests can run
against the fakes.

**Trade-off.** Per-backend flag drift is a continuing maintenance tax —
the Claude / Codex CLIs both evolve their allow/deny surfaces. Mitigated
by isolating the translation in `agents.py` and feature-flagging the
whole proposal behind `PERMISSION_PROFILES=on` so we can disable it on
a CLI upgrade.

---

### 5. Per-stage model overrides

**Problem.** Today `DEV_AGENT` / `REVIEW_AGENT` / `DECOMPOSE_AGENT` pick
*backends*; they don't pick *models within a backend*. A claude-decomposer
gets the same model tier (Opus / Sonnet / Haiku) as a claude-implementer.
In practice the decomposer and reviewer are much smaller tasks; running
them on the same tier as the implementer is wasted spend.

**What others do.** `the-foundry`'s `.env.example`:

```
AGENT_PLAN_MODEL=haiku
AGENT_IMPLEMENT_MODEL=sonnet
AGENT_VERIFY_MODEL=haiku
AGENT_<STAGE>_MAX_TURNS=...
```

`steam-hammer`'s preset-based escalation: `cheap → default → hard`
presets with increasing model tier, and failed runs can
`escalate_to_preset` for the next attempt.

**Proposed change.** Per-stage model + max-turns env vars. Defaults
preserve current behaviour:

```
DECOMPOSE_MODEL=haiku
REVIEW_MODEL=haiku
DEV_MODEL=sonnet
RESOLVE_CONFLICT_MODEL=sonnet
```

`agents.run_agent(backend, *, model=None, ...)` plumbs the model
through the CLI flag (`claude --model`, `codex --model`). Add a
preset-escalation hook: when `MAX_REVIEW_ROUNDS` is exhausted, instead
of parking, the next run can escalate the dev model from `sonnet` to
`opus` (gated by `ESCALATE_ON_EXHAUSTION=on`). The retry budget
(`MAX_RETRIES_PER_DAY`) still applies — escalation is one of the few
spawns that count.

**Impact.** Conservative estimate: 30–50% reduction in cost per
successfully shipped issue, with no functionality loss. Frees budget for
parallel candidates (proposal #6) without inflating spend.

**Cost.** ~80 lines and an env-doc update. Tests against the fakes
already verify backend dispatch; this just adds a model field.

**Trade-off.** Decomposer / reviewer quality drops if the cheaper tier
fails to follow the structured-output contract. Mitigated by the
existing "park on invalid manifest / unknown verdict" paths — a
chronically wrong haiku decomposer will surface itself within a day.

---

### 6. Pre-decomposer Decision Gate

**Problem.** `_handle_pickup` routes every unlabeled issue straight into
`_handle_decomposing`, which spawns a full decomposer agent on the
locked backend. Garbage issues ("what does this code do?", "I have an
error") get the same agent-spawn cost as well-formed feature requests.
The `ALLOWED_ISSUE_AUTHORS` allowlist filters by author but not by
content.

**What others do.** `the-anvils`'s `whilly/decision_gate.py` (ADR-008)
runs a Haiku-class LLM with a single binary question — "does this task
have enough information to proceed, or does it need clarification?" —
before the decomposer. On refuse it flips the label
`whilly:ready → needs-clarification` and posts a one-line explanatory
comment. The full decomposer never runs.

`heavy-lifting/docs/contracts/triage-routing.md` is the same idea taken
further: a triage stage emits a Story-Point estimate and a routing
classification (`research / implementation / clarification /
review_response / rejected`), with explicit SP routing rules:

| SP | Action |
| --- | --- |
| 1–3 | Route to implementation, attach Handover Brief |
| 5 | Reply with RFI questions |
| 8 | Reply with decomposition plan |
| 13 | Hard block, escalate to system design |

"Cannot decide between two SPs → pick the higher one." Overescalation
is an explicit preference.

**Proposed change.** Insert a stage `triaging` between pickup and
decomposing:

```
(no label) → triaging → decomposing | needs_clarification | rejected
```

`_handle_triaging` runs a fresh single-turn agent on a cheap model
(`TRIAGE_MODEL=haiku`) with a strict JSON output:

```json
{
  "decision": "proceed | clarify | reject",
  "size": "xs | s | m | l | xl",
  "reason": "<one short sentence>",
  "questions": ["<for clarify only>"]
}
```

- `proceed` → flip to `decomposing` as today; pass the size hint to
  the decomposer prompt so it can adjust the
  "split-if-more-than-5-files" heuristic.
- `clarify` → post the questions to the issue, add the
  `needs_clarification` label (new workflow label), park awaiting
  human. New comments resume into `triaging`.
- `reject` → comment with the reason, flip to `rejected`. Terminal.

**Impact.** Saves a full decomposer spawn (currently the most expensive
spawn after the implementer) on every garbage issue. With public-repo
issue flow this is a meaningful fraction of pickups. The size hint also
gives the decomposer a calibration anchor it doesn't have today.

**Cost.** ~250 lines: new handler, new label, parser, prompt. New label
is bootstrapped via `ensure_workflow_labels`.

**Trade-off.** Adds one stage. Mitigated by `TRIAGE=off` kill switch
mirroring the existing `DECOMPOSE=off` knob, with the same in-flight
migration path.

---

## Tier 2 — material improvements, more design work

### 7. Repo memory carried across issues

**Problem.** Every issue starts cold. The dev agent doesn't know that
yesterday's issue established that `pytest` is the test command, that
`tests/fakes.py` is where the in-memory fakes live, or that the last
three regressions came from a specific subsystem. We re-establish that
context from scratch every time.

**What others do.** `the-foundry/src/foundry/state.py` defines a
`repo_memory` table keyed `(repo, key, value)`. After every successful
PR they record `touched_files`, `verify_commands`, last 5
`common_failures`. The next task's CONTEXT stage reads it back and
inlines it in the planner prompt.

`gear-grinders` goes further with a full `KnowledgeEngine` (`src/gg/
knowledge/{engine,collectors,compiler,events,search}.py`) plus
`repair-lessons.md`, `exemplars.*` (good contributors mined from git
history), `decisions.md`, `patterns.md`.

**Proposed change.** Start small. Add a file at
`<target_root>/.agent-orchestrator/repo-memory.json` (parallel to the
per-slug PAT file location) with a fixed schema:

```json
{
  "schema_version": 1,
  "verify_commands": ["ruff check .", "pytest"],
  "touched_files_top": ["orchestrator/workflow.py", "..."],
  "common_failures": [
    {"summary": "...", "at": "2026-05-09T..."},
    ...
  ]
}
```

`_handle_in_review`'s merge terminal updates it (best-effort, swallowed
on error so a write fail doesn't block the merge). The decomposer and
implementer prompts read it and quote relevant sections. Capped sizes
(top-10 touched files, top-5 failures) so the file stays small.

**Impact.** Calibrates the agents to the codebase over time. The cost
of being wrong is small (a stale entry is just an unused prompt line).
Sets up later proposals (#10 issue-edit re-triage, #11 PR feedback
hashing) by giving them a place to write to.

**Cost.** ~150 lines + tests.

**Trade-off.** Now there's mutable state outside GitHub. Mitigated by
storing it under `target_root` (not the orchestrator's own root) so it
travels with the target repo's clone and is rebuildable.

---

### 8. Parallel implementer candidates with verifier-only scoring

**Problem.** One implementer per issue. If the agent takes a wrong turn,
we burn a whole retry round before recovering. `plans/roadmap.md` flags
this explicitly as future work.

**What others do.**
`gear-grinders/src/gg/orchestrator/{executor,evaluation}.py` spawns N
parallel candidates in isolated worktrees
(`.gg-worktrees/<run>/<candidate>/`) with `--candidates N
--max-parallel-candidates K`. Winner selection is deterministic and
verifier-only — no LLM judge:

```
status=success     +100
verification pass  +50
worktree mutation  -50
each policy viol.  -100
each failed cmd    -10
files-changed penalty
```

Eligibility gate first (success + verification + no mutation + no
violations), then max score among survivors, tiebreak by earliest index.
There's also a `--repair-fanout 2` for repair candidates seeded with
parent failure context.

`the-furnace`'s persona reviewers (security / perf / architect /
naming) demonstrate the parallel pattern on the *review* side and use
disagreement as the escalation signal: unanimous pass → auto-merge with
veto window; split vote → human tiebreaker.

`X15` takes a third route that's a strict superset of the persona-
reviewer pattern: five specialist reviewer sub-agents run in parallel —
`archon-code-review-agent`, `archon-error-handling-agent`,
`archon-test-coverage-agent`, `archon-comment-quality-agent`,
`archon-docs-impact-agent` — each writes an artifact. A separate
`archon-synthesize-review.md` agent loads all five artifacts, dedupes
findings, ranks `CRITICAL → HIGH → MEDIUM → LOW`, and emits **one
consolidated PR comment with collapsible severity sections**. Then
auto-triggers an `archon-auto-fix-review` for the easily-fixable
findings. Two roadmap gaps fall out of this one design: the
`archon-comment-quality-agent` / `archon-docs-impact-agent` cover the
"no architectural review stage" + "no docs-sync stage" entries on our
roadmap without a separate stage.

**Proposed change.** Two-step rollout.

Step A — parallel implementers only:
- `_handle_implementing` spawns up to `IMPLEMENTER_CANDIDATES` (default
  1, opt-in 2 or 3) in sibling worktrees
  `<WORKTREES_DIR>/<slug>/issue-N/cand-<i>`.
- Each candidate runs the full implement→verify loop from proposal #1.
- A new `_score_candidate` computes the rubric. Winner branch is the one
  we push and PR; loser worktrees are tarred to
  `<target_root>/.agent-orchestrator/losers/issue-N-<sha>.tgz` for
  post-mortem (`the-furnace`-style "preserve on failure" rule), then
  removed.

Step B — specialist reviewers + synthesizer on the validate side:
- `_handle_validating` fans out to N reviewer agents with different
  system prompts and tight scopes (start with the five X15 names —
  `code-review`, `error-handling`, `test-coverage`, `comment-quality`,
  `docs-impact`).
- Each writes a structured artifact (the envelope from proposal #3).
- A synthesizer agent dedupes findings across artifacts, ranks them by
  severity, and emits **one** PR review with the consolidated finding
  set. The dev fix-prompt is keyed off the synthesized set, so the dev
  sees one prioritized list rather than five.
- Aggregation rule: any `blocker` survives synthesis → bounce back to
  implementing; only `nit`/`minor` survive → flip to `in_review` and
  forward as advisory. We do *not* adopt the-furnace's
  "split-vote → human" route because the synthesizer collapses
  disagreement before the human sees it.

**Impact.** Higher first-pass success rate on hard issues; fewer
review/fix rounds. Pairs with #5 — running 3 parallel implementers on
sonnet costs the same as one on opus and explores three solution paths.

**Cost.** Step A: ~500 lines, mostly in the executor and scoring.
Step B: ~300 lines, mostly in the reviewer fan-out. Tests need a new
fake that produces N candidates.

**Trade-off.** Higher disk / network footprint. Capped by
`MAX_PARALLEL_CANDIDATES`. Persona reviewers multiply review tokens by
the persona count — mitigated by running personas on haiku (proposal #5).

---

### 9. Spec-first split — separate test-writer from implementer

**Problem.** Today's dev agent writes both the production code and the
tests. As `the-furnace/openspec/concept.md` §3.4 puts it: "When the
coder writes its own tests, it can tune them — consciously or not — to
pass its own implementation." We have no structural defence against
that failure mode.

**What others do.**

- `the-furnace` runs a `specAgent` that writes failing tests against
  `main`. The contract is "tests must fail on default branch." The
  `coderAgent` that runs next is **forbidden from touching test files**
  (enforced by post-session `git diff --name-only HEAD` check; off-
  policy modifications are reverted before the next role runs).
- `night-shift`'s Specify phase materializes an OpenSpec change file
  before any implementation, with a board-status approval gate
  (`Refined → Ready`) that the operator must move manually.
- `drop-forge`'s OpenSpec methodology (`proposal.md → design.md →
  tasks.md`) is the same pattern in artifact form.

**Proposed change.** Add a stage `specifying` between `ready` and
`implementing`:

```
ready → specifying → implementing → verifying → validating → …
```

`_handle_specifying`:
1. Spawn a spec agent with read+test-write tools, deny implementation
   paths (`Edit/Write` allowed only against `tests/**`).
2. Spec agent commits failing tests; the orchestrator runs the test
   command and verifies failure on `origin/<base>` and on HEAD.
3. On verified-failing: flip to `implementing`. The implementer prompt
   includes the test-file allowlist and an explicit "you may not modify
   `tests/**`" rule. Post-implementer the orchestrator runs
   `git diff --name-only HEAD origin/<base>` and rejects + parks if
   any test file was touched.
4. On spec-agent failure ("I can't write tests for this") the agent
   emits a typed reason (`ac-clarification` / `dep-missing` /
   `design-question` — borrowed from `the-furnace`). We park awaiting
   human with the typed reason, which is more actionable than today's
   freeform park comments.

**Impact.** Forces the implementer to satisfy an externally-defined
contract. Closes a real failure mode the orchestrator can't currently
detect. Pairs with #1 (verifying) by ensuring there's something
meaningful to verify against.

**Cost.** ~600 lines and a new label. Largest Tier-2 change.

**Trade-off.** Some issues genuinely can't be expressed as failing
tests (docs changes, refactors). Add a `spec_skip: true` field to the
decomposer manifest so the decomposer can opt out per-child. Touching
the manifest schema requires backward-compat care
(`docs/architecture.md`'s rule on pinned-state schema).

---

### 10. Issue-edit content-hash re-triage

**Problem.** If a human edits the issue body or adds acceptance criteria
mid-flight, the orchestrator doesn't notice. The dev agent re-reads the
issue on resume, but the workflow state machine doesn't react: an issue
that was triaged as size=s and decomposed to single might genuinely
need split after the edit.

**What others do.** `heavy-lifting/docs/contracts/triage-routing.md`
stores SHA-256 of `(title + description + acceptance_criteria + references)`
excluding the orchestrator's own writes
(`compute_user_content_hash`) in
`fetch.context.metadata.last_triage_user_content_hash`. On next poll,
if the hash changed → fresh triage. Reopen detection: tracker
`status==NEW` + DONE impl exists + no in-flight, with a
`last_reopen_consumed_done_impl_id` marker to prevent self-loops.

**Proposed change.** In `_handle_pickup` and at the start of every
per-tick handler, compute `user_content_hash` over (title + body +
human-authored comments, excluding orchestrator-authored ones). Compare
against the value in pinned state. On change:

- Before `validating`: route back to `triaging` (or `decomposing` if
  triage is disabled). The decomposer manifest is re-derived against
  the new body.
- After `validating`: post a comment "issue body changed; resuming dev
  session" and resume the dev session on its locked backend with the
  new body quoted. Don't re-decompose mid-implementation — too
  disruptive.

**Impact.** Catches the "human updates the issue mid-flight" failure
mode that today silently goes unrecognized. Particularly useful when
proposal #6 (Decision Gate / Triage) is in place — a user can iterate
on the issue body in response to clarification questions, and the
orchestrator naturally re-triages each time.

**Cost.** ~100 lines: hash helper, pinned-state field, wiring in three
handlers. Tests in `test_workflow.py` against the fakes.

**Trade-off.** Excluding orchestrator-authored comments cleanly requires
a stable "is this an orchestrator comment" predicate. We already write
HTML markers (`<!--orchestrator-state ...-->`) and a fixed pickup
comment; extend by tagging every orchestrator comment with a
`<!-- agent-orchestrator -->` marker (one-line change).

---

### 11. PR-feedback hash debounce and per-PR resilient polling

**Problem.** `_handle_in_review`'s comment-debounce logic is dense:
three id-namespace watermarks (`pr_last_comment_id`,
`pr_last_review_comment_id`, `pr_last_review_summary_id`), an
`IN_REVIEW_DEBOUNCE_SECONDS` window, and a standing-CHANGES_REQUESTED
veto. It works, but the failure modes are subtle (re-bumping a
watermark past an automation's own park comment took an explicit
`_bump_in_review_watermarks` helper). And every comment counts as a
fresh resume trigger — a chatty PR thread can resume the dev many
times.

**What others do.**

- `the-foundry/src/foundry/workflows.py:_feedback_hash` SHA-256's the
  formatted feedback block. The next poll skips the PR if the hash
  hasn't changed. So even if the watermark moved, the dev isn't
  re-spawned with the same content twice.
- `blast-furnace`'s `sync-tracker-state` ignores per-comment events
  entirely and only reacts to a `rework` label flip. A human applies
  the label when they think there's enough feedback to act on — a
  natural batch signal.
- `the-anvils/whilly/sources/github_pr_feedback.py` uses a
  *per-PR* cursor: a slow or erroring PR's cursor doesn't advance,
  but the other PRs in the tick keep polling. Our current loop is
  effectively all-or-nothing per issue (a single
  `IN_REVIEW_DEBOUNCE_SECONDS` window).
- `heavy-lifting/docs/contracts/event-ingestion.md` builds a composite
  cursor `<iso_updated_at>|<source>|<numeric_id>` for ordered merging
  of `issues/comments`, `pulls/comments`, `pulls/reviews` — equal-
  timestamp comments aren't dropped or duplicated. We already split
  by id-namespace but don't have ordered merging.

**Proposed change.** Three orthogonal upgrades, each adoptable
independently:

1. **Feedback-hash debounce.** Before resuming the dev in
   `_handle_in_review`, compute SHA-256 of the formatted prompt
   (quoted comments + review bodies + inline diffs). Compare against
   `pr_feedback_hash` in pinned state; skip if unchanged. Cost: ~30
   lines.
2. **Optional label-only rework trigger.** Add `REWORK_LABEL=rework`
   env. When set, `_handle_in_review` only resumes when the label is
   present *and* the hash changed. The label is auto-removed on a
   successful push back to `validating`. Cost: ~80 lines + label
   bootstrap. Mutually exclusive with the current per-comment debounce
   in `REWORK_TRIGGER=label` mode, gated by an env knob so adopters
   can pick.
3. **Per-PR resilient polling.** Today an exception in `pr_reviews_after`
   for issue N kills the tick for that issue (caught by the outer
   per-issue exception isolation). Tightening: wrap each feedback
   source in its own try/except so a slow PR's review-summary fetch
   doesn't suppress the inline-comments fetch in the same tick.
   Cost: ~50 lines, mostly defensive logging.

**Impact.** Removes a class of subtle replay bugs ("HITL ping re-treated
as PR feedback") at the source rather than via watermark gymnastics.
Label-mode is an explicit opt-in for repos with chatty review threads.

**Cost.** ~150 lines total across three changes. Tests against the
fakes already exercise the watermark paths; add hash-equality cases.

**Trade-off.** Label-mode is a behavioural change visible to humans
(they have to add a label). Off-by-default; off-by-env for migration.

---

### 12. Routing-label workflow dispatch

**Problem.** Today the workflow label drives only *which stage* an
issue is in. The agent prompt the dev (or reviewer) actually runs is
hardcoded inside `workflow.py` per stage. A repo with both "small
typo-style fixes" and "multi-day refactors" has no way to route them
to different prompts without code changes.

**What others do.** `X15`'s `backlog-orchestrator` requires every issue
to carry exactly one `archon-workflow:<name>` label
(`workflowLabelToName` in `packages/backlog-orchestrator/src/config.ts`).
The map binds each routing label to a workflow definition (`ralph`,
`fix-issue`, `fix-issue-simple`, `refactor`, `docs`, `video-recording`,
…). The orchestrator looks the label up and runs the corresponding
prompt from `.archon/commands/defaults/<name>.md`. Adding a new
workflow is a config-map edit + a new markdown file — no orchestrator
code change. `src/eligibility.ts` rejects issues missing or carrying
ambiguous routing labels with a precise comment ("Missing
archon-workflow:* routing label" / "Ambiguous workflow routing labels"),
which is also a much sharper diagnostic than today's freeform
`awaiting_human` reasons.

`iron-press` has a related but heavier mechanism (a full DAG workflow
per label — covered by proposal #18). X15's version is strictly simpler:
label → single prompt path, no graph engine.

**Proposed change.** Insert a second label dimension alongside the
workflow-stage label:

```
aorch-route:<name>     # e.g. aorch-route:bugfix, aorch-route:feature, aorch-route:docs
```

`config.py` exposes a map:

```python
ROUTES = {
    "bugfix":  {"prompt": "prompts/dev-bugfix.md",  "decompose": False, "max_files": 5},
    "feature": {"prompt": "prompts/dev-feature.md", "decompose": True},
    "docs":    {"prompt": "prompts/dev-docs.md",    "decompose": False, "verify_skip": True},
    "refactor":{"prompt": "prompts/dev-refactor.md","decompose": True,  "spec_required": True},
}
DEFAULT_ROUTE = "feature"
```

Wiring:
- `_handle_pickup` reads `aorch-route:*` labels; missing label → take
  `DEFAULT_ROUTE`; multiple labels → park with a precise diagnostic
  comment (X15's pattern, sharper than `awaiting_human`).
- Each per-stage prompt builder in `workflow.py` reads the route from
  pinned state and selects the prompt file via the route map. The
  decomposer can read `decompose=False` and short-circuit straight to
  `ready`; the reviewer can read `verify_skip=True` and skip proposal #1
  for docs-only changes.
- Routes are versioned via a `schema_version` in the map and persisted
  on the issue's pinned state at pickup so a mid-flight config change
  doesn't move issues onto a new prompt.

**Impact.** A single config edit + a prompt file lets the operator add
support for a new *kind* of issue. Docs-only routes can skip `verifying`
+ the reviewer agent entirely (saves both wall-clock and tokens).
Bugfix routes can pin a tighter `max_files` budget that decomposer
respects. Today these options require code changes.

**Cost.** ~400 lines: route map config + parser, prompt-file loader,
wiring in each handler, label bootstrap, validation. Tests against the
fakes need a new fake-route fixture.

**Trade-off.** Adds a second label dimension that humans have to apply
on issue creation. Mitigated by `DEFAULT_ROUTE` and by a non-fatal
"missing route → comment + take default" pickup path (only ambiguous
routes park). Long-term we may want to auto-suggest the route from the
issue body (X15 doesn't; it expects the human to label) — out of scope
for the first version.

---

## Tier 3 — opportunistic polish

### 13. Inline Escalation Manager before parking on awaiting_human

`night-shift/docs/superpowers/specs/2026-05-02-escalation-manager-
design.md`: before flipping to `blocked`, run a capped recovery agent
(1 primary attempt + 1 repair turn) that either re-routes the workflow
or produces a structured human-handoff comment (root cause + evidence
+ recommended action). The agent **cannot** mutate status / merge / close;
only the orchestrator applies those actions based on the agent's output.

For us: wrap every `_park_awaiting_human` call in
`_attempt_escalation(park_reason)` that runs a one-shot agent on
`ESCALATE_MODEL=haiku` to produce a structured comment. Most parks
today have an unhelpful `last_message` quoted; this turns them into a
diagnostic with actionable next steps. ~250 lines. Opt-in via
`ESCALATION_AGENT=on`.

### 14. Image and attachment ingestion from issue/PR bodies

`steam-hammer` auto-downloads GitHub issue/PR image attachments and
includes them in the agent's working set. Many bug reports are
unreproducible from text alone (screenshots of failing UIs, stack-
trace screenshots, sketches). For us: scan issue/PR body and comments
for `![alt](https://github.com/.../assets/...)` URLs; download to
`<worktree>/.agent-orchestrator/attachments/`; mention them in the
implementer prompt. Both Claude and Codex CLIs can `Read` image files.
~150 lines.

### 15. Per-issue / per-plan USD budget enforcement

`the-anvils` tracks per-plan spend and emits `plan.budget_exceeded`
events at threshold crossings, with `WHILLY_BUDGET_USD` as a hard cap.
`iron-press` has `ORCH_OVER_BUDGET` as a global kill switch. Today our
only cost controls are wall-clock (`AGENT_TIMEOUT`) and spawn-count
(`MAX_RETRIES_PER_DAY`); we have no $ accounting at all. Both Claude
and Codex JSONL events carry per-event cost. Sum into pinned state
(`spent_usd`); compare against `MAX_USD_PER_ISSUE` (default 0 →
unbounded); park awaiting human on exhaustion. ~200 lines.

### 16. Append-only JSONL event ledger

`blast-furnace`'s `<runId>_handoff.jsonl` carries every stage transition
(`timestamp, stage, eventType, message, context`). `the-foundry`'s
`task_events` SQLite table does the same. Useful for replay, audit,
debugging — strictly better than today's "tail the orchestrator's log
file."

For us: write to
`<target_root>/.agent-orchestrator/events/issue-<n>.jsonl`. One file
per issue, append-only, rotated on terminal. Schema: workflow events
(`stage_entered`, `stage_exited`, `park`, `resume`, `agent_spawn`,
`agent_returned`, `verify_run`, `pr_opened`, `pr_merged`). ~200 lines
including a `--replay <issue-n>` mode that re-walks the events for
post-mortem. Off by default to keep zero new files on the filesystem.

### 17. Devcontainer / fresh-clone isolation per attempt

`the-furnace` boots a devcontainer per *attempt* (not per ticket).
`steam-hammer`'s daemon mode uses fresh per-worker clones, not
worktrees. Both reduce blast radius: an agent that mutates
`.git/hooks` (which our existing `_git_hardened` defends against
during merge but not during the implementer run) can't leak across
attempts.

For us: add an isolation level env `WORKTREE_ISOLATION=worktree|clone|
container` (default `worktree`, current behaviour). `clone` makes
`_ensure_worktree` do a fresh `git clone --depth 100` instead of
`git worktree add`. `container` is a longer story (rootless podman or
docker; the agent CLIs need to be installed inside; auth handoff is
finicky); defer.

Material only if we hit blast-radius issues in practice. Lower priority
because our existing host-is-sandbox posture is documented and
intentional.

### 18. Workflow-as-DAG selectable by issue label

`iron-press`'s `workflow.json` defines nodes and edges declaratively;
`iron-press.config.json` maps issue labels to whole workflows
(`{"feature": "simple", "bug": "sm"}`). Adding a new workflow is a JSON
+ skill-markdown drop, not a code change.

Heavyweight. Our state machine is small enough that the configurability
gain doesn't justify the engine rewrite today. Worth re-evaluating
once Tier-1 and Tier-2 land and the workflow has more stages —
because at that point any *new* stage costs us a fresh round of
`workflow.py` editing and dispatcher updates, whereas a DAG model
makes new stages near-free.

### 19. `SAFE_AGENT_MODE` as a documented opt-out

`the-foundry/src/foundry/security.py` defaults `SAFE_AGENT_MODE=true`,
which strips the dangerous flags. Their README explicitly contrasts this
with our default. We could expose `SAFE_AGENT_MODE=on` as an env var
that maps to dropping the `--dangerously-*` flags on both backends, off
by default to preserve current behaviour, with `README.md` documenting
when to flip it on (running on an unsegregated host with sensitive
data nearby). ~30 lines.

### 20. Pre-implement checkpoint diff

`the-foundry/src/foundry/security.py:checkpoint_diff` writes
`git diff --binary HEAD` to a file before each implementer attempt; on
retry it `git reset --hard HEAD` and re-runs. Today our retries reuse
the worktree as-is (or force-rebuild from `origin/<base>` if no
unpushed commits). Saving the failed-attempt diff (instead of throwing
it away on rebuild) is one `git diff --binary HEAD > ...` and lets us
inspect what the agent tried. ~40 lines, no functional change.

### 21. `agent_retro` append-only feedback channel

`heavy-lifting`'s `result_payload.metadata.agent_retro` lets the agent
emit append-only retrospective entries (tag / message / category /
severity). Aggregated via `GET /retro/tags` for operator visibility on
recurring failure modes. We could add a `retro` array to the pinned
JSON, with a max-N cap and rotation, and a small `--retro` CLI flag to
`main.py` that dumps an aggregated view. Useful once we have a few
weeks of runs to mine. ~80 lines.

### 22. Static linter for the orchestrator's own prompts / code

`gear-grinders/src/gg/.../agent_patterns.py` lints agent code for
unbounded loops, unbounded retries, prompts >~4000 tokens, and tool-
registry mismatches. Reports `[P]` (procedural blocker) vs `[H]`
(advisory). Could be a pre-commit hook over `orchestrator/workflow.py`'s
prompt-building helpers. Niche; only valuable if we add many more
prompts.

### 23. Producer trailer in commit messages

A subset of proposal #3. Standalone value: provenance in git log. Even
without the opposite-model reviewer router, `Produced-By: claude` in
each commit makes `git log` legible. ~10 lines.

### 24. Bug-fixture test repo (orchestrator regression test)

`the-smelters/projects/python_fixture` is a small repo with five
intentionally seeded bugs (divide-by-zero, off-by-one, cache eviction,
…). Their orchestrator's regression test runs the full pipeline against
it and asserts the bugs get fixed. We have no end-to-end test that
exercises a real agent against the real `workflow.py`; everything in
`tests/test_workflow.py` uses fakes.

Build a `tests/fixtures/seeded-bugs/` mini-repo + a manually-runnable
integration test (`pytest --runintegration`) that runs against either
backend. Won't run in CI by default (needs agent auth), but gives us
a one-command repro for regressions. ~300 lines + the fixture repo.

### 25. `--dry-run` mode that previews state-comment writes

`steam-hammer`'s `--dry-run` previews scope decisions and state-comment
writes without invoking agents. Useful operationally for testing config
changes. For us: a `python -m orchestrator.main --once --dry-run` that
runs every handler in read-only mode, logging the writes it would do
without performing them. ~100 lines. Pairs well with proposal #16
(event ledger).

### 26. PREVIOUS_ITERATION_DIFF in the dev fix prompt

`the-smelters/agno_orchestrator.py` records the prior iteration's diff
(via `git stash create`) and feeds it back into the coder prompt to
prevent "blind reversions of correct edits" — a failure mode where
round 2 reverses something round 1 got right. Concretely: when
`_handle_validating` resumes the dev with CHANGES_REQUESTED, include
the prior round's diff explicitly. ~50 lines. Cheap and addresses a
real recurring failure mode.

### 27. Area-lock concurrency policy

`X15`'s `hasAreaConflict()` in
`packages/backlog-orchestrator/src/eligibility.ts` reads `area:*` labels
on each issue and refuses to schedule a run whose area set overlaps with
an already-running issue's area set (or with the changed-files snapshot
of an already-open PR). It's a coarse but cheap merge-conflict
avoidance: an operator labels two related issues `area:auth` and the
orchestrator serializes them without needing to inspect diffs.

For us: today the polling loop runs handlers serially per repo, so the
issue is mostly latent — but proposal #8 (parallel implementers) and
the natural progression of running multiple issues from one repo
concurrently make it real. Add `AreaLockPolicy = 'none' | 'conservative'`
config (`conservative` by default once #8 lands). `hasAreaConflict()` is
implementable in ~80 lines against `gh.list_pollable_issues()` output
+ `PullRequest.get_files()` on the open PRs. The fallback when areas
aren't labeled at all is "no lock," so adopters opt in by labeling.

### 28. Per-issue auto-merge label and "completes without PR" allowlist

Two small `X15`-inspired config refinements that share a theme: let the
human declare per-issue intent rather than relying on global flags.

- **Per-issue auto-merge opt-in.** Today `AUTO_MERGE` is a global env
  flag. X15's `isAutoMergeCandidate()` requires an `archon:auto-merge`
  label on the issue itself; without it the PR halts at
  `ready_for_review`. For us: add `PER_ISSUE_AUTO_MERGE_LABEL` (default
  empty → today's behaviour). When set, `_handle_in_review`'s
  auto-merge gate additionally requires the label on the issue.
  Operators can run with `AUTO_MERGE=on` globally for safety-low repos
  and require explicit opt-in on safety-high repos. ~30 lines.
- **Completes-without-PR allowlist.** X15's
  `workflowLabelsCompletingWithoutPr` is a per-workflow boolean for
  agent jobs that legitimately produce no PR (their example:
  `video-recording`). Our `_handle_implementing` parks as `_on_question`
  when the agent finishes without commits, which is wrong for, e.g.,
  a "look up some data and post a comment" issue. Add a
  `complete-without-pr` flag to the route map (proposal #12); when
  set, an empty-diff success terminates the issue as `done` with a
  summary comment instead of parking. ~60 lines on top of #12.

---

## Out of scope (explicitly rejected)

| Proposal | Why declined |
| --- | --- |
| Postgres / SQLite as primary state store (`the-anvils`, `the-foundry`, `heavy-lifting`) | Conflicts with our explicit design choice in `docs/workflow.md`: state lives in GitHub Issues for observability. We'd add operational complexity without a corresponding usability win. |
| Temporal as workflow engine (`the-furnace`, `night-shift`) | Big architectural lift; adds an external dependency our deployment story (`run.sh`-style on a VPS) explicitly avoids. Re-evaluate if/when we move to a GitHub App. |
| Visual workflow editor / Studio UI (`iron-press`) | Builds value only after #18 (workflow-as-DAG); itself a multi-week build. Out of band. |
| Telegram / Slack notifier (`drop-forge`) | Already covered by GitHub @-mentions per `docs/workflow.md`'s HITL design. Adding a sidecar channel is reasonable but cosmetic. |
| Tracker-agnostic adapter (Linear / Jira / GitHub Projects) | The current `GitHubClient` is intentionally GitHub-coupled per `docs/workflow.md`. Adding a `TrackerProtocol` is a real abstraction tax for unclear demand. |
| Web dashboard for run status (`the-foundry`, `the-anvils`, `heavy-lifting`) | GitHub Issues *is* the dashboard, per our design intent. |

## Suggested implementation order

A pragmatic build order if the team picks up this proposal:

1. #1 (Local quality gate) — single biggest win; closes the named gap
   in `plans/roadmap.md`.
2. #5 (Per-stage model overrides) — trivial; frees budget for later
   proposals.
3. #10 (Issue-edit content-hash re-triage) — small fix to a real bug.
4. #11 (PR-feedback hash debounce, just step 1) — small fix to another
   real bug.
5. #3 (Structured reviewer schema) — sharper fix loop; reviewer prompt
   work pairs with #1.
6. #4 (Permission profiles) — independent; mostly env wiring on top of
   what `agents.py` already does.
7. #2 (In-session stop-hook QG) — extends #1; depends on hook surfaces
   in the CLIs.
8. #6 (Decision Gate / triaging) — depends on #5 (cheap model) being
   in place.
9. #7 (Repo memory) — useful once #1, #6, #10 are landed and have
   somewhere meaningful to write to.
10. Tier 2 (#8 parallel candidates, #9 spec-first) — biggest design
    work, evaluate after Tier 1 is dogfooded.
11. Tier 3 as opportunistic backlog.

## References

Files cited above resolve under
`https://github.com/podlodka-ai-club/<repo>/blob/<default-branch>/...`.
Default branch is `main` for most repos, but **`X15` defaults to `dev`**
and **`heavy-lifting` defaults to `master`** (its `main` branch contains
only a placeholder `readme`). Substitute the right branch when
following these paths or they will 404:

- `the-foundry/src/foundry/{stages,security,state,workflows}.py`,
  `.env.example`, `prompts/verify.md`.
- `X15/packages/backlog-orchestrator/README.md` (branch `dev`),
  `packages/backlog-orchestrator/src/{config,types,eligibility,
  orchestrator}.ts`, `packages/backlog-orchestrator/src/db/store.ts`,
  `packages/backlog-orchestrator/src/adapters/github-gh.ts`,
  `packages/backlog-orchestrator/e2e/{run-fixture,run-live}.ts`,
  `packages/backlog-orchestrator/e2e/fixtures/*`,
  `.archon/commands/defaults/{archon-validate,archon-synthesize-review,
  archon-code-review-agent,archon-error-handling-agent,
  archon-test-coverage-agent,archon-comment-quality-agent,
  archon-docs-impact-agent,archon-ralph-generate}.md`.
- `iron-press/src/sdk/workflow/permission-profiles.ts`,
  `src/workflows/sm/workflow.json`, `src/dynamic-loader.ts`,
  `iron-press.config.json`.
- `heavy-lifting` (branch `master`):
  `docs/contracts/{triage-routing,event-ingestion,task-handoff}.md`,
  `docs/integrations/github.md`, `src/backend/schemas.py`,
  `src/backend/services/retro_service.py`, `orchestrator-research.md`.
- `the-furnace/openspec/concept.md`,
  `openspec/changes/{persona-reviewers,vote-aggregator}/`,
  `server/src/agents/coder/prompt.md`.
- `gear-grinders/src/gg/orchestrator/{executor,evaluation,verification,
  protocol,truth,agent_patterns,prompts,prompt_manifest}.py`,
  `docs/diagram-design.md`.
- `boiler-room/boiler_room/agents/{claude,copilot,codex}.py`,
  `tests/e2e/test_e2e.py`,
  `docs/superpowers/specs/2026-04-22-e2e-test-design.md`.
- `steam-hammer/cmd/orchestrator/main.go`,
  `internal/core/{agentexec,orchestration,githublifecycle}/`.
- `night-shift/orchestrator/src/{activity-deps,intake,comment-markers}.ts`,
  `orchestrator/src/phases/{specify,implement,review,escalation}/`,
  `docs/superpowers/specs/{2026-04-27-deterministic-phases-workflow-
  reference,2026-05-02-escalation-manager-design}.md`,
  `orchestrator/src/eval/`.
- `the-smelters/agno_orchestrator.py`,
  `agno_tools/claude_code_step.py`,
  `projects/python_fixture/`, `tasks/python_fixture/`.
- `blast-furnace/docs/{orchestrator-target-state-plan,
  handoff-ledger-migration}.md`,
  `openspec/changes/archive/2026-04-27-stop-hook-quality-gate-for-develop/`,
  `src/jobs/{develop,quality-gate}.ts`.
- `the-anvils/docs/Whilly-v4-Architecture.md`,
  `docs/workshop/adr/{008,011,016,018,021}*`,
  `whilly/quality/{python,node,go,rust}.py`,
  `whilly/{decision_gate,decomposer}.py`,
  `whilly/sources/github_pr_feedback.py`,
  `whilly/pipeline/human_review.py`,
  `whilly/triz_analyzer.py`, `.importlinter`.
- `drop-forge/architecture.md`,
  `docs/superpowers/specs/2026-04-28-cross-agent-review-design.md`,
  `internal/reviewrunner/{reviewer.go,prompts/*,reviewparse/parse.go,
  prcommenter/format.go}`,
  `internal/agentmeta/trailer.go`.
