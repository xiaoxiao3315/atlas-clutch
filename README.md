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
python smoke_consultation.py
python smoke_runtime.py
python smoke_workflow.py
```
