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
        "robot_id": "robot-context-smoke-123456",
        "owner_channel_id": "owner-context-smoke-123456",
        "last_seq": 220,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "context-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T15:00:00+08:00"},
    }


def extract_id(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"{label} missing")
    return match.group(0)


def extract_task_id(text: str) -> str:
    return extract_id(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text, "task_id")


def extract_learn_id(text: str) -> str:
    return extract_id(r"LEARN-\d{8}-\d{6}(?:-\d{2})?", text, "learn_id")


def extract_apply_id(text: str) -> str:
    return extract_id(r"APPLY-\d{8}-\d{6}(?:-\d{2})?", text, "apply_id")


def extract_context_id(text: str) -> str:
    return extract_id(r"CTX-\d{8}-\d{6}(?:-\d{2})?", text, "context_id")


def assert_no_secret_in_tree(root: Path, needle: str) -> None:
    if not root.exists():
        return
    for file_path in root.rglob("*"):
        if file_path.is_file():
            assert_not_contains(file_path.read_text(encoding="utf-8"), needle)


def report(secret: str) -> str:
    return f"""Execution summary:
- Local smoke passed. Octo UI live was skipped.

Modified files:
- bridge.py

Commands:
- python smoke_context.py

Test results:
- passed: smoke_context.py

Key logs or screenshots:
- logs/bridge.log has no token hit

Unverified:
- Octo UI live was not run

Unresolved risks:
- live evidence still required

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
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }
    secret = "bf_context_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-context-", ignore_cleanup_errors=True) as tmp:
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
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/context help", ctx)
            assert route == "local_command"
            assert_contains(help_text, "Context Pack")
            assert_contains(help_text, "writes workbench/context_packs only")
            assert_contains(help_text, "does not execute commands")

            bridge.prepare_reply("/project new kiro-proxy Kiro reverse proxy project", ctx)
            task_reply, route = bridge.prepare_reply("/task new Check Kiro reverse proxy status --project kiro-proxy", ctx)
            assert route == "local_command"
            task_id = extract_task_id(task_reply)
            bridge.prepare_reply(f"/task handoff {task_id} codex", ctx)
            bridge.prepare_reply(f"/task report {task_id}\n{report(secret)}", ctx)
            bridge.prepare_reply(f"/task review {task_id}", ctx)
            bridge.prepare_reply(f"/task decide {task_id} needs_evidence Need live upstream evidence {secret}", ctx)
            bridge.prepare_reply(f"/retro create {task_id}", ctx)
            bridge.prepare_reply(f"/retro approve {task_id} Approved retro, workbench only {secret}", ctx)
            proposed, route = bridge.prepare_reply(f"/learn propose retro {task_id}", ctx)
            learn_id = extract_learn_id(proposed)
            bridge.prepare_reply(f"/learn approve {learn_id} Approved for local registry only {secret}", ctx)
            plan_reply, route = bridge.prepare_reply(f"/apply plan {learn_id} global", ctx)
            apply_id = extract_apply_id(plan_reply)
            bridge.prepare_reply(f"/apply enact {apply_id} Confirm Workbench Playbook only {secret}", ctx)

            packed, route = bridge.prepare_reply(f"/context pack task {task_id}", ctx)
            assert route == "local_command"
            context_id = extract_context_id(packed)
            context_file = bridge.CONTEXT_PACKS_DIR / f"{context_id}.md"
            context_text = context_file.read_text(encoding="utf-8")
            for needle in (
                "## Task Summary",
                "## Project Summary",
                "## Evidence Summary",
                "## Evidence Gaps",
                "## Retro Lessons",
                "## Learning Registry Summary",
                "## Playbook Advisory",
                "## Copyable Handoff Context",
                "## Not Applied To",
                "runtime_injection_enabled: false",
                "external_execution_enabled: false",
            ):
                assert_contains(context_text, needle)
            assert_not_contains(context_text, "bf_")

            minimal_task, route = bridge.prepare_reply("/task new Minimal context task", ctx)
            minimal_task_id = extract_task_id(minimal_task)
            minimal_pack, route = bridge.prepare_reply(f"/context pack task {minimal_task_id}", ctx)
            minimal_context_id = extract_context_id(minimal_pack)
            minimal_text = (bridge.CONTEXT_PACKS_DIR / f"{minimal_context_id}.md").read_text(encoding="utf-8")
            assert_contains(minimal_text, "not available")

            shown, route = bridge.prepare_reply(f"/context show {context_id}", ctx)
            assert route == "local_command"
            assert_contains(shown, "Context Pack")
            assert_contains(shown, "playbook_advisory")

            listed, route = bridge.prepare_reply("/context list", ctx)
            assert route == "local_command"
            assert_contains(listed, context_id)

            codex_handoff, route = bridge.prepare_reply(f"/context handoff {task_id} codex", ctx)
            assert route == "local_command"
            assert_contains(codex_handoff, "Execution target: Codex")
            assert_contains(codex_handoff, "copy-only context")
            assert_contains(codex_handoff, "Playbook Advisory")

            kiro_handoff, route = bridge.prepare_reply(f"/context handoff {task_id} kiro", ctx)
            assert route == "local_command"
            assert_contains(kiro_handoff, "Execution target: Kiro")

            advisory, route = bridge.prepare_reply(f"/playbook advise task {task_id}", ctx)
            assert route == "local_command"
            assert_contains(advisory, "search_scope: workbench/playbooks only")
            assert_contains(advisory, "Playbook Advisory")

            task_handoff, route = bridge.prepare_reply(f"/task handoff {task_id} codex --with-context", ctx)
            assert route == "local_command"
            assert_contains(task_handoff, "Handoff Context for Codex")
            assert_contains(task_handoff, "Playbook Advisory")

            project_pack, route = bridge.prepare_reply("/context pack project kiro-proxy", ctx)
            assert route == "local_command"
            project_context_id = extract_context_id(project_pack)
            project_context_text = (bridge.CONTEXT_PACKS_DIR / f"{project_context_id}.md").read_text(encoding="utf-8")
            assert_contains(project_context_text, "source_project_id: kiro-proxy")

            project_advisory, route = bridge.prepare_reply("/playbook advise project kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_advisory, "Playbook Advisory")

            project_brief, route = bridge.prepare_reply("/project brief kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "latest_context_pack")
            assert_contains(project_brief, "playbook_advisory_count")

            project_dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(project_dashboard, "context_pack_count")
            assert_contains(project_dashboard, "projects_with_context")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "context_pack_count")
            assert_contains(status, "runtime_injection_enabled: false")
            assert_contains(status, "external_execution_enabled: false")

            archived, route = bridge.prepare_reply(f"/context archive {context_id}", ctx)
            assert route == "local_command"
            assert_contains(archived, "status: archived")
            assert_contains((bridge.CONTEXT_PACKS_DIR / f"{context_id}.md").read_text(encoding="utf-8"), "status: archived")

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution request must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("Please execute dir E:\\ai and edit files", ctx)
            finally:
                bridge.call_hermes = original_call_hermes
            assert route == "work_order"
            assert_contains(work_order, "不运行命令")
            assert_not_contains(work_order, "已执行")

            for root_dir in (
                bridge.CONTEXT_PACKS_DIR,
                bridge.APPLICATIONS_DIR,
                bridge.PLAYBOOKS_DIR,
                bridge.LEARNING_DIR,
                bridge.TASKS_DIR,
                bridge.PROJECTS_DIR,
                bridge.EVIDENCE_DIR,
                bridge.RETROS_DIR,
            ):
                assert_no_secret_in_tree(root_dir, "bf_")
            assert_not_contains(bridge.LOG_FILE.read_text(encoding="utf-8"), "bf_")
            reset_logger()

        print("smoke_context: OK")
        return 0
    finally:
        for name, value in old_paths.items():
            setattr(bridge, name, value)
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
