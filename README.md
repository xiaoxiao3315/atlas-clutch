# Octo-Hermes Bridge

Local bridge for Octo messages and Hermes consultation mode.

## Current Boundary

- Mode is still consultation / dispatch only.
- The bridge does not run commands.
- The bridge does not call Codex, Kiro, or OpenClaw.
- The bridge does not modify, delete, commit, deploy, or publish files.
- Execution-like requests are converted into a work order with target, scope, execution boundary, acceptance criteria, and risks.

## Atlas Workbench

Atlas is the Octo entrypoint for turning goals into a repeatable work loop:

```text
Goal -> Work order -> Codex/Kiro return report -> Atlas review -> User decision
```

User sends a goal:

```text
生成工作单：检查 Kiro 反代当前状态
```

Atlas generates a work order with:

- target
- scope
- execution boundary
- acceptance criteria
- risks
- return evidence requirements

Codex or Kiro returns evidence using:

```text
/template report
```

The return report should include modified files, commands, test results, evidence, unresolved risks, and questions for Atlas.

Atlas reviews the return report:

```text
审查这份 Codex 返回报告：<paste report>
```

Review output must distinguish:

- verified
- unverified
- risk
- next-step decision

User makes the final confirmation:

- pass and continue
- request more evidence
- continue with the next work order
- roll back
- pause

No evidence means no completion claim.

## Task Ledger

OHB-LOOP-004 adds a local task ledger under:

```text
workbench/
  tasks/
  projects/
  evidence/
  retros/
  decisions/
  daily/
  archive/
```

The `workbench/` directory is local runtime state and is ignored by git.

Task ids use this shape:

```text
OHB-YYYYMMDD-HHMMSS
```

Example loop in Octo:

```text
/task new 检查 Kiro 反代当前状态
```

Atlas returns a `task_id` and a work order. The user copies that work order to Codex or Kiro for manual execution.

Generate a standard handoff package:

```text
/task handoff <task_id> codex
```

or:

```text
/task handoff <task_id> kiro
```

Copy the handoff package to the selected execution tool manually. The bridge does not call Codex or Kiro.

After Codex or Kiro returns evidence, paste it back:

```text
/task report <task_id>
<粘贴 Codex/Kiro 返回报告>
```

Run return-report quality checks:

```text
/task qa <task_id>
```

Ask Atlas to review:

```text
/task review <task_id>
```

Atlas review must distinguish verified, unverified, risk, missing evidence, and next step.

Then record the user decision:

```text
/task decide <task_id> needs_evidence 需要补 Octo UI live 回归证据
```

or:

```text
/task decide <task_id> pass 验收通过
```

Close only after `passed` or `cancelled`:

```text
/task close <task_id>
```

Daily brief:

```text
/daily brief
```

Standard manual handoff flow:

```text
/task new 检查 Kiro 反代当前状态
/task handoff <task_id> codex
复制交接包给 Codex
Codex 返回报告
/task report <task_id>
<粘贴报告>
/task qa <task_id>
/task review <task_id>
/task decide <task_id> pass 验收通过
/task close <task_id>
```

Ledger commands:

```text
/task help
/task new <标题>
/task list
/task show <task_id>
/task handoff <task_id> codex|kiro
/task report <task_id>
/task qa <task_id>
/task review <task_id>
/task next <task_id>
/task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>
/task close <task_id>
/daily brief
```

Security notes:

- Task commands only write inside `workbench/`.
- Task commands do not execute shell commands.
- Task commands do not call Codex/Kiro/OpenClaw.
- Task commands do not read `.env`.
- Reports are sanitized before saving common sensitive values such as `bf_` tokens, `sk-` keys, `Authorization:`, `Cookie:`, `password:`, `api_key:`, and `secret:`.

## Project Index

OHB-PROJECT-006 adds a lightweight local project layer under:

```text
workbench/projects/
```

Project files are local runtime state and are ignored by git. A project is only an index and dashboard for tasks. It is not an Agent self-training system, it does not write Memory or SkillRepo, and it does not execute Codex/Kiro automatically.

Project ids must use lowercase letters, numbers, `-`, or `_` only. Path-like ids such as `../bad`, `bad\path`, or ids containing Windows filename metacharacters are rejected.

Project workflow in Octo:

