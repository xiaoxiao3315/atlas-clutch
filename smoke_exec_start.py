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


def sensitive_needles() -> tuple[str, ...]:
    return (
        "bf" + "_",
        "sk" + "-",
        "Authorization" + ": Bearer",
        "Cookie" + ":",
    )


def assert_no_sensitive_markers(text: str) -> None:
    for needle in sensitive_needles():
        assert_not_contains(text, needle)


def assert_no_sensitive_in_tree(root: Path) -> None:
    if not root.exists():
        return
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        assert_no_sensitive_markers(text)


def assert_complete_runner_payload(payload: str, task_id: str, dispatch_id: str) -> None:
    if payload.strip() == "# Semi-Auto Execution Package for Codex":
        raise AssertionError("runner received only the payload title")
    if len(payload.splitlines()) < 40:
        raise AssertionError("runner payload is unexpectedly short")
    for needle in (
        "# Semi-Auto Execution Package for Codex",
        "## Dispatch Summary",
        f"task_id: {task_id}",
        f"dispatch_id: {dispatch_id}",
        "## Dispatch Package",
        "## Goal",
        "## Scope",
        "## Execution Boundary",
        "## Acceptance Criteria",
        "## Return Report Format",
    ):
        assert_contains(payload, needle)


def create_read_only_dispatch(ctx: dict, project_id: str = "auto_exec") -> tuple[str, str]:
    task_reply, route = bridge.prepare_reply(
        f"/task new Read-only inspect local state only, no code changes, do not modify files --project {project_id}",
        ctx,
    )
    assert route == "local_command"
    task_id = extract_task_id(task_reply)
    dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex --with-context", ctx)
    assert route == "local_command"
    dispatch_id = extract_dispatch_id(dispatch_reply)
    return task_id, dispatch_id


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-exec-start-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        original_probe = bridge.probe_codex_noninteractive
        original_probe_write = bridge.probe_codex_workspace_write
        original_runner = bridge.run_allowlisted_external_command
        original_post_run = bridge.run_allowlisted_post_run_command
        external_commands: list[list[str]] = []
        post_run_commands: list[list[str]] = []
        forbidden_secret = "bf" + "_exec_start_secret_123456"
        try:
            (root / ".env").write_text(
                "\n".join(
                    [
                        f"BOT_TOKEN={forbidden_secret}",
                        "API_SECRET=do-not-read",
                    ]
                ),
                encoding="utf-8",
            )
            project_sentinel = root / "user_project" / "sentinel.txt"
            project_sentinel.parent.mkdir(parents=True, exist_ok=True)
            project_sentinel.write_text("unchanged", encoding="utf-8")

            ctx = context()
            bridge.prepare_reply("/project new auto_exec Auto execution smoke", ctx)

            captured_runner_inputs: list[str] = []

            def fake_post_run(argv: list[str]) -> dict:
                post_run_commands.append(list(argv))
                if not bridge.is_allowed_post_run_command(list(argv)):
                    raise AssertionError(f"unexpected post-run command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": " M bridge.py" if argv == ["git", "status", "--short"] else " bridge.py | 1 +",
                    "stderr": "",
                }

            def fake_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                external_commands.append(list(argv))
                captured_runner_inputs.append(input_text)
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": "fake runner received full stdin payload",
                    "stderr": "",
                }

            bridge.run_allowlisted_external_command = fake_runner
            bridge.run_allowlisted_post_run_command = fake_post_run
            bridge.probe_codex_noninteractive = lambda: {
                "supported": True,
                "mode": "codex_exec_read_only_stdin",
                "reason": "smoke fake non-interactive read-only stdin runner",
                "command": ["codex", "exec", "--sandbox", "read-only", "-"],
            }

            task_id, dispatch_id = create_read_only_dispatch(ctx)
            started, route = bridge.prepare_reply(f"/exec start {dispatch_id}", ctx)
            assert route == "local_command"
            exec_id = extract_exec_id(started)
            assert_contains(started, "Execution auto-run returned")
            assert_contains(started, "status: returned")
            assert_contains(started, "read_only_gate: passed")
            assert_contains(started, "dispatch_receive_synced: true")
            assert_contains(started, "no_git_add_commit_push: true")
            assert_no_sensitive_markers(started)
            if len(captured_runner_inputs) != 1:
                raise AssertionError("runner input was not captured exactly once")
            assert_complete_runner_payload(captured_runner_inputs[0], task_id, dispatch_id)
            assert_no_sensitive_markers(captured_runner_inputs[0])

            exec_file = bridge.EXECUTIONS_DIR / f"{exec_id}.md"
            dispatch_file = bridge.DISPATCHES_DIR / f"{dispatch_id}.md"
            task_file = bridge.TASKS_DIR / f"{task_id}.md"
            for path in (exec_file, dispatch_file, task_file):
                if not path.exists():
                    raise AssertionError(f"expected record missing: {path}")
                assert_no_sensitive_markers(path.read_text(encoding="utf-8", errors="replace"))
            assert_contains(exec_file.read_text(encoding="utf-8"), "status: returned")
            assert_contains(exec_file.read_text(encoding="utf-8"), "read_only_auto_run: true")
            assert_contains(exec_file.read_text(encoding="utf-8"), "completion_state: completed")
            assert_contains(exec_file.read_text(encoding="utf-8"), "returncode: 0")
            assert_contains(exec_file.read_text(encoding="utf-8"), "## Post-Run Snapshot")
            assert_contains(dispatch_file.read_text(encoding="utf-8"), "status: returned")
            assert_contains(task_file.read_text(encoding="utf-8"), "status: reported")
            if not any(bridge.EVIDENCE_DIR.rglob("*.md")):
                raise AssertionError("expected evidence intake record")
            if external_commands != [["codex", "exec", "--sandbox", "read-only", "-"]]:
                raise AssertionError(f"unexpected runner command shape: {external_commands}")

            def timeout_with_output_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                external_commands.append(list(argv))
                captured_runner_inputs.append(input_text)
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 124,
                    "stdout": "\n".join(
                        [
                            "AUTORUN-PAYLOAD-OK",
                            "Task id: timeout-test",
                            "Dispatch id: timeout-test",
                            "Execution summary:",
                            "- fake timeout produced useful output",
                            "Modified files:",
                            "- none",
                            "Commands:",
                            "- fake codex exec read-only",
                            "Test results:",
                            "- timeout stdout preserved",
                            "Unverified:",
                            "- live UI",
                            "Unresolved risks:",
                            "- timeout still occurred",
                        ]
                    ),
                    "stderr": "execution timed out after producing stdout",
                    "timed_out": True,
                }

            bridge.run_allowlisted_external_command = timeout_with_output_runner
            timeout_task_id, timeout_dispatch_id = create_read_only_dispatch(ctx)
            timeout_reply, route = bridge.prepare_reply(f"/exec start {timeout_dispatch_id}", ctx)
            assert route == "local_command"
            timeout_exec_id = extract_exec_id(timeout_reply)
            assert_contains(timeout_reply, "status: returned")
            assert_contains(timeout_reply, "completion_state: timeout_with_output")
            timeout_exec_text = (bridge.EXECUTIONS_DIR / f"{timeout_exec_id}.md").read_text(encoding="utf-8")
            for needle in (
                "status: returned",
                "returncode: 124",
                "timed_out: true",
                "completion_state: timeout_with_output",
                "AUTORUN-PAYLOAD-OK",
                "## Runner Stdout",
                "## Runner Metadata",
            ):
                assert_contains(timeout_exec_text, needle)
            assert_contains((bridge.DISPATCHES_DIR / f"{timeout_dispatch_id}.md").read_text(encoding="utf-8"), "status: returned")
            assert_contains((bridge.TASKS_DIR / f"{timeout_task_id}.md").read_text(encoding="utf-8"), "status: reported")

            def timeout_with_payload_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                external_commands.append(list(argv))
                captured_runner_inputs.append(input_text)
                return {
                    "returncode": 124,
                    "stdout": "",
                    "stderr": f"runner saw payload\n{input_text[:900]}",
                    "timed_out": True,
                }

            bridge.run_allowlisted_external_command = timeout_with_payload_runner
            payload_task_id, payload_dispatch_id = create_read_only_dispatch(ctx)
            payload_reply, route = bridge.prepare_reply(f"/exec start {payload_dispatch_id}", ctx)
            assert route == "local_command"
            payload_exec_id = extract_exec_id(payload_reply)
            assert_contains(payload_reply, "status: failed")
            assert_contains(payload_reply, "completion_state: timeout_with_payload")
            payload_exec_text = (bridge.EXECUTIONS_DIR / f"{payload_exec_id}.md").read_text(encoding="utf-8")
            assert_contains(payload_exec_text, "completion_state: timeout_with_payload")
            assert_contains(payload_exec_text, "payload_state: payload_seen")

            def payload_missing_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                external_commands.append(list(argv))
                captured_runner_inputs.append(input_text)
                return {
                    "returncode": 124,
                    "stdout": "",
                    "stderr": "execution timed out before prompt echo",
                    "timed_out": True,
                }

            bridge.run_allowlisted_external_command = payload_missing_runner
            missing_task_id, missing_dispatch_id = create_read_only_dispatch(ctx)
            missing_reply, route = bridge.prepare_reply(f"/exec start {missing_dispatch_id}", ctx)
            assert route == "local_command"
            missing_exec_id = extract_exec_id(missing_reply)
            assert_contains(missing_reply, "status: failed")
            assert_contains(missing_reply, "completion_state: payload_missing")
            missing_exec_text = (bridge.EXECUTIONS_DIR / f"{missing_exec_id}.md").read_text(encoding="utf-8")
            assert_contains(missing_exec_text, "completion_state: payload_missing")
            assert_contains(missing_exec_text, "payload_state: payload_missing")

            write_task_reply, route = bridge.prepare_reply(
                "/task new Create or update only workbench/tmp/write-runner-live-smoke.txt. Do not modify source code. No git add/commit/push. --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            write_task_id = extract_task_id(write_task_reply)
            write_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {write_task_id} codex --with-context", ctx)
            assert route == "local_command"
            write_dispatch_id = extract_dispatch_id(write_dispatch_reply)
            manual, route = bridge.prepare_reply(f"/exec start {write_dispatch_id}", ctx)
            assert route == "local_command"
            manual_exec_id = extract_exec_id(manual)
            assert_contains(manual, "status: needs_manual_start")
            assert_contains(manual, "human_confirm_required: true")
            assert_contains((bridge.EXECUTIONS_DIR / f"{manual_exec_id}.md").read_text(encoding="utf-8"), "auto_execute_enabled: false")
            if len(captured_runner_inputs) != 4:
                raise AssertionError("write-task dispatch must not call runner")

            write_capture_start = len(captured_runner_inputs)
            external_start = len(external_commands)

            def fake_write_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                external_commands.append(list(argv))
                captured_runner_inputs.append(input_text)
                if argv != ["codex", "exec", "--sandbox", "workspace-write", "-"]:
                    raise AssertionError(f"write runner used unexpected command: {argv}")
                assert_contains(input_text, "## Human Write Approval")
                assert_contains(input_text, "## Workspace-Write Forbidden Actions")
                assert_contains(input_text, "workbench/tmp/write-runner-live-smoke.txt")
                assert_contains(input_text, "Do not run git add.")
                assert_contains(input_text, "Do not deploy.")
                return {
                    "returncode": 0,
                    "stdout": "\n".join(
                        [
                            "Task id: write-test",
                            "Dispatch id: write-test",
                            "Execution summary:",
                            "- fake workspace-write runner completed",
                            "Modified files:",
                            "- bridge.py",
                            "Commands:",
                            "- python -B smoke_exec_start.py",
                            "Test results:",
                            "- passed",
                            "Unverified:",
                            "- live UI",
                            "Unresolved risks:",
                            "- none",
                        ]
                    ),
                    "stderr": "",
                    "timed_out": False,
                }

            bridge.run_allowlisted_external_command = fake_write_runner
            bridge.probe_codex_workspace_write = lambda: {
                "supported": True,
                "mode": "codex_exec_workspace_write_stdin",
                "reason": "smoke fake non-interactive workspace-write runner",
                "command": ["codex", "exec", "--sandbox", "workspace-write", "-"],
            }
            approved, route = bridge.prepare_reply(f"/exec approve {manual_exec_id} write", ctx)
            assert route == "local_command"
            assert_contains(approved, "workspace-write runner returned")
            assert_contains(approved, "status: returned")
            assert_contains(approved, "runner_sandbox: workspace-write")
            if len(captured_runner_inputs) != write_capture_start + 1:
                raise AssertionError("write approval must call runner exactly once")
            if external_commands[external_start:] != [["codex", "exec", "--sandbox", "workspace-write", "-"]]:
                raise AssertionError("write approval used non-workspace-write command")
            approved_exec_text = (bridge.EXECUTIONS_DIR / f"{manual_exec_id}.md").read_text(encoding="utf-8")
            for needle in (
                "status: returned",
                "write_confirmed: true",
                "runner_sandbox: workspace-write",
                "## Human Write Approval",
                "## Post-Run Snapshot",
                "## Runner Test Results",
            ):
                assert_contains(approved_exec_text, needle)

            deploy_task_reply, route = bridge.prepare_reply(
                "/task new Deploy app to production --project auto_exec",
                ctx,
            )
            assert route == "local_command"
            deploy_task_id = extract_task_id(deploy_task_reply)
            deploy_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {deploy_task_id} codex --with-context", ctx)
            assert route == "local_command"
            deploy_dispatch_id = extract_dispatch_id(deploy_dispatch_reply)
            deploy_start, route = bridge.prepare_reply(f"/exec start {deploy_dispatch_id}", ctx)
            assert route == "local_command"
            deploy_exec_id = extract_exec_id(deploy_start)
            before_deploy_approve_inputs = len(captured_runner_inputs)
            deploy_approved, route = bridge.prepare_reply(f"/exec approve {deploy_exec_id} write", ctx)
            assert route == "local_command"
            assert_contains(deploy_approved, "write approval refused")
            assert_contains(deploy_approved, "deploy_forbidden: true")
            if len(captured_runner_inputs) != before_deploy_approve_inputs:
                raise AssertionError("deploy approval must not call runner")

            bridge.probe_codex_noninteractive = lambda: {
                "supported": False,
                "mode": "unsupported_help_shape",
                "reason": "smoke unavailable",
                "command": [],
            }
            unsupported_task_id, unsupported_dispatch_id = create_read_only_dispatch(ctx)
            unsupported, route = bridge.prepare_reply(f"/exec start {unsupported_dispatch_id}", ctx)
            assert route == "local_command"
            unsupported_exec_id = extract_exec_id(unsupported)
            assert_contains(unsupported, "Execution needs manual start")
            assert_contains(unsupported, "status: needs_manual_start")
            assert_contains(unsupported, "codex non-interactive unsupported")
            assert_contains((bridge.EXECUTIONS_DIR / f"{unsupported_exec_id}.md").read_text(encoding="utf-8"), "status: needs_manual_start")
            if len(captured_runner_inputs) != write_capture_start + 1:
                raise AssertionError("unsupported Codex path must not call runner")

            dashboard, route = bridge.prepare_reply("/exec dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "started_count:")
            assert_contains(dashboard, "needs_manual_start_count:")
            assert_contains(dashboard, "read_only_auto_exec_enabled: true")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "execution_needs_manual_start_count:")
            assert_contains(status, "read_only_auto_exec_enabled: true")

            if project_sentinel.read_text(encoding="utf-8") != "unchanged":
                raise AssertionError("user project sentinel was modified")
            assert_no_sensitive_in_tree(bridge.WORKBENCH_DIR)
            assert_no_sensitive_in_tree(bridge.LOG_DIR)
            assert_no_sensitive_markers("\n".join(str(command) for command in external_commands))
            assert_no_sensitive_markers("\n".join(str(command) for command in post_run_commands))
            for command in external_commands + post_run_commands:
                command_text = " ".join(command)
                for forbidden in ("git add", "git commit", "git push", "git merge", "deploy"):
                    assert_not_contains(command_text, forbidden)
            _ = unsupported_task_id
        finally:
            bridge.probe_codex_noninteractive = original_probe
            bridge.probe_codex_workspace_write = original_probe_write
            bridge.run_allowlisted_external_command = original_runner
            bridge.run_allowlisted_post_run_command = original_post_run
            restore_paths(old_paths)

    print("smoke_exec_start passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
