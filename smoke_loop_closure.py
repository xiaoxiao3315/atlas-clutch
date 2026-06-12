from __future__ import annotations

"""Phase 1.8 loop-closure smoke.

Covers the three task types that must round-trip for the control plane to be
considered closed:
  1. Manual handoff loop with pre-registration enforcement
     (report without prior dispatch/handoff is refused; handoff registers).
  2. Kiro read-only execution via the simulated adapter
     (OHB_EXEC_SIMULATE_KIRO=1) with dispatch sync and evidence intake.
  3. Codex read-only auto execution that auto-closes
     (QA -> evidence verify -> review -> decide pass -> close -> retro ->
      learn scan) plus the deterministic auto-rework path.
"""

import os
import tempfile
from pathlib import Path

import bridge
from smoke_exec import (
    assert_contains,
    assert_not_contains,
    configure_temp_paths,
    context,
    extract_dispatch_id,
    extract_exec_id,
    extract_task_id,
    restore_paths,
)


def create_read_only_task(ctx: dict, title_suffix: str) -> str:
    task_reply, route = bridge.prepare_reply(
        f"/task new Read-only inspect local state only, no code changes, do not modify files {title_suffix} --project loop_closure",
        ctx,
    )
    assert route == "local_command"
    return extract_task_id(task_reply)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-loop-closure-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        original_runner = bridge.run_allowlisted_external_command
        original_post_run = bridge.run_allowlisted_post_run_command
        original_probe = bridge.probe_codex_noninteractive
        old_sim_kiro = os.environ.pop("OHB_EXEC_SIMULATE_KIRO", None)
        try:
            ctx = context()
            bridge.prepare_reply("/project new loop_closure Phase 1.8 loop closure smoke", ctx)

            def fake_post_run(argv: list[str]) -> dict:
                if not bridge.is_allowed_post_run_command(list(argv)):
                    raise AssertionError(f"unexpected post-run command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                }

            bridge.run_allowlisted_post_run_command = fake_post_run

            # ---- 1. pre-registration enforcement (manual handoff loop) ----
            manual_task_id = create_read_only_task(ctx, "manual loop")
            report_body = (
                "执行摘要：\n- 检查完成\n修改文件：\n- none\n执行命令：\n- dir\n"
                "测试结果：\n- 通过\n关键日志或截图：\n- logs ok\n未验证：\n- live UI\n未解决风险：\n- none\n"
            )
            refused, route = bridge.prepare_reply(f"/task report {manual_task_id}\n{report_body}", ctx)
            assert route == "local_command"
            assert_contains(refused, "pre_registration_enforced: true")
            assert_contains(refused, "回传被拒绝")
            manual_task_file = bridge.TASKS_DIR / f"{manual_task_id}.md"
            assert_not_contains(manual_task_file.read_text(encoding="utf-8"), "### Report at")

            handoff, route = bridge.prepare_reply(f"/task handoff {manual_task_id} kiro", ctx)
            assert route == "local_command"
            assert_contains(handoff, "执行交接包")
            assert_contains(manual_task_file.read_text(encoding="utf-8"), "execution registered")

            reported, route = bridge.prepare_reply(f"/task report {manual_task_id}\n{report_body}", ctx)
            assert route == "local_command"
            assert_contains(reported, "status：reported")
            print("loop_closure 1: pre-registration enforced + manual handoff loop OK")

            # ---- 2. kiro adapter: fail-closed without simulation ----
            kiro_task_id = create_read_only_task(ctx, "kiro probe")
            kiro_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {kiro_task_id} kiro", ctx)
            assert route == "local_command"
            kiro_dispatch_id = extract_dispatch_id(kiro_dispatch_reply)
            no_sim, route = bridge.prepare_reply(f"/exec start {kiro_dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(no_sim, "needs_manual_start")
            assert_contains(no_sim, "kiro non-interactive adapter not wired")
            print("loop_closure 2a: kiro fail-closed without simulation OK")

            # ---- 2b. kiro simulated read-only round trip ----
            os.environ["OHB_EXEC_SIMULATE_KIRO"] = "1"
            kiro_run_task_id = create_read_only_task(ctx, "kiro simulated run")
            kiro_run_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {kiro_run_task_id} kiro", ctx)
            assert route == "local_command"
            kiro_run_dispatch_id = extract_dispatch_id(kiro_run_dispatch_reply)
            kiro_started, route = bridge.prepare_reply(f"/exec start {kiro_run_dispatch_id}", ctx)
            assert route == "local_command"
            kiro_exec_id = extract_exec_id(kiro_started)
            assert_contains(kiro_started, "status: returned")
            assert_contains(kiro_started, "Auto postprocess:")
            kiro_exec_text = (bridge.EXECUTIONS_DIR / f"{kiro_exec_id}.md").read_text(encoding="utf-8")
            assert_contains(kiro_exec_text, "runner_mode: simulated")
            assert_contains(kiro_exec_text, "status: returned")
            kiro_dispatch_text = (bridge.DISPATCHES_DIR / f"{kiro_run_dispatch_id}.md").read_text(encoding="utf-8")
            assert_contains(kiro_dispatch_text, "status: returned")
            kiro_task_text = (bridge.TASKS_DIR / f"{kiro_run_task_id}.md").read_text(encoding="utf-8")
            assert_contains(kiro_task_text, "### Report at")
            del os.environ["OHB_EXEC_SIMULATE_KIRO"]
            print("loop_closure 2b: kiro simulated round trip OK")

            # ---- 3. codex auto pass closes the full loop with learn scan ----
            def full_evidence_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": "fake runner received full stdin payload",
                    "stderr": "",
                }

            bridge.run_allowlisted_external_command = full_evidence_runner
            bridge.probe_codex_noninteractive = lambda sandbox_mode="read-only": {
                "supported": True,
                "mode": "codex_exec_read_only_stdin",
                "reason": "smoke fake non-interactive read-only stdin runner",
                "command": ["codex", "exec", "--sandbox", "read-only", "-"],
            }
            pass_task_id = create_read_only_task(ctx, "codex auto pass")
            pass_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {pass_task_id} codex --with-context", ctx)
            assert route == "local_command"
            pass_dispatch_id = extract_dispatch_id(pass_dispatch_reply)
            pass_started, route = bridge.prepare_reply(f"/exec start {pass_dispatch_id}", ctx)
            assert route == "local_command"
            pass_exec_id = extract_exec_id(pass_started)
            assert_contains(pass_started, "Auto postprocess: pass")
            assert_contains(pass_started, "auto_decision: pass")
            assert_contains(pass_started, "auto_closed: true")
            assert_contains(pass_started, "auto_retro_created: true")
            assert_contains(pass_started, "auto_learn_scan_done: true")
            pass_exec_text = (bridge.EXECUTIONS_DIR / f"{pass_exec_id}.md").read_text(encoding="utf-8")
            assert_contains(pass_exec_text, "auto_decision: pass")
            if not (bridge.RETROS_DIR / f"{pass_task_id}.md").exists():
                raise AssertionError("auto pass must create a retro")
            assert_contains(pass_started, "Learn scan preview:")
            print("loop_closure 3: codex auto pass with retro + learn scan OK")

            # ---- 4. deterministic auto rework on blocked implementation ----
            def blocked_runner(argv: list[str], *, input_text: str = "", **_: object) -> dict:
                if not bridge.is_allowed_external_command(list(argv)):
                    raise AssertionError(f"unexpected external command: {argv}")
                return {
                    "returncode": 0,
                    "stdout": "\n".join(
                        [
                            "Execution summary:",
                            "- could not modify files because this Codex session has read-only filesystem permissions",
                            "Modified files:",
                            "- none",
                            "Commands:",
                            "- fake codex exec read-only",
                            "Test results:",
                            "- inspection only; implementation was blocked",
                            "Unverified:",
                            "- source change",
                            "Unresolved risks:",
                            "- unable to write",
                        ]
                    ),
                    "stderr": "",
                    "timed_out": False,
                }

            bridge.run_allowlisted_external_command = blocked_runner
            rework_task_id = create_read_only_task(ctx, "rework case")
            rework_dispatch_reply, route = bridge.prepare_reply(f"/dispatch create {rework_task_id} codex", ctx)
            assert route == "local_command"
            rework_dispatch_id = extract_dispatch_id(rework_dispatch_reply)
            rework_started, route = bridge.prepare_reply(f"/exec start {rework_dispatch_id}", ctx)
            assert route == "local_command"
            assert_contains(rework_started, "Auto postprocess: rework (needs_evidence)")
            assert_contains(rework_started, "auto_decision: needs_evidence")
            assert_contains(rework_started, "auto_rework_decided: true")
            assert_contains(rework_started, "auto_closed: false")
            rework_task_text = (bridge.TASKS_DIR / f"{rework_task_id}.md").read_text(encoding="utf-8")
            assert_contains(rework_task_text, "status: needs_evidence")
            print("loop_closure 4: deterministic auto rework OK")

        finally:
            bridge.run_allowlisted_external_command = original_runner
            bridge.run_allowlisted_post_run_command = original_post_run
            bridge.probe_codex_noninteractive = original_probe
            if old_sim_kiro is not None:
                os.environ["OHB_EXEC_SIMULATE_KIRO"] = old_sim_kiro
            else:
                os.environ.pop("OHB_EXEC_SIMULATE_KIRO", None)
            restore_paths(old_paths)

    print("\nsmoke_loop_closure: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
