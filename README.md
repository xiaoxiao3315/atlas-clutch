# Octo-Hermes Bridge

Local bridge for Octo messages and Hermes consultation mode.

## Start

Double-click `start_bridge.cmd`, or run:

```powershell
.\start_bridge.cmd
```

The script starts `python bridge.py` from this folder and creates `logs\` if needed.

## Logs

Runtime events are written to:

```text
logs/bridge.log
```

The log records startup, registration, inbound message sequence, outbound route, and errors. It does not log `.env` contents or the bot token.

## Local Commands

- `/status` returns registration state, uptime, `last_seq`, dedupe cache size, log path, and safety boundary.
- `/help` returns bridge usage notes.

## Consultation Mode Boundary

- The bridge does not run commands.
- The bridge does not modify, delete, commit, or publish files.
- Execution-like requests are converted into a work order.
- Work orders include target, scope, execution boundary, acceptance criteria, and risks.
- Without verifiable evidence, the bridge must not claim completion.

## Checks

```powershell
python -m py_compile bridge.py
python smoke_consultation.py
```
