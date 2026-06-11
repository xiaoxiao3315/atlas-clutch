"""Focused synthetic checks for OHB-FEATURESLICE-014.

CJK fixtures decoded from Unicode escapes:
- HEAD_CU   = "\\u53ea\\u5141\\u8bb8\\u521b\\u5efa\\u6216\\u66f4\\u65b0" + colon
- ACC_HEAD  = "\\u9a8c\\u6536" + colon
- VAL_HEAD  = "\\u5fc5\\u987b\\u9a8c\\u8bc1" + colon
- EXISTS    = "\\u5b58\\u5728"; NON_EMPTY = "\\u975e\\u7a7a"; CONTAINS = "\\u5305\\u542b"

Run: python featureslice_synthetic_check.py
Throwaway check script. Do not commit.
"""
import tempfile
from pathlib import Path

import bridge
from smoke_exec import configure_temp_paths, context, extract_exec_id, restore_paths

HEAD_CU = "只允许创建或更新："
ACC_HEAD = "验收："
VAL_HEAD = "必须验证："
EXISTS = "存在"
NON_EMPTY = "非空"
CONTAINS = "包含"
WRITE_ONLY = "只写一行："
PHRASE_CU = "只允许创建或更新"

TOOL = "workbench/tmp/featureslice/tool.py"
NOTES = "workbench/tmp/featureslice/notes.md"
TOOL_CONTENT = "def answer():\n    return 42\n"
NOTES_CONTENT = "# Feature Slice\nNotes about the slice.\n"
VALID_EVIDENCE = (
    f"validation: python -B -m py_compile {TOOL} => returncode: 0\n"
    "validation: git diff --check => returncode: 0"
)


def check(name, ok, detail=""):
    assert ok, f"FAIL {name}: {detail}"
    print(f"PASS {name}")


# ---- 1-2. acceptance parser units ----
parse = bridge.extract_concrete_acceptance_checks
en_checks = parse(f"Acceptance:\n- {TOOL} => exists\n- {TOOL} => non-empty\n- {TOOL} => contains: def answer\n- {NOTES} => contains: Feature Slice")
check("1 EN exists/non-empty/contains parse", [
    (c["kind"], c["target"], c["expected"]) for c in en_checks
] == [
    ("exists", TOOL, ""), ("non_empty", TOOL, ""), ("contains", TOOL, "def answer"), ("contains", NOTES, "Feature Slice"),
], en_checks)
cn_checks = parse(f"{ACC_HEAD}\n- {TOOL} => {EXISTS}\n- {TOOL} => {NON_EMPTY}\n- {TOOL} => {CONTAINS}：def answer\n- {NOTES} => {CONTAINS}:Feature Slice")
check("2 CJK exists/non-empty/contains parse", [
    (c["kind"], c["target"], c["expected"]) for c in cn_checks
] == [
    ("exists", TOOL, ""), ("non_empty", TOOL, ""), ("contains", TOOL, "def answer"), ("contains", NOTES, "Feature Slice"),
], cn_checks)

# ---- 3. exact-one-line still parses ----
legacy = parse(f"{ACC_HEAD}\n- {TOOL} => {WRITE_ONLY}solo ok")
check("3 exact-one-line bullet still parses", legacy == [{"kind": "exact_one_line", "expected": "solo ok", "target": TOOL}], legacy)
check("3b generic exact-one-line still parses", parse("Write exactly one line: g ok.") == [{"kind": "exact_one_line", "expected": "g ok", "target": ""}])

