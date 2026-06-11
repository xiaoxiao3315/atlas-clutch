"""Five-core-metrics snapshot for Atlas Clutch (strategy doc §6.1).

Computes, from the workbench ledger only:
  1. median_time_to_accepted   - task created -> user decision pass (minutes)
  2. human_handoff_proxy       - manual-copy dispatches per closed task
  3. evidence_completeness     - closed tasks with verified_evidence_ready closure
  4. first_pass_review_rate    - closed tasks passed with no needs_evidence/blocked cycle
  5. auto_recovery_rate        - failed execs recovered without manual redo

Cohorts: ALL / REAL / SYNTHETIC (synthetic = harness self-test projects and
file-pack style titles), so harness noise never inflates the headline numbers.

Usage:
  python -B scripts/metrics_snapshot.py            # last 7 days, text
  python -B scripts/metrics_snapshot.py --days 30
  python -B scripts/metrics_snapshot.py --all --json

Boundary: read-only over workbench/tasks, dispatches, executions (plus the
mkdir-if-missing that every bridge ledger reader performs). It reads no .env,
no tokens, no logs, and never writes or modifies ledger entries. Standard
library only; reuses bridge.py parsers so ledger semantics stay canonical.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402  (parsers + canonical ledger paths)

CLOSED_OK = {"passed", "archived"}
SYNTHETIC_PROJECTS = {"auto_exec", "auto-dispatch-runner", "retro-live"}
SYNTHETIC_TITLE = re.compile(
    r"filepack|featureslice|live-tool|synthetic|smoke|payload|只允许创建或更新", re.IGNORECASE
)
TIMELINE_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}T\S+) (.+)$", re.MULTILINE)
TARGETS = {
    "median_time_to_accepted_minutes": "phase-1: -30% vs baseline",
    "human_handoff_proxy_per_closed_task": "phase-1: <= 6",
    "evidence_completeness_rate": "phase-1: >= 80%",
    "first_pass_review_rate": "phase-1: >= 40%",
    "auto_recovery_rate": "phase-1: >= 0% (M6 not built yet)",
}


def parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value.strip())
        return ts if ts.tzinfo else ts.astimezone()
    except (ValueError, TypeError):
        return None


def timeline_events(text: str) -> list[tuple[datetime, str]]:
    events = []
    for match in TIMELINE_LINE.finditer(bridge.task_section(text, "Timeline")):
        ts = parse_ts(match.group(1))
        if ts:
            events.append((ts, match.group(2).strip()))
    return sorted(events, key=lambda item: item[0])


def is_synthetic(project_id: str, title: str) -> bool:
    return project_id in SYNTHETIC_PROJECTS or bool(SYNTHETIC_TITLE.search(title or ""))


def load_tasks(since: datetime | None) -> list[dict]:
    tasks = []
    for path in sorted(bridge.TASKS_DIR.glob("OHB-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = bridge.task_metadata(text)
        created = parse_ts(meta.get("created_at", ""))
        if created is None or (since and created < since):
            continue
        events = timeline_events(text)
        decisions = [(ts, line) for ts, line in events if "user decision" in line]
        pass_ts = next((ts for ts, line in decisions if "user decision pass" in line), None)
        rework = sum(
            1 for _, line in decisions
            if "needs_evidence" in line or "blocked" in line
        )
        closure = bridge.task_section(text, "Closure Evidence")
        closure_ok = "evidence_closure_state: verified_evidence_ready" in closure
        tasks.append(
            {
                "task_id": path.stem,
                "title": bridge.task_title_from_text(path.stem, text),
                "project_id": meta.get("project_id", ""),
                "status": meta.get("status", "unknown"),
                "created": created,
                "pass_ts": pass_ts,
                "rework_cycles": rework,
                "closure_ok": closure_ok,
            }
        )
    return tasks


def load_manual_dispatch_count(since: datetime | None) -> int:
    count = 0
    for path in sorted(bridge.DISPATCHES_DIR.glob("DISPATCH-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = bridge.task_metadata(text)
        created = parse_ts(meta.get("created_at", ""))
        if since and (created is None or created < since):
            continue
        if "manual_copy_only: true" in bridge.task_section(text, "Sent Record"):
            count += 1
    return count


def load_exec_failures(since: datetime | None) -> dict:
    execs = []
    for path in sorted(bridge.EXECUTIONS_DIR.glob("EXEC-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = bridge.task_metadata(text)
        created = parse_ts(meta.get("created_at", ""))
        if since and (created is None or created < since):
            continue
        execs.append(
            {
                "exec_id": path.stem,
                "dispatch_id": meta.get("dispatch_id", ""),
                "status": meta.get("status", ""),
                "completion_state": meta.get("completion_state", ""),
                "returncode": meta.get("returncode", ""),
                "timed_out": meta.get("timed_out", ""),
                "created": created,
            }
        )
    def failed(e: dict) -> bool:
        return (
            e["status"] in {"needs_manual_start", "failed"}
            or e["timed_out"] == "true"
            or (e["completion_state"] not in {"completed", ""})
            or (e["returncode"] not in {"0", ""})
        )
    failures = [e for e in execs if failed(e)]
    ok_by_dispatch: dict[str, list[datetime]] = {}
    for e in execs:
        if not failed(e) and e["completion_state"] == "completed" and e["dispatch_id"]:
            ok_by_dispatch.setdefault(e["dispatch_id"], []).append(e["created"])
    recovered = sum(
        1 for e in failures
        if e["dispatch_id"]
        and any(ts and e["created"] and ts > e["created"] for ts in ok_by_dispatch.get(e["dispatch_id"], []))
    )
    return {"failure_count": len(failures), "recovered_any_means": recovered, "auto_recovered": 0}


def pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def cohort_metrics(tasks: list[dict], manual_dispatches: int, exec_stats: dict, scale: float) -> dict:
    closed = [t for t in tasks if t["status"] in CLOSED_OK]
    accepted = [t for t in closed if t["pass_ts"]]
    durations = [
        (t["pass_ts"] - t["created"]).total_seconds() / 60.0
        for t in accepted
        if t["pass_ts"] >= t["created"]
    ]
    first_pass = [t for t in accepted if t["rework_cycles"] == 0]
    complete = [t for t in closed if t["closure_ok"]]
    return {
        "tasks_created": len(tasks),
        "tasks_closed": len(closed),
        "median_time_to_accepted_minutes": round(statistics.median(durations), 1) if durations else None,
        "human_handoff_proxy_per_closed_task": (
            round(manual_dispatches * scale / len(closed), 2) if closed else None
        ),
        "evidence_completeness_rate": pct(len(complete), len(closed)),
        "first_pass_review_rate": pct(len(first_pass), len(accepted)),
        "auto_recovery_rate": pct(exec_stats["auto_recovered"], exec_stats["failure_count"]),
        "rework_cycles_total": sum(t["rework_cycles"] for t in tasks),
        "failures": exec_stats["failure_count"],
        "recovered_any_means": exec_stats["recovered_any_means"],
    }


def render(name: str, metrics: dict) -> str:
    lines = [f"cohort: {name} (created={metrics['tasks_created']}, closed={metrics['tasks_closed']})"]
    for key in (
        "median_time_to_accepted_minutes",
        "human_handoff_proxy_per_closed_task",
        "evidence_completeness_rate",
        "first_pass_review_rate",
        "auto_recovery_rate",
    ):
        value = metrics[key]
        shown = "n/a" if value is None else value
        suffix = "%" if key.endswith("rate") and value is not None else ""
        lines.append(f"  {key:<38} {shown}{suffix:<4} [{TARGETS[key]}]")
    lines.append(
        f"  supporting: rework_cycles={metrics['rework_cycles_total']}, "
        f"exec_failures={metrics['failures']}, recovered_any_means={metrics['recovered_any_means']}"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas Clutch five-metrics snapshot (read-only)")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--all", action="store_true", help="ignore window, scan full ledger")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    since = None if args.all else datetime.now().astimezone() - timedelta(days=args.days)
    tasks = load_tasks(since)
    manual_dispatches = load_manual_dispatch_count(since)
    exec_stats = load_exec_failures(since)

    real = [t for t in tasks if not is_synthetic(t["project_id"], t["title"])]
    synthetic = [t for t in tasks if is_synthetic(t["project_id"], t["title"])]
    share = (len(real) / len(tasks)) if tasks else 0.0

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "window": "all" if args.all else f"last {args.days} days",
        "note_handoff": "proxy = manual-copy dispatches per closed task; manual dispatch split between cohorts by task share",
        "note_auto_recovery": "always 0 until /exec rerun exists (audit M6); recovered_any_means counts manual redo",
        "cohorts": {
            "ALL": cohort_metrics(tasks, manual_dispatches, exec_stats, 1.0),
            "REAL": cohort_metrics(real, manual_dispatches, exec_stats, share),
            "SYNTHETIC": cohort_metrics(synthetic, manual_dispatches, exec_stats, 1.0 - share),
        },
        "synthetic_projects": sorted(SYNTHETIC_PROJECTS),
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"Atlas Clutch metrics snapshot | {result['window']} | generated {result['generated_at']}")
    print(f"synthetic cohort = projects {sorted(SYNTHETIC_PROJECTS)} or file-pack style titles")
    print()
    for name in ("ALL", "REAL", "SYNTHETIC"):
        print(render(name, result["cohorts"][name]))
        print()
    print("notes:")
    print(f"- {result['note_handoff']}")
    print(f"- {result['note_auto_recovery']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
