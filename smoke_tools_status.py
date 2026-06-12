"""Focused smoke for TOOL-INTEGRATION-001: /tools status surface.

Proves /tools is a pure read-only status surface:
- /tools status reports every registered tool
- no external command or subprocess runs while handling it
- browser-harness is reported as stage1-approved while still not executed
- agentmemory is reported as ingestion-approved with MCP connect still deferred
- headroom is reported as benchmark-first / not proxying live traffic
"""
from __future__ import annotations

import bridge

EXPECTED_TOOL_IDS = [
    "codegraph",
    "understand-anything",
    "academic-research-skills",
    "agentmemory",
    "headroom",
    "browser-harness",
]


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def main() -> int:
    def forbid_execution(*args, **kwargs):
        raise AssertionError(f"/tools must not execute anything: {args} {kwargs}")

    orig_runner = bridge.run_allowlisted_external_command
    orig_post = bridge.run_allowlisted_post_run_command
    orig_subprocess_run = bridge.subprocess.run
    bridge.run_allowlisted_external_command = forbid_execution
    bridge.run_allowlisted_post_run_command = forbid_execution
    bridge.subprocess.run = forbid_execution
    try:
        reply, route = bridge.prepare_reply("/tools status", {})
        if route != "local_command":
            raise AssertionError(f"unexpected route: {route}")
        assert_contains(reply, "status_surface_only")
        for tool_id in EXPECTED_TOOL_IDS:
            assert_contains(reply, f"- {tool_id}\n  status: ")
        assert_contains(reply, "- browser-harness\n  status: stage1-approved")
        assert_contains(reply, "Stage 2 (real profile) needs separate approval")
        assert_contains(reply, "- agentmemory\n  status: ingestion-approved")
        assert_contains(reply, "MCP connect still deferred")
        assert_contains(reply, "library-only; not proxying live Claude/Codex/Kiro/Hermes traffic")
        assert_contains(reply, "headroom_offline_benchmark.py")
        assert_contains(reply, "headroom stays library-only and does not proxy live traffic")

        help_reply = bridge.handle_local_command("/tools", {})
        assert_contains(help_reply, "/tools status")
        help_reply_2 = bridge.handle_local_command("/tools help", {})
        assert_contains(help_reply_2, "status surface only")
    finally:
        bridge.run_allowlisted_external_command = orig_runner
        bridge.run_allowlisted_post_run_command = orig_post
        bridge.subprocess.run = orig_subprocess_run

    print("smoke_tools_status: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
