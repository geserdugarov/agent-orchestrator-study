# Orchestrator

We have a repository for the orchestrator on GitHub.

Components of the orchestrator:

1. Issue tracker — we need a source of tasks to implement
2. Where the agent will run

# Choice of issue tracker

What we want:
1. Build asynchronous team work via a Telegram chat and GitHub Issues
2. Start building the orchestrator through itself as soon as possible (like compiler bootstrapping).

It follows that we should use GitHub Issues as the task tracker for the orchestrator.

We will try to keep the orchestrator stateless, with a minimum of data in its internal DB. Let its data live in plain sight in
GitHub Issues. This will let us observe the orchestrator's work and understand what stage it is currently in.

## Alternatives

### beads-rust

A good solution for the orchestrator's internal subtasks. We could use a combination of GitHub Issues for human interaction
and beads for communication between sub-agents inside the orchestrator. For now I propose to definitely start with GitHub Issues. We can also give the agent access to it for creating its own service tasks.

### A custom issue tracker built into the orchestrator

Wasted effort. We have limited time (2 weeks). We need to cut scope to the bare minimum. We don't build anything extra.


# Where the agent will run

During development — on the developers' machines.
Target solution: deploy it to a VPS so it runs as a background agent.

It is clear we will run agents with the `--dangerously-skip-permissions` flag or similar, because the agent is autonomous and
nobody is going to grant it permissions for every little action. We need to isolate it, but it is not yet clear how. Depending on the deployment, the
options are:

1. No isolation — e.g. on the developer's machine or on a dedicated VPS
2. Docker container
3. Virtual machine

# Human-in-the-loop

We are not aiming for full agent autonomy. Better to summon a human than to autonomously do something stupid.

Current vision: in case of uncertainty, summon a human via GitHub Issues using an @mention.

We expect that there should not be too many summons. We need to tune things so the agent can resolve small questions on its own but summons a human for important decisions. We need to find that boundary.

# Orchestrator workflow

We pick a fixed workflow with mandatory stages:

1. Decomposition — has HITL
2. Implementation — autonomous
3. Validation — autonomous
4. Acceptance — has HITL

User-orchestrator interaction happens through GitHub Issues.

## Alternatives

A dynamic workflow. Add another agent ahead of task execution that builds a plan by selecting the needed stages.
For example, it could add extra stages (architectural exploration, design review) or remove unnecessary stages (skip
acceptance for trivial fixes).

That seems excessive for our task. The project deadline is tight. First we implement the simple solution.

# Labels

An issue must have at most 1 label. It is essentially the task status.

* no label — needs to start being decomposed
* `decomposing` — applied to issues currently being decomposed
* `ready` — applied to decomposed issues ready for development
* `blocked` — applied to decomposed issues that are blocked by other tasks
* `umbrella` — applied to a decomposed parent that has no implementation of its own; auto-closes to `done` once every child resolves
* `implementing` — applied to issues being worked on by code agents
* `validating` — applied to issues going through automated validation
* `in_review` — applied to issues for which a PR is ready
* `resolving_conflict` — auto-resolving merge conflicts after a sibling PR landed first
* `done` — terminal status for completed issues whose code is merged into `main` (+ the issue must be closed)
* `rejected` — status for issues that were declined (+ the issue must be closed)

# Decomposition

The orchestrator must notice that a new open issue without labels has appeared, attach the `decomposing` label, and start decomposing it.

The orchestrator must study the task context in the GitHub Issue and gather the missing information by interacting in the comments
of the issue with a human.

Once enough information has been collected, the bot can create nested issues to hand off to code agents for implementation.

If a task can be implemented within a single agent context, then no subtasks need to be created. The current criterion the orchestrator passes to the decomposer in the prompt is: if the change touches more than ~5 files or requires more than one logical commit — propose splitting; otherwise leave it as is. The criterion is imperfect but explicit, and easy to tune later.

