from __future__ import annotations

import tempfile
from pathlib import Path

import dashboard_server as dashboard
from smoke_dashboard import build_fixture, snapshot_tree


TASK_ID = "OHB-20260607-160000"
DISPATCH_ID = "DISPATCH-20260607-160100"
PILOT_ID = "PILOT-20260607-160300"
COLLECTION_ID = "COLLECT-20260607-160400"
PROJECT_ID = "demo"


def assert_contains(text: str | None, needle: str) -> None:
    if text is None or needle not in text:
        raise AssertionError(f"missing expected text: {needle}")


def assert_not_contains(text: str | None, needle: str) -> None:
    if text is not None and needle in text:
        raise AssertionError(f"unexpected text: {needle}")


def forbidden_markers() -> tuple[str, ...]:
    return (
        "bf" + "_",
        "sk" + "-",
        "Authorization" + ": Bearer",
        "Cookie" + ":",
    )


def assert_safe_html(*pages: str | None) -> None:
    for html in pages:
        assert_contains(html, "read_only_dashboard")
        assert_contains(html, "copy only")
        assert_contains(html, "Read-only local dashboard. Copy helpers do not execute commands.")
        for marker in forbidden_markers():
            assert_not_contains(html, marker)
        assert_not_contains(html, "dashboard_secret")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-dashboard-drilldown-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_workbench = dashboard.WORKBENCH_DIR
        try:
            workbench = build_fixture(root)
            dashboard.WORKBENCH_DIR = workbench
            before = snapshot_tree(workbench)

            home = dashboard.render_dashboard_html(dashboard.build_dashboard_data())
            for link in (
                f'href="/task/{TASK_ID}"',
                f'href="/project/{PROJECT_ID}"',
                f'href="/dispatch/{DISPATCH_ID}"',
                f'href="/pilot/{PILOT_ID}"',
                f'href="/collection/{COLLECTION_ID}"',
            ):
                assert_contains(home, link)

            task_html = dashboard.render_detail_html("task", dashboard.build_task_detail_data(TASK_ID))
            project_html = dashboard.render_detail_html("project", dashboard.build_project_detail_data(PROJECT_ID))
            dispatch_html = dashboard.render_detail_html("dispatch", dashboard.build_dispatch_detail_data(DISPATCH_ID))
            pilot_html = dashboard.render_detail_html("pilot", dashboard.build_pilot_detail_data(PILOT_ID))
            collection_html = dashboard.render_detail_html("collection", dashboard.build_collection_detail_data(COLLECTION_ID))
            copy_dispatch_html = dashboard.render_copy_payload_page("Copy Dispatch", dashboard.dispatch_package_text(DISPATCH_ID))
            copy_context_codex_html = dashboard.render_copy_payload_page("Copy Context Codex", dashboard.context_handoff_text(TASK_ID, "codex"))
            copy_context_kiro_html = dashboard.render_copy_payload_page("Copy Context Kiro", dashboard.context_handoff_text(TASK_ID, "kiro"))

            assert_safe_html(
                task_html,
                project_html,
                dispatch_html,
                pilot_html,
                collection_html,
                copy_dispatch_html,
                copy_context_codex_html,
                copy_context_kiro_html,
            )

            for html, needles in (
                (task_html, ("Task Summary", "latest dispatch", "Copy task next command", "Copy context handoff command")),
                (project_html, ("Project Summary", "Active Tasks", "Evidence Gaps", "Recommended Next Commands")),
                (dispatch_html, ("Dispatch Summary", "Copy dispatch package command", "Copy exec prepare command")),
                (pilot_html, ("Pilot Summary", "Copy pilot metrics command")),
                (collection_html, ("Collection Summary", "standard return report summary")),
                (copy_dispatch_html, ("Manual copy only. Not sent automatically.", "Manual Dispatch Package")),
                (copy_context_codex_html, ("Handoff Context for Codex", "Manual copy only. Not sent automatically.")),
                (copy_context_kiro_html, ("Handoff Context for Kiro", "Manual copy only. Not sent automatically.")),
            ):
                for needle in needles:
                    assert_contains(html, needle)

            if "do_POST" in dashboard.DashboardHandler.__dict__:
                raise AssertionError("dashboard must not define a POST handler")

            after = snapshot_tree(workbench)
            if before != after:
                raise AssertionError("drilldown rendering modified workbench files")
        finally:
            dashboard.WORKBENCH_DIR = old_workbench

    print("smoke_dashboard_drilldown passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
