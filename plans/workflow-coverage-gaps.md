# Workflow coverage gaps after `decomposing.md`

What `docs/workflow.md` prescribes that is **not** delivered by the
combination of Day 1–10 ship + `plans/decomposing.md` (Day 11–12).

The full label sequence (no-label → `decomposing` → `ready`/`blocked` →
`implementing` → `validating` → `in_review` → `done`/`rejected`) is wired
end-to-end. The gaps below are behaviors workflow.md asks for inside or
around that sequence, plus items workflow.md itself flags as future work.

## Real gaps (workflow.md prescribes, code doesn't do)

1. **`rejected` → re-decompose loop.** `docs/workflow.md:141`: *"В случае
   reject бот закрывает PR и возвращается на этап декомпозиции с новыми
   вводными."* Today `_handle_in_review` (`workflow.py:1732-1742`) treats
   reject as terminal: PR closed → label `rejected` → issue closed. No
   back-edge to `decomposing` with the rejection feedback as new input.
   `plans/decomposing.md` does not add one (`Out` section explicitly
   defers re-decomposition for in-flight `blocked` parents, but the
   reject-on-PR case is a different path and is just absent).

2. **Project tests/linters during `validating`.** `docs/workflow.md:130`:
   *"здесь нужен прогон тестов, линтеров и прочих проверок, специфичных
   для проекта."* `_handle_validating` (`workflow.py:1161+`) only spawns
   the reviewer LLM; there is no `pytest` / `ruff` / `mypy` /
   project-script invocation. Project-level checks happen externally via
   PR CI and are only consulted at the AUTO_MERGE gate
   (`pr_combined_check_state`), not as a precondition for the
   `validating → in_review` flip.

3. **Container / VM isolation + VPS deploy.** `docs/workflow.md:39-45`
   flags Docker / VM isolation as an open question. Roadmap Day 13
   ("Dockerfile / systemd / GitHub App migration") is the only
   `⬜ Not started` row in `plans/roadmap.md`.

## Explicit future work in workflow.md itself

These are acknowledged in workflow.md as deferred / optional / out of
scope for the first version:

4. **Parallel implementers + pick-best/merge** (`workflow.md:118-119`).
   workflow.md itself says *"В первой версии пусть будет 1 решение."*
   `plans/decomposing.md` "Out" section also defers it.
5. **Architectural review** (`workflow.md:128`) — phrased as *"Можем
   добавить"* (optional).
6. **Documentation stage** (`workflow.md:154`) — listed under "Дальнейшие
   шаги."
7. **Dynamic flow** (`workflow.md:67-71, 156`) — explicit "Альтернативы,"
   judged excessive for the 2-week budget.

## Plan-internal deferrals (not required by workflow.md)

The plan's "Out" section also defers items workflow.md does **not**
require, included here for completeness so future readers don't double-
count them as gaps:

- Cross-repo sub-issues. workflow.md doesn't speak to repo placement.
- Multi-level decomposition (a child being itself decomposed).
  workflow.md only mentions one level of "вложенные Issue."
- beads-rust integration. workflow.md lists this under "Альтернативы" and
  recommends starting with GitHub Issues.
- Re-decomposition when an in-flight `blocked` parent gets new info.
  workflow.md doesn't prescribe this.

## Recommended next pieces

If the goal is to fully honor workflow.md as written:

1. Wire `rejected` back into `decomposing` (gap #1) — the smallest
   delta, and the one that closes a literal contradiction with the doc.
2. Run project tests/linters as part of `_handle_validating` (gap #2),
   so the orchestrator doesn't rely on external CI for correctness gating
   inside its own state machine.
3. Roadmap Day 13 (gap #3) for the deployment story.

Items #4-7 stay future per workflow.md's own text.