```text
/project new kiro_proxy Kiro 反代检查
/task new 检查 Kiro 反代当前状态 --project kiro_proxy
/project tasks kiro_proxy
/project brief kiro_proxy
/project dashboard
/daily brief
```

Attach an existing task:

```text
/project attach kiro_proxy <task_id>
```

If the task already belongs to another project, Atlas returns an explicit refusal and does not overwrite the existing `project_id`.

Project commands:

```text
/project help
/project new <project_id> <项目名称>
/project list
/project show <project_id>
/project set <project_id> status <active|paused|archived>
/project set <project_id> priority <P0|P1|P2|P3>
/project note <project_id> <单行备注>
/project attach <project_id> <task_id>
/project tasks <project_id>
/project brief <project_id>
/project dashboard
```

Notes, titles, reports, task files, and project files are sanitized before saving common sensitive values. The project layer only writes inside `workbench/`.

## Evidence Chain

OHB-EVIDENCE-007 adds the evidence chain foundation under:

```text
workbench/evidence/
```

Each task can have:

```text
workbench/evidence/<task_id>.md
```

The evidence layer distinguishes:

- `claimed`: an executor or user says something passed, but evidence is thin.
- `observed`: the user pasted logs, command output, screenshots, paths, or other observable material.
- `verified`: Atlas has been explicitly told to mark a specific evidence item as supporting acceptance.
- `missing`: evidence is absent or still insufficient.

Report is not verified by itself. A smoke result is not the same as live Octo UI acceptance. If live validation is skipped, the task keeps a risk marker and the review should say local passed / live pending.

Evidence workflow:

```text
/task new 检查 Kiro 反代当前状态 --project kiro-proxy
/task handoff <task_id> codex
/task report <task_id>
<粘贴执行端报告>
/evidence add <task_id> smoke
<粘贴 smoke、日志、截图或命令输出证据>
/evidence list <task_id>
/evidence gaps <task_id>
/task qa <task_id>
/task review <task_id>
/task decide <task_id> needs_evidence 需要补真实上游请求证据
/project brief kiro-proxy
/project dashboard
```

Evidence commands:

```text
/evidence help
/evidence add <task_id> <file|command|log|screenshot|ui|live|smoke|report|decision|other>
/evidence list <task_id>
/evidence show <task_id> <evidence_id>
/evidence mark <task_id> <evidence_id> <verified|partial|rejected> <说明>
/evidence gaps <task_id>
```

User can force-record `pass`, but if evidence gaps remain, the task keeps `evidence_gap_risk: true`. Missing evidence is never auto-marked as verified.

This stage does not do retro, Agent self-training, Memory writes, SkillRepo edits, model training, or automatic Codex/Kiro execution.

## Retro

OHB-RETRO-008 adds task and project retrospective notes under:

```text
workbench/retros/
```

Each task can have one retro file:

```text
workbench/retros/<task_id>.md
```

Retro is only experience capture inside `workbench`. It is not Agent self-training, does not automatically modify Hermes, does not write Memory, does not modify SkillRepo, does not change system prompts, and does not generate executable patches. Agent self-training is reserved for OHB-LEARN-009.

Retro workflow:

```text
/task new 检查 Kiro 反代当前状态 --project kiro-proxy
/task handoff <task_id> codex
/task report <task_id>
<粘贴执行端报告>
/evidence add <task_id> smoke
<粘贴证据>
/evidence gaps <task_id>
/task qa <task_id>
/task review <task_id>
/task decide <task_id> needs_evidence 需要补真实上游请求证据
/task close <task_id>
/retro create <task_id>
/retro show <task_id>
/retro approve <task_id> 复盘确认，只写入 workbench，不写 Memory
/project brief kiro-proxy
/retro project kiro-proxy
/retro dashboard
```

Retro commands:

```text
/retro help
/retro create <task_id>
/retro show <task_id>
/retro list
/retro list --project <project_id>
/retro approve <task_id> <说明>
/retro archive <task_id>
/retro project <project_id>
/retro dashboard
```

If live validation was skipped or evidence gaps remain, the retro keeps those risks. A `passed` task can still produce a retro with unresolved evidence gaps; retro never rewrites missing evidence as verified.

## Manual Start

Double-click `start_bridge.cmd`, or run:

```powershell
.\start_bridge.cmd
```

