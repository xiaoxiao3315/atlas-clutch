"""Smoke: Atlas auto tool routing records adapter output as evidence.

Covers: keyword task -> dispatch create runs the tool pre-pass -> adapter
output lands in the evidence ledger as verified, source tool-auto;
idempotent on second dispatch; no-match tasks get no evidence; kill switch
honored; boilerplate like "no code changes" never triggers a tool.
Runs entirely in a temp workbench; adapters are read-only/prepared-only.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import bridge
from smoke_exec import (
    assert_contains,
    assert_not_contains,
    configure_temp_paths,
    context,
    extract_dispatch_id,
    extract_task_id,
    restore_paths,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-tool-autoroute-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        old_env = os.environ.get("OHB_TOOL_AUTOROUTE")
        try:
            os.environ.pop("OHB_TOOL_AUTOROUTE", None)
            ctx = context()
            bridge.prepare_reply("/project new auto_exec Tool autoroute smoke", ctx)

            # 1. Code-intelligence task: routing must fire and record evidence.
            task_reply, route = bridge.prepare_reply(
                "/task new Refactor the dispatch function class structure for clarity --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            task_id = extract_task_id(task_reply)
            dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            assert route == "local_command"
            extract_dispatch_id(dispatch_reply)
            assert_contains(dispatch_reply, "Tool Pre-pass:")
            assert_contains(dispatch_reply, "tool_auto_routing: codegraph query")
            assert_contains(dispatch_reply, "-> EV-")
            evidence_text = (bridge.EVIDENCE_DIR / f"{task_id}.md").read_text(encoding="utf-8")
            assert_contains(evidence_text, "auto_tool_command: codegraph query")
            assert_contains(evidence_text, "source: tool-auto")
            assert_contains(evidence_text, "verified: verified")
            if evidence_text.count("auto_tool_command:") != 1:
                raise AssertionError(f"code-refactor task should route exactly one tool:\n{evidence_text}")
            assert_not_contains(evidence_text, "auto_tool_command: browser")
            assert_not_contains(evidence_text, "browser-harness")
            task_text = (bridge.TASKS_DIR / f"{task_id}.md").read_text(encoding="utf-8")
            assert_contains(task_text, "evidence EV-")

            # 2. Idempotence: a second dispatch on the same task must not duplicate.
            second_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            assert route == "local_command"
            assert_contains(second_reply, "already recorded; skipped")
            evidence_after = (bridge.EVIDENCE_DIR / f"{task_id}.md").read_text(encoding="utf-8")
            if evidence_after.count("auto_tool_command: codegraph query") != 1:
                raise AssertionError("tool pre-pass duplicated evidence on second dispatch")

            # 3. Boilerplate must not trigger routing ("no code changes" etc.).
            plain_reply, route = bridge.prepare_reply(
                "/task new Read-only inspect local state only, no code changes, do not modify files --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            plain_task_id = extract_task_id(plain_reply)
            plain_dispatch, route = bridge.prepare_reply(f"/dispatch create {plain_task_id} codex", ctx)
            assert route == "local_command"
            assert_contains(plain_dispatch, "tool_auto_routing: no matching tool")
            if (bridge.EVIDENCE_DIR / f"{plain_task_id}.md").exists():
                plain_evidence = (bridge.EVIDENCE_DIR / f"{plain_task_id}.md").read_text(encoding="utf-8")
                assert_not_contains(plain_evidence, "auto_tool_command:")

            # 4. Kill switch.
            os.environ["OHB_TOOL_AUTOROUTE"] = "false"
            kill_reply, route = bridge.prepare_reply(
                "/task new Investigate the research literature on memory architecture --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            kill_task_id = extract_task_id(kill_reply)
            kill_dispatch, route = bridge.prepare_reply(f"/dispatch create {kill_task_id} codex", ctx)
            assert route == "local_command"
            assert_contains(kill_dispatch, "tool_auto_routing: disabled")
            if (bridge.EVIDENCE_DIR / f"{kill_task_id}.md").exists():
                assert_not_contains(
                    (bridge.EVIDENCE_DIR / f"{kill_task_id}.md").read_text(encoding="utf-8"),
                    "auto_tool_command:",
                )
            os.environ.pop("OHB_TOOL_AUTOROUTE", None)

            # 5. Multi-rule cap: research+memory task matches at most 2 tools.
            multi_reply, route = bridge.prepare_reply(
                "/task new Research previously recorded memory architecture overview --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            multi_task_id = extract_task_id(multi_reply)
            multi_dispatch, route = bridge.prepare_reply(f"/dispatch create {multi_task_id} codex", ctx)
            assert route == "local_command"
            assert_contains(multi_dispatch, "tool_auto_routing:")
            multi_evidence = (bridge.EVIDENCE_DIR / f"{multi_task_id}.md").read_text(encoding="utf-8")
            if multi_evidence.count("auto_tool_command:") > 2:
                raise AssertionError("tool pre-pass exceeded the 2-tool cap")
        finally:
            if old_env is None:
                os.environ.pop("OHB_TOOL_AUTOROUTE", None)
            else:
                os.environ["OHB_TOOL_AUTOROUTE"] = old_env
            restore_paths(old_paths)

    print("smoke_tool_autoroute passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