The decomposer returns a structured response — a single fenced JSON block `orchestrator-manifest` with schema `decision: "single" | "split"`, an optional `children` list, an optional `depends_on` (an array of 0-indexed references between children, with no cycles or self-dependencies, no more than 10 children), and an optional `umbrella` boolean (default false). The `_parse_manifest` parser rejects malformed manifests and moves the issue into `awaiting_human` for triage.

When `umbrella` is true on a `split` decision, the parent has no implementation work of its own — its only purpose is to aggregate the children. The orchestrator labels it `umbrella` instead of `blocked`, and `_handle_umbrella` auto-resolves it to `done` (closing the issue) once every child reaches `done`. A non-umbrella decomposed parent goes through `blocked` and re-enters implementation once its children resolve.

**TODO:** refine the criteria (file threshold, layer-based splitting) as we accumulate experience.

Related tasks need to be linked to each other. Those waiting on other tasks must keep the `blocked` label.
Tasks that are ready to implement and have no blockers get the `ready` label.

# Implementation

When implementation begins, the bot must remove the `ready` label and apply `implementing`. It posts a comment that it has started work.

Review the comments in the issue. If session identifiers are already present in the comments, resume the existing session with its context. Read new comments into the context.

If a PR has already been created, read the active discussions from it.

After implementation finishes, the bot must create a PR with the changes. Leave a comment on the issue with the session identifiers so that
we can return to those sessions with context if needed via `codex resume` or `claude resume`.

After work finishes, remove the `implementing` label and apply the `validating` label. Push the changes to the branch.

Possibly at this stage several agents will work in parallel and produce several solutions for the task. We need to pick one best
solution out of them, or merge them together. In the first version there will be 1 solution.

# Validation

Create a fresh, clean code-agent session. Study the changes and comments in the issue.
Where possible this should be a different code agent, not the same one that wrote the code. It will check the written code against the task at hand. On a `CHANGES_REQUESTED` verdict the orchestrator posts feedback into the PR, **resumes the implementer's session (locked backend)** right inside `validating` with a fix prompt, pushes a commit, and increments `review_round` — the issue stays in `validating` for the next reviewer pass. There is no return to `ready`; `ready` is reserved for the initial transition into implementation. After `MAX_REVIEW_ROUNDS` (default 3) unsuccessful rounds or a verdict that could not be parsed (no `VERDICT:` marker found) — park HITL.

The backend choice (`codex` / `claude`) is set via the `DEV_AGENT` and `REVIEW_AGENT` environment variables; by default claude implements and codex reviews.

We can add an architectural review — for example, flagging absence of large files that could be split.

**TODO (not implemented):** running tests, linters, and other project-specific checks at the validation stage. Currently `_handle_validating` only spawns the reviewer agent and runs no other local checks; the GitHub checks status (`pr_combined_check_state`) is consulted later, in the auto-merge gate of `_handle_in_review` (under `AUTO_MERGE=on`), not before the transition into `in_review`.

By this stage the PR has already been created (the implementer opens it before the transition to `validating`). After reviewer approval, change the label to `in_review`.

# Acceptance (HITL)