The script starts `python bridge.py` from this folder, creates `logs\` and `runtime\` if needed, and marks the startup method as `manual`.

## Background Task

Install the Windows Scheduled Task:

```powershell
.\install_bridge_task.cmd
```

Task name:

```text
OctoHermesBridge
```

The task starts at current-user logon and calls:

```text
start_bridge.cmd task
```

Uninstall the task:

```powershell
.\uninstall_bridge_task.cmd
```

Check task status:

```powershell
.\status_bridge_task.cmd
```

## Safe Stop

Preferred graceful stop:

```powershell
.\stop_bridge.cmd
```

This writes `runtime/stop.request`; the bridge exits on its next poll cycle and releases its lock.

Other options:

- Manual foreground run: press `Ctrl+C` in the bridge window.
- Scheduled task run: run `uninstall_bridge_task.cmd` to stop and remove the task.
- Last resort: run `schtasks /End /TN OctoHermesBridge`.

## Single Instance

The bridge uses:

```text
runtime/bridge.lock
runtime/bridge.pid
```

If another live `bridge.py` process already holds the lock, a new start exits without creating a second bridge. If the previous process crashed, stale lock and pid files are removed during the next start.

## Logs

Runtime events are written to:

```text
logs/bridge.log
```

The log records startup, registration, inbound message sequence, outbound route, and errors. It does not log `.env` contents or the bot token.

Log rotation is simple: if `logs/bridge.log` is larger than 5 MB at startup, it is moved to:

```text
logs/bridge.log.1
```

## Heartbeat

Runtime health is written to:

```text
runtime/heartbeat.json
```

It includes `run_id`, `pid`, `started_at`, `updated_at`, `registered`, `robot_id_masked`, `owner_channel_id`, `last_seq`, `processed_count`, `mode`, startup method, lock status, and last error.

## Local Commands

- `/status` returns registration state, run id, pid, startup method, heartbeat time, lock state, `last_seq`, dedupe cache size, log path, heartbeat path, and safety boundary.
- `/help` returns bridge usage notes.
- `/workflow` returns the Atlas workbench loop.
- `/project help` returns project index commands.
- `/project dashboard` returns the cross-project dashboard.
- `/task help` returns task ledger commands.
- `/task new <标题> --project <project_id>` creates a task and attaches it to a project.
- `/evidence help` returns evidence chain commands.
- `/evidence gaps <task_id>` returns claimed / observed / verified / missing gaps.
- `/retro help` returns task and project retrospective commands.
- `/retro dashboard` returns the cross-project retro dashboard.
- `/task handoff <task_id> codex|kiro` returns a copyable execution package.
- `/task qa <task_id>` checks whether the return report has enough evidence.
- `/task next <task_id>` returns the next recommended action for the current status.
- `/daily brief` returns today's active task summary.
- `/template wo` returns the work order template.
- `/template report` returns the Codex/Kiro return report template.
- `/template review` returns the Atlas review checklist.

Recommended Octo prompts:

```text
生成工作单：检查 Kiro 反代当前状态
审查这份 Codex 返回报告：<paste report>
判断下一步优先级：<paste options>
把这个报错整理成 Kiro/Codex 可执行任务：<paste error>
根据以下证据判断是否完成：<paste evidence>
```

## Troubleshooting

- Duplicate start exits: check `runtime/heartbeat.json` and `runtime/bridge.lock`.
- Bridge does not start after login: run `status_bridge_task.cmd`, then inspect `logs/bridge.log`.
- Stale lock after a hard crash: start the bridge again; it should remove stale lock files if the old pid is not alive.
- Invalid token or `.env` errors: fix `.env` locally. Do not paste or commit token values.
- Confirm no token in logs:

```powershell
Select-String -Path .\logs\bridge.log -Pattern 'bf_' -SimpleMatch
```

## Checks

```powershell
python -m py_compile bridge.py
python -m py_compile smoke_consultation.py
python -m py_compile smoke_project.py
python -m py_compile smoke_evidence.py
python -m py_compile smoke_retro.py
python smoke_consultation.py
python smoke_runtime.py
python smoke_workflow.py
python smoke_task_loop.py
python smoke_handoff.py
python smoke_project.py
python smoke_evidence.py
python smoke_retro.py
```
