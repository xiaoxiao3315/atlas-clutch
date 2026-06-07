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
        "robot_id": "robot-pilot-smoke-123456",
        "owner_channel_id": "owner-pilot-smoke-123456",
        "last_seq": 300,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "pilot-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T19:00:00+08:00"},
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


def extract_pilot_id(text: str) -> str:
    return extract_id(r"PILOT-\d{8}-\d{6}(?:-\d{2})?", text, "pilot_id")


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    if not root.exists():
        return
    for file_path in root.rglob("*"):
        if file_path.is_file():
            assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


def return_report(secret: str) -> str:
    return f"""Execution summary:
- Manual executor report for pilot smoke.

Modified files:
- bridge.py
- README.md
- smoke_pilot.py

Commands:
- python -m py_compile bridge.py
- python smoke_pilot.py

Test results:
- passed: py_compile
- passed: smoke_pilot

Key logs or screenshots:
- pilot metrics generated locally

Unverified:
- Octo UI live was not run

Unresolved risks:
- live debt remains

Rollback notes:
- revert pilot-only changes if needed

Token sample: {secret}
Authorization: bearer {secret}
Cookie: session={secret}
"""


def main() -> int:
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
        "LEARNING_PACKAGES_DIR": bridge.LEARNING_DIR / "packages",
        "LEARNING_LOGS_DIR": bridge.LEARNING_LOGS_DIR,
        "APPLICATIONS_DIR": bridge.APPLICATIONS_DIR,
        "PLAYBOOKS_DIR": bridge.PLAYBOOKS_DIR,
        "PROJECT_PLAYBOOKS_DIR": bridge.PROJECT_PLAYBOOKS_DIR,
        "CONTEXT_PACKS_DIR": bridge.CONTEXT_PACKS_DIR,
        "DISPATCHES_DIR": bridge.DISPATCHES_DIR,
        "PILOTS_DIR": bridge.PILOTS_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }
    secret = "bf_pilot_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-pilot-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
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
            bridge.PILOTS_DIR = bridge.WORKBENCH_DIR / "pilots"
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/pilot help", ctx)
            assert route == "local_command"
            for needle in ("/pilot start", "/pilot metrics", "does not call Codex/Kiro", "does not run commands"):
                assert_contains(help_text, needle)

            missing_project, route = bridge.prepare_reply("/pilot start missing_project Missing pilot", ctx)
            assert route == "local_command"
            assert_contains(missing_project, "not found")

            bridge.prepare_reply("/project new kiro-proxy Kiro reverse proxy real pilot", ctx)
            started, route = bridge.prepare_reply("/pilot start kiro-proxy Kiro reverse proxy pilot", ctx)
            assert route == "local_command"
            pilot_id = extract_pilot_id(started)
            pilot_file = bridge.PILOTS_DIR / f"{pilot_id}.md"
            if not pilot_file.exists():
                raise AssertionError("pilot file not created")
            pilot_text = pilot_file.read_text(encoding="utf-8")
            for needle in (
                "status: active",
                "project_id: kiro-proxy",
                "mode: consultation",
                "external_execution_enabled: false",
                "## Metrics",
                "## Friction Log",
                "live_debt",
            ):
                assert_contains(pilot_text, needle)

            listed, route = bridge.prepare_reply("/pilot list", ctx)
            assert route == "local_command"
            assert_contains(listed, pilot_id)

            task_reply, route = bridge.prepare_reply("/task new Check Kiro reverse proxy status --project kiro-proxy", ctx)
            assert route == "local_command"
            task_id = extract_task_id(task_reply)
            bridge.prepare_reply(f"/context pack task {task_id}", ctx)
            dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {task_id} codex --with-context", ctx)
            assert route == "local_command"
            dispatch_id = extract_dispatch_id(dispatch_reply)
            bridge.prepare_reply(f"/dispatch mark {dispatch_id} sent manual copy", ctx)
            bridge.prepare_reply(f"/dispatch receive {dispatch_id}\n{return_report(secret)}", ctx)
            bridge.prepare_reply(f"/dispatch qa {dispatch_id}", ctx)
            bridge.prepare_reply(f"/task review {task_id}", ctx)
            bridge.prepare_reply(f"/task decide {task_id} needs_evidence Need live proof {secret}", ctx)

            add_task, route = bridge.prepare_reply(f"/pilot add-task {pilot_id} {task_id}", ctx)
            assert route == "local_command"
            assert_contains(add_task, "Pilot task linked")
            add_dispatch, route = bridge.prepare_reply(f"/pilot add-dispatch {pilot_id} {dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(add_dispatch, "Pilot dispatch linked")

            note, route = bridge.prepare_reply(f"/pilot note {pilot_id} manual copy still costs focus {secret}", ctx)
            assert route == "local_command"
            assert_contains(note, "Pilot note recorded")
            assert_not_contains(pilot_file.read_text(encoding="utf-8"), "bf_")

            metrics, route = bridge.prepare_reply(f"/pilot metrics {pilot_id}", ctx)
            assert route == "local_command"
            for needle in (
                "task_count",
                "dispatch_count",
                "returned_count",
                "qa_pass_count",
                "needs_evidence_count",
                "closed_count",
                "evidence_gap_count",
                "context_pack_count",
                "manual_copy_count",
                "estimated_time_saved",
                "main_friction",
            ):
                assert_contains(metrics, needle)

            shown, route = bridge.prepare_reply(f"/pilot show {pilot_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "Pilot summary")
            assert_contains(shown, "project_id: kiro-proxy")

            completed, route = bridge.prepare_reply(f"/pilot complete {pilot_id} smoke pilot done {secret}", ctx)
            assert route == "local_command"
            for needle in (
                "Pilot completed",
                "哪些地方提效",
                "哪些地方仍然麻烦",
                "哪些命令最有用",
                "哪些命令没用上",
                "是否值得继续扩展",
                "下一阶段建议",
            ):
                assert_contains(completed, needle)
            assert_contains(pilot_file.read_text(encoding="utf-8"), "status: completed")

            dashboard, route = bridge.prepare_reply("/pilot dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "Pilot dashboard")
            assert_contains(dashboard, pilot_id)

            exec_request, route = bridge.prepare_reply(r"帮我执行 dir E:\ai", ctx)
            assert route == "work_order"
            assert_contains(exec_request, "dir E")

            assert_no_secret_in_tree(bridge.PILOTS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.WORKBENCH_DIR, "bf_")
            assert_no_secret_in_tree(bridge.LOG_DIR, "bf_")

    finally:
        for name, value in old_paths.items():
            setattr(bridge, name, value)
        reset_logger()

    print("smoke_pilot passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
