from __future__ import annotations

import argparse
import html
import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
WORKBENCH_DIR = ROOT / "workbench"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PREVIEW = 220
RECENT_LIMIT = 20
TEXTAREA_LIMIT = 12_000
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,160}$")


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


def sanitize_block(value: object, limit: int | None = None) -> str:
    text = str(value or "")
    for pattern in sensitive_patterns():
        text = pattern.sub("[REDACTED]", text)
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 24)].rstrip() + "\n...[truncated]"
    return text


def sanitize_text(value: object, limit: int | None = None) -> str:
    text = sanitize_block(value)
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 16)].rstrip() + " ...[truncated]"
    return text


def html_text(value: object) -> str:
    return html.escape(sanitize_text(value), quote=True)


def html_block(value: object) -> str:
    return html.escape(sanitize_block(value), quote=True)


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


def section_block(text: str, heading: str, limit: int = 4000) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^## .+$", text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    return sanitize_block(text[match.end():end].strip(), limit)


def markdown_files(folder: str, prefix: str) -> list[Path]:
    directory = WORKBENCH_DIR / folder
    if not directory.exists():
        return []
    return sorted(directory.glob(f"{prefix}*.md"), key=lambda path: path.stat().st_mtime, reverse=True)


def record_from_file(path: Path) -> tuple[str, str, dict[str, str]]:
    text = safe_read(path)
    return text, title_from_markdown(path.stem, text), parse_metadata(text)


def safe_record_id(value: str) -> str:
    clean = sanitize_text(unquote(str(value or "")), 180)
    if not SAFE_ID_RE.fullmatch(clean):
        return ""
    return clean


def read_record(folder: str, record_id: str) -> dict | None:
    clean_id = safe_record_id(record_id)
    if not clean_id:
        return None
    path = WORKBENCH_DIR / folder / f"{clean_id}.md"
    if not path.exists() or not path.is_file():
        return None
    text, title, meta = record_from_file(path)
    return {
        "id": clean_id,
        "path": path,
        "text": text,
        "title": title,
        "meta": meta,
    }


def read_project_record(project_id: str) -> dict | None:
    clean_id = safe_record_id(project_id)
    if not clean_id:
        return None
    direct = read_record("projects", clean_id)
    if direct:
        return direct
    for path in markdown_files("projects", ""):
        text, title, meta = record_from_file(path)
        if sanitize_text(meta.get("project_id") or path.stem, 180) == clean_id:
            return {
                "id": clean_id,
                "path": path,
                "text": text,
                "title": title,
                "meta": meta,
            }
    return None


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
        text, _title, meta = record_from_file(path)
        task_id = meta.get("task_id", "")
        project_id = meta.get("project_id", "")
        if not project_id and task_id:
            task_record = read_record("tasks", task_id)
            if task_record:
                project_id = task_record["meta"].get("project_id", "")
        items.append(
            {
                "collection_id": path.stem,
                "task_id": task_id,
                "project_id": project_id or "unassigned",
                "profile": meta.get("profile", ""),
                "created_at": meta.get("created_at", ""),
                "verified": meta.get("verified", "false") or "false",
                "evidence_summary": sanitize_text(section(text, "Evidence Summary") or section(text, "Observed Evidence") or section(text, "Collection Summary"), MAX_PREVIEW),
                "standard_return_report": sanitize_text(section(text, "Standard Return Report"), MAX_PREVIEW),
            }
        )
    return items


def execution_next_command(record: dict) -> str:
    status = sanitize_text(record.get("status", "")).lower()
    exec_id = sanitize_text(record.get("exec_id", ""))
    dispatch_id = sanitize_text(record.get("dispatch_id", ""))
    if status == "prepared":
        return f"/exec start {dispatch_id}" if dispatch_id else f"/exec package {exec_id}"
    if status == "started":
        return f"/exec show {exec_id}"
    if status == "needs_manual_start":
        return f"/exec package {exec_id}"
    if status == "returned":
        return f"/dispatch qa {dispatch_id}" if dispatch_id else f"/exec show {exec_id}"
    if status == "failed":
        return f"/exec show {exec_id}"
    if status in {"opened", "copied"}:
        return f"/exec receive {exec_id}"
    return f"/exec show {exec_id}" if exec_id else ""


