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
        "robot_id": "robot-auto-evidence-123456",
        "owner_channel_id": "owner-auto-evidence-123456",
        "last_seq": 420,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "auto-evidence-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T20:00:00+08:00"},
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


def read_only_kiro_report() -> str:
    return """Execution summary:
- Read-only validation for Kiro reverse proxy chat path.
- 只读检查，未修改文件，未改代码，未提交，未读取 .env。

Modified files:
- N/A

修改文件：
- 无

Commands:
- Invoke-RestMethod POST http://127.0.0.1:8080/v1/chat/completions stream=false
- Invoke-RestMethod POST http://127.0.0.1:8080/v1/chat/completions stream=true
- Select-String gateway.log request_id
- Get-Process -Id 17848

Test results:
- stream=false status: 200 response contains KIROPILOT-OK
- stream=true status: 200 response contains KIROPILOT-OK

Key logs or screenshots:
- gateway.log request_id=req_autoevidence_false stream=false status=success_non_stream
- gateway.log request_id=req_autoevidence_true stream=true status=success_stream
- acp_raw.log contains KIROPILOT-OK

Process evidence:
- PID 17848 was checked and appears stale; no kill was performed.

Unverified:
- none

Unresolved risks:
- none

Sensitive scan:
- bf_: 0 hits
- Authorization: 0 命中
- Cookie: 0 命中
- sk-: 0

Rollback notes:
- No changes were made.
"""


def assert_no_secret_in_tree(root: Path) -> None:
    forbidden = ("bf_", "sk-super-secret", "Authorization: Bearer", "Cookie:")
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        text = file_path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert_not_contains(text, needle)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-auto-evidence-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        try:
            ctx = context()

            bridge.prepare_reply("/project new kiro_proxy Kiro reverse proxy project", ctx)
            task_reply, route = bridge.prepare_reply(
                "/task new Read-only Kiro reverse proxy validation --project kiro_proxy",
                ctx,
            )
            assert route == "local_command"
            task_id = extract_task_id(task_reply)

            report = read_only_kiro_report()

            preview, route = bridge.prepare_reply(f"/evidence intake {task_id}\n{report}", ctx)
            assert route == "local_command"
            for needle in (
                "Evidence intake preview",
                "evidence_type: api",
                "read_only_mode: true",
                "no_modification_ok: true",
                "sensitive_risk: false",
                "sensitive_zero_hit_ok: true",
                "recommendation: pass_candidate",
            ):
                assert_contains(preview, needle)
            assert_contains(preview, "request_id")

            secret_header = "Authorization: Bearer abcdefghijklmnop"
            sensitive_preview, route = bridge.prepare_reply(f"/evidence intake {task_id}\n{secret_header}", ctx)
            assert route == "local_command"
            assert_contains(sensitive_preview, "sensitive_risk: true")
            assert_contains(sensitive_preview, "recommendation: blocked")
            assert_not_contains(sensitive_preview, "abcdefghijklmnop")

            secret_key = "sk-super-secret-1234567890"
            key_preview, route = bridge.prepare_reply(f"/evidence intake {task_id}\napi_key: {secret_key}", ctx)
            assert route == "local_command"
            assert_contains(key_preview, "sensitive_risk: true")
            assert_not_contains(key_preview, secret_key)

            bridge.prepare_reply(f"/task handoff {task_id} codex", ctx)
            reported, route = bridge.prepare_reply(f"/task report {task_id}\n{report}", ctx)
            assert route == "local_command"
            assert_contains(reported, "type=api")
            assert_contains(reported, "auto_intake_recommendation: pass_candidate")
            assert_contains(reported, "read_only_mode: true")
            evidence_id = extract_evidence_id(reported)

            qa, route = bridge.prepare_reply(f"/task qa {task_id}", ctx)
            assert route == "local_command"
            for needle in (
                "质检结论：pass_candidate",
                "read_only_mode: true",
                "no_modification_ok: true",
                "sensitive_risk: false",
                "sensitive_risk_reason: zero-hit checks only",
                "evidence_type: api",
                "recommendation: pass_candidate",
            ):
                assert_contains(qa, needle)
            assert_not_contains(qa, "recommendation：needs_evidence")

            marked, route = bridge.prepare_reply(
                f"/evidence mark {task_id} {evidence_id} verified user checked Kiro read-only evidence",
                ctx,
            )
            assert route == "local_command"
            assert_contains(marked, "verified")

            gaps, route = bridge.prepare_reply(f"/evidence gaps {task_id}", ctx)
            assert route == "local_command"
            assert_contains(gaps, "read_only_no_modification_ok: true")
            assert_contains(gaps, "sensitive_zero_hit_ok: true")
            assert_contains(gaps, "remaining_gaps: none")
            assert_contains(gaps, "recommendation: ready_for_review")

            reviewed, route = bridge.prepare_reply(f"/task review {task_id}", ctx)
            assert route == "local_command"
            assert_contains(reviewed, "建议决策：pass")
            assert_not_contains(reviewed, "observed 尚未 verified")

            dispatch_task, route = bridge.prepare_reply(
                "/task new Dispatch Kiro read-only report --project kiro_proxy",
                ctx,
            )
            dispatch_task_id = extract_task_id(dispatch_task)
            dispatch_created, route = bridge.prepare_reply(f"/dispatch create {dispatch_task_id} codex", ctx)
            assert route == "local_command"
            dispatch_id = extract_dispatch_id(dispatch_created)
            returned, route = bridge.prepare_reply(f"/dispatch receive {dispatch_id}\n{report}", ctx)
            assert route == "local_command"
            assert_contains(returned, "status: returned")
            dispatch_qa, route = bridge.prepare_reply(f"/dispatch qa {dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(dispatch_qa, f"dispatch_id: {dispatch_id}")
            assert_contains(dispatch_qa, "status: qa_ready")
            assert_contains(dispatch_qa, "pass_candidate")
            assert_not_contains(dispatch_qa, "sensitive_risk: true")

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

            assert_no_secret_in_tree(bridge.WORKBENCH_DIR)
            if bridge.LOG_FILE.exists():
                log_text = bridge.LOG_FILE.read_text(encoding="utf-8")
                assert_not_contains(log_text, "bf_")
                assert_not_contains(log_text, "sk-super-secret")
                assert_not_contains(log_text, "Authorization: Bearer")
                assert_not_contains(log_text, "Cookie:")
        finally:
            restore_paths(old_paths)

    print("smoke_auto_evidence passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
