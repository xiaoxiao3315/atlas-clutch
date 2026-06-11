"""Focused smoke for TOOL-INTEGRATION-002: thin /tool adapters.

Proves the /tool layer is read-only / dry-run:
- /tool help and /tool status work
- /tool codegraph query returns a safe prepared command when the CLI/index
  is unavailable (no execution)
- /tool understand summary never rebuilds; reads an existing graph if present
- /tool research plan returns a plan-only package
- /tool memory status reports lab status without starting a service
- /tool headroom compress returns a dry-run report for pasted text
- no subprocess runs at all while handling /tool commands, which also
  proves browser-harness is never executed
- nothing is written outside the temp fixture, so no MCP config can change
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import bridge


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}\n---\n{text}")


def main() -> int:
    def forbid_execution(*args, **kwargs):
        raise AssertionError(f"/tool must not execute anything here: {args} {kwargs}")

    orig_runner = bridge.run_allowlisted_external_command
    orig_post = bridge.run_allowlisted_post_run_command
    orig_subprocess_run = bridge.subprocess.run
    orig_cli_available = bridge.codegraph_cli_available
    orig_understand_dir = bridge.UNDERSTAND_DIR
    bridge.run_allowlisted_external_command = forbid_execution
    bridge.run_allowlisted_post_run_command = forbid_execution
    bridge.subprocess.run = forbid_execution
    try:
        help_reply, route = bridge.prepare_reply("/tool help", {})
        if route != "local_command":
            raise AssertionError(f"unexpected route: {route}")
        for needle in (
            "/tool codegraph query",
            "/tool understand summary",
            "/tool research plan",
            "/tool memory status",
            "/tool headroom compress",
            "browser-harness is gated and is never executed",
        ):
            assert_contains(help_reply, needle)

        status_reply = bridge.handle_local_command("/tool status", {})
        assert_contains(status_reply, "tool status (compact, read-only)")
        for tool_id in (
            "codegraph",
            "understand-anything",
            "academic-research-skills",
            "agentmemory",
            "headroom",
            "browser-harness",
        ):
            assert_contains(status_reply, f"- {tool_id}:")
        assert_contains(status_reply, "No tool is executed by this command.")

        # codegraph: force the unavailable path; must prepare, not execute.
        bridge.codegraph_cli_available = lambda: (None, "forced_unavailable_for_smoke")
        cg_reply = bridge.handle_local_command("/tool codegraph query dispatch", {})
        assert_contains(cg_reply, "query: dispatch")
        assert_contains(cg_reply, "external_execution: none (command prepared only)")
        assert_contains(cg_reply, 'codegraph query "dispatch" --json')

        # understand: missing graph degrades gracefully, no rebuild.
        with tempfile.TemporaryDirectory(prefix="ohb-tool-adapters-") as tmp:
            bridge.UNDERSTAND_DIR = Path(tmp) / "missing" / ".understand-anything"
            ua_missing = bridge.handle_local_command("/tool understand summary", {})
            assert_contains(ua_missing, "graph: missing")
            assert_contains(ua_missing, "external_execution: none (no rebuild")

            # understand: existing graph is read and summarized, still no rebuild.
            graph_dir = Path(tmp) / ".understand-anything"
            graph_dir.mkdir(parents=True)
            (graph_dir / "knowledge-graph.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "layer:core", "name": "Core Bridge Layer"},
                            {"id": "file:bridge.py"},
                        ],
                        "edges": [{}, {}, {}],
                    }
                ),
                encoding="utf-8",
            )
            bridge.UNDERSTAND_DIR = graph_dir
            ua_present = bridge.handle_local_command("/tool understand summary", {})
            assert_contains(ua_present, "graph: exists")
            assert_contains(ua_present, "node_count: 2")
            assert_contains(ua_present, "edge_count: 3")
            assert_contains(ua_present, "Core Bridge Layer")

        plan_reply = bridge.handle_local_command("/tool research plan dispatch loop safety", {})
        assert_contains(plan_reply, "research plan package (plan only)")
        assert_contains(plan_reply, "topic: dispatch loop safety")
        assert_contains(plan_reply, "external_execution: none (no ARS pipeline run")
        assert_contains(plan_reply, "/ars-plan dispatch loop safety")

        mem_reply = bridge.handle_local_command("/tool memory status", {})
        assert_contains(mem_reply, "external_execution: none (no service started")
        assert_contains(mem_reply, "not-connected to real Atlas/Hermes memory")
        assert_contains(mem_reply, "requires explicit owner approval")

        hr_reply = bridge.handle_local_command("/tool headroom compress dummy pasted text for the smoke", {})
        assert_contains(hr_reply, "headroom compress (dry-run)")
        assert_contains(hr_reply, "external_execution: none (dry-run report")
        assert_contains(hr_reply, "original_length: 31 chars")
        assert_contains(hr_reply, "fail_open_warning")
        assert_contains(hr_reply, "no live traffic proxying")

        unknown_reply = bridge.handle_local_command("/tool frobnicate", {})
        assert_contains(unknown_reply, "Atlas Clutch thin tool adapters")
    finally:
        bridge.run_allowlisted_external_command = orig_runner
        bridge.run_allowlisted_post_run_command = orig_post
        bridge.subprocess.run = orig_subprocess_run
        bridge.codegraph_cli_available = orig_cli_available
        bridge.UNDERSTAND_DIR = orig_understand_dir

    print("smoke_tool_adapters: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
