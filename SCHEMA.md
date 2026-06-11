# Atlas Clutch Ledger Schema Reference

schema_version: 0.1.0 (documentation baseline, 2026-06-11)

Every ledger object is a Markdown file: a `# <id> <title>` heading, a metadata
block of `key: value` lines (everything before the first `## ` heading, parsed
by `bridge.task_metadata`), then `## ` sections (extracted by
`bridge.task_section`). Timestamps are local-offset ISO 8601 from
`bridge.iso_now()`. IDs embed creation time and gain `-NN` suffixes on
same-second collisions.

## Objects

| Object | ID prefix | Directory | Key metadata | Status flow |
|---|---|---|---|---|
| Task | `OHB-` | workbench/tasks | status, created_at, project_id, evidence_gap_risk, live_skipped | open → reported → reviewed → passed/needs_evidence/blocked/cancelled → archived |
| Project | (slug) | workbench/projects | status, created_at | active (Lessons Learned appended by approved retros) |
| Dispatch | `DISPATCH-` | workbench/dispatches | status, task_id, target_executor, context_id | ready → sent → returned → qa_ready → reviewed → closed (needs_evidence loops back) |
| Execution | `EXEC-` | workbench/executions | status, dispatch_id, task_id, run_policy, completion_state, returncode, timed_out, owner_write_policy, write_target_fidelity, **recovered_from**, **superseded_by** | prepared → started → returned / failed / needs_manual_start / cancelled |
| Evidence | `EV-` (entries) | workbench/evidence/`<task_id>`.md | per-entry: type, verified/observed/claimed, source | accumulates; closure derives evidence_closure_state |
| Retro | (task id) | workbench/retros | retro_id, task_id, project_id, status | draft → approved → archived |
| Learning proposal | `LEARN-` | workbench/learning/proposals | status, source_task_id, application_status | proposed → approved/rejected/deferred (registry copy on approve) |
| Apply plan | `APPLY-` | workbench/applications | status, source_learn_id, target_path | planned → applied → reverted/cancelled |
| Context pack | `CTX-` | workbench/context_packs | source_task_id / source_project_id, target | created → archived |
| Pilot | `PILOT-` | workbench/pilots | project_id, status | started → completed |
| Collection | `COLLECT-` | workbench/collections | task_id, profile, kind | snapshot/smoke records, attachable to tasks |
| Playbook | — | workbench/playbooks/global.md, projects/`<id>`.md | per-entry: learn_id, apply_id, applied_at | append-only; reverts append Revert Notes |

## Field conventions

- Booleans are the strings `true` / `false`.
- `recovered_from` / `superseded_by` (added 2026-06-11): link a rerun execution
  to the failed execution it replaces; written by `/exec rerun` via
  `upsert_exec_field`. `scripts/metrics_snapshot.py` counts completed
  executions with `recovered_from` as `recovered_via_rerun`.
- Closure facts live in the task's `## Closure Evidence` section
  (`evidence_closure_state: verified_evidence_ready` is the completeness
  criterion used by metrics).
- Timeline entries are `- <iso_timestamp> <event text>.` lines; metrics parse
  `task created` and `user decision <decision>` events.

## Versioning policy (strategy doc §6.5)

- This document is the schema source of truth; bump `schema_version` with
  SemVer on any field addition (minor) or breaking rename (major).
- Per-artifact `schema_version:` stamping in new ledger files is planned but
  not yet implemented — run it as a dispatched workbench mission so the
  change itself flows through task → evidence → review.
- Parsers must stay tolerant of unknown fields (current `task_metadata`
  behavior) so minor bumps never break old records.
