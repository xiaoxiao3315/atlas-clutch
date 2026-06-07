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
        "last_seq": 130,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "evidence-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T11:00:00+08:00"},
    }


def extract_task_id(text: str) -> str:
    match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("task_id missing")
    return match.group(0)


def extract_evidence_id(text: str) -> str:
    match = re.search(r"EV-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("evidence_id missing")
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

    secret = "bf_evidence_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-evidence-") as tmp:
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

            help_text, route = bridge.prepare_reply("/evidence help", ctx)
            assert route == "local_command"
            for needle in ("/evidence add", "/evidence list", "/evidence gaps", "report 不是 verified"):
                assert_contains(help_text, needle)

            project, route = bridge.prepare_reply("/project new kiro-proxy Kiro 反代项目", ctx)
            assert route == "local_command"
            assert_contains(project, "kiro-proxy")

            created, route = bridge.prepare_reply(
                "/task new 检查 Kiro 反代当前状态 --project kiro-proxy",
                ctx,
            )
            assert route == "local_command"
            task_id = extract_task_id(created)
            task_file = bridge.TASKS_DIR / f"{task_id}.md"

            evidence_body = f"""python smoke_project.py：通过
logs\\bridge.log 搜索 bf_：无结果
workbench\\projects\\*.md 搜索 bf_：无结果
Authorization: Bearer {secret}
"""
            added, route = bridge.prepare_reply(f"/evidence add {task_id} smoke\n{evidence_body}", ctx)
            assert route == "local_command"
            evidence_id = extract_evidence_id(added)
            evidence_file = bridge.EVIDENCE_DIR / f"{task_id}.md"
            if not evidence_file.exists():
                raise AssertionError("evidence file was not created")
            assert_contains(evidence_file.read_text(encoding="utf-8"), evidence_id)
            assert_not_contains(evidence_file.read_text(encoding="utf-8"), "bf_")
            assert_not_contains(evidence_file.read_text(encoding="utf-8"), secret)

            listed, route = bridge.prepare_reply(f"/evidence list {task_id}", ctx)
            assert route == "local_command"
            assert_contains(listed, evidence_id)
            assert_contains(listed, "type=smoke")

            shown, route = bridge.prepare_reply(f"/evidence show {task_id} {evidence_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "证据摘要")
            assert_contains(shown, "verified：no")

            gaps, route = bridge.prepare_reply(f"/evidence gaps {task_id}", ctx)
            assert route == "local_command"
            assert_contains(gaps, "missing")
            assert_contains(gaps, "observed 但未 verified")

            marked, route = bridge.prepare_reply(
                f"/evidence mark {task_id} {evidence_id} verified 本地 smoke 输出可复核",
                ctx,
            )
            assert route == "local_command"
            assert_contains(marked, "verified：verified")
            assert_contains(evidence_file.read_text(encoding="utf-8"), "verified: verified")

            report_body = f"""执行摘要：
- 本地 smoke 已通过，但 live Octo UI 回归未运行，待补。

修改文件：
- bridge.py
- smoke_evidence.py

执行命令：
- python -m py_compile bridge.py
- python smoke_evidence.py

测试结果：
- 通过：py_compile
- 通过：smoke_evidence.py

关键日志或截图：
- logs\\bridge.log 无 bf_ 命中

未验证：
- Octo UI live 回归未运行

未解决风险：
- live 验收跳过，需要用户补真实 Octo UI 证据

secret: {secret}
"""
            reported, route = bridge.prepare_reply(f"/task report {task_id}\n{report_body}", ctx)
            assert route == "local_command"
            assert_contains(reported, "自动证据")
            assert_contains(reported, "live_skipped：true")
            auto_evidence_id = extract_evidence_id(reported)
            assert_contains(evidence_file.read_text(encoding="utf-8"), auto_evidence_id)
            assert_not_contains(evidence_file.read_text(encoding="utf-8"), "bf_")

            qa, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            for needle in ("claimed", "observed", "missing", "sensitive_risk", "recommendation：needs_evidence"):
                assert_contains(qa, needle)

            reviewed, route = bridge.prepare_reply(f"/task review {task_id}", ctx)
            assert route == "local_command"
            for needle in ("已验证 verified", "已观察 observed", "仅声称 claimed", "缺失 missing", "风险 risk", "evidence gaps"):
                assert_contains(reviewed, needle)
            assert_contains(reviewed, "不能建议 pass")
            assert_contains(reviewed, "live 待补")
            assert_not_contains(reviewed, "建议决策：pass")

            decided, route = bridge.prepare_reply(f"/task decide {task_id} pass 用户强制记录通过", ctx)
            assert route == "local_command"
            assert_contains(decided, "status：passed")
            assert_contains(decided, "evidence_gap_risk=true")
            assert_contains(task_file.read_text(encoding="utf-8"), "evidence_gap_risk: true")
            assert_not_contains(task_file.read_text(encoding="utf-8"), "bf_")

            project_brief, route = bridge.prepare_reply("/project brief kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "evidence_gaps")
            assert_contains(project_brief, "live_skipped")

            dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "evidence_gap_count")
            assert_contains(dashboard, "live_skipped_count")

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

            assert_no_secret_in_tree(bridge.EVIDENCE_DIR, "bf_")
            assert_no_secret_in_tree(bridge.TASKS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.PROJECTS_DIR, "bf_")
            log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
            assert_not_contains(log_text, "bf_")
            reset_logger()

        print("smoke_evidence: OK")
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
