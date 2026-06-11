"""CI safety gate for the public Atlas Clutch repository.

Scans TRACKED files only (sourced from `git ls-files`); it never opens
untracked files, so a local .env is never read. Output prints file paths
and pattern labels only - never matched values - so it is safe for public
CI logs.

Exit code 0: clean. Exit code 1: suspicious tracked path or content.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SUSPICIOUS_DIR_SEGMENTS = {
    "logs",
    "runtime",
    "workbench",
    ".codegraph",
    ".understand-anything",
    "__pycache__",
}
SUSPICIOUS_SUFFIXES = {".env", ".local", ".pyc", ".zip", ".patch", ".token", ".key"}

CONTENT_PATTERNS = [
    ("private_key_header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("bearer_token", re.compile(r"(?i)bearer[ \t]+[A-Za-z0-9_\-.=]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{24,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("cookie_header", re.compile(r"(?i)cookie[ \t]*:[ \t]*\S+=\S+")),
    ("authorization_header", re.compile(r"(?i)authorization[ \t]*:[ \t]*\S{12,}")),
    ("password_assignment", re.compile(r"(?i)password[ \t]*[:=][ \t]*[\"'][^\"']{8,}[\"']")),
    ("secret_assignment", re.compile(r"(?i)\bsecret[ \t]*[:=][ \t]*[\"'][^\"']{8,}[\"']")),
    ("api_key_assignment", re.compile(r"(?i)api[_-]?key[ \t]*[:=][ \t]*[\"'][^\"']{8,}[\"']")),
]

# Known dummy fixtures used by the sanitizer smoke tests (fake values by
# construction, verified 2026-06-11; the secret_assignment hits are all the
# 'secret = "bf_<smoke>_secret_123456"' redaction fixtures). Pairs are pinned
# per (path, label): a NEW match in these files under any other label still
# fails the gate.
ALLOWLIST = {
    ("smoke_auto_evidence.py", "bearer_token"),
    ("smoke_web_design.py", "bearer_token"),
    ("smoke_web_design.py", "cookie_header"),
    ("smoke_dispatch.py", "cookie_header"),
    ("smoke_exec.py", "cookie_header"),
    ("smoke_pilot.py", "cookie_header"),
    ("smoke_task_loop.py", "cookie_header"),
    ("smoke_apply.py", "secret_assignment"),
    ("smoke_context.py", "secret_assignment"),
    ("smoke_dispatch.py", "secret_assignment"),
    ("smoke_evidence.py", "secret_assignment"),
    ("smoke_exec.py", "secret_assignment"),
    ("smoke_learn.py", "secret_assignment"),
    ("smoke_pilot.py", "secret_assignment"),
    ("smoke_project.py", "secret_assignment"),
    ("smoke_retro.py", "secret_assignment"),
    ("smoke_task_loop.py", "secret_assignment"),
}


def tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def path_problems(rel_path: str) -> list[str]:
    problems = []
    parts = rel_path.replace("\\", "/").split("/")
    basename = parts[-1]
    for segment in parts:
        if segment in SUSPICIOUS_DIR_SEGMENTS:
            problems.append(f"suspicious_path_segment:{segment}")
    if basename == ".env" or basename.startswith(".env."):
        problems.append("suspicious_path_basename:.env")
    suffix = Path(basename).suffix.lower()
    if suffix in SUSPICIOUS_SUFFIXES:
        problems.append(f"suspicious_path_suffix:{suffix}")
    return problems


def main() -> int:
    failures: list[str] = []
    skipped_binary: list[str] = []
    allowlisted = 0
    files = tracked_files()

    for rel_path in files:
        for problem in path_problems(rel_path):
            failures.append(f"{rel_path} [{problem}]")

        full = ROOT / rel_path
        try:
            data = full.read_bytes()
        except OSError:
            skipped_binary.append(f"{rel_path} [unreadable]")
            continue
        if b"\x00" in data:
            skipped_binary.append(f"{rel_path} [binary]")
            continue
        text = data.decode("utf-8", errors="replace")
        for label, pattern in CONTENT_PATTERNS:
            if pattern.search(text):
                if (rel_path, label) in ALLOWLIST:
                    allowlisted += 1
                    continue
                failures.append(f"{rel_path} [{label}]")

    print(f"ci_safety_check: scanned {len(files)} tracked files")
    if skipped_binary:
        for item in skipped_binary:
            print(f"ci_safety_check: content scan skipped {item}")
    print(f"ci_safety_check: allowlisted fixture hits: {allowlisted}")
    if failures:
        print("ci_safety_check: FAIL")
        for item in failures:
            print(f"ci_safety_check: suspicious {item}")
        return 1
    print("ci_safety_check: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