# ---- 8-9. validation command allowlist ----
ok_cmd = bridge.is_allowed_required_validation_command
check("8 py_compile accepted", ok_cmd(f"python -B -m py_compile {TOOL}"))
check("8 git diff --check accepted", ok_cmd("git diff --check"))
check("8 git status accepted", ok_cmd("git status --short"))
check("8 smoke scripts accepted", ok_cmd("python smoke_exec_start.py") and ok_cmd("python smoke_task_loop.py"))
for label, bad in (
    ("9 git add rejected", "git add ."),
    ("9 git commit rejected", "git commit -m x"),
    ("9 git push rejected", "git push origin main"),
    ("9 && rejected", "git diff --check && git push"),
    ("9 || rejected", "git diff --check || true"),
    ("9 ; rejected", "git diff --check; rm x"),
    ("9 pipe rejected", "git diff --check | tee out"),
    ("9 redirection rejected", "python a.py > out.txt"),
    ("9 backtick rejected", "python `which a`.py"),
    ("9 env expansion rejected", "python $HOME/a.py"),
    ("9 absolute rejected", r"python C:\Users\ROG\a.py"),
    ("9 posix absolute rejected", "python /etc/a.py"),
    ("9 traversal rejected", "python ../a.py"),
    ("9 deploy rejected", "deploy production"),
):
    check(label, not ok_cmd(bad), bad)
val_block = bridge.extract_required_validation_commands(f"Required validation:\n- python -B -m py_compile {TOOL}\n- git diff --check")
check("8b EN validation block parses", val_block["ok"] and val_block["commands"] == [f"python -B -m py_compile {TOOL}", "git diff --check"], val_block)
val_cn = bridge.extract_required_validation_commands(f"{VAL_HEAD}\n- git diff --check")
check("8c CJK validation block parses", val_cn["ok"] and val_cn["commands"] == ["git diff --check"], val_cn)
val_bad = bridge.extract_required_validation_commands("Required validation:\n- git push origin main")
check("9b unsafe validation block not ok", val_bad["found"] and not val_bad["ok"], val_bad)

# ---- live flows ----
SLICE_COMMAND = "\n".join(
    [
        f"/auto task {HEAD_CU}",
        f"- {TOOL}",
        f"- {NOTES}",
        ACC_HEAD,
        f"- {TOOL} => {CONTAINS}：def answer",
        f"- {NOTES} => {CONTAINS}：Feature Slice",
        VAL_HEAD,
        f"- python -B -m py_compile {TOOL}",
        "- git diff --check",
        "Do not modify source code except the allowed targets. No git add/commit/push. --project auto_exec",
    ]
)