def build_executions() -> list[dict]:
    items = []
    for path in markdown_files("executions", "EXEC-"):
        _text, _title, meta = record_from_file(path)
        item = {
            "exec_id": path.stem,
            "dispatch_id": meta.get("dispatch_id", ""),
            "task_id": meta.get("task_id", ""),
            "project_id": meta.get("project_id", "unassigned") or "unassigned",
            "target_executor": meta.get("target_executor", ""),
            "status": meta.get("status", "unknown"),
            "updated_at": meta.get("updated_at", ""),
            "auto_run_mode": meta.get("auto_run_mode", "") or "manual",
        }
        item["next_command"] = execution_next_command(item)
        items.append(item)
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


def build_suggestions(
    summary: dict,
    projects: list[dict],
    tasks: list[dict],
    dispatches: list[dict],
    pilots: list[dict],
    executions: list[dict],
) -> list[str]:
    suggestions: list[str] = []
    if summary["needs_evidence_tasks"]:
        suggestions.append("Needs-evidence tasks are present; prioritize adding verifiable evidence.")
    if any(item.get("status") == "ready" for item in dispatches):
        suggestions.append("Ready dispatches exist; prepare/package the handoff and record manual send state.")
    if any(item.get("status") == "returned" for item in dispatches):
        suggestions.append("Returned dispatches exist; run QA and then Atlas review.")
    if any(item.get("status") == "prepared" for item in executions):
        suggestions.append("Prepared execution sessions exist; use /exec start for read-only dispatches or /exec package for manual copy.")
    if any(item.get("status") == "needs_manual_start" for item in executions):
        suggestions.append("Some executions need manual start; copy the package and paste the return report back.")
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
        "suggestions": build_suggestions(summary, projects, tasks, dispatches, pilots, executions),
    }


def latest_by_key(items: list[dict], key: str, value: str) -> dict | None:
    for item in items:
        if item.get(key) == value:
            return item
    return None


def list_by_key(items: list[dict], key: str, value: str, limit: int = RECENT_LIMIT) -> list[dict]:
    return [item for item in items if item.get(key) == value][:limit]


def latest_context_for_task(task_id: str) -> dict | None:
    return latest_by_key(build_context_packs(), "source_task_id", task_id)


def latest_evidence_for_task(task_id: str) -> str:
    record = read_record("evidence", task_id)
    if not record:
        return "none recorded"
    text = record["text"]
    return sanitize_text(
        section(text, "Evidence Records")
        or section(text, "Evidence")
        or section(text, "Observed Evidence")
        or text,
        MAX_PREVIEW,
    )


def latest_project_pilot_id(project_id: str) -> str:
    for pilot in build_pilots():
        if pilot.get("project_id") == project_id and pilot.get("status") == "active":
            return pilot.get("pilot_id", "")
    for pilot in build_pilots():
        if pilot.get("project_id") == project_id:
            return pilot.get("pilot_id", "")
    return "<pilot_id>"


def build_task_detail_data(task_id: str) -> dict | None:
    clean_id = safe_record_id(task_id)
    record = read_record("tasks", clean_id)
    if not record:
        return None
    text = record["text"]
    meta = record["meta"]
    project_id = meta.get("project_id", "") or "unassigned"
    dispatches = list_by_key(build_dispatches(), "task_id", clean_id)
    collections = list_by_key(build_collections(), "task_id", clean_id)
    latest_dispatch = dispatches[0] if dispatches else {}
    latest_collection = collections[0] if collections else {}
    pilot_id = latest_project_pilot_id(project_id)
    live_value = meta.get("live_skipped", "")
    if not live_value:
        live_value = str("live_skipped" in text.lower()).lower()
    commands = [
        ("Copy context handoff command", f"/context handoff {clean_id} codex"),
        ("Copy task next command", f"/context pack task {clean_id}"),
        ("Copy task next command", f"/dispatch create {clean_id} codex --with-context"),
        ("Copy task next command", f"/task qa {clean_id}"),
        ("Copy task next command", f"/task review {clean_id}"),
        ("Copy evidence mark command template", f"/evidence mark {clean_id} <evidence_id> verified <note>"),
        ("Copy task next command", f"/pilot add-task {pilot_id} {clean_id}"),
    ]
    return {
        "task_id": clean_id,
        "title": record["title"],
        "project_id": project_id,
        "status": meta.get("status", "unknown"),
        "updated_at": meta.get("updated_at", ""),
        "evidence_gap_risk": meta.get("evidence_gap_risk", "") or str(
            meta.get("status", "") == "needs_evidence" or bool(section(text, "Evidence Gaps"))
        ).lower(),
        "live_skipped": live_value,
        "latest_dispatch": latest_dispatch.get("dispatch_id", "none"),
        "latest_evidence": latest_evidence_for_task(clean_id),
        "latest_collection": latest_collection.get("collection_id", "none"),
        "latest_review_summary": sanitize_text(section(text, "Atlas Review") or section(text, "Review"), MAX_PREVIEW) or "none recorded",
        "user_decision": sanitize_text(section(text, "User Decision"), MAX_PREVIEW) or "none recorded",
        "commands": commands,
    }


