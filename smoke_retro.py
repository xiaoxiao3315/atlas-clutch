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
        "last_seq": 140,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "retro-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T12:00:00+08:00"},
    }


def extract_task_id(text: str) -> str:
    match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("task_id missing")
    return match.group(0)


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    for file_path in root.rglob("*.md"):
        assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


def make_report(secret: str, live_skipped: bool = True) -> str:
    live_line = "Octo UI live 回归未运行，待补。" if live_skipped else "Octo UI live 回归：通过。"
    return f"""执行摘要：
- 本地 smoke 已通过，{live_line}

修改文件：
- bridge.py

执行命令：
- python -m py_compile bridge.py
- python smoke_retro.py

测试结果：
- 通过：py_compile
- 通过：smoke_retro.py

关键日志或截图：
- logs\\bridge.log 无 bf_ 命中

未验证：
- {live_line}

未解决风险：
- live skipped 需要补真实 Octo UI 证据

secret: {secret}
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
        "LEARNING_PACKAGES_DIR": bridge.LEARNING_PACKAGES_DIR,
        "LEARNING_LOGS_DIR": bridge.LEARNING_LOGS_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }

    secret = "bf_retro_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-retro-") as tmp:
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

            help_text, route = bridge.prepare_reply("/retro help", ctx)
            assert route == "local_command"
            for needle in ("/retro create", "/retro approve", "不是 Agent 自训", "Memory/SkillRepo"):
                assert_contains(help_text, needle)

            project, route = bridge.prepare_reply("/project new kiro-proxy Kiro 反代项目", ctx)
            assert route == "local_command"
            assert_contains(project, "kiro-proxy")

            no_decision_reply, route = bridge.prepare_reply(
                "/task new 未决策任务 --project kiro-proxy",
                ctx,
            )
            no_decision_task_id = extract_task_id(no_decision_reply)
            blocked_retro, route = bridge.prepare_reply(f"/retro create {no_decision_task_id}", ctx)
            assert route == "local_command"
            assert_contains(blocked_retro, "暂不生成复盘")
            assert_contains(blocked_retro, "/task decide")
            if (bridge.RETROS_DIR / f"{no_decision_task_id}.md").exists():
                raise AssertionError("retro should not be created before user decision")

            needs_reply, route = bridge.prepare_reply(
                "/task new 检查 Kiro 反代当前状态 --project kiro-proxy",
                ctx,
            )
            needs_task_id = extract_task_id(needs_reply)
            bridge.prepare_reply(f"/task report {needs_task_id}\n{make_report(secret)}", ctx)
            bridge.prepare_reply(f"/task review {needs_task_id}", ctx)
            bridge.prepare_reply(
                f"/task decide {needs_task_id} needs_evidence 需要补真实上游请求证据 {secret}",
                ctx,
            )
            created_needs, route = bridge.prepare_reply(f"/retro create {needs_task_id}", ctx)
            assert route == "local_command"
            assert_contains(created_needs, "复盘已创建")
            needs_retro_file = bridge.RETROS_DIR / f"{needs_task_id}.md"
            needs_retro_text = needs_retro_file.read_text(encoding="utf-8")
            for needle in (
                f"task_id: {needs_task_id}",
                "project_id: kiro-proxy",
                "## Evidence Gaps",
                "## Lessons Learned",
                "## Candidate Improvements",
                "## Do Not Auto-Apply",
                "阻塞/待补证据复盘",
            ):
                assert_contains(needs_retro_text, needle)
            assert_not_contains(needs_retro_text, "retro_type: 已通过复盘")

            shown, route = bridge.prepare_reply(f"/retro show {needs_task_id}", ctx)
            assert route == "local_command"
            for needle in ("复盘摘要", "lessons_learned", "candidate_improvements"):
                assert_contains(shown, needle)

            listed, route = bridge.prepare_reply("/retro list", ctx)
            assert route == "local_command"
            assert_contains(listed, needs_task_id)

            listed_project, route = bridge.prepare_reply("/retro list --project kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(listed_project, needs_task_id)

            approved, route = bridge.prepare_reply(
                f"/retro approve {needs_task_id} 复盘确认，只写入 workbench，不写 Memory {secret}",
                ctx,
            )
            assert route == "local_command"
            assert_contains(approved, "status：approved")
            needs_retro_text = needs_retro_file.read_text(encoding="utf-8")
            assert_contains(needs_retro_text, "status: approved")
            project_text = (bridge.PROJECTS_DIR / "kiro-proxy.md").read_text(encoding="utf-8")
            assert_contains(project_text, "Lessons Learned")
            assert_not_contains(project_text, "bf_")

            archived, route = bridge.prepare_reply(f"/retro archive {needs_task_id}", ctx)
            assert route == "local_command"
            assert_contains(archived, "status：archived")
            assert_contains(needs_retro_file.read_text(encoding="utf-8"), "status: archived")

            passed_reply, route = bridge.prepare_reply(
                "/task new 已通过但 live 待补任务 --project kiro-proxy",
                ctx,
            )
            passed_task_id = extract_task_id(passed_reply)
            bridge.prepare_reply(f"/task report {passed_task_id}\n{make_report(secret)}", ctx)
            bridge.prepare_reply(f"/task review {passed_task_id}", ctx)
            bridge.prepare_reply(f"/task decide {passed_task_id} pass 用户强制记录通过 {secret}", ctx)
            closed, route = bridge.prepare_reply(f"/task close {passed_task_id}", ctx)
            assert route == "local_command"
            assert_contains(closed, "尚未生成")
            assert_contains(closed, f"/retro create {passed_task_id}")

            created_passed, route = bridge.prepare_reply(f"/retro create {passed_task_id}", ctx)
            assert route == "local_command"
            assert_contains(created_passed, "复盘已创建")
            passed_retro_text = (bridge.RETROS_DIR / f"{passed_task_id}.md").read_text(encoding="utf-8")
            assert_contains(passed_retro_text, "已通过复盘")
            assert_contains(passed_retro_text, "Evidence Gaps")
            assert_contains(passed_retro_text, "Octo UI live 验收")
            assert_not_contains(passed_retro_text, "bf_")

            project_retro, route = bridge.prepare_reply("/retro project kiro-proxy", ctx)
            assert route == "local_command"
            for needle in ("项目复盘摘要", "高频 evidence gaps", "Candidate Improvements"):
                assert_contains(project_retro, needle)

            dashboard, route = bridge.prepare_reply("/retro dashboard", ctx)
            assert route == "local_command"
            for needle in ("Atlas 复盘看板", "retro_count", "candidate_improvement_count"):
                assert_contains(dashboard, needle)

            project_brief, route = bridge.prepare_reply("/project brief kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "Retro 视角")
            assert_contains(project_brief, "候选改进项")

            project_dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(project_dashboard, "retro_count")
            assert_contains(project_dashboard, "candidate_improvement_count")

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

            assert_no_secret_in_tree(bridge.RETROS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.TASKS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.PROJECTS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.EVIDENCE_DIR, "bf_")
            log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
            assert_not_contains(log_text, "bf_")
            reset_logger()

        print("smoke_retro: OK")
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