At this stage we expect a created PR on GitHub. The user can give feedback through PR comments. Automated review by other bots
(Code Rabbit) is also possible. The orchestrator must react to comments and update the branch
(resume the implementer's session and apply fixes).

A PR can reach two terminals — merge (issue → `done`) or close without merge (issue → `rejected`). Who initiates depends on settings and human presence:

* **merge under `AUTO_MERGE=on`** (default `off`) — the orchestrator merges the PR itself, **without requiring human approval**: a reviewer-agent approval is enough (`agent_approved_sha == pr.head.sha`, the snapshot is taken in `_handle_validating` on a `VERDICT: APPROVED` verdict). A real `APPROVED` review from a human/bot on the *current* head SHA is a separate, equivalent path to approval; stale APPROVEDs on older commits do not count. Gates in evaluation order: (0) **standing CHANGES_REQUESTED veto** — `gh.pr_has_changes_requested(pr, head_sha=head_sha)`: if a human has a standing `CHANGES_REQUESTED` review on this commit, the merge does not happen even with `agent_approved_sha == head_sha`, a mergeable PR, and green checks (silent return until the next tick — the human must dismiss the review or push a fix); (1) approval is present (see above); (2) `pr_is_mergeable` is true; (3) combined CI status is `success`. Not every unmet gate parks: a HITL ping is posted only when `pr_is_mergeable=False` (branch protection / conflicts / out-of-date base) and when `pr_combined_check_state` ∈ {`failure`, `none`}. The states "standing CHANGES_REQUESTED", "no approval", `pr_is_mergeable=None` (GitHub is still computing it), `pending` checks, and a `head_sha` shift mid-tick are just silent returns until the next tick, with no HITL. After a successful merge — the `done` label, the `merged_at` stamp, and the issue is closed.
* **merge under `AUTO_MERGE=off`** — the orchestrator does not merge itself and **does not check any approval gates**: after processing new PR comments, `_handle_in_review` simply returns control without calling `gh.pr_is_approved` or reacting to `APPROVED` reviews. The merge is performed by a human (or an external bot) by hand through the GitHub UI; the next tick will see `pr_state == merged` and move the issue to `done`. If "N approvals" are required — that is enforced by **GitHub branch protection**, not by the orchestrator; an approve on its own without pressing Merge does not move the issue forward.
* **reject — PR closed without merge** (by a human in the UI, by a bot, or by CLI) — terminal: the orchestrator sees `pr_state == closed`, applies the `rejected` label, sets the `closed_without_merge_at` stamp, and closes the issue.
* **reject — issue closed manually while the PR is still open** — a separate path and an important caveat: the orchestrator applies the `rejected` label and the `closed_without_merge_at` stamp, but **the PR stays open** (the handler does not call `pr.edit(state="closed")` in this branch). This is a hard-stop signal from the human — without this branch `AUTO_MERGE` could merge the PR over the human's "no" even when the issue is closed. If the PR also needs to be closed — the human does that by hand. There is no return to the decomposition stage in any reject scenario — if the task needs to be reopened, create a new issue.

A background process monitors for new comments or reviews on the PR. There are four sources, but only three *high-watermarks* in pinned state — one per id namespace in the GitHub REST API: `pr_last_comment_id` covers both the issue thread *and* PR conversation comments (both live in the shared IssueComment id space, so a single watermark suffices), `pr_last_review_comment_id` — inline review comments (PullRequestComment id space), `pr_last_review_summary_id` — PR review bodies in the PullRequestReview id space; the code only forwards bodies from `CHANGES_REQUESTED` and `COMMENTED` reviews to the implementer, **`APPROVED` is excluded as informational** (filter in `gh.pr_reviews_after`); empty bodies and dismissed/pending reviews are also dropped. If there are new comments and more than `IN_REVIEW_DEBOUNCE_SECONDS` (default 600 s) has passed since the most recent of them, the orchestrator resumes the implementer's session (the same backend that wrote the code) with the new comments quoted, pushes a fix, and moves the issue back into `validating` (not `ready`!) — there the reviewer will run again on the new diff. If the debounce window has not yet elapsed — wait for the next tick, the human may still be typing.

When changes are merged into `main`, the agent should walk the related tasks and, where possible, update their status from `blocked` to `ready`.

# What the orchestrator needs to operate

1. API keys / subscriptions for AI, or more precisely just configured, ready-to-call agents (`codex` launches and is ready to work)
2. A GitHub API token

# Next steps
1. Add a documentation stage so the agent keeps the `docs/` folder up to date.
2. Add several parallel agents working on the same task so we can pick the best of them afterwards.
3. A dynamic workflow?
