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
        "last_seq": 150,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "learn-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T13:00:00+08:00"},
    }


def extract_task_id(text: str) -> str:
    match = re.search(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("task_id missing")
    return match.group(0)


def extract_learn_id(text: str) -> str:
    match = re.search(r"LEARN-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("learn_id missing")
    return match.group(0)


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    for file_path in root.rglob("*.md"):
        assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


def report(secret: str) -> str:
    return f"""执行摘要：
- 本地 smoke 已通过，Octo UI live 回归未运行，待补。

修改文件：
- bridge.py

执行命令：
- python smoke_learn.py

测试结果：
- 通过：smoke_learn.py

关键日志或截图：
- logs\\bridge.log 无 bf_ 命中

未验证：
- Octo UI live 回归未运行

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

    secret = "bf_learn_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-learn-") as tmp:
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

            help_text, route = bridge.prepare_reply("/learn help", ctx)
            assert route == "local_command"
            for needle in ("不是模型训练", "不自动改 Hermes", "不写 Memory", "workbench learning registry"):
                assert_contains(help_text, needle)

            bridge.prepare_reply("/project new kiro-proxy Kiro 反代项目", ctx)
            task_reply, route = bridge.prepare_reply("/task new 检查 Kiro 反代当前状态 --project kiro-proxy", ctx)
            assert route == "local_command"
            task_id = extract_task_id(task_reply)
            bridge.prepare_reply(f"/task report {task_id}\n{report(secret)}", ctx)
            bridge.prepare_reply(f"/task review {task_id}", ctx)
            bridge.prepare_reply(f"/task decide {task_id} needs_evidence 需要补真实上游请求证据 {secret}", ctx)
            retro_created, route = bridge.prepare_reply(f"/retro create {task_id}", ctx)
            assert route == "local_command"
            assert_contains(retro_created, "复盘已创建")
            bridge.prepare_reply(f"/retro approve {task_id} 复盘确认，只写入 workbench，不写 Memory {secret}", ctx)

            scan, route = bridge.prepare_reply(f"/learn scan retro {task_id}", ctx)
            assert route == "local_command"
            assert_contains(scan, "候选学习项")
            assert_contains(scan, "Candidate")

            proposed, route = bridge.prepare_reply(f"/learn propose retro {task_id}", ctx)
            assert route == "local_command"
            learn_id = extract_learn_id(proposed)
            proposal_file = bridge.LEARNING_PROPOSALS_DIR / f"{learn_id}.md"
            proposal_text = proposal_file.read_text(encoding="utf-8")
            assert_contains(proposal_text, "## Do Not Auto-Apply")
            assert_contains(proposal_text, "Application Status: not_applied")
            assert_not_contains(proposal_text, "bf_")

            listed, route = bridge.prepare_reply("/learn list", ctx)
            assert route == "local_command"
            assert_contains(listed, learn_id)

            shown, route = bridge.prepare_reply(f"/learn show {learn_id}", ctx)
            assert route == "local_command"
            for needle in ("Learning proposal", "proposed_behavior_change", "application_status"):
                assert_contains(shown, needle)

            reviewed, route = bridge.prepare_reply(f"/learn review {learn_id}", ctx)
            assert route == "local_command"
            assert_contains(reviewed, "建议决策")
            assert_contains(reviewed, "application_enabled: false")

            approved, route = bridge.prepare_reply(
                f"/learn approve {learn_id} 批准进入本地 learning registry，但不应用 {secret}",
                ctx,
            )
            assert route == "local_command"
            assert_contains(approved, "已批准但未应用")
            assert_contains(approved, "application_status：not_applied")
            registry_file = bridge.LEARNING_REGISTRY_DIR / f"{learn_id}.md"
            if not registry_file.exists():
                raise AssertionError("registry file was not created")
            assert_contains(registry_file.read_text(encoding="utf-8"), "application_status: not_applied")
            assert_not_contains(registry_file.read_text(encoding="utf-8"), "bf_")

            registry, route = bridge.prepare_reply("/learn registry", ctx)
            assert route == "local_command"
            assert_contains(registry, learn_id)
            assert_contains(registry, "not_applied")

            packaged, route = bridge.prepare_reply(f"/learn package {learn_id}", ctx)
            assert route == "local_command"
            assert_contains(packaged, "not applied automatically")
            package_file = bridge.LEARNING_PACKAGES_DIR / f"{learn_id}.md"
            if not package_file.exists():
                raise AssertionError("learning package was not created")
            assert_contains(package_file.read_text(encoding="utf-8"), "not applied automatically")

            manual_reject, route = bridge.prepare_reply("/learn propose manual 拒绝测试 proposal", ctx)
            reject_id = extract_learn_id(manual_reject)
            rejected, route = bridge.prepare_reply(f"/learn reject {reject_id} 证据不足 {secret}", ctx)
            assert route == "local_command"
            assert_contains(rejected, "status：rejected")
            if not (bridge.LEARNING_REJECTED_DIR / f"{reject_id}.md").exists():
                raise AssertionError("rejected copy was not created")

            manual_defer, route = bridge.prepare_reply("/learn propose manual 延后测试 proposal", ctx)
            defer_id = extract_learn_id(manual_defer)
            deferred, route = bridge.prepare_reply(f"/learn defer {defer_id} 等待更多证据 {secret}", ctx)
            assert route == "local_command"
            assert_contains(deferred, "status：deferred")
            if not (bridge.LEARNING_DEFERRED_DIR / f"{defer_id}.md").exists():
                raise AssertionError("deferred copy was not created")

            dashboard, route = bridge.prepare_reply("/learn dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "proposals")
            assert_contains(dashboard, "not_applied")

            status, route = bridge.prepare_reply("/learn status", ctx)
            assert route == "local_command"
            assert_contains(status, "application_enabled: false")

            bridge_status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(bridge_status, "learning_proposals")
            assert_contains(bridge_status, "application_enabled：false")

            retro_dashboard, route = bridge.prepare_reply("/retro dashboard", ctx)
            assert route == "local_command"
            assert_contains(retro_dashboard, "proposed_learning_count")
            assert_contains(retro_dashboard, "not_applied_learning_count")

            project_brief, route = bridge.prepare_reply("/project brief kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "Learning 视角")
            assert_contains(project_brief, "learning_proposal_count")

            project_dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(project_dashboard, "learning_proposal_count")
            assert_contains(project_dashboard, "not_applied_learning_count")

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

            assert_no_secret_in_tree(bridge.LEARNING_DIR, "bf_")
            assert_no_secret_in_tree(bridge.RETROS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.TASKS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.PROJECTS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.EVIDENCE_DIR, "bf_")
            assert_not_contains(bridge.LOG_FILE.read_text(encoding="utf-8"), "bf_")
            reset_logger()

        print("smoke_learn: OK")
        return 0
    finally:
        for name, value in old_paths.items():
            setattr(bridge, name, value)
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
