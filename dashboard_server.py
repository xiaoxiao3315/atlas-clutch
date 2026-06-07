from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
WORKBENCH_DIR = ROOT / "workbench"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PREVIEW = 220
RECENT_LIMIT = 20


def sensitive_patterns() -> list[re.Pattern[str]]:
    bot_prefix = "bf" + "_"
    key_prefix = "sk" + "-"
    auth_header = "Authorization" + r"\s*:\s*Bearer\s+\S+"
    cookie_header = "Cookie" + r"\s*:\s*\S+"
    return [
        re.compile(re.escape(bot_prefix) + r"[A-Za-z0-9._-]*", re.IGNORECASE),
        re.compile(re.escape(key_prefix) + r"[A-Za-z0-9._-]*", re.IGNORECASE),
        re.compile(auth_header, re.IGNORECASE),
        re.compile(cookie_header, re.IGNORECASE),
        re.compile(r"(?im)^\s*(password|api_key|secret)\s*[:=].*$"),
    ]


def sanitize_text(value: object, limit: int | None = None) -> str:
    text = str(value or "")
    for pattern in sensitive_patterns():
        text = pattern.sub("[REDACTED]", text)
    text = text.replace("\x00", "")
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 16)].rstrip() + " ...[truncated]"
    return text


def html_text(value: object) -> str:
    return html.escape(sanitize_text(value), quote=True)


def safe_read(path: Path, limit: int = 250_000) -> str:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return ""
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def parse_metadata(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line.strip())
        if match:
            meta[match.group(1)] = sanitize_text(match.group(2), 300)
    return meta