def build_project_detail_data(project_id: str) -> dict | None:
    clean_id = safe_record_id(project_id)
    record = read_project_record(clean_id)
    if not record:
        return None
    tasks = list_by_key(build_tasks(), "project_id", clean_id)
    dispatches = list_by_key(build_dispatches(), "project_id", clean_id)
    pilots = list_by_key(build_pilots(), "project_id", clean_id)
    evidence_gaps = [task for task in tasks if task.get("status") == "needs_evidence" or task.get("evidence_gap_risk")]
    learning = build_learning_playbook_state()
    commands = [
        ("Copy task next command", f"/project brief {clean_id}"),
        ("Copy task next command", f"/context pack project {clean_id}"),
        ("Copy task next command", f"/project dashboard"),
        ("Copy task next command", f"/pilot start {clean_id} <title>"),
        ("Copy task next command", f"/task new <title>"),
    ]
    meta = record["meta"]
    return {
        "project_id": clean_id,
        "title": record["title"],
        "status": meta.get("status", "unknown"),
        "priority": meta.get("priority", "unknown"),
        "updated_at": meta.get("updated_at", ""),
        "active_tasks": [task for task in tasks if task.get("status") not in {"passed", "cancelled", "archived"}][:10],
        "needs_evidence_tasks": evidence_gaps[:10],
        "recent_dispatches": dispatches[:10],
        "recent_pilots": pilots[:10],
        "evidence_gaps": evidence_gaps[:10],
        "retro_learn_playbook_summary": (
            f"learning_records={learning['learning_records']}; "
            f"application_records={learning['application_records']}; "
            f"playbook_records={learning['playbook_records']}"
        ),
        "commands": commands,
    }


def build_dispatch_detail_data(dispatch_id: str) -> dict | None:
    clean_id = safe_record_id(dispatch_id)
    record = read_record("dispatches", clean_id)
    if not record:
        return None
    text = record["text"]
    meta = record["meta"]
    task_id = meta.get("task_id", "")
    commands = [
        ("Copy dispatch package command", f"/dispatch package {clean_id}"),
        ("Copy task next command", f"/exec start {clean_id}"),
        ("Copy exec prepare command", f"/exec prepare {clean_id}"),
        ("Copy task next command", f"/dispatch qa {clean_id}"),
        ("Copy task next command", f"/dispatch link-review {clean_id}"),
    ]
    if task_id:
        commands.append(("Copy context handoff command", f"/context handoff {task_id} codex"))
    return {
        "dispatch_id": clean_id,
        "title": record["title"],
        "task_id": task_id,
        "project_id": meta.get("project_id", "") or "unassigned",
        "target_executor": meta.get("target_executor", ""),
        "status": meta.get("status", "unknown"),
        "updated_at": meta.get("updated_at", ""),
        "context_id": meta.get("context_id", "") or "none",
        "return_report_summary": sanitize_text(section(text, "Return Report"), MAX_PREVIEW) or "none recorded",
        "qa_result_summary": sanitize_text(section(text, "QA Result"), MAX_PREVIEW) or "none recorded",
        "commands": commands,
    }


