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
        "last_seq": 100,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "handoff-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-06T19:30:00+08:00"},
    }


def extract_task_id(text: str) -> str:
    match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("task_id missing")
    return match.group(0)


def assert_handoff_shape(text: str, task_id: str, platform: str) -> None:
    for needle in (
        f"执行对象：{platform}",
        f"task_id：{task_id}",
        "目标",
        "范围",
        "执行边界",
        "禁止事项",
        "建议检查步骤",
        "验收标准",
        "回传报告格式",
        "敏感信息处理要求",
        "用户最终验收要求",
    ):
        assert_contains(text, needle)
    for sensitive in ("bf_", "sk-", "Authorization", "Cookie"):
        assert_not_contains(text, sensitive)
    assert_not_contains(text, "已执行")


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

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-handoff-") as tmp:
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

            missing, route = bridge.prepare_reply("/task handoff OHB-20260606-000000 codex", ctx)
            assert route == "local_command"
            assert_contains(missing, "任务不存在")

            created, route = bridge.prepare_reply("/task new 检查 Kiro 反代当前状态", ctx)
            assert route == "local_command"
            task_id = extract_task_id(created)

            next_open, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_open, "status：open")
            assert_contains(next_open, "/task handoff")

            codex_handoff, route = bridge.prepare_reply(f"/task handoff {task_id} codex", ctx)
            assert route == "local_command"
            assert_handoff_shape(codex_handoff, task_id, "Codex")

            kiro_handoff, route = bridge.prepare_reply(f"/task handoff {task_id} kiro", ctx)
            assert route == "local_command"
            assert_handoff_shape(kiro_handoff, task_id, "Kiro")

            bad_platform, route = bridge.prepare_reply(f"/task handoff {task_id} atlas", ctx)
            assert route == "local_command"
            assert_contains(bad_platform, "用法")

            qa_empty, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            assert_contains(qa_empty, "质检结论：needs_evidence")
            assert_contains(qa_empty, "Execution Report")

            incomplete_report = "修改文件：bridge.py\n说明：只写了结论，没有命令和测试。"
            reported, route = bridge.prepare_reply(f"/task report {task_id}\n{incomplete_report}", ctx)
            assert route == "local_command"
            assert_contains(reported, "status：reported")

            qa_incomplete, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            assert_contains(qa_incomplete, "质检结论：needs_evidence")
            assert_contains(qa_incomplete, "缺失项")

            next_reported, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_reported, "status：reported")
            assert_contains(next_reported, "/task qa")
            assert_contains(next_reported, "/task review")

            full_report = """修改文件 / Modified files:
- bridge.py

执行命令 / Commands:
- python -m py_compile bridge.py
- python smoke_handoff.py

测试结果 / Test results:
- 通过：py_compile
- 通过：smoke_handoff

关键日志或截图 / Logs:
- logs/bridge.log tail checked

未验证 / Unverified:
- Octo UI live 回归未运行

未解决风险 / Unresolved risks:
- 需要用户在 Octo UI 验收
"""
            bridge.prepare_reply(f"/task report {task_id}\n{full_report}", ctx)
            qa_full, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            assert_contains(qa_full, "质检结论：needs_evidence")
            assert_contains(qa_full, "recommendation：needs_evidence")
            assert_contains(qa_full, "live UI 验收跳过")

            reviewed, route = bridge.prepare_reply(f"/task review {task_id}", ctx)
            assert route == "local_command"
            assert_contains(reviewed, "状态：reviewed")

            next_reviewed, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_reviewed, "status：reviewed")
            assert_contains(next_reviewed, "/task decide")

            decided_needs, route = bridge.prepare_reply(
                f"/task decide {task_id} needs_evidence 需要补真实上游请求证据",
                ctx,
            )
            assert route == "local_command"
            assert_contains(decided_needs, "status：needs_evidence")

            next_needs, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_needs, "status：needs_evidence")
            assert_contains(next_needs, "/task report")

            bridge.prepare_reply(f"/task decide {task_id} pass 验收通过", ctx)
            next_passed, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_passed, "status：passed")
            assert_contains(next_passed, "/task close")

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

            for file_path in bridge.TASKS_DIR.glob("*.md"):
                task_text = file_path.read_text(encoding="utf-8")
                for sensitive in ("bf_", "sk-", "Authorization", "Cookie"):
                    assert_not_contains(task_text, sensitive)
            if bridge.LOG_FILE.exists():
                log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
                assert_not_contains(log_text, "bf_")
            reset_logger()

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

    print("smoke_handoff: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
