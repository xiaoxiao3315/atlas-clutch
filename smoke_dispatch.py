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
        "robot_id": "robot-dispatch-smoke-123456",
        "owner_channel_id": "owner-dispatch-smoke-123456",
        "last_seq": 260,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "dispatch-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T18:00:00+08:00"},
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


def extract_context_id(text: str) -> str:
    return extract_id(r"CTX-\d{8}-\d{6}(?:-\d{2})?", text, "context_id")


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    if not root.exists():
        return
    for file_path in root.rglob("*"):
        if file_path.is_file():
            assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


def complete_report(secret: str) -> str:
    return f"""Task id: test
Dispatch id: test

Execution summary:
- Manual executor report only. No Bridge command execution.

Modified files:
- bridge.py
- README.md
- smoke_dispatch.py

Commands:
- python -m py_compile bridge.py
- python smoke_dispatch.py

Test results:
- passed: py_compile
- passed: smoke_dispatch

Key logs or screenshots:
- logs/bridge.log checked for token hits

Unverified:
- Octo UI live was not run

Unresolved risks:
- live evidence still needs manual confirmation

Rollback notes:
- revert the dispatch-related local code changes if needed

Token sample: {secret}
Authorization: bearer {secret}
Cookie: session={secret}
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
        "APPLICATIONS_DIR": bridge.APPLICATIONS_DIR,
        "PLAYBOOKS_DIR": bridge.PLAYBOOKS_DIR,
        "PROJECT_PLAYBOOKS_DIR": bridge.PROJECT_PLAYBOOKS_DIR,
        "CONTEXT_PACKS_DIR": bridge.CONTEXT_PACKS_DIR,
        "DISPATCHES_DIR": bridge.DISPATCHES_DIR,
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }
    secret = "bf_dispatch_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-dispatch-", ignore_cleanup_errors=True) as tmp:
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
            bridge.APPLICATIONS_DIR = bridge.WORKBENCH_DIR / "applications"
            bridge.PLAYBOOKS_DIR = bridge.WORKBENCH_DIR / "playbooks"
            bridge.PROJECT_PLAYBOOKS_DIR = bridge.PLAYBOOKS_DIR / "projects"
            bridge.CONTEXT_PACKS_DIR = bridge.WORKBENCH_DIR / "context_packs"
            bridge.DISPATCHES_DIR = bridge.WORKBENCH_DIR / "dispatches"
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/dispatch help", ctx)
            assert route == "local_command"
            for needle in ("/dispatch create", "/dispatch package", "/dispatch qa", "does not call Codex/Kiro"):
                assert_contains(help_text, needle)

            missing, route = bridge.prepare_reply("/dispatch create OHB-20260607-000000 codex", ctx)
            assert route == "local_command"
            assert_contains(missing, "not found")

            bridge.prepare_reply("/project new kiro_proxy Kiro proxy project", ctx)
            task_reply, route = bridge.prepare_reply("/task new Check Kiro reverse proxy status --project kiro_proxy", ctx)
            assert route == "local_command"
            task_id = extract_task_id(task_reply)

            next_without_dispatch, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(next_without_dispatch, f"/dispatch create {task_id} codex --with-context")

            codex_create, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            assert route == "local_command"
            codex_dispatch = extract_dispatch_id(codex_create)
            assert_contains(codex_create, "target_executor: codex")
            codex_file = bridge.DISPATCHES_DIR / f"{codex_dispatch}.md"
            codex_text = codex_file.read_text(encoding="utf-8")
            for needle in (
                "status: ready",
                "mode: manual",
                "external_execution_enabled: false",
                "runtime_injection_enabled: false",
                "## Do Not Auto-Execute",
            ):
                assert_contains(codex_text, needle)

            kiro_create, route = bridge.prepare_reply(f"/dispatch create {task_id} kiro", ctx)
            assert route == "local_command"
            kiro_dispatch = extract_dispatch_id(kiro_create)
            assert_contains(kiro_create, "target_executor: kiro")

            context_create, route = bridge.prepare_reply(f"/dispatch create {task_id} codex --with-context", ctx)
            assert route == "local_command"
            context_dispatch = extract_dispatch_id(context_create)
            context_id = extract_context_id(context_create)
            if not (bridge.CONTEXT_PACKS_DIR / f"{context_id}.md").exists():
                raise AssertionError("context pack not created")

            listed, route = bridge.prepare_reply("/dispatch list", ctx)
            assert route == "local_command"
            assert_contains(listed, codex_dispatch)
            filtered, route = bridge.prepare_reply("/dispatch list --status ready", ctx)
            assert route == "local_command"
            assert_contains(filtered, context_dispatch)

            shown, route = bridge.prepare_reply(f"/dispatch show {context_dispatch}", ctx)
            assert route == "local_command"
            assert_contains(shown, "Dispatch summary")
            assert_contains(shown, "target_executor: codex")

            package, route = bridge.prepare_reply(f"/dispatch package {context_dispatch}", ctx)
            assert route == "local_command"
            for needle in (
                "Manual Dispatch Package for Codex",
                f"dispatch_id: {context_dispatch}",
                f"task_id: {task_id}",
                "## Goal",
                "## Scope",
                "## Execution Boundary",
                "## Acceptance Criteria",
                "## Return Report Format",
                "manual copy only",
            ):
                assert_contains(package, needle)
            for sensitive in ("bf_", "sk-", "Authorization:", "Cookie:"):
                assert_not_contains(package, sensitive)

            marked, route = bridge.prepare_reply(f"/dispatch mark {context_dispatch} sent copied manually", ctx)
            assert route == "local_command"
            assert_contains(marked, "status: sent")
            ready_next, route = bridge.prepare_reply(f"/task next {task_id}", ctx)
            assert route == "local_command"
            assert_contains(ready_next, "/dispatch receive")

            returned, route = bridge.prepare_reply(f"/dispatch receive {context_dispatch}\n{complete_report(secret)}", ctx)
            assert route == "local_command"
            assert_contains(returned, "status: returned")
            assert_contains(returned, "synced_task_report: true")
            dispatch_text = (bridge.DISPATCHES_DIR / f"{context_dispatch}.md").read_text(encoding="utf-8")
            task_text = (bridge.TASKS_DIR / f"{task_id}.md").read_text(encoding="utf-8")
            assert_contains(dispatch_text, "[REDACTED_TOKEN]")
            assert_contains(task_text, "[REDACTED_TOKEN]")
            assert_not_contains(dispatch_text, "bf_")
            assert_not_contains(task_text, "bf_")

            qa, route = bridge.prepare_reply(f"/dispatch qa {context_dispatch}", ctx)
            assert route == "local_command"
            assert_contains(qa, "Dispatch QA")
            assert_contains(qa, "task_id")
            assert_contains(qa, "recommendation")

            reviewed, route = bridge.prepare_reply(f"/task review {task_id}", ctx)
            assert route == "local_command"
            assert_contains(reviewed, "review")
            linked, route = bridge.prepare_reply(f"/dispatch link-review {context_dispatch}", ctx)
            assert route == "local_command"
            assert_contains(linked, "has_review: true")

            decided, route = bridge.prepare_reply(f"/task decide {task_id} pass User accepted with manual evidence {secret}", ctx)
            assert route == "local_command"
            assert_contains(decided, "status")
            closed_dispatch, route = bridge.prepare_reply(f"/dispatch close {context_dispatch}", ctx)
            assert route == "local_command"
            assert_contains(closed_dispatch, "status: closed")
            assert_contains(closed_dispatch, "evidence_closure_state: closed_with_evidence_gap_risk")
            assert_contains(closed_dispatch, "evidence_gap_risk: true")
            assert_contains(closed_dispatch, "live_skipped: true")
            bridge.prepare_reply(f"/task close {task_id}", ctx)

            cancelled, route = bridge.prepare_reply(f"/dispatch cancel {codex_dispatch} duplicate {secret}", ctx)
            assert route == "local_command"
            assert_contains(cancelled, "status: cancelled")
            failed, route = bridge.prepare_reply(f"/dispatch fail {kiro_dispatch} executor unavailable {secret}", ctx)
            assert route == "local_command"
            assert_contains(failed, "status: failed")
            close_cancelled, route = bridge.prepare_reply(f"/dispatch close {codex_dispatch}", ctx)
            assert route == "local_command"
            assert_contains(close_cancelled, "status: closed")

            stale_create, route = bridge.prepare_reply(f"/dispatch create {task_id} codex", ctx)
            stale_dispatch = extract_dispatch_id(stale_create)
            bridge.prepare_reply(f"/dispatch mark {stale_dispatch} sent stale setup", ctx)
            stale_file = bridge.DISPATCHES_DIR / f"{stale_dispatch}.md"
            stale_text = stale_file.read_text(encoding="utf-8")
            stale_text = stale_text.replace("sent_at: " + bridge.task_metadata(stale_text).get("sent_at", ""), "sent_at: 2026-06-06T00:00:00+08:00")
            stale_text = bridge.replace_task_field(stale_text, "updated_at", "2026-06-06T00:00:00+08:00")
            stale_file.write_text(stale_text, encoding="utf-8")
            stale, route = bridge.prepare_reply("/dispatch stale", ctx)
            assert route == "local_command"
            assert_contains(stale, stale_dispatch)

            dashboard, route = bridge.prepare_reply("/dispatch dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "dispatch_ready_count")
            assert_contains(dashboard, "dispatch_stale_count")

            project_brief, route = bridge.prepare_reply("/project brief kiro_proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "Dispatch view")
            assert_contains(project_brief, "returned_pending_qa")

            project_dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(project_dashboard, "Dispatch summary")
            assert_contains(project_dashboard, "dispatch_sent_count")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "dispatch_count")
            assert_contains(status, "dispatch_sent")
            assert_contains(status, "external_execution_enabled: false")

            task_show, route = bridge.prepare_reply(f"/task show {task_id}", ctx)
            assert route == "local_command"
            assert_contains(task_show, "latest_dispatch_id")
            assert_contains(task_show, "dispatch_status")
            assert_contains(task_show, "target_executor")

            exec_request, route = bridge.prepare_reply(r"帮我执行 dir E:\ai", ctx)
            assert route == "work_order"
            assert_contains(exec_request, "dir E")

            assert_no_secret_in_tree(bridge.WORKBENCH_DIR, "bf_")
            assert_no_secret_in_tree(bridge.LOG_DIR, "bf_")

    finally:
        for name, value in old_paths.items():
            setattr(bridge, name, value)
        reset_logger()

    print("smoke_dispatch passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
