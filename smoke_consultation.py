from __future__ import annotations

import tempfile
from pathlib import Path

import bridge


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected text: {needle}")


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
    }

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-consultation-") as tmp:
            root = Path(tmp)
            bridge.WORKBENCH_DIR = root / "workbench"
            bridge.TASKS_DIR = bridge.WORKBENCH_DIR / "tasks"
            bridge.PROJECTS_DIR = bridge.WORKBENCH_DIR / "projects"
            bridge.EVIDENCE_DIR = bridge.WORKBENCH_DIR / "evidence"
            bridge.RETROS_DIR = bridge.WORKBENCH_DIR / "retros"
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"

            context = {
                "registered": True,
                "robot_id": "robot-1234567890",
                "owner_channel_id": "owner-1234567890",
                "last_seq": 42,
                "state": {
                    bridge.PROCESSED_STATE_KEY: ["1:a", "2:b"],
                },
            }

            status, route = bridge.prepare_reply("/status", context)
            assert route == "local_command"
            assert_contains(status, "Octo-Hermes Bridge 状态")
            assert_contains(status, "last_seq：42")
            assert_contains(status, "咨询/调度")

            help_text, route = bridge.prepare_reply("/help", context)
            assert route == "local_command"
            assert_contains(help_text, "/status")
            assert_contains(help_text, "/help")

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution requests must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("请执行 powershell Get-ChildItem，并修改文件", context)
            finally:
                bridge.call_hermes = original_call_hermes
    finally:
        bridge.WORKBENCH_DIR = old_paths["WORKBENCH_DIR"]
        bridge.TASKS_DIR = old_paths["TASKS_DIR"]
        bridge.PROJECTS_DIR = old_paths["PROJECTS_DIR"]
        bridge.EVIDENCE_DIR = old_paths["EVIDENCE_DIR"]
        bridge.RETROS_DIR = old_paths["RETROS_DIR"]
        bridge.DECISIONS_DIR = old_paths["DECISIONS_DIR"]
        bridge.DAILY_DIR = old_paths["DAILY_DIR"]
        bridge.ARCHIVE_DIR = old_paths["ARCHIVE_DIR"]

    assert route == "work_order"
    for heading in ("目标", "范围", "执行边界", "验收标准", "风险点"):
        assert_contains(work_order, heading)

    assert_contains(work_order, "不运行命令")
    assert_contains(work_order, "不修改文件")
    assert_contains(work_order, "没有可验证")
    assert_not_contains(work_order, "我是 Codex")
    assert_not_contains(work_order, "我是 OpenClaw")
    assert_not_contains(work_order, "任务已完成")
    assert_not_contains(work_order, "已执行")
    assert_not_contains(work_order, "已修改")

    print("smoke_consultation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
