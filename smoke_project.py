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
        "robot_id": "robot-1234567890",
        "owner_channel_id": "owner-1234567890",
        "last_seq": 120,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "project-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T10:00:00+08:00"},
    }


def extract_task_id(text: str) -> str:
    match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("task_id missing")
    return match.group(0)


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    for file_path in root.rglob("*.md"):
        assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


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
        "LEARNING_PACKAGES_DIR": bridge.LEARNING_PACKAGES_DIR,
        "LEARNING_LOGS_DIR": bridge.LEARNING_LOGS_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }

    secret = "bf_project_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-project-") as tmp:
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
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/project help", ctx)
            assert route == "local_command"
            for needle in ("/project new", "/project attach", "/project dashboard", "project_id"):
                assert_contains(help_text, needle)

            for bad_id in ("../bad", r"bad\path", "bad:path", "Bad"):
                rejected, route = bridge.prepare_reply(f"/project new {bad_id} 测试项目", ctx)
                assert route == "local_command"
                assert_contains(rejected, "拒绝")

            missing_project, route = bridge.prepare_reply("/task new 不应创建 --project missing_project", ctx)
            assert route == "local_command"
            assert_contains(missing_project, "项目不存在")
            if list(bridge.TASKS_DIR.glob("*.md")):
                raise AssertionError("task was created for missing project")

            created_project, route = bridge.prepare_reply("/project new kiro_proxy Kiro 反代项目", ctx)
            assert route == "local_command"
            assert_contains(created_project, "项目已创建：kiro_proxy")
            project_file = bridge.PROJECTS_DIR / "kiro_proxy.md"
            if not project_file.exists():
                raise AssertionError("project file was not created")
            project_text = project_file.read_text(encoding="utf-8")
            for needle in ("status: active", "priority: P2", "owner: 小小", "## Active Tasks"):
                assert_contains(project_text, needle)

            listed, route = bridge.prepare_reply("/project list", ctx)
            assert route == "local_command"
            assert_contains(listed, "kiro_proxy")
            assert_contains(listed, "Kiro 反代项目")

            shown, route = bridge.prepare_reply("/project show kiro_proxy", ctx)
            assert route == "local_command"
            assert_contains(shown, "项目摘要：kiro_proxy")
            assert_contains(shown, "active_tasks")

            updated_status, route = bridge.prepare_reply("/project set kiro_proxy status paused", ctx)
            assert route == "local_command"
            assert_contains(updated_status, "status：paused")
            updated_priority, route = bridge.prepare_reply("/project set kiro_proxy priority P1", ctx)
            assert route == "local_command"
            assert_contains(updated_priority, "priority：P1")
            bridge.prepare_reply("/project set kiro_proxy status active", ctx)

            note_reply, route = bridge.prepare_reply(
                f"/project note kiro_proxy 单行备注需要脱敏 {secret}",
                ctx,
            )
            assert route == "local_command"
            assert_contains(note_reply, "项目备注已追加")
            assert_not_contains(project_file.read_text(encoding="utf-8"), "bf_")

            created_task, route = bridge.prepare_reply(
                "/task new 检查 Kiro 反代当前状态 --project kiro_proxy",
                ctx,
            )
            assert route == "local_command"
            first_task_id = extract_task_id(created_task)
            first_task_file = bridge.TASKS_DIR / f"{first_task_id}.md"
            first_task_text = first_task_file.read_text(encoding="utf-8")
            assert_contains(first_task_text, "project_id: kiro_proxy")
            assert_contains(project_file.read_text(encoding="utf-8"), first_task_id)

            task_show, route = bridge.prepare_reply(f"/task show {first_task_id}", ctx)
            assert route == "local_command"
            assert_contains(task_show, "project：kiro_proxy")

            other_project, route = bridge.prepare_reply("/project new other_project 另一个项目", ctx)
            assert route == "local_command"
            assert_contains(other_project, "项目已创建：other_project")
            conflict, route = bridge.prepare_reply(f"/project attach other_project {first_task_id}", ctx)
            assert route == "local_command"
            assert_contains(conflict, "拒绝")
            assert_contains(conflict, "kiro_proxy")
            assert_contains(first_task_file.read_text(encoding="utf-8"), "project_id: kiro_proxy")

            unassigned_reply, route = bridge.prepare_reply("/task new 已有任务用于 attach", ctx)
            assert route == "local_command"
            second_task_id = extract_task_id(unassigned_reply)

            attach_reply, route = bridge.prepare_reply(f"/project attach kiro_proxy {second_task_id}", ctx)
            assert route == "local_command"
            assert_contains(attach_reply, "任务已关联项目")
            bridge.prepare_reply(f"/project attach kiro_proxy {second_task_id}", ctx)
            project_text = project_file.read_text(encoding="utf-8")
            if project_text.count(second_task_id) != 1:
                raise AssertionError("duplicate task_id was added to project Active Tasks")

            project_tasks, route = bridge.prepare_reply("/project tasks kiro_proxy", ctx)
            assert route == "local_command"
            assert_contains(project_tasks, first_task_id)
            assert_contains(project_tasks, second_task_id)

            project_brief, route = bridge.prepare_reply("/project brief kiro_proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "项目简报：kiro_proxy")
            assert_contains(project_brief, "活跃任务")

            dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "Atlas 跨项目看板")
            assert_contains(dashboard, "kiro_proxy")

            daily, route = bridge.prepare_reply("/daily brief", ctx)
            assert route == "local_command"
            assert_contains(daily, "项目：kiro_proxy")
            assert_contains(daily, first_task_id)

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

            assert_no_secret_in_tree(bridge.PROJECTS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.TASKS_DIR, "bf_")
            log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
            assert_not_contains(log_text, "bf_")
            reset_logger()

        print("smoke_project: OK")
        return 0
    finally:
        bridge.WORKBENCH_DIR = old_paths["WORKBENCH_DIR"]
        bridge.TASKS_DIR = old_paths["TASKS_DIR"]
        bridge.PROJECTS_DIR = old_paths["PROJECTS_DIR"]
        bridge.EVIDENCE_DIR = old_paths["EVIDENCE_DIR"]
        bridge.RETROS_DIR = old_paths["RETROS_DIR"]
        bridge.LEARNING_DIR = old_paths["LEARNING_DIR"]
        bridge.LEARNING_CANDIDATES_DIR = old_paths["LEARNING_CANDIDATES_DIR"]
        bridge.LEARNING_PROPOSALS_DIR = old_paths["LEARNING_PROPOSALS_DIR"]
        bridge.LEARNING_REGISTRY_DIR = old_paths["LEARNING_REGISTRY_DIR"]
        bridge.LEARNING_REJECTED_DIR = old_paths["LEARNING_REJECTED_DIR"]
        bridge.LEARNING_DEFERRED_DIR = old_paths["LEARNING_DEFERRED_DIR"]
        bridge.LEARNING_PACKAGES_DIR = old_paths["LEARNING_PACKAGES_DIR"]
        bridge.LEARNING_LOGS_DIR = old_paths["LEARNING_LOGS_DIR"]
        bridge.DECISIONS_DIR = old_paths["DECISIONS_DIR"]
        bridge.DAILY_DIR = old_paths["DAILY_DIR"]
        bridge.ARCHIVE_DIR = old_paths["ARCHIVE_DIR"]
        bridge.LOG_DIR = old_paths["LOG_DIR"]
        bridge.LOG_FILE = old_paths["LOG_FILE"]
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
