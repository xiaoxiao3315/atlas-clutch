from __future__ import annotations

import json
import logging
import os
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


def main() -> int:
    old_token = os.environ.get("OCTO_BOT_TOKEN")
    old_log_dir = bridge.LOG_DIR
    old_log_file = bridge.LOG_FILE
    old_rotate_bytes = bridge.LOG_ROTATE_BYTES
    old_workbench_dir = bridge.WORKBENCH_DIR
    old_tasks_dir = bridge.TASKS_DIR
    old_projects_dir = bridge.PROJECTS_DIR
    old_evidence_dir = bridge.EVIDENCE_DIR
    old_retros_dir = bridge.RETROS_DIR
    old_decisions_dir = bridge.DECISIONS_DIR
    old_daily_dir = bridge.DAILY_DIR
    old_archive_dir = bridge.ARCHIVE_DIR

    full_token = "bf_smoke_secret_token_123456"
    full_robot_id = "robot-full-id-1234567890"

    try:
        os.environ["OCTO_BOT_TOKEN"] = full_token

        with tempfile.TemporaryDirectory(prefix="ohb-smoke-") as tmp:
            root = Path(tmp)

            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            bridge.LOG_ROTATE_BYTES = 16
            bridge.WORKBENCH_DIR = root / "workbench"
            bridge.TASKS_DIR = bridge.WORKBENCH_DIR / "tasks"
            bridge.PROJECTS_DIR = bridge.WORKBENCH_DIR / "projects"
            bridge.EVIDENCE_DIR = bridge.WORKBENCH_DIR / "evidence"
            bridge.RETROS_DIR = bridge.WORKBENCH_DIR / "retros"
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR.mkdir()
            bridge.LOG_FILE.write_text("x" * 32, encoding="utf-8")
            reset_logger()
            bridge.setup_logging()
            bridge.log_event("secret_log_check", token=full_token)

            log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
            rotated_text = bridge.LOG_FILE.with_name("bridge.log.1").read_text(encoding="utf-8")
            assert_not_contains(log_text, full_token)
            assert_not_contains(log_text, "bf_")
            assert_contains(log_text, "[REDACTED]")
            assert_contains(rotated_text, "xxxxxxxx")
            reset_logger()

            runtime_dir = root / "runtime"
            guard = bridge.acquire_single_instance(runtime_dir=runtime_dir, run_id="run-smoke-1")
            try:
                blocked = False
                try:
                    bridge.acquire_single_instance(runtime_dir=runtime_dir, run_id="run-smoke-2")
                except bridge.AlreadyRunningError:
                    blocked = True
                if not blocked:
                    raise AssertionError("second bridge instance was not blocked")
            finally:
                guard.release()

            stale_payload = {
                "run_id": "stale",
                "pid": -1,
                "started_at": bridge.iso_now(),
                "script": "bridge.py",
                "cwd": str(root),
            }
            (runtime_dir / "bridge.lock").write_text(json.dumps(stale_payload), encoding="utf-8")
            (runtime_dir / "bridge.pid").write_text(json.dumps(stale_payload), encoding="utf-8")
            stale_guard = bridge.acquire_single_instance(runtime_dir=runtime_dir, run_id="run-smoke-3")
            stale_guard.release()

            runtime_info = {
                "run_id": "run-smoke-status",
                "pid": os.getpid(),
                "started_at": bridge.STARTED_AT_TEXT,
                "startup_method": "task",
                "lock_status": "held",
            }
            state = {
                bridge.PROCESSED_STATE_KEY: ["1:a", "2:b"],
                "last_error": f"failed with {full_token}",
            }
            heartbeat_path = runtime_dir / "heartbeat.json"
            heartbeat = bridge.write_heartbeat(
                runtime_info,
                state,
                last_seq=88,
                registered=True,
                robot_id=full_robot_id,
                owner_channel_id="owner-channel-123456",
                heartbeat_file=heartbeat_path,
            )

            heartbeat_text = heartbeat_path.read_text(encoding="utf-8")
            assert_not_contains(heartbeat_text, full_token)
            assert_not_contains(heartbeat_text, "bf_")
            assert_contains(heartbeat_text, "robot_id_masked")
            assert_contains(heartbeat_text, "run-smoke-status")

            context = {
                "registered": True,
                "robot_id": full_robot_id,
                "owner_channel_id": "owner-channel-123456",
                "last_seq": 88,
                "state": state,
                "runtime_info": runtime_info,
                "heartbeat": heartbeat,
            }
            status, route = bridge.prepare_reply("/status", context)
            assert route == "local_command"
            assert_contains(status, "run-smoke-status")
            assert_contains(status, "pid")
            assert_contains(status, "启动方式：task")
            assert_contains(status, "单实例锁：held")
            assert_not_contains(status, full_robot_id)
            assert_not_contains(status, full_token)
            assert_not_contains(status, "bf_")

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution requests must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("请执行 powershell Get-ChildItem 并提交代码", context)
            finally:
                bridge.call_hermes = original_call_hermes

            assert route == "work_order"
            assert_contains(work_order, "工作单")
            assert_contains(work_order, "不运行命令")
            assert_contains(work_order, "不修改文件")
            assert_not_contains(work_order, "已执行")

        print("smoke_runtime: OK")
        return 0
    finally:
        if old_token is None:
            os.environ.pop("OCTO_BOT_TOKEN", None)
        else:
            os.environ["OCTO_BOT_TOKEN"] = old_token
        bridge.LOG_DIR = old_log_dir
        bridge.LOG_FILE = old_log_file
        bridge.LOG_ROTATE_BYTES = old_rotate_bytes
        bridge.WORKBENCH_DIR = old_workbench_dir
        bridge.TASKS_DIR = old_tasks_dir
        bridge.PROJECTS_DIR = old_projects_dir
        bridge.EVIDENCE_DIR = old_evidence_dir
        bridge.RETROS_DIR = old_retros_dir
        bridge.DECISIONS_DIR = old_decisions_dir
        bridge.DAILY_DIR = old_daily_dir
        bridge.ARCHIVE_DIR = old_archive_dir
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
