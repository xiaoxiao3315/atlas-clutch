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
        "last_seq": 88,
        "state": {
            bridge.PROCESSED_STATE_KEY: [],
        },
        "runtime_info": {
            "run_id": "task-loop-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {
            "updated_at": "2026-06-06T18:30:00+08:00",
        },
    }


def main() -> int:
    old_paths = {
        "WORKBENCH_DIR": bridge.WORKBENCH_DIR,
        "TASKS_DIR": bridge.TASKS_DIR,
        "PROJECTS_DIR": bridge.PROJECTS_DIR,
        "EVIDENCE_DIR": bridge.EVIDENCE_DIR,
        "RETROS_DIR": bridge.RETROS_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }

    secret = "bf_task_loop_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-task-loop-") as tmp:
            root = Path(tmp)
            bridge.WORKBENCH_DIR = root / "workbench"
            bridge.TASKS_DIR = bridge.WORKBENCH_DIR / "tasks"
            bridge.PROJECTS_DIR = bridge.WORKBENCH_DIR / "projects"
            bridge.EVIDENCE_DIR = bridge.WORKBENCH_DIR / "evidence"
            bridge.RETROS_DIR = bridge.WORKBENCH_DIR / "retros"
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/task help", ctx)
            assert route == "local_command"
            assert_contains(help_text, "/task new")
            assert_contains(help_text, "/daily brief")

            created, route = bridge.prepare_reply("/task new 检查 Kiro 反代当前状态", ctx)
            assert route == "local_command"
            assert_contains(created, "任务已创建")
            task_id_match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", created)
            if not task_id_match:
                raise AssertionError("task_id was not returned")
            task_id = task_id_match.group(0)
            task_file = bridge.TASKS_DIR / f"{task_id}.md"
            if not task_file.exists():
                raise AssertionError("task file was not created")
            task_text = task_file.read_text(encoding="utf-8")
            for needle in ("status: open", "## Goal", "## Execution Boundary", "## Evidence Required"):
                assert_contains(task_text, needle)

            listed, route = bridge.prepare_reply("/task list", ctx)
            assert route == "local_command"
            assert_contains(listed, task_id)
            assert_contains(listed, "open")

            shown, route = bridge.prepare_reply(f"/task show {task_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "任务摘要")
            assert_contains(shown, "等待 Codex/Kiro 回传报告")

            report_body = f"""我已经完成了。
Authorization: Bearer {secret}
Cookie: session={secret}
password: hunter2
api_key: sk-task-loop-secret
secret: raw-secret-value
"""
            reported, route = bridge.prepare_reply(f"/task report {task_id}\n{report_body}", ctx)
            assert route == "local_command"
            assert_contains(reported, "status：reported")
            task_text = task_file.read_text(encoding="utf-8")
            assert_contains(task_text, "## Execution Report")
            assert_contains(task_text, "[REDACTED")
            assert_not_contains(task_text, "bf_")
            assert_not_contains(task_text, "sk-task-loop-secret")
            assert_not_contains(task_text, "hunter2")
            assert_not_contains(task_text, "raw-secret-value")

            reviewed, route = bridge.prepare_reply(f"/task review {task_id}", ctx)
            assert route == "local_command"
            for needle in ("已验证", "未验证", "风险", "待补证据", "下一步建议"):
                assert_contains(reviewed, needle)
            assert_contains(reviewed, "不能判定完成")
            assert_not_contains(reviewed, "验收通过")
            task_text = task_file.read_text(encoding="utf-8")
            assert_contains(task_text, "status: reviewed")

            decided, route = bridge.prepare_reply(
                f"/task decide {task_id} needs_evidence 需要补真实上游请求证据 {secret}",
                ctx,
            )
            assert route == "local_command"
            assert_contains(decided, "status：needs_evidence")
            task_text = task_file.read_text(encoding="utf-8")
            assert_contains(task_text, "status: needs_evidence")
            assert_not_contains(task_text, "bf_")

            brief, route = bridge.prepare_reply("/daily brief", ctx)
            assert route == "local_command"
            assert_contains(brief, "Atlas 今日简报")
            assert_contains(brief, task_id)
            assert_contains(brief, "needs_evidence")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "open_tasks")
            assert_contains(status, "needs_evidence_tasks")
            assert_contains(status, task_id)

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

            log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
            assert_not_contains(log_text, "bf_")
            for file_path in bridge.TASKS_DIR.glob("*.md"):
                assert_not_contains(file_path.read_text(encoding="utf-8"), "bf_")
            reset_logger()

        print("smoke_task_loop: OK")
        return 0
    finally:
        bridge.WORKBENCH_DIR = old_paths["WORKBENCH_DIR"]
        bridge.TASKS_DIR = old_paths["TASKS_DIR"]
        bridge.PROJECTS_DIR = old_paths["PROJECTS_DIR"]
        bridge.EVIDENCE_DIR = old_paths["EVIDENCE_DIR"]
        bridge.RETROS_DIR = old_paths["RETROS_DIR"]
        bridge.DECISIONS_DIR = old_paths["DECISIONS_DIR"]
        bridge.DAILY_DIR = old_paths["DAILY_DIR"]
        bridge.ARCHIVE_DIR = old_paths["ARCHIVE_DIR"]
        bridge.LOG_DIR = old_paths["LOG_DIR"]
        bridge.LOG_FILE = old_paths["LOG_FILE"]
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
