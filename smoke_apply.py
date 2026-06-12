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
        "robot_id": "robot-apply-smoke-123456",
        "owner_channel_id": "owner-apply-smoke-123456",
        "last_seq": 180,
        "state": {bridge.PROCESSED_STATE_KEY: []},
        "runtime_info": {
            "run_id": "apply-smoke",
            "pid": 1234,
            "startup_method": "manual",
            "lock_status": "held",
        },
        "heartbeat": {"updated_at": "2026-06-07T14:00:00+08:00"},
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


def extract_apply_id(text: str) -> str:
    match = re.search(r"APPLY-\d{8}-\d{6}(?:-\d{2})?", text)
    if not match:
        raise AssertionError("apply_id missing")
    return match.group(0)


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
- python smoke_apply.py

Test results:
- passed: smoke_apply.py

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
        "DECISIONS_DIR": bridge.DECISIONS_DIR,
        "DAILY_DIR": bridge.DAILY_DIR,
        "ARCHIVE_DIR": bridge.ARCHIVE_DIR,
        "LOG_DIR": bridge.LOG_DIR,
        "LOG_FILE": bridge.LOG_FILE,
    }
    secret = "bf_apply_secret_123456"

    try:
        with tempfile.TemporaryDirectory(prefix="ohb-apply-", ignore_cleanup_errors=True) as tmp:
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
            bridge.DECISIONS_DIR = bridge.WORKBENCH_DIR / "decisions"
            bridge.DAILY_DIR = bridge.WORKBENCH_DIR / "daily"
            bridge.ARCHIVE_DIR = bridge.WORKBENCH_DIR / "archive"
            bridge.LOG_DIR = root / "logs"
            bridge.LOG_FILE = bridge.LOG_DIR / "bridge.log"
            reset_logger()
            bridge.setup_logging()

            ctx = context()

            help_text, route = bridge.prepare_reply("/apply help", ctx)
            assert route == "local_command"
            assert_contains(help_text, "workbench/playbooks")
            assert_contains(help_text, "runtime_injection_enabled: false")
            assert_contains(help_text, "does not execute commands")

            playbook_help, route = bridge.prepare_reply("/playbook help", ctx)
            assert route == "local_command"
            assert_contains(playbook_help, "Workbench reference layer")

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

            unapproved_reply, route = bridge.prepare_reply("/learn propose manual Unapproved apply should fail", ctx)
            assert route == "local_command"
            unapproved_learn_id = extract_learn_id(unapproved_reply)
            refused, route = bridge.prepare_reply(f"/apply plan {unapproved_learn_id} global", ctx)
            assert route == "local_command"
            assert_contains(refused, "not approved")

            proposed, route = bridge.prepare_reply(f"/learn propose retro {task_id}", ctx)
            assert route == "local_command"
            learn_id = extract_learn_id(proposed)
            bridge.prepare_reply(f"/learn approve {learn_id} Approved for local registry only {secret}", ctx)

            plan_reply, route = bridge.prepare_reply(f"/apply plan {learn_id} global", ctx)
            assert route == "local_command"
            apply_id = extract_apply_id(plan_reply)
            assert_contains(plan_reply, "not enacted")
            apply_file = bridge.APPLICATIONS_DIR / f"{apply_id}.md"
            apply_text = apply_file.read_text(encoding="utf-8")
            assert_contains(apply_text, "target_path: workbench/playbooks/atlas_workbench_playbook.md")
            assert_contains(apply_text, "runtime_injection_enabled: false")
            assert_contains(apply_text, "Do Not Auto-Apply Beyond Workbench")
            assert_not_contains(apply_text, "bf_")

            show, route = bridge.prepare_reply(f"/apply show {apply_id}", ctx)
            assert route == "local_command"
            for needle in ("Apply plan", "source_learn_id", "target_path", "runtime_impact"):
                assert_contains(show, needle)

            listed, route = bridge.prepare_reply("/apply list", ctx)
            assert route == "local_command"
            assert_contains(listed, apply_id)
            planned_list, route = bridge.prepare_reply("/apply list --status planned", ctx)
            assert route == "local_command"
            assert_contains(planned_list, apply_id)

            project_plan, route = bridge.prepare_reply(f"/apply plan {learn_id} project kiro-proxy", ctx)
            assert route == "local_command"
            project_apply_id = extract_apply_id(project_plan)
            assert_contains(project_plan, "workbench/playbooks/projects/kiro-proxy.md")
            cancelled, route = bridge.prepare_reply(f"/apply cancel {project_apply_id} Cancel before enact {secret}", ctx)
            assert route == "local_command"
            assert_contains(cancelled, "status: cancelled")

            enacted, route = bridge.prepare_reply(f"/apply enact {apply_id} Confirm Workbench Playbook only {secret}", ctx)
            assert route == "local_command"
            assert_contains(enacted, "Applied to Workbench Playbook")
            assert_contains(enacted, "not applied to Hermes / Memory / SkillRepo / system prompt / project code")
            playbook_file = bridge.PLAYBOOKS_DIR / "atlas_workbench_playbook.md"
            playbook_text = playbook_file.read_text(encoding="utf-8")
            assert_contains(playbook_text, learn_id)
            assert_contains(playbook_text, "runtime_injection_enabled: false")
            assert_not_contains(playbook_text, "bf_")

            registry_text = (bridge.LEARNING_REGISTRY_DIR / f"{learn_id}.md").read_text(encoding="utf-8")
            assert_contains(registry_text, "application_status: applied_to_workbench_playbook")
            assert_not_contains(registry_text, "bf_")

            playbook_show, route = bridge.prepare_reply("/playbook show global", ctx)
            assert route == "local_command"
            assert_contains(playbook_show, "entry_count: 1")
            assert_contains(playbook_show, learn_id)

            playbook_list, route = bridge.prepare_reply("/playbook list", ctx)
            assert route == "local_command"
            assert_contains(playbook_list, "global")

            search, route = bridge.prepare_reply("/playbook search Behavior", ctx)
            assert route == "local_command"
            assert_contains(search, "Playbook search")
            assert_contains(search, "global")

            dashboard, route = bridge.prepare_reply("/apply dashboard", ctx)
            assert route == "local_command"
            assert_contains(dashboard, "applied_count: 1")
            assert_contains(dashboard, "runtime_injection_enabled: false")

            learn_status, route = bridge.prepare_reply("/learn status", ctx)
            assert route == "local_command"
            assert_contains(learn_status, "applied_to_workbench_playbook_count")
            assert_contains(learn_status, "runtime_injection_enabled: false")
            assert_contains(learn_status, "external_application_enabled: false")

            status, route = bridge.prepare_reply("/status", ctx)
            assert route == "local_command"
            assert_contains(status, "apply_plans")
            assert_contains(status, "playbook_entries")
            assert_contains(status, "runtime_injection_enabled: false")

            project_brief, route = bridge.prepare_reply("/project brief kiro-proxy", ctx)
            assert route == "local_command"
            assert_contains(project_brief, "project_playbook_entries")
            assert_contains(project_brief, "applied_learnings")

            project_dashboard, route = bridge.prepare_reply("/project dashboard", ctx)
            assert route == "local_command"
            assert_contains(project_dashboard, "playbook_entry_count")
            assert_contains(project_dashboard, "pending_apply_count")

            reverted, route = bridge.prepare_reply(f"/apply revert {apply_id} Revert from Workbench only {secret}", ctx)
            assert route == "local_command"
            assert_contains(reverted, "status: reverted")
            assert_contains(reverted, "reverted_from_workbench_playbook")
            playbook_text = playbook_file.read_text(encoding="utf-8")
            assert_contains(playbook_text, "Revert Note")
            registry_text = (bridge.LEARNING_REGISTRY_DIR / f"{learn_id}.md").read_text(encoding="utf-8")
            assert_contains(registry_text, "application_status: reverted_from_workbench_playbook")
            assert_not_contains(registry_text, "bf_")

            original_call_hermes = bridge.call_hermes

            def fail_call_hermes(_: str) -> str:
                raise AssertionError("execution request must not call Hermes")

            bridge.call_hermes = fail_call_hermes
            try:
                work_order, route = bridge.prepare_reply("Please run dir E:\\ai and fix files", ctx)
            finally:
                bridge.call_hermes = original_call_hermes
            assert route == "work_order"
            assert_contains(work_order, "不运行命令")
            assert_not_contains(work_order, "已执行")

            assert_no_secret_in_tree(bridge.APPLICATIONS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.PLAYBOOKS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.LEARNING_DIR, "bf_")
            assert_no_secret_in_tree(bridge.TASKS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.PROJECTS_DIR, "bf_")
            assert_no_secret_in_tree(bridge.EVIDENCE_DIR, "bf_")
            assert_no_secret_in_tree(bridge.RETROS_DIR, "bf_")
            assert_not_contains(bridge.LOG_FILE.read_text(encoding="utf-8"), "bf_")
            reset_logger()

        print("smoke_apply: OK")
        return 0
    finally:
        for name, value in old_paths.items():
            setattr(bridge, name, value)
        reset_logger()


if __name__ == "__main__":
    raise SystemExit(main())
