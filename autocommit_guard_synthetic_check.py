"""Synthetic check: worktree ownership guard for auto commit/close.

Parser-level only (no live flows, no git, no file writes). Validates
evaluate_worktree_ownership — the gate that blocks the auto pipeline from
committing or auto-closing over working-tree changes not attributable to the
current run's declared allowed-write targets:
  1. Changes confined to declared targets -> owned (valid flow preserved).
  2. Post-run changed paths outside targets -> blocked
     (changed_paths_outside_run_targets).
  3. Pre-run dirty paths outside targets -> blocked (unowned_dirty_worktree),
     even when the run itself wrote nothing new.
  4. Pre-run dirty path that IS a declared target -> owned.
  5. git status unavailable in either snapshot -> blocked (fail closed).
  6. No declared targets + any dirt -> blocked.
  7. Untracked (??) and renamed (R old -> new) entries are attributed.
  8. File-pack targets feed the guard (regression for 0090199/16df7a9).

Run: py -3 -B autocommit_guard_synthetic_check.py
"""
import bridge

TARGET_A = "workbench/tmp/guard/a.txt"
TARGET_B = "workbench/tmp/guard/b.md"


def check(name, ok, detail=""):
    assert ok, f"FAIL {name}: {detail}"
    print(f"PASS {name}")


def snap(status_stdout: str, returncode: int = 0) -> str:
    """Fabricate a snapshot in collect_post_run_snapshot's format."""
    return "\n".join(
        [
            "### git status --short",
            f"- returncode: {returncode}",
            "stdout:",
            "```text",
            status_stdout.strip() or "- empty",
            "```",
            "stderr:",
            "```text",
            "- empty",
            "```",
        ]
    )


CLEAN = snap("")

# ---- 1. owned: post-run changes confined to declared targets ----
owned = bridge.evaluate_worktree_ownership([TARGET_A, TARGET_B], snap(f" M {TARGET_A}\n?? {TARGET_B}"), CLEAN)
check("1 changes inside targets are owned", owned["status"] == "owned", owned)

# ---- 2. post-run path outside targets blocks ----
outside = bridge.evaluate_worktree_ownership([TARGET_A], snap(f" M {TARGET_A}\n M bridge.py"), CLEAN)
check(
    "2 changed path outside targets blocks",
    outside["status"] == "blocked" and outside["reason"].startswith("changed_paths_outside_run_targets"),
    outside,
)
check("2b unowned path reported", "bridge.py" in outside["unowned_paths"], outside)

# ---- 3. pre-run dirty unowned path blocks, even with no new changes ----
predirty = bridge.evaluate_worktree_ownership([TARGET_A], snap(" M bridge.py"), snap(" M bridge.py"))
check(
    "3 pre-run unowned dirt blocks (the mid-flight-agent race)",
    predirty["status"] == "blocked" and predirty["reason"].startswith("unowned_dirty_worktree"),
    predirty,
)

# ---- 4. pre-run dirty path that IS a target stays owned ----
owned_dirty = bridge.evaluate_worktree_ownership([TARGET_A], snap(f" M {TARGET_A}"), snap(f" M {TARGET_A}"))
check("4 pre-run dirt on a declared target is owned", owned_dirty["status"] == "owned", owned_dirty)

# ---- 5. git status unavailable fails closed ----
no_pre = bridge.evaluate_worktree_ownership([TARGET_A], snap(f" M {TARGET_A}"), snap("", returncode=1))
check(
    "5a failed pre-run git status blocks",
    no_pre["status"] == "blocked" and no_pre["reason"].startswith("auto_commit_blocked"),
    no_pre,
)
no_post = bridge.evaluate_worktree_ownership([TARGET_A], "", CLEAN)
check("5b missing post-run git status blocks", no_post["status"] == "blocked", no_post)

# ---- 6. no declared targets: any dirt is unowned ----
no_targets = bridge.evaluate_worktree_ownership([], snap(f" M {TARGET_A}"), CLEAN)
check("6 dirt without declared targets blocks", no_targets["status"] == "blocked", no_targets)

# ---- 7. untracked and renamed entries are attributed ----
renamed = bridge.evaluate_worktree_ownership([TARGET_A], snap(f"R  old.txt -> {TARGET_A}"), CLEAN)
check("7a rename attributes the new path", renamed["status"] == "owned", renamed)
untracked = bridge.evaluate_worktree_ownership([TARGET_A], snap("?? stray.txt"), CLEAN)
check(
    "7b untracked stray file blocks",
    untracked["status"] == "blocked" and "stray.txt" in untracked["unowned_paths"],
    untracked,
)

# ---- 8. file-pack regression: pack targets feed the guard ----
pack = bridge.extract_file_pack_targets(f"Allowed write targets:\n- {TARGET_A}\n- {TARGET_B}")
check("8a file pack still parses", pack["ok"] and pack["targets"] == [TARGET_A, TARGET_B], pack)
pack_unsafe = bridge.extract_file_pack_targets("Allowed write targets:\n- ../evil.txt")
check("8b unsafe pack still refused", pack_unsafe["found"] and not pack_unsafe["ok"], pack_unsafe)
pack_owned = bridge.evaluate_worktree_ownership(pack["targets"], snap(f" M {TARGET_A}\n M {TARGET_B}"), CLEAN)
check("8c pack targets drive ownership", pack_owned["status"] == "owned", pack_owned)

print("ALL CHECKS PASSED")
