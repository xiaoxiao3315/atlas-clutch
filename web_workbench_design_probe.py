from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


EXPECTED_WORKBENCH_DIRS = [
    "projects",
    "tasks",
    "evidence",
    "retros",
    "learning",
    "applications",
    "playbooks",
    "context_packs",
    "dispatches",
    "pilots",
    "collections",
    "executions",
    "designs",
]

SENSITIVE_PATTERNS = [
    re.compile(r"bf_[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"Authorization\s*:\s*Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"Cookie\s*:\s*\S+", re.IGNORECASE),
]


def sanitize(text: str) -> str:
    value = str(text or "")
    for pattern in SENSITIVE_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def count_files(directory: Path) -> int:
    if not directory.exists() or not directory.is_dir():
        return 0
    count = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name == ".env" or name.startswith(".env."):
            continue
        count += 1
    return count


def read_readme_headings(readme_path: Path) -> list[str]:
    if not readme_path.exists():
        return []
    headings: list[str] = []
    for line in readme_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#"):
            headings.append(sanitize(line.strip()))
    return headings[:80]


def log_probe(root: Path) -> dict:
    logs_dir = root / "logs"
    bridge_log = logs_dir / "bridge.log"
    return {
        "logs_dir_exists": logs_dir.exists(),
        "bridge_log_exists": bridge_log.exists(),
        "bridge_log_size_bytes": bridge_log.stat().st_size if bridge_log.exists() else 0,
        "log_content_read": False,
    }


def build_summary(root: Path) -> dict:
    workbench = root / "workbench"
    dirs = []
    for name in EXPECTED_WORKBENCH_DIRS:
        path = workbench / name
        dirs.append(
            {
                "name": name,
                "exists": path.exists() and path.is_dir(),
                "file_count": count_files(path),
            }
        )
    return {
        "root": str(root),
        "workbench_exists": workbench.exists(),
        "env_file_read": False,
        "user_task_executed": False,
        "octo_web_modified": False,
        "octo_server_modified": False,
        "docker_modified": False,
        "workbench_dirs": dirs,
        "readme_headings": read_readme_headings(root / "README.md"),
        "logs": log_probe(root),
    }


def render_text(summary: dict) -> str:
    lines = [
        "OHB-WEB-017A read-only design probe",
        f"root: {sanitize(summary['root'])}",
        f"workbench_exists: {str(summary['workbench_exists']).lower()}",
        f"env_file_read: {str(summary['env_file_read']).lower()}",
        f"user_task_executed: {str(summary['user_task_executed']).lower()}",
        f"octo_web_modified: {str(summary['octo_web_modified']).lower()}",
        f"octo_server_modified: {str(summary['octo_server_modified']).lower()}",
        f"docker_modified: {str(summary['docker_modified']).lower()}",
        "",
        "Workbench directories:",
    ]
    for item in summary["workbench_dirs"]:
        lines.append(
            f"- {item['name']}: exists={str(item['exists']).lower()} files={item['file_count']}"
        )
    lines.extend(
        [
            "",
            "README headings:",
            *(f"- {heading}" for heading in summary["readme_headings"]),
            "",
            "Logs:",
            f"- logs_dir_exists: {str(summary['logs']['logs_dir_exists']).lower()}",
            f"- bridge_log_exists: {str(summary['logs']['bridge_log_exists']).lower()}",
            f"- bridge_log_size_bytes: {summary['logs']['bridge_log_size_bytes']}",
            f"- log_content_read: {str(summary['logs']['log_content_read']).lower()}",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Atlas Workbench design probe.")
    parser.add_argument("--root", default=Path(__file__).resolve().parent, type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON summary")
    args = parser.parse_args()
    root = args.root.resolve()
    summary = build_summary(root)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