with tempfile.TemporaryDirectory(prefix="ohb-featureslice-diag-") as tmp:
    root = Path(tmp)
    old_paths = configure_temp_paths(root)
    orig_runner = bridge.run_allowlisted_external_command
    orig_post = bridge.run_allowlisted_post_run_command
    orig_probe_codex = bridge.probe_codex_noninteractive
    orig_probe_claude_ww = bridge.probe_claude_workspace_write

    state = {
        "wrote": False,
        "commands": [],
        "files": {TOOL: TOOL_CONTENT, NOTES: NOTES_CONTENT},
        "evidence": VALID_EVIDENCE,
        "payloads": [],
        # Pre-run worktree dirt the mock reports in every `git status --short`
        # snapshot (pre-run and post-run alike). Clean by default so auto-pass
        # scenarios own all dirty paths under the worktree ownership guard
        # (commit 86e456c). Scenario 10b sets it to bridge.py to exercise the
        # guard's unowned-dirt block.
        "baseline_dirt": "",
    }

    def fake_post_run(argv):
        if argv == ["git", "status", "--short"]:
            lines = []
            if state["baseline_dirt"]:
                lines.append(state["baseline_dirt"])
            if state["wrote"]:
                lines.extend(f"?? {rel}" for rel in state["files"])
            return {"returncode": 0, "stdout": "\n".join(lines), "stderr": ""}
        return {"returncode": 0, "stdout": " bridge.py | 1 +", "stderr": ""}

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
                "Execution summary:\n- built the feature slice\nModified files:\n" + "\n".join(modified)
                + "\nCommands:\n- listed below\nTest results:\n- passed\n"
                + (state["evidence"] + "\n" if state["evidence"] else "")
                + "Unverified:\n- live UI\nUnresolved risks:\n- none"
            )
            return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}
        if argv == ["codex", "exec", "--sandbox", "read-only", "-"]:
            return {"returncode": 0, "stdout": "review_verdict: pass_candidate\n- ok", "stderr": "", "timed_out": False}
        raise AssertionError(f"unexpected argv: {argv}")

    try:
        bridge.run_allowlisted_post_run_command = fake_post_run
        bridge.run_allowlisted_external_command = fake_runner
        bridge.probe_codex_noninteractive = lambda sandbox_mode="read-only": {
            "supported": True, "mode": "codex_exec_read_only_stdin", "reason": "fake", "command": ["codex", "exec", "--sandbox", "read-only", "-"],
        }
        bridge.probe_claude_workspace_write = lambda: {
            "supported": True, "mode": "claude_print_workspace_write_stdin", "reason": "fake", "command": ["claude", "-p", "--permission-mode", "acceptEdits"],
        }
        ctx = context()
        bridge.prepare_reply("/project new auto_exec Auto execution smoke", ctx)

        # ---- 10. /auto task feature slice auto-passes ----
        reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        exec_id = extract_exec_id(reply)
        meta = bridge.task_metadata(bridge.read_exec(exec_id))
        check("10 claude runner reached", ["claude", "-p", "--permission-mode", "acceptEdits"] in state["commands"], state["commands"])
        check("10 payload carries validation instructions", "Required Validation Commands" in state["payloads"][0] and "git diff --check" in state["payloads"][0], state["payloads"][0][:400])
        check("10 write_target_fidelity passed", meta.get("write_target_fidelity") == "passed", meta.get("write_target_fidelity"))
        check("10 acceptance passed", meta.get("acceptance_fidelity") == "passed", meta.get("acceptance_fidelity_reason"))
        check("10 validation detected+passed", meta.get("required_validation_detected") == "true" and meta.get("required_validation_status") == "passed", meta.get("required_validation_reason"))
        check("10 review pass_candidate", meta.get("codex_review_status") == "pass_candidate", meta.get("codex_review_status"))
        check("10 verification not blocked", meta.get("verification_status") == "not_blocked", meta.get("verification_status"))
        check("10 auto pass", "auto_decision: pass" in reply, reply[-700:])
        check("10 summary shows validation", "required_validation_status: passed" in reply, reply[-700:])

        # ---- 10b. unowned pre-run worktree dirt -> needs_human_review ----
        # Stale-fixture regression guard. A concurrent actor's uncommitted
        # bridge.py (dirty before this run, outside the declared targets) must
        # never be swept into an auto pass. The worktree ownership guard
        # (commit 86e456c) blocks auto close here while acceptance/validation
        # still pass, isolating the ownership gate as the sole blocker.
        state.update({"wrote": False, "commands": [], "payloads": [], "files": {TOOL: TOOL_CONTENT, NOTES: NOTES_CONTENT}, "evidence": VALID_EVIDENCE, "baseline_dirt": " M bridge.py"})
        unowned_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        unowned_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(unowned_reply)))
        check("10b worktree ownership blocked", unowned_meta.get("worktree_ownership") == "blocked", unowned_meta.get("worktree_ownership"))
        check("10b reason flags unowned bridge.py", "unowned_dirty_worktree" in unowned_meta.get("worktree_ownership_reason", "") and "bridge.py" in unowned_meta.get("worktree_ownership_reason", ""), unowned_meta.get("worktree_ownership_reason"))
        check("10b acceptance still passes", unowned_meta.get("acceptance_fidelity") == "passed", unowned_meta.get("acceptance_fidelity_reason"))
        check("10b unowned dirt needs human review", "auto_decision: needs_human_review" in unowned_reply, unowned_reply[-700:])
        state["baseline_dirt"] = ""

        # ---- 11. /auto write feature slice auto-passes ----
        state.update({"wrote": False, "commands": [], "payloads": [], "files": {TOOL: TOOL_CONTENT, NOTES: NOTES_CONTENT}, "evidence": VALID_EVIDENCE})
        write_reply, _ = bridge.prepare_reply(SLICE_COMMAND.replace("/auto task", "/auto write", 1), ctx)
        write_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(write_reply)))
        check("11 /auto write slice passes", write_meta.get("required_validation_status") == "passed" and "auto_decision: pass" in write_reply, write_reply[-400:])

        # ---- 12. missing validation evidence blocks ----
        state.update({"wrote": False, "commands": [], "files": {TOOL: TOOL_CONTENT, NOTES: NOTES_CONTENT}, "evidence": ""})
        miss_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        miss_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(miss_reply)))
        check("12 missing evidence fails validation", miss_meta.get("required_validation_status") == "failed" and "missing validation evidence" in miss_meta.get("required_validation_reason", ""), miss_meta.get("required_validation_reason"))
        check("12 missing evidence blocked", "auto_decision: needs_human_review" in miss_reply, miss_reply[-400:])

        # ---- 13. nonzero validation returncode blocks ----
        state.update({"wrote": False, "commands": [], "evidence": f"validation: python -B -m py_compile {TOOL} => returncode: 1\nvalidation: git diff --check => returncode: 0"})
        rc_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        rc_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(rc_reply)))
        check("13 nonzero returncode fails validation", rc_meta.get("required_validation_status") == "failed" and "returncode 1" in rc_meta.get("required_validation_reason", ""), rc_meta.get("required_validation_reason"))
        check("13 nonzero blocked", "auto_decision: needs_human_review" in rc_reply, rc_reply[-400:])

        # ---- 14. wrong acceptance content blocked ----
        state.update({"wrote": False, "commands": [], "files": {TOOL: "def other():\n    pass\n", NOTES: NOTES_CONTENT}, "evidence": VALID_EVIDENCE})
        wrong_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        wrong_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(wrong_reply)))
        check("14 missing literal fails acceptance", wrong_meta.get("acceptance_fidelity") == "failed" and "does not contain" in wrong_meta.get("acceptance_fidelity_reason", ""), wrong_meta.get("acceptance_fidelity_reason"))
        check("14 wrong content blocked", "auto_decision: needs_human_review" in wrong_reply, wrong_reply[-400:])

        # ---- 4-7. evaluator edge cases via live runs ----
        # 4: undeclared acceptance target
        und_command = SLICE_COMMAND.replace(f"- {NOTES} => {CONTAINS}：Feature Slice", f"- workbench/tmp/featureslice/ghost.md => {CONTAINS}：Feature Slice")
        state.update({"wrote": False, "commands": [], "files": {TOOL: TOOL_CONTENT, NOTES: NOTES_CONTENT}, "evidence": VALID_EVIDENCE})
        und_reply, _ = bridge.prepare_reply(und_command, ctx)
        und_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(und_reply)))
        check("4 undeclared acceptance target fails", und_meta.get("acceptance_fidelity") == "failed" and "not in declared" in und_meta.get("acceptance_fidelity_reason", ""), und_meta.get("acceptance_fidelity_reason"))

        # 5: missing file (claude "forgets" to write notes.md)
        state.update({"wrote": False, "commands": [], "files": {TOOL: TOOL_CONTENT}, "evidence": VALID_EVIDENCE})
        missing_reply, _ = bridge.prepare_reply(SLICE_COMMAND, ctx)
        missing_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(missing_reply)))
        check("5 missing file fails acceptance", missing_meta.get("acceptance_fidelity") == "failed" and "not verifiable" in missing_meta.get("acceptance_fidelity_reason", ""), missing_meta.get("acceptance_fidelity_reason"))

        # 6: non-empty fails on whitespace-only file
        ws_command = SLICE_COMMAND.replace(f"- {TOOL} => {CONTAINS}：def answer", f"- {TOOL} => {NON_EMPTY}")
        state.update({"wrote": False, "commands": [], "files": {TOOL: "   \n\n", NOTES: NOTES_CONTENT}, "evidence": VALID_EVIDENCE})
        ws_reply, _ = bridge.prepare_reply(ws_command, ctx)
        ws_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(ws_reply)))
        check("6 whitespace-only fails non-empty", ws_meta.get("acceptance_fidelity") == "failed" and "empty" in ws_meta.get("acceptance_fidelity_reason", ""), ws_meta.get("acceptance_fidelity_reason"))

        # 7 covered by case 14 (contains literal absent).

        # ---- 15. file-pack exact-one-line still passes ----
        pack_command = "\n".join(
            [
                f"/auto task {HEAD_CU}",
                "- workbench/tmp/filepack/a.txt",
                "- workbench/tmp/filepack/b.md",
                ACC_HEAD,
                f"- workbench/tmp/filepack/a.txt => {WRITE_ONLY}alpha ok",
                f"- workbench/tmp/filepack/b.md => {WRITE_ONLY}beta ok",
                "Do not modify source code. No git add/commit/push. --project auto_exec",
            ]
        )
        state.update({"wrote": False, "commands": [], "files": {"workbench/tmp/filepack/a.txt": "alpha ok\n", "workbench/tmp/filepack/b.md": "beta ok\n"}, "evidence": ""})
        pack_reply, _ = bridge.prepare_reply(pack_command, ctx)
        pack_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(pack_reply)))
        check("15 file-pack exact-one-line still passes", pack_meta.get("acceptance_fidelity") == "passed" and "auto_decision: pass" in pack_reply, pack_reply[-400:])
        check("15 no validation declared -> unknown", pack_meta.get("required_validation_detected") == "false" and pack_meta.get("required_validation_status") == "unknown", pack_meta.get("required_validation_status"))

        # ---- 16. single-file CJK exact-one-line still passes ----
        state.update({"wrote": False, "commands": [], "files": {"workbench/tmp/cn-single.txt": "cn single ok\n"}, "evidence": ""})
        single_reply, _ = bridge.prepare_reply(
            f"/auto task {PHRASE_CU} workbench/tmp/cn-single.txt. {WRITE_ONLY}cn single ok. Do not modify source code. No git add/commit/push. --project auto_exec",
            ctx,
        )
        single_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(single_reply)))
        check("16 single CJK still passes", single_meta.get("acceptance_fidelity") == "passed" and "auto_decision: pass" in single_reply, single_reply[-400:])

        # ---- 17. no-criteria case unchanged ----
        state.update({"wrote": False, "commands": [], "files": {"workbench/tmp/free-file.txt": "anything\nmore\n"}, "evidence": ""})
        free_reply, _ = bridge.prepare_reply(
            f"/auto task {PHRASE_CU} workbench/tmp/free-file.txt. Do not modify source code. No git add/commit/push. --project auto_exec",
            ctx,
        )
        free_meta = bridge.task_metadata(bridge.read_exec(extract_exec_id(free_reply)))
        check("17 no criteria unknown", free_meta.get("acceptance_fidelity") == "unknown" and free_meta.get("acceptance_criteria_detected") == "false", free_meta.get("acceptance_fidelity"))
        check("17 not over-gated", "auto_decision: pass" in free_reply, free_reply[-400:])
    finally:
        bridge.run_allowlisted_external_command = orig_runner
        bridge.run_allowlisted_post_run_command = orig_post
        bridge.probe_codex_noninteractive = orig_probe_codex
        bridge.probe_claude_workspace_write = orig_probe_claude_ww
        restore_paths(old_paths)

print("\nALL FEATURE SLICE CHECKS PASSED")