def title_from_markdown(record_id: str, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title.startswith(record_id):
                title = title[len(record_id):].strip()
            return sanitize_text(title or record_id, 160)
    return record_id


def section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^## .+$", text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    return sanitize_text(text[match.end():end], 1200)


def markdown_files(folder: str, prefix: str) -> list[Path]:
    directory = WORKBENCH_DIR / folder
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{prefix}*.md"), key=lambda path: path.stat().st_mtime, reverse=True)


def record_from_file(path: Path) -> tuple[str, str, dict[str, str]]:
    text = safe_read(path)
    return text, title_from_markdown(path.stem, text), parse_metadata(text)


def count_ids(text: str, prefix: str) -> int:
    return len(set(re.findall(rf"{re.escape(prefix)}-\d{{8}}-\d{{6}}(?:-\d{{2}})?", text)))


def latest_value(records: list[dict], key: str) -> str:
    return sanitize_text(records[0].get(key, "none") if records else "none")


def build_projects(tasks: list[dict]) -> list[dict]:
    task_by_project: dict[str, list[dict]] = {}
    for task in tasks:
        project_id = task.get("project_id") or "unassigned"
        task_by_project.setdefault(project_id, []).append(task)

    projects = []
    for path in markdown_files("projects", ""):
        text, title, meta = record_from_file(path)
        project_id = sanitize_text(meta.get("project_id") or path.stem, 120)
        related = task_by_project.get(project_id, [])
        projects.append(
            {
                "project_id": project_id,
                "title": title,
                "status": meta.get("status", "unknown"),
                "priority": meta.get("priority", "unknown"),
                "active_tasks": sum(1 for item in related if item.get("status") not in {"passed", "cancelled", "archived"}),
                "needs_evidence": sum(1 for item in related if item.get("status") == "needs_evidence" or item.get("evidence_gap_risk")),
                "updated_at": meta.get("updated_at", ""),
                "summary": section(text, "Summary") or section(text, "Notes"),
            }
        )
    return projects


def build_tasks() -> list[dict]:
    tasks = []
    for path in markdown_files("tasks", "OHB-"):
        text, title, meta = record_from_file(path)
        status = meta.get("status", "unknown")
        gap_text = section(text, "Evidence Gaps") or section(text, "Risks") or section(text, "Atlas Review")
        evidence_gap_risk = (
            meta.get("evidence_gap_risk", "").lower() == "true"
            or status == "needs_evidence"
            or "needs_evidence" in gap_text.lower()
        )
        tasks.append(
            {
                "task_id": path.stem,
                "project_id": meta.get("project_id", "unassigned") or "unassigned",
                "status": status,
                "title": title,
                "updated_at": meta.get("updated_at", ""),
                "evidence_gap_risk": evidence_gap_risk,
                "reason_summary": sanitize_text(gap_text or status, MAX_PREVIEW),
            }
        )
    return tasks


def build_dispatches() -> list[dict]:
    items = []
    for path in markdown_files("dispatches", "DISPATCH-"):
        text, title, meta = record_from_file(path)
        items.append(
            {
                "dispatch_id": path.stem,
                "task_id": meta.get("task_id", ""),
                "project_id": meta.get("project_id", "unassigned") or "unassigned",
                "target_executor": meta.get("target_executor", ""),
                "status": meta.get("status", "unknown"),
                "updated_at": meta.get("updated_at", ""),
                "title": title,
                "return_summary": sanitize_text(section(text, "Return Report"), MAX_PREVIEW),
            }
        )
    return items


def build_pilots() -> list[dict]:
    items = []
    for path in markdown_files("pilots", "PILOT-"):
        text, title, meta = record_from_file(path)
        metrics = section(text, "Metrics")
        friction = section(text, "Friction Log")
        items.append(
            {
                "pilot_id": path.stem,
                "project_id": meta.get("project_id", ""),
                "status": meta.get("status", "unknown"),
                "title": title,
                "task_count": count_ids(section(text, "Tasks Included"), "OHB"),
                "dispatch_count": count_ids(section(text, "Dispatches"), "DISPATCH"),
                "estimated_time_saved": sanitize_text(extract_metric(metrics, "estimated_time_saved") or section(text, "Time Saved Estimate"), 120),
                "main_friction": sanitize_text(extract_metric(metrics, "main_friction") or friction or "none recorded", MAX_PREVIEW),
            }
        )
    return items


def extract_metric(text: str, name: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped.startswith(name + ":"):
            return stripped.split(":", 1)[1].strip()
    return ""


def build_collections() -> list[dict]:
    items = []
    for path in markdown_files("collections", "COLLECT-"):
        _text, _title, meta = record_from_file(path)
        items.append(
            {
                "collection_id": path.stem,
                "task_id": meta.get("task_id", ""),
                "profile": meta.get("profile", ""),
                "created_at": meta.get("created_at", ""),
                "verified": meta.get("verified", "false") or "false",
            }
        )
    return items


def build_executions() -> list[dict]:
    items = []
    for path in markdown_files("executions", "EXEC-"):
        _text, _title, meta = record_from_file(path)
        items.append(
            {
                "exec_id": path.stem,
                "dispatch_id": meta.get("dispatch_id", ""),
                "task_id": meta.get("task_id", ""),
                "project_id": meta.get("project_id", "unassigned") or "unassigned",
                "target_executor": meta.get("target_executor", ""),
                "status": meta.get("status", "unknown"),
            }
        )
    return items


def build_context_packs() -> list[dict]:
    items = []
    for path in markdown_files("context_packs", "CTX-"):
        _text, title, meta = record_from_file(path)
        items.append(
            {
                "context_id": path.stem,
                "source_task_id": meta.get("source_task_id", ""),
                "source_project_id": meta.get("source_project_id", ""),
                "title": title,
                "created_at": meta.get("created_at", ""),
            }
        )
    return items


def count_files(folder: str) -> int:
    directory = WORKBENCH_DIR / folder
    if not directory.exists():
        return 0
    return sum(1 for path in directory.rglob("*.md") if path.is_file())


def build_learning_playbook_state() -> dict:
    return {
        "learning_records": count_files("learning"),
        "application_records": count_files("applications"),
        "playbook_records": count_files("playbooks"),
    }


def build_suggestions(summary: dict, projects: list[dict], tasks: list[dict], dispatches: list[dict], pilots: list[dict]) -> list[str]:
    suggestions: list[str] = []
    if summary["needs_evidence_tasks"]:
        suggestions.append("Needs-evidence tasks are present; prioritize adding verifiable evidence.")
    if any(item.get("status") == "ready" for item in dispatches):
        suggestions.append("Ready dispatches exist; prepare/package the handoff and record manual send state.")
    if any(item.get("status") == "returned" for item in dispatches):
        suggestions.append("Returned dispatches exist; run QA and then Atlas review.")
    if any(item.get("status") == "active" for item in pilots):
        suggestions.append("Active pilots exist; refresh metrics or complete the pilot when the trial is done.")
    if any(item.get("project_id") == "unassigned" for item in tasks):
        suggestions.append("Unassigned tasks exist; attach them to a project when useful.")
    if not suggestions:
        suggestions.append("No urgent dashboard action detected; continue the current Workbench loop.")
    return suggestions


def build_dashboard_data() -> dict:
    tasks = build_tasks()
    projects = build_projects(tasks)
    dispatches = build_dispatches()
    pilots = build_pilots()
    collections = build_collections()
    executions = build_executions()
    context_packs = build_context_packs()
    learning = build_learning_playbook_state()
    evidence_gaps = [
        task for task in tasks
        if task.get("status") == "needs_evidence" or task.get("evidence_gap_risk")
    ]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "read_only_dashboard",
        "bind": DEFAULT_HOST,
        "external_access": False,
        "auto_execute_enabled": False,
        "project_count": len(projects),
        "active_project_count": sum(1 for item in projects if item.get("status") == "active"),
        "task_count": len(tasks),
        "open_tasks": sum(1 for item in tasks if item.get("status") in {"draft", "open", "reported", "reviewed", "needs_evidence"}),
        "needs_evidence_tasks": sum(1 for item in tasks if item.get("status") == "needs_evidence"),
        "dispatch_count": len(dispatches),
        "returned_dispatches": sum(1 for item in dispatches if item.get("status") in {"returned", "qa_ready", "reviewed"}),
        "evidence_gap_count": len(evidence_gaps),
        "pilot_count": len(pilots),
        "collection_count": len(collections),
        "execution_count": len(executions),
        "context_pack_count": len(context_packs),
        "learning_records": learning["learning_records"],
        "playbook_records": learning["playbook_records"],
        "latest_task_id": latest_value(tasks, "task_id"),
        "latest_dispatch_id": latest_value(dispatches, "dispatch_id"),
        "latest_pilot_id": latest_value(pilots, "pilot_id"),
    }
    return {
        "summary": summary,
        "projects": projects,
        "tasks": tasks[:RECENT_LIMIT],
        "dispatches": dispatches[:RECENT_LIMIT],
        "evidence_gaps": evidence_gaps[:RECENT_LIMIT],
        "pilots": pilots[:RECENT_LIMIT],
        "collections": collections[:RECENT_LIMIT],
        "executions": executions[:RECENT_LIMIT],
        "context_packs": context_packs[:RECENT_LIMIT],
        "learning": learning,
        "suggestions": build_suggestions(summary, projects, tasks, dispatches, pilots),
    }


def card(label: str, value: object) -> str:
    return f'<div class="metric"><span>{html_text(label)}</span><strong>{html_text(value)}</strong></div>'


def render_table(headers: list[str], rows: list[dict], empty: str = "none") -> str:
    if not rows:
        return f'<p class="empty">{html_text(empty)}</p>'
    head = "".join(f"<th>{html_text(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{html_text(row.get(header, ''))}</td>" for header in headers) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def render_dashboard_html(data: dict | None = None) -> str:
    data = data or build_dashboard_data()
    summary = data["summary"]
    metrics = [
        "project_count",
        "active_project_count",
        "task_count",
        "open_tasks",
        "needs_evidence_tasks",
        "dispatch_count",
        "returned_dispatches",
        "evidence_gap_count",
        "pilot_count",
        "collection_count",
        "execution_count",
        "latest_task_id",
        "latest_dispatch_id",
        "latest_pilot_id",
    ]
    html_metrics = "".join(card(name, summary.get(name, "")) for name in metrics)
    suggestions = "".join(f"<li>{html_text(item)}</li>" for item in data["suggestions"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Atlas Workbench Dashboard</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde5;
      --text: #20242c;
      --muted: #667085;
      --accent: #0f766e;
      --accent-2: #365f91;
      --warn: #9a3412;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", Arial, sans-serif;
    }}
    header {{
      padding: 20px 28px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ margin: 22px 0 10px; font-size: 17px; letter-spacing: 0; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 10px 18px; color: var(--muted); }}
    main {{ padding: 18px 28px 30px; max-width: 1500px; margin: 0 auto; }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      min-height: 70px;
    }}
    .metric span {{ color: var(--muted); display: block; font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 20px; overflow-wrap: anywhere; }}
    section {{
      margin-top: 18px;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow-x: auto;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
    td {{ overflow-wrap: anywhere; }}
    ul {{ margin: 8px 0 0; padding-left: 20px; }}
    .empty {{ color: var(--muted); margin: 0; }}
    .notice {{ color: var(--warn); }}
    footer {{ padding: 16px 28px 26px; color: var(--muted); text-align: center; }}
  </style>
</head>
<body>
  <header>
    <h1>Atlas Workbench Dashboard</h1>
    <div class="meta">
      <span>mode: {html_text(summary["mode"])}</span>
      <span>bind: {html_text(summary["bind"])}</span>
      <span>external_access: {html_text(str(summary["external_access"]).lower())}</span>
      <span>auto_execute_enabled: {html_text(str(summary["auto_execute_enabled"]).lower())}</span>
      <span>generated_at: {html_text(summary["generated_at"])}</span>
    </div>
  </header>
  <main>
    <div class="metrics">{html_metrics}</div>
    <section><h2>Projects</h2>{render_table(["project_id", "status", "priority", "active_tasks", "needs_evidence", "updated_at"], data["projects"])}</section>
    <section><h2>Tasks</h2>{render_table(["task_id", "project_id", "status", "title", "updated_at", "evidence_gap_risk"], data["tasks"])}</section>
    <section><h2>Dispatches</h2>{render_table(["dispatch_id", "task_id", "project_id", "target_executor", "status", "updated_at"], data["dispatches"])}</section>
    <section><h2>Evidence Gaps</h2>{render_table(["task_id", "project_id", "status", "reason_summary"], data["evidence_gaps"])}</section>
    <section><h2>Pilots</h2>{render_table(["pilot_id", "project_id", "status", "task_count", "dispatch_count", "estimated_time_saved", "main_friction"], data["pilots"])}</section>
    <section><h2>Collections</h2>{render_table(["collection_id", "task_id", "profile", "created_at", "verified"], data["collections"])}</section>
    <section><h2>Executions</h2>{render_table(["exec_id", "dispatch_id", "task_id", "target_executor", "status"], data["executions"])}</section>
    <section><h2>Context Packs</h2>{render_table(["context_id", "source_task_id", "source_project_id", "created_at"], data["context_packs"])}</section>
    <section><h2>Learning / Playbook</h2>{render_table(["learning_records", "application_records", "playbook_records"], [data["learning"]])}</section>
    <section><h2>Suggested Next Actions</h2><ul>{suggestions}</ul></section>
  </main>
  <footer>Read-only local dashboard. No commands are executed.</footer>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        data = build_dashboard_data()
        if parsed.path == "/":
            self.send_text(200, render_dashboard_html(data), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/summary":
            self.send_json(200, data["summary"])
            return
        if parsed.path == "/api/projects":
            self.send_json(200, data["projects"])
            return
        if parsed.path == "/api/tasks":
            self.send_json(200, data["tasks"])
            return
        if parsed.path == "/api/dispatches":
            self.send_json(200, data["dispatches"])
            return
        self.send_text(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def send_json(self, status: int, body: object) -> None:
        payload = json.dumps(body, ensure_ascii=False, indent=2)
        self.send_text(status, payload, "application/json; charset=utf-8")

    def send_text(self, status: int, body: str, content_type: str) -> None:
        payload = sanitize_text(body).encode("utf-8") if content_type.startswith("text/plain") else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    if host != DEFAULT_HOST:
        raise ValueError("dashboard must bind to 127.0.0.1")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Atlas Workbench Dashboard: http://{host}:{port}/")
    print("mode: read_only_dashboard")
    print("external_access: false")
    print("auto_execute_enabled: false")
    server.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local read-only Atlas Workbench dashboard.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
