# Atlas Clutch

> A local AI workbench bridge for safe Claude execution, Codex review, and gated file automation.

Atlas Clutch (formerly Octo-Hermes Bridge) is a local bridge that turns natural-language task messages into governed AI work: Claude writes files, Codex reviews the result, and a set of safety gates decides whether the change can auto-close or must wait for human review.

> **Naming note:** Atlas Clutch is the product name. Runtime internals keep their original Octo / Hermes / Atlas Workbench identifiers (task IDs `OHB-*`, the `OctoHermesBridge` scheduled task, etc.) for compatibility with existing deployments.

## Why

Letting an AI agent write files directly is convenient and risky. A naive automation loop will happily touch files you never mentioned, claim success without evidence, sweep unrelated worktree changes into its result, or run `git push` and `deploy` on its own initiative.

Atlas Clutch treats every write as a contract:

- The task must **declare** exactly which files may be created or updated.
- The writer (Claude) runs in workspace-write mode against those targets only.
- After the run, the bridge **verifies** — not trusts — what actually changed on disk.
- An independent reviewer (Codex) must return a pass candidate.
- Only when every gate passes does the task auto-close; otherwise it is routed to a human.

## Architecture

```text
User / boss (natural-language task)
        │
        ▼
Atlas / Bridge ── parses owner-write targets, acceptance criteria,
        │         required validation commands
        ▼
Claude (writer) ── workspace-write execution, declared targets only
        │
        ▼
Safety gates ── write-target fidelity · acceptance fidelity ·
        │       validation evidence · worktree ownership
        ▼
Codex (reviewer) ── independent review, pass_candidate / concerns
        │
        ▼
auto_decision: pass ──────────── or ──────────── needs_human_review
```

## Capabilities

- Single-file owner-write tasks
- CJK owner-write target parsing (`只允许创建或更新：`)
- CJK exact-one-line acceptance (`只写一行：`)
- Bounded file packs (2–5 declared files per task)
- Feature-slice acceptance criteria: `exists` / `non-empty` / `contains`
- Required validation evidence gate (allowlisted commands such as `py_compile`, `git diff --check`)
- No `git add` / `git commit` / `git push` from the AI writer — ever
- Deploy commands are forbidden in tasks and validation blocks

## Examples

All examples write only under `workbench/tmp/`, which is local runtime state and never committed.

**One file, CJK exact-one-line acceptance:**

```text
/auto task 只允许创建或更新 workbench/tmp/cn-single.txt. 只写一行：cn single ok. Do not modify source code. No git add/commit/push. --project auto_exec
```

**Two-file file pack:**

```text
/auto task 只允许创建或更新：
- workbench/tmp/filepack/a.txt
- workbench/tmp/filepack/b.md
验收：
- workbench/tmp/filepack/a.txt => 只写一行：alpha ok
- workbench/tmp/filepack/b.md => 只写一行：beta ok
Do not modify source code. No git add/commit/push. --project auto_exec
```

**Feature slice with acceptance criteria and required validation:**

```text
/auto task 只允许创建或更新：
- workbench/tmp/featureslice/tool.py
- workbench/tmp/featureslice/notes.md
验收：
- workbench/tmp/featureslice/tool.py => 包含：def answer
- workbench/tmp/featureslice/notes.md => 包含：Feature Slice
必须验证：
- python -B -m py_compile workbench/tmp/featureslice/tool.py
- git diff --check
Do not modify source code except the allowed targets. No git add/commit/push. --project auto_exec
```

## Safety model

- **Declared write targets only.** A task must list the files it is allowed to create or update; anything else is out of bounds.
- **Workspace-relative paths only.** Absolute paths, drive letters, traversal (`../`), environment-variable expansion, and disallowed extensions are refused at parse time.
- **Post-run write-target fidelity.** After the run, `git status` is compared against the declared targets. Changes outside the declaration — including pre-existing dirt from another actor — block auto-close (worktree ownership guard).
- **Acceptance fidelity.** Concrete acceptance criteria (`exists`, `non-empty`, `contains`, exact-one-line) are re-verified against the real files on disk. A missing or wrong file fails the gate; the bridge never fakes a pass.
- **Validation evidence.** When a task declares required validation commands, the run must produce evidence of each command with returncode 0. Validation commands come from a strict allowlist — no shell chaining, pipes, redirection, or absolute paths.
- **Independent Codex review.** A separate reviewer must return `pass_candidate` before auto-close.
- Any gate failure results in `needs_human_review` instead of a silent pass.

## Current limitations

- **Local-first.** Designed to run on the owner's machine; not hardened for multi-tenant or hosted use.
- **Requires a compatible Octo deployment** for message intake and routing.
- **Requires a user-owned Claude and Codex environment** (your own authenticated CLIs); no bundled model access.
- **Not a SaaS.** There is no hosted service, account system, or public API.
- **Not for broad write access.** The design intentionally bounds writes to small declared target sets; it is not a general-purpose autonomous coding agent.

## Quick start

Prerequisites: Windows, Python 3, a configured Octo deployment, and locally authenticated `claude` and `codex` CLIs. Runtime configuration lives in a local `.env` (never committed).

```powershell
# start the bridge in the foreground
.\start_bridge.cmd

# optional: install as a Windows scheduled task (starts at logon)
.\install_bridge_task.cmd

# optional: local read-only dashboard at http://127.0.0.1:8765/
.\start_dashboard.cmd

# graceful stop
.\stop_bridge.cmd
```

Sanity checks:

```powershell
python -B -m py_compile bridge.py
python smoke_exec_start.py
python smoke_task_loop.py
```

## Repository hygiene

This repository contains source code only. Runtime files must never be committed:

- `.env` and any token, key, or credential material
- `logs/` and `runtime/` state
- `workbench/` history (tasks, dispatches, executions, evidence, retros, projects, tmp)
- caches (`__pycache__/`, `.codegraph/`), archives (`*.zip`), and patch files

`.gitignore` enforces all of the above. If you fork or copy this project, audit `git ls-files` before pushing anywhere.
