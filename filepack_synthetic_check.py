"""Synthetic check: bounded file-pack parsing, bullet-truncation hardening.

Parser-level only (no live flows, no file writes). Validates:
  1. Core pack extraction still works (EN + CJK headings, both colon widths).
  2. Unsafe paths refuse the whole pack (absolute, traversal, .env, extension).
  3. Hardening: a dash line inside a pack block that fails the strict
     bullet shape (path with spaces, trailing tokens) marks the pack unsafe
     instead of silently ending the bullet loop and truncating user intent.
  4. A non-dash line still ends the block normally.
  5. >5 targets refused; empty pack refused; flattened heading not detected.

Run: py -3 -B filepack_synthetic_check.py
"""
import bridge

A_REL = "workbench/tmp/filepack/a.txt"
B_REL = "workbench/tmp/filepack/b.md"
HEAD_EN = "Allowed write targets:"
HEAD_CU_FW = "只允许创建或更新："
HEAD_CU_ASCII = "只允许创建或更新:"


def check(name, ok, detail=""):
    assert ok, f"FAIL {name}: {detail}"
    print(f"PASS {name}")


# ---- 1. core extraction (regression) ----
pack_en = bridge.extract_file_pack_targets(f"{HEAD_EN}\n- {A_REL}\n- {B_REL}")
check("1a EN pack extracts two targets", pack_en["ok"] and pack_en["targets"] == [A_REL, B_REL], pack_en)
pack_cn = bridge.extract_file_pack_targets(f"{HEAD_CU_FW}\n- {A_REL}\n- {B_REL}")
check("1b CJK pack (fullwidth colon)", pack_cn["ok"] and pack_cn["targets"] == [A_REL, B_REL], pack_cn)
pack_cn2 = bridge.extract_file_pack_targets(f"{HEAD_CU_ASCII}\n- {A_REL}\n- {B_REL}")
check("1c CJK pack (ASCII colon)", pack_cn2["ok"] and pack_cn2["targets"] == [A_REL, B_REL], pack_cn2)

# ---- 2. unsafe paths refuse the whole pack (regression) ----
for label, bad in (
    ("absolute drive", "C:/evil.txt"),
    ("absolute posix", "/etc/evil.txt"),
    ("traversal", "../evil.txt"),
    ("backslash traversal", "..\\evil.txt"),
    ("env-like", "config/.env.txt"),
    ("disallowed extension", "tool.exe"),
):
    pack = bridge.extract_file_pack_targets(f"{HEAD_EN}\n- {A_REL}\n- {bad}")
    check(f"2 unsafe refuses pack ({label})", pack["found"] and not pack["ok"] and "unsafe" in pack["reason"], pack)

# ---- 3. malformed dash line inside the block marks the pack unsafe ----
spaced = bridge.extract_file_pack_targets(f"{HEAD_EN}\n- {A_REL}\n- my file.txt\n- {B_REL}")
check(
    "3a path-with-spaces bullet refuses pack",
    spaced["found"] and not spaced["ok"] and "unsafe" in spaced["reason"],
    spaced,
)
check(
    "3b later valid bullets still parsed (loop continues past unsafe line)",
    B_REL in spaced["targets"],
    spaced,
)
trailing = bridge.extract_file_pack_targets(f"{HEAD_EN}\n- {A_REL} please\n- {B_REL}")
check(
    "3c bullet with trailing tokens refuses pack",
    trailing["found"] and not trailing["ok"] and "unsafe" in trailing["reason"],
    trailing,
)
bare_dash = bridge.extract_file_pack_targets(f"{HEAD_EN}\n-broken.txt\n- {B_REL}")
check(
    "3d dash without space refuses pack",
    bare_dash["found"] and not bare_dash["ok"],
    bare_dash,
)

# ---- 4. non-dash line still ends the block normally ----
ended = bridge.extract_file_pack_targets(
    f"{HEAD_EN}\n- {A_REL}\n\nUnrelated prose line.\n- {B_REL}"
)
check(
    "4 blank/prose line ends block; stray bullet outside block ignored",
    ended["ok"] and ended["targets"] == [A_REL],
    ended,
)

# ---- 5. bounds (regression) ----
six = "\n".join([HEAD_EN] + [f"- workbench/tmp/filepack/f{i}.txt" for i in range(6)])
pack6 = bridge.extract_file_pack_targets(six)
check("5a six targets refused", pack6["found"] and not pack6["ok"] and "maximum" in pack6["reason"], pack6)
empty = bridge.extract_file_pack_targets(f"{HEAD_EN}\nNo bullets here.")
check("5b heading without bullets refused", empty["found"] and not empty["ok"], empty)
flattened = bridge.extract_file_pack_targets(f"{HEAD_EN} - {A_REL} - {B_REL}")
check("5c flattened single-line heading not detected as pack", not flattened["found"], flattened)
dup = bridge.extract_file_pack_targets(f"{HEAD_EN}\n- {A_REL}\n- {A_REL}\n- {B_REL}")
check("5d duplicates dedupe", dup["ok"] and dup["targets"] == [A_REL, B_REL], dup)

print("ALL CHECKS PASSED")
