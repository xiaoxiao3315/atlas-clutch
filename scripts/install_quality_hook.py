"""Install a pre-push git hook that runs the quality gate.

Usage: python -B scripts/install_quality_hook.py

Writes .git/hooks/pre-push so every push first runs
`python -B scripts/quality_gate.py` and is blocked on failure
(bypass in an emergency with `git push --no-verify`).
Standard library only; touches nothing outside .git/hooks.
"""
from __future__ import annotations

import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOOK = """#!/bin/sh
# Installed by scripts/install_quality_hook.py - Atlas Clutch quality gate.
echo "[pre-push] running Atlas Clutch quality gate (13 checks)..."
python -B scripts/quality_gate.py
status=$?
if [ $status -ne 0 ]; then
  echo "[pre-push] quality gate FAILED; push blocked."
  echo "[pre-push] fix the failure, or bypass once with: git push --no-verify"
  exit $status
fi
echo "[pre-push] quality gate passed."
exit 0
"""


def main() -> int:
    hooks_dir = ROOT / ".git" / "hooks"
    if not hooks_dir.is_dir():
        print(f"error: {hooks_dir} not found (not a git repo?)")
        return 1
    target = hooks_dir / "pre-push"
    if target.exists() and "Atlas Clutch quality gate" not in target.read_text(encoding="utf-8", errors="replace"):
        backup = hooks_dir / "pre-push.backup"
        target.replace(backup)
        print(f"existing pre-push hook moved to {backup}")
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed: {target}")
    print("every git push now runs the quality gate first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
