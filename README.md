# Atlas Clutch

**The local AI workforce control plane.**

*Claude writes. Codex reviews. Safety gates decide.*

Atlas Clutch turns local AI agents into a governed workforce: Claude writes files, Codex reviews the result, and the bridge enforces scope, evidence, validation, and closure.

## Why this exists

Most agent automation fails for one reason: it trusts what the model says it did. An ungoverned agent will touch files you never mentioned, report success without proof, and happily run `git push` on its own initiative. Atlas Clutch treats every write as a contract — the task declares exactly which files may change, Claude executes against that declaration, Codex reviews independently, and the gates verify reality on disk before anything is allowed to close.

## Architecture

```text
User / Boss
    │  natural-language task
    ▼
Atlas Clutch Bridge ── parses targets, acceptance criteria, validation requirements
    │
    ▼
Claude Writer ── workspace-write execution, declared targets only
    │
    ▼
Codex Reviewer ── independent pass_candidate / concerns verdict
    │
    ▼
Safety Gates ── target fidelity · acceptance fidelity · validation evidence · worktree ownership
    │
    ▼
auto-close ─────────── or ─────────── needs_human_review
```

## Core capabilities

- Natural-language owner-write tasks
- CJK / Chinese target parsing (`只允许创建或更新：`)
- Single-file writes
- Bounded file packs (2–5 declared files)
- Feature-slice automation
- Acceptance fidelity: exact-one-line / exists / non-empty / contains
- Required validation evidence (allowlisted commands, returncode 0)
- Post-run target fidelity against the real worktree
- Worktree ownership guard — pre-existing unrelated dirt blocks auto-close
- No `git add` / `git commit` / `git push` from the writer, ever
- Deploy commands forbidden in tasks and validation blocks

## Safety model

1. **Declared targets only.** A task must list every file it may create or update. Anything outside that list is out of scope by definition.
2. **Workspace-relative paths only.** Absolute paths, drive letters, traversal (`../`), environment-variable expansion, and disallowed extensions are refused at parse time.
3. **Post-run diff is checked.** After the run, `git status` is compared against the declaration. Changes outside it — including dirt left by another actor before the run — block auto-close.
4. **Acceptance is re-evaluated from disk.** Criteria (`exists`, `non-empty`, `contains`, exact-one-line) are verified against the actual files, not the model's report. A missing or wrong file fails the gate; the bridge never fakes a pass.
5. **Required validation must return 0.** Declared validation commands come from a strict allowlist — no shell chaining, pipes, redirection, or absolute paths — and each must produce returncode-0 evidence.
6. **Codex review is required.** An independent reviewer must return `pass_candidate`.
7. **Any failing gate produces `needs_human_review`.** There is no silent pass.

## Examples

All examples write only under `workbench/tmp/`, which is local runtime state and never committed.

**Single file, Chinese exact-one-line acceptance:**

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

## What it is not

- **Not SaaS.** No hosted service, no accounts, no public API.
- **Not a general unbounded coding agent.** Writes are bounded to small declared target sets by design.
- **Not a deployment bot.** Deploy commands are rejected everywhere.
- **Not a secret manager.** It never reads `.env` and refuses to print or store credential material.
- **Not a code publisher.** It will never `git add`, `commit`, or `push` on its own.

## Quick start

Prerequisites: Windows, Python 3, a compatible Octo deployment, and locally authenticated `claude` and `codex` CLIs.

1. Clone this repository.
2. Create your local configuration in `.env` (never committed; see repository hygiene below).
3. Start your Octo deployment.
4. Start the bridge:

   ```powershell
   .\start_bridge.cmd
   ```

5. Open your local Octo UI and send a task.

Optional: install as a Windows scheduled task with `.\install_bridge_task.cmd`, run the local read-only dashboard with `.\start_dashboard.cmd`, and stop gracefully with `.\stop_bridge.cmd`.

Sanity checks:

```powershell
python -B -m py_compile bridge.py
python smoke_exec_start.py
python smoke_task_loop.py
```

## Repository hygiene

This repository contains source code only. The following must never be committed:

- `.env`, tokens, keys, or any credential material
- runtime state and `logs/`
- `workbench/` history (tasks, dispatches, executions, evidence, retros, projects, tmp)
- archives (`*.zip`) and patch files (`*.patch`)
- caches (`__pycache__/`, `.codegraph/`)

`.gitignore` enforces all of the above. If you fork this project, audit `git ls-files` before pushing anywhere.

## Status

- Local-first: designed to run on the owner's machine.
- Experimental but functional: the gate pipeline is exercised by smoke tests and synthetic checks.
- Private-first recommended: keep your copy in a private repository.
- Built for owner-controlled workstations, not shared or hosted environments.

Runtime internals retain their original Octo / Hermes / Atlas Workbench identifiers for compatibility with existing deployments; Atlas Clutch is the product name.
