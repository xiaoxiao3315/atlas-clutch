"""Smoke: the learning loop runs end to end with human gates intact.

Covers the Phase 1.8 closure path:
  auto-closed task -> retro -> auto-harvested learning proposals (idempotent)
  -> human /learn review -> human /learn approve (registry + /apply pointer)
  -> /apply plan (playbook entry stays unwritten until explicitly enacted).

Run: python smoke_learning_loop.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import bridge
from smoke_exec import (
    assert_contains,
    configure_temp_paths,
    context,
    extract_exec_id,
    extract_task_id,
    restore_paths,
)

TARGET_FILE = "workbench/tmp/learning-loop-smoke.txt"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-learning-loop-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        orig_runner = bridge.run_allowlisted_external_command
        orig_post_run = bridge.run_allowlisted_post_run_command
        orig_probe = bridge.probe_codex_noninteractive
        orig_probe_write = bridge.probe_codex_workspace_write
        state = {"wrote": False}
        try:
            def fake_post_run(argv: list[str]) -> dict:
                if not bridge.is_allowed_post_run_command(list(argv)):
                    raise AssertionError(f"unexpected post-run command: {argv}")
                status = f"?? {TARGET_FILE}" if state["wrote"] else ""
                diff_stat = f" {TARGET_FILE} | 1 +" if state["wrote"] else ""
                return {
                    "returncode": 0,
                    "stdout": status if argv == ["git", "status", "--short"] else diff_stat,
                    "stderr": "",
                }

            def fake_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                target = root / TARGET_FILE
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("learning loop smoke\n", encoding="utf-8")
                state["wrote"] = True
                return {
                    "returncode": 0,
                    "stdout": "\n".join(
                        [
                            "Execution summary:",
                            "- fake workspace-write runner completed",
                            "Modified files:",
                            f"- {TARGET_FILE}",
                            "Commands:",
                            "- python -B smoke_learning_loop.py",
                            "Test results:",
                            "- passed",
                            "Unverified:",
                            "- live UI",
                            "Unresolved risks:",
                            "- none",
                        ]
                    ),
                    "stderr": "",
                    "timed_out": False,
                }

            bridge.run_allowlisted_post_run_command = fake_post_run
            bridge.run_allowlisted_external_command = fake_runner
            bridge.probe_codex_noninteractive = lambda sandbox_mode="read-only": {
                "supported": True,
                "mode": "codex_exec_read_only_stdin",
                "reason": "smoke fake read-only runner",
                "command": ["codex", "exec", "--sandbox", "read-only", "-"],
            }
            bridge.probe_codex_workspace_write = lambda: {
                "supported": True,
                "mode": "codex_exec_workspace_write_stdin",
                "reason": "smoke fake workspace-write runner",
                "command": ["codex", "exec", "--sandbox", "workspace-write", "-"],
            }

            ctx = context()
            bridge.prepare_reply("/project new learn_loop Learning loop smoke", ctx)

            # 1. Auto-closed owner-write run creates retro + harvested proposals.
            reply, route = bridge.prepare_reply(
                "/run codex-write Create or update only workbench/tmp/learning-loop-smoke.txt. "
                "Do not modify source code. No git add/commit/push. --project learn_loop",
                ctx,
            )
            assert route == "local_command"
            task_id = extract_task_id(reply)
            exec_id = extract_exec_id(reply)

            exec_meta = bridge.task_metadata(bridge.read_exec(exec_id))
            if exec_meta.get("auto_decision") != "pass":
                raise AssertionError(f"expected auto_decision pass, got {exec_meta.get('auto_decision')}")
            if exec_meta.get("auto_closed") != "true":
                raise AssertionError(f"expected auto_closed true, got {exec_meta.get('auto_closed')}")
            if exec_meta.get("auto_retro_created") != "true":
                raise AssertionError(f"expected auto_retro_created true, got {exec_meta.get('auto_retro_created')}")
            if exec_meta.get("auto_learning_proposals", "none") in ("", "none"):
                raise AssertionError("exec metadata missing auto_learning_proposals ids")
            if not (root / TARGET_FILE).exists():
                raise AssertionError(f"runner did not write temp target: {TARGET_FILE}")

            proposals = bridge.learning_proposals_for_task(task_id)
            if not 1 <= len(proposals) <= 3:
                raise AssertionError(f"expected 1-3 harvested proposals, got {proposals}")
            for learn_id in proposals:
                text = (bridge.LEARNING_PROPOSALS_DIR / f"{learn_id}.md").read_text(encoding="utf-8")
                meta = bridge.task_metadata(text)
                if meta.get("status") != "proposed":
                    raise AssertionError(f"{learn_id} should be proposed, got {meta.get('status')}")
                if meta.get("source_task_id") != task_id:
                    raise AssertionError(f"{learn_id} has wrong source_task_id")
                if bridge.registry_path(learn_id).exists():
                    raise AssertionError(f"{learn_id} must not be auto-approved into registry")

            # 2. Harvest is idempotent: rerunning never duplicates proposals.
            ids_again, summary, scan_done = bridge.auto_propose_learning_from_retro(task_id)
            if not scan_done or ids_again != proposals:
                raise AssertionError(f"idempotent rerun changed proposals: {ids_again} vs {proposals}")
            if "already exist" not in summary:
                raise AssertionError(f"rerun summary should mark skip, got: {summary}")
            if bridge.learning_proposals_for_task(task_id) != proposals:
                raise AssertionError("rerun created duplicate proposals")

            # 3. Human gates still work: review, approve (registry + apply pointer).
            first = proposals[0]
            review_reply, route = bridge.prepare_reply(f"/learn review {first}", ctx)
            assert route == "local_command"
            assert_contains(review_reply, first)

            approve_reply, route = bridge.prepare_reply(f"/learn approve {first} smoke approval note", ctx)
            assert route == "local_command"
            assert_contains(approve_reply, f"/apply plan {first} global")
            if not bridge.registry_path(first).exists():
                raise AssertionError("approve must register the learning entry")

            # 4. Playbook entry remains explicit: plan exists, playbook unwritten.
            apply_reply, route = bridge.prepare_reply(f"/apply plan {first} global", ctx)
            assert route == "local_command"
            assert_contains(apply_reply, "Apply plan created")
            assert_contains(apply_reply, "not enacted")
            playbook_path = bridge.global_playbook_path()
            if playbook_path.exists() and first in playbook_path.read_text(encoding="utf-8"):
                raise AssertionError("playbook must not gain the entry before explicit /apply enactment")

            print("smoke_learning_loop: PASS")
            return 0
        finally:
            bridge.run_allowlisted_external_command = orig_runner
            bridge.run_allowlisted_post_run_command = orig_post_run
            bridge.probe_codex_noninteractive = orig_probe
            bridge.probe_codex_workspace_write = orig_probe_write
            restore_paths(old_paths)


if __name__ == "__main__":
    raise SystemExit(main())
