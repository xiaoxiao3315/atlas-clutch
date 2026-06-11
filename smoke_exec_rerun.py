"""Smoke: /exec rerun recovers a failed execution through the sealed start path.

Covers: failed (timeout_with_payload) exec -> /exec rerun -> new exec runs the
full gated auto-close; recovered_from / superseded_by stamped on both records;
rerun refuses healthy executions and already-superseded executions.
Runs entirely in a temp workbench; no real ledger writes, no external calls.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import bridge
from smoke_exec import (
    assert_contains,
    assert_not_contains,
    configure_temp_paths,
    context,
    extract_dispatch_id,
    extract_exec_id,
    extract_task_id,
    restore_paths,
)


def metadata_field(text: str, field: str) -> str:
    for line in text.splitlines():
        if line.startswith(f"{field}: "):
            return line[len(field) + 2 :].strip()
    return ""


def create_read_only_dispatch(ctx: dict) -> tuple[str, str]:
    task_reply, route = bridge.prepare_reply(
        "/task new Read-only inspect local state only, no code changes, do not modify files --project auto_exec",
        ctx,
    )
    assert route == "local_command"
    task_id = extract_task_id(task_reply)
    dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex --with-context", ctx)
    assert route == "local_command"
    return task_id, extract_dispatch_id(dispatch_reply)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-exec-rerun-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        original_probe = bridge.probe_codex_noninteractive
        original_runner = bridge.run_allowlisted_external_command
        original_post_run = bridge.run_allowlisted_post_run_command
        try:
            ctx = context()
            bridge.prepare_reply("/project new auto_exec Auto execution rerun smoke", ctx)

            bridge.probe_codex_noninteractive = lambda: {
                "supported": True,
                "mode": "codex_exec_read_only_stdin",
                "reason": "smoke fake non-interactive read-only stdin runner",
                "command": ["codex", "exec", "--sandbox", "read-only", "-"],
            }

            def fake_post_run(argv: list[str]) -> dict:
                if not bridge.is_allowed_post_run_command(list(argv)):
                    raise AssertionError(f"unexpected post-run command: {argv}")
                return {"returncode": 0, "stdout": "", "stderr": ""}

            def failing_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 124,
                    "stdout": "",
                    "stderr": f"runner saw payload\n{input_text[:600]}",
                    "timed_out": True,
                }

            def healthy_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": "fake runner received full stdin payload",
                    "stderr": "",
                }

            bridge.run_allowlisted_post_run_command = fake_post_run
            bridge.run_allowlisted_external_command = failing_runner

            task_id, dispatch_id = create_read_only_dispatch(ctx)
            failed_reply, route = bridge.prepare_reply(f"/exec start {dispatch_id}", ctx)
            assert route == "local_command"
            old_exec_id = extract_exec_id(failed_reply)
            assert_contains(failed_reply, "completion_state: timeout_with_payload")
            assert_contains(failed_reply, "status: failed")

            # Recover through the same gates with a healthy runner.
            bridge.run_allowlisted_external_command = healthy_runner
            rerun_reply, route = bridge.prepare_reply(f"/exec rerun {old_exec_id}", ctx)
            assert route == "local_command"
            assert_contains(rerun_reply, "Execution rerun:")
            assert_contains(rerun_reply, "recovered_from_stamped: true")
            assert_contains(rerun_reply, "status: returned")
            assert_contains(rerun_reply, "auto_decision: pass")

            old_text = (bridge.EXECUTIONS_DIR / f"{old_exec_id}.md").read_text(encoding="utf-8")
            new_exec_id = metadata_field(old_text, "superseded_by")
            if not new_exec_id or new_exec_id == old_exec_id:
                raise AssertionError(f"old exec missing superseded_by stamp: {new_exec_id!r}")
            new_text = (bridge.EXECUTIONS_DIR / f"{new_exec_id}.md").read_text(encoding="utf-8")
            if metadata_field(new_text, "recovered_from") != old_exec_id:
                raise AssertionError("new exec missing recovered_from stamp")
            assert_contains(new_text, "status: returned")
            assert_contains(new_text, "completion_state: completed")
            assert_contains((bridge.TASKS_DIR / f"{task_id}.md").read_text(encoding="utf-8"), "status: archived")

            # Refuse rerunning a healthy execution.
            refused, route = bridge.prepare_reply(f"/exec rerun {new_exec_id}", ctx)
            assert route == "local_command"
            assert_contains(refused, "exec rerun refused")
            assert_not_contains(refused, "Execution rerun:")

            # Refuse rerunning an already-superseded execution.
            superseded, route = bridge.prepare_reply(f"/exec rerun {old_exec_id}", ctx)
            assert route == "local_command"
            assert_contains(superseded, "already superseded")

            # Refuse cancelled executions. A cancelled exec means a human
            # intentionally stopped that execution, so recovery must require a
            # fresh dispatch instead of silently replaying the old one.
            cancel_task_id, cancel_dispatch_id = create_read_only_dispatch(ctx)
            cancel_prepare, route = bridge.prepare_reply(f"/exec prepare {cancel_dispatch_id}", ctx)
            assert route == "local_command"
            cancel_exec_id = extract_exec_id(cancel_prepare)
            cancelled, route = bridge.prepare_reply(f"/exec cancel {cancel_exec_id} user stopped", ctx)
            assert route == "local_command"
            assert_contains(cancelled, "status: cancelled")
            cancel_rerun, route = bridge.prepare_reply(f"/exec rerun {cancel_exec_id}", ctx)
            assert route == "local_command"
            assert_contains(cancel_rerun, "status=cancelled")
            assert_contains(cancel_rerun, "explicit human stop/cancel decisions")
            assert_not_contains(cancel_rerun, "Execution rerun:")
            if "superseded_by:" in (bridge.EXECUTIONS_DIR / f"{cancel_exec_id}.md").read_text(encoding="utf-8"):
                raise AssertionError(f"cancelled exec should not be superseded: {cancel_exec_id}")

            # Not covered here: a rerun whose replacement exec itself lands in
            # needs_manual_start. The core semantic is implemented by stamping
            # only after build_exec_start_reply returns a distinct new exec id;
            # this smoke focuses on successful recovery, healthy refusal,
            # superseded refusal, and cancelled refusal.
        finally:
            bridge.probe_codex_noninteractive = original_probe
            bridge.run_allowlisted_external_command = original_runner
            bridge.run_allowlisted_post_run_command = original_post_run
            restore_paths(old_paths)

    print("smoke_exec_rerun passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
