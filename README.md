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
- Reports are sanitized before saving common sensitive values such as bot tokens, API keys, authorization headers, cookie headers, passwords, api_key fields, and secret fields.

## Manual Dispatch Queue

OHB-DISPATCH-012 adds a manual dispatch queue under:

```text
workbench/dispatches/
```

Dispatch ids use this shape:

```text
DISPATCH-YYYYMMDD-HHMMSS
```

Dispatch records are local runtime state and are ignored by git. They only record a manual copy / manual return workflow. They do not call Codex or Kiro, do not run commands, and do not modify user project files.

Recommended flow in Octo:

```text
/task new 检查 Kiro 反代当前状态
/dispatch create <task_id> codex --with-context
/dispatch package <dispatch_id>
```

Copy the package manually to Codex or Kiro. After the executor returns a report:

```text
/dispatch mark <dispatch_id> sent 手工复制给 Codex
/dispatch receive <dispatch_id>
<paste Codex/Kiro return report>
/dispatch qa <dispatch_id>
/task review <task_id>
/dispatch link-review <dispatch_id>
/task decide <task_id> pass 验收通过
/dispatch close <dispatch_id>
/task close <task_id>
```

Useful commands:

```text
/dispatch help
/dispatch create <task_id> codex|kiro [--with-context]
/dispatch list
/dispatch list --status sent
/dispatch show <dispatch_id>
/dispatch package <dispatch_id>
/dispatch mark <dispatch_id> sent <note>
/dispatch receive <dispatch_id>
/dispatch qa <dispatch_id>
/dispatch link-review <dispatch_id>
/dispatch dashboard
/dispatch stale
/dispatch cancel <dispatch_id> <note>
/dispatch fail <dispatch_id> <note>
/dispatch close <dispatch_id>
```

`/task show`, `/task next`, `/project brief`, `/project dashboard`, and `/status` include dispatch status. Stale dispatches are `sent` dispatches older than 24 hours without a return record.

Security notes:

- Dispatch commands write only `workbench/dispatches/` and may create `workbench/context_packs/` when `--with-context` is requested.
- `/dispatch receive` sanitizes pasted reports and also syncs the report into the task ledger.
- Dispatch records keep `external_execution_enabled: false` and `runtime_injection_enabled: false`.
- Do not paste `.env`, tokens, cookies, passwords, API keys, or secrets into reports.

## Semi-Auto Execution

OHB-EXEC-016 adds a semi-auto execution session ledger under:

```text
workbench/executions/
```

Execution ids use this shape:

```text
EXEC-YYYYMMDD-HHMMSS
```

This layer wraps an existing dispatch package into an execution session. It does not call Codex/Kiro, does not run the prompt, does not inject runtime content, does not read `.env`, and does not pass secrets. It only records whether the user prepared, opened, copied, returned, cancelled, or failed a manual executor handoff.

Recommended flow:

```text
/dispatch create <task_id> codex --with-context
/exec prepare <dispatch_id>
/exec package <exec_id>
manually copy the package to Codex
/exec mark <exec_id> copied copied into Codex window

Codex returns a report:
/exec receive <exec_id>
<paste report>
/dispatch qa <dispatch_id>
/task review <task_id>
/task decide <task_id> pass|needs_evidence|blocked|cancelled <note>
```

Commands:

```text
/exec help
/exec prepare <dispatch_id>
/exec package <exec_id>
/exec mark <exec_id> copied <note>
/exec mark <exec_id> opened <note>
/exec receive <exec_id>
/exec cancel <exec_id> <note>
/exec fail <exec_id> <note>
/exec show <exec_id>
/exec list
/exec dashboard
/exec stale
```

Safety notes:

- `human_confirm_required` is always `true`.
- `external_execution_enabled` is always `false`.
- `auto_execute_enabled` is always `false`.
- `/exec prepare` writes only `workbench/executions/<exec_id>.md`.
- `/exec package` only displays a copy payload.
- `/exec mark copied` can sync the dispatch status to `sent`, but it still does not send anything.
- `/exec receive` syncs the pasted report through the existing `/dispatch receive` and `/task report` logic.
- The bridge still waits for `/dispatch qa`, `/task review`, and the user's `/task decide`.

