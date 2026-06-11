"""MVP regression smoke for the Atlas Clutch auto pipeline.

Exercises the /auto owner-write pipeline end to end with stubbed Claude
(writer) and Codex (reviewer) boundaries, against a throwaway temp workspace.
No real Claude/Codex/git mutation of the real repo happens.

Scenarios:
  1. Valid owner-write file-pack pass (write-target + acceptance + review + decision)
  2. Malformed file-pack heading without a valid bullet -> safe refusal, no exec
  3. Feature-slice with required validation -> validation passed + auto pass
  4. Acceptance failure (writer omits a required literal) -> needs_human_review

Fixtures are ASCII-only by design; no literal CJK characters appear here.
Run: python smoke_auto_pipeline.py
Throwaway check script.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import bridge
from smoke_exec import configure_temp_paths, context, extract_exec_id, restore_paths

PACK_A = "workbench/tmp/autopipe/a.txt"
PACK_B = "workbench/tmp/autopipe/b.md"
SLICE_TOOL = "workbench/tmp/autopipe/tool.py"
SLICE_NOTES = "workbench/tmp/autopipe/notes.md"

SLICE_TOOL_CONTENT = "def answer():\n    return 42\n"
SLICE_NOTES_CONTENT = "# Feature Slice\nNotes about the slice.\n"
SLICE_WRONG_TOOL_CONTENT = "def other():\n    return 0\n"
SLICE_EVIDENCE = (
    f"validation: python -B -m py_compile {SLICE_TOOL} => returncode: 0\n"
    "validation: git diff --check => returncode: 0"
)

PACK_COMMAND = "\n".join(
    [
        "/auto task Allowed write targets:",
        f"- {PACK_A}",
        f"- {PACK_B}",
        "Acceptance:",
        f"- {PACK_A} => exactly one line: alpha ok",
        f"- {PACK_B} => exactly one line: beta ok",
        "Do not modify source code. No git add/commit/push. --project auto_exec",
    ]
)

MALFORMED_PACK_COMMAND = "\n".join(
    [
        "/auto task Allowed write targets:",
        "this line is prose, not a dash bullet",
        "Acceptance:",
        f"- {PACK_A} => exactly one line: alpha ok",
        "Do not modify source code. No git add/commit/push. --project auto_exec",
    ]
)

SLICE_COMMAND = "\n".join(
    [
        "/auto task Allowed write targets:",
        f"- {SLICE_TOOL}",
        f"- {SLICE_NOTES}",
        "Acceptance:",
        f"- {SLICE_TOOL} => contains: def answer",
        f"- {SLICE_NOTES} => contains: Feature Slice",
        "Required validation:",
        f"- python -B -m py_compile {SLICE_TOOL}",
        "- git diff --check",
        "Do not modify source code except the allowed targets. No git add/commit/push. --project auto_exec",
    ]
)

ACCEPTANCE_FAIL_COMMAND = "\n".join(
    [
        "/auto task Allowed write targets:",
        f"- {SLICE_TOOL}",
        f"- {SLICE_NOTES}",
        "Acceptance:",
        f"- {SLICE_TOOL} => contains: def answer",
        f"- {SLICE_NOTES} => contains: Feature Slice",
        "Do not modify source code except the allowed targets. No git add/commit/push. --project auto_exec",
    ]
)


def check(name: str, ok: bool, detail: object = "") -> None:
    assert ok, f"FAIL {name}: {detail}"
    print(f"PASS {name}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ohb-autopipe-") as tmp:
        root = Path(tmp)
        # The bridge runs declared validation commands (e.g. git diff --check)
        # against the workspace when runner evidence is missing; git-init the
        # temp root so those commands behave deterministically.
        subprocess.run(["git", "init", "-q", str(root)], capture_output=True, text=True, shell=False)
        old_paths = configure_temp_paths(root)
        orig_runner = bridge.run_allowlisted_external_command
        orig_post = bridge.run_allowlisted_post_run_command
        orig_probe_codex = bridge.probe_codex_noninteractive
        orig_probe_claude_ww = bridge.probe_claude_workspace_write

        state = {
            "wrote": False,
            "commands": [],
            "files": {},
            "evidence": "",
            "payloads": [],
        }

        def reset_state(files: dict, evidence: str = "") -> None:
            state.update(
                {"wrote": False, "commands": [], "files": dict(files), "evidence": evidence, "payloads": []}
            )

        def fake_post_run(argv):
            if argv == ["git", "status", "--short"]:
                lines = [f"?? {rel}" for rel in state["files"]] if state["wrote"] else []
                return {"returncode": 0, "stdout": "\n".join(lines), "stderr": ""}
            return {"returncode": 0, "stdout": "", "stderr": ""}

        def fake_runner(argv, *, input_text="", **_):
            assert bridge.is_allowed_external_command(list(argv)), f"not allowlisted: {argv}"
            state["commands"].append(list(argv))
            if argv == ["claude", "-p", "--permission-mode", "acceptEdits"]:
                state["wrote"] = True
                state["payloads"].append(input_text)
                modified = []
                for rel, content in state["files"].items():
                    target_abs = root / rel
                    target_abs.parent.mkdir(parents=True, exist_ok=True)
                    target_abs.write_text(content, encoding="utf-8")
                    modified.append(f"- {rel}")
                stdout = (
                    "Execution summary:\n- built the slice\nModified files:\n"
                    + "\n".join(modified)
                    + "\nCommands:\n- listed below\nTest results:\n- passed\n"
                    + (state["evidence"] + "\n" if state["evidence"] else "")
                    + "Unverified:\n- live UI\nUnresolved risks:\n- none"
                )
                return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}
            if argv == ["codex", "exec", "--sandbox", "read-only", "-"]:
                return {
                    "returncode": 0,
                    "stdout": "review_verdict: pass_candidate\n- ok",
                    "stderr": "",
                    "timed_out": False,
                }
            raise AssertionError(f"unexpected argv: {argv}")

        try:
            bridge.run_allowlisted_post_run_command = fake_post_run
            bridge.run_allowlisted_external_command = fake_runner
            bridge.probe_codex_noninteractive = lambda sandbox_mode="read-only": {
                "supported": True,
                "mode": "codex_exec_read_only_stdin",
                "reason": "fake",
                "command": ["codex", "exec", "--sandbox", "read-only", "-"],
            }
            bridge.probe_claude_workspace_write = lambda: {
                "supported": True,
                "mode": "claude_print_workspace_write_stdin",
                "reason": "fake",
                "command": ["claude", "-p", "--permission-mode", "acceptEdits"],
            }
            ctx = context()
            bridge.prepare_reply("/project new auto_exec Auto pipeline smoke", ctx)

            # ---- 1. valid owner-write file-pack pass ----
            reset_state({PACK_A: "alpha ok\n", PACK_B: "beta ok\n"}, evidence="")
            pack_reply, _ = bridge.prepare_reply(PACK_COMMAND, ctx)
            pack_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(pack_reply)))
            check(
                "1 claude writer reached",
                ["claude", "-p", "--permission-mode", "acceptEdits"] in state["commands"],
                state["commands"],
            )
            check(
                "1 write_target_fidelity passed",
                pack_meta.get("write_target_fidelity") == "passed",
                pack_meta.get("write_target_fidelity"),
            )
            check(
                "1 acceptance_fidelity passed",
                pack_meta.get("acceptance_fidelity") == "passed",
                pack_meta.get("acceptance_fidelity_reason"),
            )
            check(
                "1 codex_review_status pass_candidate",
                pack_meta.get("codex_review_status") == "pass_candidate",
                pack_meta.get("codex_review_status"),
            )
            check(
                "1 auto_decision pass (persisted)",
                pack_meta.get("auto_decision") == "pass",
                pack_meta.get("auto_decision"),
            )

            # ---- 2. malformed file-pack heading without a valid bullet ----
            reset_state({}, evidence="")
            # The pack heading is anchored to a whole line; the pipeline strips
            # the "/auto task " prefix before parsing, so the direct parser
            # check uses the same prefix-stripped body the gate sees.
            malformed_body = MALFORMED_PACK_COMMAND.replace("/auto task ", "", 1)
            pack_parse = bridge.extract_file_pack_targets(malformed_body)
            check(
                "2 malformed pack parses found-but-not-ok",
                pack_parse["found"] and not pack_parse["ok"],
                pack_parse,
            )
            refuse_reply, _ = bridge.prepare_reply(MALFORMED_PACK_COMMAND, ctx)
            check("2 owner-write preflight refused", "status: refused" in refuse_reply, refuse_reply[-400:])
            check(
                "2 refusal flags preflight gate",
                "owner_write_preflight_refused" in refuse_reply,
                refuse_reply[-400:],
            )
            check(
                "2 write_target_fidelity missing",
                "write_target_fidelity: missing" in refuse_reply,
                refuse_reply[-400:],
            )
            check("2 no execution created", "EXEC-" not in refuse_reply, refuse_reply[-400:])
            check("2 writer never ran", state["wrote"] is False and not state["commands"], state["commands"])

            # ---- 3. feature-slice required-validation pass ----
            reset_state({SLICE_TOOL: SLICE_TOOL_CONTENT, SLICE_NOTES: SLICE_NOTES_CONTENT}, evidence=SLICE_EVIDENCE)
            slice_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
            slice_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(slice_reply)))
            check(
                "3 required_validation detected",
                slice_meta.get("required_validation_detected") == "true",
                slice_meta.get("required_validation_detected"),
            )
            check(
                "3 required_validation_status passed",
                slice_meta.get("required_validation_status") == "passed",
                slice_meta.get("required_validation_reason"),
            )
            check(
                "3 acceptance_fidelity passed",
                slice_meta.get("acceptance_fidelity") == "passed",
                slice_meta.get("acceptance_fidelity_reason"),
            )
            check(
                "3 auto_decision pass (persisted)",
                slice_meta.get("auto_decision") == "pass",
                slice_meta.get("auto_decision"),
            )

            # ---- 4. acceptance failure -> needs_human_review ----
            reset_state({SLICE_TOOL: SLICE_WRONG_TOOL_CONTENT, SLICE_NOTES: SLICE_NOTES_CONTENT}, evidence="")
            fail_reply, _ = bridge.prepare_reply(ACCEPTANCE_FAIL_COMMAND, ctx)
            fail_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(fail_reply)))
            check(
                "4 acceptance_fidelity failed",
                fail_meta.get("acceptance_fidelity") == "failed",
                fail_meta.get("acceptance_fidelity_reason"),
            )
            check(
                "4 auto_decision needs_human_review (persisted)",
                fail_meta.get("auto_decision") == "needs_human_review",
                fail_meta.get("auto_decision"),
            )
        finally:
            bridge.run_allowlisted_external_command = orig_runner
            bridge.run_allowlisted_post_run_command = orig_post
            bridge.probe_codex_noninteractive = orig_probe_codex
            bridge.probe_claude_workspace_write = orig_probe_claude_ww
            restore_paths(old_paths)

    print("\nsmoke_auto_pipeline: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
