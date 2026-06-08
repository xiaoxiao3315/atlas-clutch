from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import bridge


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected text: {needle}")


def reset_logger() -> None:
    for handler in bridge.LOGGER.handlers[:]:
        bridge.LOGGER.removeHandler(handler)
        handler.close()
    bridge.LOGGER.setLevel(logging.NOTSET)


def context() -> dict:
    return {
        "registered": True,
        "robot_id": "robot-exec-smoke-123456",
        "owner_channel_id": "owner-exec-smoke-123456",
        "last_seq": 620,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "exec-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T22:00:00+08:00"},
    }


def extract_id(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"{label} missing")
    return match.group(0)


def extract_task_id(text: str) -> str:
    return extract_id(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text, "task_id")


def extract_dispatch_id(text: str) -> str:
    return extract_id(r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?", text, "dispatch_id")


def extract_exec_id(text: str) -> str:
    return extract_id(r"EXEC-\d{8}-\d{6}(?:-\d{2})?", text, "exec_id")


def configure_temp_paths(root: Path) -> dict[str, Path]:
    old_paths = {
        "WORKBENCH_DIR": bridge.WORKBENCH_DIR,
        "TASKS_DIR": bridge.TASKS_DIR,
        "PROJECTS_DIR": bridge.PROJECTS_DIR,
        "EVIDENCE_DIR": bridge.EVIDENCE_DIR,
        "RETROS_DIR": bridge.RETROS_DIR,
        "LEARNING_DIR": bridge.LEARNING_DIR,
        "LEARNING_CANDIDATES_DIR": bridge.LEARNING_CANDIDATES_DIR,
        "LEARNING_PROPOSALS_DIR": bridge.LEARNING_PROPOSALS_DIR,
        "LEARNING_REGISTRY_DIR": bridge.LEARNING_REGISTRY_DIR,
        "LEARNING_REJECTED_DIR": bridge.LEARNING_REJECTED_DIR,
        "LEARNING_DEFERRED_DIR": bridge.LEARNING_DEFERRED_DIR,
        "LEARNING_PACKAGES_DIR": bridge.LEARNING_PACKAGES_DIR,
        "LEARNING_LOGS_DIR": bridge.LEARNING_LOGS_DIR,
        "APPLICATIONS_DIR": bridge.APPLICATIONS_DIR,
        "PLAYBOOKS_DIR": bridge.PLAYBOOKS_DIR,
        "PROJECT_PLAYBOOKS_DIR": bridge.PROJECT_PLAYBOOKS_DIR,
        "CONTEXT_PACKS_DIR": bridge.CONTEXT_PACKS_DIR,
        "DISPATCHES_DIR": bridge.DISPATCHES_DIR,
        "EXECUTIONS_DIR": bridge.EXECUTIONS_DIR,
        "PILOTS_DIR": bridge.PILOTS_DIR,
        "COLLECTIONS_DIR": bridge.COLLECTIONS_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }
    bridge.WORKBENCH_DIR = root / "workbench"
    bridge.TASKS_DIR = bridge.WORKBENCH_DIR / "tasks"
    bridge.PROJECTS_DIR = bridge.WORKBENCH_DIR / "projects"
    bridge.EVIDENCE_DIR = bridge.WORKBENCH_DIR / "evidence"
    bridge.RETROS_DIR = bridge.WORKBENCH_DIR / "retros"
    bridge.LEARNING_DIR = bridge.WORKBENCH_DIR / "learning"
    bridge.LEARNING_CANDIDATES_DIR = bridge.LEARNING_DIR / "candidates"
    bridge.LEARNING_PROPOSALS_DIR = bridge.LEARNING_DIR / "proposals"
    bridge.LEARNING_REGISTRY_DIR = bridge.LEARNING_DIR / "registry"
    bridge.LEARNING_REJECTED_DIR = bridge.LEARNING_DIR / "rejected"
    bridge.LEARNING_DEFERRED_DIR = bridge.LEARNING_DIR / "deferred"
    bridge.LEARNING_PACKAGES_DIR = bridge.LEARNING_DIR / "packages"
    bridge.LEARNING_LOGS_DIR = bridge.LEARNING_DIR / "logs"
    bridge.APPLICATIONS_DIR = bridge.WORKBENCH_DIR / "applications"
    bridge.PLAYBOOKS_DIR = bridge.WORKBENCH_DIR / "playbooks"
    bridge.PROJECT_PLAYBOOKS_DIR = bridge.PLAYBOOKS_DIR / "projects"
    bridge.CONTEXT_PACKS_DIR = bridge.WORKBENCH_DIR / "context_packs"
    bridge.DISPATCHES_DIR = bridge.WORKBENCH_DIR / "dispatches"
    bridge.EXECUTIONS_DIR = bridge.WORKBENCH_DIR / "executions"
    bridge.PILOTS_DIR = bridge.WORKBENCH_DIR / "pilots"
    bridge.COLLECTIONS_DIR = bridge.WORKBENCH_DIR / "collections"
    bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
    bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
    bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
    bridge.LOG_DIR = root / "logs"
    bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
    reset_logger()
    bridge.setup_logging()
    return old_paths


def restore_paths(old_paths: dict[str, Path]) -> None:
    for name, value in old_paths.items():
        setattr(bridge, name, value)
    reset_logger()


def complete_report(secret: str) -> str:
    return f"""Task id: test
Dispatch id: test

Execution summary:
- Manual executor report only. Atlas/Bridge did not run commands.

Modified files:
- none / read-only execution handoff test

Commands:
- python -m py_compile bridge.py
- python smoke_exec.py

Test results:
- passed: py_compile
- passed: smoke_exec

Key logs or screenshots:
- workbench/executions checked for token hits

Unverified:
- Octo UI live was not run

Unresolved risks:
- live evidence still needs manual confirmation

Rollback notes:
- revert exec ledger changes if needed

Token sample: {secret}
Authorization: Bearer {secret}
Cookie: session={secret}
password: {secret}
api_key: {secret}
secret: {secret}
"""


def assert_no_secret_in_tree(root: Path) -> None:
    forbidden = ("bf_", "sk-", "Authorization: Bearer", "Cookie:")
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert_not_contains(text, needle)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-exec-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        secret = "bf_exec_secret_123456"
        try:
            ctx = context()

            help_text, route = bridge.prepare_reply("/exec help", ctx)
            assert route == "local_command"
            for needle in (
                "Atlas execution commands",
                "/exec start <dispatch_id>",
                "read-only auto-run only",
                "needs_manual_start",
                "human_confirm_required: true",
                "read_only_auto_exec_enabled: true",
                "probes Codex non-interactive support",
            ):
                assert_contains(help_text, needle)

            missing, route = bridge.prepare_reply("/exec prepare DISPATCH-20260607-000000", ctx)
            assert route == "local_command"
            assert_contains(missing, "not found")

            bridge.prepare_reply("/project new kiro_proxy Kiro proxy project", ctx)
            task_reply, route = bridge.prepare_reply("/task new Check Kiro proxy current state --project kiro_proxy", ctx)
            assert route == "local_command"
            task_id = extract_task_id(task_reply)

            dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex --with-context", ctx)
            assert route == "local_command"
            dispatch_id = extract_dispatch_id(dispatch_reply)

            next_ready, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_ready, f"/exec prepare {dispatch_id}")

            prepared, route = bridge.prepare_reply(f"/exec prepare {dispatch_id}", ctx)
            assert route == "local_command"
            exec_id = extract_exec_id(prepared)
            assert_contains(prepared, "Execution session prepared")
            assert_contains(prepared, "human_confirm_required: true")
            assert_contains(prepared, "auto_execute_enabled: false")

            exec_file = bridge.EXECUTIONS_DIR / f"{exec_id}.md"
            if not exec_file.exists():
                raise AssertionError("execution session file not created")
            exec_text = exec_file.read_text(encoding="utf-8")
            for needle in (
                "status: prepared",
                "mode: semi_auto",
                "human_confirm_required: true",
                "external_execution_enabled: false",
                "runtime_injection_enabled: false",
                "auto_execute_enabled: false",
                "## Copy Payload",
                "## Do Not Auto-Execute",
            ):
                assert_contains(exec_text, needle)

            package, route = bridge.prepare_reply(f"/exec package {exec_id}", ctx)
            assert route == "local_command"
            for needle in (
                "Semi-Auto Execution Package",
                f"exec_id: {exec_id}",
                f"dispatch_id: {dispatch_id}",
                "human_confirm_required: true",
                "auto_execute_enabled: false",
                "Manual Dispatch Package for Codex",
            ):
                assert_contains(package, needle)
            for sensitive in ("bf_", "sk-", "Authorization: Bearer", "Cookie:"):
                assert_not_contains(package, sensitive)

            dispatch_show, route = bridge.prepare_reply(f"/dispatch show {dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(dispatch_show, f"latest_exec_id: {exec_id}")
            assert_contains(dispatch_show, "exec_status: prepared")

            dispatch_package, route = bridge.prepare_reply(f"/dispatch package {dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(dispatch_package, f"latest_exec_id: {exec_id}")
            assert_contains(dispatch_package, "exec_status: prepared")

            marked_copied, route = bridge.prepare_reply(f"/exec mark {exec_id} copied copied into Codex window", ctx)
            assert route == "local_command"
            assert_contains(marked_copied, "status: copied")
            sent_dispatch = (bridge.DISPATCHES_DIR / f"{dispatch_id}.md").read_text(encoding="utf-8")
            assert_contains(sent_dispatch, "status: sent")

            next_copied, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_copied, f"/exec receive {exec_id}")

            returned, route = bridge.prepare_reply(f"/exec receive {exec_id}\n{complete_report(secret)}", ctx)
            assert route == "local_command"
            assert_contains(returned, "Execution return recorded")
            assert_contains(returned, "status: returned")
            assert_contains(returned, "synced_dispatch_receive: true")
            for path in (
                bridge.EXECUTIONS_DIR / f"{exec_id}.md",
                bridge.DISPATCHES_DIR / f"{dispatch_id}.md",
                bridge.TASKS_DIR / f"{task_id}.md",
            ):
                text = path.read_text(encoding="utf-8")
                assert_contains(text, "[REDACTED_TOKEN]")
                assert_not_contains(text, "bf_")
                assert_not_contains(text, "Authorization: Bearer")
                assert_not_contains(text, "Cookie:")

            shown, route = bridge.prepare_reply(f"/exec show {exec_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "Execution session summary")
            assert_contains(shown, "status: returned")

            listed, route = bridge.prepare_reply("/exec list", ctx)
            assert route == "local_command"
            assert_contains(listed, exec_id)

            qa, route = bridge.prepare_reply(f"/dispatch qa {dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(qa, "Dispatch QA")

            opened_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} kiro", ctx)
            opened_dispatch = extract_dispatch_id(opened_dispatch_reply)
            opened_prepare, route = bridge.prepare_reply(f"/exec prepare {opened_dispatch}", ctx)
            opened_exec = extract_exec_id(opened_prepare)
            opened, route = bridge.prepare_reply(f"/exec mark {opened_exec} opened opened Kiro window only", ctx)
            assert route == "local_command"
            assert_contains(opened, "status: opened")

            cancel_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            cancel_dispatch = extract_dispatch_id(cancel_dispatch_reply)
            cancel_prepare, route = bridge.prepare_reply(f"/exec prepare {cancel_dispatch}", ctx)
            cancel_exec = extract_exec_id(cancel_prepare)
            cancelled, route = bridge.prepare_reply(f"/exec cancel {cancel_exec} user stopped", ctx)
            assert route == "local_command"
            assert_contains(cancelled, "status: cancelled")

            fail_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            fail_dispatch = extract_dispatch_id(fail_dispatch_reply)
            fail_prepare, route = bridge.prepare_reply(f"/exec prepare {fail_dispatch}", ctx)
            fail_exec = extract_exec_id(fail_prepare)
            failed, route = bridge.prepare_reply(f"/exec fail {fail_exec} executor window unavailable", ctx)
            assert route == "local_command"
            assert_contains(failed, "status: failed")

            stale_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            stale_dispatch = extract_dispatch_id(stale_dispatch_reply)
            stale_prepare, route = bridge.prepare_reply(f"/exec prepare {stale_dispatch}", ctx)
            stale_exec = extract_exec_id(stale_prepare)
            stale_text = bridge.read_exec(stale_exec)
            stale_text = bridge.replace_task_field(stale_text, "updated_at", "2026-01-01T00:00:00+08:00")
            bridge.write_exec(stale_exec, stale_text)
            stale, route = bridge.prepare_reply("/exec stale", ctx)
            assert route == "local_command"
            assert_contains(stale, stale_exec)

            dashboard, route = bridge.prepare_reply("/exec dashboard", ctx)
            assert route == "local_command"
            for needle in ("execution_count:", "prepared_count:", "opened_count:", "copied_count:", "returned_count:", "stale_count:"):
                assert_contains(dashboard, needle)

            pilot, route = bridge.prepare_reply("/pilot start kiro_proxy Exec pilot", ctx)
            pilot_id = extract_id(r"PILOT-\d{8}-\d{6}(?:-\d{2})?", pilot, "pilot_id")
            bridge.prepare_reply(f"/pilot add-task {pilot_id} {task_id}", ctx)
            bridge.prepare_reply(f"/pilot add-dispatch {pilot_id} {dispatch_id}", ctx)
            metrics, route = bridge.prepare_reply(f"/pilot metrics {pilot_id}", ctx)
            assert route == "local_command"
            for needle in ("execution_count:", "copied_count:", "returned_execution_count:", "stale_execution_count:"):
                assert_contains(metrics, needle)

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            for needle in ("execution_count:", "execution_returned:", "human_confirm_required: true", "auto_execute_enabled: false", "external_execution_enabled: false"):
                assert_contains(status, needle)

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution request must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("run dir E:\\ai", ctx)
            finally:
                bridge.call_hermes = original_call_hermes
            assert route == "work_order"
            assert_contains(work_order, "Bridge")
            assert_not_contains(work_order, "å·²æ‰§è¡Œ")

            assert_no_secret_in_tree(bridge.EXECUTIONS_DIR)
            assert_no_secret_in_tree(bridge.WORKBENCH_DIR)
            assert_no_secret_in_tree(bridge.LOG_DIR)
        finally:
            restore_paths(old_paths)

    print("smoke_exec passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