## Real Project Pilot

OHB-PILOT-013 adds a minimal real-project pilot record under:

```text
workbench/pilots/
```

Pilot ids use this shape:

```text
PILOT-YYYYMMDD-HHMMSS
```

The pilot layer is only an operating record. It tracks whether the existing Workbench loop is useful in real work. It does not call Codex/Kiro, does not run commands, does not change Octo Docker, and does not modify Hermes or external project files.

Recommended real trial flow:

```text
/project new kiro-proxy Kiro 反代项目
/pilot start kiro-proxy Kiro 反代真实试运行
/task new 检查 Kiro 反代当前状态 --project kiro-proxy
/context pack task <task_id>
/dispatch create <task_id> codex --with-context
/dispatch package <dispatch_id>
人工复制给 Codex/Kiro
/dispatch mark <dispatch_id> sent 手工复制完成
/dispatch receive <dispatch_id>
<paste return report>
/dispatch qa <dispatch_id>
/task review <task_id>
/task decide <task_id> needs_evidence 需要补真实上游请求证据
/pilot add-task <pilot_id> <task_id>
/pilot add-dispatch <pilot_id> <dispatch_id>
/pilot note <pilot_id> 手工复制仍然需要来回切窗口
/pilot metrics <pilot_id>
/pilot complete <pilot_id> 本轮试运行结束
```

Pilot commands:

```text
/pilot help
/pilot start <project_id> <title>
/pilot list
/pilot show <pilot_id>
/pilot add-task <pilot_id> <task_id>
/pilot add-dispatch <pilot_id> <dispatch_id>
/pilot note <pilot_id> <single-line note>
/pilot metrics <pilot_id>
/pilot complete <pilot_id> <note>
/pilot dashboard
```

Pilot metrics include task count, dispatch count, returned reports, QA pass count, needs-evidence count, closed count, evidence gaps, context pack count, manual copy count, a rough time-saved estimate, and main friction.

Use pilot results to decide whether to run another real project trial, pay down Octo UI live debt, or improve one painful command. Do not use pilot results as permission to add automatic execution.

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

## Learning Loop

OHB-LEARN-009 adds a controlled local learning proposal layer under:

```text
workbench/learning/
  candidates/
  proposals/
  registry/
  rejected/
  deferred/
  packages/
  logs/
```

This is not model training. It does not automatically modify Hermes, does not write Memory, does not modify SkillRepo, does not change system prompts, and does not apply behavior changes to any project code. It only turns approved retros into reviewable learning proposals, then stores approved proposals in a local registry. Application is disabled:

```text
application_enabled: false
```

Learning workflow:

```text
/retro dashboard
/learn scan retro <task_id>
/learn propose retro <task_id>
/learn list
/learn show <learn_id>
/learn review <learn_id>
/learn approve <learn_id> 批准进入本地 learning registry，但不应用
/learn registry
/learn package <learn_id>
/learn dashboard
```

Manual proposal:

```text
/learn propose manual <标题>
/learn review <learn_id>
/learn defer <learn_id> 等待更多证据
```

Learning commands:

```text
/learn help
/learn scan retro <task_id>
/learn propose retro <task_id>
/learn propose manual <标题>
/learn list
/learn list --status <candidate|proposed|approved|rejected|deferred|packaged>
/learn show <learn_id>
/learn review <learn_id>
/learn approve <learn_id> <说明>
/learn reject <learn_id> <说明>
/learn defer <learn_id> <说明>
/learn package <learn_id>
/learn dashboard
/learn registry
/learn status
```

Every proposal must keep evidence, an acceptance test, rollback plan, risks, approval record, application status, and a `Do Not Auto-Apply` section. A package is only a copyable manual application package for a future human-controlled phase.