def build_pilot_detail_data(pilot_id: str) -> dict | None:
    clean_id = safe_record_id(pilot_id)
    record = read_record("pilots", clean_id)
    if not record:
        return None
    text = record["text"]
    meta = record["meta"]
    project_id = meta.get("project_id", "")
    task_ids = set(re.findall(r"OHB-\d{8}-\d{6}(?:-\d{2})?", text))
    dispatch_ids = set(re.findall(r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?", text))
    tasks = [task for task in build_tasks() if task.get("task_id") in task_ids]
    dispatches = [dispatch for dispatch in build_dispatches() if dispatch.get("dispatch_id") in dispatch_ids]
    qa_pass_count = sum(
        1
        for dispatch in dispatches
        if "pass" in sanitize_text(section(read_record("dispatches", dispatch.get("dispatch_id", ""))["text"], "QA Result") if read_record("dispatches", dispatch.get("dispatch_id", "")) else "").lower()
    )
    needs_evidence_count = sum(1 for task in tasks if task.get("status") == "needs_evidence" or task.get("evidence_gap_risk"))
    commands = [
        ("Copy pilot metrics command", f"/pilot metrics {clean_id}"),
        ("Copy task next command", f"/pilot complete {clean_id} <summary>"),
        ("Copy task next command", f"/pilot add-task {clean_id} <task_id>"),
        ("Copy task next command", f"/pilot add-dispatch {clean_id} <dispatch_id>"),
    ]
    metrics = section(text, "Metrics")
    return {
        "pilot_id": clean_id,
        "title": record["title"],
        "project_id": project_id or "unassigned",
        "status": meta.get("status", "unknown"),
        "task_count": len(task_ids),
        "dispatch_count": len(dispatch_ids),
        "returned_count": sum(1 for dispatch in dispatches if dispatch.get("status") in {"returned", "qa_ready", "reviewed", "closed"}),
        "qa_pass_count": qa_pass_count,
        "needs_evidence_count": needs_evidence_count,
        "estimated_time_saved": sanitize_text(extract_metric(metrics, "estimated_time_saved") or section(text, "Time Saved Estimate"), 120) or "none recorded",
        "friction_log_summary": sanitize_text(extract_metric(metrics, "main_friction") or section(text, "Friction Log"), MAX_PREVIEW) or "none recorded",
        "commands": commands,
    }


def build_collection_detail_data(collection_id: str) -> dict | None:
    clean_id = safe_record_id(collection_id)
    record = read_record("collections", clean_id)
    if not record:
        return None
    text = record["text"]
    meta = record["meta"]
    task_id = meta.get("task_id", "")
    project_id = meta.get("project_id", "")
    if not project_id and task_id:
        task_record = read_record("tasks", task_id)
        if task_record:
            project_id = task_record["meta"].get("project_id", "")
    commands = [
        ("Copy task next command", f"/collect show {clean_id}"),
        ("Copy task next command", f"/collect report {clean_id}"),
    ]
    if task_id:
        commands.append(("Copy evidence mark command template", f"/evidence mark {task_id} <evidence_id> verified <note>"))
    return {
        "collection_id": clean_id,
        "title": record["title"],
        "task_id": task_id or "none",
        "project_id": project_id or "unassigned",
        "profile": meta.get("profile", ""),
        "created_at": meta.get("created_at", ""),
        "evidence_summary": sanitize_text(section(text, "Evidence Summary") or section(text, "Observed Evidence") or section(text, "Collection Summary"), MAX_PREVIEW) or "none recorded",
        "standard_return_report_summary": sanitize_text(section(text, "Standard Return Report"), MAX_PREVIEW) or "none recorded",
        "commands": commands,
    }


def optional_block(text: str) -> str:
    return sanitize_block(text.strip() if text else "- not available", 3000)


def dispatch_package_text(dispatch_id: str) -> str | None:
    data = build_dispatch_detail_data(dispatch_id)
    if not data:
        return None
    dispatch_record = read_record("dispatches", data["dispatch_id"])
    task_record = read_record("tasks", data["task_id"]) if data.get("task_id") else None
    dispatch_text = dispatch_record["text"] if dispatch_record else ""
    task_text = task_record["text"] if task_record else ""
    target = (data.get("target_executor") or "unknown").lower()
    display_target = "Codex" if target == "codex" else ("Kiro" if target == "kiro" else sanitize_text(target, 80))
    context_summary = section_block(dispatch_text, "Context Pack Summary", 1800) or "- not available"
    playbook_advisory = section_block(dispatch_text, "Playbook Advisory", 1800) or "- not available"
    return sanitize_block(f"""# Manual Dispatch Package for {display_target}

dispatch_id: {data['dispatch_id']}
task_id: {data.get('task_id') or 'none'}
target_executor: {target}
manual_copy_required: true
external_execution_enabled: false
runtime_injection_enabled: false

Manual copy only. Not sent automatically.
Atlas/Bridge has not sent this package and will not call {display_target}.

## Task Title
{data.get('title') or 'untitled'}

## Goal
{optional_block(section_block(task_text, 'Goal'))}

## Scope
{optional_block(section_block(task_text, 'Scope'))}

## Execution Boundary
{optional_block(section_block(task_text, 'Execution Boundary'))}

## Forbidden
- Do not read, print, log, commit, or leak .env, tokens, cookies, passwords, API keys, or secrets.
- Do not modify unauthorized projects.
- Do not change Octo Docker or Hermes main code.
- Do not claim completion without evidence.

## Suggested Checks
- Confirm working directory and current repo state before changes.
- Keep changes minimal and inside the authorized scope.
- Run relevant compile, smoke, or test commands.
- Collect files, commands, test results, logs/screenshots, unverified items, and unresolved risks.

## Acceptance Criteria
{optional_block(section_block(task_text, 'Acceptance Criteria'))}

## Context Pack Summary
{context_summary}

## Playbook Advisory
{playbook_advisory}

## Return Report Format
Task id: {data.get('task_id') or 'none'}
Dispatch id: {data['dispatch_id']}

Execution summary:
-

Modified files:
-

Commands:
-

Test results:
-

Key logs or screenshots:
-

Unverified:
-

Unresolved risks:
-

Rollback notes:
-

## Sensitive Information Handling
- Redact tokens, cookies, auth headers, passwords, API keys, and secrets before returning.
- Do not paste .env content.

## User Final Acceptance
- The user will paste the report back with /dispatch receive {data['dispatch_id']}.
- Atlas will then run /dispatch qa, /task review, and wait for the user's final /task decide.
""", TEXTAREA_LIMIT)


def context_handoff_text(task_id: str, platform: str) -> str | None:
    target = platform.lower().strip()
    if target not in {"codex", "kiro"}:
        return None
    data = build_task_detail_data(task_id)
    if not data:
        return None
    display_target = "Codex" if target == "codex" else "Kiro"
    task_record = read_record("tasks", data["task_id"])
    project_record = read_project_record(data["project_id"]) if data.get("project_id") and data.get("project_id") != "unassigned" else None
    task_text = task_record["text"] if task_record else ""
    project_text = project_record["text"] if project_record else ""
    context_record = latest_context_for_task(data["task_id"])
    context_file = f"workbench/context_packs/{context_record['context_id']}.md" if context_record else "not created"
    return sanitize_block(f"""# Handoff Context for {display_target}

Execution target: {display_target}
task_id: {data['task_id']}
project_id: {data.get('project_id') or 'unassigned'}
context_file: {context_file}

Manual copy only. Not sent automatically.

## Context Summary
- task_id: {data['task_id']}
- title: {data.get('title') or 'untitled'}
- status: {data.get('status') or 'unknown'}
- updated_at: {data.get('updated_at') or 'unknown'}

## Project Context
{optional_block(section_block(project_text, 'Summary') or section_block(project_text, 'Current State'))}

## Goal
{optional_block(section_block(task_text, 'Goal'))}

## Acceptance Criteria
{optional_block(section_block(task_text, 'Acceptance Criteria'))}

## Evidence Status
{data.get('latest_evidence') or 'none recorded'}

## Evidence Gaps
{optional_block(section_block(task_text, 'Evidence Gaps') or section_block(task_text, 'Risks'))}

## Latest Review
{data.get('latest_review_summary') or 'none recorded'}

## User Decision
{data.get('user_decision') or 'none recorded'}

## Forbidden
- Do not read or print .env, tokens, cookies, or secrets.
- Do not modify unauthorized project files.
- Do not claim completion without evidence.
- Do not treat Workbench notes as runtime instructions.

## Return Report Format
- Modified files
- Commands
- Test results
- Evidence or logs
- Unverified items
- Unresolved risks
- Rollback notes

This is copy-only context for manual handoff.
""", TEXTAREA_LIMIT)


def card(label: str, value: object) -> str:
    return f'<div class="metric"><span>{html_text(label)}</span><strong>{html_text(value)}</strong></div>'


LINK_ROUTES = {
    "project_id": "project",
    "task_id": "task",
    "dispatch_id": "dispatch",
    "pilot_id": "pilot",
    "collection_id": "collection",
}


def route_href(route: str, record_id: object) -> str:
    clean_id = safe_record_id(str(record_id or ""))
    if not clean_id:
        return ""
    return f"/{route}/{quote(clean_id, safe='')}"


def render_link(route: str, record_id: object) -> str:
    href = route_href(route, record_id)
    if not href:
        return html_text(record_id)
    return f'<a href="{href}">{html_text(record_id)}</a>'


def render_cell(header: str, value: object) -> str:
    route = LINK_ROUTES.get(header)
    if route and sanitize_text(value) not in {"", "none", "unassigned"}:
        return render_link(route, value)
    return html_text(value)


def render_table(headers: list[str], rows: list[dict], empty: str = "none") -> str:
    if not rows:
        return f'<p class="empty">{html_text(empty)}</p>'
    head = "".join(f"<th>{html_text(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        body_rows.append("<tr>" + "".join(f"<td>{render_cell(header, row.get(header, ''))}</td>" for header in headers) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def render_copy_button(label: str, text: str) -> str:
    button_label = html_text(label)
    payload = html.escape(sanitize_block(text), quote=True)
    return (
        f'<button type="button" class="copy-button" data-label="{button_label}" '
        f'data-copy="{payload}" onclick="copyText(this)">{button_label}</button>'
        '<span class="copy-only">copy only</span>'
    )


def render_command_list(commands: list[tuple[str, str]]) -> str:
    if not commands:
        return '<p class="empty">none</p>'
    items = []
    for label, command in commands:
        items.append(
            "<li>"
            f"<code>{html_text(command)}</code> "
            f"{render_copy_button(label, command)}"
            "</li>"
        )
    return f'<ul class="commands">{"".join(items)}</ul>'


def render_fields(fields: list[tuple[str, object]]) -> str:
    rows = []
    for label, value in fields:
        rows.append(f"<tr><th>{html_text(label)}</th><td>{html_text(value)}</td></tr>")
    return f'<table class="fields"><tbody>{"".join(rows)}</tbody></table>'


def detail_styles() -> str:
    return """
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d8dde5;
      --text: #20242c;
      --muted: #667085;
      --accent: #0f766e;
      --warn: #9a3412;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 "Segoe UI", Arial, sans-serif;
    }
    header {
      padding: 20px 28px 14px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    main { padding: 18px 28px 30px; max-width: 1280px; margin: 0 auto; }
    h1 { margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }
    h2 { margin: 22px 0 10px; font-size: 17px; letter-spacing: 0; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .meta { display: flex; flex-wrap: wrap; gap: 10px 18px; color: var(--muted); }
    section {
      margin-top: 18px;
      padding: 14px;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow-x: auto;
    }
    table { width: 100%; border-collapse: collapse; min-width: 640px; }
    th, td { padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 12px; font-weight: 600; }
    td { overflow-wrap: anywhere; }
    .fields th { width: 220px; }
    ul { margin: 8px 0 0; padding-left: 20px; }
    .commands li { margin: 8px 0; }
    code {
      background: #eef2f6;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 2px 5px;
      overflow-wrap: anywhere;
    }
    .copy-button {
      margin-left: 8px;
      border: 1px solid var(--accent);
      background: #f0fdfa;
      color: #0f4f48;
      border-radius: 6px;
      padding: 4px 8px;
      cursor: pointer;
      font: inherit;
    }
    .copy-only, .empty, .manual { color: var(--muted); margin-left: 8px; }
    .manual { margin-left: 0; }
    textarea {
      width: 100%;
      min-height: 520px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      color: var(--text);
      background: #fbfcfe;
      font: 13px/1.45 Consolas, "Courier New", monospace;
      white-space: pre;
    }
    footer { padding: 16px 28px 26px; color: var(--muted); text-align: center; }
    """


def copy_script() -> str:
    return """
  <script>
    function setCopied(button) {
      const label = button.getAttribute("data-label") || "Copy";
      button.textContent = "copied";
      window.setTimeout(function () { button.textContent = label; }, 1200);
    }
    function copyText(button) {
      const text = button.getAttribute("data-copy") || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { setCopied(button); });
      }
    }
    function copyArea(button, id) {
      const area = document.getElementById(id);
      if (!area) { return; }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(area.value).then(function () { setCopied(button); });
      }
    }
  </script>
    """


def render_page(title: str, body: str, subtitle: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_text(title)}</title>
  <style>{detail_styles()}</style>
  {copy_script()}
</head>
<body>
  <header>
    <h1>{html_text(title)}</h1>
    <div class="meta">
      <span>mode: read_only_dashboard</span>
      <span>bind: 127.0.0.1</span>
      <span>copy helper: copy only / no execution</span>
      <span><a href="/">dashboard home</a></span>
      {f'<span>{html_text(subtitle)}</span>' if subtitle else ''}
    </div>
  </header>
  <main>{body}</main>
  <footer>Read-only local dashboard. Copy helpers do not execute commands.</footer>
</body>
</html>"""


def render_detail_html(kind: str, data: dict | None) -> str | None:
    if not data:
        return None
    commands = data.get("commands", [])
    if kind == "task":
        copy_links = [
            ("Copy context handoff command", f"/copy/context/{data['task_id']}/codex"),
            ("Copy context handoff command", f"/copy/context/{data['task_id']}/kiro"),
        ]
        fields = [
            ("task_id", data["task_id"]),
            ("title", data["title"]),
            ("project_id", data["project_id"]),
            ("status", data["status"]),
            ("updated_at", data["updated_at"]),
            ("evidence_gap_risk", data["evidence_gap_risk"]),
            ("live_skipped", data["live_skipped"]),
            ("latest dispatch", data["latest_dispatch"]),
            ("latest evidence", data["latest_evidence"]),
            ("latest review summary", data["latest_review_summary"]),
            ("user decision", data["user_decision"]),
        ]
        body = (
            f"<section><h2>Task Summary</h2>{render_fields(fields)}</section>"
            f"<section><h2>Recommended Next Commands</h2>{render_command_list(commands)}</section>"
            f"<section><h2>Copy Packages</h2>{render_command_list(copy_links)}</section>"
        )
        return render_page(f"Task {data['task_id']}", body, data.get("status", ""))
    if kind == "project":
        fields = [
            ("project_id", data["project_id"]),
            ("title", data["title"]),
            ("status", data["status"]),
            ("priority", data["priority"]),
            ("updated_at", data["updated_at"]),
            ("retro / learn / playbook summary", data["retro_learn_playbook_summary"]),
        ]
        body = (
            f"<section><h2>Project Summary</h2>{render_fields(fields)}</section>"
            f"<section><h2>Active Tasks</h2>{render_table(['task_id', 'status', 'title', 'updated_at', 'evidence_gap_risk'], data['active_tasks'])}</section>"
            f"<section><h2>Needs Evidence Tasks</h2>{render_table(['task_id', 'status', 'title', 'updated_at'], data['needs_evidence_tasks'])}</section>"
            f"<section><h2>Recent Dispatches</h2>{render_table(['dispatch_id', 'task_id', 'target_executor', 'status', 'updated_at'], data['recent_dispatches'])}</section>"
            f"<section><h2>Recent Pilots</h2>{render_table(['pilot_id', 'status', 'task_count', 'dispatch_count', 'estimated_time_saved'], data['recent_pilots'])}</section>"
            f"<section><h2>Evidence Gaps</h2>{render_table(['task_id', 'status', 'reason_summary'], data['evidence_gaps'])}</section>"
            f"<section><h2>Recommended Next Commands</h2>{render_command_list(commands)}</section>"
        )
        return render_page(f"Project {data['project_id']}", body, data.get("status", ""))
    if kind == "dispatch":
        fields = [
            ("dispatch_id", data["dispatch_id"]),
            ("task_id", data["task_id"]),
            ("project_id", data["project_id"]),
            ("target_executor", data["target_executor"]),
            ("status", data["status"]),
            ("updated_at", data["updated_at"]),
            ("context_id", data["context_id"]),
            ("return report summary", data["return_report_summary"]),
            ("QA result summary", data["qa_result_summary"]),
        ]
        copy_links = [
            ("Copy dispatch package command", f"/copy/dispatch/{data['dispatch_id']}"),
        ]
        body = (
            f"<section><h2>Dispatch Summary</h2>{render_fields(fields)}</section>"
            f"<section><h2>Recommended Next Commands</h2>{render_command_list(commands)}</section>"
            f"<section><h2>Copy Packages</h2>{render_command_list(copy_links)}</section>"
        )
        return render_page(f"Dispatch {data['dispatch_id']}", body, data.get("status", ""))
    if kind == "pilot":
        fields = [
            ("pilot_id", data["pilot_id"]),
            ("project_id", data["project_id"]),
            ("status", data["status"]),
            ("task_count", data["task_count"]),
            ("dispatch_count", data["dispatch_count"]),
            ("returned_count", data["returned_count"]),
            ("qa_pass_count", data["qa_pass_count"]),
            ("needs_evidence_count", data["needs_evidence_count"]),
            ("estimated_time_saved", data["estimated_time_saved"]),
            ("friction log summary", data["friction_log_summary"]),
        ]
        body = (
            f"<section><h2>Pilot Summary</h2>{render_fields(fields)}</section>"
            f"<section><h2>Recommended Next Commands</h2>{render_command_list(commands)}</section>"
        )
        return render_page(f"Pilot {data['pilot_id']}", body, data.get("status", ""))
    if kind == "collection":
        fields = [
            ("collection_id", data["collection_id"]),
            ("task_id", data["task_id"]),
            ("project_id", data["project_id"]),
            ("profile", data["profile"]),
            ("created_at", data["created_at"]),
            ("evidence summary", data["evidence_summary"]),
            ("standard return report summary", data["standard_return_report_summary"]),
        ]
        body = (
            f"<section><h2>Collection Summary</h2>{render_fields(fields)}</section>"
            f"<section><h2>Recommended Next Commands</h2>{render_command_list(commands)}</section>"
        )
        return render_page(f"Collection {data['collection_id']}", body, data.get("profile", ""))
    return None


def render_copy_payload_page(title: str, payload: str | None) -> str | None:
    if payload is None:
        return None
    body = f"""
    <section>
      <p class="manual">Manual copy only. Not sent automatically.</p>
      <button type="button" class="copy-button" data-label="Copy payload" onclick="copyArea(this, 'copy-payload')">Copy payload</button><span class="copy-only">copy only</span>
      <textarea id="copy-payload" readonly>{html_block(payload)}</textarea>
    </section>
    """
    return render_page(title, body, "copy-only text area")


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
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
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
    <section><h2>Collections</h2>{render_table(["collection_id", "task_id", "project_id", "profile", "created_at", "verified"], data["collections"])}</section>
    <section><h2>Executions</h2>{render_table(["exec_id", "dispatch_id", "task_id", "target_executor", "status", "updated_at", "auto_run_mode", "next_command"], data["executions"])}</section>
    <section><h2>Context Packs</h2>{render_table(["context_id", "source_task_id", "source_project_id", "created_at"], data["context_packs"])}</section>
    <section><h2>Learning / Playbook</h2>{render_table(["learning_records", "application_records", "playbook_records"], [data["learning"]])}</section>
    <section><h2>Suggested Next Actions</h2><ul>{suggestions}</ul></section>
  </main>
  <footer>Read-only local dashboard. Copy helpers do not execute commands.</footer>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
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
        if len(parts) == 2 and parts[0] == "task":
            body = render_detail_html("task", build_task_detail_data(parts[1]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "task not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 2 and parts[0] == "project":
            body = render_detail_html("project", build_project_detail_data(parts[1]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "project not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 2 and parts[0] == "dispatch":
            body = render_detail_html("dispatch", build_dispatch_detail_data(parts[1]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "dispatch not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 2 and parts[0] == "pilot":
            body = render_detail_html("pilot", build_pilot_detail_data(parts[1]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "pilot not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 2 and parts[0] == "collection":
            body = render_detail_html("collection", build_collection_detail_data(parts[1]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "collection not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "copy" and parts[1] == "dispatch":
            body = render_copy_payload_page(f"Copy Dispatch {safe_record_id(parts[2])}", dispatch_package_text(parts[2]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "dispatch not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 4 and parts[0] == "copy" and parts[1] == "context":
            body = render_copy_payload_page(f"Copy Context {safe_record_id(parts[2])} {sanitize_text(parts[3], 40)}", context_handoff_text(parts[2], parts[3]))
            self.send_text(200, body, "text/html; charset=utf-8") if body else self.send_text(404, "context source not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "task":
            detail = build_task_detail_data(parts[2])
            self.send_json(200, detail) if detail else self.send_text(404, "task not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "project":
            detail = build_project_detail_data(parts[2])
            self.send_json(200, detail) if detail else self.send_text(404, "project not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "dispatch":
            detail = build_dispatch_detail_data(parts[2])
            self.send_json(200, detail) if detail else self.send_text(404, "dispatch not found", "text/plain; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "pilot":
            detail = build_pilot_detail_data(parts[2])
            self.send_json(200, detail) if detail else self.send_text(404, "pilot not found", "text/plain; charset=utf-8")
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
