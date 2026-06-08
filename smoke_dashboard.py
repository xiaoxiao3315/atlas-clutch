from __future__ import annotations

import tempfile
from pathlib import Path

import dashboard_server as dashboard


def assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str, needle: str) -> None:
    if needle in text:
        raise AssertionError(f"unexpected text: {needle}")


def forbidden_markers() -> tuple[str, ...]:
    return (
        "bf" + "_",
        "sk" + "-",
        "Authorization" + ": Bearer",
        "Cookie" + ":",
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def build_fixture(root: Path) -> Path:
    workbench = root / "workbench"
    bot_secret = ("bf" + "_dashboard_secret_123456")
    key_secret = ("sk" + "-dashboard-secret-123456")
    auth_line = "Authorization" + ": Bearer " + bot_secret
    cookie_line = "Cookie" + ": session=" + bot_secret

    write(root / ".env", f"TOKEN={bot_secret}\n")
    write(root / "logs" / "bridge.log", f"{auth_line}\n{cookie_line}\n")
    write(
        workbench / "projects" / "demo.md",
        """# demo Demo Project

project_id: demo
status: active
priority: P1
updated_at: 2026-06-07T16:00:00+08:00

## Active Tasks
- OHB-20260607-160000 | needs_evidence | Verify dashboard
""",
    )
    write(
        workbench / "tasks" / "OHB-20260607-160000.md",
        f"""# OHB-20260607-160000 Verify dashboard

status: needs_evidence
project_id: demo
updated_at: 2026-06-07T16:01:00+08:00
evidence_gap_risk: true

## Goal
- Show dashboard summary.

## Evidence Gaps
- Need proof. {auth_line}

## Risks
- Do not expose {key_secret}
""",
    )
    write(
        workbench / "dispatches" / "DISPATCH-20260607-160100.md",
        """# DISPATCH-20260607-160100 Verify dashboard

dispatch_id: DISPATCH-20260607-160100
status: ready
task_id: OHB-20260607-160000
project_id: demo
target_executor: codex
updated_at: 2026-06-07T16:02:00+08:00
""",
    )
    write(
        workbench / "dispatches" / "DISPATCH-20260607-160200.md",
        """# DISPATCH-20260607-160200 Returned dashboard

dispatch_id: DISPATCH-20260607-160200
status: returned
task_id: OHB-20260607-160000
project_id: demo
target_executor: kiro
updated_at: 2026-06-07T16:03:00+08:00
""",
    )
    write(
        workbench / "pilots" / "PILOT-20260607-160300.md",
        """# PILOT-20260607-160300 Dashboard pilot

pilot_id: PILOT-20260607-160300
status: active
project_id: demo
updated_at: 2026-06-07T16:04:00+08:00

## Tasks Included
- OHB-20260607-160000 | needs_evidence | Verify dashboard

## Dispatches
- DISPATCH-20260607-160100 | ready

## Metrics
- estimated_time_saved: 10 min
- main_friction: switching windows
""",
    )
    write(
        workbench / "collections" / "COLLECT-20260607-160400.md",
        """# COLLECT-20260607-160400 Dashboard collection

collection_id: COLLECT-20260607-160400
task_id: OHB-20260607-160000
profile: octo-bridge
created_at: 2026-06-07T16:05:00+08:00
verified: false
""",
    )
    write(
        workbench / "executions" / "EXEC-20260607-160500.md",
        """# EXEC-20260607-160500 Dashboard execution

exec_id: EXEC-20260607-160500
dispatch_id: DISPATCH-20260607-160100
task_id: OHB-20260607-160000
project_id: demo
target_executor: codex
status: prepared
updated_at: 2026-06-07T16:06:00+08:00
""",
    )
    write(
        workbench / "context_packs" / "CTX-20260607-160600.md",
        """# CTX-20260607-160600 Dashboard context

context_id: CTX-20260607-160600
source_task_id: OHB-20260607-160000
created_at: 2026-06-07T16:07:00+08:00
""",
    )
    write(workbench / "learning" / "proposals" / "LEARN-20260607-160700.md", "# learning\n")
    write(workbench / "playbooks" / "atlas_workbench_playbook.md", "# playbook\n")
    return workbench


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-dashboard-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_workbench = dashboard.WORKBENCH_DIR
        try:
            workbench = build_fixture(root)
            dashboard.WORKBENCH_DIR = workbench
            before = snapshot_tree(workbench)

            data = dashboard.build_dashboard_data()
            summary = data["summary"]
            assert summary["mode"] == "read_only_dashboard"
            assert summary["bind"] == "127.0.0.1"
            assert summary["project_count"] == 1
            assert summary["task_count"] == 1
            assert summary["dispatch_count"] == 2
            assert summary["returned_dispatches"] == 1
            assert summary["evidence_gap_count"] == 1
            assert summary["pilot_count"] == 1
            assert summary["collection_count"] == 1
            assert summary["execution_count"] == 1
            assert summary["context_pack_count"] == 1
            assert summary["learning_records"] >= 1
            assert summary["playbook_records"] >= 1

            html = dashboard.render_dashboard_html(data)
            for needle in (
                "Atlas Workbench Dashboard",
                "read_only_dashboard",
                "external_access: false",
                "auto_execute_enabled: false",
                "Projects",
                "Tasks",
                "Dispatches",
                "Evidence Gaps",
                "Pilots",
                "Collections",
                "Executions",
                "Context Packs",
                "Read-only local dashboard. Copy helpers do not execute commands.",
            ):
                assert_contains(html, needle)
            for marker in forbidden_markers():
                assert_not_contains(html, marker)
            assert_not_contains(html, "dashboard_secret")

            after = snapshot_tree(workbench)
            if before != after:
                raise AssertionError("dashboard aggregation modified workbench files")
        finally:
            dashboard.WORKBENCH_DIR = old_workbench

    print("smoke_dashboard passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