## Apply And Playbook

OHB-APPLY-010 adds a Workbench-only apply layer under:

```text
workbench/applications/
workbench/playbooks/
workbench/playbooks/projects/
```

Apply means writing an approved learning into the local Workbench Playbook reference layer. It does not apply to Hermes, Memory, SkillRepo, system prompts, project code, or runtime prompts. It does not run commands, call Codex/Kiro, or perform model training.

The Playbook is a readable reference layer, not a runtime layer:

```text
runtime_injection_enabled: false
external_application_enabled: false
```

Standard flow:

```text
/learn registry
/apply plan <learn_id> global
/apply show <apply_id>
/apply enact <apply_id> Confirm Workbench Playbook only; do not modify Hermes/Memory/SkillRepo
/playbook show global
/apply dashboard
/apply revert <apply_id> Revert this Playbook entry by appending a Revert Note only
/learn status
```

Project Playbook flow:

```text
/apply plan <learn_id> project <project_id>
/apply show <apply_id>
/apply enact <apply_id> Confirm project Playbook reference only
/playbook show project <project_id>
```

Apply commands:

```text
/apply help
/apply plan <learn_id> global
/apply plan <learn_id> project <project_id>
/apply show <apply_id>
/apply list
/apply list --status <planned|applied|reverted|cancelled>
/apply enact <apply_id> <说明>
/apply revert <apply_id> <说明>
/apply cancel <apply_id> <说明>
/apply dashboard
```

Playbook commands:

```text
/playbook help
/playbook show global
/playbook show project <project_id>
/playbook list
/playbook search <关键词>
```

Apply plans are stored in `workbench/applications/<apply_id>.md`. Enact appends one entry to a target playbook under `workbench/playbooks/`. Revert does not delete history; it appends a Revert Note and updates the learning registry to `reverted_from_workbench_playbook`.

APPLY-010 never changes real Hermes behavior. A future stage that changes Hermes, Memory, SkillRepo, prompts, or project code must be separately authorized and reviewed.

## Context Pack

OHB-CONTEXT-011 adds a Workbench context packaging layer under:

```text
workbench/context_packs/
```

Context Pack reads Workbench materials only and writes context pack files only. It does not read `.env`, does not modify Hermes, Memory, SkillRepo, system prompts, or project code, does not perform runtime injection, and does not call Codex/Kiro automatically. Generated content is copy-only for a human to paste into an executor.

```text
runtime_injection_enabled: false
external_execution_enabled: false
```

Task context flow:

```text
/context help
/context pack task <task_id>
/context show <context_id>
/context handoff <task_id> codex
/playbook advise task <task_id>
/task handoff <task_id> codex --with-context
```

Project context flow:

```text
/context pack project <project_id>
/playbook advise project <project_id>
/context list
/context archive <context_id>
```

Context commands:

```text
/context help
/context pack task <task_id>
/context pack project <project_id>
/context show <context_id>
/context list
/context archive <context_id>
/context handoff <task_id> codex|kiro
```

Playbook advisory commands:

```text
/playbook advise task <task_id>
/playbook advise project <project_id>
```

`/task handoff <task_id> codex|kiro --with-context` appends a Context Pack summary and Playbook Advisory to the normal manual handoff. It still does not send anything automatically.

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
- `/learn help` returns controlled learning loop commands.
- `/learn dashboard` returns proposal, registry, and not-applied counts.
- `/apply help` returns Workbench-only apply commands.
- `/apply dashboard` returns apply and playbook counts.
- `/playbook help` returns local Playbook commands.
- `/context help` returns Context Pack commands.
- `/context pack task <task_id>` writes a Workbench context pack.
- `/playbook advise task <task_id>` searches only `workbench/playbooks`.
- `/exec help` returns semi-auto execution session commands.
- `/collect help` returns read-only whitelist evidence collection commands.
- `/collect snapshot <task_id> <octo-bridge|kiro-gateway>` writes a local collection record.
- `/collect smoke <task_id> octo-bridge` runs the fixed octo-bridge smoke allowlist.
- `/task handoff <task_id> codex|kiro` returns a copyable execution package.
- `/task handoff <task_id> codex|kiro --with-context` appends context and advisory.
- `/task qa <task_id>` checks whether the return report has enough evidence.
- `/task next <task_id>` returns the next recommended action for the current status.
- `/daily brief` returns today's active task summary.
- `/template wo` returns the work order template.
- `/template report` returns the Codex/Kiro return report template.
- `/template review` returns the Atlas review checklist.

