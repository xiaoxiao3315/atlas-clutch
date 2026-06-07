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
        "robot_id": "robot-collect-smoke-123456",
        "owner_channel_id": "owner-collect-smoke-123456",
        "last_seq": 520,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "collect-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T21:00:00+08:00"},
    }


def extract_id(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"{label} missing")
    return match.group(0)


def extract_task_id(text: str) -> str:
    return extract_id(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text, "task_id")


def extract_collection_id(text: str) -> str:
    return extract_id(r"COLLECT-\d{8}-\d{6}(?:-\d{2})?", text, "collection_id")


def extract_evidence_id(text: str) -> str:
    return extract_id(r"EV-\d{8}-\d{6}(?:-\d{2})?", text, "evidence_id")


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


def assert_no_secret_in_tree(root: Path) -> None:
    forbidden = ("bf_", "sk-", "Authorization: Bearer", "Cookie:")
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert_not_contains(text, needle)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-collect-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        try:
            ctx = context()

            help_text, route = bridge.prepare_reply("/collect help", ctx)
            assert route == "local_command"
            for needle in ("read-only", "allowlisted", "does not accept user shell commands", "does not mark evidence verified"):
                assert_contains(help_text, needle)

            profiles, route = bridge.prepare_reply("/collect profiles", ctx)
            assert route == "local_command"
            assert_contains(profiles, "octo-bridge")
            assert_contains(profiles, "kiro-gateway")
            assert_contains(profiles, "arbitrary_command_enabled: false")

            bridge.prepare_reply("/project new kiro_proxy Kiro proxy project", ctx)
            task_reply, route = bridge.prepare_reply(
                "/task new Collect Kiro proxy local evidence --project kiro_proxy",
                ctx,
            )
            assert route == "local_command"
            task_id = extract_task_id(task_reply)

            refused, route = bridge.prepare_reply(f"/collect snapshot {task_id} unknown-profile", ctx)
            assert route == "local_command"
            assert_contains(refused, "unknown collect profile")

            refused_command, route = bridge.prepare_reply(f"/collect smoke {task_id} octo-bridge powershell", ctx)
            assert route == "local_command"
            assert_contains(refused_command, "Usage: /collect smoke")

            snapshot, route = bridge.prepare_reply(f"/collect snapshot {task_id} octo-bridge", ctx)
            assert route == "local_command"
            collection_id = extract_collection_id(snapshot)
            evidence_id = extract_evidence_id(snapshot)
            assert_contains(snapshot, "Collection snapshot created")
            assert_contains(snapshot, "arbitrary_command_enabled: false")
            collection_file = bridge.COLLECTIONS_DIR / f"{collection_id}.md"
            if not collection_file.exists():
                raise AssertionError("collection file not created")
            collection_text = collection_file.read_text(encoding="utf-8")
            for needle in (
                "## Commands Run",
                "## Git Evidence",
                "## Smoke Evidence",
                "## Log Evidence",
                "## Runtime Evidence",
                "## Workbench Evidence",
                "## Sensitive Scan",
                "## Standard Return Report",
                "## Do Not Treat As Verified",
                "mode: read_only_collect",
                "verified: false",
            ):
                assert_contains(collection_text, needle)

            shown, route = bridge.prepare_reply(f"/collect show {collection_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "Collection summary")
            assert_contains(shown, "profile: octo-bridge")

            report, route = bridge.prepare_reply(f"/collect report {collection_id}", ctx)
            assert route == "local_command"
            for needle in (
                f"Task id: {task_id}",
                f"Collection id: {collection_id}",
                "Modified files:",
                "Commands:",
                "Test results:",
                "Key logs or screenshots:",
                "Unverified:",
                "Unresolved risks:",
                "Rollback notes:",
                "Sensitive Information Handling:",
            ):
                assert_contains(report, needle)

            attached, route = bridge.prepare_reply(f"/collect attach {task_id} {collection_id}", ctx)
            assert route == "local_command"
            assert_contains(attached, "Collection attached")
            assert_contains(attached, task_id)
            assert_contains((bridge.EVIDENCE_DIR / f"{task_id}.md").read_text(encoding="utf-8"), evidence_id)

            qa, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            assert_contains(qa, "collection_count:")
            assert_contains(qa, "latest_collection_id:")

            smoke_task, route = bridge.prepare_reply("/task new Collect smoke allowlist --project kiro_proxy", ctx)
            smoke_task_id = extract_task_id(smoke_task)
            smoke, route = bridge.prepare_reply(f"/collect smoke {smoke_task_id} octo-bridge", ctx)
            assert route == "local_command"
            smoke_collection_id = extract_collection_id(smoke)
            assert_contains(smoke, "Collection smoke created")
            assert_contains(smoke, "whitelist_count:")
            smoke_text = (bridge.COLLECTIONS_DIR / f"{smoke_collection_id}.md").read_text(encoding="utf-8")
            assert_contains(smoke_text, "smoke_consultation.py")
            assert_contains(smoke_text, "smoke_auto_evidence.py")
            assert_not_contains(smoke_text, "powershell")

            listed, route = bridge.prepare_reply("/collect list", ctx)
            assert route == "local_command"
            assert_contains(listed, collection_id)
            assert_contains(listed, smoke_collection_id)

            project_brief, route = bridge.prepare_reply("/project brief kiro_proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "Collection view")
            assert_contains(project_brief, "collection_count")

            pilot, route = bridge.prepare_reply("/pilot start kiro_proxy Collect pilot", ctx)
            pilot_id = extract_id(r"PILOT-\d{8}-\d{6}(?:-\d{2})?", pilot, "pilot_id")
            bridge.prepare_reply(f"/pilot add-task {pilot_id} {task_id}", ctx)
            metrics, route = bridge.prepare_reply(f"/pilot metrics {pilot_id}", ctx)
            assert route == "local_command"
            assert_contains(metrics, "collection_count")
            assert_contains(metrics, "auto_evidence_count")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "collection_count")
            assert_contains(status, "collect_mode: read_only_whitelist")
            assert_contains(status, "arbitrary_command_enabled: false")

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution request must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("帮我执行 dir E:\\ai", ctx)
            finally:
                bridge.call_hermes = original_call_hermes
            assert route == "work_order"
            assert_contains(work_order, "不运行命令")
            assert_not_contains(work_order, "已执行")

            assert_no_secret_in_tree(bridge.COLLECTIONS_DIR)
            assert_no_secret_in_tree(bridge.WORKBENCH_DIR)
            assert_no_secret_in_tree(bridge.LOG_DIR)
        finally:
            restore_paths(old_paths)

    print("smoke_collect passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
