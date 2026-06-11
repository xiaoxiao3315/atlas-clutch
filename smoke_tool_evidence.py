"""Focused smoke for ATLAS-CLUTCH-EVIDENCE-020: /tool evidence surface.

Proves tool outputs become task evidence through the existing ledger:
- /tool evidence <task_id> memory status writes exactly one report evidence item
- /tool evidence <task_id> research plan <topic> writes evidence
- invalid / missing task ids write nothing
- unknown tool commands (including anything browser-harness shaped) write nothing
- no subprocess runs at all, so no tool, service, proxy, or browser-harness
  is invoked and no MCP config can change
- the evidence body carries the tool command and the external_execution line
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import bridge
from smoke_exec import configure_temp_paths, context, extract_task_id, restore_paths


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}\n---\n{text}")


def main() -> int:
    def forbid_execution(*args, **kwargs):
        raise AssertionError(f"/tool evidence must not execute anything here: {args} {kwargs}")

    orig_runner = bridge.run_allowlisted_external_command
    orig_post = bridge.run_allowlisted_post_run_command
    orig_subprocess_run = bridge.subprocess.run
    bridge.run_allowlisted_external_command = forbid_execution
    bridge.run_allowlisted_post_run_command = forbid_execution
    bridge.subprocess.run = forbid_execution

    with tempfile.TemporaryDirectory(prefix="ohb-tool-evidence-") as tmp:
        old_paths = configure_temp_paths(Path(tmp))
        try:
            ctx = context()
            task_reply, _ = bridge.prepare_reply("/task new Tool evidence smoke", ctx)
            task_id = extract_task_id(task_reply)

            # memory status -> exactly one evidence item
            reply, route = bridge.prepare_reply(f"/tool evidence {task_id} memory status", ctx)
            if route != "local_command":
                raise AssertionError(f"unexpected route: {route}")
            assert_contains(reply, "tool evidence recorded: EV-")
            assert_contains(reply, f"task_id: {task_id}")
            assert_contains(reply, "tool_command: memory status")
            assert_contains(reply, "external_execution: none (no service started")
            assert_contains(reply, "evidence_type: report")
            records = bridge.evidence_records(task_id)
            if len(records) != 1:
                raise AssertionError(f"expected exactly 1 evidence item, found {len(records)}")
            if records[0].get("type") != "report":
                raise AssertionError(f"unexpected evidence type: {records[0].get('type')}")

            # research plan -> second evidence item
            plan_reply = bridge.handle_local_command(f"/tool evidence {task_id} research plan dummy-topic", ctx)
            assert_contains(plan_reply, "tool evidence recorded: EV-")
            assert_contains(plan_reply, "tool_command: research plan dummy-topic")
            if len(bridge.evidence_records(task_id)) != 2:
                raise AssertionError("research plan evidence not recorded")

            # evidence body carries command and external_execution line
            ledger_text = bridge.read_evidence_text(task_id)
            assert_contains(ledger_text, "Tool report")
            assert_contains(ledger_text, "tool_command: memory status")
            assert_contains(ledger_text, "tool_command: research plan dummy-topic")
            assert_contains(ledger_text, "external_execution: none (no ARS pipeline run")
            assert_contains(ledger_text, "not-connected to real Atlas/Hermes memory")

            # invalid task id -> refused, nothing written
            bad_format = bridge.handle_local_command("/tool evidence BAD-ID memory status", ctx)
            assert_contains(bad_format, "tool evidence refused: invalid task id")
            assert_contains(bad_format, "evidence_written: none")

            # well-formed but missing task id -> refused, nothing written
            missing = bridge.handle_local_command("/tool evidence OHB-19990101-000000 memory status", ctx)
            assert_contains(missing, "tool evidence refused: task not found")
            assert_contains(missing, "evidence_written: none")

            # unknown tool command -> refused, nothing written
            unknown = bridge.handle_local_command(f"/tool evidence {task_id} frobnicate now", ctx)
            assert_contains(unknown, "tool evidence refused: unsupported tool command")
            assert_contains(unknown, "evidence_written: none")

            # browser-harness shaped command -> refused, never executed
            harness = bridge.handle_local_command(f"/tool evidence {task_id} browser-harness collect", ctx)
            assert_contains(harness, "tool evidence refused: unsupported tool command")
            assert_contains(harness, "browser-harness is gated and cannot be recorded or executed.")

            if len(bridge.evidence_records(task_id)) != 2:
                raise AssertionError("refused commands must not write evidence")

            # plain adapter commands stay unchanged
            plain = bridge.handle_local_command("/tool memory status", ctx)
            assert_contains(plain, "agentmemory status (read-only)")
            if "evidence recorded" in plain:
                raise AssertionError("plain /tool command must not write evidence")
        finally:
            bridge.run_allowlisted_external_command = orig_runner
            bridge.run_allowlisted_post_run_command = orig_post
            bridge.subprocess.run = orig_subprocess_run
            restore_paths(old_paths)

    print("smoke_tool_evidence: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