## Auto Evidence Intake

`/task report <task_id>` and `/dispatch receive <dispatch_id>` run a local evidence intake parser before writing the task evidence ledger.

The parser only archives observed evidence. It does not mark evidence as verified, does not decide pass, and does not call Codex/Kiro. The user still needs:

```text
/evidence mark <task_id> <evidence_id> verified <reason>
/task review <task_id>
/task decide <task_id> pass|needs_evidence|blocked|cancelled <reason>
```

Read-only reports are valid when they clearly say no files were modified:

```text
Modified files: none
Modified files: N/A
修改文件：无
无修改文件
```

For read-only validation, missing modified files is treated as `not_applicable` instead of a blocking gap when the report includes commands/checks, test or API results, logs/request ids, unverified items, and risks.

Sensitive scan results with zero hits are leak-check evidence, not leaks:

```text
authorization header: 0 hits
cookie header: 0 hits
api key prefix: 0 hits
bot token prefix: no hits
```

Actual sensitive-looking values are still redacted and flagged, including bearer authorization headers, cookies with values, password/api_key/secret fields, bot token prefixes, and long API-key prefixes. Redaction labels avoid the original token prefixes to reduce scan false positives.

Parser-only preview:

```text
/evidence intake <task_id>
<paste report>
```

Atlas Review still uses verified evidence as the stronger signal. Observed evidence from auto intake is useful for triage, but it is not proof of completion until a human marks it verified.

## Auto Evidence Collection

OHB-COLLECT-015 adds a read-only evidence collection layer under:

```text
workbench/collections/
```

Collection ids use this shape:

```text
COLLECT-YYYYMMDD-HHMMSS
```

This layer is for standardizing evidence packets. It does not accept shell commands from the user, does not call Codex/Kiro, does not restart services, does not read `.env`, and does not decide whether a task passed. A collection is observed evidence only until the user and Atlas review it.

Supported profiles:

```text
octo-bridge
kiro-gateway
```

`octo-bridge` is fixed to this repository. It can collect git state, bridge log tail, runtime heartbeat, Workbench summaries, and the fixed smoke allowlist. `kiro-gateway` is fixed to `E:\ai\kiro-gateway-mvp`. It can collect read-only git state, `logs/gateway.log`, `logs/acp_raw.log`, and a process/port summary. It does not send a real chat request and does not read `.env`.

Commands:

```text
/collect help
/collect profiles
/collect snapshot <task_id> <octo-bridge|kiro-gateway>
/collect smoke <task_id> octo-bridge
/collect list
/collect show <collection_id>
/collect report <collection_id>
/collect attach <task_id> <collection_id>
```

Recommended use:

```text
/task new Check Kiro proxy current state --project kiro_proxy
/collect snapshot <task_id> kiro-gateway
/collect show <collection_id>
/collect report <collection_id>
/collect attach <task_id> <collection_id>
/task qa <task_id>
/task review <task_id>
/task decide <task_id> needs_evidence <reason>
```

For the bridge itself:

```text
/collect smoke <task_id> octo-bridge
```

Collection files include:

- scope and fixed profile
- commands run by the whitelist
- git evidence
- smoke evidence
- log evidence
- runtime evidence
- Workbench evidence
- sensitive scan summary
- observed facts
- missing evidence
- risks
- standard return report
- a reminder that observed evidence is not verified completion

Safety rules:

