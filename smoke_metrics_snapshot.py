"""Smoke: scripts/metrics_snapshot.py runs read-only and emits a valid snapshot.

Asserts JSON mode returns the three cohorts with all five metric keys, and
text mode renders. Works on an empty ledger (CI) and a populated one (local).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import bridge
import scripts.metrics_snapshot as metrics_snapshot
from smoke_exec import configure_temp_paths, restore_paths

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "scripts" / "metrics_snapshot.py"
METRIC_KEYS = (
    "median_time_to_accepted_minutes",
    "human_handoff_proxy_per_closed_task",
    "evidence_completeness_rate",
    "first_pass_review_rate",
    "auto_recovery_rate",
    "recovered_via_rerun",
)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def controlled_fixture_check() -> None:
    with tempfile.TemporaryDirectory(prefix="ohb-metrics-fixture-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        old_paths = configure_temp_paths(root)
        try:
            bridge.ensure_workbench_dirs()
            write(
                bridge.TASKS_DIR / "OHB-20260611-100000.md",
                """# OHB-20260611-100000 Real customer task

status: archived
created_at: 2026-06-11T10:00:00+00:00
updated_at: 2026-06-11T10:10:00+00:00
project_id: client_alpha

## Timeline
- 2026-06-11T10:00:00+00:00 task created.
- 2026-06-11T10:10:00+00:00 user decision pass; status passed.

## Closure Evidence
- evidence_closure_state: verified_evidence_ready
""",
            )
            write(
                bridge.TASKS_DIR / "OHB-20260611-110000.md",
                """# OHB-20260611-110000 Smoke synthetic task

status: archived
created_at: 2026-06-11T11:00:00+00:00
updated_at: 2026-06-11T11:03:00+00:00
project_id: auto_exec

## Timeline
- 2026-06-11T11:00:00+00:00 task created.
- 2026-06-11T11:03:00+00:00 user decision pass; status passed.

## Closure Evidence
- evidence_closure_state: verified_evidence_ready
""",
            )
            write(
                bridge.EXECUTIONS_DIR / "EXEC-20260611-100001.md",
                """# EXEC-20260611-100001 Real customer task

exec_id: EXEC-20260611-100001
dispatch_id: DISPATCH-20260611-100001
task_id: OHB-20260611-100000
project_id: client_alpha
status: failed
created_at: 2026-06-11T10:01:00+00:00
completion_state: failed
returncode: 1
timed_out: false

## Status Timeline
- failed
""",
            )
            write(
                bridge.EXECUTIONS_DIR / "EXEC-20260611-110001.md",
                """# EXEC-20260611-110001 Smoke synthetic task

exec_id: EXEC-20260611-110001
dispatch_id: DISPATCH-20260611-110001
task_id: OHB-20260611-110000
project_id: auto_exec
status: failed
created_at: 2026-06-11T11:01:00+00:00
completion_state: failed
returncode: 1
timed_out: false

## Status Timeline
- failed
""",
            )
            write(
                bridge.EXECUTIONS_DIR / "EXEC-20260611-110002.md",
                """# EXEC-20260611-110002 Smoke synthetic task

exec_id: EXEC-20260611-110002
dispatch_id: DISPATCH-20260611-110001
task_id: OHB-20260611-110000
project_id: auto_exec
status: returned
created_at: 2026-06-11T11:02:00+00:00
completion_state: completed
returncode: 0
timed_out: false
recovered_from: EXEC-20260611-110001

## Status Timeline
- recovered
""",
            )
            task_index = metrics_snapshot.load_task_index()
            tasks = metrics_snapshot.load_tasks(None)
            execs = metrics_snapshot.load_execs(None, task_index)
            real = [t for t in tasks if not t["synthetic"]]
            synthetic = [t for t in tasks if t["synthetic"]]
            real_execs = [e for e in execs if not e["synthetic"]]
            synthetic_execs = [e for e in execs if e["synthetic"]]
            all_metrics = metrics_snapshot.cohort_metrics(tasks, 0, metrics_snapshot.exec_failure_stats(execs))
            real_metrics = metrics_snapshot.cohort_metrics(real, 0, metrics_snapshot.exec_failure_stats(real_execs))
            synthetic_metrics = metrics_snapshot.cohort_metrics(
                synthetic,
                0,
                metrics_snapshot.exec_failure_stats(synthetic_execs),
            )
            if all_metrics["failures"] != 2:
                raise AssertionError(f"ALL failure count polluted fixture expectation: {all_metrics}")
            if real_metrics["failures"] != 1 or real_metrics["recovered_via_rerun"] != 0:
                raise AssertionError(f"REAL cohort should only see the real failed exec: {real_metrics}")
            if synthetic_metrics["failures"] != 1 or synthetic_metrics["recovered_via_rerun"] != 1:
                raise AssertionError(f"SYNTHETIC cohort should only see synthetic failure/recovery: {synthetic_metrics}")
            if real_metrics == synthetic_metrics:
                raise AssertionError("REAL and SYNTHETIC metrics unexpectedly identical; cohort split may be global")
        finally:
            restore_paths(old_paths)


def main() -> int:
    controlled_fixture_check()

    json_proc = run("--all", "--json")
    if json_proc.returncode != 0:
        raise AssertionError(f"json mode failed rc={json_proc.returncode}: {json_proc.stderr[:500]}")
    data = json.loads(json_proc.stdout)
    if data.get("window") != "all":
        raise AssertionError(f"unexpected window: {data.get('window')!r}")
    cohorts = data.get("cohorts", {})
    for name in ("ALL", "REAL", "SYNTHETIC"):
        cohort = cohorts.get(name)
        if not isinstance(cohort, dict):
            raise AssertionError(f"missing cohort: {name}")
        for key in METRIC_KEYS:
            if key not in cohort:
                raise AssertionError(f"cohort {name} missing key: {key}")

    text_proc = run("--days", "7")
    if text_proc.returncode != 0:
        raise AssertionError(f"text mode failed rc={text_proc.returncode}: {text_proc.stderr[:500]}")
    if "Atlas Clutch metrics snapshot" not in text_proc.stdout:
        raise AssertionError("text mode missing header")

    print("smoke_metrics_snapshot passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
