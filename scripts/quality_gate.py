"""One-command quality gate for Atlas Clutch.

Runs the committed quality suite from the repository root - compile, public
safety scan, every committed smoke, the synthetic gate checks, and the git
whitespace check - then prints a summary table. Exit code 0 only if every
check passed.

Usage:
  python -B scripts/quality_gate.py        # local mode
  python -B scripts/quality_gate.py --ci   # CI mode

Local and CI modes run the same suite today; each check declares the modes
it belongs to so mode-specific differences can be added without touching
the runner.

Design rules:
- Standard library only; no PyYAML, no new dependencies.
- All checks run even after a failure, so one run reports every broken
  check; the gate fails at the end if anything failed.
- A failing check's full output is printed immediately for debugging;
  passing checks stay quiet beyond their one-line status.
- The gate itself reads no .env, credentials, tokens, private logs, or
  workbench runtime ledger contents; it only spawns the committed checks,
  which hold the same boundary.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK_TIMEOUT_SECONDS = 900
PY = sys.executable or "python"


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    modes: frozenset[str] = frozenset({"local", "ci"})


def py(*args: str) -> tuple[str, ...]:
    return (PY, "-B", *args)


# Single source of truth for the Atlas Clutch quality suite. CI calls this
# script instead of duplicating the list in workflow YAML. Order matters:
# compile and the safety scan run first, fast smokes next, the slower
# synthetic gate checks after, and the git whitespace check last so it also
# proves the suite left the tracked tree unmodified.
CHECKS: tuple[Check, ...] = (
    Check("compile bridge.py", py("-m", "py_compile", "bridge.py")),
    Check("safety scan (tracked files)", py("scripts/ci_safety_check.py")),
    Check("smoke_runtime", py("smoke_runtime.py")),
    Check("smoke_exec_start", py("smoke_exec_start.py")),
    Check("smoke_task_loop", py("smoke_task_loop.py")),
    Check("smoke_tools_status", py("smoke_tools_status.py")),
    Check("smoke_tool_adapters", py("smoke_tool_adapters.py")),
    Check("smoke_tool_evidence", py("smoke_tool_evidence.py")),
    Check("smoke_auto_pipeline", py("smoke_auto_pipeline.py")),
    Check("smoke_exec_rerun", py("smoke_exec_rerun.py")),
    Check("smoke_metrics_snapshot", py("smoke_metrics_snapshot.py")),
    Check("filepack synthetic check", py("filepack_synthetic_check.py")),
    Check("autocommit guard synthetic check", py("autocommit_guard_synthetic_check.py")),
    Check("featureslice synthetic check", py("featureslice_synthetic_check.py")),
    Check("git diff --check", ("git", "diff", "--check")),
)


@dataclass
class Result:
    check: Check
    status: str  # PASS | FAIL | TIMEOUT | ERROR
    returncode: int | None
    duration_seconds: float
    output: str


def run_check(check: Check) -> Result:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(check.argv),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
            env=env,
        )
        duration = time.monotonic() - started
        output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        status = "PASS" if proc.returncode == 0 else "FAIL"
        return Result(check, status, proc.returncode, duration, output.strip())
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        output = f"timed out after {CHECK_TIMEOUT_SECONDS}s\n{exc.stdout or ''}\n{exc.stderr or ''}"
        return Result(check, "TIMEOUT", None, duration, output.strip())
    except OSError as exc:
        duration = time.monotonic() - started
        return Result(check, "ERROR", None, duration, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas Clutch one-command quality gate")
    parser.add_argument("--ci", action="store_true", help="run in CI mode")
    args = parser.parse_args()
    mode = "ci" if args.ci else "local"

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    checks = [check for check in CHECKS if mode in check.modes]
    print(f"Atlas Clutch quality gate | mode: {mode} | checks: {len(checks)}")
    print(f"root: {ROOT}")
    print()

    results: list[Result] = []
    for index, check in enumerate(checks, start=1):
        print(f"[{index:>2}/{len(checks)}] {check.name} ...", flush=True)
        result = run_check(check)
        results.append(result)
        print(f"        {result.status} ({result.duration_seconds:.1f}s)", flush=True)
        if result.status != "PASS":
            print(f"----- output: {check.name} (rc={result.returncode}) -----")
            print(result.output or "(no output)")
            print("----- end output -----", flush=True)

    width = max(len(result.check.name) for result in results)
    print()
    print("Quality gate summary")
    print("-" * (width + 22))
    for result in results:
        print(f"{result.check.name:<{width}}  {result.status:<7}  {result.duration_seconds:>7.1f}s")
    print("-" * (width + 22))
    failed = [result for result in results if result.status != "PASS"]
    passed_count = len(results) - len(failed)
    print(f"passed: {passed_count}/{len(results)}")
    if failed:
        print("quality_gate: FAIL - " + ", ".join(result.check.name for result in failed))
        return 1
    print("quality_gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