- Fixed profile roots only; user-supplied paths are rejected.
- No arbitrary command input is accepted.
- Commands use static allowlists, no shell command string execution, timeouts, output limits, and sanitization.
- `.env`, token, cookie, authorization, password, api_key, and secret values must not be printed or saved.
- `/collect attach` writes only Workbench task/evidence/collection records.
- `/task qa`, `/task review`, `/project brief`, `/pilot metrics`, and `/status` may summarize collection counts, but collection evidence is still not a pass decision.

## Web Workbench Roadmap

OHB-WEB-017A is a read-only research and design stage for a visual Atlas Workbench. It does not implement a dashboard, does not create HTML UI, does not modify `octo-web`, does not modify `octo-server`, and does not change Docker deployment.

Roadmap:

- 017A: read-only research and design. Output: `workbench/designs/OHB-WEB-017A-web-workbench-design.md`.
- 017B: Bridge Local Dashboard MVP. Local read-only dashboard bound to localhost; still no `octo-web` changes.
- 017C: dashboard plus Octo Bot deep links / copy package helpers.
- 017D: evaluate whether an `octo-web` native Workbench page is justified.

Rules:

- 017A is design only.
- 017B must remain local and read-only.
- 017B must not modify `octo-web`.
- Only 017D may consider an `octo-web` native page.
- Official OCTO Web is an independent frontend. Do not change it before a dedicated source inspection and API boundary review.
- Do not expose Workbench data publicly.
- Do not read `.env`, tokens, cookies, authorization headers, passwords, api keys, or secrets.

Useful design commands:

```powershell
python web_workbench_design_probe.py
python smoke_web_design.py
```

## Bridge Local Dashboard MVP

OHB-WEB-017B adds a local read-only Atlas Workbench dashboard inside this bridge repo.

Start it from this directory:

```powershell
start_dashboard.cmd
```

Then open:

```text
http://127.0.0.1:8765/
```

You can also run it directly:

```powershell
python dashboard_server.py --host 127.0.0.1 --port 8765
```

Safety boundary:

- Dashboard mode is `read_only_dashboard`.
- It binds only to `127.0.0.1`.
- Do not expose it to a public network.
- It does not modify `octo-web`.
- It does not modify `octo-server`.
- It does not modify `octo-deployment`.
- It does not change Docker.
- It does not read `.env`.
- It does not show full logs.
- It does not show full Workbench file bodies.
- It does not execute commands.
- It does not call Codex/Kiro.
- It is not an automatic execution system.

Read-only pages:

- `/` shows the dashboard HTML.
- `/api/summary` returns read-only summary JSON.
- `/api/projects` returns read-only project summaries.
- `/api/tasks` returns read-only task summaries.
- `/api/dispatches` returns read-only dispatch summaries.

Stop it with `Ctrl+C` in the dashboard terminal.

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
Select-String -Path .\logs\bridge.log -Pattern '<bot-token-prefix>' -SimpleMatch
```

## Checks

```powershell
python -m py_compile bridge.py
python -m py_compile smoke_consultation.py
python -m py_compile smoke_project.py
python -m py_compile smoke_evidence.py
python -m py_compile smoke_retro.py
python -m py_compile smoke_learn.py
python -m py_compile smoke_apply.py
python -m py_compile smoke_context.py
python -m py_compile smoke_collect.py
python -m py_compile smoke_exec.py
python -m py_compile web_workbench_design_probe.py
python -m py_compile smoke_web_design.py
python -m py_compile dashboard_server.py
python -m py_compile smoke_dashboard.py
python smoke_consultation.py
python smoke_runtime.py
python smoke_workflow.py
python smoke_task_loop.py
python smoke_handoff.py
python smoke_project.py
python smoke_evidence.py
python smoke_retro.py
python smoke_learn.py
python smoke_apply.py
python smoke_context.py
python smoke_dispatch.py
python smoke_pilot.py
python smoke_auto_evidence.py
python smoke_collect.py
python smoke_exec.py
python smoke_web_design.py
python smoke_dashboard.py
```
