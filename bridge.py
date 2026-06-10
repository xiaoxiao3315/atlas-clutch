from __future__ import annotations

import base64
import atexit
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "bridge.log"
LOG_ROTATE_BYTES = 5 * 1024 * 1024
RUNTIME_DIR = ROOT / "runtime"
LOCK_FILE = RUNTIME_DIR / "bridge.lock"
PID_FILE = RUNTIME_DIR / "bridge.pid"
HEARTBEAT_FILE = RUNTIME_DIR / "heartbeat.json"
STOP_FILE = RUNTIME_DIR / "stop.request"
TEMPLATES_DIR = ROOT / "templates"
WORKBENCH_DIR = ROOT / "workbench"
TASKS_DIR = WORKBENCH_DIR / "tasks"
PROJECTS_DIR = WORKBENCH_DIR / "projects"
EVIDENCE_DIR = WORKBENCH_DIR / "evidence"
RETROS_DIR = WORKBENCH_DIR / "retros"
LEARNING_DIR = WORKBENCH_DIR / "learning"
LEARNING_CANDIDATES_DIR = LEARNING_DIR / "candidates"
LEARNING_PROPOSALS_DIR = LEARNING_DIR / "proposals"
LEARNING_REGISTRY_DIR = LEARNING_DIR / "registry"
LEARNING_REJECTED_DIR = LEARNING_DIR / "rejected"
LEARNING_DEFERRED_DIR = LEARNING_DIR / "deferred"
LEARNING_PACKAGES_DIR = LEARNING_DIR / "packages"
LEARNING_LOGS_DIR = LEARNING_DIR / "logs"
APPLICATIONS_DIR = WORKBENCH_DIR / "applications"
PLAYBOOKS_DIR = WORKBENCH_DIR / "playbooks"
PROJECT_PLAYBOOKS_DIR = PLAYBOOKS_DIR / "projects"
CONTEXT_PACKS_DIR = WORKBENCH_DIR / "context_packs"
DISPATCHES_DIR = WORKBENCH_DIR / "dispatches"
EXECUTIONS_DIR = WORKBENCH_DIR / "executions"
PILOTS_DIR = WORKBENCH_DIR / "pilots"
COLLECTIONS_DIR = WORKBENCH_DIR / "collections"
DECISIONS_DIR = WORKBENCH_DIR / "decisions"
DAILY_DIR = WORKBENCH_DIR / "daily"
ARCHIVE_DIR = WORKBENCH_DIR / "archive"
PROCESSED_STATE_KEY = "processed_message_keys"
DEFAULT_PROCESSED_LIMIT = 500
STARTED_AT = time.time()
STARTED_AT_TEXT = datetime.now().astimezone().isoformat(timespec="seconds")
OPEN_TASK_STATUSES = {"draft", "open", "reported", "reviewed", "needs_evidence"}
DECISION_STATUSES = {"pass", "needs_evidence", "blocked", "cancelled"}
TERMINAL_TASK_STATUSES = {"archived", "passed", "cancelled", "closed"}
EVIDENCE_TYPES = {
    "file",
    "command",
    "log",
    "api",
    "http",
    "process",
    "git",
    "screenshot",
    "ui",
    "live",
    "smoke",
    "report",
    "collection",
    "decision",
    "other",
}
EVIDENCE_MARK_STATUSES = {"verified", "partial", "rejected"}
LEARNING_STATUSES = {"candidate", "proposed", "approved", "rejected", "deferred", "packaged"}
APPLY_STATUSES = {"planned", "applied", "reverted", "cancelled"}
DISPATCH_STATUSES = {
    "draft",
    "ready",
    "sent",
    "returned",
    "qa_ready",
    "reviewed",
    "needs_evidence",
    "failed",
    "cancelled",
    "closed",
}
EXEC_STATUSES = {
    "prepared",
    "started",
    "opened",
    "copied",
    "returned",
    "needs_manual_start",
    "cancelled",
    "failed",
}
PILOT_STATUSES = {"active", "completed", "paused", "cancelled"}
APPLICATION_ENABLED = False
RUNTIME_INJECTION_ENABLED = False
EXTERNAL_APPLICATION_ENABLED = False
EXTERNAL_EXECUTION_ENABLED = False
HUMAN_CONFIRM_REQUIRED = True
AUTO_EXECUTE_ENABLED = False
READ_ONLY_AUTO_EXEC_ENABLED = True
COLLECT_ENABLED = True
COLLECT_MODE = "read_only_whitelist"
ARBITRARY_COMMAND_ENABLED = False
COLLECT_OUTPUT_LIMIT = 6000
COLLECT_TAIL_LINES = 80
COLLECT_COMMAND_TIMEOUT = 20
COLLECT_SMOKE_TIMEOUT = 60
EXEC_START_TIMEOUT_SECONDS = 300
RUNNER_OUTPUT_RECORD_LIMIT = 12000
RUNNER_ERROR_RECORD_LIMIT = 8000
RUNNER_SNAPSHOT_TIMEOUT_SECONDS = 20
RUNNER_SANDBOX_MODES = {"read-only", "workspace-write"}
OCTO_BRIDGE_SMOKE_ALLOWLIST = [
    "smoke_consultation.py",
    "smoke_runtime.py",
    "smoke_workflow.py",
    "smoke_task_loop.py",
    "smoke_handoff.py",
    "smoke_project.py",
    "smoke_evidence.py",
    "smoke_retro.py",
    "smoke_learn.py",
    "smoke_apply.py",
    "smoke_context.py",
    "smoke_dispatch.py",
    "smoke_pilot.py",
    "smoke_auto_evidence.py",
]
COLLECT_PROFILES = {
    "octo-bridge": {
        "root": ROOT,
        "logs": [ROOT / "logs" / "bridge.log"],
        "runtime": [ROOT / "runtime" / "heartbeat.json"],
        "smokes": OCTO_BRIDGE_SMOKE_ALLOWLIST,
    },
    "kiro-gateway": {
        "root": Path("E:/ai/kiro-gateway-mvp"),
        "logs": [
            Path("E:/ai/kiro-gateway-mvp/logs/gateway.log"),
            Path("E:/ai/kiro-gateway-mvp/logs/acp_raw.log"),
        ],
        "runtime": [],
        "smokes": [],
    },
}

LOGGER = logging.getLogger("octo-hermes-bridge")


class AlreadyRunningError(RuntimeError):
    def __init__(self, message: str, lock_info: dict | None = None) -> None:
        super().__init__(message)
        self.lock_info = lock_info or {}


@dataclass
class SingleInstanceGuard:
    runtime_dir: Path
    lock_file: Path
    pid_file: Path
    run_id: str
    pid: int
    active: bool = True

    def release(self) -> None:
        if not self.active:
            return
        self.active = False
        for path in (self.lock_file, self.pid_file):
            try:
                if not path.exists():
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("run_id") == self.run_id:
                    path.unlink()
            except Exception:
                pass


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    if LOGGER.handlers:
        return

    rotate_log_if_needed()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)


def rotate_log_if_needed() -> None:
    if not LOG_FILE.exists() or LOG_FILE.stat().st_size <= LOG_ROTATE_BYTES:
        return
    backup = LOG_FILE.with_name(f"{LOG_FILE.name}.1")
    try:
        if backup.exists():
            backup.unlink()
        LOG_FILE.replace(backup)
    except OSError as exc:
        print(f"[bridge] log rotation skipped: {exc}", file=sys.stderr)


def secret_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        upper_key = key.upper()
        if not value or len(value) < 4:
            continue
        if any(marker in upper_key for marker in ("TOKEN", "SECRET", "KEY", "PASSWORD")):
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact(value) -> str:
    text = str(value)
    for secret in secret_values():
        text = text.replace(secret, "[REDACTED]")
    text = sanitize_sensitive_text(text)
    return text


def log_event(event: str, **fields) -> None:
    if fields:
        detail = " ".join(f"{key}={redact(value)}" for key, value in fields.items())
        LOGGER.info("%s %s", event, detail)
    else:
        LOGGER.info("%s", event)


def log_error(event: str, exc: BaseException | None = None, **fields) -> None:
    detail = " ".join(f"{key}={redact(value)}" for key, value in fields.items())
    if exc is None:
        LOGGER.error("%s %s", event, detail)
        return
    LOGGER.error("%s %s error=%s", event, detail, redact(exc))
    LOGGER.error(redact(traceback.format_exc()))


def short_id(value: str) -> str:
    text = str(value or "")
    if len(text) <= 12:
        return text
    return f"{text[:6]}...{text[-4:]}"


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def startup_method() -> str:
    method = os.environ.get("OHB_START_METHOD", "").strip().lower()
    if method in {"manual", "task"}:
        return method
    legacy_method = os.environ.get("BRIDGE_STARTUP", "").strip().lower()
    if legacy_method == "task":
        return "task"
    return "unknown"


def process_command_line(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        command = (
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = %d\" "
            "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }"
        ) % pid
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_looks_like_bridge(pid: int) -> bool:
    if pid == os.getpid():
        return True
    command_line = process_command_line(pid).lower()
    if not command_line:
        return True
    return "bridge.py" in command_line


def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_json_atomic(path: Path, body: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp_file = path.with_suffix(f"{path.suffix}.tmp")
    tmp_file.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_file, path)


def acquire_single_instance(runtime_dir: Path = RUNTIME_DIR, run_id: str | None = None) -> SingleInstanceGuard:
    runtime_dir.mkdir(exist_ok=True)
    lock_file = runtime_dir / LOCK_FILE.name
    pid_file = runtime_dir / PID_FILE.name
    run_id = run_id or uuid.uuid4().hex
    pid = os.getpid()
    payload = {
        "run_id": run_id,
        "pid": pid,
        "started_at": STARTED_AT_TEXT,
        "script": str(Path(__file__).resolve()),
        "cwd": str(ROOT),
    }

    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            write_json_atomic(pid_file, payload)
            guard = SingleInstanceGuard(runtime_dir, lock_file, pid_file, run_id, pid)
            atexit.register(guard.release)
            return guard
        except FileExistsError:
            lock_info = read_json_file(lock_file)
            existing_pid = int(lock_info.get("pid") or 0)
            if pid_is_running(existing_pid) and process_looks_like_bridge(existing_pid):
                raise AlreadyRunningError(
                    f"bridge is already running with pid {existing_pid}",
                    lock_info,
                )
            try:
                lock_file.unlink(missing_ok=True)
                pid_file.unlink(missing_ok=True)
                log_event("stale_lock_removed", pid=existing_pid, lock=display_path(lock_file))
            except OSError as exc:
                raise AlreadyRunningError(f"could not remove stale lock: {exc}", lock_info) from exc


def lock_status(guard: SingleInstanceGuard | None) -> str:
    if guard and guard.active and guard.lock_file.exists():
        return "held"
    if LOCK_FILE.exists():
        info = read_json_file(LOCK_FILE)
        pid = int(info.get("pid") or 0)
        if pid_is_running(pid):
            return f"held_by_pid_{pid}"
        return "stale"
    return "none"


def stop_requested() -> bool:
    return STOP_FILE.exists()


def clear_stop_request() -> None:
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def build_heartbeat(
    runtime_info: dict,
    state: dict,
    last_seq: int,
    registered: bool,
    robot_id: str = "",
    owner_channel_id: str = "",
) -> dict:
    processed = state.get(PROCESSED_STATE_KEY) or []
    return {
        "run_id": runtime_info.get("run_id", ""),
        "pid": runtime_info.get("pid", os.getpid()),
        "started_at": runtime_info.get("started_at", STARTED_AT_TEXT),
        "updated_at": iso_now(),
        "registered": bool(registered),
        "robot_id_masked": short_id(robot_id),
        "owner_channel_id": owner_channel_id,
        "last_seq": last_seq,
        "processed_count": len(processed),
        "mode": "consultation",
        "startup_method": runtime_info.get("startup_method", startup_method()),
        "lock_status": runtime_info.get("lock_status", "unknown"),
        "last_error": safe_preview(str(state.get("last_error") or ""), 160),
    }


def write_heartbeat(
    runtime_info: dict,
    state: dict,
    last_seq: int,
    registered: bool,
    robot_id: str = "",
    owner_channel_id: str = "",
    heartbeat_file: Path = HEARTBEAT_FILE,
) -> dict:
    heartbeat = build_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
    write_json_atomic(heartbeat_file, heartbeat)
    return heartbeat


def load_env() -> None:
    if not ENV_FILE.exists():
        raise SystemExit(f"Missing .env: {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                if not isinstance(state.get(PROCESSED_STATE_KEY), list):
                    state[PROCESSED_STATE_KEY] = []
                return state
        except Exception as exc:
            log_error("state_load_failed", exc)
            return {}
    return {PROCESSED_STATE_KEY: []}


def save_state(state: dict) -> None:
    write_json_atomic(STATE_FILE, state)


def processed_limit() -> int:
    raw = os.environ.get("PROCESSED_LIMIT", str(DEFAULT_PROCESSED_LIMIT))
    try:
        return max(50, int(raw))
    except ValueError:
        return DEFAULT_PROCESSED_LIMIT


def message_key(msg: dict) -> str:
    seq = int(msg.get("message_seq") or 0)
    from_uid = str(msg.get("from_uid") or "")
    channel_id = str(msg.get("channel_id") or "")
    stable_id = (
        msg.get("message_id")
        or msg.get("msg_id")
        or msg.get("client_msg_no")
        or msg.get("message_no")
        or ""
    )
    return f"{seq}:{from_uid}:{channel_id}:{stable_id}"


def remember_processed(state: dict, key: str, limit: int) -> None:
    recent = [item for item in state.get(PROCESSED_STATE_KEY, []) if item != key]
    recent.append(key)
    state[PROCESSED_STATE_KEY] = recent[-limit:]


def stable_client_msg_no(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:32]


def post_json(path: str, body: dict, headers: dict, timeout: int = 30, retries: int = 2) -> dict:
    api_url = os.environ["OCTO_API_URL"].rstrip("/")
    url = api_url + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    last_error: BaseException | None = None

    for attempt in range(1, retries + 2):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} {url}: {redact(detail)}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt > retries:
                break
            log_event("http_retry", path=path, attempt=attempt, error=exc)
            time.sleep(min(5, attempt * 1.5))

    raise RuntimeError(f"POST failed {url}: {redact(last_error)}")


def decode_content(payload) -> str:
    if payload is None:
        return ""

    if isinstance(payload, dict):
        obj = payload
    else:
        text = str(payload)
        obj = None

        try:
            decoded = base64.b64decode(text).decode("utf-8", errors="replace")
            obj = json.loads(decoded)
        except Exception:
            try:
                obj = json.loads(text)
            except Exception:
                return text

    if isinstance(obj, dict):
        content = obj.get("content", "")
        if isinstance(content, str):
            return content.strip()
        return str(content).strip()

    return str(obj).strip()


def build_consultation_prompt(user_text: str) -> str:
    return f"""你现在通过 Octo-Hermes Bridge 回复用户。

当前是桥接器咨询/调度模式。必须遵守：
1. 你是 Atlas，不是 Codex，也不是 OpenClaw。
2. 你可以理解目标、拆解任务、生成工作单、审查思路、建议下一步。
3. 不要实际运行命令、修改文件、删除文件、提交代码、发布内容。
4. 如果用户要求执行动作，只输出给执行端的工作单，等待用户确认。
5. 没有用户提供的可验证证据时，不要声称任务已经完成。
6. 工作单必须包含：目标、范围、执行边界、验收标准、风险点。
7. 审查返回报告时，必须区分：已验证、未验证、风险、下一步决策。
8. 给出下一步时要明确决策：通过、补证据、继续、回滚或暂停。

用户消息：
{user_text}
"""


def call_hermes(user_text: str) -> str:
    hermes_python = os.environ.get("HERMES_PYTHON", r"E:\ai\Hermes\venv\Scripts\python.exe")
    hermes_cwd = os.environ.get("HERMES_CWD", r"E:\ai")
    timeout = int(os.environ.get("HERMES_TIMEOUT", "1800"))
    prompt = build_consultation_prompt(user_text)

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(
        [hermes_python, "-m", "hermes_cli.main", "-z", prompt],
        cwd=hermes_cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return f"Hermes 调用失败：{redact(stderr) or 'unknown error'}"

    output = (result.stdout or "").strip()
    return output or "Hermes 没有返回内容。"


def is_execution_request(user_text: str) -> bool:
    text = user_text.strip().lower()
    if not text:
        return False

    keywords = [
        "执行",
        "运行",
        "帮我跑",
        "跑一下",
        "启动",
        "修改文件",
        "改文件",
        "写文件",
        "删除文件",
        "提交",
        "发布",
        "部署",
        "安装",
        "execute",
        "run ",
        "powershell",
        "cmd ",
        "bash ",
        "python ",
        "node ",
        "npm ",
        "pnpm ",
        "git ",
        "commit",
        "push",
        "deploy",
    ]
    return any(keyword in text for keyword in keywords)


def safe_preview(text: str, limit: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) > limit:
        compact = compact[: limit - 3] + "..."
    return redact(compact)


def sanitize_sensitive_text(text: str) -> str:
    lines = []
    for raw_line in str(text or "").splitlines(keepends=True):
        line = raw_line
        if is_sensitive_zero_hit_line(line):
            line = re.sub(r"(?i)Authorization\s*:", "AUTH_HEADER_CHECK:", line)
            line = re.sub(r"(?i)Cookie\s*:", "COOKIE_CHECK:", line)
            line = re.sub(r"(?i)bf_", "TOKEN_PREFIX_CHECK", line)
            line = re.sub(r"(?i)sk-", "KEY_PREFIX_CHECK", line)
        lines.append(line)
    sanitized = "".join(lines)
    sanitized = re.sub(r"(?i)bf_[A-Za-z0-9._-]+", "[REDACTED_TOKEN]", sanitized)
    sanitized = re.sub(r"(?i)sk-[A-Za-z0-9._-]+", "[REDACTED_KEY]", sanitized)
    sanitized = re.sub(r"(?i)bf_", "[REDACTED_TOKEN_PREFIX]", sanitized)
    sanitized = re.sub(r"(?i)sk-", "[REDACTED_KEY_PREFIX]", sanitized)
    sanitized = re.sub(r"(?im)^\s*Authorization\s*:.*$", "[REDACTED_HEADER]", sanitized)
    sanitized = re.sub(r"(?im)^\s*Cookie\s*:.*$", "[REDACTED_COOKIE]", sanitized)
    sanitized = re.sub(r"(?im)^\s*password\s*[:=].*$", "[REDACTED_SECRET]", sanitized)
    sanitized = re.sub(r"(?im)^\s*api_key\s*[:=].*$", "[REDACTED_SECRET]", sanitized)
    sanitized = re.sub(r"(?im)^\s*secret\s*[:=].*$", "[REDACTED_SECRET]", sanitized)
    return sanitized


def is_sensitive_zero_hit_line(line: str) -> bool:
    raw = str(line or "")
    lowered = raw.lower()
    has_sensitive_word = any(
        marker in lowered
        for marker in ("bf_", "sk-", "authorization", "cookie", "password", "api_key", "secret")
    )
    if not has_sensitive_word:
        return False
    if re.search(r"(?i)Authorization\s*:\s*(Bearer|Basic)\s+\S+", raw):
        return False
    if re.search(r"(?i)Cookie\s*:\s*[^:\n=]+=", raw):
        return False
    zero_patterns = [
        r"[:：]\s*0\b",
        r"\b0\s*(hit|hits|match|matches|result|results)\b",
        r"\b(no|zero)\s+(hit|hits|match|matches|result|results)\b",
        r"命中数\s*为\s*0",
        r"0\s*命中",
        r"无命中",
        r"无结果",
        r"未命中",
        r"no leak",
        r"not found",
        r"\bnone\b",
    ]
    return any(re.search(pattern, raw, re.IGNORECASE) for pattern in zero_patterns)


def is_empty_sensitive_value(value: str) -> bool:
    cleaned = str(value or "").strip()
    lowered = cleaned.lower()
    if not cleaned:
        return True
    if cleaned in {"0", "-", "—", "无", "无命中", "无结果"}:
        return True
    if lowered in {"none", "n/a", "na", "not applicable", "no hits", "no results", "[redacted]"}:
        return True
    if re.search(r"\b0\s*(hit|hits|match|matches|result|results)\b", lowered):
        return True
    if any(marker in cleaned for marker in ("0 命中", "无命中", "无结果")):
        return True
    return False


def detect_sensitive_findings(text: str) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(line_no: int, finding: str, severity: str, reason: str, source_line: str) -> None:
        key = (finding, line_no)
        if key in seen:
            return
        seen.add(key)
        findings.append(
            {
                "finding": finding,
                "severity": severity,
                "reason": reason,
                "source_line": safe_preview(sanitize_sensitive_text(source_line), 180),
            }
        )

    for line_no, line in enumerate(str(text or "").splitlines(), 1):
        if not line.strip() or is_sensitive_zero_hit_line(line):
            continue
        if re.search(r"(?i)bf_[A-Za-z0-9._-]{4,}", line):
            add(line_no, "token_like_value", "high", f"line {line_no}: token-like value", line)
        if re.search(r"(?i)sk-[A-Za-z0-9][A-Za-z0-9._-]{7,}", line):
            add(line_no, "key_like_value", "high", f"line {line_no}: key-like value", line)
        auth_match = re.search(r"(?i)^\s*Authorization\s*:\s*(.*)$", line)
        if auth_match and not is_empty_sensitive_value(auth_match.group(1)):
            add(line_no, "auth_header_value", "high", f"line {line_no}: auth header contains a value", line)
        cookie_match = re.search(r"(?i)^\s*Cookie\s*:\s*(.*)$", line)
        if cookie_match and not is_empty_sensitive_value(cookie_match.group(1)):
            add(line_no, "cookie_value", "high", f"line {line_no}: cookie header contains a value", line)
        secret_match = re.search(r"(?i)^\s*(password|api_key|secret)\s*[:=]\s*(.*)$", line)
        if secret_match and not is_empty_sensitive_value(secret_match.group(2)):
            add(line_no, "secret_field_value", "high", f"line {line_no}: secret-like field contains a value", line)
    return findings


def report_has_read_only_marker(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    markers = [
        "read-only",
        "read only",
        "readonly",
        "no file changes",
        "no files changed",
        "no files modified",
        "no code changes",
        "not modify",
        "not modified",
        "no commit",
        "did not commit",
        "did not read .env",
        "只读",
        "只读检查",
        "只读诊断",
        "未修改文件",
        "未改代码",
        "未提交",
        "未读取 .env",
        "无修改文件",
    ]
    if any(marker in lowered for marker in markers[:12]) or any(marker in text for marker in markers[12:]):
        return True
    return report_has_no_modification_marker(text)


def report_has_no_modification_marker(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    direct_markers = [
        "modified files: none",
        "modified files: n/a",
        "modified files: na",
        "modified files: no",
        "modified files: not applicable",
        "changed files: none",
        "no modified files",
        "no files modified",
        "no files changed",
        "no code changes",
        "未修改文件",
        "未改代码",
        "无修改文件",
        "修改文件：无",
        "修改文件: 无",
        "修改文件：N/A",
        "修改文件: N/A",
    ]
    if any(marker in lowered for marker in direct_markers[:10]) or any(marker in text for marker in direct_markers[10:]):
        return True
    return bool(
        re.search(
            r"(?is)(modified files|changed files|files changed|修改文件|变更文件)\s*[:：]?\s*(?:\n\s*[-*]?\s*)?(none|n/a|na|no|not applicable|无|无修改|未修改)",
            text,
        )
    )


FALSE_PASS_REPORT_PATTERNS = (
    ("could_not_modify_files", r"\bcould\s+not\s+modify\s+files?\b"),
    ("cannot_modify_files", r"\bcannot\s+modify\s+files?\b"),
    ("read_only_filesystem_permissions", r"\bread[- ]only\s+filesystem\s+permissions\b"),
    ("implementation_blocked", r"\bimplementation\s+(?:was\s+)?blocked\b"),
    ("session_read_only", r"\bsession\s+(?:is|was)\s+read[- ]only\b"),
    ("no_code_change_read_only", r"\bno\s+code\s+change\s+was\s+made\s+because\s+read[- ]only\b"),
    ("unable_to_write", r"\b(?:unable\s+to\s+write|could\s+not\s+write|cannot\s+write)\b"),
    ("needs_evidence", r"\bneeds_evidence\b"),
    ("decision_label_needs_evidence", r"\bdecision\s+label\s*:\s*needs_evidence\b"),
    ("source_write_only_inspected", r"\bsource[- ]write\s+task\s+was\s+only\s+inspected\b"),
)


def read_only_false_pass_reasons(report: str) -> list[str]:
    text = str(report or "")
    reasons = []
    for label, pattern in FALSE_PASS_REPORT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            reasons.append(label)
    return reasons


def report_has_modified_files_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        "modified files" in lowered
        or "changed files" in lowered
        or any(marker in text for marker in ("修改文件", "变更文件"))
    )


def report_has_commands_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        any(marker in lowered for marker in ("commands", "executed commands", "command:", "powershell", "cmd ", "bash ", "curl ", "invoke-restmethod", "python ", "select-string", "get-process", "get-nettcpconnection", "git "))
        or any(marker in text for marker in ("执行命令", "命令：", "命令输出", "测试命令"))
    )


def report_has_test_results_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        any(marker in lowered for marker in ("test results", "tests:", "passed", "failed", "py_compile", "status: 200", "status=200", "http 200", "response contains"))
        or any(marker in text for marker in ("测试结果", "通过", "失败", "未运行", "验证通过"))
    )


def report_has_logs_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        any(marker in lowered for marker in ("key logs", "logs", "screenshots", "screenshot", "gateway.log", "acp_raw.log", "request_id", "evidence"))
        or any(marker in text for marker in ("关键日志", "截图", "日志", "输出", "证据"))
    )


def report_has_risks_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        any(marker in lowered for marker in ("unresolved risks", "risks", "risk:", "blockers", "blocked", "none"))
        or any(marker in text for marker in ("未解决风险", "风险", "待补"))
    )


def report_has_unverified_field(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    return (
        any(marker in lowered for marker in ("unverified", "not verified", "not run", "not covered", "not checked", "none"))
        or any(marker in text for marker in ("未验证", "未运行", "未覆盖", "待补", "无"))
    )


def detect_sensitive_zero_hit_ok(report: str) -> bool:
    text = str(report or "")
    lowered = text.lower()
    if "sensitive_zero_hit_ok: true" in lowered:
        return True
    if any(marker in text for marker in ("AUTH_HEADER_CHECK", "COOKIE_CHECK", "TOKEN_PREFIX_CHECK", "KEY_PREFIX_CHECK")):
        return True
    return any(is_sensitive_zero_hit_line(line) for line in text.splitlines())


def add_observed_item(items: list[dict], name: str, value: str, confidence: str, source_line: str) -> None:
    safe_value = safe_preview(sanitize_sensitive_text(value), 160)
    safe_line = safe_preview(sanitize_sensitive_text(source_line), 220)
    candidate = {
        "name": name,
        "value": safe_value,
        "confidence": confidence,
        "source_line": safe_line,
    }
    if candidate not in items:
        items.append(candidate)


def analyze_evidence_intake(report: str, acceptance: str = "") -> dict:
    text = str(report or "")
    lowered = text.lower()
    evidence_type = detect_evidence_type_from_report(text)
    read_only_mode = report_has_read_only_marker(text)
    no_modification_ok = bool(read_only_mode and report_has_no_modification_marker(text))
    sensitive_findings = detect_sensitive_findings(text)
    sensitive_risk = bool(sensitive_findings)
    sensitive_zero_hit_ok = detect_sensitive_zero_hit_ok(text)
    observed_items: list[dict] = []

    for line in text.splitlines():
        lowered_line = line.lower()
        if "/v1/chat/completions" in lowered_line:
            add_observed_item(observed_items, "api_endpoint", "/v1/chat/completions", "high", line)
        status_match = re.search(r"(?i)(?:http\s*)?status\s*[:=]?\s*(\d{3})", line)
        if status_match:
            add_observed_item(observed_items, "http_status", status_match.group(1), "high", line)
        response_match = re.search(r"(?i)response\s+contains\s+([A-Za-z0-9._:-]+)", line)
        if response_match:
            add_observed_item(observed_items, "response_contains", response_match.group(1), "high", line)
        request_match = re.search(r"\b(req_[A-Za-z0-9._-]+)\b", line)
        if request_match:
            add_observed_item(observed_items, "request_id", request_match.group(1), "high", line)
        if "gateway.log" in lowered_line:
            add_observed_item(observed_items, "gateway_log", "mentioned", "medium", line)
        if "acp_raw.log" in lowered_line:
            add_observed_item(observed_items, "acp_raw_log", "mentioned", "medium", line)
        if re.search(r"(?i)\bpython\s+smoke_[A-Za-z0-9_-]+\.py\b", line):
            add_observed_item(observed_items, "smoke_command", line.strip(), "high", line)
        if re.search(r"(?i)\bPID\b|\bGet-Process\b|\bGet-NetTCPConnection\b", line):
            add_observed_item(observed_items, "process_check", line.strip(), "medium", line)
        if re.search(r"(?i)\bgit\s+(status|branch)\b", line):
            add_observed_item(observed_items, "git_check", line.strip(), "medium", line)
        if any(marker in lowered_line for marker in ("octo ui", "screenshot")) or any(marker in line for marker in ("截图",)):
            add_observed_item(observed_items, "ui_evidence", line.strip(), "medium", line)
        if is_sensitive_zero_hit_line(line):
            add_observed_item(observed_items, "sensitive_zero_hit", line.strip(), "medium", line)

    if no_modification_ok:
        add_observed_item(observed_items, "modified_files", "not_applicable_read_only", "high", "read-only report says no files were modified")

    modified_ok = report_has_modified_files_field(text) or no_modification_ok
    commands_ok = report_has_commands_field(text)
    tests_ok = report_has_test_results_field(text)
    logs_ok = report_has_logs_field(text)
    risks_ok = report_has_risks_field(text)
    unverified_ok = report_has_unverified_field(text)
    supports_acceptance = bool(text.strip()) and (
        evidence_markers_present(text)
        or bool(str(acceptance or "").strip())
        or bool(observed_items)
    )

    missing_items = []
    checks = [
        ("modified_files", modified_ok, "return report should list changed files or state none/not applicable for read-only work"),
        ("commands", commands_ok, "return report should include commands/checks performed"),
        ("test_results", tests_ok, "return report should include test or validation results"),
        ("key_logs_or_screenshots", logs_ok, "return report should include logs, screenshots, request ids, or equivalent evidence"),
        ("unverified", unverified_ok, "return report should state what remains unverified, even if none"),
        ("unresolved_risks", risks_ok, "return report should state unresolved risks, even if none"),
        ("acceptance_support", supports_acceptance, "return report should contain observable support for acceptance criteria"),
    ]
    for item, ok, reason in checks:
        if not ok:
            missing_items.append({"item": item, "reason": reason})

    live_skipped = detect_live_skipped(text)
    if sensitive_risk:
        recommendation = "blocked"
    elif not text.strip():
        recommendation = "blocked"
    elif live_skipped or missing_items:
        recommendation = "needs_evidence"
    else:
        recommendation = "pass_candidate"

    return {
        "evidence_type": evidence_type,
        "observed_items": observed_items,
        "missing_items": missing_items,
        "read_only_mode": read_only_mode,
        "no_modification_ok": no_modification_ok,
        "sensitive_risk": sensitive_risk,
        "sensitive_risk_reason": "actual sensitive-looking value found" if sensitive_risk else ("zero-hit checks only" if sensitive_zero_hit_ok else "none"),
        "sensitive_findings": sensitive_findings,
        "sensitive_zero_hit_ok": sensitive_zero_hit_ok,
        "recommendation": recommendation,
        "live_skipped": live_skipped,
    }


def format_intake_summary(intake: dict) -> str:
    observed = intake.get("observed_items", [])
    missing = intake.get("missing_items", [])
    findings = intake.get("sensitive_findings", [])
    observed_lines = [
        f"- {item.get('name')}: {item.get('value')} | confidence={item.get('confidence')} | source={item.get('source_line')}"
        for item in observed[:12]
    ] or ["- none"]
    missing_lines = [
        f"- {item.get('item')}: {item.get('reason')}"
        for item in missing[:12]
    ] or ["- none"]
    finding_lines = [
        f"- {item.get('finding')} | severity={item.get('severity')} | reason={item.get('reason')} | source={item.get('source_line')}"
        for item in findings[:8]
    ] or ["- none"]
    return f"""Auto Evidence Intake
evidence_type: {intake.get('evidence_type', 'report')}
read_only_mode: {str(bool(intake.get('read_only_mode'))).lower()}
no_modification_ok: {str(bool(intake.get('no_modification_ok'))).lower()}
sensitive_risk: {str(bool(intake.get('sensitive_risk'))).lower()}
sensitive_risk_reason: {intake.get('sensitive_risk_reason', 'none')}
sensitive_zero_hit_ok: {str(bool(intake.get('sensitive_zero_hit_ok'))).lower()}
recommendation: {intake.get('recommendation', 'needs_evidence')}

observed_items:
{chr(10).join(observed_lines)}

missing_items:
{chr(10).join(missing_lines)}

sensitive_findings:
{chr(10).join(finding_lines)}"""


def build_auto_evidence_body(report: str, intake: dict) -> str:
    clean_report = sanitize_sensitive_text(report).strip() or "- empty report"
    return f"""{clean_report}

## Auto Evidence Summary
{format_intake_summary(intake)}
"""


def ensure_workbench_dirs() -> None:
    global APPLICATIONS_DIR, PLAYBOOKS_DIR, PROJECT_PLAYBOOKS_DIR, CONTEXT_PACKS_DIR, DISPATCHES_DIR, EXECUTIONS_DIR, PILOTS_DIR, COLLECTIONS_DIR
    if WORKBENCH_DIR.resolve() not in APPLICATIONS_DIR.resolve().parents:
        APPLICATIONS_DIR = WORKBENCH_DIR / "applications"
    if WORKBENCH_DIR.resolve() not in PLAYBOOKS_DIR.resolve().parents:
        PLAYBOOKS_DIR = WORKBENCH_DIR / "playbooks"
    if PLAYBOOKS_DIR.resolve() not in PROJECT_PLAYBOOKS_DIR.resolve().parents:
        PROJECT_PLAYBOOKS_DIR = PLAYBOOKS_DIR / "projects"
    if WORKBENCH_DIR.resolve() not in CONTEXT_PACKS_DIR.resolve().parents:
        CONTEXT_PACKS_DIR = WORKBENCH_DIR / "context_packs"
    if WORKBENCH_DIR.resolve() not in DISPATCHES_DIR.resolve().parents:
        DISPATCHES_DIR = WORKBENCH_DIR / "dispatches"
    if WORKBENCH_DIR.resolve() not in EXECUTIONS_DIR.resolve().parents:
        EXECUTIONS_DIR = WORKBENCH_DIR / "executions"
    if WORKBENCH_DIR.resolve() not in PILOTS_DIR.resolve().parents:
        PILOTS_DIR = WORKBENCH_DIR / "pilots"
    if WORKBENCH_DIR.resolve() not in COLLECTIONS_DIR.resolve().parents:
        COLLECTIONS_DIR = WORKBENCH_DIR / "collections"
    for path in (
        WORKBENCH_DIR,
        TASKS_DIR,
        PROJECTS_DIR,
        EVIDENCE_DIR,
        RETROS_DIR,
        LEARNING_DIR,
        LEARNING_CANDIDATES_DIR,
        LEARNING_PROPOSALS_DIR,
        LEARNING_REGISTRY_DIR,
        LEARNING_REJECTED_DIR,
        LEARNING_DEFERRED_DIR,
        LEARNING_PACKAGES_DIR,
        LEARNING_LOGS_DIR,
        APPLICATIONS_DIR,
        PLAYBOOKS_DIR,
        PROJECT_PLAYBOOKS_DIR,
        CONTEXT_PACKS_DIR,
        DISPATCHES_DIR,
        EXECUTIONS_DIR,
        PILOTS_DIR,
        COLLECTIONS_DIR,
        DECISIONS_DIR,
        DAILY_DIR,
        ARCHIVE_DIR,
    ):
        path.mkdir(exist_ok=True)


def ensure_inside_workbench(path: Path) -> Path:
    resolved = path.resolve()
    base = WORKBENCH_DIR.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"refusing to write outside workbench: {path}")
    return resolved


def ensure_inside_playbooks(path: Path) -> Path:
    resolved = ensure_inside_workbench(path)
    base = PLAYBOOKS_DIR.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"refusing to write outside workbench/playbooks: {path}")
    return resolved


def normalize_task_id(task_id: str) -> str:
    value = task_id.strip()
    if not re.fullmatch(r"OHB-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid task_id")
    return value


def task_path(task_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(TASKS_DIR / f"{normalize_task_id(task_id)}.md")


def decision_path(task_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(DECISIONS_DIR / f"{normalize_task_id(task_id)}.md")


def evidence_path(task_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(EVIDENCE_DIR / f"{normalize_task_id(task_id)}.md")


def retro_path(task_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(RETROS_DIR / f"{normalize_task_id(task_id)}.md")


def normalize_learn_id(learn_id: str) -> str:
    value = str(learn_id or "").strip()
    if not re.fullmatch(r"LEARN-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid learn_id")
    return value


def proposal_path(learn_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(LEARNING_PROPOSALS_DIR / f"{normalize_learn_id(learn_id)}.md")


def registry_path(learn_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(LEARNING_REGISTRY_DIR / f"{normalize_learn_id(learn_id)}.md")


def package_path(learn_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(LEARNING_PACKAGES_DIR / f"{normalize_learn_id(learn_id)}.md")


def normalize_apply_id(apply_id: str) -> str:
    value = str(apply_id or "").strip()
    if not re.fullmatch(r"APPLY-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid apply_id")
    return value


def normalize_context_id(context_id: str) -> str:
    value = str(context_id or "").strip()
    if not re.fullmatch(r"CTX-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid context_id")
    return value


def normalize_dispatch_id(dispatch_id: str) -> str:
    value = str(dispatch_id or "").strip()
    if not re.fullmatch(r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid dispatch_id")
    return value


def normalize_exec_id(exec_id: str) -> str:
    value = str(exec_id or "").strip()
    if not re.fullmatch(r"EXEC-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid exec_id")
    return value


def normalize_pilot_id(pilot_id: str) -> str:
    value = str(pilot_id or "").strip()
    if not re.fullmatch(r"PILOT-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid pilot_id")
    return value


def normalize_collection_id(collection_id: str) -> str:
    value = str(collection_id or "").strip()
    if not re.fullmatch(r"COLLECT-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid collection_id")
    return value


def application_path(apply_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(APPLICATIONS_DIR / f"{normalize_apply_id(apply_id)}.md")


def context_pack_path(context_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(CONTEXT_PACKS_DIR / f"{normalize_context_id(context_id)}.md")


def dispatch_path(dispatch_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(DISPATCHES_DIR / f"{normalize_dispatch_id(dispatch_id)}.md")


def exec_path(exec_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(EXECUTIONS_DIR / f"{normalize_exec_id(exec_id)}.md")


def pilot_path(pilot_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(PILOTS_DIR / f"{normalize_pilot_id(pilot_id)}.md")


def collection_path(collection_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(COLLECTIONS_DIR / f"{normalize_collection_id(collection_id)}.md")


def global_playbook_path() -> Path:
    ensure_workbench_dirs()
    return ensure_inside_playbooks(PLAYBOOKS_DIR / "atlas_workbench_playbook.md")


def project_playbook_path(project_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_playbooks(PROJECT_PLAYBOOKS_DIR / f"{validate_project_id(project_id)}.md")


def normalize_evidence_id(evidence_id: str) -> str:
    value = evidence_id.strip()
    if not re.fullmatch(r"EV-\d{8}-\d{6}(?:-\d{2})?", value):
        raise ValueError("invalid evidence_id")
    return value


def validate_project_id(project_id: str) -> str:
    value = str(project_id or "").strip()
    forbidden = ("../", "..\\", "/", "\\", ":", "*", "?", '"', "<", ">", "|")
    if any(item in value for item in forbidden):
        raise ValueError("invalid project_id: contains forbidden path or filename characters")
    if not re.fullmatch(r"[a-z0-9_-]+", value):
        raise ValueError("invalid project_id: use lowercase letters, numbers, '-' or '_' only")
    return value


def project_path(project_id: str) -> Path:
    ensure_workbench_dirs()
    return ensure_inside_workbench(PROJECTS_DIR / f"{validate_project_id(project_id)}.md")


def read_project(project_id: str) -> str:
    path = project_path(project_id)
    if not path.exists():
        raise FileNotFoundError(f"project not found: {project_id}")
    return path.read_text(encoding="utf-8")


def write_project(project_id: str, text: str) -> None:
    path = project_path(project_id)
    path.write_text(sanitize_sensitive_text(text), encoding="utf-8")


def project_title_from_text(project_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {project_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "未命名项目"


def generate_task_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("OHB-%Y%m%d-%H%M%S")
    if not task_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not task_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique task_id")


def sanitize_title(title: str) -> str:
    text = sanitize_sensitive_text(str(title or "")).strip()
    text = " ".join(text.split())
    return text[:120] or "未命名任务"


def build_task_markdown(task_id: str, title: str, project_id: str = "") -> str:
    now = iso_now()
    clean_title = sanitize_title(title)
    clean_project_id = validate_project_id(project_id) if project_id else ""
    return f"""# {task_id} {clean_title}

status: open
created_at: {now}
updated_at: {now}
source: octo
mode: consultation
project_id: {clean_project_id}
evidence_gap_risk: true
live_skipped: false

## Goal
- {clean_title}

## Scope
- 仅围绕本任务目标生成人工执行工作单。
- 不扩展到未授权项目、服务、群聊、多用户或 WebSocket。

## Execution Boundary
- Bridge/Atlas 只写本地 workbench 账本，不执行命令。
- 不自动调用 Codex/Kiro/OpenClaw。
- 不修改用户项目文件，不改 Octo Docker 部署，不改 Hermes 主体代码。
- 不读取、不记录、不提交 `.env`、token、密钥、cookie。

## Acceptance Criteria
- 执行端回传修改文件、执行命令、测试结果和未解决风险。
- Atlas 审查区分已验证、未验证、风险、待补证据、下一步建议。
- 用户做出 pass、needs_evidence、blocked 或 cancelled 决策。

## Acceptance Evidence
- criterion: 执行端回传修改文件、执行命令、测试结果和未解决风险。
  status: missing
  evidence_ids:
  notes: 尚未记录证据。
- criterion: Atlas 审查区分已验证、未验证、风险、待补证据、下一步建议。
  status: missing
  evidence_ids:
  notes: 尚未记录证据。
- criterion: 用户做出 pass、needs_evidence、blocked 或 cancelled 决策。
  status: missing
  evidence_ids:
  notes: 尚未记录证据。

## Risks
- 缺少证据时不能判定完成。
- 执行范围不清时需要补充边界。
- 任意真实执行动作必须由用户另行交给执行端处理。

## Evidence Required
- 修改文件：
- 执行命令：
- 测试结果：
- 关键日志或截图：
- 未解决风险：

## Execution Report
- 尚未回传。

## Atlas Review
- 尚未审查。

## User Decision
- 尚未决策。

## Timeline
- {now} task created with status open.
"""


def build_project_markdown(project_id: str, title: str) -> str:
    now = iso_now()
    clean_title = sanitize_title(title)
    return f"""# {project_id} {clean_title}

status: active
created_at: {now}
updated_at: {now}
description: {clean_title}
priority: P2
owner: 小小
mode: consultation

## Goal
- {clean_title}

## Current State
- 项目已创建，等待任务归属和证据回传。

## Active Tasks
- 暂无。

## Blocked Tasks
- 暂无。

## Recent Decisions
- 暂无。

## Next Actions
- 创建或关联任务：/task new <标题> --project {project_id}

## Notes
- {now} project created.
"""


def create_project(project_id: str, title: str) -> tuple[str, str]:
    clean_project_id = validate_project_id(project_id)
    path = project_path(clean_project_id)
    if path.exists():
        raise FileExistsError(f"project already exists: {clean_project_id}")
    text = build_project_markdown(clean_project_id, title)
    write_project(clean_project_id, text)
    log_event("project_created", project_id=clean_project_id)
    return clean_project_id, text


def project_metadata(text: str) -> dict:
    return task_metadata(text)


def project_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(PROJECTS_DIR.glob("*.md")):
        project_id = path.stem
        try:
            validate_project_id(project_id)
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        meta = project_metadata(text)
        records.append(
            {
                "project_id": project_id,
                "title": project_title_from_text(project_id, text),
                "status": meta.get("status", "unknown"),
                "priority": meta.get("priority", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "path": path,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def read_task(task_id: str) -> str:
    path = task_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"task not found: {task_id}")
    return path.read_text(encoding="utf-8")


def write_task(task_id: str, text: str) -> None:
    path = task_path(task_id)
    write_json_atomic(path.with_suffix(".lock.json"), {"task_id": task_id, "updated_at": iso_now()})
    path.write_text(sanitize_sensitive_text(text), encoding="utf-8")
    try:
        path.with_suffix(".lock.json").unlink(missing_ok=True)
    except OSError:
        pass


def task_title_from_text(task_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {task_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "未命名任务"


def task_metadata(text: str) -> dict:
    meta: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if line.startswith("## "):
            break
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta


def task_section(text: str, heading: str) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^## .+$", text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end():end].strip()


def generate_evidence_id(task_id: str) -> str:
    normalize_task_id(task_id)
    base = datetime.now().strftime("EV-%Y%m%d-%H%M%S")
    existing_ids = {record["evidence_id"] for record in evidence_records(task_id)}
    if base not in existing_ids:
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if candidate not in existing_ids:
            return candidate
    raise RuntimeError("could not generate unique evidence_id")


def read_evidence_text(task_id: str) -> str:
    path = evidence_path(task_id)
    if not path.exists():
        return f"# {normalize_task_id(task_id)} Evidence Ledger\n"
    return path.read_text(encoding="utf-8")


def write_evidence_text(task_id: str, text: str) -> None:
    path = evidence_path(task_id)
    path.write_text(sanitize_sensitive_text(text), encoding="utf-8")


def parse_evidence_entry(section: str, fallback_id: str) -> dict:
    fields = {
        "evidence_id": fallback_id,
        "task_id": "",
        "created_at": "",
        "source": "",
        "type": "",
        "verified": "no",
        "supports_acceptance": "",
        "claim": "",
        "observed": "",
        "risk": "",
        "notes": "",
    }
    current_key = ""
    for raw_line in section.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            continue
        matched_key = ""
        for key in fields:
            prefix = f"{key}:"
            if line.startswith(prefix):
                matched_key = key
                fields[key] = line[len(prefix):].strip()
                break
        if matched_key:
            current_key = matched_key
            continue
        if current_key:
            fields[current_key] = (fields[current_key] + "\n" + line).strip()
    if not fields["evidence_id"]:
        fields["evidence_id"] = fallback_id
    return fields


def evidence_records(task_id: str) -> list[dict]:
    normalize_task_id(task_id)
    text = read_evidence_text(task_id)
    pattern = re.compile(r"^## (EV-\d{8}-\d{6}(?:-\d{2})?)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    records = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start():end].strip()
        records.append(parse_evidence_entry(section, match.group(1)))
    return records


def detect_live_skipped(text: str) -> bool:
    value = str(text or "").lower()
    patterns = [
        r"live[^。\n]*(跳过|未做|待补|未运行|未执行|skipped|not run|not verified)",
        r"octo ui[^。\n]*(跳过|未做|待补|未运行|未执行|skipped|not run|not verified)",
        r"live\s+ui[^。\n]*(skipped|not run|not verified)",
    ]
    if any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns):
        return True
    return any(marker in str(text or "") for marker in ("live 验收跳过", "live 回归未运行", "live 待补", "Octo UI live 回归未运行"))


def generated_owner_write_exec_is_verified(task_id: str, exec_id: str) -> bool:
    try:
        exec_meta = task_metadata(read_exec(normalize_exec_id(exec_id)))
    except Exception:
        return False
    owner_write_ok = {
        "owner_write_policy": str(exec_meta.get("owner_write_policy", "")).lower() == "true",
        "owner_write_policy_status": exec_meta.get("owner_write_policy_status") == "returned",
        "write_target_fidelity": exec_meta.get("write_target_fidelity") == "passed",
        "post_run_target_fidelity": exec_meta.get("post_run_target_fidelity") == "passed",
        "runner_sandbox": exec_meta.get("runner_sandbox") == "workspace-write",
        "write_confirmed": str(exec_meta.get("write_confirmed", "")).lower() == "true",
        "returncode": str(exec_meta.get("returncode")) == "0",
        "timed_out": str(exec_meta.get("timed_out", "")).lower() == "false",
        "completion_state": exec_meta.get("completion_state") == "completed",
    }
    if not all(owner_write_ok.values()):
        return False
    for record in evidence_records(task_id):
        if record.get("verified") not in {"verified", "yes"}:
            continue
        if record.get("supports_acceptance") != "observed":
            continue
        combined = "\n".join(str(record.get(key, "")) for key in ("claim", "observed", "notes", "mark_note"))
        if exec_id in combined:
            return True
    return False


def live_skip_is_owner_write_auto_postprocess_caveat(task_id: str, text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    if not re.search(
        r"live\s+octo/atlas[^\n]*(/exec receive|/dispatch qa|/task review|final user decision)[^\n]*(not run|not verified)",
        lowered,
    ):
        return False
    exec_ids = sorted(set(re.findall(r"\bEXEC-\d{8}-\d{6}\b", value)))
    return any(generated_owner_write_exec_is_verified(task_id, exec_id) for exec_id in exec_ids)


def report_has_claim(text: str) -> bool:
    value = str(text or "").lower()
    return any(marker in str(text or "") for marker in ("完成", "通过", "已修复", "验收通过", "OK")) or any(
        marker in value for marker in ("done", "passed", "pass", "completed", "fixed")
    )


def detect_evidence_type_from_report(report: str) -> str:
    value = str(report or "").lower()
    if "/v1/chat/completions" in value and (
        "status" in value or "response contains" in value or "http" in value
    ):
        return "api"
    if any(marker in value for marker in ("gateway.log", "acp_raw.log", "request_id")):
        return "log"
    if any(marker in value for marker in ("pid", "get-process", "get-nettcpconnection")):
        return "process"
    if any(marker in value for marker in ("git status", "git branch")):
        return "git"
    if any(marker in value for marker in ("http status", "status: 200", "status=200", "http 200")):
        return "http"
    if "smoke" in value or "smoke_" in value or "smoke 测试" in str(report or ""):
        return "smoke"
    if "live" in value or "octo ui" in value:
        return "live"
    if any(marker in str(report or "") for marker in ("截图", "screenshot")):
        return "screenshot"
    if any(marker in str(report or "") for marker in ("日志", "log")):
        return "log"
    if any(marker in str(report or "") for marker in ("执行命令", "命令输出", "command")):
        return "command"
    return "report"


def evidence_support_state(body: str, evidence_type: str) -> str:
    if evidence_markers_present(body) or evidence_type in {"file", "command", "log", "api", "http", "process", "git", "screenshot", "ui", "live", "smoke", "collection"}:
        return "observed"
    if report_has_claim(body):
        return "claimed"
    return "missing"


def build_evidence_entry(
    task_id: str,
    evidence_id: str,
    evidence_type: str,
    body: str,
    source: str = "user",
    verified: str = "no",
) -> str:
    clean_body = sanitize_sensitive_text(body).strip() or "- 空证据正文。"
    support = evidence_support_state(clean_body, evidence_type)
    risk = "live_skipped" if detect_live_skipped(clean_body) else "none"
    return f"""## {evidence_id}
evidence_id: {evidence_id}
task_id: {normalize_task_id(task_id)}
created_at: {iso_now()}
source: {sanitize_title(source).lower()}
type: {evidence_type}
verified: {verified}
supports_acceptance: {support}
claim: {safe_preview(clean_body, 180) if report_has_claim(clean_body) else '未直接声称完成。'}
observed: {safe_preview(clean_body, 260) if support == 'observed' else '未提供可观察证据。'}
risk: {risk}
notes:
{clean_body}
"""


def create_evidence_entry(
    task_id: str,
    evidence_type: str,
    body: str,
    source: str = "user",
    verified: str = "no",
    sync_task: bool = True,
) -> str:
    normalized_task_id = normalize_task_id(task_id)
    read_task(normalized_task_id)
    clean_type = evidence_type.strip().lower()
    if clean_type not in EVIDENCE_TYPES:
        raise ValueError(f"invalid evidence type: {clean_type}")
    evidence_id = generate_evidence_id(normalized_task_id)
    ledger = read_evidence_text(normalized_task_id).rstrip()
    entry = build_evidence_entry(normalized_task_id, evidence_id, clean_type, body, source=source, verified=verified)
    write_evidence_text(normalized_task_id, ledger + "\n\n" + entry)
    now = iso_now()
    task_text = read_task(normalized_task_id)
    task_text = append_to_section(task_text, "Timeline", f"- {now} evidence {evidence_id} added type={clean_type}.")
    write_task(normalized_task_id, task_text)
    if sync_task:
        sync_task_evidence_state(normalized_task_id)
    log_event("evidence_added", task_id=normalized_task_id, evidence_id=evidence_id, type=clean_type)
    return evidence_id


def update_evidence_mark(task_id: str, evidence_id: str, status: str, note: str) -> dict:
    normalized_task_id = normalize_task_id(task_id)
    normalized_evidence_id = normalize_evidence_id(evidence_id)
    clean_status = status.strip().lower()
    if clean_status not in EVIDENCE_MARK_STATUSES:
        raise ValueError("invalid evidence mark status")
    ledger = read_evidence_text(normalized_task_id)
    pattern = re.compile(
        rf"(^## {re.escape(normalized_evidence_id)}\s*$)(.*?)(?=^## EV-\d{{8}}-\d{{6}}(?:-\d{{2}})?\s*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(ledger)
    if not match:
        raise FileNotFoundError(f"evidence not found: {normalized_evidence_id}")
    section = match.group(0)
    if re.search(r"^verified:.*$", section, re.MULTILINE):
        section = re.sub(r"^verified:.*$", f"verified: {clean_status}", section, count=1, flags=re.MULTILINE)
    else:
        section = section.rstrip() + f"\nverified: {clean_status}\n"
    section = section.rstrip() + f"\nmark_note: {iso_now()} {sanitize_sensitive_text(note).strip() or '未填写说明。'}\n"
    write_evidence_text(normalized_task_id, ledger[: match.start()] + section + ledger[match.end():])
    sync_task_evidence_state(normalized_task_id)
    log_event("evidence_marked", task_id=normalized_task_id, evidence_id=normalized_evidence_id, status=clean_status)
    for record in evidence_records(normalized_task_id):
        if record["evidence_id"] == normalized_evidence_id:
            return record
    raise FileNotFoundError(f"evidence not found after update: {normalized_evidence_id}")


def acceptance_criteria_items(text: str) -> list[str]:
    criteria = []
    for line in task_section(text, "Acceptance Criteria").splitlines():
        value = line.strip()
        if value.startswith("- "):
            criteria.append(value[2:].strip())
    return criteria or ["执行端提供可复核证据，并由用户最终验收。"]


def required_evidence_missing(combined_text: str, has_report_or_evidence: bool) -> list[str]:
    if not has_report_or_evidence:
        return ["Execution Report 或 Evidence Ledger"]
    text = str(combined_text or "")
    intake = analyze_evidence_intake(text)
    label_map = {
        "modified_files": "ä¿®æ”¹æ–‡ä»¶",
        "commands": "æ‰§è¡Œå‘½ä»¤",
        "test_results": "æµ‹è¯•ç»“æžœ",
        "key_logs_or_screenshots": "å…³é”®æ—¥å¿—æˆ–æˆªå›¾",
        "unresolved_risks": "æœªè§£å†³é£Žé™©",
        "unverified": "æœªéªŒè¯è¯´æ˜Ž",
        "acceptance_support": "æ”¯æ’‘éªŒæ”¶æ ‡å‡†",
    }
    label_map = {
        "modified_files": "modified_files",
        "commands": "commands",
        "test_results": "test_results",
        "key_logs_or_screenshots": "key_logs_or_screenshots",
        "unresolved_risks": "unresolved_risks",
        "unverified": "unverified",
        "acceptance_support": "acceptance_support",
    }
    missing = [label_map.get(item.get("item", ""), item.get("item", "unknown")) for item in intake["missing_items"]]
    if intake["sensitive_risk"]:
        missing.append("sensitive_risk")
    return [item for item in missing if item]
    lowered = text.lower()
    checks = [
        ("修改文件", any(marker in text for marker in ("修改文件", "文件：", ".py", ".md", ".json", ".cmd")) or "modified files" in lowered),
        ("执行命令", any(marker in text for marker in ("执行命令", "命令：", "命令输出", "python ", "Select-String", "npm ", "pnpm ")) or "commands" in lowered),
        ("测试结果", any(marker in text for marker in ("测试结果", "通过", "失败", "未运行", "smoke", "py_compile")) or any(marker in lowered for marker in ("test results", "passed", "failed"))),
        ("关键日志或截图", any(marker in text for marker in ("关键日志", "截图", "日志", "输出", "证据")) or any(marker in lowered for marker in ("logs", "screenshots", "evidence"))),
        ("未解决风险", any(marker in text for marker in ("未解决风险", "风险", "待补", "未验证", "blocked")) or "unresolved risks" in lowered),
    ]
    return [name for name, ok in checks if not ok]


def evidence_analysis(task_id: str, text: str | None = None) -> dict:
    normalized_task_id = normalize_task_id(task_id)
    task_text = text if text is not None else read_task(normalized_task_id)
    report = task_section(task_text, "Execution Report")
    records = evidence_records(normalized_task_id)
    evidence_blob = "\n".join(
        "\n".join(str(record.get(key, "")) for key in ("claim", "observed", "risk", "notes"))
        for record in records
    )
    combined = "\n".join([report, evidence_blob])
    intake = analyze_evidence_intake(combined)
    has_report = bool(report.strip()) and "尚未回传" not in report
    has_evidence = bool(records)
    claimed = []
    if report_has_claim(report) and not evidence_markers_present(report):
        claimed.append("Execution Report 声称完成或通过，但缺少可观察证据字段。")
    claimed.extend(
        f"{record['evidence_id']} {safe_preview(record.get('claim', ''), 120)}"
        for record in records
        if record.get("supports_acceptance") == "claimed"
    )
    observed_records = [
        record for record in records
        if record.get("supports_acceptance") == "observed" and record.get("verified") not in {"verified", "yes"}
    ]
    verified_records = [record for record in records if record.get("verified") in {"verified", "yes"}]
    partial_records = [record for record in records if record.get("verified") == "partial"]
    rejected_records = [record for record in records if record.get("verified") == "rejected"]
    live_skipped = detect_live_skipped(combined)
    if live_skipped and live_skip_is_owner_write_auto_postprocess_caveat(normalized_task_id, combined):
        live_skipped = False
    missing = required_evidence_missing(combined, has_report or has_evidence)
    if observed_records and not verified_records:
        missing.append("observed 证据尚未标记 verified")
    if claimed and not observed_records and not verified_records:
        missing.append("claimed 声明缺少 observed/verified 证据")
    if live_skipped:
        missing.append("Octo UI live 验收")
    risks = []
    if live_skipped:
        risks.append("live_skipped：本地通过不等于 Octo UI live 通过，不能建议 pass。")
    if rejected_records:
        risks.append("存在 rejected 证据。")
    if partial_records:
        risks.append("存在 partial 证据，需要补齐。")
    if not verified_records:
        risks.append("没有 verified 证据，不能判定完成，Atlas Review 不能建议 pass。")
    missing = sorted(set(item for item in missing if item))
    recommendation = "pass"
    if not has_report and not has_evidence:
        recommendation = "blocked"
    elif missing or risks or not verified_records:
        recommendation = "needs_evidence"
    criteria = acceptance_criteria_items(task_text)
    status = "verified" if verified_records and not live_skipped and not missing else (
        "observed" if observed_records or partial_records else ("claimed" if claimed else "missing")
    )
    if live_skipped and status == "verified":
        status = "observed"
    return {
        "task_id": normalized_task_id,
        "records": records,
        "report": report,
        "claimed": claimed,
        "observed": observed_records,
        "verified": verified_records,
        "partial": partial_records,
        "rejected": rejected_records,
        "missing": missing,
        "risks": risks,
        "live_skipped": live_skipped,
        "recommendation": recommendation,
        "criteria": criteria,
        "criteria_status": status,
        "evidence_ids": [record["evidence_id"] for record in records],
        "evidence_gap_count": len(missing) + (0 if verified_records else 1),
        "has_gaps": bool(missing or risks or not verified_records),
        "intake": intake,
        "read_only_no_modification_ok": bool(intake.get("read_only_mode") and intake.get("no_modification_ok")),
        "sensitive_zero_hit_ok": bool(intake.get("sensitive_zero_hit_ok")),
    }


def evidence_closure_state(analysis: dict) -> str:
    if analysis.get("has_gaps"):
        return "closed_with_evidence_gap_risk"
    if analysis.get("recommendation") == "pass" and analysis.get("verified"):
        return "verified_evidence_ready"
    return "needs_evidence_review"


def evidence_ready_for_auto_close(analysis: dict) -> bool:
    return evidence_closure_state(analysis) == "verified_evidence_ready"


def build_closure_evidence_summary(analysis: dict) -> str:
    missing = ", ".join(str(item) for item in analysis.get("missing", [])[:5]) or "none"
    risks = " | ".join(str(item) for item in analysis.get("risks", [])[:3]) or "none"
    return f"""- evidence_closure_state: {evidence_closure_state(analysis)}
- evidence_gap_risk: {str(bool(analysis.get('has_gaps'))).lower()}
- recommendation_at_close: {analysis.get('recommendation', 'needs_evidence')}
- verified_count: {len(analysis.get('verified', []))}
- observed_unverified_count: {len(analysis.get('observed', []))}
- claimed_count: {len(analysis.get('claimed', []))}
- missing_count: {len(analysis.get('missing', []))}
- live_skipped: {str(bool(analysis.get('live_skipped'))).lower()}
- missing: {safe_preview(missing, 240)}
- risks: {safe_preview(risks, 260)}"""


def build_evidence_gaps_text(task_id: str, analysis: dict | None = None) -> str:
    data = analysis or evidence_analysis(task_id)
    claimed_lines = [f"- {item}" for item in data["claimed"]] or ["- 无。"]
    observed_lines = [
        f"- {record['evidence_id']} observed but verified={record.get('verified', 'no')}：{safe_preview(record.get('observed') or record.get('notes'), 140)}"
        for record in data["observed"]
    ] or ["- 无。"]
    missing_lines = [f"- {item}" for item in data["missing"]] or ["- 无。"]
    risk_lines = [f"- {item}" for item in data["risks"]] or ["- 无。"]
    remaining_gaps = "none" if not data["missing"] and not data["risks"] and data["verified"] else ", ".join(data["missing"] or data["risks"])
    gap_recommendation = "ready_for_review" if remaining_gaps == "none" else data.get("recommendation", "needs_evidence")
    next_step = "补充 evidence 或 mark verified 后再 review。"
    if data["live_skipped"]:
        next_step = "补 Octo UI live 验收证据；当前只能写本地通过，live 待补。"
    elif data["observed"] and not data["verified"]:
        next_step = "人工核对 observed 证据后执行 /evidence accept <task_id> <evidence_id> <reason>，或 /evidence mark <task_id> <evidence_id> verified|partial|rejected <说明>。"
    elif not data["records"]:
        next_step = "先通过 /evidence add 或 /task report 记录证据。"
    return f"""只有 claimed：
{chr(10).join(claimed_lines)}

observed 但未 verified：
{chr(10).join(observed_lines)}

missing：
{chr(10).join(missing_lines)}

风险：
{chr(10).join(risk_lines)}

read_only_no_modification_ok: {str(bool(data.get('read_only_no_modification_ok'))).lower()}
sensitive_zero_hit_ok: {str(bool(data.get('sensitive_zero_hit_ok'))).lower()}
remaining_gaps: {remaining_gaps}
recommendation: {gap_recommendation}

下一步需要补：
- {next_step}"""


def build_acceptance_evidence_body(analysis: dict) -> str:
    evidence_ids = ", ".join(analysis.get("evidence_ids", []))
    notes = []
    if analysis.get("live_skipped"):
        notes.append("live skipped，不能完整封板。")
    if analysis.get("missing"):
        notes.append("缺口：" + "；".join(analysis["missing"][:6]))
    if not notes:
        notes.append("证据链当前无明显缺口。")
    lines = []
    for criterion in analysis.get("criteria", []):
        lines.extend(
            [
                f"- criterion: {criterion}",
                f"  status: {analysis.get('criteria_status', 'missing')}",
                f"  evidence_ids: {evidence_ids}",
                f"  notes: {' '.join(notes)}",
            ]
        )
    if analysis.get("live_skipped"):
        lines.extend(
            [
                "- criterion: Octo UI live 验收",
                "  status: missing",
                f"  evidence_ids: {evidence_ids}",
                "  notes: report 明确跳过或未完成 live 验收。",
            ]
        )
    return "\n".join(lines)


def sync_task_evidence_state(task_id: str, text: str | None = None) -> dict:
    normalized_task_id = normalize_task_id(task_id)
    task_text = text if text is not None else read_task(normalized_task_id)
    analysis = evidence_analysis(normalized_task_id, task_text)
    task_text = set_section_body(task_text, "Acceptance Evidence", build_acceptance_evidence_body(analysis))
    task_text = replace_task_field(task_text, "evidence_gap_risk", "true" if analysis["has_gaps"] else "false")
    task_text = replace_task_field(task_text, "live_skipped", "true" if analysis["live_skipped"] else "false")
    task_text = replace_task_field(task_text, "updated_at", iso_now())
    write_task(normalized_task_id, task_text)
    return analysis


def replace_task_field(text: str, field: str, value: str) -> str:
    replacement = f"{field}: {sanitize_sensitive_text(value)}"
    pattern = re.compile(rf"^{re.escape(field)}:.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return text.replace("\n\n## Goal", f"\n{replacement}\n\n## Goal", 1)


def append_to_section(text: str, heading: str, addition: str) -> str:
    clean_addition = sanitize_sensitive_text(addition).strip()
    if not clean_addition:
        clean_addition = "- 无正文。"
    heading_line = f"## {heading}"
    if heading_line not in text:
        return text.rstrip() + f"\n\n{heading_line}\n{clean_addition}\n"
    pattern = re.compile(rf"(^## {re.escape(heading)}\s*$)", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return text.rstrip() + f"\n\n{heading_line}\n{clean_addition}\n"
    next_match = re.search(r"^## .+$", text[match.end():], re.MULTILINE)
    insert_at = match.end() + next_match.start() if next_match else len(text)
    before = text[:insert_at].rstrip()
    after = text[insert_at:]
    return before + "\n\n" + clean_addition + "\n" + after


def set_section_body(text: str, heading: str, body: str) -> str:
    clean_body = sanitize_sensitive_text(body).strip() or "- 暂无。"
    heading_line = f"## {heading}"
    if heading_line not in text:
        return text.rstrip() + f"\n\n{heading_line}\n{clean_body}\n"
    pattern = re.compile(rf"(^## {re.escape(heading)}\s*$)", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return text.rstrip() + f"\n\n{heading_line}\n{clean_body}\n"
    next_match = re.search(r"^## .+$", text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    before = text[:match.end()].rstrip()
    after = text[end:]
    return before + "\n" + clean_body + "\n" + after


def update_task_status(text: str, status: str) -> str:
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", iso_now())
    return text


def create_task(title: str, project_id: str = "") -> tuple[str, str]:
    ensure_workbench_dirs()
    task_id = generate_task_id()
    text = build_task_markdown(task_id, title, project_id=project_id)
    write_task(task_id, text)
    log_event("task_created", task_id=task_id, project_id=project_id or "unassigned")
    return task_id, text


def task_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(TASKS_DIR.glob("OHB-*.md")):
        task_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "task_id": task_id,
                "title": task_title_from_text(task_id, text),
                "status": meta.get("status", "unknown"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "project_id": meta.get("project_id", ""),
                "path": path,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def project_task_records(project_id: str) -> list[dict]:
    clean_project_id = validate_project_id(project_id)
    return [record for record in task_records() if record.get("project_id") == clean_project_id]


def task_project_id(task_id: str) -> str:
    return task_metadata(read_task(task_id)).get("project_id", "")


def attach_task_to_project(project_id: str, task_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    normalized_task_id = normalize_task_id(task_id)
    project_text = read_project(clean_project_id)
    task_text = read_task(normalized_task_id)
    task_meta = task_metadata(task_text)
    existing_project = task_meta.get("project_id", "").strip()
    if existing_project and existing_project != clean_project_id:
        raise ValueError(
            f"task already belongs to project {existing_project}; this command will not overwrite it automatically"
        )

    now = iso_now()
    task_title = task_title_from_text(normalized_task_id, task_text)
    task_status_value = task_meta.get("status", "unknown")
    if existing_project != clean_project_id:
        task_text = replace_task_field(task_text, "project_id", clean_project_id)
        task_text = replace_task_field(task_text, "updated_at", now)
        task_text = append_to_section(task_text, "Timeline", f"- {now} attached to project {clean_project_id}.")
        write_task(normalized_task_id, task_text)

    active_section = task_section(project_text, "Active Tasks")
    if normalized_task_id not in active_section:
        existing_lines = [
            line for line in active_section.splitlines()
            if line.strip() and line.strip() != "- 暂无。"
        ]
        existing_lines.append(f"- {normalized_task_id} | {task_status_value} | {sanitize_title(task_title)}")
        project_text = set_section_body(project_text, "Active Tasks", "\n".join(existing_lines))
        project_text = replace_task_field(project_text, "updated_at", now)
        write_project(clean_project_id, project_text)
    log_event("project_task_attached", project_id=clean_project_id, task_id=normalized_task_id)
    return normalized_task_id


def workbench_counts() -> dict:
    counts = {
        "open_tasks": 0,
        "reported_tasks": 0,
        "needs_evidence_tasks": 0,
        "recent_task_id": "",
        "active_projects": 0,
        "paused_projects": 0,
        "archived_projects": 0,
        "learning_proposals": 0,
        "learning_approved": 0,
        "learning_not_applied": 0,
        "apply_plans": 0,
        "playbook_entries": 0,
        "applied_to_workbench_playbook": 0,
        "context_pack_count": 0,
        "latest_context_id": "",
        "dispatch_count": 0,
        "dispatch_sent": 0,
        "dispatch_returned": 0,
        "dispatch_needs_evidence": 0,
        "dispatch_ready_count": 0,
        "dispatch_failed_count": 0,
        "dispatch_stale_count": 0,
        "execution_count": 0,
        "execution_returned": 0,
        "execution_prepared_count": 0,
        "execution_started_count": 0,
        "execution_opened_count": 0,
        "execution_copied_count": 0,
        "execution_needs_manual_start_count": 0,
        "execution_failed_count": 0,
        "execution_stale_count": 0,
        "latest_exec_id": "",
        "collection_count": 0,
        "latest_collection_id": "",
        "smoke_collection_count": 0,
        "failed_collection_count": 0,
    }
    records = task_records()
    if records:
        counts["recent_task_id"] = records[0]["task_id"]
    for record in records:
        status = record.get("status")
        if status in OPEN_TASK_STATUSES:
            counts["open_tasks"] += 1
        if status == "reported":
            counts["reported_tasks"] += 1
        if status == "needs_evidence":
            counts["needs_evidence_tasks"] += 1
    for project in project_records():
        status = project.get("status")
        if status == "active":
            counts["active_projects"] += 1
        if status == "paused":
            counts["paused_projects"] += 1
        if status == "archived":
            counts["archived_projects"] += 1
    learn_counts = learning_counts()
    counts["learning_proposals"] = learn_counts["learning_proposals"]
    counts["learning_approved"] = learn_counts["learning_approved"]
    counts["learning_not_applied"] = learn_counts["learning_not_applied"]
    apply_view = apply_counts()
    counts["apply_plans"] = apply_view["apply_plans"]
    counts["playbook_entries"] = apply_view["playbook_entries"]
    counts["applied_to_workbench_playbook"] = apply_view["applied_to_workbench_playbook"]
    context_view = context_counts()
    counts["context_pack_count"] = context_view["context_pack_count"]
    counts["latest_context_id"] = context_view["latest_context_id"]
    dispatch_view = dispatch_counts()
    counts["dispatch_count"] = dispatch_view["dispatch_count"]
    counts["dispatch_sent"] = dispatch_view["dispatch_sent_count"]
    counts["dispatch_returned"] = dispatch_view["dispatch_returned_count"]
    counts["dispatch_needs_evidence"] = dispatch_view["dispatch_needs_evidence_count"]
    counts["dispatch_ready_count"] = dispatch_view["dispatch_ready_count"]
    counts["dispatch_failed_count"] = dispatch_view["dispatch_failed_count"]
    counts["dispatch_stale_count"] = dispatch_view["dispatch_stale_count"]
    exec_view = exec_counts()
    counts["execution_count"] = exec_view["execution_count"]
    counts["execution_returned"] = exec_view["execution_returned_count"]
    counts["execution_prepared_count"] = exec_view["execution_prepared_count"]
    counts["execution_started_count"] = exec_view["execution_started_count"]
    counts["execution_opened_count"] = exec_view["execution_opened_count"]
    counts["execution_copied_count"] = exec_view["execution_copied_count"]
    counts["execution_needs_manual_start_count"] = exec_view["execution_needs_manual_start_count"]
    counts["execution_failed_count"] = exec_view["execution_failed_count"]
    counts["execution_stale_count"] = exec_view["execution_stale_count"]
    counts["latest_exec_id"] = exec_view["latest_exec_id"]
    collect_view = collection_counts()
    counts["collection_count"] = collect_view["collection_count"]
    counts["latest_collection_id"] = collect_view["latest_collection_id"]
    counts["smoke_collection_count"] = collect_view["smoke_collection_count"]
    counts["failed_collection_count"] = collect_view["failed_collection_count"]
    return counts


def build_task_help_reply() -> str:
    return """Atlas 任务账本命令
- /task help：查看本说明。
- /task new <标题>：创建任务工作单，只写 workbench，不执行。
- /task new <标题> --project <project_id>：创建任务并归属到项目。
- /task list：列出最近 10 个任务。
- /task show <task_id>：查看任务摘要、状态和下一步。
- /task handoff <task_id> codex|kiro：生成可复制给执行端的标准交接包。
- /task handoff <task_id> codex|kiro --with-context：追加 Context Pack 摘要和 Playbook Advisory。
- /task report <task_id>：下一行粘贴 Codex/Kiro 回传报告，状态改为 reported。
- /task qa <task_id>：检查回传报告是否满足最小证据要求，不替代 Atlas 审查。
- /task review <task_id>：读取任务与回传报告，生成 Atlas 审查。
- /task next <task_id>：根据当前状态给出下一步建议。
- /task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>：记录用户决策。
- /task close <task_id>：仅 passed/cancelled 可关闭，状态改为 archived。
- /evidence add/list/show/mark/gaps：维护任务证据链。
- /daily brief：汇总今日 open/reported/reviewed/needs_evidence 任务。"""


def build_task_help_reply() -> str:
    return """Atlas task ledger commands
- /task help: show this help.
- /task new <title>: create a work order in workbench only; no execution.
- /task new <title> --project <project_id>: create and attach to a project.
- /task list: list recent tasks.
- /task show <task_id>: show task summary, latest dispatch, status, and next step.
- /task handoff <task_id> codex|kiro [--with-context]: copy-only executor handoff.
- /dispatch create <task_id> codex|kiro [--with-context]: create a manual dispatch record.
- /dispatch package <dispatch_id>: show copy-only execution package.
- /dispatch mark <dispatch_id> sent <note>: record manual send.
- /dispatch receive <dispatch_id>: paste return report and sync the task report.
- /dispatch qa <dispatch_id>: QA the returned report.
- /dispatch link-review <dispatch_id>: link task review status.
- /task report <task_id>: paste Codex/Kiro return report; status becomes reported.
- /task qa <task_id>: check minimum report evidence; does not replace Atlas review.
- /task review <task_id>: generate Atlas review.
- /task next <task_id>: recommend the next manual step.
- /task accept-evidence <task_id> <reason>: batch accept all unverified observed evidence; no decision or close effect.
- /task decide <task_id> <pass|needs_evidence|blocked|cancelled> <note>: record user decision.
- /task close <task_id>: close only after passed/cancelled.
- /evidence add/list/show/mark/gaps: maintain evidence chain.
- /daily brief: summarize today's active task loop."""


def parse_task_new_tail(tail: str) -> tuple[str, str]:
    text = str(tail or "").strip()
    if " --project " not in f" {text} ":
        return text, ""
    title_part, project_part = re.split(r"\s+--project\s+", text, maxsplit=1)
    project_tokens = project_part.strip().split(maxsplit=1)
    if not project_tokens:
        raise ValueError("missing project_id after --project")
    return title_part.strip(), validate_project_id(project_tokens[0])


def build_task_new_reply(title: str) -> str:
    clean_title, project_id = parse_task_new_tail(title)
    if project_id:
        if not project_path(project_id).exists():
            return f"项目不存在：{project_id}。未创建任务，请先执行 /project new {project_id} <项目名称>。"
    task_id, text = create_task(clean_title, project_id=project_id)
    if project_id:
        attach_task_to_project(project_id, task_id)
        text = read_task(task_id)
    project_line = f"\n项目：{project_id}" if project_id else ""
    return f"""任务已创建：{task_id}

状态：open
{project_line}
路径：workbench/tasks/{task_id}.md

工作单：
{text}

下一步：
- 把上面的工作单交给 Codex/Kiro 人工执行。
- 执行端回传后，在 Octo 发送：/task report {task_id}
  <粘贴报告>"""


def build_task_list_reply() -> str:
    records = task_records()[:10]
    if not records:
        return "最近任务：暂无。"
    lines = ["最近 10 个任务："]
    for record in records:
        lines.append(
            f"- {record['task_id']} | {record['status']} | {record['updated_at']} | {record['title']}"
        )
    return "\n".join(lines)


def build_task_next_advice(task_id: str, status: str, latest_dispatch: dict | None = None) -> str:
    dispatch = latest_dispatch if latest_dispatch is not None else latest_dispatch_for_task(task_id)
    if not dispatch:
        return f"create manual dispatch: /dispatch create {task_id} codex --with-context"
    dispatch_id = dispatch.get("dispatch_id", "")
    dispatch_status_value = dispatch.get("status", "unknown")
    latest_exec = latest_exec_for_dispatch(dispatch_id) if dispatch_id else None
    if latest_exec and latest_exec.get("status") == "prepared":
        return f"start read-only runner: /exec start {dispatch_id}; fallback payload: /exec package {latest_exec.get('exec_id')}"
    if latest_exec and latest_exec.get("status") == "started":
        return f"auto runner started; inspect /exec show {latest_exec.get('exec_id')}"
    if latest_exec and latest_exec.get("status") == "needs_manual_start":
        return f"manual start required: /exec package {latest_exec.get('exec_id')}; then /exec receive {latest_exec.get('exec_id')}"
    if latest_exec and latest_exec.get("status") in {"opened", "copied"}:
        return f"wait for executor return; when ready use /exec receive {latest_exec.get('exec_id')}"
    if latest_exec and latest_exec.get("status") == "returned" and dispatch_status_value in {"returned", "sent", "ready"}:
        return f"run /dispatch qa {dispatch_id}, then /task review {task_id}"
    if dispatch_status_value == "ready":
        return f"prepare semi-auto execution session: /exec prepare {dispatch_id}"
    if dispatch_status_value == "sent":
        return f"wait for manual return; when available use /dispatch receive {dispatch_id}"
    if dispatch_status_value == "returned":
        return f"run /dispatch qa {dispatch_id}, then /task review {task_id}"
    if dispatch_status_value == "qa_ready":
        return f"run /task review {task_id}, then /dispatch link-review {dispatch_id}"
    if dispatch_status_value == "needs_evidence":
        return f"supply more report evidence with /dispatch receive {dispatch_id} or create a new dispatch"
    if dispatch_status_value == "reviewed":
        return f"record decision: /task decide {task_id} pass|needs_evidence|blocked|cancelled <note>"
    if dispatch_status_value in {"failed", "cancelled"}:
        return f"inspect failed/cancelled dispatch {dispatch_id}; create a new dispatch if the task remains active"
    if dispatch_status_value == "closed":
        if status in {"passed", "cancelled", "archived"}:
            return "task/dispatch are closed or ready to close"
        return f"dispatch is closed; create a new manual dispatch if task status remains {status}"
    return "status unknown; inspect /task show and /dispatch show first"


def build_task_show_reply(task_id: str) -> str:
    text = read_task(task_id)
    meta = task_metadata(text)
    goal = safe_preview(task_section(text, "Goal"), 220) or "未填写"
    review = safe_preview(task_section(text, "Atlas Review"), 220)
    decision = safe_preview(task_section(text, "User Decision"), 220)
    next_step = "等待 Codex/Kiro 回传报告"
    status = meta.get("status", "unknown")
    if status == "reported":
        next_step = f"发送 /task review {task_id}"
    elif status == "reviewed":
        next_step = f"发送 /task decide {task_id} needs_evidence|pass <说明>"
    elif status == "needs_evidence":
        next_step = "补充证据后再次 /task report"
    elif status == "passed":
        next_step = f"发送 /task close {task_id}"
    elif status == "cancelled":
        next_step = f"发送 /task close {task_id}"
    project_id = meta.get("project_id", "").strip()
    project_line = f"- project：{project_id}\n" if project_id else ""
    return f"""任务摘要：{task_id}
- status：{status}
- title：{task_title_from_text(task_id, text)}
{project_line}- updated_at：{meta.get('updated_at', '')}
- goal：{goal}
- atlas_review：{review or '尚未审查'}
- user_decision：{decision or '尚未决策'}
- 下一步：{next_step}"""


def build_task_show_reply(task_id: str) -> str:
    text = read_task(task_id)
    meta = task_metadata(text)
    normalized_task_id = normalize_task_id(task_id)
    latest_dispatch = latest_dispatch_for_task(normalized_task_id)
    goal = safe_preview(task_section(text, "Goal"), 220) or "not filled"
    review = safe_preview(task_section(text, "Atlas Review"), 220)
    decision = safe_preview(task_section(text, "User Decision"), 220)
    status = meta.get("status", "unknown")
    project_id = meta.get("project_id", "").strip()
    next_step = build_task_next_advice(normalized_task_id, status, latest_dispatch)
    dispatch_id = latest_dispatch.get("dispatch_id") if latest_dispatch else "none"
    dispatch_status = latest_dispatch.get("status") if latest_dispatch else "none"
    target_executor = latest_dispatch.get("target_executor") if latest_dispatch else "none"
    latest_exec = latest_exec_for_dispatch(dispatch_id) if latest_dispatch else None
    exec_id = latest_exec.get("exec_id") if latest_exec else "none"
    exec_status = latest_exec.get("status") if latest_exec else "none"
    project_line = f"- project: {project_id}\n- project：{project_id}\n" if project_id else ""
    legacy_next = "等待 Codex/Kiro 回传报告" if status == "open" else next_step
    return f"""任务摘要 / Task summary: {normalized_task_id}
- status: {status}
- status：{status}
- title: {task_title_from_text(normalized_task_id, text)}
{project_line}- updated_at: {meta.get('updated_at', '')}
- latest_dispatch_id: {dispatch_id}
- dispatch_status: {dispatch_status}
- target_executor: {target_executor}
- latest_exec_id: {exec_id}
- exec_status: {exec_status}
- goal: {goal}
- atlas_review: {review or 'not reviewed'}
- user_decision: {decision or 'not decided'}
- next: {next_step}
- 下一步：{legacy_next}
- safety: Atlas/Bridge records the manual workflow only; it does not execute commands or call Codex/Kiro."""


def build_task_report_reply(task_id: str, report: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_task(normalized_task_id)
    intake = analyze_evidence_intake(report, task_section(text, "Acceptance Criteria"))
    clean_report = sanitize_sensitive_text(report).strip() or "- 空报告：需要补充执行证据。"
    now = iso_now()
    addition = f"### Report at {now}\n{clean_report}"
    text = text.replace("## Execution Report\n- 尚未回传.", "## Execution Report")
    text = append_to_section(text, "Execution Report", addition)
    text = append_to_section(text, "Timeline", f"- {now} report appended; status reported.")
    text = update_task_status(text, "reported")
    write_task(normalized_task_id, text)
    evidence_type = intake["evidence_type"]
    evidence_body = build_auto_evidence_body(report, intake)
    evidence_id = create_evidence_entry(
        normalized_task_id,
        evidence_type,
        evidence_body,
        source="user",
        verified="no",
        sync_task=False,
    )
    analysis = sync_task_evidence_state(normalized_task_id)
    log_event("task_reported", task_id=normalized_task_id, evidence_id=evidence_id)
    gap_line = "- evidence_gap_risk：true（证据缺口仍需处理）" if analysis["has_gaps"] else "- evidence_gap_risk：false"
    live_line = "- live_skipped：true（live 待补）" if analysis["live_skipped"] else "- live_skipped：false"
    return f"""任务已记录回传：{task_id}
- status：reported
- 已追加到：workbench/tasks/{normalized_task_id}.md
- 自动证据：{evidence_id} type={evidence_type} verified=no
- auto_intake_recommendation: {intake['recommendation']}
- read_only_mode: {str(bool(intake['read_only_mode'])).lower()}
- no_modification_ok: {str(bool(intake['no_modification_ok'])).lower()}
- sensitive_risk: {str(bool(intake['sensitive_risk'])).lower()}
{gap_line}
{live_line}
- 下一步：发送 /task qa {normalized_task_id}，再发送 /task review {normalized_task_id}"""


def evidence_markers_present(text: str) -> bool:
    value = str(text or "")
    text = value
    lowered = value.lower()
    english_markers = (
        "modified files",
        "changed files",
        "commands:",
        "test results",
        "key logs",
        "screenshots",
        "unverified:",
        "unresolved risks",
        "rollback notes",
        "post-run snapshot",
        "stdout summary",
        "request_id",
        "status: 200",
        "py_compile",
        "passed:",
        "failed:",
    )
    if any(marker in lowered for marker in english_markers):
        return True
    return any(marker in text for marker in ("修改文件", "执行命令", "测试结果", "通过", "日志", "截图", "路径", "输出"))


def build_atlas_review_for_task(task_id: str, text: str) -> str:
    analysis = evidence_analysis(task_id, text)
    report = analysis["report"]
    acceptance = task_section(text, "Acceptance Criteria")
    verified_lines = [
        f"- {record['evidence_id']} | {record.get('type', '')} | {safe_preview(record.get('observed') or record.get('notes'), 160)}"
        for record in analysis["verified"]
    ] or ["- 无 verified 证据。没有 verified 证据时，不能建议 pass。"]
    observed_lines = [
        f"- {record['evidence_id']} | {record.get('type', '')} | verified={record.get('verified', 'no')} | {safe_preview(record.get('observed') or record.get('notes'), 160)}"
        for record in analysis["observed"]
    ] or ["- 无 observed 证据。"]
    claimed_lines = [f"- {item}" for item in analysis["claimed"]] or ["- 无仅声称项。"]
    missing_lines = [f"- {item}" for item in analysis["missing"]] or ["- 无明显缺失项。"]
    risk_lines = [f"- {item}" for item in analysis["risks"]] or ["- 未发现明显风险。"]
    decision = analysis["recommendation"]
    if analysis["live_skipped"]:
        decision = "needs_evidence（本地通过，live 待补）"
    elif analysis["observed"] and not analysis["verified"]:
        decision = "needs_evidence（observed 尚未 verified）"
    return f"""### Review at {iso_now()}

已验证 verified：
{chr(10).join(verified_lines)}

已观察 observed：
{chr(10).join(observed_lines)}

仅声称 claimed：
{chr(10).join(claimed_lines)}

缺失 missing：
{chr(10).join(missing_lines)}

风险 risk：
{chr(10).join(risk_lines)}

待补证据 evidence gaps：
{build_evidence_gaps_text(task_id, analysis)}

下一步建议：
- 建议决策：{decision}。
- 验收标准摘要：{safe_preview(acceptance, 220) or '未读取到验收标准。'}
- 用户确认前不关闭任务；用户强制 pass 也不会把缺失证据自动标为 verified。"""


def build_task_review_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_task(normalized_task_id)
    status = task_metadata(text).get("status", "unknown")
    if status in TERMINAL_TASK_STATUSES:
        return f"""Atlas review no-op: {normalized_task_id}
- status: {status}
- already_terminal: true
- ledger_write: none
- reason: terminal task status; not reopening review
- review_preview: {safe_preview(task_section(text, 'Atlas Review'), 420) or 'none'}"""
    sync_task_evidence_state(normalized_task_id)
    text = read_task(normalized_task_id)
    review = build_atlas_review_for_task(normalized_task_id, text)
    now = iso_now()
    text = append_to_section(text, "Atlas Review", review)
    text = append_to_section(text, "Timeline", f"- {now} Atlas review generated; status reviewed.")
    text = update_task_status(text, "reviewed")
    write_task(normalized_task_id, text)
    log_event("task_reviewed", task_id=normalized_task_id)
    return f"""Atlas 审查已生成：{normalized_task_id}

{review}

状态：reviewed
下一步：
- /task decide {normalized_task_id} needs_evidence <说明>
- 或由用户强制记录：/task decide {normalized_task_id} pass <说明>"""


def build_task_decide_reply(task_id: str, decision: str, note: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    normalized = decision.strip().lower()
    if normalized not in DECISION_STATUSES:
        return "决策无效。可用：pass、needs_evidence、blocked、cancelled。"
    status = "passed" if normalized == "pass" else normalized
    clean_note = sanitize_sensitive_text(note).strip() or "未填写说明。"
    now = iso_now()
    pre_analysis = sync_task_evidence_state(normalized_task_id)
    text = read_task(normalized_task_id)
    risk_note = ""
    if normalized == "pass" and pre_analysis["has_gaps"]:
        risk_note = "\n- evidence_gap_risk：true\n- reminder：当前仍存在未验证/缺失证据；用户可强制记录 pass，但缺口不会自动标为 verified。"
    decision_text = f"### Decision at {now}\n- decision：{normalized}\n- status：{status}\n- note：{clean_note}"
    if risk_note:
        decision_text += risk_note
    text = append_to_section(text, "User Decision", decision_text)
    text = append_to_section(text, "Timeline", f"- {now} user decision {normalized}; status {status}.")
    text = update_task_status(text, status)
    if risk_note:
        text = replace_task_field(text, "evidence_gap_risk", "true")
    write_task(normalized_task_id, text)
    decision_record = f"# {normalized_task_id} decision\n\n{decision_text}\n"
    decision_path(normalized_task_id).write_text(sanitize_sensitive_text(decision_record), encoding="utf-8")
    log_event("task_decided", task_id=normalized_task_id, decision=normalized)
    warning = ""
    if risk_note:
        warning = "\n- 提醒：当前仍存在未验证/缺失证据，已保留 evidence_gap_risk=true。"
    return f"""用户决策已记录：{task_id}
- decision：{normalized}
- status：{status}
{warning}
- 下一步：{('/task close ' + normalized_task_id) if status in {'passed', 'cancelled'} else '按决策补证据、处理阻塞或暂停'}"""


def evidence_record_has_sensitive_risk(record: dict) -> bool:
    text = "\n".join(
        str(record.get(key, ""))
        for key in ("claim", "observed", "risk", "notes")
    )
    lowered = text.lower()
    if "sensitive_risk: true" in lowered or "sensitive_riskï¼štrue" in lowered:
        return True
    return bool(analyze_evidence_intake(text).get("sensitive_risk"))


def batch_accept_skip_reason(record: dict) -> str:
    support = str(record.get("supports_acceptance", "")).strip().lower()
    verified = str(record.get("verified", "no")).strip().lower()
    if support != "observed":
        return f"supports_acceptance={support or 'missing'}"
    if verified in {"verified", "yes"}:
        return "already_verified"
    if verified in {"partial", "rejected"}:
        return f"marked_{verified}"
    if verified not in {"", "no", "false"}:
        return f"verified={verified}"
    if evidence_record_has_sensitive_risk(record):
        return "sensitive_risk"
    return ""


def build_task_accept_evidence_reply(task_id: str, note: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    clean_note = sanitize_sensitive_text(note).strip()
    if not clean_note:
        raise ValueError("accept reason is required")
    read_task(normalized_task_id)
    before_status = task_status(normalized_task_id)
    records = evidence_records(normalized_task_id)
    accepted_ids = []
    skipped = []
    for record in records:
        reason = batch_accept_skip_reason(record)
        if reason:
            skipped.append((record.get("evidence_id", "unknown"), reason))
            continue
        updated = update_evidence_mark(
            normalized_task_id,
            record["evidence_id"],
            "verified",
            f"batch accepted by user: {clean_note}",
        )
        accepted_ids.append(updated["evidence_id"])
    analysis = evidence_analysis(normalized_task_id)
    after_status = task_status(normalized_task_id)
    accepted_lines = [f"- {evidence_id}" for evidence_id in accepted_ids] or ["- none"]
    skipped_lines = [f"- {evidence_id}: {reason}" for evidence_id, reason in skipped[:20]] or ["- none"]
    log_event(
        "task_evidence_batch_accepted",
        task_id=normalized_task_id,
        accepted_count=len(accepted_ids),
        skipped_count=len(skipped),
    )
    return f"""task evidence accepted: {normalized_task_id}
- accepted_count: {len(accepted_ids)}
- skipped_count: {len(skipped)}
- accepted_ids:
{chr(10).join(accepted_lines)}
- skipped:
{chr(10).join(skipped_lines)}
- evidence_gap_risk: {str(analysis['has_gaps']).lower()}
- recommendation: {analysis['recommendation']}
- evidence_closure_state: {evidence_closure_state(analysis)}
- status_before: {before_status}
- status_after: {after_status}
- decision_effect: none
- auto_close_effect: none
- next: /evidence gaps {normalized_task_id} or /task review {normalized_task_id}"""


def build_task_close_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    analysis = sync_task_evidence_state(normalized_task_id)
    text = read_task(normalized_task_id)
    status = task_metadata(text).get("status", "unknown")
    if status not in {"passed", "cancelled"}:
        return f"不能关闭：{normalized_task_id} 当前 status={status}。只有 passed/cancelled 可以关闭。"
    now = iso_now()
    closure_summary = build_closure_evidence_summary(analysis)
    closure_state = evidence_closure_state(analysis)
    text = append_to_section(text, "Closure Evidence", f"### Close at {now}\n{closure_summary}")
    text = append_to_section(text, "Timeline", f"- {now} task archived from status {status}; evidence_closure_state={closure_state}.")
    text = update_task_status(text, "archived")
    text = replace_task_field(text, "evidence_gap_risk", "true" if analysis["has_gaps"] else "false")
    text = replace_task_field(text, "live_skipped", "true" if analysis["live_skipped"] else "false")
    write_task(normalized_task_id, text)
    log_event("task_archived", task_id=normalized_task_id)
    retro_line = f"- retro：尚未生成，建议发送 /retro create {normalized_task_id}。"
    if retro_exists(normalized_task_id):
        try:
            retro_meta = task_metadata(read_retro(normalized_task_id))
            retro_line = f"- retro：workbench/retros/{normalized_task_id}.md status={retro_meta.get('status', 'unknown')}"
        except Exception:
            retro_line = f"- retro：workbench/retros/{normalized_task_id}.md"
    closure_warning = "evidence gaps remain; archived does not mean verified completion" if analysis["has_gaps"] else "none"
    return f"""任务已关闭：{normalized_task_id}
- status：archived
- 文件保留：workbench/tasks/{normalized_task_id}.md
- closure_warning: {closure_warning}
{closure_summary}
{retro_line}"""


def task_status(task_id: str) -> str:
    return task_metadata(read_task(task_id)).get("status", "unknown")


SUPPORTED_EXECUTOR_TARGETS = {"codex", "kiro", "claude"}
EXECUTOR_DISPLAY_NAMES = {"codex": "Codex", "kiro": "Kiro", "claude": "Claude"}


def build_task_handoff_reply(task_id: str, platform: str) -> str:
    platform_parts = platform.strip().lower().split()
    with_context = "--with-context" in platform_parts
    target = " ".join(part for part in platform_parts if part != "--with-context").strip()
    if target not in SUPPORTED_EXECUTOR_TARGETS:
        return "用法：/task handoff <task_id> codex|claude|kiro [--with-context]"
    text = read_task(task_id)
    title = task_title_from_text(task_id, text)
    display_platform = EXECUTOR_DISPLAY_NAMES.get(target, target.title())
    goal = task_section(text, "Goal") or "- 未填写。"
    scope = task_section(text, "Scope") or "- 未填写。"
    boundary = task_section(text, "Execution Boundary") or "- 未填写。"
    acceptance = task_section(text, "Acceptance Criteria") or "- 未填写。"
    risks = task_section(text, "Risks") or "- 未填写。"
    evidence_required = task_section(text, "Evidence Required") or "- 未填写。"
    reply = sanitize_sensitive_text(f"""# {display_platform} 执行交接包

执行对象：{display_platform}
task_id：{task_id}
任务标题：{title}

你是 {display_platform} 执行端。请只在用户明确授权的工作目录内执行，并严格按本交接包回传证据。

## 目标
{goal}

## 范围
{scope}

## 执行边界
{boundary}

## 禁止事项
- 不读取、不打印、不提交 `.env`、bf token、密钥、cookie。
- 不修改未授权项目文件。
- 不改 Octo Docker 部署。
- 不改 Hermes 主体代码。
- 不接 OpenClaw。
- 不做群聊、多用户、WebSocket。
- 不声称完成，除非提供可复核证据。

## 建议检查步骤
- 先确认工作目录、当前状态和相关文件。
- 将计划与任务范围对齐，再做最小必要改动。
- 执行后收集文件、命令、测试、日志或截图证据。
- 明确未验证内容和未解决风险。

## 验收标准
{acceptance}

## 风险点
{risks}

## 回传报告格式
请按以下结构回传：

任务编号：{task_id}

执行摘要：
-

修改文件：
-

执行命令：
-

测试结果：
-

关键日志或截图：
-

未验证：
-

未解决风险：
-

回滚或恢复建议：
-

## 敏感信息处理要求
- 如果输出中出现 token、会话头、认证头、密码、api key、secret，请先脱敏。
- 不要贴完整密钥、会话凭据、令牌或 `.env` 内容。

## 用户最终验收要求
- 用户会基于回传报告执行 `/task qa {task_id}`、`/task review {task_id}` 和 `/task decide ...`。
- 你只负责执行与回传证据，不代表用户做最终验收。

## 证据要求
{evidence_required}""")
    if with_context:
        reply += "\n\n" + build_copyable_handoff_context(task_id, target, create_file=False)
    return reply


def qa_report_items(report: str, acceptance: str) -> dict:
    intake = analyze_evidence_intake(report, acceptance)
    checks = {
        "修改文件": not any(item.get("item") == "modified_files" for item in intake["missing_items"]),
        "执行命令": not any(item.get("item") == "commands" for item in intake["missing_items"]),
        "测试结果": not any(item.get("item") == "test_results" for item in intake["missing_items"]),
        "关键日志或截图": not any(item.get("item") == "key_logs_or_screenshots" for item in intake["missing_items"]),
        "未解决风险": not any(item.get("item") == "unresolved_risks" for item in intake["missing_items"]),
        "未验证说明": not any(item.get("item") == "unverified" for item in intake["missing_items"]),
        "敏感信息风险": not intake["sensitive_risk"],
        "支撑验收标准": not any(item.get("item") == "acceptance_support" for item in intake["missing_items"]),
    }
    return checks


def build_task_qa_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    sync_task_evidence_state(normalized_task_id)
    text = read_task(normalized_task_id)
    report = task_section(text, "Execution Report")
    acceptance = task_section(text, "Acceptance Criteria")
    has_report = bool(report.strip()) and "### Report at" in report
    intake = analyze_evidence_intake(report, acceptance) if has_report else analyze_evidence_intake("")
    checks = qa_report_items(report, acceptance) if has_report else {
        "修改文件": False,
        "执行命令": False,
        "测试结果": False,
        "关键日志或截图": False,
        "未解决风险": False,
        "未验证说明": False,
        "敏感信息风险": True,
        "支撑验收标准": False,
    }
    analysis = evidence_analysis(normalized_task_id, text)
    if has_report:
        intake = analysis.get("intake", intake)

    required = ["修改文件", "执行命令", "测试结果", "关键日志或截图", "未解决风险", "未验证说明", "支撑验收标准"]
    satisfied = [name for name, ok in checks.items() if ok]
    missing = [name for name in required if not checks.get(name)]
    risks = []
    if not has_report:
        risks.append("缺少 Execution Report。")
    if not checks.get("敏感信息风险"):
        risks.append("报告中存在疑似敏感信息，保存前应脱敏。")
    if missing:
        risks.append("回传证据不足，不能支撑最终验收。")
    if analysis["live_skipped"]:
        risks.append("live UI 验收跳过，不能完整封板。")

    if not has_report:
        conclusion = "needs_evidence"
        recommended = "blocked"
    elif intake.get("sensitive_risk"):
        conclusion = "needs_evidence"
        recommended = "blocked"
    elif missing or analysis["live_skipped"]:
        conclusion = "needs_evidence"
        recommended = "needs_evidence"
    else:
        conclusion = "pass_candidate"
        recommended = "pass_candidate"
    next_step = (
        f"建议继续 /task review {normalized_task_id}，再由用户按 review 结论决策。"
        if conclusion == "pass_candidate"
        else f"建议补齐缺失项后重新 /task report {normalized_task_id}，或 /evidence add {normalized_task_id} <type>。"
    )
    claimed_lines = [f"- {item}" for item in analysis["claimed"]] or ["- 无。"]
    observed_lines = [
        f"- {record['evidence_id']} | {record.get('type', '')} | verified={record.get('verified', 'no')} | {safe_preview(record.get('observed') or record.get('notes'), 140)}"
        for record in analysis["observed"]
    ] or ["- 无。"]
    chain_missing_lines = [f"- {item}" for item in analysis["missing"]] or ["- 无。"]
    collection_items = collections_for_task(normalized_task_id)
    latest_collection_id = collection_items[0]["collection_id"] if collection_items else "none"
    collection_smoke_count = sum(1 for item in collection_items if item.get("kind") == "smoke")
    collection_failed_count = sum(1 for item in collection_items if item.get("smoke_failed"))
    return f"""回传质检：{task_id}

质检结论：{conclusion}

claimed：
{chr(10).join(claimed_lines)}

observed：
{chr(10).join(observed_lines)}

missing：
{chr(10).join(chain_missing_lines)}

sensitive_risk：{str(not checks.get('敏感信息风险')).lower()}
sensitive_risk: {str(bool(intake.get('sensitive_risk'))).lower()}
sensitive_risk_reason: {intake.get('sensitive_risk_reason', 'none')}
read_only_mode: {str(bool(intake.get('read_only_mode'))).lower()}
no_modification_ok: {str(bool(intake.get('no_modification_ok'))).lower()}
evidence_type: {intake.get('evidence_type', 'report')}
collection_count: {len(collection_items)}
latest_collection_id: {latest_collection_id}
smoke_collection_count: {collection_smoke_count}
failed_collection_count: {collection_failed_count}

intake_observed_items:
{chr(10).join('- ' + item.get('name', 'unknown') + ': ' + item.get('value', '') for item in intake.get('observed_items', [])[:8]) if intake.get('observed_items') else '- none'}

intake_missing_items:
{chr(10).join('- ' + item.get('item', 'unknown') + ': ' + item.get('reason', '') for item in intake.get('missing_items', [])[:8]) if intake.get('missing_items') else '- none'}

已满足项：
{chr(10).join('- ' + item for item in satisfied) if satisfied else '- 无'}

缺失项：
{chr(10).join('- ' + item for item in missing) if missing else '- 无'}

风险项：
{chr(10).join('- ' + item for item in risks) if risks else '- 未发现明显风险'}

建议下一步：
- {next_step}

recommendation：{recommended}
recommendation: {recommended}
推荐决策：{recommended}

说明：
- /task qa 只检查报告质量和证据链缺口，不替代 /task review。
- report 不是 verified；需要 /evidence mark 才能把证据标为 verified。"""


def build_task_next_reply(task_id: str) -> str:
    text = read_task(task_id)
    status = task_metadata(text).get("status", "unknown")
    title = task_title_from_text(task_id, text)
    advice_map = {
        "open": f"建议生成交接包：/task handoff {task_id} codex 或 /task handoff {task_id} kiro。",
        "reported": f"建议先质检再审查：/task qa {task_id}，然后 /task review {task_id}。",
        "reviewed": f"建议记录用户决策：/task decide {task_id} pass|needs_evidence|blocked|cancelled <说明>。",
        "needs_evidence": f"建议补证据后重新回传：/task report {task_id}\\n<补充报告>。",
        "passed": f"建议关闭任务：/task close {task_id}。",
        "blocked": "建议说明阻塞原因，或拆分新任务处理阻塞点。",
        "cancelled": f"建议关闭或归档：/task close {task_id}。",
        "archived": "任务已关闭，无需继续处理。",
    }
    advice = advice_map.get(status, "状态未知，建议先 /task show 查看任务内容。")
    return f"""任务下一步：{task_id}
- title：{title}
- status：{status}
- 建议：{advice}
- 安全边界：Atlas/Bridge 不执行命令，不自动调用 Codex/Kiro。"""


def build_task_next_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_task(normalized_task_id)
    status = task_metadata(text).get("status", "unknown")
    latest_dispatch = latest_dispatch_for_task(normalized_task_id)
    dispatch_line = "- latest_dispatch_id: none\n- dispatch_status: none\n- target_executor: none\n- latest_exec_id: none\n- exec_status: none"
    if latest_dispatch:
        latest_exec = latest_exec_for_dispatch(latest_dispatch.get("dispatch_id", ""))
        dispatch_line = (
            f"- latest_dispatch_id: {latest_dispatch.get('dispatch_id')}\n"
            f"- dispatch_status: {latest_dispatch.get('status')}\n"
            f"- target_executor: {latest_dispatch.get('target_executor')}\n"
            f"- latest_exec_id: {(latest_exec or {}).get('exec_id', 'none')}\n"
            f"- exec_status: {(latest_exec or {}).get('status', 'none')}"
        )
    advice = build_task_next_advice(normalized_task_id, status, latest_dispatch)
    legacy_map = {
        "open": f"建议生成交接包：/task handoff {normalized_task_id} codex 或 /task handoff {normalized_task_id} kiro。",
        "reported": f"建议先质检再审查：/task qa {normalized_task_id}，然后 /task review {normalized_task_id}。",
        "reviewed": f"建议记录用户决策：/task decide {normalized_task_id} pass|needs_evidence|blocked|cancelled <说明>。",
        "needs_evidence": f"建议补证据后重新回传：/task report {normalized_task_id}\\n<补充报告>。",
        "passed": f"建议关闭任务：/task close {normalized_task_id}。",
        "blocked": "建议说明阻塞原因，或拆分新任务处理阻塞点。",
        "cancelled": f"建议关闭或归档：/task close {normalized_task_id}。",
        "archived": "任务已关闭，无需继续处理。",
    }
    legacy_advice = legacy_map.get(status, "状态未知，建议先 /task show 查看任务内容。")
    return f"""Task next step: {normalized_task_id}
- title: {task_title_from_text(normalized_task_id, text)}
- status: {status}
- status：{status}
{dispatch_line}
- recommendation: {advice}
- legacy_recommendation：{legacy_advice}
- safety: manual dispatch only; Atlas/Bridge does not run commands and does not call Codex/Kiro."""


def build_evidence_help_reply() -> str:
    return """Atlas 证据链命令
- /evidence help：查看本说明。
- /evidence add <task_id> <type>：下一行开始粘贴证据正文，只记录不执行。
- /evidence list <task_id>：列出任务证据。
- /evidence show <task_id> <evidence_id>：显示证据摘要。
- /evidence mark <task_id> <evidence_id> <verified|partial|rejected> <说明>：人工标记证据可用性。
- /evidence accept <task_id> <evidence_id> <reason>: accept reviewed observed evidence as verified; no task decision or close effect.
- /task accept-evidence <task_id> <reason>: batch accept all unverified observed evidence; skips claimed/partial/rejected/sensitive-risk evidence.
- /evidence gaps <task_id>：按验收标准和证据链输出缺口。
- /evidence intake <task_id>：预览自动证据摄取结果，只解析不写入。

type 可用：file、command、log、api、http、process、git、screenshot、ui、live、smoke、report、decision、other。
report 不是 verified；smoke 通过不等于 live 通过；Bridge 不执行证据正文里的命令。"""


def build_evidence_add_reply(task_id: str, evidence_type: str, body: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    if evidence_type.strip().lower() not in EVIDENCE_TYPES:
        return "证据类型无效。可用：file、command、log、screenshot、ui、live、smoke、report、decision、other。"
    evidence_id = create_evidence_entry(normalized_task_id, evidence_type, body, source="user", verified="no")
    analysis = evidence_analysis(normalized_task_id)
    return f"""证据已记录：{evidence_id}
- task_id：{normalized_task_id}
- type：{evidence_type.strip().lower()}
- verified：no
- evidence_gap_risk：{str(analysis['has_gaps']).lower()}
- live_skipped：{str(analysis['live_skipped']).lower()}
- 路径：workbench/evidence/{normalized_task_id}.md
- 下一步：/evidence gaps {normalized_task_id}、/evidence accept {normalized_task_id} {evidence_id} <reason> 或 /evidence mark {normalized_task_id} {evidence_id} verified <说明>"""


def build_evidence_intake_reply(task_id: str, body: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    task_text = read_task(normalized_task_id)
    intake = analyze_evidence_intake(body, task_section(task_text, "Acceptance Criteria"))
    return f"""Evidence intake preview: {normalized_task_id}

{format_intake_summary(intake)}

write_effect: none
verified_effect: none
decision_effect: none
note: observed evidence still requires manual /evidence accept or /evidence mark before Atlas review can treat it as verified."""


def build_evidence_list_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    read_task(normalized_task_id)
    records = evidence_records(normalized_task_id)
    if not records:
        return f"证据列表：{normalized_task_id}\n- 暂无证据。"
    lines = [f"证据列表：{normalized_task_id}"]
    for record in records[:30]:
        summary = safe_preview(record.get("observed") or record.get("claim") or record.get("notes"), 140)
        lines.append(
            f"- {record['evidence_id']} | type={record.get('type', '')} | source={record.get('source', '')} | verified={record.get('verified', 'no')} | {summary}"
        )
    return "\n".join(lines)


def build_evidence_show_reply(task_id: str, evidence_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    normalized_evidence_id = normalize_evidence_id(evidence_id)
    for record in evidence_records(normalized_task_id):
        if record["evidence_id"] == normalized_evidence_id:
            return f"""证据摘要：{normalized_evidence_id}
- task_id：{normalized_task_id}
- created_at：{record.get('created_at', '')}
- source：{record.get('source', '')}
- type：{record.get('type', '')}
- verified：{record.get('verified', 'no')}
- supports_acceptance：{record.get('supports_acceptance', '')}
- claim：{safe_preview(record.get('claim', ''), 260)}
- observed：{safe_preview(record.get('observed', ''), 260)}
- risk：{record.get('risk', '')}
- notes：{safe_preview(record.get('notes', ''), 320)}"""
    raise FileNotFoundError(f"evidence not found: {normalized_evidence_id}")


def build_evidence_mark_reply(task_id: str, evidence_id: str, status: str, note: str) -> str:
    record = update_evidence_mark(task_id, evidence_id, status, note)
    analysis = evidence_analysis(task_id)
    return f"""证据已标记：{record['evidence_id']}
- task_id：{normalize_task_id(task_id)}
- verified：{record.get('verified', status)}
- evidence_gap_risk：{str(analysis['has_gaps']).lower()}
- recommendation：{analysis['recommendation']}
- 下一步：/evidence gaps {normalize_task_id(task_id)} 或 /task review {normalize_task_id(task_id)}"""


def build_evidence_accept_reply(task_id: str, evidence_id: str, note: str) -> str:
    clean_note = sanitize_sensitive_text(note).strip()
    if not clean_note:
        raise ValueError("accept reason is required")
    record = update_evidence_mark(task_id, evidence_id, "verified", f"accepted by user: {clean_note}")
    analysis = evidence_analysis(task_id)
    normalized_task_id = normalize_task_id(task_id)
    return f"""evidence accepted: {record['evidence_id']}
- task_id: {normalized_task_id}
- verified: {record.get('verified', 'verified')}
- evidence_gap_risk: {str(analysis['has_gaps']).lower()}
- recommendation: {analysis['recommendation']}
- evidence_closure_state: {evidence_closure_state(analysis)}
- decision_effect: none
- auto_close_effect: none
- next: /evidence gaps {normalized_task_id} or /task review {normalized_task_id}"""


def build_evidence_gaps_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    sync_task_evidence_state(normalized_task_id)
    analysis = evidence_analysis(normalized_task_id)
    gap_recommendation = "ready_for_review" if not analysis["missing"] and not analysis["risks"] and analysis["verified"] else analysis["recommendation"]
    return f"""证据缺口：{normalized_task_id}

{build_evidence_gaps_text(normalized_task_id, analysis)}

recommendation：{gap_recommendation}
recommendation: {gap_recommendation}
evidence_gap_count：{analysis['evidence_gap_count']}
live_skipped：{str(analysis['live_skipped']).lower()}
read_only_no_modification_ok: {str(bool(analysis.get('read_only_no_modification_ok'))).lower()}
sensitive_zero_hit_ok: {str(bool(analysis.get('sensitive_zero_hit_ok'))).lower()}"""


def handle_evidence_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=4)
    if len(parts) < 2 or parts[0].lower() != "/evidence":
        return None
    subcommand = parts[1].lower()
    try:
        if subcommand == "help":
            return build_evidence_help_reply()
        if subcommand == "add":
            if len(parts) < 4:
                return "用法：/evidence add <task_id> <type>\n<证据正文>"
            inline_body = parts[4] if len(parts) > 4 else ""
            body = "\n".join(lines[1:]).strip() or inline_body
            return build_evidence_add_reply(parts[2], parts[3], body)
        if subcommand == "intake":
            if len(parts) < 3:
                return "Usage: /evidence intake <task_id>\n<pasted report>"
            inline_body = parts[3] if len(parts) > 3 else ""
            body = "\n".join(lines[1:]).strip() or inline_body
            return build_evidence_intake_reply(parts[2], body)
        if subcommand == "list":
            if len(parts) < 3:
                return "用法：/evidence list <task_id>"
            return build_evidence_list_reply(parts[2])
        if subcommand == "show":
            if len(parts) < 4:
                return "用法：/evidence show <task_id> <evidence_id>"
            return build_evidence_show_reply(parts[2], parts[3])
        if subcommand == "mark":
            mark_parts = first_line.split(maxsplit=5)
            if len(mark_parts) < 5:
                return "用法：/evidence mark <task_id> <evidence_id> <verified|partial|rejected> <说明>"
            note = mark_parts[5] if len(mark_parts) > 5 else ""
            return build_evidence_mark_reply(mark_parts[2], mark_parts[3], mark_parts[4], note)
        if subcommand == "accept":
            accept_parts = first_line.split(maxsplit=4)
            if len(accept_parts) < 4:
                return "Usage: /evidence accept <task_id> <evidence_id> <reason>"
            note = accept_parts[4] if len(accept_parts) > 4 else "\n".join(lines[1:]).strip()
            if not note.strip():
                return "Usage: /evidence accept <task_id> <evidence_id> <reason>"
            return build_evidence_accept_reply(accept_parts[2], accept_parts[3], note)
        if subcommand == "gaps":
            if len(parts) < 3:
                return "用法：/evidence gaps <task_id>"
            return build_evidence_gaps_reply(parts[2])
        return build_evidence_help_reply()
    except FileNotFoundError as exc:
        return f"证据或任务不存在：{safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"证据操作被拒绝：{safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"证据操作失败：{safe_preview(str(exc), 180)}"


def read_retro(task_id: str) -> str:
    path = retro_path(task_id)
    if not path.exists():
        raise FileNotFoundError(f"retro not found: {task_id}")
    return path.read_text(encoding="utf-8")


def write_retro(task_id: str, text: str) -> None:
    path = retro_path(task_id)
    path.write_text(sanitize_sensitive_text(text), encoding="utf-8")


def retro_title_from_text(task_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# Retro {task_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "未命名复盘"


def retro_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(RETROS_DIR.glob("OHB-*.md")):
        task_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "task_id": task_id,
                "project_id": meta.get("project_id", ""),
                "status": meta.get("status", "unknown"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "title": retro_title_from_text(task_id, text),
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at", ""), reverse=True)


def retro_exists(task_id: str) -> bool:
    return retro_path(task_id).exists()


def task_has_user_decision(text: str) -> bool:
    decision = task_section(text, "User Decision")
    return "### Decision at" in decision or "decision：" in decision or "decision:" in decision


def retro_outcome_label(task_status: str, analysis: dict) -> str:
    if task_status in {"needs_evidence", "blocked"}:
        return "阻塞/待补证据复盘"
    if task_status in {"passed", "archived"}:
        if analysis.get("has_gaps"):
            return "已通过复盘（保留证据缺口风险）"
        return "已通过复盘"
    if task_status == "cancelled":
        return "已取消复盘"
    return "过程复盘"


def summarize_retro_lessons(task_status: str, analysis: dict) -> list[str]:
    lessons = [
        "report 不是 verified，复盘必须区分 claimed、observed、verified。",
        "所有结论都应能追溯到 Execution Report、Evidence Ledger 或 User Decision。",
    ]
    if analysis.get("live_skipped"):
        lessons.append("smoke 通过不等于 live 通过，live skipped 必须保留为风险。")
    if analysis.get("has_gaps"):
        lessons.append("关闭或 pass 前仍可保留 evidence_gap_risk，不能把缺口自动改写成 verified。")
    if task_status in {"needs_evidence", "blocked"}:
        lessons.append("needs_evidence/blocked 任务只能沉淀为待补证据或阻塞经验，不能写成成功完成。")
    return lessons


def build_retro_markdown(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    sync_task_evidence_state(normalized_task_id)
    task_text = read_task(normalized_task_id)
    meta = task_metadata(task_text)
    if not task_has_user_decision(task_text):
        raise ValueError(f"task {normalized_task_id} has no user decision; suggest /task decide first")
    analysis = evidence_analysis(normalized_task_id, task_text)
    task_status = meta.get("status", "unknown")
    project_id = meta.get("project_id", "")
    title = task_title_from_text(normalized_task_id, task_text)
    now = iso_now()
    final_decision = task_section(task_text, "User Decision") or "- 未记录用户决策。"
    report = task_section(task_text, "Execution Report") or "- 未记录执行报告。"
    review = task_section(task_text, "Atlas Review") or "- 未生成 Atlas 审查。"
    lessons = summarize_retro_lessons(task_status, analysis)
    outcome = retro_outcome_label(task_status, analysis)
    verified_count = len(analysis["verified"])
    observed_count = len(analysis["observed"])
    claimed_count = len(analysis["claimed"])
    missing_count = len(analysis["missing"])
    worked = [
        "任务账本保留了工作单、回传报告、审查和用户决策。",
        "证据链已记录 evidence_id，可复盘 claimed / observed / verified 的差异。",
    ]
    if verified_count:
        worked.append(f"已有 {verified_count} 条 verified 证据可作为局部验收依据。")
    failed = []
    if analysis["live_skipped"]:
        failed.append("Octo UI live 验收跳过或待补，不能完整封板。")
    if analysis["missing"]:
        failed.append("仍存在证据缺口：" + "；".join(analysis["missing"][:5]))
    if not analysis["verified"]:
        failed.append("没有 verified 证据，不能把执行声明写成已验证完成。")
    if not failed:
        failed.append("未发现明显失败点，但仍需保留未解决风险记录。")
    candidate_improvements = [
        "建立执行端回传前的证据字段检查清单。",
        "对 live skipped 自动提示补 Octo UI 证据。",
    ]
    if project_id:
        candidate_improvements.append(f"在项目 {project_id} 的 brief 中优先显示 evidence gaps 和 retro 待办。")
    followups = []
    if analysis["live_skipped"]:
        followups.append("- 补 Octo UI live 回归证据。")
    if analysis["missing"]:
        followups.append("- 补齐缺失证据：" + "；".join(analysis["missing"][:6]))
    if task_status in {"needs_evidence", "blocked"}:
        followups.append("- 处理 needs_evidence / blocked 后再重新 review。")
    if not followups:
        followups.append("- 无强制后续任务；可按用户确认进入下一阶段。")
    return f"""# Retro {normalized_task_id} {sanitize_title(title)}

retro_id: RETRO-{normalized_task_id}
task_id: {normalized_task_id}
project_id: {project_id}
created_at: {now}
updated_at: {now}
source: atlas
mode: consultation
status: draft

## Task Summary
- task_id: {normalized_task_id}
- title: {sanitize_title(title)}
- task_status: {task_status}
- retro_type: {outcome}
- project_id: {project_id or 'unassigned'}
- report_summary: {safe_preview(report, 260)}
- review_summary: {safe_preview(review, 260)}

## Final Decision
{final_decision}

## Evidence Summary
- verified_count: {verified_count}
- observed_unverified_count: {observed_count}
- claimed_count: {claimed_count}
- missing_count: {missing_count}
- live_skipped: {str(analysis['live_skipped']).lower()}
- recommendation_at_retro: {analysis['recommendation']}

## What Worked
{chr(10).join('- ' + item for item in worked)}

## What Failed
{chr(10).join('- ' + item for item in failed)}

## Evidence Gaps
{build_evidence_gaps_text(normalized_task_id, analysis)}

## Process Issues
- live skipped / evidence gaps 必须保留，不得在 retro 中写成已验证。
- 用户可以强制 pass，但 retro 仍需记录 evidence_gap_risk。
- Retro 只沉淀经验，不自动改规则或执行后续动作。

## Lessons Learned
{chr(10).join('- ' + item for item in lessons)}

## Project Impact
- project_id: {project_id or 'unassigned'}
- impact: {outcome}
- unresolved_evidence_gap_count: {analysis['evidence_gap_count']}

## Follow-up Tasks
{chr(10).join(followups)}

## Candidate Improvements
{chr(10).join('- ' + item for item in candidate_improvements)}

## Do Not Auto-Apply
- 本复盘不会自动修改 Hermes。
- 本复盘不会自动写入 Memory。
- 本复盘不会自动修改 SkillRepo。
- 本复盘不会自动修改系统提示词。
- 本复盘不会自动修改项目代码。
- Candidate Improvements 只是候选建议，等待 OHB-LEARN-009 或用户明确授权后再处理。
"""


def build_retro_help_reply() -> str:
    return """Atlas 复盘命令
- /retro help：查看本说明。
- /retro create <task_id>：根据任务、报告、审查、决策和证据链生成复盘。
- /retro show <task_id>：显示复盘摘要。
- /retro list：列出最近 10 个复盘。
- /retro list --project <project_id>：列出项目复盘。
- /retro approve <task_id> <说明>：确认复盘，只写 workbench，不写 Memory/SkillRepo。
- /retro archive <task_id>：归档复盘状态。
- /retro project <project_id>：生成项目复盘摘要。
- /retro dashboard：生成跨项目复盘看板。

Retro 不是 Agent 自训，不会自动修改 Hermes、Memory、SkillRepo、系统提示词或项目代码。"""


def build_retro_create_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    read_task(normalized_task_id)
    if retro_exists(normalized_task_id):
        return f"复盘已存在：workbench/retros/{normalized_task_id}.md。默认不覆盖。"
    try:
        text = build_retro_markdown(normalized_task_id)
    except ValueError as exc:
        return f"暂不生成复盘：{safe_preview(str(exc), 220)}。建议先 /task decide {normalized_task_id} needs_evidence|pass <说明>。"
    write_retro(normalized_task_id, text)
    log_event("retro_created", task_id=normalized_task_id)
    meta = task_metadata(text)
    return f"""复盘已创建：{normalized_task_id}
- status：{meta.get('status', 'draft')}
- project_id：{meta.get('project_id', '') or 'unassigned'}
- 路径：workbench/retros/{normalized_task_id}.md
- 下一步：/retro show {normalized_task_id} 或 /retro approve {normalized_task_id} <说明>"""


def build_retro_show_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_retro(normalized_task_id)
    meta = task_metadata(text)
    return f"""复盘摘要：{normalized_task_id}
- project_id：{meta.get('project_id', '') or 'unassigned'}
- status：{meta.get('status', 'unknown')}
- final_decision：{safe_preview(task_section(text, 'Final Decision'), 220)}
- lessons_learned：{safe_preview(task_section(text, 'Lessons Learned'), 260)}
- evidence_gaps：{safe_preview(task_section(text, 'Evidence Gaps'), 260)}
- follow_up_tasks：{safe_preview(task_section(text, 'Follow-up Tasks'), 220)}
- candidate_improvements：{safe_preview(task_section(text, 'Candidate Improvements'), 220)}"""


def build_retro_list_reply(project_id: str = "") -> str:
    clean_project_id = validate_project_id(project_id) if project_id else ""
    records = retro_records()
    if clean_project_id:
        records = [record for record in records if record.get("project_id") == clean_project_id]
    if not records:
        return f"复盘列表：{clean_project_id or 'all'}\n- 暂无。"
    lines = [f"复盘列表：{clean_project_id or 'all'}"]
    for record in records[:10]:
        lines.append(
            f"- {record['task_id']} | project={record.get('project_id') or 'unassigned'} | {record.get('status')} | {record.get('created_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def replace_retro_status(text: str, status: str) -> str:
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", iso_now())
    return text


def project_append_lesson(project_id: str, task_id: str, retro_text: str, note: str) -> None:
    clean_project_id = validate_project_id(project_id)
    project_text = read_project(clean_project_id)
    lesson = safe_preview(task_section(retro_text, "Lessons Learned"), 320) or "未提取到 Lessons Learned。"
    now = iso_now()
    addition = f"- {now} {task_id}: {lesson} approval_note={sanitize_sensitive_text(note).strip() or '未填写。'}"
    project_text = append_to_section(project_text, "Lessons Learned", addition)
    project_text = replace_task_field(project_text, "updated_at", now)
    write_project(clean_project_id, project_text)


def build_retro_approve_reply(task_id: str, note: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_retro(normalized_task_id)
    meta = task_metadata(text)
    if meta.get("status") == "approved":
        return f"""复盘已确认：{normalized_task_id}
- status：approved
- already_approved: true
- ledger_write: none
- project_lessons：no-op，已 approved，不重复追加。
- 边界：未写 Memory，未写 SkillRepo，未修改 Hermes。"""
    text = append_to_section(text, "Timeline", f"- {iso_now()} retro approved. note={sanitize_sensitive_text(note).strip() or '未填写说明。'}")
    text = replace_retro_status(text, "approved")
    write_retro(normalized_task_id, text)
    project_id = meta.get("project_id", "")
    project_line = "- project_lessons：未关联项目。"
    if project_id:
        try:
            project_append_lesson(project_id, normalized_task_id, text, note)
            project_line = f"- project_lessons：已追加到 workbench/projects/{project_id}.md 的 Lessons Learned。"
        except Exception as exc:
            project_line = f"- project_lessons：追加失败 {safe_preview(str(exc), 120)}。"
    log_event("retro_approved", task_id=normalized_task_id)
    return f"""复盘已确认：{normalized_task_id}
- status：approved
{project_line}
- 边界：未写 Memory，未写 SkillRepo，未修改 Hermes。"""


def build_retro_archive_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_retro(normalized_task_id)
    text = append_to_section(text, "Timeline", f"- {iso_now()} retro archived.")
    text = replace_retro_status(text, "archived")
    write_retro(normalized_task_id, text)
    log_event("retro_archived", task_id=normalized_task_id)
    return f"""复盘已归档：{normalized_task_id}
- status：archived
- 文件保留：workbench/retros/{normalized_task_id}.md"""


def build_retro_project_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    read_project(clean_project_id)
    records = [record for record in retro_records() if record.get("project_id") == clean_project_id]
    lines = [f"项目复盘摘要：{clean_project_id}"]
    if not records:
        lines.append("- 暂无 retro。")
        return "\n".join(lines)
    lines.append("最近 retro：")
    for record in records[:5]:
        lines.append(f"- {record['task_id']} | {record['status']} | {record['title']}")
    gaps = []
    issues = []
    lessons = []
    followups = []
    candidates = []
    for record in records:
        text = record["text"]
        gaps.append(task_section(text, "Evidence Gaps"))
        issues.append(task_section(text, "Process Issues"))
        lessons.append(task_section(text, "Lessons Learned"))
        followups.append(task_section(text, "Follow-up Tasks"))
        candidates.append(task_section(text, "Candidate Improvements"))
    lines.extend(
        [
            "",
            "高频 evidence gaps：",
            f"- {safe_preview(' '.join(gaps), 360) or '暂无。'}",
            "",
            "高频 process issues：",
            f"- {safe_preview(' '.join(issues), 360) or '暂无。'}",
            "",
            "Lessons Learned：",
            f"- {safe_preview(' '.join(lessons), 420) or '暂无。'}",
            "",
            "Follow-up Tasks：",
            f"- {safe_preview(' '.join(followups), 360) or '暂无。'}",
            "",
            "Candidate Improvements：",
            f"- {safe_preview(' '.join(candidates), 360) or '暂无。'}",
            "",
            "当前项目治理建议：",
            "- 优先补 evidence gaps，再决定是否进入 OHB-LEARN-009；不要自动修改 Memory/SkillRepo。",
        ]
    )
    return "\n".join(lines)


def build_retro_dashboard_reply() -> str:
    projects = project_records()
    records = retro_records()
    learn_counts = learning_counts()
    learning_records_list = learning_records()
    approved = [record for record in records if record.get("status") == "approved"]
    needs = [
        record for record in records
        if "needs_evidence" in record.get("text", "") or "待补证据" in record.get("text", "")
    ]
    blocked = [record for record in records if "blocked" in record.get("text", "") or "阻塞" in record.get("text", "")]
    candidates = sum(1 for record in records if task_section(record.get("text", ""), "Candidate Improvements").strip())
    lines = [
        "Atlas 复盘看板",
        f"- retro_count：{len(records)}",
        f"- approved_retro_count：{len(approved)}",
        f"- needs_evidence_retro_count：{len(needs)}",
        f"- blocked_retro_count：{len(blocked)}",
        f"- candidate_improvement_count：{candidates}",
        f"- proposed_learning_count：{learn_counts['learning_proposals']}",
        f"- approved_learning_count：{learn_counts['learning_approved']}",
        f"- deferred_learning_count：{learn_counts['learning_deferred']}",
        f"- not_applied_learning_count：{learn_counts['learning_not_applied']}",
        "",
        "active 项目：",
    ]
    active_projects = [project for project in projects if project.get("status") == "active"]
    if not active_projects:
        lines.append("- 暂无 active 项目。")
    for project in active_projects[:15]:
        project_retros = [record for record in records if record.get("project_id") == project["project_id"]]
        project_approved = [record for record in project_retros if record.get("status") == "approved"]
        lines.append(
            f"- {project['project_id']} | retro_count={len(project_retros)} | approved={len(project_approved)} | priority={project.get('priority') or 'P?'} | {project.get('title')}"
        )
    common = " ".join(task_section(record.get("text", ""), "Process Issues") for record in records)
    lines.extend(
        [
            "",
            "常见问题摘要：",
            f"- {safe_preview(common, 420) or '暂无。'}",
            "",
            "优先改进建议：",
            "- 优先复盘 needs_evidence / blocked / live skipped 的任务。",
            "- Candidate Improvements 只记录候选，不自动应用，等待 OHB-LEARN-009。",
        ]
    )
    proposed = [record for record in learning_records_list if record.get("status") in {"proposed", "deferred"}]
    lines.append("")
    lines.append("Learning 视角：")
    if proposed:
        lines.append(f"- 建议优先 review：{proposed[0]['learn_id']} | {proposed[0]['title']}")
    else:
        lines.append("- 暂无待 review learning proposal。")
    lines.append("- application_enabled: false")
    return "\n".join(lines)


def handle_retro_command(user_text: str) -> str | None:
    first_line = user_text.strip().splitlines()[0] if user_text.strip() else ""
    parts = first_line.split(maxsplit=3)
    if len(parts) < 2 or parts[0].lower() != "/retro":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_retro_help_reply()
        if subcommand == "create":
            return build_retro_create_reply(tail)
        if subcommand == "show":
            return build_retro_show_reply(tail)
        if subcommand == "list":
            if tail == "--project" and len(parts) > 3:
                return build_retro_list_reply(parts[3])
            if tail.startswith("--project "):
                return build_retro_list_reply(tail.split(maxsplit=1)[1])
            return build_retro_list_reply()
        if subcommand == "approve":
            approve_parts = first_line.split(maxsplit=3)
            if len(approve_parts) < 3:
                return "用法：/retro approve <task_id> <说明>"
            note = approve_parts[3] if len(approve_parts) > 3 else ""
            return build_retro_approve_reply(approve_parts[2], note)
        if subcommand == "archive":
            return build_retro_archive_reply(tail)
        if subcommand == "project":
            return build_retro_project_reply(tail)
        if subcommand == "dashboard":
            return build_retro_dashboard_reply()
        return build_retro_help_reply()
    except FileNotFoundError as exc:
        return f"复盘或任务不存在：{safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"复盘操作被拒绝：{safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"复盘操作失败：{safe_preview(str(exc), 180)}"


def generate_learn_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("LEARN-%Y%m%d-%H%M%S")
    if not proposal_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not proposal_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique learn_id")


def read_proposal(learn_id: str) -> str:
    path = proposal_path(learn_id)
    if not path.exists():
        raise FileNotFoundError(f"learning proposal not found: {learn_id}")
    return path.read_text(encoding="utf-8")


def write_proposal(learn_id: str, text: str) -> None:
    proposal_path(learn_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def proposal_title_from_text(learn_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {learn_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "未命名学习提案"


def learning_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(LEARNING_PROPOSALS_DIR.glob("LEARN-*.md")):
        learn_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "learn_id": learn_id,
                "title": proposal_title_from_text(learn_id, text),
                "status": meta.get("status", "unknown"),
                "source": meta.get("source", ""),
                "source_task_id": meta.get("source_task_id", ""),
                "source_project_id": meta.get("source_project_id", ""),
                "source_retro_id": meta.get("source_retro_id", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at", ""), reverse=True)


def registry_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(LEARNING_REGISTRY_DIR.glob("LEARN-*.md")):
        learn_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "learn_id": learn_id,
                "title": proposal_title_from_text(learn_id, text),
                "status": meta.get("status", "unknown"),
                "source_task_id": meta.get("source_task_id", ""),
                "source_project_id": meta.get("source_project_id", ""),
                "approved_at": meta.get("approved_at", ""),
                "application_status": meta.get("application_status", "not_applied"),
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("approved_at", ""), reverse=True)


def learning_counts() -> dict:
    records = learning_records()
    registry = registry_records()
    applied_to_playbook = sum(1 for record in registry if record.get("application_status") == "applied_to_workbench_playbook")
    reverted_from_playbook = sum(1 for record in registry if record.get("application_status") == "reverted_from_workbench_playbook")
    return {
        "learning_proposals": len(records),
        "learning_approved": sum(1 for record in records if record.get("status") in {"approved", "packaged"}),
        "learning_rejected": sum(1 for record in records if record.get("status") == "rejected"),
        "learning_deferred": sum(1 for record in records if record.get("status") == "deferred"),
        "learning_packaged": sum(1 for record in records if record.get("status") == "packaged"),
        "learning_not_applied": sum(1 for record in registry if record.get("application_status") == "not_applied"),
        "approved_not_applied_count": sum(1 for record in registry if record.get("application_status") == "not_applied"),
        "applied_to_workbench_playbook_count": applied_to_playbook,
        "reverted_from_workbench_playbook_count": reverted_from_playbook,
        "registry_count": len(registry),
        "package_count": len(list(LEARNING_PACKAGES_DIR.glob("LEARN-*.md"))) if LEARNING_PACKAGES_DIR.exists() else 0,
        "last_learn_id": records[0]["learn_id"] if records else "",
    }


def learning_project_counts(project_id: str) -> dict:
    clean_project_id = validate_project_id(project_id)
    records = [record for record in learning_records() if record.get("source_project_id") == clean_project_id]
    registry = [record for record in registry_records() if record.get("source_project_id") == clean_project_id]
    return {
        "proposal_count": len(records),
        "approved_count": sum(1 for record in records if record.get("status") in {"approved", "packaged"}),
        "deferred_count": sum(1 for record in records if record.get("status") == "deferred"),
        "not_applied_count": sum(1 for record in registry if record.get("application_status") == "not_applied"),
    }


def candidate_lines_from_retro(retro_text: str) -> list[str]:
    candidates = []
    for section_name in ("Candidate Improvements", "Lessons Learned"):
        section = task_section(retro_text, section_name)
        for raw_line in section.splitlines():
            line = raw_line.strip()
            if line.startswith("- "):
                value = line[2:].strip()
                if value and value not in candidates:
                    candidates.append(value)
    return candidates


def retro_is_approved(retro_text: str) -> bool:
    return task_metadata(retro_text).get("status") == "approved"


def build_learning_proposal_markdown(
    learn_id: str,
    title: str,
    source: str = "manual",
    source_task_id: str = "",
    source_project_id: str = "",
    source_retro_id: str = "",
    problem: str = "",
    evidence: str = "",
    lesson: str = "",
    proposed_change: str = "",
    risks: str = "",
) -> str:
    now = iso_now()
    clean_title = sanitize_title(title)
    return f"""# {learn_id} {clean_title}

status: proposed
created_at: {now}
updated_at: {now}
source: {sanitize_title(source).lower()}
source_task_id: {source_task_id}
source_project_id: {source_project_id}
source_retro_id: {source_retro_id}
mode: consultation
owner: 小小

## Problem
{sanitize_sensitive_text(problem).strip() or '- 待补充问题描述。'}

## Evidence
{sanitize_sensitive_text(evidence).strip() or '- 待补充证据。'}

## Lesson
- {sanitize_sensitive_text(lesson).strip() or clean_title}

## Proposed Behavior Change
- {sanitize_sensitive_text(proposed_change).strip() or clean_title}

## Scope
- 仅作为 Atlas Workbench 本地 learning proposal，供后续人工审查。
- 适用项目：{source_project_id or 'unassigned'}。

## Non-Goals
- 不做模型训练。
- 不自动修改 Hermes、Memory、SkillRepo、系统提示词或项目代码。
- 不自动调用 Codex/Kiro。

## Safety Boundary
- Bridge 只写 workbench/learning。
- application_enabled: false。
- approved 只代表进入本地 registry，不代表已经应用。

## Acceptance Test
- 人工检查 proposal 有来源 retro/task/project、证据、风险、rollback plan。
- /learn review {learn_id} 输出建议决策。
- /learn approve {learn_id} 后 registry application_status 仍为 not_applied。

## Rollback Plan
- 删除或归档 workbench/learning/registry/{learn_id}.md。
- 将 proposal status 改为 deferred 或 rejected。
- 不需要回滚 Hermes、Memory、SkillRepo，因为本阶段不会修改它们。

## Risks
{sanitize_sensitive_text(risks).strip() or '- 若证据不足或范围过宽，应 defer 或 reject。'}

## Approval Record
- 尚未批准。

## Application Status
- Application Status: not_applied
- application_status: not_applied
- application_enabled: false
- applied_to_hermes: false
- applied_to_memory: false
- applied_to_skillrepo: false

## Do Not Auto-Apply
- 本提案不会自动修改 Hermes。
- 本提案不会自动写入 Memory。
- 本提案不会自动修改 SkillRepo。
- 本提案不会自动修改系统提示词。
- 本提案不会自动修改项目代码。
- Learning package 也只是人工应用包，不会自动应用。
"""


def build_learn_help_reply() -> str:
    return """Atlas 受控学习循环命令
- /learn help：查看本说明。
- /learn scan retro <task_id>：从 retro 查看候选学习项，不创建 proposal。
- /learn propose retro <task_id>：从 retro 创建 learning proposal。
- /learn propose manual <标题>：创建空白手工 proposal。
- /learn list：列出最近 10 个 proposal。
- /learn list --status <candidate|proposed|approved|rejected|deferred|packaged>：按状态列出。
- /learn show <learn_id>：查看 proposal 摘要。
- /learn review <learn_id>：审查 proposal。
- /learn approve <learn_id> <说明>：批准进入本地 registry，但不应用。
- /learn reject <learn_id> <说明>：拒绝 proposal。
- /learn defer <learn_id> <说明>：延后 proposal。
- /learn package <learn_id>：为 approved proposal 生成人工应用包，不应用。
- /learn dashboard：查看学习看板。
- /learn registry：列出 approved registry。
- /learn status：查看学习系统状态。

LEARN-009 不是模型训练，不自动改 Hermes，不写 Memory，不改 SkillRepo，只写 workbench learning registry。application_enabled: false。"""


def build_learn_scan_retro_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    retro_text = read_retro(normalized_task_id)
    meta = task_metadata(retro_text)
    candidates = candidate_lines_from_retro(retro_text)
    risk = "- retro_status 不是 approved，允许 scan，但建议 approve retro 后再 propose。" if meta.get("status") != "approved" else "- retro 已 approved。"
    lines = [
        f"Learning candidates from retro：{normalized_task_id}",
        f"- project_id：{meta.get('project_id', '') or 'unassigned'}",
        f"- retro_status：{meta.get('status', 'unknown')}",
        risk,
        "",
        "候选学习项 / Candidate Improvements：",
    ]
    lines.extend([f"- {item}" for item in candidates] or ["- 未发现 Candidate Improvements 或 Lessons Learned。"])
    lines.extend(
        [
            "",
            "来源摘要：",
            f"- Evidence Gaps：{safe_preview(task_section(retro_text, 'Evidence Gaps'), 220)}",
            f"- Process Issues：{safe_preview(task_section(retro_text, 'Process Issues'), 220)}",
        ]
    )
    return "\n".join(lines)


def build_learn_propose_retro_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    retro_text = read_retro(normalized_task_id)
    meta = task_metadata(retro_text)
    candidates = candidate_lines_from_retro(retro_text)
    if not candidates:
        return f"未创建 proposal：retro {normalized_task_id} 中没有 Candidate Improvements 或 Lessons Learned。"
    learn_ids = []
    source_project_id = meta.get("project_id", "")
    approved_note = "retro 已 approved。" if meta.get("status") == "approved" else "retro 未 approved，review 时通常建议 defer。"
    for candidate in candidates[:3]:
        learn_id = generate_learn_id()
        problem = task_section(retro_text, "Process Issues") or task_section(retro_text, "Evidence Gaps")
        evidence = "\n".join(
            [
                f"- source_retro: workbench/retros/{normalized_task_id}.md",
                f"- retro_status: {meta.get('status', 'unknown')}",
                f"- {approved_note}",
                safe_preview(task_section(retro_text, "Evidence Summary"), 420),
            ]
        )
        proposal = build_learning_proposal_markdown(
            learn_id,
            candidate,
            source="retro",
            source_task_id=normalized_task_id,
            source_project_id=source_project_id,
            source_retro_id=f"RETRO-{normalized_task_id}",
            problem=problem,
            evidence=evidence,
            lesson=candidate,
            proposed_change=candidate,
            risks=task_section(retro_text, "Evidence Gaps") or "- 需要人工 review 证据是否足够。",
        )
        write_proposal(learn_id, proposal)
        learn_ids.append(learn_id)
        log_event("learn_proposed", learn_id=learn_id, source_task_id=normalized_task_id)
    return f"""Learning proposal 已创建：
{chr(10).join('- ' + learn_id for learn_id in learn_ids)}

- source_task_id：{normalized_task_id}
- 不自动 approve。
- 不自动应用。
- 下一步：/learn review {learn_ids[0]}"""


def build_learn_propose_manual_reply(title: str) -> str:
    learn_id = generate_learn_id()
    proposal = build_learning_proposal_markdown(
        learn_id,
        title,
        source="manual",
        problem="- 手工 proposal，待用户补充。",
        evidence="- 手工 proposal，待用户补充 evidence。",
        lesson="- 待填写 lesson。",
        proposed_change="- 待填写 proposed behavior change。",
        risks="- 手工 proposal 尚未绑定 retro/evidence，review 应谨慎。",
    )
    write_proposal(learn_id, proposal)
    log_event("learn_manual_proposed", learn_id=learn_id)
    return f"""手工 learning proposal 已创建：{learn_id}
- 路径：workbench/learning/proposals/{learn_id}.md
- status：proposed
- application_status：not_applied
- 下一步：手工补充后 /learn review {learn_id}"""


def build_learn_list_reply(status_filter: str = "") -> str:
    records = learning_records()
    if status_filter:
        normalized = status_filter.strip().lower()
        if normalized not in LEARNING_STATUSES:
            return "status 无效。可用：candidate、proposed、approved、rejected、deferred、packaged。"
        records = [record for record in records if record.get("status") == normalized]
    if not records:
        return f"Learning proposals：{status_filter or 'all'}\n- 暂无。"
    lines = [f"Learning proposals：{status_filter or 'all'}"]
    for record in records[:10]:
        lines.append(
            f"- {record['learn_id']} | {record['status']} | task={record.get('source_task_id') or 'none'} | project={record.get('source_project_id') or 'unassigned'} | {record['updated_at']} | {record['title']}"
        )
    return "\n".join(lines)


def build_learn_show_reply(learn_id: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    meta = task_metadata(text)
    return f"""Learning proposal：{normalized_learn_id}
- title：{proposal_title_from_text(normalized_learn_id, text)}
- status：{meta.get('status', 'unknown')}
- source：{meta.get('source', '')}
- source_task_id：{meta.get('source_task_id', '')}
- source_project_id：{meta.get('source_project_id', '') or 'unassigned'}
- problem：{safe_preview(task_section(text, 'Problem'), 220)}
- lesson：{safe_preview(task_section(text, 'Lesson'), 220)}
- proposed_behavior_change：{safe_preview(task_section(text, 'Proposed Behavior Change'), 220)}
- scope：{safe_preview(task_section(text, 'Scope'), 180)}
- risks：{safe_preview(task_section(text, 'Risks'), 220)}
- acceptance_test：{safe_preview(task_section(text, 'Acceptance Test'), 220)}
- application_status：{safe_preview(task_section(text, 'Application Status'), 180)}"""


def proposal_has_auto_apply_intent(text: str) -> bool:
    review_surface = "\n".join(
        [
            task_section(text, "Proposed Behavior Change"),
            task_section(text, "Scope"),
            task_section(text, "Acceptance Test"),
        ]
    ).lower()
    risky_phrases = [
        "\u81ea\u52a8\u4fee\u6539 hermes",
        "\u81ea\u52a8\u5199\u5165 memory",
        "\u81ea\u52a8\u4fee\u6539 skillrepo",
        "\u81ea\u52a8\u6539\u7cfb\u7edf\u63d0\u793a\u8bcd",
        "\u81ea\u52a8\u5e94\u7528",
        "modify hermes automatically",
        "write memory automatically",
    ]
    return any(phrase in review_surface for phrase in risky_phrases)


def build_learn_review_reply(learn_id: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    meta = task_metadata(text)
    source_task_id = meta.get("source_task_id", "")
    retro_status = "none"
    live_or_gap = False
    evidence_enough = bool(task_section(text, "Evidence").strip()) and "待补充" not in task_section(text, "Evidence")
    if source_task_id:
        try:
            retro_text = read_retro(source_task_id)
            retro_status = task_metadata(retro_text).get("status", "unknown")
            retro_gap_text = task_section(retro_text, "Evidence Gaps")
            live_or_gap = "live_skipped" in retro_text or "live skipped" in retro_text.lower() or "missing" in retro_gap_text
        except Exception:
            retro_status = "missing"
            evidence_enough = False
    auto_apply = proposal_has_auto_apply_intent(text)
    scope_text = task_section(text, "Scope")
    scope_too_wide = any(marker in scope_text for marker in ("所有", "全局", "全部", "all projects", "global"))
    has_acceptance = bool(task_section(text, "Acceptance Test").strip())
    has_rollback = bool(task_section(text, "Rollback Plan").strip())
    decision = "approve"
    reasons = []
    if auto_apply:
        decision = "reject"
        reasons.append("proposal 试图自动改 Hermes/Memory/SkillRepo 或自动应用。")
    if retro_status not in {"approved", "none"}:
        decision = "defer" if decision != "reject" else decision
        reasons.append("source retro 未 approved。")
    if not evidence_enough:
        decision = "defer" if decision != "reject" else decision
        reasons.append("证据不足。")
    if live_or_gap:
        decision = "defer" if decision != "reject" else decision
        reasons.append("来源存在 live skipped / evidence gaps。")
    if scope_too_wide:
        decision = "defer" if decision != "reject" else decision
        reasons.append("适用范围过宽，建议拆小。")
    if not has_acceptance:
        decision = "defer" if decision != "reject" else decision
        reasons.append("缺 Acceptance Test。")
    if not has_rollback:
        decision = "defer" if decision != "reject" else decision
        reasons.append("缺 Rollback Plan。")
    if not reasons:
        reasons.append("只登记到 workbench registry，application_status 保持 not_applied，证据和回滚信息可审查。")
    return f"""Learning proposal 审查：{normalized_learn_id}

证据是否足够：
- {str(evidence_enough).lower()}

是否来自 approved retro：
- retro_status：{retro_status}

live skipped / evidence gaps：
- {str(live_or_gap).lower()}

越权修改 Hermes / Memory / SkillRepo：
- {str(auto_apply).lower()}

范围是否过宽：
- {str(scope_too_wide).lower()}

Acceptance Test：
- {str(has_acceptance).lower()}

Rollback Plan：
- {str(has_rollback).lower()}

审查理由：
{chr(10).join('- ' + item for item in reasons)}

建议决策：{decision}

安全边界：
- application_enabled: false
- approve 只写本地 registry，不应用。"""


def replace_proposal_status(text: str, status: str) -> str:
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", iso_now())
    return text


def build_registry_markdown(learn_id: str, proposal_text: str, approval_note: str) -> str:
    meta = task_metadata(proposal_text)
    title = proposal_title_from_text(learn_id, proposal_text)
    approved_at = iso_now()
    rollback = safe_preview(task_section(proposal_text, "Rollback Plan"), 600)
    acceptance = safe_preview(task_section(proposal_text, "Acceptance Test"), 600)
    return f"""# {learn_id} {title}

status: approved
learn_id: {learn_id}
title: {sanitize_title(title)}
source_task_id: {meta.get('source_task_id', '')}
source_project_id: {meta.get('source_project_id', '')}
approved_at: {approved_at}
approval_note: {sanitize_sensitive_text(approval_note).strip() or '未填写说明。'}
application_status: not_applied

## Registry Entry
- learn_id: {learn_id}
- title: {sanitize_title(title)}
- status: approved
- source_task_id: {meta.get('source_task_id', '')}
- source_project_id: {meta.get('source_project_id', '') or 'unassigned'}
- approved_at: {approved_at}
- approval_note: {sanitize_sensitive_text(approval_note).strip() or '未填写说明。'}
- application_status: not_applied

## Rollback Plan
{rollback}

## Acceptance Test
{acceptance}

## Application Status
- application_status: not_applied
- application_enabled: false
- not_applied_to_hermes: true
- not_written_to_memory: true
- not_written_to_skillrepo: true
"""


def rebuild_registry_index() -> None:
    records = registry_records()
    lines = ["# Learning Registry Index", "", "application_enabled: false", ""]
    for record in records:
        lines.append(
            f"- {record['learn_id']} | project={record.get('source_project_id') or 'unassigned'} | application_status={record.get('application_status')} | {record.get('title')}"
        )
    (LEARNING_REGISTRY_DIR / "index.md").write_text(sanitize_sensitive_text("\n".join(lines) + "\n"), encoding="utf-8")


def build_learn_approve_reply(learn_id: str, note: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    now = iso_now()
    approval = f"### Approved at {now}\n- note: {sanitize_sensitive_text(note).strip() or '未填写说明。'}\n- application_status: not_applied\n- application_enabled: false"
    text = append_to_section(text, "Approval Record", approval)
    text = replace_proposal_status(text, "approved")
    write_proposal(normalized_learn_id, text)
    registry_text = build_registry_markdown(normalized_learn_id, text, note)
    registry_path(normalized_learn_id).write_text(sanitize_sensitive_text(registry_text), encoding="utf-8")
    rebuild_registry_index()
    log_event("learn_approved", learn_id=normalized_learn_id)
    return f"""Learning proposal 已批准但未应用：{normalized_learn_id}
- status：approved
- registry：workbench/learning/registry/{normalized_learn_id}.md
- application_status：not_applied
- application_enabled：false
- 未写 Memory，未写 SkillRepo，未改 Hermes。"""


def move_learning_copy(learn_id: str, target_dir: Path, text: str) -> None:
    ensure_workbench_dirs()
    ensure_inside_workbench(target_dir / f"{learn_id}.md").write_text(sanitize_sensitive_text(text), encoding="utf-8")


def build_learn_reject_reply(learn_id: str, note: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    text = append_to_section(text, "Approval Record", f"### Rejected at {iso_now()}\n- note: {sanitize_sensitive_text(note).strip() or '未填写说明。'}")
    text = replace_proposal_status(text, "rejected")
    write_proposal(normalized_learn_id, text)
    move_learning_copy(normalized_learn_id, LEARNING_REJECTED_DIR, text)
    log_event("learn_rejected", learn_id=normalized_learn_id)
    return f"""Learning proposal 已拒绝：{normalized_learn_id}
- status：rejected
- rejected_copy：workbench/learning/rejected/{normalized_learn_id}.md
- 原始证据保留。"""


def build_learn_defer_reply(learn_id: str, note: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    text = append_to_section(text, "Approval Record", f"### Deferred at {iso_now()}\n- reason: {sanitize_sensitive_text(note).strip() or '未填写说明。'}")
    text = replace_proposal_status(text, "deferred")
    write_proposal(normalized_learn_id, text)
    move_learning_copy(normalized_learn_id, LEARNING_DEFERRED_DIR, text)
    log_event("learn_deferred", learn_id=normalized_learn_id)
    return f"""Learning proposal 已延后：{normalized_learn_id}
- status：deferred
- deferred_copy：workbench/learning/deferred/{normalized_learn_id}.md
- 未应用。"""


def build_learn_package_reply(learn_id: str) -> str:
    normalized_learn_id = normalize_learn_id(learn_id)
    text = read_proposal(normalized_learn_id)
    meta = task_metadata(text)
    if meta.get("status") not in {"approved", "packaged"}:
        return f"不能生成 package：{normalized_learn_id} 当前 status={meta.get('status', 'unknown')}。只有 approved proposal 可 package。"
    title = proposal_title_from_text(normalized_learn_id, text)
    package = f"""# Learning Package {normalized_learn_id} {title}

learn_id: {normalized_learn_id}
source_task_id: {meta.get('source_task_id', '')}
source_project_id: {meta.get('source_project_id', '')}
application_status: not_applied
application_enabled: false

## Lesson
{task_section(text, 'Lesson')}

## Proposed Behavior
{task_section(text, 'Proposed Behavior Change')}

## Acceptance Test
{task_section(text, 'Acceptance Test')}

## Rollback Plan
{task_section(text, 'Rollback Plan')}

## Suggested Manual Application Steps
- 人工阅读本 package。
- 在后续明确授权的 OHB-APPLY 阶段决定是否应用。
- 应用前再次核对 evidence、approval、rollback、acceptance test。

## Explicit Warning
- not applied automatically。
- 不修改 Hermes。
- 不写 Memory。
- 不改 SkillRepo。
- 不改系统提示词。
- 不改项目代码。
"""
    package_path(normalized_learn_id).write_text(sanitize_sensitive_text(package), encoding="utf-8")
    text = append_to_section(text, "Application Status", f"- package_created_at: {iso_now()}\n- package_path: workbench/learning/packages/{normalized_learn_id}.md\n- application_status: not_applied")
    text = replace_proposal_status(text, "packaged")
    write_proposal(normalized_learn_id, text)
    log_event("learn_packaged", learn_id=normalized_learn_id)
    return f"""Learning package 已生成：{normalized_learn_id}
- package：workbench/learning/packages/{normalized_learn_id}.md
- status：packaged
- application_status：not_applied
- explicit warning：not applied automatically"""


def build_learn_dashboard_reply() -> str:
    records = learning_records()
    counts = learning_counts()
    apply_view = apply_counts()
    project_distribution: dict[str, int] = {}
    high_risk = []
    for record in records:
        project_distribution[record.get("source_project_id") or "unassigned"] = project_distribution.get(record.get("source_project_id") or "unassigned", 0) + 1
        text = record.get("text", "")
        if "live skipped" in text.lower() or "evidence gaps" in text.lower() or "未 approved" in text:
            high_risk.append(record)
    lines = [
        "Atlas Learning Dashboard",
        f"- proposals：{counts['learning_proposals']}",
        f"- approved：{counts['learning_approved']}",
        f"- rejected：{counts['learning_rejected']}",
        f"- deferred：{counts['learning_deferred']}",
        f"- packaged：{counts['learning_packaged']}",
        f"- not_applied：{counts['learning_not_applied']}",
        f"- approved_not_applied: {counts['approved_not_applied_count']}",
        f"- applied_to_playbook: {counts['applied_to_workbench_playbook_count']}",
        f"- reverted_from_playbook: {counts['reverted_from_workbench_playbook_count']}",
        f"- pending_apply_plans: {apply_view['planned_count']}",
        f"- application_enabled：{str(APPLICATION_ENABLED).lower()}",
        f"- runtime_injection_enabled: {str(RUNTIME_INJECTION_ENABLED).lower()}",
        "",
        "来源项目分布：",
    ]
    lines.extend([f"- {project}: {count}" for project, count in sorted(project_distribution.items())] or ["- 暂无。"])
    lines.append("")
    lines.append("高风险 proposal：")
    lines.extend([f"- {record['learn_id']} | {record['status']} | {record['title']}" for record in high_risk[:10]] or ["- 暂无。"])
    lines.extend(
        [
            "",
            "Apply view:",
            f"- approved_not_applied: {counts['approved_not_applied_count']}",
            f"- applied_to_playbook: {counts['applied_to_workbench_playbook_count']}",
            f"- reverted_from_playbook: {counts['reverted_from_workbench_playbook_count']}",
            f"- pending_apply_plans: {apply_view['planned_count']}",
            "",
            "今日建议：",
            "- 优先 /learn review proposed/deferred 中证据最完整、范围最小的 proposal。",
            "- Approved proposals can move to /apply plan <learn_id> global|project <project_id> for Workbench-only playbook use.",
        ]
    )
    return "\n".join(lines)


def build_learn_registry_reply() -> str:
    records = registry_records()
    if not records:
        return "Learning registry：暂无 approved learning。"
    lines = ["Learning registry："]
    for record in records[:30]:
        lines.append(
            f"- {record['learn_id']} | {record.get('title')} | project={record.get('source_project_id') or 'unassigned'} | application_status={record.get('application_status')} | approved_at={record.get('approved_at')}"
        )
    return "\n".join(lines)


def build_learn_status_reply() -> str:
    counts = learning_counts()
    return f"""Learning system status
- learning_dir：workbench/learning/
- proposal_count：{counts['learning_proposals']}
- registry_count：{counts['registry_count']}
- approved_not_applied_count: {counts['approved_not_applied_count']}
- applied_to_workbench_playbook_count: {counts['applied_to_workbench_playbook_count']}
- reverted_from_workbench_playbook_count: {counts['reverted_from_workbench_playbook_count']}
- package_count：{counts['package_count']}
- last_learn_id：{counts['last_learn_id'] or 'none'}
- application_enabled: false
- runtime_injection_enabled: false
- external_application_enabled: false
- safety_boundary：只写 workbench learning registry/packages；不改 Hermes、Memory、SkillRepo、系统提示词或项目代码。"""


def handle_learn_command(user_text: str) -> str | None:
    first_line = user_text.strip().splitlines()[0] if user_text.strip() else ""
    parts = first_line.split(maxsplit=4)
    if len(parts) < 2 or parts[0].lower() != "/learn":
        return None
    subcommand = parts[1].lower()
    try:
        if subcommand == "help":
            return build_learn_help_reply()
        if subcommand == "scan":
            if len(parts) < 4 or parts[2].lower() != "retro":
                return "用法：/learn scan retro <task_id>"
            return build_learn_scan_retro_reply(parts[3])
        if subcommand == "propose":
            if len(parts) < 4:
                return "用法：/learn propose retro <task_id> 或 /learn propose manual <标题>"
            kind = parts[2].lower()
            tail = parts[3] if len(parts) > 3 else ""
            if kind == "retro":
                return build_learn_propose_retro_reply(tail)
            if kind == "manual":
                manual_title = tail if len(parts) == 4 else f"{tail} {parts[4]}"
                return build_learn_propose_manual_reply(manual_title)
            return "用法：/learn propose retro <task_id> 或 /learn propose manual <标题>"
        if subcommand == "list":
            if len(parts) >= 4 and parts[2] == "--status":
                return build_learn_list_reply(parts[3])
            return build_learn_list_reply()
        if subcommand == "show":
            return build_learn_show_reply(parts[2] if len(parts) > 2 else "")
        if subcommand == "review":
            return build_learn_review_reply(parts[2] if len(parts) > 2 else "")
        if subcommand == "approve":
            approve_parts = first_line.split(maxsplit=3)
            if len(approve_parts) < 3:
                return "用法：/learn approve <learn_id> <说明>"
            return build_learn_approve_reply(approve_parts[2], approve_parts[3] if len(approve_parts) > 3 else "")
        if subcommand == "reject":
            reject_parts = first_line.split(maxsplit=3)
            if len(reject_parts) < 3:
                return "用法：/learn reject <learn_id> <说明>"
            return build_learn_reject_reply(reject_parts[2], reject_parts[3] if len(reject_parts) > 3 else "")
        if subcommand == "defer":
            defer_parts = first_line.split(maxsplit=3)
            if len(defer_parts) < 3:
                return "用法：/learn defer <learn_id> <说明>"
            return build_learn_defer_reply(defer_parts[2], defer_parts[3] if len(defer_parts) > 3 else "")
        if subcommand == "package":
            return build_learn_package_reply(parts[2] if len(parts) > 2 else "")
        if subcommand == "dashboard":
            return build_learn_dashboard_reply()
        if subcommand == "registry":
            return build_learn_registry_reply()
        if subcommand == "status":
            return build_learn_status_reply()
        return build_learn_help_reply()
    except FileNotFoundError as exc:
        return f"学习提案或来源不存在：{safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"学习操作被拒绝：{safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"学习操作失败：{safe_preview(str(exc), 180)}"


def generate_apply_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("APPLY-%Y%m%d-%H%M%S")
    if not application_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not application_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique apply_id")


def read_application(apply_id: str) -> str:
    path = application_path(apply_id)
    if not path.exists():
        raise FileNotFoundError(f"apply plan not found: {apply_id}")
    return path.read_text(encoding="utf-8")


def write_application(apply_id: str, text: str) -> None:
    application_path(apply_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def application_title_from_text(apply_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {apply_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return task_metadata(text).get("title", "untitled apply plan")


def application_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(APPLICATIONS_DIR.glob("APPLY-*.md")):
        apply_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "apply_id": apply_id,
                "title": application_title_from_text(apply_id, text),
                "status": meta.get("status", "unknown"),
                "source_learn_id": meta.get("source_learn_id", ""),
                "source_project_id": meta.get("source_project_id", ""),
                "source_task_id": meta.get("source_task_id", ""),
                "target": meta.get("target", ""),
                "target_path": meta.get("target_path", ""),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at", ""), reverse=True)


def playbook_entry_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return len(re.findall(r"^## LEARN-\d{8}-\d{6}(?:-\d{2})?\b", text, re.MULTILINE))


def playbook_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    global_path = global_playbook_path()
    if global_path.exists():
        records.append(
            {
                "name": "global",
                "path": global_path,
                "entry_count": playbook_entry_count(global_path),
                "updated_at": datetime.fromtimestamp(global_path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    for path in sorted(PROJECT_PLAYBOOKS_DIR.glob("*.md")):
        records.append(
            {
                "name": f"projects/{path.stem}",
                "path": path,
                "entry_count": playbook_entry_count(path),
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    return records


def apply_counts() -> dict:
    records = application_records()
    registry = registry_records()
    global_entries = playbook_entry_count(global_playbook_path())
    project_entries = sum(playbook_entry_count(path) for path in PROJECT_PLAYBOOKS_DIR.glob("*.md")) if PROJECT_PLAYBOOKS_DIR.exists() else 0
    return {
        "apply_plans": len(records),
        "planned_count": sum(1 for record in records if record.get("status") == "planned"),
        "applied_count": sum(1 for record in records if record.get("status") == "applied"),
        "reverted_count": sum(1 for record in records if record.get("status") == "reverted"),
        "cancelled_count": sum(1 for record in records if record.get("status") == "cancelled"),
        "global_playbook_entries": global_entries,
        "project_playbook_entries": project_entries,
        "playbook_entries": global_entries + project_entries,
        "applied_to_workbench_playbook": sum(1 for record in registry if record.get("application_status") == "applied_to_workbench_playbook"),
        "reverted_from_workbench_playbook": sum(1 for record in registry if record.get("application_status") == "reverted_from_workbench_playbook"),
    }


def project_apply_counts(project_id: str) -> dict:
    clean_project_id = validate_project_id(project_id)
    records = [
        record for record in application_records()
        if record.get("source_project_id") == clean_project_id or record.get("target_path", "").endswith(f"/projects/{clean_project_id}.md")
    ]
    registry = [record for record in registry_records() if record.get("source_project_id") == clean_project_id]
    return {
        "playbook_entry_count": playbook_entry_count(project_playbook_path(clean_project_id)),
        "applied_learning_count": sum(1 for record in registry if record.get("application_status") == "applied_to_workbench_playbook"),
        "reverted_learning_count": sum(1 for record in registry if record.get("application_status") == "reverted_from_workbench_playbook"),
        "pending_apply_count": sum(1 for record in records if record.get("status") == "planned"),
        "suggested_not_applied_learnings": sum(1 for record in registry if record.get("application_status") == "not_applied"),
    }


def playbook_display_path(path: Path) -> str:
    safe_path = ensure_inside_playbooks(path)
    try:
        relative = safe_path.relative_to(WORKBENCH_DIR.resolve())
        return f"workbench/{relative.as_posix()}"
    except ValueError:
        return display_path(safe_path).replace("\\", "/")


def playbook_path_from_display(value: str) -> Path:
    normalized = str(value or "").replace("\\", "/").strip()
    prefix = "workbench/playbooks/"
    if not normalized.startswith(prefix):
        raise ValueError("target_path must be inside workbench/playbooks")
    suffix = normalized[len(prefix):]
    suffix_parts = Path(suffix).parts
    if not suffix or suffix.startswith("/") or ".." in suffix_parts:
        raise ValueError("invalid playbook target_path")
    return ensure_inside_playbooks(PLAYBOOKS_DIR / Path(suffix))


def approved_learning_text(learn_id: str) -> tuple[str, str]:
    normalized_learn_id = normalize_learn_id(learn_id)
    proposal = read_proposal(normalized_learn_id)
    proposal_status = task_metadata(proposal).get("status", "unknown")
    if proposal_status not in {"approved", "packaged"}:
        raise ValueError(f"learning proposal is not approved: {normalized_learn_id} status={proposal_status}")
    registry_file = registry_path(normalized_learn_id)
    if not registry_file.exists():
        raise ValueError(f"learning registry entry missing for approved proposal: {normalized_learn_id}")
    return proposal, registry_file.read_text(encoding="utf-8")


def build_playbook_entry(
    learn_id: str,
    title: str,
    proposal_text: str,
    registry_text: str,
    apply_id: str,
    applied_at: str = "pending",
) -> str:
    proposal_meta = task_metadata(proposal_text)
    registry_meta = task_metadata(registry_text)
    source_project_id = registry_meta.get("source_project_id") or proposal_meta.get("source_project_id") or "unassigned"
    lesson = task_section(proposal_text, "Lesson") or task_section(registry_text, "Registry Entry")
    behavior = task_section(proposal_text, "Proposed Behavior Change") or title
    acceptance = task_section(proposal_text, "Acceptance Test") or "- Human checks this playbook entry before using it."
    rollback = task_section(proposal_text, "Rollback Plan") or "- Append a Revert Note and set registry application_status to reverted_from_workbench_playbook."
    evidence = task_section(proposal_text, "Evidence") or "- Source evidence remains in the learning proposal and registry."
    safety = task_section(proposal_text, "Safety Boundary") or "- Workbench reference only. No runtime injection."
    return sanitize_sensitive_text(f"""## {learn_id} {sanitize_title(title)}

applied_at: {applied_at}
apply_id: {apply_id}
source_learn_id: {learn_id}
source_project_id: {source_project_id}
application_status: applied_to_workbench_playbook
runtime_injection_enabled: false

### Lesson
{lesson}

### Behavior Guideline
{behavior}

### When To Use
- Use as a human-readable Atlas Workbench reference when a similar task pattern appears.

### When Not To Use
- Do not use when the source evidence does not match the new task.
- Do not treat this entry as a runtime rule, system prompt, Memory item, or SkillRepo patch.

### Acceptance Check
{acceptance}

### Rollback Note
{rollback}

### Source Evidence
{evidence}

### Safety Boundary
{safety}

### Not Applied To
- Hermes config
- Hermes Memory
- SkillRepo
- System prompt
- Project code
""")


def build_apply_plan_markdown(
    apply_id: str,
    learn_id: str,
    target: str,
    target_path: Path,
    proposal_text: str,
    registry_text: str,
) -> str:
    now = iso_now()
    title = proposal_title_from_text(learn_id, proposal_text)
    proposal_meta = task_metadata(proposal_text)
    registry_meta = task_metadata(registry_text)
    source_project_id = registry_meta.get("source_project_id") or proposal_meta.get("source_project_id") or ""
    source_task_id = registry_meta.get("source_task_id") or proposal_meta.get("source_task_id") or ""
    entry = build_playbook_entry(learn_id, title, proposal_text, registry_text, apply_id)
    return sanitize_sensitive_text(f"""# {apply_id} {sanitize_title(title)}

status: planned
created_at: {now}
updated_at: {now}
source_learn_id: {learn_id}
source_project_id: {source_project_id}
source_task_id: {source_task_id}
target: {target}
target_path: {playbook_display_path(target_path)}
mode: consultation
owner: XiaoXiao

## Source Learning
- learn_id: {learn_id}
- proposal_path: workbench/learning/proposals/{learn_id}.md
- registry_path: workbench/learning/registry/{learn_id}.md
- application_status: {registry_meta.get('application_status', 'not_applied')}

## Proposed Playbook Entry
{entry}

## Scope
- Workbench-only apply.
- Write only the target playbook under workbench/playbooks.

## Non-Goals
- Do not modify Hermes.
- Do not write Memory.
- Do not modify SkillRepo.
- Do not modify system prompts.
- Do not modify project code.
- Do not call Codex/Kiro.
- Do not execute commands.
- Do not perform model training.

## Safety Boundary
- target_path must stay under workbench/playbooks.
- runtime_injection_enabled: false
- external_application_enabled: false

## Acceptance Test
- /apply show {apply_id} displays status, source_learn_id, target_path, rollback plan, and runtime impact.
- /apply enact {apply_id} appends the proposed entry to the selected playbook only.
- /learn registry shows application_status=applied_to_workbench_playbook after enact.

## Rollback Plan
- /apply revert {apply_id} appends a Revert Note to the playbook.
- Registry application_status becomes reverted_from_workbench_playbook.
- Original playbook history is retained for audit.

## Apply Record
- not enacted.

## Revert Record
- not reverted.

## Runtime Impact
- none
- runtime_injection_enabled: false
- external_application_enabled: false

## Do Not Auto-Apply Beyond Workbench
- This apply plan only writes workbench/playbooks after explicit /apply enact.
- It does not modify Hermes, Memory, SkillRepo, system prompts, or project code.
""")


def build_apply_help_reply() -> str:
    return """Atlas Workbench apply commands
- /apply help
- /apply plan <learn_id> global
- /apply plan <learn_id> project <project_id>
- /apply show <apply_id>
- /apply list
- /apply list --status <planned|applied|reverted|cancelled>
- /apply enact <apply_id> <note>
- /apply revert <apply_id> <note>
- /apply cancel <apply_id> <note>
- /apply dashboard

Boundary: apply means workbench/playbooks only. It does not modify Hermes, Memory, SkillRepo, system prompts, project code, or runtime prompts. It does not execute commands. runtime_injection_enabled: false."""


def build_apply_plan_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if len(parts) < 2:
        return "Usage: /apply plan <learn_id> global OR /apply plan <learn_id> project <project_id>"
    learn_id = normalize_learn_id(parts[0])
    kind = parts[1].lower()
    proposal_text, registry_text = approved_learning_text(learn_id)
    if kind == "global":
        target = "global_playbook"
        target_path = global_playbook_path()
    elif kind == "project":
        if len(parts) < 3:
            return "Usage: /apply plan <learn_id> project <project_id>"
        project_id = validate_project_id(parts[2])
        read_project(project_id)
        target = "project_playbook"
        target_path = project_playbook_path(project_id)
    else:
        return "Usage: /apply plan <learn_id> global OR /apply plan <learn_id> project <project_id>"
    apply_id = generate_apply_id()
    plan = build_apply_plan_markdown(apply_id, learn_id, target, target_path, proposal_text, registry_text)
    write_application(apply_id, plan)
    log_event("apply_planned", apply_id=apply_id, learn_id=learn_id, target=target)
    return f"""Apply plan created: {apply_id}
- status: planned
- source_learn_id: {learn_id}
- target: {target}
- target_path: {playbook_display_path(target_path)}
- path: workbench/applications/{apply_id}.md
- next: /apply show {apply_id}
- not enacted; playbook was not written."""


def build_apply_show_reply(apply_id: str) -> str:
    normalized_apply_id = normalize_apply_id(apply_id)
    text = read_application(normalized_apply_id)
    meta = task_metadata(text)
    return f"""Apply plan: {normalized_apply_id}
- title: {application_title_from_text(normalized_apply_id, text)}
- status: {meta.get('status', 'unknown')}
- source_learn_id: {meta.get('source_learn_id', '')}
- target: {meta.get('target', '')}
- target_path: {meta.get('target_path', '')}
- proposed_playbook_entry: {safe_preview(apply_plan_proposed_entry(text), 420)}
- safety_boundary: {safe_preview(task_section(text, 'Safety Boundary'), 240)}
- rollback_plan: {safe_preview(task_section(text, 'Rollback Plan'), 240)}
- runtime_impact: {safe_preview(task_section(text, 'Runtime Impact'), 180)}"""


def build_apply_list_reply(status_filter: str = "") -> str:
    records = application_records()
    if status_filter:
        normalized = status_filter.strip().lower()
        if normalized not in APPLY_STATUSES:
            return "invalid status. Use planned, applied, reverted, or cancelled."
        records = [record for record in records if record.get("status") == normalized]
    if not records:
        return f"Apply plans: {status_filter or 'all'}\n- none."
    lines = [f"Apply plans: {status_filter or 'all'}"]
    for record in records[:10]:
        lines.append(
            f"- {record['apply_id']} | {record['status']} | learn={record.get('source_learn_id') or 'none'} | target={record.get('target')} | {record.get('updated_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def apply_plan_proposed_entry(text: str) -> str:
    heading = "## Proposed Playbook Entry"
    start = text.find(heading)
    if start < 0:
        return ""
    body_start = start + len(heading)
    end_marker = "\n## Scope"
    end = text.find(end_marker, body_start)
    if end < 0:
        end = len(text)
    return text[body_start:end].strip()


def update_registry_application_status(learn_id: str, application_status: str, note: str = "") -> None:
    normalized_learn_id = normalize_learn_id(learn_id)
    path = registry_path(normalized_learn_id)
    if not path.exists():
        raise FileNotFoundError(f"learning registry entry not found: {normalized_learn_id}")
    text = path.read_text(encoding="utf-8")
    clean_status = sanitize_sensitive_text(application_status).strip()
    text = re.sub(r"^application_status:.*$", f"application_status: {clean_status}", text, flags=re.MULTILINE)
    text = re.sub(r"^-\s*application_status:.*$", f"- application_status: {clean_status}", text, flags=re.MULTILINE)
    text = append_to_section(text, "Application History", f"- {iso_now()} application_status={clean_status} note={sanitize_sensitive_text(note).strip() or 'no note'}")
    path.write_text(sanitize_sensitive_text(text), encoding="utf-8")
    rebuild_registry_index()


def update_proposal_application_status(learn_id: str, application_status: str, note: str = "") -> None:
    normalized_learn_id = normalize_learn_id(learn_id)
    try:
        text = read_proposal(normalized_learn_id)
    except FileNotFoundError:
        return
    clean_status = sanitize_sensitive_text(application_status).strip()
    text = re.sub(r"^-\s*application_status:.*$", f"- application_status: {clean_status}", text, flags=re.MULTILINE)
    text = re.sub(r"^- Application Status:.*$", f"- Application Status: {clean_status}", text, flags=re.MULTILINE)
    text = append_to_section(text, "Application Status", f"- {iso_now()} application_status: {clean_status} note={sanitize_sensitive_text(note).strip() or 'no note'}")
    write_proposal(normalized_learn_id, text)


def ensure_playbook_header(path: Path, target: str) -> None:
    safe_path = ensure_inside_playbooks(path)
    if safe_path.exists():
        return
    now = iso_now()
    if target == "global_playbook":
        title = "Atlas Workbench Playbook"
    else:
        title = f"Atlas Project Playbook {safe_path.stem}"
    safe_path.write_text(
        sanitize_sensitive_text(
            f"""# {title}

created_at: {now}
updated_at: {now}
mode: consultation
runtime_injection_enabled: false
external_application_enabled: false

This is a Workbench reference layer, not a runtime layer.
"""
        ),
        encoding="utf-8",
    )


def build_apply_enact_reply(apply_id: str, note: str) -> str:
    normalized_apply_id = normalize_apply_id(apply_id)
    text = read_application(normalized_apply_id)
    meta = task_metadata(text)
    if meta.get("status") != "planned":
        return f"Cannot enact {normalized_apply_id}: status must be planned, current={meta.get('status', 'unknown')}."
    target_path = playbook_path_from_display(meta.get("target_path", ""))
    ensure_playbook_header(target_path, meta.get("target", "global_playbook"))
    now = iso_now()
    entry = apply_plan_proposed_entry(text).replace("applied_at: pending", f"applied_at: {now}")
    if not entry.strip():
        raise ValueError("apply plan has no Proposed Playbook Entry")
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + sanitize_sensitive_text(entry).strip() + "\n")
    text = append_to_section(text, "Apply Record", f"### Applied at {now}\n- note: {sanitize_sensitive_text(note).strip() or 'no note'}\n- target_path: {playbook_display_path(target_path)}\n- runtime_injection_enabled: false")
    text = replace_task_field(text, "status", "applied")
    text = replace_task_field(text, "updated_at", now)
    write_application(normalized_apply_id, text)
    learn_id = meta.get("source_learn_id", "")
    if learn_id:
        update_registry_application_status(learn_id, "applied_to_workbench_playbook", f"apply_id={normalized_apply_id}; {note}")
        update_proposal_application_status(learn_id, "applied_to_workbench_playbook", f"apply_id={normalized_apply_id}; {note}")
    log_event("apply_enacted", apply_id=normalized_apply_id, learn_id=learn_id)
    return f"""Applied to Workbench Playbook: {normalized_apply_id}
- status: applied
- target_path: {playbook_display_path(target_path)}
- registry_application_status: applied_to_workbench_playbook
- runtime_injection_enabled: false
- Applied to Workbench Playbook, but not applied to Hermes / Memory / SkillRepo / system prompt / project code."""


def build_apply_revert_reply(apply_id: str, note: str) -> str:
    normalized_apply_id = normalize_apply_id(apply_id)
    text = read_application(normalized_apply_id)
    meta = task_metadata(text)
    if meta.get("status") != "applied":
        return f"Cannot revert {normalized_apply_id}: status must be applied, current={meta.get('status', 'unknown')}."
    target_path = playbook_path_from_display(meta.get("target_path", ""))
    ensure_playbook_header(target_path, meta.get("target", "global_playbook"))
    now = iso_now()
    learn_id = meta.get("source_learn_id", "")
    revert_note = f"""## Revert Note {normalized_apply_id}

reverted_at: {now}
apply_id: {normalized_apply_id}
source_learn_id: {learn_id}
application_status: reverted_from_workbench_playbook
runtime_injection_enabled: false
note: {sanitize_sensitive_text(note).strip() or 'no note'}

The original playbook entry is retained for audit. This revert did not modify Hermes, Memory, SkillRepo, system prompts, or project code.
"""
    with target_path.open("a", encoding="utf-8") as handle:
        handle.write("\n\n" + sanitize_sensitive_text(revert_note).strip() + "\n")
    text = append_to_section(text, "Revert Record", f"### Reverted at {now}\n- note: {sanitize_sensitive_text(note).strip() or 'no note'}\n- target_path: {playbook_display_path(target_path)}\n- runtime_injection_enabled: false")
    text = replace_task_field(text, "status", "reverted")
    text = replace_task_field(text, "updated_at", now)
    write_application(normalized_apply_id, text)
    if learn_id:
        update_registry_application_status(learn_id, "reverted_from_workbench_playbook", f"apply_id={normalized_apply_id}; {note}")
        update_proposal_application_status(learn_id, "reverted_from_workbench_playbook", f"apply_id={normalized_apply_id}; {note}")
    log_event("apply_reverted", apply_id=normalized_apply_id, learn_id=learn_id)
    return f"""Workbench-only apply reverted: {normalized_apply_id}
- status: reverted
- target_path: {playbook_display_path(target_path)}
- registry_application_status: reverted_from_workbench_playbook
- runtime_injection_enabled: false
- Revert Note appended; original playbook entry was not deleted."""


def build_apply_cancel_reply(apply_id: str, note: str) -> str:
    normalized_apply_id = normalize_apply_id(apply_id)
    text = read_application(normalized_apply_id)
    meta = task_metadata(text)
    if meta.get("status") != "planned":
        return f"Cannot cancel {normalized_apply_id}: status must be planned, current={meta.get('status', 'unknown')}."
    now = iso_now()
    text = append_to_section(text, "Apply Record", f"### Cancelled at {now}\n- note: {sanitize_sensitive_text(note).strip() or 'no note'}")
    text = replace_task_field(text, "status", "cancelled")
    text = replace_task_field(text, "updated_at", now)
    write_application(normalized_apply_id, text)
    log_event("apply_cancelled", apply_id=normalized_apply_id)
    return f"""Apply plan cancelled: {normalized_apply_id}
- status: cancelled
- no playbook write occurred."""


def build_apply_dashboard_reply() -> str:
    counts = apply_counts()
    return f"""Atlas Apply Dashboard
- planned_count: {counts['planned_count']}
- applied_count: {counts['applied_count']}
- reverted_count: {counts['reverted_count']}
- cancelled_count: {counts['cancelled_count']}
- global_playbook_entries: {counts['global_playbook_entries']}
- project_playbook_entries: {counts['project_playbook_entries']}
- runtime_injection_enabled: false
- external_application_enabled: false

Today suggestion:
- Enact only planned apply plans that have an approved learning registry entry and a clear rollback plan.
- Keep Playbook as reference layer; do not treat it as runtime behavior."""


def handle_apply_command(user_text: str) -> str | None:
    first_line = user_text.strip().splitlines()[0] if user_text.strip() else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/apply":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_apply_help_reply()
        if subcommand == "plan":
            return build_apply_plan_reply(tail)
        if subcommand == "show":
            return build_apply_show_reply(tail)
        if subcommand == "list":
            list_parts = tail.split()
            if len(list_parts) >= 2 and list_parts[0] == "--status":
                return build_apply_list_reply(list_parts[1])
            return build_apply_list_reply()
        if subcommand == "enact":
            enact_parts = tail.split(maxsplit=1)
            if not enact_parts:
                return "Usage: /apply enact <apply_id> <note>"
            return build_apply_enact_reply(enact_parts[0], enact_parts[1] if len(enact_parts) > 1 else "")
        if subcommand == "revert":
            revert_parts = tail.split(maxsplit=1)
            if not revert_parts:
                return "Usage: /apply revert <apply_id> <note>"
            return build_apply_revert_reply(revert_parts[0], revert_parts[1] if len(revert_parts) > 1 else "")
        if subcommand == "cancel":
            cancel_parts = tail.split(maxsplit=1)
            if not cancel_parts:
                return "Usage: /apply cancel <apply_id> <note>"
            return build_apply_cancel_reply(cancel_parts[0], cancel_parts[1] if len(cancel_parts) > 1 else "")
        if subcommand == "dashboard":
            return build_apply_dashboard_reply()
        return build_apply_help_reply()
    except FileNotFoundError as exc:
        return f"apply source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"apply operation refused: {safe_preview(str(exc), 240)}"
    except Exception as exc:
        return f"apply operation failed: {safe_preview(str(exc), 180)}"


def build_playbook_help_reply() -> str:
    return """Atlas Playbook commands
- /playbook help
- /playbook show global
- /playbook show project <project_id>
- /playbook list
- /playbook search <keyword>
- /playbook advise task <task_id>
- /playbook advise project <project_id>

Playbook is a Workbench reference layer only. It does not change runtime behavior. runtime_injection_enabled: false."""


def playbook_summary(path: Path, label: str) -> str:
    safe_path = ensure_inside_playbooks(path)
    if not safe_path.exists():
        return f"Playbook {label}: none\n- path: {playbook_display_path(safe_path)}\n- entry_count: 0"
    text = safe_path.read_text(encoding="utf-8")
    entries = re.findall(r"^## (LEARN-\d{8}-\d{6}(?:-\d{2})?.*)$", text, re.MULTILINE)
    recent = entries[-5:]
    lines = [
        f"Playbook {label}",
        f"- path: {playbook_display_path(safe_path)}",
        f"- entry_count: {playbook_entry_count(safe_path)}",
        f"- updated_at: {datetime.fromtimestamp(safe_path.stat().st_mtime).astimezone().isoformat(timespec='seconds')}",
        "- runtime_injection_enabled: false",
        "",
        "Recent entries:",
    ]
    lines.extend([f"- {entry}" for entry in recent] or ["- none."])
    return "\n".join(lines)


def build_playbook_show_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if not parts:
        return "Usage: /playbook show global OR /playbook show project <project_id>"
    kind = parts[0].lower()
    if kind == "global":
        return playbook_summary(global_playbook_path(), "global")
    if kind == "project":
        if len(parts) < 2:
            return "Usage: /playbook show project <project_id>"
        project_id = validate_project_id(parts[1])
        read_project(project_id)
        return playbook_summary(project_playbook_path(project_id), f"project {project_id}")
    return "Usage: /playbook show global OR /playbook show project <project_id>"


def build_playbook_list_reply() -> str:
    records = playbook_records()
    if not records:
        return "Playbooks:\n- none."
    lines = ["Playbooks:"]
    for record in records:
        lines.append(f"- {record['name']} | entries={record['entry_count']} | updated_at={record['updated_at']} | path={display_path(record['path']).replace(chr(92), '/')}")
    return "\n".join(lines)


def build_playbook_search_reply(keyword: str) -> str:
    clean_keyword = sanitize_sensitive_text(keyword).strip()
    if not clean_keyword:
        return "Usage: /playbook search <keyword>"
    records = playbook_records()
    hits = []
    lowered = clean_keyword.lower()
    for record in records:
        path = ensure_inside_playbooks(record["path"])
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if lowered in line.lower():
                hits.append(f"- {record['name']}:{index} {safe_preview(line, 180)}")
                if len(hits) >= 10:
                    break
        if len(hits) >= 10:
            break
    if not hits:
        return f"Playbook search: {clean_keyword}\n- no hits in workbench/playbooks."
    return "Playbook search: " + clean_keyword + "\n" + "\n".join(hits)


def context_keywords_from_text(text: str, limit: int = 20) -> list[str]:
    clean = sanitize_sensitive_text(text).lower()
    tokens = []
    for token in re.findall(r"[a-z0-9_-]{4,}", clean):
        if token not in tokens:
            tokens.append(token)
    for raw in re.split(r"\s+", sanitize_sensitive_text(text)):
        value = raw.strip("，。,.!?:;()[]{}<>|`'\"")
        if len(value) >= 3 and value not in tokens:
            tokens.append(value)
    return tokens[:limit]


def playbook_entry_units() -> list[dict]:
    units = []
    for record in playbook_records():
        path = ensure_inside_playbooks(record["path"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        matches = list(re.finditer(r"^## (LEARN-\d{8}-\d{6}(?:-\d{2})?.*)$", text, re.MULTILINE))
        if not matches and text.strip():
            units.append({"name": record["name"], "heading": record["name"], "body": text, "path": path})
            continue
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            units.append(
                {
                    "name": record["name"],
                    "heading": match.group(1).strip(),
                    "body": text[match.start():end].strip(),
                    "path": path,
                }
            )
    return units


def score_playbook_unit(unit: dict, keywords: list[str]) -> int:
    haystack = f"{unit.get('heading', '')}\n{unit.get('body', '')}".lower()
    score = 0
    for keyword in keywords:
        key = keyword.lower()
        if key and key in haystack:
            score += 1
    return score


def playbook_advisory_lines_for_keywords(keywords: list[str], max_items: int = 5) -> list[str]:
    scored = []
    for unit in playbook_entry_units():
        score = score_playbook_unit(unit, keywords)
        if score:
            scored.append((score, unit))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return ["- 未找到相关 playbook 条目 / no relevant playbook entries found."]
    lines = []
    for score, unit in scored[:max_items]:
        lines.append(f"- {unit['heading']} | source={unit['name']} | score={score} | {safe_preview(unit['body'], 220)}")
    return lines


def playbook_advisory_for_task(task_id: str, max_items: int = 5) -> str:
    normalized_task_id = normalize_task_id(task_id)
    task_text = read_task(normalized_task_id)
    meta = task_metadata(task_text)
    project_text = ""
    if meta.get("project_id"):
        try:
            project_text = read_project(meta["project_id"])
        except Exception:
            project_text = ""
    evidence_gap_text = ""
    try:
        evidence_gap_text = "\n".join(evidence_analysis(normalized_task_id, task_text).get("missing", []))
    except Exception:
        evidence_gap_text = ""
    retro_text = ""
    try:
        retro_text = read_retro(normalized_task_id)
    except Exception:
        retro_text = ""
    keyword_source = "\n".join(
        [
            task_title_from_text(normalized_task_id, task_text),
            meta.get("project_id", ""),
            task_section(task_text, "Goal"),
            task_section(task_text, "Scope"),
            task_section(task_text, "Acceptance Criteria"),
            evidence_gap_text,
            task_section(retro_text, "Lessons Learned"),
            task_section(retro_text, "Candidate Improvements"),
            project_title_from_text(meta.get("project_id", ""), project_text) if project_text and meta.get("project_id") else "",
        ]
    )
    lines = playbook_advisory_lines_for_keywords(context_keywords_from_text(keyword_source), max_items=max_items)
    return "\n".join(lines)


def playbook_advisory_for_project(project_id: str, max_items: int = 5) -> str:
    clean_project_id = validate_project_id(project_id)
    project_text = read_project(clean_project_id)
    tasks = project_task_records(clean_project_id)
    task_texts = []
    for task in tasks[:10]:
        try:
            task_texts.append(read_task(task["task_id"]))
        except Exception:
            continue
    retro_text = "\n".join(task_section(record.get("text", ""), "Lessons Learned") for record in retro_records() if record.get("project_id") == clean_project_id)
    keyword_source = "\n".join(
        [
            clean_project_id,
            project_title_from_text(clean_project_id, project_text),
            task_section(project_text, "Current State"),
            task_section(project_text, "Next Actions"),
            retro_text,
            "\n".join(task_title_from_text(task_metadata(text).get("task_id", ""), text) if task_metadata(text).get("task_id") else text.splitlines()[0] for text in task_texts),
        ]
    )
    lines = playbook_advisory_lines_for_keywords(context_keywords_from_text(keyword_source), max_items=max_items)
    return "\n".join(lines)


def build_playbook_advise_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /playbook advise task <task_id> OR /playbook advise project <project_id>"
    kind, value = parts[0].lower(), parts[1].strip()
    if kind == "task":
        normalized_task_id = normalize_task_id(value)
        advisory = playbook_advisory_for_task(normalized_task_id)
        return f"""Playbook Advisory for task {normalized_task_id}
- search_scope: workbench/playbooks only
- runtime_injection_enabled: false

{advisory}"""
    if kind == "project":
        clean_project_id = validate_project_id(value)
        advisory = playbook_advisory_for_project(clean_project_id)
        return f"""Playbook Advisory for project {clean_project_id}
- search_scope: workbench/playbooks only
- runtime_injection_enabled: false

{advisory}"""
    return "Usage: /playbook advise task <task_id> OR /playbook advise project <project_id>"


def handle_playbook_command(user_text: str) -> str | None:
    first_line = user_text.strip().splitlines()[0] if user_text.strip() else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/playbook":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_playbook_help_reply()
        if subcommand == "show":
            return build_playbook_show_reply(tail)
        if subcommand == "list":
            return build_playbook_list_reply()
        if subcommand == "search":
            return build_playbook_search_reply(tail)
        if subcommand == "advise":
            return build_playbook_advise_reply(tail)
        return build_playbook_help_reply()
    except FileNotFoundError as exc:
        return f"playbook source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"playbook operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"playbook operation failed: {safe_preview(str(exc), 180)}"


def generate_context_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("CTX-%Y%m%d-%H%M%S")
    if not context_pack_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not context_pack_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique context_id")


def read_context_pack(context_id: str) -> str:
    path = context_pack_path(context_id)
    if not path.exists():
        raise FileNotFoundError(f"context pack not found: {context_id}")
    return path.read_text(encoding="utf-8")


def write_context_pack(context_id: str, text: str) -> None:
    context_pack_path(context_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def context_title_from_text(context_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {context_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "untitled context pack"


def context_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(CONTEXT_PACKS_DIR.glob("CTX-*.md")):
        context_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "context_id": context_id,
                "title": context_title_from_text(context_id, text),
                "status": meta.get("status", "active"),
                "target": meta.get("target", "generic"),
                "source_task_id": meta.get("source_task_id", ""),
                "source_project_id": meta.get("source_project_id", ""),
                "created_at": meta.get("created_at", ""),
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)


def context_counts() -> dict:
    records = context_records()
    project_ids = {record.get("source_project_id") for record in records if record.get("source_project_id")}
    projects = {record.get("project_id") for record in project_records()}
    return {
        "context_pack_count": len(records),
        "latest_context_id": records[0]["context_id"] if records else "",
        "projects_with_context": len(project_ids),
        "projects_missing_context": len(projects - project_ids),
    }


def optional_section(label: str, value: str) -> str:
    text = sanitize_sensitive_text(value).strip()
    return text if text else "- not available"


def latest_execution_report(task_text: str) -> str:
    report = task_section(task_text, "Execution Report")
    if "### Report at" not in report:
        return "- not available"
    chunks = [chunk.strip() for chunk in report.split("### Report at") if chunk.strip()]
    if not chunks:
        return safe_preview(report, 800)
    return "### Report at " + chunks[-1]


def evidence_summary_for_task(task_id: str) -> tuple[str, str]:
    try:
        records = evidence_records(task_id)
    except Exception:
        return "- not available", "- not available"
    if not records:
        return "- not available", "- no evidence records found"
    summary_lines = []
    gap_lines = []
    for record in records[:10]:
        summary_lines.append(
            f"- {record.get('evidence_id')} | type={record.get('type')} | verified={record.get('verified')} | supports={record.get('supports_acceptance')} | {safe_preview(record.get('observed') or record.get('claim') or record.get('notes'), 180)}"
        )
        if record.get("verified") != "verified" or record.get("supports_acceptance") in {"missing", "claimed"}:
            gap_lines.append(f"- {record.get('evidence_id')} needs stronger verification or acceptance support.")
    if not gap_lines:
        gap_lines.append("- no obvious evidence gaps in evidence ledger")
    return "\n".join(summary_lines), "\n".join(gap_lines)


def learning_summary_for_task_project(task_id: str = "", project_id: str = "") -> str:
    related = []
    for record in registry_records():
        if task_id and record.get("source_task_id") == task_id:
            related.append(record)
        elif project_id and record.get("source_project_id") == project_id:
            related.append(record)
    if not related:
        return "- not available"
    return "\n".join(
        f"- {record['learn_id']} | application_status={record.get('application_status')} | project={record.get('source_project_id') or 'unassigned'} | {record.get('title')}"
        for record in related[:10]
    )


def retro_lessons_for_task_project(task_id: str = "", project_id: str = "") -> str:
    texts = []
    if task_id:
        try:
            retro_text = read_retro(task_id)
            texts.append(task_section(retro_text, "Lessons Learned") or task_section(retro_text, "Candidate Improvements"))
        except Exception:
            pass
    if project_id:
        for record in retro_records():
            if record.get("project_id") == project_id:
                texts.append(task_section(record.get("text", ""), "Lessons Learned") or task_section(record.get("text", ""), "Candidate Improvements"))
    merged = "\n".join(text for text in texts if text.strip()).strip()
    return merged or "- not available"


def task_summary_block(task_id: str) -> tuple[str, str, str, str, str, str, str, str]:
    normalized_task_id = normalize_task_id(task_id)
    task_text = read_task(normalized_task_id)
    meta = task_metadata(task_text)
    project_id = meta.get("project_id", "")
    project_text = ""
    if project_id:
        try:
            project_text = read_project(project_id)
        except Exception:
            project_text = ""
    task_summary = f"""- task_id: {normalized_task_id}
- title: {task_title_from_text(normalized_task_id, task_text)}
- status: {meta.get('status', 'unknown')}
- project_id: {project_id or 'unassigned'}
- updated_at: {meta.get('updated_at', '')}"""
    project_summary = (
        f"- project_id: {project_id}\n- title: {project_title_from_text(project_id, project_text)}\n- status: {project_metadata(project_text).get('status', 'unknown')}"
        if project_text and project_id
        else "- not available"
    )
    evidence_summary, evidence_gaps = evidence_summary_for_task(normalized_task_id)
    return (
        task_text,
        project_text,
        task_summary,
        project_summary,
        evidence_summary,
        evidence_gaps,
        project_id,
        meta.get("status", "unknown"),
    )


def build_context_pack_markdown_for_task(context_id: str, task_id: str, target: str = "generic") -> str:
    normalized_task_id = normalize_task_id(task_id)
    task_text, _project_text, task_summary, project_summary, evidence_summary, evidence_gaps, project_id, task_status_value = task_summary_block(normalized_task_id)
    title = task_title_from_text(normalized_task_id, task_text)
    advisory = playbook_advisory_for_task(normalized_task_id)
    retro_lessons = retro_lessons_for_task_project(task_id=normalized_task_id, project_id=project_id)
    learning_summary = learning_summary_for_task_project(task_id=normalized_task_id, project_id=project_id)
    latest_report = latest_execution_report(task_text)
    review_summary = optional_section("Atlas Review Summary", task_section(task_text, "Atlas Review"))
    user_decision = optional_section("User Decision", task_section(task_text, "User Decision"))
    acceptance = optional_section("Acceptance Criteria", task_section(task_text, "Acceptance Criteria"))
    next_action = f"- Review evidence gaps first; then decide whether to ask Codex/Kiro for more evidence or continue. task_status={task_status_value}"
    handoff_context = build_copyable_handoff_context(normalized_task_id, target, advisory, create_file=False)
    return sanitize_sensitive_text(f"""# {context_id} {sanitize_title(title)}

context_id: {context_id}
status: active
created_at: {iso_now()}
source: atlas
mode: consultation
target: {target}
source_task_id: {normalized_task_id}
source_project_id: {project_id}
runtime_injection_enabled: false
external_execution_enabled: false

## Task Summary
{task_summary}

## Project Summary
{project_summary}

## Current Status
- task_status: {task_status_value}
- runtime_injection_enabled: false
- external_execution_enabled: false

## Acceptance Criteria
{acceptance}

## Evidence Summary
{evidence_summary}

## Evidence Gaps
{evidence_gaps}

## Latest Report
{latest_report}

## Atlas Review Summary
{review_summary}

## User Decision
{user_decision}

## Retro Lessons
{retro_lessons}

## Learning Registry Summary
{learning_summary}

## Playbook Advisory
{advisory}

## Safety Boundary
- Read only workbench materials.
- Write only workbench/context_packs.
- Do not read .env.
- Do not modify Hermes, Memory, SkillRepo, system prompts, or project code.
- Do not run commands or call Codex/Kiro automatically.

## Recommended Next Action
{next_action}

## Copyable Handoff Context
{handoff_context}

## Not Applied To
- Hermes config
- Hermes Memory
- SkillRepo
- System prompt
- Project code
""")


def project_context_task_summary(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    records = [
        record for record in project_task_records(clean_project_id)
        if record.get("status") in {"open", "reported", "reviewed", "needs_evidence", "blocked"}
    ]
    if not records:
        return "- not available"
    return "\n".join(f"- {record['task_id']} | {record['status']} | {record['updated_at']} | {record['title']}" for record in records[:20])


def project_evidence_gap_summary(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    lines = []
    for record in project_task_records(clean_project_id):
        try:
            analysis = evidence_analysis(record["task_id"])
        except Exception:
            continue
        if analysis.get("has_gaps"):
            lines.append(f"- {record['task_id']} | {record['title']} | missing={'; '.join(analysis.get('missing', [])[:3])}")
    return "\n".join(lines[:20]) if lines else "- not available"


def build_context_pack_markdown_for_project(context_id: str, project_id: str, target: str = "generic") -> str:
    clean_project_id = validate_project_id(project_id)
    project_text = read_project(clean_project_id)
    project_meta = project_metadata(project_text)
    title = project_title_from_text(clean_project_id, project_text)
    task_summary = project_context_task_summary(clean_project_id)
    evidence_gaps = project_evidence_gap_summary(clean_project_id)
    advisory = playbook_advisory_for_project(clean_project_id)
    retro_lessons = retro_lessons_for_task_project(project_id=clean_project_id)
    learning_summary = learning_summary_for_task_project(project_id=clean_project_id)
    return sanitize_sensitive_text(f"""# {context_id} {sanitize_title(title)}

context_id: {context_id}
status: active
created_at: {iso_now()}
source: atlas
mode: consultation
target: {target}
source_task_id:
source_project_id: {clean_project_id}
runtime_injection_enabled: false
external_execution_enabled: false

## Task Summary
{task_summary}

## Project Summary
- project_id: {clean_project_id}
- title: {title}
- status: {project_meta.get('status', 'unknown')}
- priority: {project_meta.get('priority', '')}
- updated_at: {project_meta.get('updated_at', '')}

## Current Status
{optional_section('Current Status', task_section(project_text, 'Current State'))}

## Acceptance Criteria
- project-level pack; see task-specific packs for full criteria.

## Evidence Summary
- project evidence is summarized through task evidence ledgers.

## Evidence Gaps
{evidence_gaps}

## Latest Report
- project-level pack; see task-specific reports.

## Atlas Review Summary
- project-level pack; see task-specific Atlas reviews.

## User Decision
- project-level pack; see task-specific user decisions.

## Retro Lessons
{retro_lessons}

## Learning Registry Summary
{learning_summary}

## Playbook Advisory
{advisory}

## Safety Boundary
- Read only workbench materials.
- Write only workbench/context_packs.
- Do not read .env.
- Do not modify Hermes, Memory, SkillRepo, system prompts, or project code.
- Do not run commands or call Codex/Kiro automatically.

## Recommended Next Action
- Pick the highest-risk active/reported/reviewed/needs_evidence task and generate a task handoff with context.

## Copyable Handoff Context
- Project context pack for manual copy only. It is not sent automatically.

## Not Applied To
- Hermes config
- Hermes Memory
- SkillRepo
- System prompt
- Project code
""")


def build_context_help_reply() -> str:
    return """Atlas Context Pack commands
- /context help
- /context pack task <task_id>
- /context pack project <project_id>
- /context show <context_id>
- /context list
- /context archive <context_id>
- /context handoff <task_id> codex|kiro

Boundary: reads workbench only, writes workbench/context_packs only, does not read .env, does not execute commands, does not call Codex/Kiro, and does not perform runtime injection."""


def build_context_pack_task_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    context_id = generate_context_id()
    markdown = build_context_pack_markdown_for_task(context_id, normalized_task_id)
    write_context_pack(context_id, markdown)
    meta = task_metadata(read_task(normalized_task_id))
    log_event("context_pack_created", context_id=context_id, source_task_id=normalized_task_id)
    return f"""Context Pack created: {context_id}
- source_task_id: {normalized_task_id}
- source_project_id: {meta.get('project_id', '') or 'unassigned'}
- path: workbench/context_packs/{context_id}.md
- runtime_injection_enabled: false
- external_execution_enabled: false
- next: /context show {context_id}"""


def build_context_pack_project_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    read_project(clean_project_id)
    context_id = generate_context_id()
    markdown = build_context_pack_markdown_for_project(context_id, clean_project_id)
    write_context_pack(context_id, markdown)
    log_event("context_pack_created", context_id=context_id, source_project_id=clean_project_id)
    return f"""Context Pack created: {context_id}
- source_project_id: {clean_project_id}
- path: workbench/context_packs/{context_id}.md
- runtime_injection_enabled: false
- external_execution_enabled: false
- next: /context show {context_id}"""


def build_context_show_reply(context_id: str) -> str:
    normalized_context_id = normalize_context_id(context_id)
    text = read_context_pack(normalized_context_id)
    meta = task_metadata(text)
    return f"""Context Pack: {normalized_context_id}
- title: {context_title_from_text(normalized_context_id, text)}
- status: {meta.get('status', 'active')}
- target: {meta.get('target', 'generic')}
- source_task_id: {meta.get('source_task_id', '') or 'none'}
- source_project_id: {meta.get('source_project_id', '') or 'unassigned'}
- task_summary: {safe_preview(task_section(text, 'Task Summary'), 260)}
- evidence_gaps: {safe_preview(task_section(text, 'Evidence Gaps'), 260)}
- playbook_advisory: {safe_preview(task_section(text, 'Playbook Advisory'), 260)}
- recommended_next_action: {safe_preview(task_section(text, 'Recommended Next Action'), 220)}
- runtime_injection_enabled: false
- external_execution_enabled: false"""


def build_context_list_reply() -> str:
    records = context_records()
    if not records:
        return "Context Packs:\n- none."
    lines = ["Context Packs:"]
    for record in records[:10]:
        lines.append(
            f"- {record['context_id']} | {record.get('status')} | target={record.get('target')} | task={record.get('source_task_id') or 'none'} | project={record.get('source_project_id') or 'unassigned'} | {record.get('created_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def build_context_archive_reply(context_id: str) -> str:
    normalized_context_id = normalize_context_id(context_id)
    text = read_context_pack(normalized_context_id)
    text = replace_task_field(text, "status", "archived")
    text = append_to_section(text, "Archive Record", f"- archived_at: {iso_now()}")
    write_context_pack(normalized_context_id, text)
    log_event("context_pack_archived", context_id=normalized_context_id)
    return f"""Context Pack archived: {normalized_context_id}
- status: archived
- file retained: workbench/context_packs/{normalized_context_id}.md"""


def build_copyable_handoff_context(task_id: str, platform: str, advisory: str = "", create_file: bool = False) -> str:
    normalized_task_id = normalize_task_id(task_id)
    target = platform.strip().lower()
    if target not in {"codex", "claude", "kiro", "generic"}:
        raise ValueError("target must be codex, claude, kiro, or generic")
    task_text, _project_text, task_summary, project_summary, evidence_summary, evidence_gaps, project_id, _status = task_summary_block(normalized_task_id)
    display_target = EXECUTOR_DISPLAY_NAMES.get(target, "Generic")
    advisory_text = advisory or playbook_advisory_for_task(normalized_task_id)
    context_file_line = "- context_file: not created"
    if create_file:
        context_id = generate_context_id()
        write_context_pack(context_id, build_context_pack_markdown_for_task(context_id, normalized_task_id, target=target))
        context_file_line = f"- context_file: workbench/context_packs/{context_id}.md"
    return sanitize_sensitive_text(f"""# Handoff Context for {display_target}

Execution target: {display_target}
task_id: {normalized_task_id}
project_id: {project_id or 'unassigned'}
{context_file_line}

## Context Summary
{task_summary}

## Project Context
{project_summary}

## Evidence Status
{evidence_summary}

## Evidence Gaps
{evidence_gaps}

## Playbook Advisory
{advisory_text}

## Forbidden
- Do not read or print .env, tokens, cookies, or secrets.
- Do not modify unauthorized project files.
- Do not claim completion without evidence.
- Do not treat Playbook as runtime instructions.

## Return Report Format
- Modified files
- Commands
- Test results
- Evidence or logs
- Unverified items
- Unresolved risks
- Rollback notes

This is not sent automatically. It is copy-only context for manual handoff.
""")


def build_context_handoff_reply(task_id: str, platform: str) -> str:
    target = platform.strip().lower()
    if target not in {"codex", "kiro"}:
        return "Usage: /context handoff <task_id> codex|kiro"
    return build_copyable_handoff_context(task_id, target, create_file=False)


def handle_context_command(user_text: str) -> str | None:
    first_line = user_text.strip().splitlines()[0] if user_text.strip() else ""
    parts = first_line.split(maxsplit=3)
    if len(parts) < 2 or parts[0].lower() != "/context":
        return None
    subcommand = parts[1].lower()
    try:
        if subcommand == "help":
            return build_context_help_reply()
        if subcommand == "pack":
            if len(parts) < 4:
                return "Usage: /context pack task <task_id> OR /context pack project <project_id>"
            kind = parts[2].lower()
            if kind == "task":
                return build_context_pack_task_reply(parts[3])
            if kind == "project":
                return build_context_pack_project_reply(parts[3])
            return "Usage: /context pack task <task_id> OR /context pack project <project_id>"
        if subcommand == "show":
            return build_context_show_reply(parts[2] if len(parts) > 2 else "")
        if subcommand == "list":
            return build_context_list_reply()
        if subcommand == "archive":
            return build_context_archive_reply(parts[2] if len(parts) > 2 else "")
        if subcommand == "handoff":
            handoff_parts = (parts[2] + (" " + parts[3] if len(parts) > 3 else "")).split()
            if len(handoff_parts) != 2:
                return "Usage: /context handoff <task_id> codex|kiro"
            return build_context_handoff_reply(handoff_parts[0], handoff_parts[1])
        return build_context_help_reply()
    except FileNotFoundError as exc:
        return f"context source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"context operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"context operation failed: {safe_preview(str(exc), 180)}"


def generate_dispatch_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("DISPATCH-%Y%m%d-%H%M%S")
    if not dispatch_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not dispatch_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique dispatch_id")


def read_dispatch(dispatch_id: str) -> str:
    path = dispatch_path(dispatch_id)
    if not path.exists():
        raise FileNotFoundError(f"dispatch not found: {dispatch_id}")
    return path.read_text(encoding="utf-8")


def write_dispatch(dispatch_id: str, text: str) -> None:
    dispatch_path(dispatch_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def dispatch_title_from_text(dispatch_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {dispatch_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "untitled dispatch"


def quote_markdown_block(text: str) -> str:
    clean = sanitize_sensitive_text(text).strip()
    if not clean:
        return "> not available"
    return "\n".join(f"> {line}" if line else ">" for line in clean.splitlines())


def dispatch_is_stale_record(record: dict, now: datetime | None = None) -> bool:
    if record.get("status") != "sent":
        return False
    timestamp = record.get("sent_at") or record.get("updated_at") or record.get("created_at")
    if not timestamp:
        return False
    try:
        baseline = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    current = now or datetime.now().astimezone()
    if baseline.tzinfo is None:
        baseline = baseline.astimezone()
    return current - baseline > timedelta(hours=24)


def dispatch_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(DISPATCHES_DIR.glob("DISPATCH-*.md")):
        dispatch_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        record = {
            "dispatch_id": dispatch_id,
            "title": dispatch_title_from_text(dispatch_id, text),
            "status": meta.get("status", "unknown"),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "sent_at": meta.get("sent_at", ""),
            "task_id": meta.get("task_id", ""),
            "project_id": meta.get("project_id", ""),
            "target_executor": meta.get("target_executor", ""),
            "context_id": meta.get("context_id", ""),
            "path": path,
            "text": text,
        }
        record["stale"] = dispatch_is_stale_record(record)
        records.append(record)
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def latest_dispatch_for_task(task_id: str) -> dict | None:
    normalized_task_id = normalize_task_id(task_id)
    matches = [record for record in dispatch_records() if record.get("task_id") == normalized_task_id]
    status_rank = {
        "returned": 70,
        "qa_ready": 65,
        "needs_evidence": 60,
        "sent": 55,
        "reviewed": 50,
        "ready": 40,
        "failed": 20,
        "cancelled": 10,
        "closed": 0,
    }
    matches = sorted(
        matches,
        key=lambda item: (item.get("updated_at", ""), status_rank.get(item.get("status", ""), 0)),
        reverse=True,
    )
    return matches[0] if matches else None


def dispatch_counts(records: list[dict] | None = None) -> dict:
    items = records if records is not None else dispatch_records()
    return {
        "dispatch_count": len(items),
        "dispatch_ready_count": sum(1 for item in items if item.get("status") == "ready"),
        "dispatch_sent_count": sum(1 for item in items if item.get("status") == "sent"),
        "dispatch_returned_count": sum(1 for item in items if item.get("status") == "returned"),
        "dispatch_qa_ready_count": sum(1 for item in items if item.get("status") == "qa_ready"),
        "dispatch_needs_evidence_count": sum(1 for item in items if item.get("status") == "needs_evidence"),
        "dispatch_failed_count": sum(1 for item in items if item.get("status") == "failed"),
        "dispatch_stale_count": sum(1 for item in items if item.get("stale")),
    }


def dispatches_for_project(project_id: str) -> list[dict]:
    clean_project_id = validate_project_id(project_id)
    return [record for record in dispatch_records() if record.get("project_id") == clean_project_id]


def dispatch_context_summary(context_id: str) -> str:
    if not context_id:
        return "- context_id: none\n- context pack: not created"
    try:
        text = read_context_pack(context_id)
    except Exception as exc:
        return f"- context_id: {context_id}\n- context_error: {safe_preview(str(exc), 160)}"
    return f"""- context_id: {context_id}
- file: workbench/context_packs/{context_id}.md
- task_summary: {safe_preview(task_section(text, 'Task Summary'), 240)}
- evidence_gaps: {safe_preview(task_section(text, 'Evidence Gaps'), 240)}
- playbook_advisory: {safe_preview(task_section(text, 'Playbook Advisory'), 240)}"""


def build_dispatch_markdown(
    dispatch_id: str,
    task_id: str,
    target: str,
    with_context: bool,
    requested_executor: str = "",
    routing_reason: str = "",
) -> str:
    normalized_task_id = normalize_task_id(task_id)
    task_text, _project_text, task_summary, _project_summary, _evidence_summary, _evidence_gaps, project_id, _status = task_summary_block(normalized_task_id)
    clean_target = target.strip().lower()
    if clean_target not in SUPPORTED_EXECUTOR_TARGETS:
        raise ValueError("target_executor must be codex, claude, or kiro")
    clean_requested = str(requested_executor or "").strip().lower() or clean_target
    clean_routing_reason = sanitize_sensitive_text(str(routing_reason or "").strip()) or "explicit target selection"
    title = task_title_from_text(normalized_task_id, task_text)
    now = iso_now()
    context_id = ""
    if with_context:
        context_id = generate_context_id()
        write_context_pack(context_id, build_context_pack_markdown_for_task(context_id, normalized_task_id, target=clean_target))
    advisory = playbook_advisory_for_task(normalized_task_id)
    handoff = build_task_handoff_reply(normalized_task_id, clean_target)
    return sanitize_sensitive_text(f"""# {dispatch_id} {sanitize_title(title)}

dispatch_id: {dispatch_id}
status: ready
created_at: {now}
updated_at: {now}
sent_at:
task_id: {normalized_task_id}
project_id: {project_id}
target_executor: {clean_target}
requested_executor: {clean_requested}
routing_reason: {safe_preview(clean_routing_reason, 180)}
context_id: {context_id}
mode: manual
external_execution_enabled: false
runtime_injection_enabled: false
owner: local

## Task Summary
{task_summary}

## Handoff Package
{quote_markdown_block(handoff)}

## Context Pack Summary
{dispatch_context_summary(context_id)}

## Playbook Advisory
{advisory}

## Execution Window
- status: ready
- manual_copy_required: true
- external_execution_enabled: false

## Sent Record
- not sent.

## Return Report
- not returned.

## QA Result
- not checked.

## Atlas Review Link
- task_review: workbench/tasks/{normalized_task_id}.md#Atlas-Review

## User Decision Link
- task_decision: workbench/tasks/{normalized_task_id}.md#User-Decision

## Evidence Links
- task_file: workbench/tasks/{normalized_task_id}.md
- context_file: {('workbench/context_packs/' + context_id + '.md') if context_id else 'not created'}

## Status Timeline
- {now} dispatch created; status ready; target_executor={clean_target}; with_context={str(with_context).lower()}.

## Safety Boundary
- Dispatch may read workbench task, evidence, context, retro, learning, and playbook materials only.
- Dispatch writes only workbench/dispatches and may create workbench/context_packs when requested.
- Do not read .env.
- Do not write Hermes, Memory, SkillRepo, system prompts, Octo Docker, or user project code.
- Do not print tokens, cookies, passwords, api keys, or secrets.

## Do Not Auto-Execute
- This dispatch does not call Codex or Kiro.
- This dispatch does not run commands.
- This dispatch does not modify user project files.
- It only records manual copy, manual sent status, manual return reports, QA notes, and review links.
""")


def build_dispatch_help_reply() -> str:
    return """Atlas Dispatch commands
- /dispatch help
- /dispatch create <task_id> codex|kiro [--with-context]
- /dispatch list [--status <status>]
- /dispatch show <dispatch_id>
- /dispatch package <dispatch_id>
- /dispatch mark <dispatch_id> sent <note>
- /dispatch receive <dispatch_id>
  <pasted Codex/Kiro return report>
- /dispatch qa <dispatch_id>
- /dispatch link-review <dispatch_id>
- /dispatch close <dispatch_id>
- /dispatch cancel <dispatch_id> <note>
- /dispatch fail <dispatch_id> <note>
- /dispatch dashboard
- /dispatch stale

Boundary: manual dispatch ledger only. It does not call Codex/Kiro, does not run commands, does not modify user project files, and writes only workbench dispatch/context/task evidence records."""


def build_dispatch_create_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if len(parts) < 2:
        return "Usage: /dispatch create <task_id> codex|claude|kiro|auto [--with-context]"
    task_id = normalize_task_id(parts[0])
    target = parts[1].lower()
    requested_executor = target
    routing_reason = "explicit target selection"
    task_text = read_task(task_id)
    if target == "auto":
        routing_source = "\n".join(
            [task_title_from_text(task_id, task_text), task_section(task_text, "Goal")]
        )
        target, routing_reason = route_auto_executor(routing_source)
    if target not in SUPPORTED_EXECUTOR_TARGETS:
        return "Usage: /dispatch create <task_id> codex|claude|kiro|auto [--with-context]"
    with_context = "--with-context" in [part.lower() for part in parts[2:]]
    dispatch_id = generate_dispatch_id()
    markdown = build_dispatch_markdown(
        dispatch_id,
        task_id,
        target,
        with_context,
        requested_executor=requested_executor,
        routing_reason=routing_reason,
    )
    write_dispatch(dispatch_id, markdown)
    meta = task_metadata(markdown)
    log_event("dispatch_created", dispatch_id=dispatch_id, task_id=task_id, target_executor=target)
    return f"""Dispatch created: {dispatch_id}
- status: ready
- task_id: {task_id}
- target_executor: {target}
- requested_executor: {requested_executor}
- routing_reason: {safe_preview(routing_reason, 160)}
- context_id: {meta.get('context_id') or 'none'}
- path: workbench/dispatches/{dispatch_id}.md
- external_execution_enabled: false
- runtime_injection_enabled: false
- next: /dispatch package {dispatch_id} and manually copy it to {target}"""


def build_dispatch_list_reply(tail: str = "") -> str:
    parts = str(tail or "").strip().split()
    status_filter = ""
    if parts:
        if len(parts) == 2 and parts[0] == "--status":
            status_filter = parts[1].lower()
            if status_filter not in DISPATCH_STATUSES:
                return "Invalid status. Use: draft/ready/sent/returned/qa_ready/reviewed/needs_evidence/failed/cancelled/closed"
        else:
            return "Usage: /dispatch list [--status <status>]"
    records = dispatch_records()
    if status_filter:
        records = [record for record in records if record.get("status") == status_filter]
    if not records:
        return "Dispatches:\n- none."
    lines = ["Dispatches:"]
    for record in records[:20]:
        stale = " stale" if record.get("stale") else ""
        lines.append(
            f"- {record['dispatch_id']} | {record.get('status')}{stale} | task={record.get('task_id')} | target={record.get('target_executor')} | updated={record.get('updated_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def build_dispatch_show_reply(dispatch_id: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = meta.get("task_id", "")
    task_status_value = "unknown"
    task_next = "unknown"
    if task_id:
        try:
            task_text = read_task(task_id)
            task_status_value = task_metadata(task_text).get("status", "unknown")
            task_next = safe_preview(build_task_next_reply(task_id), 180)
        except Exception as exc:
            task_next = f"task error: {safe_preview(str(exc), 120)}"
    record = {
        "status": meta.get("status", "unknown"),
        "sent_at": meta.get("sent_at", ""),
        "updated_at": meta.get("updated_at", ""),
        "created_at": meta.get("created_at", ""),
    }
    stale = dispatch_is_stale_record(record)
    latest_exec = latest_exec_for_dispatch(normalized_dispatch_id)
    exec_id = latest_exec.get("exec_id") if latest_exec else "none"
    exec_status = latest_exec.get("status") if latest_exec else "none"
    exec_opened_at = latest_exec.get("opened_at") if latest_exec else "none"
    exec_copied_at = latest_exec.get("copied_at") if latest_exec else "none"
    exec_returned_at = latest_exec.get("returned_at") if latest_exec else "none"
    return f"""Dispatch summary: {normalized_dispatch_id}
- status: {meta.get('status', 'unknown')}
- task_id: {task_id or 'none'}
- task_status: {task_status_value}
- project_id: {meta.get('project_id') or 'unassigned'}
- target_executor: {meta.get('target_executor') or 'unknown'}
- context_id: {meta.get('context_id') or 'none'}
- latest_exec_id: {exec_id}
- exec_status: {exec_status}
- exec_opened_at: {exec_opened_at or 'none'}
- exec_copied_at: {exec_copied_at or 'none'}
- exec_returned_at: {exec_returned_at or 'none'}
- updated_at: {meta.get('updated_at', '')}
- stale: {str(stale).lower()}
- return_report: {safe_preview(task_section(text, 'Return Report'), 240)}
- qa_result: {safe_preview(task_section(text, 'QA Result'), 240)}
- next: {task_next}"""


def build_dispatch_package_reply(dispatch_id: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    target = meta.get("target_executor", "").strip().lower()
    if target not in SUPPORTED_EXECUTOR_TARGETS:
        return "Dispatch target is invalid. Expected codex, claude, or kiro."
    task_text = read_task(task_id)
    display_target = EXECUTOR_DISPLAY_NAMES.get(target, target.title())
    latest_exec = latest_exec_for_dispatch(normalized_dispatch_id)
    latest_exec_id = latest_exec.get("exec_id") if latest_exec else "none"
    exec_status = latest_exec.get("status") if latest_exec else "none"
    exec_next = exec_next_action(latest_exec) if latest_exec else f"prepare semi-auto session: /exec prepare {normalized_dispatch_id}"
    return sanitize_sensitive_text(f"""# Manual Dispatch Package for {display_target}

dispatch_id: {normalized_dispatch_id}
task_id: {task_id}
target_executor: {target}
manual_copy_required: true
external_execution_enabled: false
runtime_injection_enabled: false

This package is for manual copy only. Atlas/Bridge has not sent it and will not call {display_target}.

## Execution Session
- latest_exec_id: {latest_exec_id}
- exec_status: {exec_status}
- next_action: {exec_next}
- human_confirm_required: true
- auto_execute_enabled: false

## Task Title
{task_title_from_text(task_id, task_text)}

## Goal
{optional_section('Goal', task_section(task_text, 'Goal'))}

## Scope
{optional_section('Scope', task_section(task_text, 'Scope'))}

## Execution Boundary
{optional_section('Execution Boundary', task_section(task_text, 'Execution Boundary'))}

## Forbidden
- Do not read, print, log, commit, or leak .env, tokens, cookies, passwords, api keys, or secrets.
- Do not modify unauthorized projects.
- Do not change Octo Docker or Hermes main code.
- Do not claim completion without evidence.

## Suggested Checks
- Confirm working directory and current repo state before changes.
- Keep changes minimal and inside the authorized scope.
- Run relevant compile, smoke, or test commands.
- Collect files, commands, test results, logs/screenshots, unverified items, and unresolved risks.

## Acceptance Criteria
{optional_section('Acceptance Criteria', task_section(task_text, 'Acceptance Criteria'))}

## Context Pack Summary
{dispatch_context_summary(meta.get('context_id', ''))}

## Playbook Advisory
{playbook_advisory_for_task(task_id)}

## Return Report Format
Task id: {task_id}
Dispatch id: {normalized_dispatch_id}

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
- Redact tokens, cookies, Authorization headers, passwords, api keys, and secrets before returning.
- Do not paste .env content.

## User Final Acceptance
- The user will paste your report back with /dispatch receive {normalized_dispatch_id}.
- Atlas will then run /dispatch qa, /task review, and wait for the user's final /task decide.
""")


def update_dispatch_status(dispatch_id: str, status: str, timeline: str, extra_fields: dict[str, str] | None = None) -> str:
    if status not in DISPATCH_STATUSES:
        raise ValueError("invalid dispatch status")
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    now = iso_now()
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", now)
    if extra_fields:
        for key, value in extra_fields.items():
            text = replace_task_field(text, key, value)
    text = append_to_section(text, "Status Timeline", f"- {now} {timeline}")
    write_dispatch(normalized_dispatch_id, text)
    return text


def build_dispatch_mark_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=3)
    if len(parts) < 2 or parts[1].lower() != "sent":
        return "Usage: /dispatch mark <dispatch_id> sent <note>"
    dispatch_id = normalize_dispatch_id(parts[0])
    note = sanitize_sensitive_text(parts[2] if len(parts) > 2 else "").strip() or "manual sent recorded"
    now = iso_now()
    text = update_dispatch_status(dispatch_id, "sent", f"marked sent: {note}", {"sent_at": now})
    text = set_section_body(text, "Sent Record", f"- sent_at: {now}\n- note: {note}\n- manual_copy_only: true")
    write_dispatch(dispatch_id, text)
    log_event("dispatch_sent", dispatch_id=dispatch_id)
    return f"""Dispatch marked sent: {dispatch_id}
- status: sent
- sent_at: {now}
- note: {note}
- next: wait for manual report, then /dispatch receive {dispatch_id}"""


def build_dispatch_receive_reply(dispatch_id: str, report: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    clean_report = sanitize_sensitive_text(report).strip() or "- empty return report; needs evidence."
    now = iso_now()
    text = replace_task_field(text, "status", "returned")
    text = replace_task_field(text, "updated_at", now)
    text = append_to_section(text, "Return Report", f"### Return at {now}\n{clean_report}")
    text = append_to_section(text, "Status Timeline", f"- {now} return report received; status returned; synced to task report.")
    write_dispatch(normalized_dispatch_id, text)
    task_reply = build_task_report_reply(task_id, report)
    log_event("dispatch_returned", dispatch_id=normalized_dispatch_id, task_id=task_id)
    return f"""Dispatch return recorded: {normalized_dispatch_id}
- status: returned
- task_id: {task_id}
- synced_task_report: true
- path: workbench/dispatches/{normalized_dispatch_id}.md
- next: /dispatch qa {normalized_dispatch_id}, then /task review {task_id}

Task sync:
{safe_preview(task_reply, 360)}"""


def dispatch_qa_conclusion(qa_text: str) -> str:
    for line in qa_text.splitlines()[:12]:
        lowered = line.lower()
        if "needs_evidence" in lowered:
            return "needs_evidence"
        if "pass" in lowered and any(marker in lowered for marker in ("quality", "conclusion", "recommendation", "status")):
            return "pass"
        if "pass" in lowered and any(marker in line for marker in ("质检", "结论", "推荐", "状态")):
            return "pass"
    return "needs_evidence"


def build_dispatch_qa_reply(dispatch_id: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    return_report = task_section(text, "Return Report")
    if "### Return at" not in return_report:
        qa_text = """Quality conclusion: needs_evidence

Satisfied:
- none

Missing:
- Return Report
- Modified files
- Commands
- Test results
- Key logs or screenshots
- Unverified items
- Unresolved risks

Risks:
- No return report is recorded, so the dispatch cannot support acceptance.

Recommended decision: needs_evidence"""
        status = "needs_evidence"
    else:
        qa_text = build_task_qa_reply(task_id)
        conclusion = dispatch_qa_conclusion(qa_text)
        status = "qa_ready" if conclusion == "pass" else "needs_evidence"
        qa_text = f"dispatch_id: {normalized_dispatch_id}\n{qa_text}\n\nDispatch QA note:\n- observed evidence is not verified automatically.\n- Recommended: /task accept-evidence <task_id> <reason>, /evidence accept <task_id> <evidence_id> <reason>, or /evidence mark <task_id> <evidence_id> verified, then /task review {task_id}."
    now = iso_now()
    text = read_dispatch(normalized_dispatch_id)
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", now)
    text = append_to_section(text, "QA Result", f"### QA at {now}\n{qa_text}")
    text = append_to_section(text, "Status Timeline", f"- {now} dispatch QA generated; status {status}.")
    write_dispatch(normalized_dispatch_id, text)
    log_event("dispatch_qa", dispatch_id=normalized_dispatch_id, status=status)
    return f"""Dispatch QA: {normalized_dispatch_id}
- status: {status}
- task_id: {task_id}

{qa_text}

Next:
- /task review {task_id}
- /dispatch link-review {normalized_dispatch_id}"""


def build_dispatch_link_review_reply(dispatch_id: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    task_text = read_task(task_id)
    review = task_section(task_text, "Atlas Review")
    decision = task_section(task_text, "User Decision")
    has_review = "Review at" in review or "å®¡æŸ¥" in review
    now = iso_now()
    link_body = f"""- linked_at: {now}
- task_file: workbench/tasks/{task_id}.md
- atlas_review_summary: {safe_preview(review, 420) or 'not reviewed'}
- user_decision_summary: {safe_preview(decision, 220) or 'not decided'}"""
    text = set_section_body(text, "Atlas Review Link", link_body)
    text = append_to_section(text, "Status Timeline", f"- {now} linked task review; has_review={str(has_review).lower()}.")
    if has_review:
        text = replace_task_field(text, "status", "reviewed")
        text = replace_task_field(text, "updated_at", now)
    write_dispatch(normalized_dispatch_id, text)
    return f"""Dispatch review link updated: {normalized_dispatch_id}
- task_id: {task_id}
- has_review: {str(has_review).lower()}
- status: {'reviewed' if has_review else meta.get('status', 'unknown')}
- next: /task decide {task_id} pass|needs_evidence|blocked|cancelled <note>"""


def build_dispatch_close_reply(dispatch_id: str) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    task_id = meta.get("task_id", "")
    dispatch_status_value = meta.get("status", "unknown")
    task_status_value = "unknown"
    closure_summary = "- evidence_closure_state: not_applicable\n- evidence_gap_risk: false"
    if task_id:
        task_status_value = task_status(task_id)
        try:
            closure_summary = build_closure_evidence_summary(sync_task_evidence_state(task_id))
        except Exception:
            closure_summary = "- evidence_closure_state: unavailable\n- evidence_gap_risk: unknown"
    if dispatch_status_value not in {"cancelled", "failed", "closed"} and task_status_value not in {"passed", "cancelled", "archived"}:
        return (
            f"Cannot close dispatch {normalized_dispatch_id}: dispatch_status={dispatch_status_value}, "
            f"task_status={task_status_value}. Close only after task passed/cancelled/archived or dispatch cancelled/failed."
        )
    update_dispatch_status(normalized_dispatch_id, "closed", f"dispatch closed; previous_status={dispatch_status_value}; task_status={task_status_value}.")
    log_event("dispatch_closed", dispatch_id=normalized_dispatch_id)
    return f"""Dispatch closed: {normalized_dispatch_id}
- status: closed
- task_id: {task_id or 'none'}
- task_status: {task_status_value}
{closure_summary}"""


def build_dispatch_terminal_reply(tail: str, status: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if not parts:
        return f"Usage: /dispatch {status} <dispatch_id> <note>"
    dispatch_id = normalize_dispatch_id(parts[0])
    note = sanitize_sensitive_text(parts[1] if len(parts) > 1 else "").strip() or f"manual {status}"
    update_dispatch_status(dispatch_id, status, f"marked {status}: {note}")
    log_event(f"dispatch_{status}", dispatch_id=dispatch_id)
    return f"""Dispatch marked {status}: {dispatch_id}
- status: {status}
- note: {note}
- manual ledger only: true"""


def build_dispatch_dashboard_reply() -> str:
    records = dispatch_records()
    counts = dispatch_counts(records)
    ready = [record for record in records if record.get("status") == "ready"][:10]
    sent = [record for record in records if record.get("status") == "sent"][:10]
    returned = [record for record in records if record.get("status") == "returned"][:10]
    failed = [record for record in records if record.get("status") == "failed"][:10]
    lines = [
        "Atlas Dispatch Dashboard",
        f"- dispatch_count: {counts['dispatch_count']}",
        f"- dispatch_ready_count: {counts['dispatch_ready_count']}",
        f"- dispatch_sent_count: {counts['dispatch_sent_count']}",
        f"- dispatch_returned_count: {counts['dispatch_returned_count']}",
        f"- dispatch_needs_evidence_count: {counts['dispatch_needs_evidence_count']}",
        f"- dispatch_failed_count: {counts['dispatch_failed_count']}",
        f"- dispatch_stale_count: {counts['dispatch_stale_count']}",
        "- external_execution_enabled: false",
        "",
        "Ready:",
    ]
    lines.extend([f"- {item['dispatch_id']} | task={item.get('task_id')} | target={item.get('target_executor')}" for item in ready] or ["- none"])
    lines.append("")
    lines.append("Sent not returned:")
    lines.extend([f"- {item['dispatch_id']} | task={item.get('task_id')} | stale={str(item.get('stale')).lower()}" for item in sent] or ["- none"])
    lines.append("")
    lines.append("Returned pending QA:")
    lines.extend([f"- {item['dispatch_id']} | task={item.get('task_id')} | target={item.get('target_executor')}" for item in returned] or ["- none"])
    lines.append("")
    lines.append("Failed:")
    lines.extend([f"- {item['dispatch_id']} | task={item.get('task_id')} | {item.get('title')}" for item in failed] or ["- none"])
    lines.append("")
    lines.append("Suggested next actions:")
    if ready:
        lines.append(f"- Prepare semi-auto execution session: /exec prepare {ready[0]['dispatch_id']}")
    if sent:
        lines.append(f"- If report is back, record it: /dispatch receive {sent[0]['dispatch_id']}")
    if returned:
        lines.append(f"- QA returned report: /dispatch qa {returned[0]['dispatch_id']}")
    if not (ready or sent or returned):
        lines.append("- Create a manual dispatch: /dispatch create <task_id> codex --with-context")
    return "\n".join(lines)


def build_dispatch_stale_reply() -> str:
    stale = [record for record in dispatch_records() if record.get("stale")]
    if not stale:
        return "Stale dispatches:\n- none. Rule: status=sent and sent/updated time older than 24 hours."
    lines = ["Stale dispatches (sent > 24h):"]
    for record in stale[:20]:
        lines.append(
            f"- {record['dispatch_id']} | task={record.get('task_id')} | target={record.get('target_executor')} | sent_at={record.get('sent_at') or record.get('updated_at')} | next=/dispatch receive {record['dispatch_id']} OR /dispatch fail {record['dispatch_id']} <note>"
        )
    return "\n".join(lines)


def handle_dispatch_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/dispatch":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_dispatch_help_reply()
        if subcommand == "create":
            return build_dispatch_create_reply(tail)
        if subcommand == "list":
            return build_dispatch_list_reply(tail)
        if subcommand == "show":
            return build_dispatch_show_reply(tail)
        if subcommand == "package":
            return build_dispatch_package_reply(tail)
        if subcommand == "mark":
            return build_dispatch_mark_reply(tail)
        if subcommand == "receive":
            receive_parts = tail.split(maxsplit=1)
            if not receive_parts:
                return "Usage: /dispatch receive <dispatch_id>\n<pasted return report>"
            dispatch_id = receive_parts[0]
            inline = receive_parts[1] if len(receive_parts) > 1 else ""
            body = "\n".join(lines[1:]).strip()
            return build_dispatch_receive_reply(dispatch_id, body or inline)
        if subcommand == "qa":
            return build_dispatch_qa_reply(tail)
        if subcommand == "link-review":
            return build_dispatch_link_review_reply(tail)
        if subcommand == "close":
            return build_dispatch_close_reply(tail)
        if subcommand == "cancel":
            return build_dispatch_terminal_reply(tail, "cancelled")
        if subcommand == "fail":
            return build_dispatch_terminal_reply(tail, "failed")
        if subcommand == "dashboard":
            return build_dispatch_dashboard_reply()
        if subcommand == "stale":
            return build_dispatch_stale_reply()
        return build_dispatch_help_reply()
    except FileNotFoundError as exc:
        return f"dispatch source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"dispatch operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"dispatch operation failed: {safe_preview(str(exc), 180)}"


def generate_exec_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("EXEC-%Y%m%d-%H%M%S")
    if not exec_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not exec_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique exec_id")


def read_exec(exec_id: str) -> str:
    path = exec_path(exec_id)
    if not path.exists():
        raise FileNotFoundError(f"execution session not found: {exec_id}")
    return path.read_text(encoding="utf-8")


def write_exec(exec_id: str, text: str) -> None:
    exec_path(exec_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def exec_title_from_text(exec_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {exec_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "untitled execution"


def exec_is_stale_record(record: dict, now: datetime | None = None) -> bool:
    if record.get("status") not in {"prepared", "started", "opened", "copied", "needs_manual_start"}:
        return False
    timestamp = record.get("updated_at") or record.get("created_at")
    if not timestamp:
        return False
    try:
        baseline = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    current = now or datetime.now().astimezone()
    if baseline.tzinfo is None:
        baseline = baseline.astimezone()
    return current - baseline > timedelta(hours=24)


def exec_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(EXECUTIONS_DIR.glob("EXEC-*.md")):
        exec_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        record = {
            "exec_id": exec_id,
            "title": exec_title_from_text(exec_id, text),
            "status": meta.get("status", "unknown"),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "opened_at": meta.get("opened_at", ""),
            "copied_at": meta.get("copied_at", ""),
            "returned_at": meta.get("returned_at", ""),
            "started_at": meta.get("started_at", ""),
            "auto_run_mode": meta.get("auto_run_mode", ""),
            "read_only_auto_run": meta.get("read_only_auto_run", ""),
            "runner_probe": meta.get("runner_probe", ""),
            "runner_mode": meta.get("runner_mode", ""),
            "runner_sandbox": meta.get("runner_sandbox", ""),
            "returncode": meta.get("returncode", ""),
            "timed_out": meta.get("timed_out", ""),
            "stdout_chars": meta.get("stdout_chars", ""),
            "stderr_chars": meta.get("stderr_chars", ""),
            "completion_state": meta.get("completion_state", ""),
            "payload_state": meta.get("payload_state", ""),
            "run_policy": meta.get("run_policy", ""),
            "run_policy_reason": meta.get("run_policy_reason", ""),
            "write_confirmed": meta.get("write_confirmed", ""),
            "write_approved_at": meta.get("write_approved_at", ""),
            "owner_write_policy": meta.get("owner_write_policy", ""),
            "owner_write_policy_status": meta.get("owner_write_policy_status", ""),
            "owner_write_policy_reason": meta.get("owner_write_policy_reason", ""),
            "write_target_fidelity": meta.get("write_target_fidelity", ""),
            "write_target_lines": meta.get("write_target_lines", ""),
            "dispatch_id": meta.get("dispatch_id", ""),
            "task_id": meta.get("task_id", ""),
            "project_id": meta.get("project_id", ""),
            "target_executor": meta.get("target_executor", ""),
            "path": path,
            "text": text,
        }
        record["stale"] = exec_is_stale_record(record)
        records.append(record)
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def latest_exec_for_dispatch(dispatch_id: str) -> dict | None:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    matches = [record for record in exec_records() if record.get("dispatch_id") == normalized_dispatch_id]
    status_rank = {
        "returned": 80,
        "started": 75,
        "copied": 70,
        "opened": 65,
        "prepared": 60,
        "needs_manual_start": 55,
        "failed": 20,
        "cancelled": 10,
    }
    matches = sorted(
        matches,
        key=lambda item: (item.get("updated_at", ""), status_rank.get(item.get("status", ""), 0)),
        reverse=True,
    )
    return matches[0] if matches else None


WRITE_APPROVAL_ELIGIBLE_STATUSES = {"prepared", "needs_manual_start"}


@dataclass(frozen=True)
class RunPolicy:
    name: str
    auto_execute_enabled: bool
    human_confirm_required: bool
    read_only_gate_label: str


RUN_POLICY_MANUAL = RunPolicy(
    name="manual_confirmation",
    auto_execute_enabled=False,
    human_confirm_required=True,
    read_only_gate_label="not_run",
)
RUN_POLICY_READ_ONLY_AUTO = RunPolicy(
    name="read_only_auto_start",
    auto_execute_enabled=True,
    human_confirm_required=False,
    read_only_gate_label="passed",
)
RUN_POLICY_APPROVED_WRITE = RunPolicy(
    name="approved_workspace_write",
    auto_execute_enabled=True,
    human_confirm_required=False,
    read_only_gate_label="not_required_after_write_approval",
)
RUN_POLICY_OWNER_APPROVED_WRITE = RunPolicy(
    name="owner_approved_workspace_write",
    auto_execute_enabled=True,
    human_confirm_required=False,
    read_only_gate_label="not_required_after_owner_write_preflight",
)


def run_policy_for_sandbox(sandbox_mode: str, owner_write_policy: bool = False) -> RunPolicy:
    if sandbox_mode == "workspace-write":
        return RUN_POLICY_OWNER_APPROVED_WRITE if owner_write_policy else RUN_POLICY_APPROVED_WRITE
    return RUN_POLICY_READ_ONLY_AUTO


def run_policy_fields(policy: RunPolicy, reason: str = "") -> dict[str, str]:
    return {
        "run_policy": policy.name,
        "run_policy_reason": safe_preview(reason, 180),
    }


def resolve_write_approval_target(target_id: str) -> tuple[str, str, bool]:
    value = str(target_id or "").strip()
    if re.fullmatch(r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?", value):
        dispatch_id = normalize_dispatch_id(value)
        record = latest_exec_for_dispatch(dispatch_id)
        if record:
            status = record.get("status", "unknown")
            if status not in WRITE_APPROVAL_ELIGIBLE_STATUSES:
                raise ValueError(
                    f"dispatch latest execution {record.get('exec_id', '')} status={status}; "
                    "write approval requires prepared/needs_manual_start or no existing execution"
                )
            return normalize_exec_id(record.get("exec_id", "")), dispatch_id, False
        target = task_metadata(read_dispatch(dispatch_id)).get("target_executor", "").lower()
        if target not in {"codex", "claude"}:
            raise ValueError("dispatch target_executor must be codex or claude for workspace-write approval")
        exec_id, _text = create_exec_session(dispatch_id)
        return exec_id, dispatch_id, True
    if re.fullmatch(r"EXEC-\d{8}-\d{6}(?:-\d{2})?", value):
        exec_id = normalize_exec_id(value)
        text = read_exec(exec_id)
        dispatch_id = normalize_dispatch_id(task_metadata(text).get("dispatch_id", ""))
        return exec_id, dispatch_id, False
    raise ValueError("approval target must be an EXEC-* or DISPATCH-* id")


def latest_write_approval_candidate() -> dict | None:
    for record in exec_records():
        if record.get("status") not in WRITE_APPROVAL_ELIGIBLE_STATUSES:
            continue
        if record.get("target_executor", "").lower() != "codex":
            continue
        if record.get("stale"):
            continue
        return record
    return None


def latest_exec_for_task(task_id: str) -> dict | None:
    normalized_task_id = normalize_task_id(task_id)
    matches = [record for record in exec_records() if record.get("task_id") == normalized_task_id]
    return matches[0] if matches else None


def execs_for_project(project_id: str) -> list[dict]:
    clean_project_id = validate_project_id(project_id)
    return [record for record in exec_records() if record.get("project_id") == clean_project_id]


def exec_counts(records: list[dict] | None = None) -> dict:
    items = records if records is not None else exec_records()
    return {
        "execution_count": len(items),
        "execution_prepared_count": sum(1 for item in items if item.get("status") == "prepared"),
        "execution_started_count": sum(1 for item in items if item.get("status") == "started"),
        "execution_opened_count": sum(1 for item in items if item.get("status") == "opened"),
        "execution_copied_count": sum(1 for item in items if item.get("status") == "copied"),
        "execution_returned_count": sum(1 for item in items if item.get("status") == "returned"),
        "execution_needs_manual_start_count": sum(1 for item in items if item.get("status") == "needs_manual_start"),
        "execution_failed_count": sum(1 for item in items if item.get("status") == "failed"),
        "execution_cancelled_count": sum(1 for item in items if item.get("status") == "cancelled"),
        "execution_stale_count": sum(1 for item in items if item.get("stale")),
        "latest_exec_id": items[0]["exec_id"] if items else "",
    }


def build_exec_payload(exec_id: str, dispatch_id: str) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    package = build_dispatch_package_reply(normalized_dispatch_id)
    text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(text)
    target = meta.get("target_executor", "unknown")
    display_target = "Codex" if target == "codex" else "Kiro" if target == "kiro" else target
    return sanitize_sensitive_text(f"""# Semi-Auto Execution Package for {display_target}

exec_id: {normalized_exec_id}
dispatch_id: {normalized_dispatch_id}
task_id: {meta.get('task_id', '')}
target_executor: {target}
human_confirm_required: true
external_execution_enabled: false
runtime_injection_enabled: false
auto_execute_enabled: false

This package is the execution payload for {display_target}. Atlas/Bridge only permits read-only auto start after safety checks; otherwise this payload is for manual copy only.

## Dispatch Summary
- dispatch_id: {normalized_dispatch_id}
- dispatch_status: {meta.get('status', 'unknown')}
- task_id: {meta.get('task_id', '')}
- project_id: {meta.get('project_id', '') or 'unassigned'}
- target_executor: {target}
- context_id: {meta.get('context_id', '') or 'none'}

## Manual Confirmation Required
- Copy this payload into {display_target} manually.
- Confirm any action inside {display_target} manually.
- Paste the return report back with /exec receive {normalized_exec_id}.

## Auto-Run Output Guidance
- Keep the final answer concise so the runner can exit before timeout.
- For minimal AUTORUN validation, include AUTORUN-PAYLOAD-OK and a short return report.
- Do not stream long analysis logs; summarize commands, results, unverified items, and risks.

## Dispatch Package
{package}
""")


CJK_CHAR_PATTERN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def detect_source_language(text: str) -> str:
    sample = str(text or "")
    has_cjk = bool(CJK_CHAR_PATTERN.search(sample))
    has_latin = bool(re.search(r"[A-Za-z]", sample))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "zh"
    return "en"


def claude_source_request_text(dispatch_id: str) -> str:
    dispatch_text = read_dispatch(dispatch_id)
    meta = task_metadata(dispatch_text)
    task_id = meta.get("task_id", "")
    parts = [dispatch_title_from_text(normalize_dispatch_id(dispatch_id), dispatch_text)]
    if task_id:
        try:
            task_text = read_task(task_id)
            parts = [task_title_from_text(task_id, task_text), task_section(task_text, "Goal")]
        except Exception:
            pass
    return "\n".join(part for part in parts if part and part.strip())


def render_claude_english_runner_payload(exec_id: str, dispatch_id: str, sandbox_mode: str, owner_write_policy: bool) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    meta = task_metadata(read_dispatch(normalized_dispatch_id))
    source_text = claude_source_request_text(normalized_dispatch_id)
    source_language = detect_source_language(source_text)
    policy = run_policy_for_sandbox(sandbox_mode, owner_write_policy=owner_write_policy)
    targets = extract_declared_owner_write_targets(normalized_exec_id, normalized_dispatch_id)
    if sandbox_mode == "workspace-write":
        target_lines = "\n".join(f"- {target}" for target in targets) or "- none declared; do not write anything"
        write_rules = """## Allowed Write Targets
{targets}
- Write only inside the allowed targets above. Any other change fails the run.

## Human Write Approval
- exec_id: {exec_id}
- approval_mode: workspace-write
- owner_write_policy: {owner}
- user_explicit_confirmation_required: true
- This approval allows modifying workspace files only for the scoped task.""".format(
            targets=target_lines, exec_id=normalized_exec_id, owner=str(bool(owner_write_policy)).lower()
        )
    else:
        write_rules = """## Allowed Write Targets
- none (this is a read-only run; do not create, modify, or delete any file)"""
    return sanitize_sensitive_text(f"""# Claude Execution Package (rendered in English)

exec_id: {normalized_exec_id}
dispatch_id: {normalized_dispatch_id}
task_id: {meta.get('task_id', '')}
project_id: {meta.get('project_id', '') or 'unassigned'}
target_executor: claude
source_language: {source_language}
executor_prompt_language: en
executor_prompt_rendered_for: claude

## Goal
- Fulfill the Original User Request section below exactly as written.
- Treat that section as the authoritative task intent even if it is not in English.
- Use all file paths, command names, code identifiers, task/dispatch/exec IDs exactly as written there; never translate or alter them.

## Original User Request (preserved verbatim, source language: {source_language})
{source_text or '- empty'}

## Scope
- Only the work described in the Original User Request, within the allowed write targets below.
- Do not expand scope, refactor unrelated code, or touch unrelated files.

{write_rules}

## Constraints
- Work only inside the authorized project workspace.
- Keep changes minimal and reviewable.
- runner_sandbox: {sandbox_mode}

## Acceptance Criteria
- The Original User Request is satisfied exactly.
- No files outside the allowed write targets are created, modified, or deleted.
- The return report below is complete and concise.

## Safety Boundaries
- Atlas/Bridge stays in control; you are an executor only.
- Do not claim completion without verifiable evidence.
- Codex will review this work read-only before any close decision.

## Evidence Requirements (return report)
- Summarize modified files.
- Summarize commands and tests run.
- State git status and git diff --stat observations.
- State unresolved risks and unverified items.
- Keep output concise enough to avoid runner timeout; include AUTORUN-PAYLOAD-OK.

## Secrets Rule
- Do not read, print, or modify .env files, tokens, cookies, Authorization headers, passwords, API keys, or any secrets.

## Git Rule
- Do not run git add, git commit, git push, git merge, or deploy. Not explicitly allowed for this run.

## Run Policy
- run_policy: {policy.name}
- policy_auto_execute_enabled: {str(policy.auto_execute_enabled).lower()}
- policy_human_confirm_required: {str(policy.human_confirm_required).lower()}
- runner_sandbox: {sandbox_mode}
- read_only_gate: {policy.read_only_gate_label}
- owner_write_policy: {str(bool(owner_write_policy)).lower()}
""")


def render_executor_prompt_for_target(target_executor: str, payload_text: str, metadata: dict | None = None) -> str:
    """Claude-bound executor payloads are rendered in English; other targets
    keep their existing payload text unchanged."""
    if str(target_executor or "").strip().lower() != "claude":
        return payload_text
    meta = metadata or {}
    return render_claude_english_runner_payload(
        str(meta.get("exec_id", "")),
        str(meta.get("dispatch_id", "")),
        str(meta.get("sandbox_mode", "read-only")),
        bool(meta.get("owner_write_policy", False)),
    )


def build_runner_payload(exec_id: str, dispatch_id: str, sandbox_mode: str, owner_write_policy: bool = False) -> str:
    target = task_metadata(read_dispatch(normalize_dispatch_id(dispatch_id))).get("target_executor", "").strip().lower()
    if target == "claude":
        return render_executor_prompt_for_target(
            "claude",
            "",
            {
                "exec_id": exec_id,
                "dispatch_id": dispatch_id,
                "sandbox_mode": sandbox_mode,
                "owner_write_policy": owner_write_policy,
            },
        )
    payload = build_exec_payload(exec_id, dispatch_id)
    policy = run_policy_for_sandbox(sandbox_mode, owner_write_policy=owner_write_policy)
    payload = sanitize_sensitive_text(f"""{payload}

## Run Policy
- run_policy: {policy.name}
- policy_auto_execute_enabled: {str(policy.auto_execute_enabled).lower()}
- policy_human_confirm_required: {str(policy.human_confirm_required).lower()}
- runner_sandbox: {sandbox_mode}
- read_only_gate: {policy.read_only_gate_label}
- owner_write_policy: {str(bool(owner_write_policy)).lower()}
""")
    if sandbox_mode != "workspace-write":
        return payload
    return sanitize_sensitive_text(f"""{payload}

## Human Write Approval
- exec_id: {normalize_exec_id(exec_id)}
- approval_mode: workspace-write
- owner_write_policy: {str(bool(owner_write_policy)).lower()}
- user_explicit_confirmation_required: true
- This approval allows the executor to modify workspace files only for the scoped task.

## Workspace-Write Forbidden Actions
- Do not run git add.
- Do not run git commit.
- Do not run git push.
- Do not run git merge.
- Do not deploy.
- Do not read or print .env files.
- Do not print tokens, cookies, Authorization headers, passwords, api keys, or secrets.

## Required Return Evidence
- Summarize modified files.
- Summarize commands and tests run.
- State git status and git diff --stat observations.
- State unresolved risks and unverified items.
- Keep output concise enough to avoid runner timeout.
""")


def build_exec_markdown(exec_id: str, dispatch_id: str) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    dispatch_text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(dispatch_text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    target = meta.get("target_executor", "").strip().lower()
    if target not in SUPPORTED_EXECUTOR_TARGETS:
        raise ValueError("dispatch target_executor must be codex, claude, or kiro")
    title = dispatch_title_from_text(normalized_dispatch_id, dispatch_text)
    now = iso_now()
    payload = build_exec_payload(normalized_exec_id, normalized_dispatch_id)
    return sanitize_sensitive_text(f"""# {normalized_exec_id} {sanitize_title(title)}

exec_id: {normalized_exec_id}
dispatch_id: {normalized_dispatch_id}
task_id: {task_id}
project_id: {meta.get('project_id', '')}
target_executor: {target}
status: prepared
created_at: {now}
updated_at: {now}
opened_at:
copied_at:
started_at:
returned_at:
mode: semi_auto
run_policy: {RUN_POLICY_MANUAL.name}
run_policy_reason: prepared for manual copy unless guarded start or explicit write approval succeeds
human_confirm_required: true
external_execution_enabled: false
runtime_injection_enabled: false
auto_execute_enabled: false
read_only_auto_run: false
auto_run_mode:
runner_probe:
runner_mode:
runner_sandbox:
executor_status: pending
executor_reason:
command_attempted:
returncode:
timed_out: false
stdout_chars: 0
stderr_chars: 0
completion_state: prepared
payload_state:
write_confirmed: false
write_approved_at:
owner_write_policy: false
owner_write_policy_status:
owner_write_policy_reason:
write_target_fidelity:
post_run_target_fidelity:
write_target_lines:
writer_executor:
reviewer_executor:
review_required_by:
codex_review_status:
codex_review_reason:
source_language:
executor_prompt_language:
executor_prompt_rendered_for:
auto_postprocess_enabled: false
auto_qa_done: false
auto_evidence_verified: false
auto_review_done: false
auto_dispatch_review_linked: false
auto_decision:
auto_closed: false
auto_retro_created: false
auto_postprocess_reason:
manual_copy_required: true

## Dispatch Summary
- dispatch_id: {normalized_dispatch_id}
- dispatch_status: {meta.get('status', 'unknown')}
- task_id: {task_id}
- project_id: {meta.get('project_id', '') or 'unassigned'}
- target_executor: {target}
- context_id: {meta.get('context_id', '') or 'none'}

## Executor Target
- target_executor: {target}
- semi_auto_only: true
- user_confirms_in_executor: true

## Prepared Command Or Instructions
- No command is executed by Atlas/Bridge.
- Use /exec package {normalized_exec_id} to show the copy payload.
- Manually copy the payload into {target}.
- After the executor returns a report, paste it with /exec receive {normalized_exec_id}.

## Manual Confirmation Required
- human_confirm_required: true
- The user must open or select the executor.
- The user must paste the payload.
- The user must confirm any executor-side action.

## Human Write Approval
- not approved.

## Copy Payload
{payload}

## Open Record
- not opened.

## Auto Start
- not started.

## Runner Metadata
- not started.

## Runner Stdout
- not captured.

## Runner Stderr
- not captured.

## Post-Run Snapshot
- not captured.

## Runner Test Results
- not captured.

## Return Record
- not returned.

## Safety Boundary
- This execution session writes only workbench/executions and syncs return reports through existing dispatch/task ledgers.
- It does not read .env.
- It does not print tokens, cookies, Authorization headers, passwords, api keys, or secrets.
- It does not modify user project files.
- It does not change Octo Docker, Hermes, Memory, SkillRepo, or runtime prompts.

## Do Not Auto-Execute
- This session does not call Codex/Kiro.
- This session does not run the dispatch package.
- This session does not inject prompts into any runtime.
- Actual execution happens only after the user manually confirms inside the chosen executor.

## Status Timeline
- {now} execution session prepared; no external call made.
""")


def create_exec_session(dispatch_id: str) -> tuple[str, str]:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    dispatch_text = read_dispatch(normalized_dispatch_id)
    dispatch_status_value = task_metadata(dispatch_text).get("status", "unknown")
    if dispatch_status_value not in {"ready", "sent"}:
        raise ValueError("dispatch status must be ready or sent")
    exec_id = generate_exec_id()
    markdown = build_exec_markdown(exec_id, normalized_dispatch_id)
    write_exec(exec_id, markdown)
    log_event("exec_prepared", exec_id=exec_id, dispatch_id=normalized_dispatch_id)
    return exec_id, markdown


READ_ONLY_BOUNDARY_MARKERS = (
    "read-only",
    "read only",
    "only read",
    "no code changes",
    "no file changes",
    "do not modify",
    "do not write",
    "只读",
    "不改代码",
    "不修改",
    "不创建文件",
    "不写文件",
    "不提交",
)

WRITE_INTENT_PATTERNS = (
    r"\bwrite\s+code\b",
    r"\bwrite\s+implementation\b",
    r"\bmodify\s+(code|files?)\b",
    r"\bcreate\s+(files?|migration|component|route)\b",
    r"\bedit\s+(files?|code)\b",
    r"\bapply\s+patch\b",
    r"\bgit\s+(add|commit|push|merge)\b",
    r"\bdeploy\b",
    r"写代码",
    r"修改代码",
    r"改代码",
    r"创建文件",
    r"新建文件",
    r"修改文件",
    r"提交",
    r"部署",
    r"发布",
)

SOURCE_FILE_REF_PATTERN = r"(?:[A-Za-z0-9_.-]+[\\/])*[A-Za-z0-9_.-]+\.py|\*\.py"

SOURCE_WRITE_INTENT_PATTERNS = (
    r"\bwrite\s+implementation\b",
    r"\bsource[-/\s]+write\s+implementation\b",
    r"\bsource[-\s]+code\s+implementation\b",
    r"\b(?:implement|implementation)\s+(?:in|inside|within)\s+(?:" + SOURCE_FILE_REF_PATTERN + r")\b",
    r"\b(?:modify|update|edit|patch|change|harden)\s+(?:" + SOURCE_FILE_REF_PATTERN + r")\b",
    r"\badd\s+regression\s+tests?\s+(?:in|to)\s+(?:" + SOURCE_FILE_REF_PATTERN + r")\b",
    r"\b(?:modify|update|edit|patch|change|harden)\s+(?:tracked\s+)?source\s+(?:code|files?)\b",
    r"\b(?:edit|patch|modify|change|update|harden)\s+source\b",
)

NEGATED_SOURCE_WRITE_PATTERNS = (
    r"\b(?:do\s+not|don't|must\s+not|cannot|can't)\s+(?:modify|update|edit|patch|change|harden|implement|write)\b.*(?:source|\.py)",
    r"\bwithout\s+(?:modifying|updating|editing|patching|changing|hardening|implementing|writing)\b.*(?:source|\.py)",
    r"\bno\s+(?:source\s+)?code\s+changes?\b",
    r"\bno\s+changes?\s+to\s+(?:source|.*\.py)\b",
)

WRITE_APPROVAL_FORBIDDEN_PATTERNS = (
    r"\bgit\s+(add|commit|push|merge)\b",
    r"\bdeploy\b",
    r"æäº¤",
    r"éƒ¨ç½²",
    r"å‘å¸ƒ",
)

WRITE_APPROVAL_SECRET_FORBIDDEN_PATTERNS = (
    r"\b(?:read|cat|open|print|show|dump|echo|log|paste|return|exfiltrate|leak)\b.{0,100}\b(?:\.env|tokens?|cookies?|authorization\s+headers?|passwords?|api[_\s-]?keys?|secrets?)\b",
    r"\b(?:\.env|tokens?|cookies?|authorization\s+headers?|passwords?|api[_\s-]?keys?|secrets?)\b.{0,100}\b(?:read|cat|open|print|show|dump|echo|log|paste|return|exfiltrate|leak)\b",
)

OWNER_WRITE_HARD_DENY_PATTERNS = (
    r"\blive\s+validation\s+hard[-\s]?deny\b",
    r"\bhard[-\s]?deny\s+explicit\s+target\b",
)

OWNER_WRITE_NOOP_PATTERNS = (
    r"\bno[-\s]?op\b",
    r"\bnoop\b",
    r"\bno\s+operation\b",
)

OWNER_WRITE_REAL_ACTION_PATTERNS = (
    r"\b(?:create|update|touch|append|add|modify|edit|patch|implement)\b",
    r"\bwrite\s+(?:implementation|code|files?|changes?|to|into)\b",
    r"\bsource[-/\s]+write\b",
)

EXPLICIT_WRITE_TARGET_PATTERNS = (
    r"\b(create|update|write|touch)\b.+\b(workbench|logs|runtime|tmp|templates)[\\/][^\s`'\"<>]+",
    r"\b(create|update|write|touch)\b.+[A-Za-z0-9_.-]+[\\/][^\s`'\"<>]+\.(txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html)\b",
    r"\b(create|update|write|touch)\b.+\b(file|path)\b.+[^\s`'\"<>]+\.(txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html)\b",
    r"\b(create\s+or\s+update|create/update|update\s+or\s+create)\b.+[^\s`'\"<>]+[\\/][^\s`'\"<>]+",
    r"\b(?:target\s+files?|target\s+paths?|write\s+targets?|allowed\s+write\s+paths?)\s*:\s*[^\n]+?\.(txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html)\b",
    r"\b(?:append|add)\b.+\bto\s+[^\s`'\"<>]+\.(txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html)\b",
    r"\breadme(?:\.md)?\s+only\b",
)

NEGATIVE_BOUNDARY_MARKERS = (
    "do not",
    "don't",
    "must not",
    "no ",
    "not ",
    "forbidden",
    "禁止",
    "不要",
    "不得",
    "不能",
    "不",
)


def context_text_for_dispatch(dispatch_text: str) -> str:
    context_id = task_metadata(dispatch_text).get("context_id", "")
    if not context_id:
        return ""
    try:
        return read_context_pack(context_id)
    except Exception:
        return ""


def exec_start_source_bundle(dispatch_id: str) -> tuple[str, str, str, str]:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    dispatch_text = read_dispatch(normalized_dispatch_id)
    meta = task_metadata(dispatch_text)
    task_id = normalize_task_id(meta.get("task_id", ""))
    task_text = read_task(task_id)
    context_text = context_text_for_dispatch(dispatch_text)
    package = build_dispatch_package_reply(normalized_dispatch_id)
    return dispatch_text, task_text, context_text, package


def line_has_explicit_write_target(line: str) -> bool:
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in EXPLICIT_WRITE_TARGET_PATTERNS)


def line_has_source_write_intent(line: str) -> bool:
    if any(re.search(pattern, line, re.IGNORECASE) for pattern in NEGATED_SOURCE_WRITE_PATTERNS):
        return False
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in SOURCE_WRITE_INTENT_PATTERNS)


def line_has_write_intent(line: str) -> bool:
    lowered = line.lower()
    field_label = re.sub(r"^[>\-\s]*", "", lowered).strip()
    report_field_labels = {
        "modified files:",
        "modified files：",
        "files changed:",
        "files changed：",
        "修改文件:",
        "修改文件：",
        "变更文件:",
        "变更文件：",
    }
    if field_label in report_field_labels:
        return False
    if line_has_source_write_intent(line):
        return True
    evidence_or_report_markers = (
        "return report",
        "report format",
        "modified files",
        "rollback notes",
        "acceptance",
        "evidence",
        "report:",
        "报告",
        "回传",
        "验收",
        "证据",
    )
    if any(marker in lowered for marker in evidence_or_report_markers):
        return False
    safety_capability_markers = (
        "dispatch writes only workbench/dispatches",
        "write only workbench/context_packs",
        "writes only workbench/executions",
        "writes only workbench execution",
        "bridge/atlas only writes",
    )
    if any(marker in lowered for marker in safety_capability_markers):
        return False
    if line_has_explicit_write_target(line):
        return True
    if any(marker in lowered for marker in NEGATIVE_BOUNDARY_MARKERS):
        return False
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in WRITE_INTENT_PATTERNS)


def read_only_dispatch_gate(dispatch_id: str) -> dict:
    dispatch_text, task_text, context_text, package = exec_start_source_bundle(dispatch_id)
    combined = "\n".join([dispatch_text, task_text, context_text, package])
    lowered = combined.lower()
    has_read_only_marker = any(marker in lowered for marker in READ_ONLY_BOUNDARY_MARKERS)
    write_lines = [
        sanitize_sensitive_text(line.strip())
        for line in combined.splitlines()
        if line_has_write_intent(line)
    ][:5]
    return {
        "ok": bool(has_read_only_marker and not write_lines),
        "has_read_only_marker": has_read_only_marker,
        "write_intent_lines": write_lines,
        "reason": (
            "read-only boundary accepted"
            if has_read_only_marker and not write_lines
            else "missing read-only boundary" if not has_read_only_marker else "write intent detected"
        ),
    }


def line_has_forbidden_write_approval_action(line: str) -> bool:
    lowered = line.lower()
    if any(marker in lowered for marker in NEGATIVE_BOUNDARY_MARKERS):
        return False
    patterns = WRITE_APPROVAL_FORBIDDEN_PATTERNS + WRITE_APPROVAL_SECRET_FORBIDDEN_PATTERNS
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns)


def write_approval_gate(dispatch_id: str) -> dict:
    dispatch_text, task_text, context_text, package = exec_start_source_bundle(dispatch_id)
    combined = "\n".join([dispatch_text, task_text, context_text, package])
    forbidden_lines = [
        sanitize_sensitive_text(line.strip())
        for line in combined.splitlines()
        if line_has_forbidden_write_approval_action(line)
    ][:5]
    return {
        "ok": not forbidden_lines,
        "forbidden_lines": forbidden_lines,
        "reason": "write approval accepted" if not forbidden_lines else "forbidden write/deploy action detected",
    }


def owner_write_target_gate(dispatch_id: str) -> dict:
    dispatch_text, task_text, _context_text, _package = exec_start_source_bundle(dispatch_id)
    task_id = task_metadata(dispatch_text).get("task_id", "")
    combined = "\n".join(
        [
            dispatch_title_from_text(normalize_dispatch_id(dispatch_id), dispatch_text),
            task_title_from_text(task_id, task_text) if task_id else "",
            task_section(task_text, "Goal"),
        ]
    )
    target_lines = [
        sanitize_sensitive_text(line.strip())
        for line in combined.splitlines()
        if line.strip() and (line_has_source_write_intent(line) or line_has_explicit_write_target(line))
    ][:5]
    return {
        "ok": bool(target_lines),
        "target_lines": target_lines,
        "reason": "explicit write target accepted" if target_lines else "missing explicit write target",
    }


def owner_write_scope_lines(dispatch_id: str) -> list[str]:
    dispatch_text, task_text, _context_text, _package = exec_start_source_bundle(dispatch_id)
    task_id = task_metadata(dispatch_text).get("task_id", "")
    return [
        dispatch_title_from_text(normalize_dispatch_id(dispatch_id), dispatch_text),
        task_title_from_text(task_id, task_text) if task_id else "",
        task_section(task_text, "Goal"),
    ]


def line_has_owner_write_hard_deny(line: str) -> bool:
    lowered = line.lower()
    if any(marker in lowered for marker in NEGATIVE_BOUNDARY_MARKERS):
        return False
    if any(re.search(pattern, line, re.IGNORECASE) for pattern in OWNER_WRITE_HARD_DENY_PATTERNS):
        return True
    return any(re.search(pattern, line, re.IGNORECASE) for pattern in WRITE_APPROVAL_SECRET_FORBIDDEN_PATTERNS)


def line_has_owner_write_noop(line: str) -> bool:
    if not any(re.search(pattern, line, re.IGNORECASE) for pattern in OWNER_WRITE_NOOP_PATTERNS):
        return False
    if line_has_source_write_intent(line):
        return False
    return not any(re.search(pattern, line, re.IGNORECASE) for pattern in OWNER_WRITE_REAL_ACTION_PATTERNS)


def owner_write_hard_deny_gate(dispatch_id: str) -> dict:
    deny_lines = [
        sanitize_sensitive_text(line.strip())
        for line in owner_write_scope_lines(dispatch_id)
        if line.strip() and line_has_owner_write_hard_deny(line)
    ][:5]
    return {
        "ok": not deny_lines,
        "deny_lines": deny_lines,
        "reason": "owner write hard-deny accepted" if not deny_lines else "owner write hard-deny request detected",
    }


def owner_write_noop_gate(dispatch_id: str) -> dict:
    noop_lines = [
        sanitize_sensitive_text(line.strip())
        for line in owner_write_scope_lines(dispatch_id)
        if line.strip() and line_has_owner_write_noop(line)
    ][:5]
    return {
        "ok": not noop_lines,
        "noop_lines": noop_lines,
        "reason": "owner write no-op accepted" if not noop_lines else "owner write no-op request detected",
    }


def owner_write_preflight_gate(dispatch_id: str) -> dict:
    hard_deny_gate = owner_write_hard_deny_gate(dispatch_id)
    approval_gate = write_approval_gate(dispatch_id)
    noop_gate = owner_write_noop_gate(dispatch_id)
    target_gate = owner_write_target_gate(dispatch_id)
    ok = hard_deny_gate["ok"] and approval_gate["ok"] and noop_gate["ok"] and target_gate["ok"]
    if not hard_deny_gate["ok"]:
        reason = hard_deny_gate["reason"]
    elif not approval_gate["ok"]:
        reason = approval_gate["reason"]
    elif not noop_gate["ok"]:
        reason = noop_gate["reason"]
    elif not target_gate["ok"]:
        reason = target_gate["reason"]
    else:
        reason = "owner write preflight accepted"
    return {
        "ok": ok,
        "reason": reason,
        "hard_deny_gate": hard_deny_gate,
        "approval_gate": approval_gate,
        "noop_gate": noop_gate,
        "target_gate": target_gate,
    }


def command_basename(command: str) -> str:
    return Path(str(command or "")).name.lower()


def process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def is_allowed_external_command(argv: list[str]) -> bool:
    if not argv:
        return False
    name = command_basename(argv[0])
    codex_names = {"codex", "codex.exe", "codex.cmd", "codex.bat"}
    claude_names = {"claude", "claude.exe", "claude.cmd", "claude.bat"}
    if name in codex_names:
        if len(argv) == 2 and argv[1] == "--help":
            return True
        if len(argv) == 3 and argv[1:3] == ["exec", "--help"]:
            return True
        if len(argv) == 5 and argv[1:3] == ["exec", "--sandbox"] and argv[3] in RUNNER_SANDBOX_MODES and argv[4] == "-":
            return True
        return False
    if name in claude_names:
        # Probe commands only.
        if len(argv) == 2 and argv[1] in {"--version", "--help"}:
            return True
        # Read-only non-interactive print mode; prompt arrives via stdin only.
        # No permission-bypass, no tool-allow, no workspace-write flags.
        if len(argv) == 2 and argv[1] == "-p":
            return True
        # Owner-approved workspace-write shape: print mode with edit
        # acceptance only. No --dangerously-skip-permissions, no tool
        # allowlists, no arbitrary flags.
        if len(argv) == 4 and argv[1:] == ["-p", "--permission-mode", "acceptEdits"]:
            return True
        return False
    return False


def is_allowed_post_run_command(argv: list[str]) -> bool:
    if argv == ["git", "status", "--short"]:
        return True
    if argv == ["git", "diff", "--stat"]:
        return True
    if argv == ["git", "diff", "--cached", "--stat"]:
        return True
    return False


def windows_subprocess_argv(argv: list[str]) -> list[str]:
    """Return an argv that works for Windows .cmd/.bat shims with shell=False."""
    if os.name == "nt" and argv:
        name = command_basename(argv[0]).lower()
        if name.endswith((".cmd", ".bat")):
            return ["cmd.exe", "/d", "/c", subprocess.list2cmdline(argv)]
    return argv


def run_allowlisted_external_command(
    argv: list[str],
    *,
    input_text: str = "",
    timeout: int = EXEC_START_TIMEOUT_SECONDS,
) -> dict:
    if not is_allowed_external_command(argv):
        raise ValueError("external command is not allowlisted")
    try:
        completed = subprocess.run(
            windows_subprocess_argv(argv),
            cwd=ROOT,
            input=sanitize_sensitive_text(input_text),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": sanitize_sensitive_text(completed.stdout or ""),
            "stderr": sanitize_sensitive_text(completed.stderr or ""),
            "timed_out": False,
        }
    except FileNotFoundError as exc:
        return {"returncode": 127, "stdout": "", "stderr": sanitize_sensitive_text(str(exc)), "timed_out": False}
    except subprocess.TimeoutExpired as exc:
        stdout = sanitize_sensitive_text(process_text(exc.stdout))
        stderr = sanitize_sensitive_text(process_text(exc.stderr) or "execution timed out")
        return {
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
        }


def run_allowlisted_post_run_command(argv: list[str]) -> dict:
    if not is_allowed_post_run_command(argv):
        raise ValueError("post-run command is not allowlisted")
    try:
        completed = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RUNNER_SNAPSHOT_TIMEOUT_SECONDS,
            shell=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": sanitize_sensitive_text(completed.stdout or ""),
            "stderr": sanitize_sensitive_text(completed.stderr or ""),
        }
    except FileNotFoundError as exc:
        return {"returncode": 127, "stdout": "", "stderr": sanitize_sensitive_text(str(exc))}
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": sanitize_sensitive_text(process_text(exc.stdout)),
            "stderr": sanitize_sensitive_text(process_text(exc.stderr) or "post-run snapshot timed out"),
        }


def probe_codex_noninteractive(sandbox_mode: str = "read-only") -> dict:
    if sandbox_mode not in RUNNER_SANDBOX_MODES:
        return {"supported": False, "mode": "invalid_sandbox", "reason": f"unsupported sandbox mode: {sandbox_mode}", "command": []}
    if os.environ.get("OHB_EXEC_SIMULATE_CODEX") == "1":
        return {"supported": True, "mode": "simulated", "reason": "OHB_EXEC_SIMULATE_CODEX enabled", "command": []}
    codex_path = shutil.which("codex")
    if not codex_path:
        return {"supported": False, "mode": "missing", "reason": "codex command not found", "command": []}
    # Probe the actual non-interactive command directly. Some Windows shims or
    # top-level help output do not reliably expose subcommands.
    exec_help = run_allowlisted_external_command([codex_path, "exec", "--help"], timeout=10)
    exec_help_text = f"{exec_help.get('stdout', '')}\n{exec_help.get('stderr', '')}".lower()
    if exec_help.get("returncode") != 0:
        return {"supported": False, "mode": "exec_help_failed", "reason": "codex exec --help failed", "command": []}
    has_prompt_arg = "prompt" in exec_help_text
    has_requested_sandbox = "sandbox" in exec_help_text and sandbox_mode in exec_help_text
    has_stdin_prompt = "stdin" in exec_help_text and ("`-`" in exec_help_text or " - " in f" {exec_help_text} ")
    if has_prompt_arg and has_requested_sandbox and has_stdin_prompt:
        mode_label = sandbox_mode.replace("-", "_")
        return {
            "supported": True,
            "mode": f"codex_exec_{mode_label}_stdin",
            "reason": f"codex exec help exposes prompt, stdin, and {sandbox_mode} sandbox",
            "command": [codex_path, "exec", "--sandbox", sandbox_mode, "-"],
        }
    return {
        "supported": False,
        "mode": "unsupported_help_shape",
        "reason": f"codex exec help did not expose a recognized {sandbox_mode} stdin non-interactive shape",
        "command": [],
    }


def probe_codex_workspace_write() -> dict:
    return probe_codex_noninteractive("workspace-write")


def probe_claude_noninteractive(sandbox_mode: str = "read-only") -> dict:
    # Real Claude CLI probe for read-only execution only. Workspace-write
    # Claude execution is intentionally not enabled and stays fail-closed.
    if sandbox_mode != "read-only":
        return {
            "supported": False,
            "mode": "not_configured",
            "reason": f"claude {sandbox_mode} execution is not enabled; only read-only is wired",
            "command": [],
        }
    if os.environ.get("OHB_EXEC_SIMULATE_CLAUDE") == "1":
        return {"supported": True, "mode": "simulated", "reason": "OHB_EXEC_SIMULATE_CLAUDE enabled", "command": []}
    claude_path = shutil.which("claude")
    if not claude_path:
        return {"supported": False, "mode": "not_configured", "reason": "claude command not found", "command": []}
    version = run_allowlisted_external_command([claude_path, "--version"], timeout=10)
    if version.get("returncode") != 0:
        return {
            "supported": False,
            "mode": "version_probe_failed",
            "reason": f"claude --version failed with returncode {version.get('returncode')}",
            "command": [],
        }
    help_result = run_allowlisted_external_command([claude_path, "--help"], timeout=10)
    help_text = f"{help_result.get('stdout', '')}\n{help_result.get('stderr', '')}".lower()
    if help_result.get("returncode") != 0 or "--print" not in help_text:
        return {
            "supported": False,
            "mode": "unsupported_help_shape",
            "reason": "claude --help did not expose a non-interactive print mode",
            "command": [],
        }
    version_label = safe_preview(str(version.get("stdout", "")).strip() or "unknown version", 60)
    return {
        "supported": True,
        "mode": "claude_print_read_only_stdin",
        "reason": f"claude CLI available ({version_label}); non-interactive print mode with prompt via stdin; no permission-bypass flags",
        "command": [claude_path, "-p"],
    }


def probe_claude_workspace_write() -> dict:
    # Narrow owner-approved write shape only. Availability checks are the
    # same as the read-only probe; only the command shape differs.
    base = probe_claude_noninteractive("read-only")
    if not base.get("supported"):
        return base
    if base.get("mode") == "simulated":
        return base
    claude_path = (base.get("command") or [None])[0] or shutil.which("claude")
    if not claude_path:
        return {"supported": False, "mode": "not_configured", "reason": "claude command not found", "command": []}
    return {
        "supported": True,
        "mode": "claude_print_workspace_write_stdin",
        "reason": "claude CLI available; narrow accept-edits print mode for owner-approved workspace-write; no permission-bypass flags",
        "command": [claude_path, "-p", "--permission-mode", "acceptEdits"],
    }


def route_auto_executor(text: str, owner_write: bool = False) -> tuple[str, str]:
    """Resolve an auto/default executor. Explicit targets never come here.

    Policy: Claude writes code, Codex reviews. With no clear signal the
    auto target stays on the current safe default (codex).
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    review_of_claude = any(
        re.search(r"\b(review|qa|audit|verify)\b", line, re.IGNORECASE)
        and re.search(r"\bclaude\b", line, re.IGNORECASE)
        for line in lines
    )
    if review_of_claude:
        return "codex", "auto routing: read-only review of claude-written work goes to codex"
    if owner_write:
        return "claude", "auto routing: owner-write code task prefers claude writer with codex review gate"
    if any(line_has_source_write_intent(line) or line_has_explicit_write_target(line) for line in lines):
        return "claude", "auto routing: code-writing signal prefers claude writer with codex review gate"
    return "codex", "auto routing: no clear code-writing signal; codex stays the safe default"


def executor_probe_for_target(target_executor: str) -> dict:
    if target_executor == "claude":
        return probe_claude_noninteractive()
    return probe_codex_noninteractive()


def executor_status_from_probe(target_executor: str, probe: dict) -> str:
    if probe.get("supported"):
        return "available"
    return "not_configured" if target_executor == "claude" else "unavailable"


def build_manual_start_hint(exec_id: str, dispatch_id: str, reason: str) -> str:
    return sanitize_sensitive_text(f"""Manual start required for {exec_id}
- reason: {reason}
- show package: /exec package {exec_id}
- approve workspace-write runner: /exec approve {dispatch_id} write
- approve by execution id: /exec approve {exec_id} write
- after manual return: /exec receive {exec_id}
- dispatch fallback: /dispatch package {dispatch_id}
- boundary: copy-only unless the user explicitly confirms executor-side actions.""")


def runner_output_limit(text: str, limit: int) -> str:
    clean = sanitize_sensitive_text(text)
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 80)].rstrip() + "\n...[truncated runner output]"


def collect_post_run_snapshot() -> str:
    commands = [
        ("git status --short", ["git", "status", "--short"]),
        ("git diff --stat", ["git", "diff", "--stat"]),
        ("git diff --cached --stat", ["git", "diff", "--cached", "--stat"]),
    ]
    lines = []
    for label, argv in commands:
        result = run_allowlisted_post_run_command(argv)
        lines.extend(
            [
                f"### {label}",
                f"- returncode: {result.get('returncode')}",
                "stdout:",
                "```text",
                runner_output_limit(str(result.get("stdout", "")).strip() or "- empty", 4000),
                "```",
                "stderr:",
                "```text",
                runner_output_limit(str(result.get("stderr", "")).strip() or "- empty", 2000),
                "```",
            ]
        )
    return sanitize_sensitive_text("\n".join(lines))


def normalize_fidelity_path(value: str) -> str:
    text = str(value or "").strip().strip("`'\"")
    text = text.strip(".,;:")
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def extract_declared_owner_write_targets(exec_id: str, dispatch_id: str) -> list[str]:
    try:
        exec_meta = task_metadata(read_exec(exec_id))
    except Exception:
        exec_meta = {}
    labels = (
        "allowed_write_targets",
        "allowed write targets",
        "allowed write paths",
        "write target",
        "target files",
        "target paths",
    )
    source_lines = [str(exec_meta.get("write_target_lines", ""))]
    try:
        source_lines.extend(owner_write_scope_lines(dispatch_id))
    except Exception:
        pass
    targets: list[str] = []
    path_pattern = re.compile(
        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)*\."
        r"(?:txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html))\b",
        re.IGNORECASE,
    )
    explicit_only_pattern = re.compile(
        r"\bcreate or update only\s+[`'\"]?([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)*\."
        r"(?:txt|md|json|py|yaml|yml|toml|cmd|bat|ps1|tsx?|jsx?|css|html))\b",
        re.IGNORECASE,
    )
    for block in source_lines:
        for line in block.splitlines():
            lowered = line.lower()
            if any(label in lowered for label in labels):
                for match in path_pattern.finditer(line):
                    target = normalize_fidelity_path(match.group(1))
                    if target and target not in targets:
                        targets.append(target)
                continue
            # Bridge's own explicit single-target declaration: accept only the
            # path immediately following the phrase, on the same physical line.
            # Strip trailing sentence punctuation from the captured target only.
            # No arbitrary unlabeled path harvesting.
            for match in explicit_only_pattern.finditer(line):
                target = normalize_fidelity_path(match.group(1).rstrip(".,;:)]"))
                if target and target not in targets:
                    targets.append(target)
    return targets


def post_run_snapshot_command_section(post_run_snapshot: str, label: str) -> str:
    pattern = re.compile(rf"(?ms)^### {re.escape(label)}\s*$.*?(?=^### |\Z)")
    match = pattern.search(str(post_run_snapshot or ""))
    return match.group(0) if match else ""


def post_run_snapshot_stdout_block(post_run_snapshot: str, label: str) -> str:
    section = post_run_snapshot_command_section(post_run_snapshot, label)
    match = re.search(r"(?ms)^stdout:\s*\n```text\n(.*?)\n```", section)
    return match.group(1) if match else ""


def post_run_snapshot_returncode(post_run_snapshot: str, label: str) -> int | None:
    section = post_run_snapshot_command_section(post_run_snapshot, label)
    match = re.search(r"(?m)^-\s*returncode:\s*(-?\d+)\s*$", section)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_git_status_short_paths(post_run_snapshot: str) -> list[str]:
    stdout_block = post_run_snapshot_stdout_block(post_run_snapshot, "git status --short")
    paths: list[str] = []
    for raw_line in stdout_block.splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^([ MADRCU?!]{1,2})\s+(.+)$", line)
        if not match:
            continue
        status = match.group(1)
        if not any(ch in status for ch in "MADRCU?!"):
            continue
        path_text = match.group(2).strip()
        if " -> " in path_text:
            path_text = path_text.rsplit(" -> ", 1)[1].strip()
        path = normalize_fidelity_path(path_text)
        if path and path not in paths:
            paths.append(path)
    return paths


def path_matches_owner_write_target(path: str, target: str) -> bool:
    clean_path = normalize_fidelity_path(path)
    clean_target = normalize_fidelity_path(target)
    return bool(clean_path and clean_target) and (
        clean_path == clean_target or clean_path.startswith(clean_target.rstrip("/") + "/")
    )


def owner_write_post_run_target_fidelity(
    exec_id: str, dispatch_id: str, post_run_snapshot: str, pre_run_snapshot: str = ""
) -> dict:
    targets = extract_declared_owner_write_targets(exec_id, dispatch_id)
    status_section = post_run_snapshot_command_section(post_run_snapshot, "git status --short")
    status_returncode = post_run_snapshot_returncode(post_run_snapshot, "git status --short")
    changed_paths = parse_git_status_short_paths(post_run_snapshot)
    baseline_paths = parse_git_status_short_paths(pre_run_snapshot) if pre_run_snapshot else []
    if "...[truncated runner output]" in status_section:
        return {
            "status": "failed",
            "targets": targets,
            "changed_paths": changed_paths,
            "unauthorized_paths": [],
            "reason": "snapshot_truncated: git status --short section was truncated",
            "git_status_returncode": status_returncode,
        }
    if not targets:
        return {
            "status": "failed",
            "targets": targets,
            "changed_paths": changed_paths,
            "unauthorized_paths": [],
            "reason": "target_unresolved: no declared allowed_write_targets/write target labels found",
            "git_status_returncode": status_returncode,
        }
    if status_returncode != 0:
        return {
            "status": "failed",
            "targets": targets,
            "changed_paths": changed_paths,
            "unauthorized_paths": [],
            "reason": "git_status_returncode_missing_or_failed",
            "git_status_returncode": status_returncode,
        }
    if pre_run_snapshot:
        # Baseline-aware mode: judge only paths that became dirty during the
        # run. Pre-existing working-tree dirt (developer edits, bridge
        # bookkeeping) is not evidence about this run; any NEW path outside
        # the declared targets still fails closed.
        new_paths = [path for path in changed_paths if path not in baseline_paths]
        unauthorized = [
            path for path in new_paths
            if not any(path_matches_owner_write_target(path, target) for target in targets)
        ]
        if unauthorized:
            status = "failed"
            reason = "post-run new changed paths outside declared targets"
        elif new_paths:
            status = "passed"
            reason = "post-run new changed paths stayed inside declared targets"
        else:
            status = "passed"
            reason = "no new changed paths beyond pre-run baseline"
        return {
            "status": status,
            "targets": targets,
            "changed_paths": changed_paths,
            "unauthorized_paths": unauthorized,
            "reason": reason,
            "git_status_returncode": status_returncode,
        }
    if not changed_paths:
        return {
            "status": "failed",
            "targets": targets,
            "changed_paths": changed_paths,
            "unauthorized_paths": [],
            "reason": "no_changed_files: owner-write produced no changed paths",
            "git_status_returncode": status_returncode,
        }
    unauthorized = [
        path for path in changed_paths
        if not any(path_matches_owner_write_target(path, target) for target in targets)
    ]
    matched_allowed = [
        path for path in changed_paths
        if any(path_matches_owner_write_target(path, target) for target in targets)
    ]
    if unauthorized:
        status = "failed"
        reason = "post-run changed paths outside declared targets"
    elif not matched_allowed:
        status = "failed"
        reason = "no_changed_files: no changed path matched declared targets"
    else:
        status = "passed"
        reason = "post-run changed paths stayed inside declared targets"
    return {
        "status": status,
        "targets": targets,
        "changed_paths": changed_paths,
        "unauthorized_paths": unauthorized,
        "reason": reason,
        "git_status_returncode": status_returncode,
    }


def extract_test_results_summary(stdout: str) -> str:
    text = sanitize_sensitive_text(stdout)
    pattern = re.compile(r"(?im)^(test results|tests?|测试结果)\s*:\s*$")
    match = pattern.search(text)
    if not match:
        return safe_preview(text, 800) or "- no stdout captured"
    next_match = re.search(
        r"(?im)^(key logs|unverified|unresolved risks|rollback notes|modified files|commands|关键日志|未验证|未解决风险)\s*:\s*$",
        text[match.end():],
    )
    end = match.end() + next_match.start() if next_match else len(text)
    return safe_preview(text[match.end():end].strip(), 1000) or "- test section empty"


def runner_output_has_payload(output: str, exec_id: str, dispatch_id: str, task_id: str) -> bool:
    lowered = sanitize_sensitive_text(output).lower()
    markers = [
        "# semi-auto execution package",
        f"exec_id: {exec_id}".lower(),
        f"dispatch_id: {dispatch_id}".lower(),
        f"task_id: {task_id}".lower(),
        "## dispatch package",
        "## goal",
        "## execution boundary",
    ]
    return sum(1 for marker in markers if marker and marker in lowered) >= 2


def stdout_has_valid_runner_output(stdout: str, dispatch_id: str, task_id: str) -> bool:
    clean = sanitize_sensitive_text(stdout).strip()
    lowered = clean.lower()
    if not clean:
        return False
    if "autorun-payload-ok" in lowered:
        return True
    has_ids = task_id.lower() in lowered and dispatch_id.lower() in lowered
    report_markers = (
        "execution summary",
        "modified files",
        "commands",
        "test results",
        "unverified",
        "unresolved risks",
    )
    return has_ids and sum(1 for marker in report_markers if marker in lowered) >= 3


def classify_runner_completion(exec_id: str, dispatch_id: str, task_id: str, result: dict) -> dict:
    stdout = sanitize_sensitive_text(result.get("stdout", ""))
    stderr = sanitize_sensitive_text(result.get("stderr", ""))
    output = f"{stdout}\n{stderr}"
    returncode = int(result.get("returncode") if str(result.get("returncode", "")).strip() else 0)
    timed_out = bool(result.get("timed_out")) or returncode == 124
    payload_seen = runner_output_has_payload(output, exec_id, dispatch_id, task_id)
    valid_stdout = stdout_has_valid_runner_output(stdout, dispatch_id, task_id)
    if returncode == 0:
        completion_state = "completed"
    elif timed_out and valid_stdout:
        completion_state = "timeout_with_output"
    elif timed_out and payload_seen:
        completion_state = "timeout_with_payload"
    elif timed_out:
        completion_state = "payload_missing"
    else:
        completion_state = "failed"
    return {
        "returncode": str(returncode),
        "timed_out": "true" if timed_out else "false",
        "stdout_chars": str(len(stdout)),
        "stderr_chars": str(len(stderr)),
        "completion_state": completion_state,
        "payload_state": "payload_seen" if payload_seen else "payload_missing",
        "has_valid_stdout": valid_stdout,
    }


def update_exec_fields(exec_id: str, fields: dict[str, str]) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    text = read_exec(normalized_exec_id)
    for key, value in fields.items():
        text = replace_task_field(text, key, value)
    write_exec(normalized_exec_id, text)
    return text


def persist_runner_result(exec_id: str, result: dict, probe: dict, completion: dict, post_run_snapshot: str = "") -> None:
    stdout = sanitize_sensitive_text(result.get("stdout", ""))
    stderr = sanitize_sensitive_text(result.get("stderr", ""))
    fields = {
        "runner_mode": str(probe.get("mode", "")),
        "auto_run_mode": str(probe.get("mode", "")),
        "runner_probe": safe_preview(str(probe.get("reason", "")), 120),
        "returncode": completion["returncode"],
        "timed_out": completion["timed_out"],
        "stdout_chars": completion["stdout_chars"],
        "stderr_chars": completion["stderr_chars"],
        "completion_state": completion["completion_state"],
        "payload_state": completion["payload_state"],
    }
    update_exec_fields(exec_id, fields)
    now = iso_now()
    append_exec_autostart_section(
        exec_id,
        "runner output",
        "\n".join(
            [
                f"- returncode: {completion['returncode']}",
                f"- timed_out: {completion['timed_out']}",
                f"- completion_state: {completion['completion_state']}",
                f"- payload_state: {completion['payload_state']}",
                f"- stdout_chars: {completion['stdout_chars']}",
                f"- stderr_chars: {completion['stderr_chars']}",
                f"- recorded_at: {now}",
            ]
        ),
    )
    text = read_exec(exec_id)
    text = append_to_section(
        text,
        "Runner Metadata",
        "\n".join(
            [
                f"### Runner result at {now}",
                f"- runner_mode: {probe.get('mode', '')}",
                f"- runner_probe: {safe_preview(str(probe.get('reason', '')), 160)}",
                f"- returncode: {completion['returncode']}",
                f"- timed_out: {completion['timed_out']}",
                f"- completion_state: {completion['completion_state']}",
                f"- payload_state: {completion['payload_state']}",
                f"- stdout_chars: {completion['stdout_chars']}",
                f"- stderr_chars: {completion['stderr_chars']}",
            ]
        ),
    )
    text = append_to_section(
        text,
        "Runner Stdout",
        f"### stdout at {now}\n```text\n{runner_output_limit(stdout, RUNNER_OUTPUT_RECORD_LIMIT) or '- empty'}\n```",
    )
    text = append_to_section(
        text,
        "Runner Stderr",
        f"### stderr at {now}\n```text\n{runner_output_limit(stderr, RUNNER_ERROR_RECORD_LIMIT) or '- empty'}\n```",
    )
    text = append_to_section(
        text,
        "Post-Run Snapshot",
        f"### snapshot at {now}\n{post_run_snapshot or '- snapshot unavailable'}",
    )
    text = append_to_section(
        text,
        "Runner Test Results",
        f"### test summary at {now}\n{extract_test_results_summary(stdout)}",
    )
    write_exec(exec_id, text)


def build_auto_runner_report(
    exec_id: str,
    dispatch_id: str,
    result: dict,
    mode: str,
    completion: dict | None = None,
    post_run_snapshot: str = "",
    sandbox_mode: str = "read-only",
) -> str:
    stdout = sanitize_sensitive_text(result.get("stdout", ""))
    stderr = sanitize_sensitive_text(result.get("stderr", ""))
    completion = completion or {"completion_state": "completed", "timed_out": "false"}
    stdout_summary = safe_preview(stdout, 1400) or "- none"
    stderr_summary = safe_preview(stderr, 800) or "- none"
    exec_meta = task_metadata(read_exec(exec_id))
    return sanitize_sensitive_text(f"""Task id: {exec_meta.get('task_id', '')}
Dispatch id: {dispatch_id}
Execution id: {exec_id}

Execution summary:
- Dispatch runner completed with mode={mode}.
- Target executor: {exec_meta.get('target_executor', '') or 'codex'}
- Executor status: {exec_meta.get('executor_status', '') or 'available'}
- Runner sandbox: {sandbox_mode}
- Return code: {result.get('returncode')}
- Timed out: {completion.get('timed_out', 'false')}
- Completion state: {completion.get('completion_state', 'completed')}
- Bridge did not run git add, commit, push, merge, deployment, or Docker actions.

Modified files:
- See post-run git status and diff snapshot below. Bridge does not claim unverified file changes.

Commands:
- codex non-interactive {sandbox_mode} runner with payload injected through stdin, or safe simulated runner.

Test results:
- stdout summary: {stdout_summary}
- stderr summary: {stderr_summary}

Key logs or screenshots:
- execution record: workbench/executions/{exec_id}.md

Post-run snapshot:
{safe_preview(post_run_snapshot, 1400) if post_run_snapshot else '- not captured'}

Unverified:
- Atlas review still required.
- Any executor claim must be checked against evidence.

Unresolved risks:
- Non-interactive executor output is summarized, not a full live UI proof.

Rollback notes:
- No project file rollback expected from Bridge read-only runner.
""")


def build_simulated_runner_result(exec_id: str, dispatch_id: str, payload: str = "") -> dict:
    return {
        "returncode": 0,
        "stdout": sanitize_sensitive_text(
            f"safe simulated returned: exec_id={exec_id} dispatch_id={dispatch_id}; read-only runner path exercised; payload_chars={len(payload)}."
        ),
        "stderr": "",
        "timed_out": False,
    }


def record_exec_failure_evidence(exec_id: str, dispatch_id: str, task_id: str, result: dict, completion: dict, post_run_snapshot: str) -> None:
    try:
        update_dispatch_status(
            dispatch_id,
            "failed",
            f"execution {exec_id} failed: {completion.get('completion_state')} returncode={completion.get('returncode')}",
        )
    except Exception:
        pass
    body = sanitize_sensitive_text(
        f"""Execution failure evidence
exec_id: {exec_id}
dispatch_id: {dispatch_id}
returncode: {completion.get('returncode')}
timed_out: {completion.get('timed_out')}
completion_state: {completion.get('completion_state')}
payload_state: {completion.get('payload_state')}
stdout_chars: {completion.get('stdout_chars')}
stderr_chars: {completion.get('stderr_chars')}

stdout_summary:
{safe_preview(result.get('stdout', ''), 1200) or '- none'}

stderr_summary:
{safe_preview(result.get('stderr', ''), 800) or '- none'}

post_run_snapshot:
{safe_preview(post_run_snapshot, 1200) if post_run_snapshot else '- not captured'}
"""
    )
    try:
        create_evidence_entry(task_id, "command", body, source="exec_runner", verified="no", sync_task=True)
    except Exception:
        pass


CODEX_REVIEW_VERDICTS = {"pass_candidate", "needs_revision", "failed"}


def build_codex_review_payload(exec_id: str, dispatch_id: str, result: dict, post_run_snapshot: str) -> str:
    targets = extract_declared_owner_write_targets(exec_id, dispatch_id)
    target_lines = "\n".join(f"- {target}" for target in targets) or "- none"
    return sanitize_sensitive_text(
        f"""# Codex Read-Only Review of Claude Write
exec_id: {exec_id}
dispatch_id: {dispatch_id}
writer_executor: claude
reviewer_executor: codex

You are Codex acting as a read-only reviewer of work written by Claude.
Do not modify any files. Do not run git add/commit/push. Do not deploy.
Review the Claude write evidence below against the declared targets.

## Declared allowed_write_targets
{target_lines}

## Claude runner stdout
{runner_output_limit(str(result.get('stdout', '')), 4000) or '- empty'}

## Claude runner stderr
{runner_output_limit(str(result.get('stderr', '')), 1500) or '- empty'}

## Post-run snapshot (changed paths evidence)
{runner_output_limit(post_run_snapshot or '- not captured', 3000)}

## Required verdict format
Reply with exactly one line in this form, then evidence-based reasons:
review_verdict: pass_candidate OR needs_revision OR failed
"""
    )


def run_codex_review_of_claude_write(exec_id: str, dispatch_id: str, result: dict, post_run_snapshot: str) -> dict:
    probe = probe_codex_noninteractive("read-only")
    command = list(probe.get("command") or [])
    command_attempted = " ".join(command) or "none"
    if not probe.get("supported"):
        return {
            "status": "unavailable",
            "reason": f"codex reviewer unavailable: {probe.get('reason', 'unknown')}",
            "command_attempted": "none",
            "returncode": "none",
        }
    if probe.get("mode") == "simulated" or not command:
        return {
            "status": "inconclusive",
            "reason": "codex reviewer has no real read-only command; no review verdict",
            "command_attempted": command_attempted,
            "returncode": "none",
        }
    payload = build_codex_review_payload(exec_id, dispatch_id, result, post_run_snapshot)
    review_result = run_allowlisted_external_command(command, input_text=payload, timeout=EXEC_START_TIMEOUT_SECONDS)
    returncode = str(review_result.get("returncode"))
    if review_result.get("returncode") != 0:
        return {
            "status": "failed",
            "reason": f"codex review command failed with returncode {returncode}",
            "command_attempted": command_attempted,
            "returncode": returncode,
        }
    output = f"{review_result.get('stdout', '')}\n{review_result.get('stderr', '')}"
    match = re.search(r"review_verdict:\s*(pass_candidate|needs_revision|failed)\b", output, re.IGNORECASE)
    if not match:
        return {
            "status": "inconclusive",
            "reason": "codex review returned no recognizable review_verdict line",
            "command_attempted": command_attempted,
            "returncode": returncode,
        }
    verdict = match.group(1).lower()
    return {
        "status": verdict,
        "reason": f"codex read-only review verdict: {verdict}",
        "command_attempted": command_attempted,
        "returncode": returncode,
    }


def execute_exec_runner(
    exec_id: str,
    dispatch_id: str,
    probe: dict,
    sandbox_mode: str,
    approval_note: str = "",
    owner_write_metadata: dict[str, str] | None = None,
) -> str:
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    sandbox_mode = sandbox_mode if sandbox_mode in RUNNER_SANDBOX_MODES else "read-only"
    now = iso_now()
    write_mode = sandbox_mode == "workspace-write"
    owner_write_fields = {
        str(key): sanitize_sensitive_text(value)
        for key, value in (owner_write_metadata or {}).items()
    }
    owner_write_policy = str(owner_write_fields.get("owner_write_policy", "")).lower() == "true"
    run_policy = run_policy_for_sandbox(sandbox_mode, owner_write_policy=owner_write_policy)
    target_executor = task_metadata(read_exec(exec_id)).get("target_executor", "").strip().lower() or "codex"
    extra_fields = {
        "started_at": now,
        "executor_status": executor_status_from_probe(target_executor, probe),
        "executor_reason": safe_preview(str(probe.get("reason", "")), 160),
        "command_attempted": " ".join(probe.get("command") or []) or "none",
        "human_confirm_required": str(run_policy.human_confirm_required).lower(),
        "auto_execute_enabled": str(run_policy.auto_execute_enabled).lower(),
        "read_only_auto_run": "false" if write_mode else "true",
        "auto_run_mode": str(probe.get("mode", "unknown")),
        "runner_mode": str(probe.get("mode", "unknown")),
        "runner_sandbox": sandbox_mode,
        "runner_probe": safe_preview(probe.get("reason", ""), 120),
        "completion_state": "started",
        **run_policy_fields(run_policy, str(probe.get("reason", ""))),
    }
    if owner_write_fields:
        extra_fields.update(owner_write_fields)
        extra_fields["owner_write_policy_status"] = owner_write_fields.get("owner_write_policy_status", "started") or "started"
    if write_mode:
        extra_fields.update({"write_confirmed": "true", "write_approved_at": now})
    update_exec_status(
        exec_id,
        "started",
        f"{sandbox_mode} runner started via {probe.get('mode')}",
        extra_fields,
    )
    if write_mode:
        text = read_exec(exec_id)
        owner_lines = ""
        if owner_write_fields:
            owner_lines = (
                f"\n- owner_write_policy: {owner_write_fields.get('owner_write_policy', 'false')}"
                f"\n- owner_write_policy_status: {owner_write_fields.get('owner_write_policy_status', 'started')}"
                f"\n- write_target_fidelity: {owner_write_fields.get('write_target_fidelity', '') or 'none'}"
            )
        text = append_to_section(
            text,
            "Human Write Approval",
            f"### write approved at {now}\n- approval: write\n- runner_sandbox: workspace-write\n- run_policy: {run_policy.name}\n- note: {sanitize_sensitive_text(approval_note).strip() or 'explicit write approval'}{owner_lines}\n- forbidden_git_write_actions: true\n- deploy_forbidden: true",
        )
        write_exec(exec_id, text)
    append_exec_autostart_section(
        exec_id,
        "started",
        f"- mode: {probe.get('mode')}\n- run_policy: {run_policy.name}\n- runner_sandbox: {sandbox_mode}\n- read_only_gate: {run_policy.read_only_gate_label}\n- command_allowlist: true\n- payload_injection: stdin\n- started_at: {now}",
    )
    package = build_runner_payload(
        exec_id,
        normalized_dispatch_id,
        sandbox_mode,
        owner_write_policy=owner_write_policy,
    )
    if target_executor == "claude":
        update_exec_fields(
            exec_id,
            {
                "source_language": detect_source_language(claude_source_request_text(normalized_dispatch_id)),
                "executor_prompt_language": "en",
                "executor_prompt_rendered_for": "claude",
            },
        )
    pre_run_snapshot = collect_post_run_snapshot() if owner_write_policy else ""
    if probe.get("mode") == "simulated":
        result = build_simulated_runner_result(exec_id, normalized_dispatch_id, package)
    else:
        command = list(probe.get("command") or [])
        if not command:
            reason = "probe returned no command"
            hint = mark_exec_needs_manual_start(exec_id, normalized_dispatch_id, reason)
            return f"""Execution needs manual start: {exec_id}
- status: needs_manual_start
- reason: {reason}
- next: /exec package {exec_id}

{hint}"""
        result = run_allowlisted_external_command(command, input_text=package, timeout=EXEC_START_TIMEOUT_SECONDS)
    task_id = task_metadata(read_exec(exec_id)).get("task_id", "")
    completion = classify_runner_completion(exec_id, normalized_dispatch_id, task_id, result)
    post_run_snapshot = collect_post_run_snapshot()
    owner_fidelity_result = None
    if owner_write_policy:
        owner_fidelity_result = owner_write_post_run_target_fidelity(
            exec_id, normalized_dispatch_id, post_run_snapshot, pre_run_snapshot
        )
        update_exec_fields(
            exec_id,
            {
                # write_target_fidelity stays the preflight gate verdict;
                # the post-run verdict is gated via post_run_target_fidelity
                # in every owner-write hard gate.
                "post_run_target_fidelity": owner_fidelity_result["status"],
                "write_target_lines": safe_preview(
                    "; ".join(owner_fidelity_result.get("targets", []))
                    or owner_write_fields.get("write_target_lines", "")
                    or "none",
                    240,
                ),
            },
        )
    persist_runner_result(exec_id, result, probe, completion, post_run_snapshot)
    if owner_write_policy:
        owner_status = (
            "returned"
            if completion["completion_state"] in {"completed", "timeout_with_output"}
            else "failed"
            if int(completion["returncode"]) != 0
            else completion["completion_state"]
        )
        update_exec_fields(exec_id, {"owner_write_policy_status": owner_status})
    if write_mode and target_executor == "claude":
        # Claude wrote; Codex must review read-only before any auto close.
        review = run_codex_review_of_claude_write(exec_id, normalized_dispatch_id, result, post_run_snapshot)
        update_exec_fields(
            exec_id,
            {
                "codex_review_status": review["status"],
                "codex_review_reason": safe_preview(str(review.get("reason", "")), 200),
            },
        )
        append_exec_autostart_section(
            exec_id,
            "codex read-only review of claude write",
            f"- codex_review_status: {review['status']}\n"
            f"- codex_review_reason: {safe_preview(str(review.get('reason', '')), 200)}\n"
            f"- review_command_attempted: {review.get('command_attempted', 'none')}\n"
            f"- review_returncode: {review.get('returncode', 'none')}\n"
            "- review_sandbox: read-only",
        )
    if completion["completion_state"] in {"completed", "timeout_with_output"}:
        report = build_auto_runner_report(
            exec_id,
            normalized_dispatch_id,
            result,
            str(probe.get("mode", "unknown")),
            completion,
            post_run_snapshot,
            sandbox_mode,
        )
        receive_reply = build_exec_receive_reply(exec_id, report)
        auto_postprocess_reply = build_exec_auto_postprocess_reply(
            exec_id,
            normalized_dispatch_id,
            completion,
            sandbox_mode=sandbox_mode,
            receive_synced="synced_dispatch_receive: true" in receive_reply,
        )
        if completion["completion_state"] == "timeout_with_output":
            update_exec_fields(
                exec_id,
                {
                    "completion_state": "timeout_with_output",
                    "timed_out": "true",
                    "returncode": completion["returncode"],
                    "stdout_chars": completion["stdout_chars"],
                    "stderr_chars": completion["stderr_chars"],
                    "runner_sandbox": sandbox_mode,
                },
            )
            append_exec_autostart_section(
                exec_id,
                "timeout with output",
                "- timeout_with_output: true\n- stdout was preserved and synced through return report\n- next: /dispatch qa then /task review",
            )
        log_event("exec_auto_returned", exec_id=exec_id, dispatch_id=normalized_dispatch_id)
        state_label = "timed out with output" if completion["completion_state"] == "timeout_with_output" else "returned"
        response_title = "Execution auto-run" if sandbox_mode == "read-only" else f"Execution {sandbox_mode} runner"
        return f"""{response_title} {state_label}: {exec_id}
- status: returned
- dispatch_id: {normalized_dispatch_id}
- run_policy: {run_policy.name}
- read_only_gate: {run_policy.read_only_gate_label}
- runner_sandbox: {sandbox_mode}
- runner_mode: {probe.get('mode')}
- returncode: {completion['returncode']}
- timed_out: {completion['timed_out']}
- completion_state: {completion['completion_state']}
- stdout_chars: {completion['stdout_chars']}
- stderr_chars: {completion['stderr_chars']}
- stdout_summary: {safe_preview(result.get('stdout', ''), 360) or 'none'}
- stderr_summary: {safe_preview(result.get('stderr', ''), 240) or 'none'}
- dispatch_receive_synced: true
- evidence_intake: generated through dispatch/task report chain
- post_run_snapshot: recorded
- owner_write_post_run_fidelity: {owner_fidelity_result.get('status') if owner_fidelity_result else 'not_applicable'}
- no_git_add_commit_push: true
- deploy_forbidden: true
- next: /dispatch qa {normalized_dispatch_id}, then /task review {task_metadata(read_exec(exec_id)).get('task_id', '')}

{safe_preview(receive_reply, 700)}

{auto_postprocess_reply}"""
    if result.get("returncode") != 0:
        update_exec_status(
            exec_id,
            "failed",
            f"{sandbox_mode} runner {completion['completion_state']} returncode={completion['returncode']}",
            {
                "auto_execute_enabled": "false",
                "completion_state": completion["completion_state"],
                "timed_out": completion["timed_out"],
                "returncode": completion["returncode"],
                "runner_sandbox": sandbox_mode,
                **run_policy_fields(run_policy, str(probe.get("reason", ""))),
            },
        )
        record_exec_failure_evidence(exec_id, normalized_dispatch_id, task_id, result, completion, post_run_snapshot)
        log_event("exec_failed", exec_id=exec_id, dispatch_id=normalized_dispatch_id)
        response_title = "Execution auto-run" if sandbox_mode == "read-only" else f"Execution {sandbox_mode} runner"
        auto_postprocess_reply = build_exec_auto_postprocess_reply(
            exec_id,
            normalized_dispatch_id,
            completion,
            sandbox_mode=sandbox_mode,
            receive_synced=False,
        )
        return f"""{response_title} failed: {exec_id}
- status: failed
- dispatch_id: {normalized_dispatch_id}
- run_policy: {run_policy.name}
- runner_sandbox: {sandbox_mode}
- returncode: {completion['returncode']}
- timed_out: {completion['timed_out']}
- completion_state: {completion['completion_state']}
- payload_state: {completion['payload_state']}
- stdout_chars: {completion['stdout_chars']}
- stderr_chars: {completion['stderr_chars']}
- stdout_summary: {safe_preview(result.get('stdout', ''), 300) or 'none'}
- stderr_summary: {safe_preview(result.get('stderr', ''), 300) or 'none'}
- post_run_snapshot: recorded
- failure_evidence: recorded
- no_git_write_actions_by_bridge: true
- next: inspect /exec show {exec_id}; if payload_state=payload_seen rerun with shorter output or paste saved stdout via /exec receive; if payload_missing use /exec package manually

{auto_postprocess_reply}"""
    return f"""Execution {sandbox_mode} runner ended without return sync: {exec_id}
- status: started
- completion_state: {completion['completion_state']}
- next: /exec show {exec_id}"""


def append_exec_autostart_section(exec_id: str, title: str, body: str) -> None:
    text = read_exec(exec_id)
    text = append_to_section(text, "Auto Start", f"### {title} at {iso_now()}\n{sanitize_sensitive_text(body)}")
    write_exec(exec_id, text)


def update_exec_status(exec_id: str, status: str, timeline: str, extra_fields: dict[str, str] | None = None) -> str:
    if status not in EXEC_STATUSES:
        raise ValueError("invalid execution status")
    normalized_exec_id = normalize_exec_id(exec_id)
    text = read_exec(normalized_exec_id)
    now = iso_now()
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", now)
    if extra_fields:
        for key, value in extra_fields.items():
            text = replace_task_field(text, key, value)
    text = append_to_section(text, "Status Timeline", f"- {now} {timeline}")
    write_exec(normalized_exec_id, text)
    return text


def exec_next_action(record: dict | None) -> str:
    if not record:
        return "prepare execution session: /exec prepare <dispatch_id>"
    exec_id = record.get("exec_id", "")
    status = record.get("status", "unknown")
    completion_state = record.get("completion_state", "")
    if status == "returned" and completion_state == "timeout_with_output":
        return f"timeout produced usable stdout and was synced; run /dispatch qa {record.get('dispatch_id', '')}, then /task review {record.get('task_id', '')}"
    if status == "failed" and completion_state == "timeout_with_payload":
        return f"payload reached runner but output was incomplete; inspect /exec show {exec_id}, then rerun with shorter output or manually /exec receive"
    if status == "failed" and completion_state == "payload_missing":
        return f"payload was not observed in runner output; use /exec package {exec_id} for manual copy or rerun after checking Codex CLI"
    if status == "prepared":
        return f"start read-only runner with /exec start {record.get('dispatch_id', '')}, or show payload with /exec package {exec_id}"
    if status == "started":
        return f"wait for auto runner output; if stuck inspect /exec show {exec_id}"
    if status == "needs_manual_start":
        return f"manual start required; approve workspace-write with /exec approve {record.get('dispatch_id', '')} write, or /exec approve {exec_id} write; then /exec receive {exec_id}"
    if status in {"opened", "copied"}:
        return f"wait for executor return; when ready use /exec receive {exec_id}"
    if status == "returned":
        dispatch_id = record.get("dispatch_id", "")
        return f"run /dispatch qa {dispatch_id}, then /task review {record.get('task_id', '')}"
    if status in {"failed", "cancelled"}:
        return f"inspect {exec_id}; prepare a new session if the dispatch still needs execution"
    return "inspect /exec show and /dispatch show first"


def build_exec_help_reply() -> str:
    return """Atlas execution commands
- /exec help
- /exec prepare <dispatch_id>
- /exec start <dispatch_id>
- /exec approve <exec_id|dispatch_id> write
- /exec approve-latest write
- /exec package <exec_id>
- /exec mark <exec_id> copied <note>
- /exec mark <exec_id> opened <note>
- /exec receive <exec_id>
  <pasted Codex/Kiro return report>
- /exec cancel <exec_id> <note>
- /exec fail <exec_id> <note>
- /exec show <exec_id>
- /exec list
- /exec dashboard
- /exec stale

Boundary:
- read-only auto-run only for dispatches with explicit read-only / no-code-change boundaries
- write or modification tasks degrade to needs_manual_start
- workspace-write runner requires explicit /exec approve <exec_id|dispatch_id> write
- execution records expose run_policy: manual_confirmation, read_only_auto_start, approved_workspace_write, or owner_approved_workspace_write
- dispatch-id approval is the fast path; it reuses a prepared/manual-start exec or creates one when no execution exists yet
- approve-latest approves only the newest prepared/manual-start Codex execution; it does not bypass write gates
- human_confirm_required: true for ordinary write/modify/deploy tasks; accepted codex-write owner runs record owner_approved_workspace_write
- external_execution_enabled: false
- auto_execute_enabled: false for arbitrary tasks
- read_only_auto_exec_enabled: true
- probes Codex non-interactive support before any auto run
- sends the full execution payload through stdin, not as a title-only prompt argument
- forbids git add/commit/push/merge and deploy in workspace-write prompt and Bridge post-run commands
- does not execute arbitrary local commands from package content
- does not read .env or print tokens/cookies/secrets
- writes only workbench execution/dispatch/task records"""


def build_exec_prepare_reply(dispatch_id: str) -> str:
    parts = str(dispatch_id or "").strip().split()
    if len(parts) != 1:
        return "Usage: /exec prepare <dispatch_id>"
    exec_id, text = create_exec_session(parts[0])
    meta = task_metadata(text)
    return f"""Execution session prepared: {exec_id}
- status: prepared
- dispatch_id: {normalize_dispatch_id(parts[0])}
- task_id: {meta.get('task_id', '')}
- target_executor: {meta.get('target_executor', '')}
- run_policy: {RUN_POLICY_MANUAL.name}
- human_confirm_required: true
- external_execution_enabled: false
- auto_execute_enabled: false
- path: workbench/executions/{exec_id}.md
- next: /exec package {exec_id}"""


def mark_exec_needs_manual_start(exec_id: str, dispatch_id: str, reason: str) -> str:
    now = iso_now()
    text = update_exec_status(
        exec_id,
        "needs_manual_start",
        f"auto start refused or unavailable: {reason}",
        {
            "started_at": "",
            "human_confirm_required": "true",
            "auto_execute_enabled": "false",
            "read_only_auto_run": "false",
            "auto_run_mode": "manual",
            "runner_mode": "manual",
            "runner_probe": safe_preview(reason, 120),
            "completion_state": "needs_manual_start",
            **run_policy_fields(RUN_POLICY_MANUAL, reason),
        },
    )
    hint = build_manual_start_hint(exec_id, dispatch_id, reason)
    text = append_to_section(text, "Auto Start", f"### needs_manual_start at {now}\n{hint}")
    write_exec(exec_id, text)
    return hint


def build_exec_start_reply(dispatch_id: str) -> str:
    parts = str(dispatch_id or "").strip().split()
    if len(parts) != 1:
        return "Usage: /exec start <dispatch_id>"
    normalized_dispatch_id = normalize_dispatch_id(parts[0])
    exec_id, _text = create_exec_session(normalized_dispatch_id)
    gate = read_only_dispatch_gate(normalized_dispatch_id)
    if not gate["ok"]:
        reason = f"{gate['reason']}; human_confirm_required=true"
        if gate.get("write_intent_lines"):
            reason += f"; write_intent={safe_preview('; '.join(gate['write_intent_lines']), 220)}"
        hint = mark_exec_needs_manual_start(exec_id, normalized_dispatch_id, reason)
        log_event("exec_needs_manual_start", exec_id=exec_id, dispatch_id=normalized_dispatch_id)
        return f"""Execution start requires manual confirmation: {exec_id}
- status: needs_manual_start
- dispatch_id: {normalized_dispatch_id}
- run_policy: {RUN_POLICY_MANUAL.name}
- read_only_gate: failed
- reason: {safe_preview(reason, 260)}
- human_confirm_required: true
- auto_execute_enabled: false
- next: /exec approve {normalized_dispatch_id} write OR /exec approve {exec_id} write OR /exec package {exec_id}

{hint}"""
    target_executor = task_metadata(read_exec(exec_id)).get("target_executor", "").strip().lower() or "codex"
    probe = executor_probe_for_target(target_executor)
    executor_status = executor_status_from_probe(target_executor, probe)
    command_attempted = " ".join(probe.get("command") or []) or "none"
    update_exec_fields(
        exec_id,
        {
            "executor_status": executor_status,
            "executor_reason": safe_preview(str(probe.get("reason", "")), 160),
            "command_attempted": command_attempted,
        },
    )
    if not probe.get("supported"):
        reason = f"{target_executor} non-interactive unsupported: {probe.get('reason', 'unknown')}"
        hint = mark_exec_needs_manual_start(exec_id, normalized_dispatch_id, reason)
        log_event("exec_needs_manual_start", exec_id=exec_id, dispatch_id=normalized_dispatch_id)
        return f"""Execution needs manual start: {exec_id}
- status: needs_manual_start
- dispatch_id: {normalized_dispatch_id}
- run_policy: {RUN_POLICY_MANUAL.name}
- read_only_gate: passed
- target_executor: {target_executor}
- executor_status: {executor_status}
- executor_reason: {safe_preview(str(probe.get('reason', 'unknown')), 200)}
- command_attempted: {command_attempted}
- runner_probe: {safe_preview(probe.get('mode', 'unknown'), 80)}
- reason: {safe_preview(reason, 260)}
- human_confirm_required: true
- auto_execute_enabled: false
- next: /exec package {exec_id}

{hint}"""
    return execute_exec_runner(exec_id, normalized_dispatch_id, probe, "read-only")


def build_exec_approve_reply(tail: str, owner_write_metadata: dict[str, str] | None = None) -> str:
    parts = str(tail or "").strip().split(maxsplit=2)
    if len(parts) < 2 or parts[1].lower() != "write":
        return "Usage: /exec approve <exec_id|dispatch_id> write"
    if re.fullmatch(r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?", parts[0]):
        dispatch_id = normalize_dispatch_id(parts[0])
        gate = write_approval_gate(dispatch_id)
        if not gate["ok"]:
            reason = f"{gate['reason']}: {safe_preview('; '.join(gate.get('forbidden_lines', [])), 260)}"
            record = latest_exec_for_dispatch(dispatch_id)
            exec_id = "none"
            status = "refused"
            next_action = f"no execution created; revise the request or use /dispatch package {dispatch_id} for manual handling"
            if record and record.get("status") in WRITE_APPROVAL_ELIGIBLE_STATUSES:
                exec_id = normalize_exec_id(record.get("exec_id", ""))
                status = "needs_manual_start"
                update_exec_status(
                    exec_id,
                    "needs_manual_start",
                    f"write approval refused before execution: {reason}",
                    {
                        "auto_execute_enabled": "false",
                        "write_confirmed": "false",
                        "runner_sandbox": "manual",
                        "completion_state": "write_approval_refused",
                        **run_policy_fields(RUN_POLICY_MANUAL, reason),
                    },
                )
                next_action = f"/exec package {exec_id}"
            return f"""Execution write approval refused before execution
- status: {status}
- dispatch_id: {dispatch_id}
- exec_id: {exec_id}
- run_policy: {RUN_POLICY_MANUAL.name}
- reason: {safe_preview(reason, 320)}
- approval_preflight_noop: true
- forbidden_git_write_actions: true
- deploy_forbidden: true
- next: {next_action}"""
    exec_id, dispatch_id, created_from_dispatch = resolve_write_approval_target(parts[0])
    approval_note = parts[2] if len(parts) > 2 else (
        "explicit user write approval via dispatch_id" if parts[0].startswith("DISPATCH-") else "explicit user write approval"
    )
    text = read_exec(exec_id)
    meta = task_metadata(text)
    status = meta.get("status", "unknown")
    if status not in WRITE_APPROVAL_ELIGIBLE_STATUSES:
        return f"""Execution approval refused: {exec_id}
- status: {status}
- required_status: prepared OR needs_manual_start
- reason: write approval requires an explicit prepared/manual-start execution or a dispatch id with no existing execution"""
    target_executor = meta.get("target_executor", "").lower()
    requested_owner_policy = str((owner_write_metadata or {}).get("owner_write_policy", "")).lower() == "true"
    if target_executor == "claude":
        # Claude workspace-write is allowed only under explicit owner-approved
        # write policy with resolvable declared allowed_write_targets.
        # Claude-written work then requires Codex review before auto close.
        claude_refusal_reason = ""
        if not requested_owner_policy:
            claude_refusal_reason = (
                "claude workspace-write requires explicit owner write policy (/run claude-write); "
                "plain write approval stays codex-only"
            )
        else:
            declared_targets = extract_declared_owner_write_targets(exec_id, dispatch_id)
            if not declared_targets:
                claude_refusal_reason = (
                    "claude workspace-write refused: declared allowed_write_targets missing or unresolved"
                )
        if claude_refusal_reason:
            return f"""Execution approval refused: {exec_id}
- target_executor: claude
- executor_status: not_configured
- executor_reason: {claude_refusal_reason}
- command_attempted: none
- reason: {claude_refusal_reason}
- next: /exec package {exec_id}"""
    elif target_executor != "codex":
        executor_status = "unavailable"
        return f"""Execution approval refused: {exec_id}
- target_executor: {target_executor or 'unknown'}
- executor_status: {executor_status}
- executor_reason: workspace-write runner is currently supported only for Codex and owner-approved Claude dispatches
- command_attempted: none
- reason: workspace-write runner is currently supported only for Codex and owner-approved Claude dispatches
- next: /exec package {exec_id}"""
    gate = write_approval_gate(dispatch_id)
    if not gate["ok"]:
        reason = f"{gate['reason']}: {safe_preview('; '.join(gate.get('forbidden_lines', [])), 260)}"
        update_exec_status(
            exec_id,
            "needs_manual_start",
            f"write approval refused: {reason}",
            {
                "auto_execute_enabled": "false",
                "write_confirmed": "false",
                "runner_sandbox": "manual",
                "completion_state": "write_approval_refused",
                **run_policy_fields(RUN_POLICY_MANUAL, reason),
            },
        )
        return f"""Execution write approval refused: {exec_id}
- status: needs_manual_start
- run_policy: {RUN_POLICY_MANUAL.name}
- reason: {safe_preview(reason, 320)}
- forbidden_git_write_actions: true
- deploy_forbidden: true
- next: /exec package {exec_id}"""
    owner_write_fields = {
        str(key): sanitize_sensitive_text(value)
        for key, value in (owner_write_metadata or {}).items()
    }
    if target_executor == "claude":
        # Claude writes; Codex must review before any auto close.
        owner_write_fields.update(
            {
                "writer_executor": "claude",
                "reviewer_executor": "codex",
                "review_required_by": "codex",
                "codex_review_status": "pending",
            }
        )
    if owner_write_fields:
        update_exec_fields(exec_id, owner_write_fields)
    probe = probe_claude_workspace_write() if target_executor == "claude" else probe_codex_workspace_write()
    if not probe.get("supported"):
        reason = f"{target_executor} workspace-write non-interactive unsupported: {probe.get('reason', 'unknown')}"
        extra_fields = {
            "auto_execute_enabled": "false",
            "write_confirmed": "true",
            "write_approved_at": iso_now(),
            "runner_sandbox": "workspace-write",
            "runner_probe": safe_preview(reason, 120),
            "completion_state": "needs_manual_start",
            **run_policy_fields(RUN_POLICY_MANUAL, reason),
        }
        if owner_write_fields:
            extra_fields.update(owner_write_fields)
            extra_fields["owner_write_policy_status"] = "needs_manual_start"
        update_exec_status(
            exec_id,
            "needs_manual_start",
            f"write approval could not start: {reason}",
            extra_fields,
        )
        hint = build_manual_start_hint(exec_id, dispatch_id, reason)
        return f"""Execution write approval needs manual start: {exec_id}
- status: needs_manual_start
- run_policy: {RUN_POLICY_MANUAL.name}
- runner_sandbox: workspace-write
- reason: {safe_preview(reason, 260)}
- created_exec_for_dispatch: {str(created_from_dispatch).lower()}
- next: /exec package {exec_id}

{hint}"""
    runner_reply = execute_exec_runner(
        exec_id,
        dispatch_id,
        probe,
        "workspace-write",
        approval_note,
        owner_write_fields,
    )
    return f"""{runner_reply}

Write approval UX:
- approval_target: {parts[0]}
- resolved_exec_id: {exec_id}
- created_exec_for_dispatch: {str(created_from_dispatch).lower()}"""


def build_exec_approve_latest_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if not parts or parts[0].lower() != "write":
        return "Usage: /exec approve-latest write"
    record = latest_write_approval_candidate()
    if not record:
        latest = exec_records()[:1]
        if not latest:
            return """Execution approve-latest refused
- reason: no execution sessions found
- required_status: prepared OR needs_manual_start
- next: /exec prepare <dispatch_id> OR /exec start <dispatch_id>"""
        latest_record = latest[0]
        latest_dispatch_id = latest_record.get("dispatch_id", "")
        return f"""Execution approve-latest refused
- reason: no prepared/manual-start Codex execution found
- latest_exec_id: {latest_record.get('exec_id', '')}
- latest_status: {latest_record.get('status', 'unknown')}
- latest_target_executor: {latest_record.get('target_executor', '') or 'unknown'}
- required_status: prepared OR needs_manual_start
- next: /exec start {latest_dispatch_id} OR /exec approve {latest_dispatch_id} write"""
    note = parts[1] if len(parts) > 1 else "explicit user write approval via approve-latest"
    reply = build_exec_approve_reply(f"{record['exec_id']} write {note}")
    return f"""{reply}

One command task run:
- approval_target: approve-latest
- resolved_exec_id: {record['exec_id']}
- resolved_dispatch_id: {record.get('dispatch_id', '')}
- selected_status: {record.get('status', 'unknown')}
- selected_task_id: {record.get('task_id', '')}"""


def build_run_help_reply() -> str:
    return """Atlas one-command run
- /run help
- /run codex <task title> [--project <project_id>]
- /run codex-write <task title> [--project <project_id>]

Boundary:
- creates a local task, creates a Codex dispatch with context, then starts the existing execution flow
- read-only/no-change tasks may use the guarded read-only Codex runner
- write/modify/deploy tasks stop at needs_manual_start and require explicit /exec approve <exec_id|dispatch_id> write or /exec approve-latest write
- codex-write is the owner write policy path: it is an explicit owner workspace-write approval for targeted write tasks with run_policy=owner_approved_workspace_write
- codex-write bypasses read-only start only after owner preflight accepts an explicit safe write target; hard-deny, forbidden, or targetless requests refuse before execution creation
- accepted codex-write requests still use the write approval gate, command allowlist, stdin payload checks, and post-run evidence checks
- does not run git add/commit/push/merge, deploy, package managers, shells, or arbitrary commands
- does not read .env or print tokens/cookies/secrets
- writes only workbench task/dispatch/context/execution/evidence records"""


def reply_field(reply: str, field: str) -> str:
    prefix = f"- {field}:"
    for line in str(reply or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return ""


def create_codex_dispatch(
    task_id: str,
    with_context: bool = True,
    target: str = "codex",
    requested_executor: str = "",
    routing_reason: str = "",
) -> tuple[str, dict]:
    normalized_task_id = normalize_task_id(task_id)
    clean_target = str(target or "codex").strip().lower()
    if clean_target not in SUPPORTED_EXECUTOR_TARGETS:
        raise ValueError("target_executor must be codex, claude, or kiro")
    read_task(normalized_task_id)
    dispatch_id = generate_dispatch_id()
    markdown = build_dispatch_markdown(
        dispatch_id,
        normalized_task_id,
        clean_target,
        with_context,
        requested_executor=requested_executor,
        routing_reason=routing_reason,
    )
    write_dispatch(dispatch_id, markdown)
    meta = task_metadata(markdown)
    log_event("dispatch_created", dispatch_id=dispatch_id, task_id=normalized_task_id, target_executor=clean_target)
    return dispatch_id, meta


def one_command_next_action(record: dict | None, dispatch_id: str) -> str:
    if not record:
        return f"inspect /dispatch show {dispatch_id}"
    if record.get("status") == "needs_manual_start":
        exec_id = record.get("exec_id", "")
        return f"/exec approve {dispatch_id} write OR /exec approve-latest write OR /exec package {exec_id}"
    return exec_next_action(record)


def build_run_codex_reply(
    tail: str,
    owner_write_policy: bool = False,
    target: str = "codex",
    requested_executor: str = "",
    routing_reason: str = "",
) -> str:
    clean_target = str(target or "codex").strip().lower()
    clean_title, project_id = parse_task_new_tail(tail)
    if not clean_title:
        return (
            f"Usage: /run {clean_target}-write <task title> [--project <project_id>]"
            if owner_write_policy
            else f"Usage: /run {clean_target} <task title> [--project <project_id>]"
        )
    if project_id and not project_path(project_id).exists():
        return f"""One command task run refused
- reason: project not found
- project_id: {project_id}
- next: /project new {project_id} <project title>"""

    task_id, _task_text = create_task(clean_title, project_id=project_id)
    if project_id:
        attach_task_to_project(project_id, task_id)
    dispatch_id, dispatch_meta = create_codex_dispatch(
        task_id,
        with_context=True,
        target=clean_target,
        requested_executor=requested_executor,
        routing_reason=routing_reason,
    )
    start_reply = ""
    approval_reply = ""
    owner_write_policy_status = "not_requested"
    owner_write_policy_reason = ""
    write_target_fidelity = "not_run"
    write_target_lines = "none"
    if owner_write_policy:
        preflight_gate = owner_write_preflight_gate(dispatch_id)
        approval_gate = preflight_gate["approval_gate"]
        target_gate = preflight_gate["target_gate"]
        hard_deny_gate = preflight_gate["hard_deny_gate"]
        noop_gate = preflight_gate["noop_gate"]
        write_target_fidelity = "passed" if target_gate["ok"] else "missing"
        write_target_lines = safe_preview("; ".join(target_gate.get("target_lines", [])), 240) or "none"
        if not preflight_gate["ok"]:
            owner_write_policy_status = "no_op" if not noop_gate["ok"] else "refused"
            reason = f"{preflight_gate['reason']}; owner_write_policy=true; preflight_noop=true; human_confirm_required=true"
            detail_lines = hard_deny_gate.get("deny_lines") or approval_gate.get("forbidden_lines") or noop_gate.get("noop_lines") or []
            owner_write_policy_reason = reason
            stopped_label = "no-op" if owner_write_policy_status == "no_op" else "refused"
            read_only_gate_label = "owner_write_preflight_noop" if owner_write_policy_status == "no_op" else "owner_write_preflight_refused"
            next_label = (
                "no execution created; add a concrete write action or use the manual dispatch package"
                if owner_write_policy_status == "no_op"
                else "no execution created; revise the request with a safe explicit target or use the manual dispatch package"
            )
            start_reply = f"""Owner write policy {stopped_label} before execution
- status: {owner_write_policy_status}
- dispatch_id: {dispatch_id}
- run_policy: {RUN_POLICY_MANUAL.name}
- read_only_gate: {read_only_gate_label}
- owner_write_preflight_noop: true
- owner_write_hard_deny: {str(not hard_deny_gate['ok']).lower()}
- owner_write_noop: {str(not noop_gate['ok']).lower()}
- write_target_fidelity: {write_target_fidelity}
- write_target_lines: {write_target_lines}
- reason: {safe_preview(reason, 260)}
- hard_deny_or_forbidden_lines: {safe_preview('; '.join(detail_lines), 260) or 'none'}
- human_confirm_required: true
- auto_execute_enabled: false
- next: {next_label}"""
        else:
            approval_reply = build_exec_approve_reply(
                f"{dispatch_id} write explicit owner write policy approval",
                {
                    "owner_write_policy": "true",
                    "owner_write_policy_status": "approved",
                    "owner_write_policy_reason": "explicit owner write policy approval applied",
                    "write_target_fidelity": write_target_fidelity,
                    "write_target_lines": write_target_lines,
                },
            )
            record = latest_exec_for_dispatch(dispatch_id)
            owner_write_policy_status = record.get("status", "unknown") if record else "unknown"
            owner_write_policy_reason = "explicit owner write policy approval applied"
    else:
        start_reply = build_exec_start_reply(dispatch_id)
    record = latest_exec_for_dispatch(dispatch_id)
    exec_id = record.get("exec_id", "none") if record else "none"
    exec_status = record.get("status", "unknown") if record else "unknown"
    owner_preflight_stopped = owner_write_policy and owner_write_policy_status in {"refused", "no_op"} and exec_id == "none"
    if owner_preflight_stopped:
        exec_status = owner_write_policy_status
    if owner_write_policy and exec_id != "none":
        current_exec_meta = task_metadata(read_exec(exec_id))
        final_write_target_fidelity = current_exec_meta.get("write_target_fidelity") or write_target_fidelity
        final_write_target_lines = current_exec_meta.get("write_target_lines") or write_target_lines
        update_exec_fields(
            exec_id,
            {
                "owner_write_policy": "true",
                "owner_write_policy_status": owner_write_policy_status,
                "owner_write_policy_reason": owner_write_policy_reason,
                "write_target_fidelity": final_write_target_fidelity,
                "write_target_lines": final_write_target_lines,
            },
        )
        write_target_fidelity = final_write_target_fidelity
        write_target_lines = final_write_target_lines
    exec_meta = task_metadata(read_exec(exec_id)) if record and exec_id != "none" else {}
    read_only_gate = reply_field(start_reply, "read_only_gate") or ("bypassed_owner_write_policy" if owner_write_policy else "not_reported")
    auto_execute_enabled = exec_meta.get("auto_execute_enabled", "false")
    run_policy = exec_meta.get("run_policy", "") or (RUN_POLICY_MANUAL.name if owner_preflight_stopped else "none")
    human_confirm_required = exec_meta.get("human_confirm_required", "true" if owner_preflight_stopped else "false")
    runner_sandbox = exec_meta.get("runner_sandbox", "") or "none"
    runner_mode = exec_meta.get("runner_mode", "") or exec_meta.get("auto_run_mode", "") or "none"
    auto_decision = exec_meta.get("auto_decision", "") or "none"
    next_action = (
        "no execution created; revise the request with a safe explicit target or inspect /dispatch show " + dispatch_id
        if owner_write_policy and owner_write_policy_status == "refused" and exec_id == "none"
        else "no execution created; add a concrete write action or inspect /dispatch show " + dispatch_id
        if owner_write_policy and owner_write_policy_status == "no_op" and exec_id == "none"
        else one_command_next_action(record, dispatch_id)
    )
    log_event(
        "run_codex",
        task_id=task_id,
        dispatch_id=dispatch_id,
        exec_id=exec_id,
        exec_status=exec_status,
        owner_write_policy=owner_write_policy,
    )
    title = "One command owner write run:" if owner_write_policy else "One command task run:"
    command_chain = (
        "task -> dispatch -> owner write preflight"
        if owner_preflight_stopped
        else "task -> dispatch -> exec approve write" if owner_write_policy else "task -> dispatch -> exec start"
    )
    owner_policy_lines = (
        f"""
- owner_write_policy: true
- owner_write_policy_status: {owner_write_policy_status}
- owner_write_policy_reason: {safe_preview(owner_write_policy_reason, 180) or 'none'}
- owner_write_preflight_noop: {str(owner_preflight_stopped).lower()}
- write_target_fidelity: {write_target_fidelity}
- write_target_lines: {write_target_lines}
- workspace_write_requires_explicit_owner_command: true"""
        if owner_write_policy
        else ""
    )
    approval_section = (
        f"""

Owner write approval summary:
{safe_preview(approval_reply, 1600)}"""
        if owner_write_policy and approval_reply
        else ""
    )
    routing_lines = (
        f"\n- requested_executor: {requested_executor}"
        f"\n- routing_reason: {safe_preview(routing_reason, 160)}"
        if requested_executor
        else ""
    )
    return sanitize_sensitive_text(f"""{title}
- status: {exec_status}
- target_executor: {dispatch_meta.get('target_executor', '') or 'codex'}{routing_lines}
- command_chain: {command_chain}
- task_id: {task_id}
- dispatch_id: {dispatch_id}
- exec_id: {exec_id}
- project_id: {project_id or 'unassigned'}
- context_id: {dispatch_meta.get('context_id') or 'none'}
- run_policy: {run_policy}
- read_only_gate: {read_only_gate}
- auto_execute_enabled: {auto_execute_enabled}
- human_confirm_required: {human_confirm_required}
- runner_sandbox: {runner_sandbox}
- runner_mode: {runner_mode}
- auto_decision: {auto_decision}
- no_git_add_commit_push: true
- deploy_forbidden: true
{owner_policy_lines}
- next: {next_action}

Execution start summary:
{safe_preview(start_reply, 1600) if start_reply else 'Owner write policy bypassed read-only start and used direct dispatch workspace-write approval.'}
{approval_section}""")


def build_exec_package_reply(exec_id: str) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    text = read_exec(normalized_exec_id)
    meta = task_metadata(text)
    return build_exec_payload(normalized_exec_id, meta.get("dispatch_id", ""))


def build_exec_mark_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=2)
    if len(parts) < 2 or parts[1].lower() not in {"copied", "opened"}:
        return "Usage: /exec mark <exec_id> copied|opened <note>"
    exec_id = normalize_exec_id(parts[0])
    action = parts[1].lower()
    note = sanitize_sensitive_text(parts[2] if len(parts) > 2 else "").strip() or f"manual {action} recorded"
    now = iso_now()
    section = "Copy Payload" if action == "copied" else "Open Record"
    section_body = f"- {action}_at: {now}\n- note: {note}\n- manual_only: true\n- human_confirm_required: true"
    text = update_exec_status(exec_id, action, f"marked {action}: {note}", {f"{action}_at": now})
    text = append_to_section(text, section, f"### {action} at {now}\n{section_body}")
    write_exec(exec_id, text)
    meta = task_metadata(text)
    dispatch_id = meta.get("dispatch_id", "")
    if action == "copied" and dispatch_id:
        try:
            dispatch_text = read_dispatch(dispatch_id)
            if task_metadata(dispatch_text).get("status") == "ready":
                update_dispatch_status(dispatch_id, "sent", f"execution session {exec_id} copied: {note}", {"sent_at": now})
        except Exception:
            pass
    log_event(f"exec_{action}", exec_id=exec_id)
    return f"""Execution session marked {action}: {exec_id}
- status: {action}
- {action}_at: {now}
- note: {note}
- human_confirm_required: true
- next: {exec_next_action(task_metadata_record(exec_id))}"""


def task_metadata_record(exec_id: str) -> dict:
    text = read_exec(exec_id)
    meta = task_metadata(text)
    meta["exec_id"] = normalize_exec_id(exec_id)
    return meta


def build_exec_receive_reply(exec_id: str, report: str) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    text = read_exec(normalized_exec_id)
    meta = task_metadata(text)
    dispatch_id = normalize_dispatch_id(meta.get("dispatch_id", ""))
    clean_report = sanitize_sensitive_text(report).strip() or "- empty return report; needs evidence."
    dispatch_reply = build_dispatch_receive_reply(dispatch_id, clean_report)
    now = iso_now()
    text = read_exec(normalized_exec_id)
    text = replace_task_field(text, "status", "returned")
    text = replace_task_field(text, "updated_at", now)
    text = replace_task_field(text, "returned_at", now)
    text = append_to_section(text, "Return Record", f"### Return at {now}\n{clean_report}\n\nDispatch sync:\n{safe_preview(dispatch_reply, 500)}")
    text = append_to_section(text, "Status Timeline", f"- {now} return report received; synced through dispatch {dispatch_id}.")
    write_exec(normalized_exec_id, text)
    log_event("exec_returned", exec_id=normalized_exec_id, dispatch_id=dispatch_id)
    return f"""Execution return recorded: {normalized_exec_id}
- status: returned
- dispatch_id: {dispatch_id}
- synced_dispatch_receive: true
- human_confirm_required: true
- external_execution_enabled: false
- next: /dispatch qa {dispatch_id}

Dispatch sync:
{safe_preview(dispatch_reply, 520)}"""


def auto_postprocess_commands(exec_id: str, task_id: str, dispatch_id: str, reason: str = "") -> str:
    clean_reason = safe_preview(reason, 160) or "auto postprocess gate failed"
    return "\n".join(
        [
            f"- /exec show {exec_id}",
            f"- /dispatch qa {dispatch_id}",
            f"- /evidence gaps {task_id}",
            f"- /task review {task_id}",
            f"- /dispatch link-review {dispatch_id}",
            f"- /task decide {task_id} needs_evidence human review required: {clean_reason}",
        ]
    )


def record_auto_postprocess_state(
    exec_id: str,
    task_id: str,
    dispatch_id: str,
    *,
    enabled: bool,
    qa_done: bool = False,
    evidence_verified: bool = False,
    review_done: bool = False,
    dispatch_review_linked: bool = False,
    decision: str = "needs_human_review",
    closed: bool = False,
    retro_created: bool = False,
    reason: str = "",
) -> None:
    clean_reason = safe_preview(reason, 260) or decision
    fields = {
        "auto_postprocess_enabled": str(bool(enabled)).lower(),
        "auto_qa_done": str(bool(qa_done)).lower(),
        "auto_evidence_verified": str(bool(evidence_verified)).lower(),
        "auto_review_done": str(bool(review_done)).lower(),
        "auto_dispatch_review_linked": str(bool(dispatch_review_linked)).lower(),
        "auto_decision": sanitize_sensitive_text(decision),
        "auto_closed": str(bool(closed)).lower(),
        "auto_retro_created": str(bool(retro_created)).lower(),
        "auto_postprocess_reason": clean_reason,
    }
    update_exec_fields(exec_id, fields)
    append_exec_autostart_section(
        exec_id,
        "auto postprocess",
        "\n".join(f"- {key}: {value}" for key, value in fields.items()),
    )
    try:
        task_text = read_task(task_id)
        task_text = append_to_section(
            task_text,
            "Timeline",
            f"- {iso_now()} auto postprocess decision={decision}; closed={str(bool(closed)).lower()}; reason={clean_reason}.",
        )
        write_task(task_id, task_text)
    except Exception:
        pass
    log_event("exec_auto_postprocess", exec_id=exec_id, dispatch_id=dispatch_id, decision=decision)


def generated_exec_evidence_ids(task_id: str, exec_id: str) -> list[str]:
    ids = []
    for record in evidence_records(task_id):
        if record.get("verified") in {"verified", "yes"}:
            continue
        combined = "\n".join(
            str(record.get(key, ""))
            for key in ("claim", "observed", "risk", "notes")
        )
        if exec_id in combined and record.get("supports_acceptance") == "observed":
            ids.append(record["evidence_id"])
    return ids


def auto_verify_exec_evidence(task_id: str, exec_id: str, sandbox_mode: str = "read-only") -> list[str]:
    verified_ids = []
    sandbox_label = "approved workspace-write" if sandbox_mode == "workspace-write" else "read-only"
    for evidence_id in generated_exec_evidence_ids(task_id, exec_id):
        update_evidence_mark(
            task_id,
            evidence_id,
            "verified",
            f"auto verified for {sandbox_label} exec {exec_id}: returncode=0, timed_out=false, completion_state=completed, no git/deploy action by Bridge.",
        )
        verified_ids.append(evidence_id)
    return verified_ids


def build_exec_auto_postprocess_reply(
    exec_id: str,
    dispatch_id: str,
    completion: dict,
    *,
    sandbox_mode: str,
    receive_synced: bool,
) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    normalized_dispatch_id = normalize_dispatch_id(dispatch_id)
    exec_meta = task_metadata(read_exec(normalized_exec_id))
    task_id = normalize_task_id(exec_meta.get("task_id", ""))
    write_confirmed = str(exec_meta.get("write_confirmed", "")).lower() == "true"
    owner_write_policy = str(exec_meta.get("owner_write_policy", "")).lower() == "true"
    authorized_sandbox = sandbox_mode == "read-only" or (sandbox_mode == "workspace-write" and write_confirmed)
    sandbox_label = "approved workspace-write" if sandbox_mode == "workspace-write" else "read-only"
    expected_policy = run_policy_for_sandbox(sandbox_mode, owner_write_policy=owner_write_policy).name
    hard_gates = {
        "run_policy": exec_meta.get("run_policy") == expected_policy,
        "runner_sandbox": sandbox_mode in RUNNER_SANDBOX_MODES,
        "runner_authorization": authorized_sandbox,
        "write_confirmed": sandbox_mode != "workspace-write" or write_confirmed,
        "returncode": str(completion.get("returncode")) == "0",
        "timed_out": str(completion.get("timed_out")).lower() == "false",
        "completion_state": completion.get("completion_state") == "completed",
        "dispatch_receive_synced": bool(receive_synced),
        "no_git_add_commit_push": True,
        "deploy_forbidden": True,
    }
    if owner_write_policy:
        hard_gates["write_target_fidelity"] = exec_meta.get("write_target_fidelity") == "passed"
        hard_gates["post_run_target_fidelity"] = exec_meta.get("post_run_target_fidelity") == "passed"
    if str(exec_meta.get("review_required_by", "")).strip().lower() == "codex":
        # Claude-written work cannot auto-close without a codex pass_candidate
        # review verdict; missing/pending/inconclusive reviews fail this gate.
        hard_gates["codex_review"] = exec_meta.get("codex_review_status") == "pass_candidate"
    failed_hard = [name for name, ok in hard_gates.items() if not ok]
    if failed_hard:
        reason = "gate failed: " + ", ".join(failed_hard)
        record_auto_postprocess_state(
            normalized_exec_id,
            task_id,
            normalized_dispatch_id,
            enabled=False,
            decision="needs_human_review",
            reason=reason,
        )
        return f"""Auto postprocess: needs_human_review
- auto_postprocess_enabled: false
- run_policy: {exec_meta.get('run_policy', '') or 'none'}
- auto_decision: needs_human_review
- auto_closed: false
- auto_postprocess_reason: {reason}
Next commands:
{auto_postprocess_commands(normalized_exec_id, task_id, normalized_dispatch_id, reason)}"""

    qa_done = False
    evidence_verified = False
    review_done = False
    dispatch_review_linked = False
    try:
        qa_reply = build_dispatch_qa_reply(normalized_dispatch_id)
        qa_done = True
        task_text = read_task(task_id)
        report = task_section(task_text, "Execution Report")
        intake = analyze_evidence_intake(report, task_section(task_text, "Acceptance Criteria"))
        false_pass_reasons = read_only_false_pass_reasons(report)
        if false_pass_reasons:
            reason = "report indicates blocked write/source implementation: " + ", ".join(false_pass_reasons[:4])
            record_auto_postprocess_state(
                normalized_exec_id,
                task_id,
                normalized_dispatch_id,
                enabled=True,
                qa_done=qa_done,
                evidence_verified=False,
                review_done=False,
                dispatch_review_linked=False,
                decision="needs_human_review",
                reason=reason,
            )
            return f"""Auto postprocess: needs_human_review
- auto_postprocess_enabled: true
- run_policy: {exec_meta.get('run_policy', '') or 'none'}
- auto_qa_done: {str(qa_done).lower()}
- auto_evidence_verified: false
- auto_review_done: false
- auto_dispatch_review_linked: false
- auto_decision: needs_human_review
- auto_closed: false
- auto_postprocess_reason: {reason}
Next commands:
{auto_postprocess_commands(normalized_exec_id, task_id, normalized_dispatch_id, reason)}

QA preview:
{safe_preview(qa_reply, 260)}"""
        eligible_ids = generated_exec_evidence_ids(task_id, normalized_exec_id)
        evidence_intake_generated = bool(eligible_ids)
        sensitive_ok = not bool(intake.get("sensitive_risk"))
        if evidence_intake_generated and sensitive_ok:
            verified_ids = auto_verify_exec_evidence(task_id, normalized_exec_id, sandbox_mode)
            evidence_verified = bool(verified_ids)
        else:
            verified_ids = []
        review_reply = build_task_review_reply(task_id)
        review_done = True
        link_reply = build_dispatch_link_review_reply(normalized_dispatch_id)
        dispatch_review_linked = True
        analysis = evidence_analysis(task_id)
        review_has_no_gaps = not analysis["has_gaps"] and analysis["recommendation"] == "pass"
        closure_evidence_ready = evidence_ready_for_auto_close(analysis)
        gates = {
            **hard_gates,
            "evidence_intake": evidence_intake_generated,
            "sensitive_risk": sensitive_ok,
            "review_has_no_remaining_gaps": review_has_no_gaps,
            "closure_evidence_ready": closure_evidence_ready,
        }
        failed = [name for name, ok in gates.items() if not ok]
        if failed:
            reason = "gate failed: " + ", ".join(failed)
            record_auto_postprocess_state(
                normalized_exec_id,
                task_id,
                normalized_dispatch_id,
                enabled=True,
                qa_done=qa_done,
                evidence_verified=evidence_verified,
                review_done=review_done,
                dispatch_review_linked=dispatch_review_linked,
                decision="needs_human_review",
                reason=reason,
            )
            return f"""Auto postprocess: needs_human_review
- auto_postprocess_enabled: true
- run_policy: {exec_meta.get('run_policy', '') or 'none'}
- auto_qa_done: {str(qa_done).lower()}
- auto_evidence_verified: {str(evidence_verified).lower()}
- auto_review_done: {str(review_done).lower()}
- auto_dispatch_review_linked: {str(dispatch_review_linked).lower()}
- auto_decision: needs_human_review
- auto_closed: false
- auto_postprocess_reason: {reason}
- evidence_ids: {', '.join(verified_ids) if verified_ids else 'none'}
Next commands:
{auto_postprocess_commands(normalized_exec_id, task_id, normalized_dispatch_id, reason)}

Review preview:
{safe_preview(review_reply, 420)}

Dispatch link preview:
{safe_preview(link_reply, 260)}

QA preview:
{safe_preview(qa_reply, 260)}"""

        decide_reply = build_task_decide_reply(
            task_id,
            "pass",
            f"auto pass: {sandbox_label} exec {normalized_exec_id} completed with verified generated evidence and no remaining review gaps.",
        )
        post_decision_analysis = sync_task_evidence_state(task_id)
        if not evidence_ready_for_auto_close(post_decision_analysis):
            reason = f"post-decision evidence closure gate failed: {evidence_closure_state(post_decision_analysis)}"
            record_auto_postprocess_state(
                normalized_exec_id,
                task_id,
                normalized_dispatch_id,
                enabled=True,
                qa_done=qa_done,
                evidence_verified=evidence_verified,
                review_done=review_done,
                dispatch_review_linked=dispatch_review_linked,
                decision="needs_human_review",
                reason=reason,
            )
            return f"""Auto postprocess: needs_human_review
- auto_postprocess_enabled: true
- run_policy: {exec_meta.get('run_policy', '') or 'none'}
- auto_qa_done: {str(qa_done).lower()}
- auto_evidence_verified: {str(evidence_verified).lower()}
- auto_review_done: {str(review_done).lower()}
- auto_dispatch_review_linked: {str(dispatch_review_linked).lower()}
- auto_decision: needs_human_review
- auto_closed: false
- auto_postprocess_reason: {reason}
- evidence_ids: {', '.join(verified_ids) if verified_ids else 'none'}
{build_closure_evidence_summary(post_decision_analysis)}
Next commands:
{auto_postprocess_commands(normalized_exec_id, task_id, normalized_dispatch_id, reason)}

Decision preview:
{safe_preview(decide_reply, 260)}"""
        close_reply = build_task_close_reply(task_id)
        retro_reply = build_retro_create_reply(task_id)
        retro_created = retro_exists(task_id)
        final_analysis = evidence_analysis(task_id)
        record_auto_postprocess_state(
            normalized_exec_id,
            task_id,
            normalized_dispatch_id,
            enabled=True,
            qa_done=qa_done,
            evidence_verified=evidence_verified,
            review_done=review_done,
            dispatch_review_linked=dispatch_review_linked,
            decision="pass",
            closed=True,
            retro_created=retro_created,
            reason="all gates passed",
        )
        return f"""Auto postprocess: pass
- auto_postprocess_enabled: true
- run_policy: {exec_meta.get('run_policy', '') or 'none'}
- auto_qa_done: true
- auto_evidence_verified: {str(evidence_verified).lower()}
- auto_review_done: true
- auto_dispatch_review_linked: true
- auto_decision: pass
- auto_closed: true
- auto_retro_created: {str(retro_created).lower()}
- auto_postprocess_reason: all gates passed
- evidence_ids: {', '.join(verified_ids) if verified_ids else 'none'}
- task_status: {task_status(task_id)}
{build_closure_evidence_summary(final_analysis)}

Close loop:
- dispatch QA done
- generated evidence auto-verified
- task review done
- dispatch review linked
- task decided pass via {sandbox_label}
- task closed
- retro draft created

Decision preview:
{safe_preview(decide_reply, 260)}

Close preview:
{safe_preview(close_reply, 260)}

Retro preview:
{safe_preview(retro_reply, 260)}"""
    except Exception as exc:
        reason = f"auto postprocess failed: {safe_preview(str(exc), 220)}"
        record_auto_postprocess_state(
            normalized_exec_id,
            task_id,
            normalized_dispatch_id,
            enabled=True,
            qa_done=qa_done,
            evidence_verified=evidence_verified,
            review_done=review_done,
            dispatch_review_linked=dispatch_review_linked,
            decision="needs_human_review",
            reason=reason,
        )
        return f"""Auto postprocess: needs_human_review
- auto_postprocess_enabled: true
- auto_decision: needs_human_review
- auto_closed: false
- auto_postprocess_reason: {reason}
Next commands:
{auto_postprocess_commands(normalized_exec_id, task_id, normalized_dispatch_id, reason)}"""


def build_exec_terminal_reply(tail: str, status: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if not parts:
        return f"Usage: /exec {status} <exec_id> <note>"
    exec_id = normalize_exec_id(parts[0])
    note = sanitize_sensitive_text(parts[1] if len(parts) > 1 else "").strip() or f"manual {status}"
    update_exec_status(exec_id, status, f"marked {status}: {note}")
    log_event(f"exec_{status}", exec_id=exec_id)
    return f"""Execution session marked {status}: {exec_id}
- status: {status}
- note: {note}
- manual ledger only: true
- auto_execute_enabled: false"""


def build_exec_show_reply(exec_id: str) -> str:
    normalized_exec_id = normalize_exec_id(exec_id)
    text = read_exec(normalized_exec_id)
    meta = task_metadata(text)
    record = dict(meta)
    record["exec_id"] = normalized_exec_id
    record["stale"] = exec_is_stale_record(record)
    return f"""Execution session summary: {normalized_exec_id}
- status: {meta.get('status', 'unknown')}
- dispatch_id: {meta.get('dispatch_id', '')}
- task_id: {meta.get('task_id', '')}
- project_id: {meta.get('project_id', '') or 'unassigned'}
- target_executor: {meta.get('target_executor', '')}
- created_at: {meta.get('created_at', '')}
- updated_at: {meta.get('updated_at', '')}
- opened_at: {meta.get('opened_at', '') or 'none'}
- copied_at: {meta.get('copied_at', '') or 'none'}
- started_at: {meta.get('started_at', '') or 'none'}
- returned_at: {meta.get('returned_at', '') or 'none'}
- stale: {str(bool(record.get('stale'))).lower()}
- run_policy: {meta.get('run_policy', '') or 'none'}
- run_policy_reason: {meta.get('run_policy_reason', '') or 'none'}
- human_confirm_required: {meta.get('human_confirm_required', 'true')}
- external_execution_enabled: {meta.get('external_execution_enabled', 'false')}
- auto_execute_enabled: {meta.get('auto_execute_enabled', 'false')}
- read_only_auto_run: {meta.get('read_only_auto_run', 'false')}
- auto_run_mode: {meta.get('auto_run_mode', '') or 'none'}
- runner_mode: {meta.get('runner_mode', '') or meta.get('auto_run_mode', '') or 'none'}
- runner_sandbox: {meta.get('runner_sandbox', '') or 'none'}
- runner_probe: {meta.get('runner_probe', '') or 'none'}
- returncode: {meta.get('returncode', '') or 'none'}
- timed_out: {meta.get('timed_out', 'false')}
- stdout_chars: {meta.get('stdout_chars', '0')}
- stderr_chars: {meta.get('stderr_chars', '0')}
- completion_state: {meta.get('completion_state', '') or 'unknown'}
- payload_state: {meta.get('payload_state', '') or 'unknown'}
- write_confirmed: {meta.get('write_confirmed', 'false')}
- write_approved_at: {meta.get('write_approved_at', '') or 'none'}
- owner_write_policy: {meta.get('owner_write_policy', 'false')}
- owner_write_policy_status: {meta.get('owner_write_policy_status', '') or 'none'}
- owner_write_policy_reason: {meta.get('owner_write_policy_reason', '') or 'none'}
- write_target_fidelity: {meta.get('write_target_fidelity', '') or 'none'}
- write_target_lines: {meta.get('write_target_lines', '') or 'none'}
- auto_postprocess_enabled: {meta.get('auto_postprocess_enabled', 'false')}
- auto_qa_done: {meta.get('auto_qa_done', 'false')}
- auto_evidence_verified: {meta.get('auto_evidence_verified', 'false')}
- auto_review_done: {meta.get('auto_review_done', 'false')}
- auto_dispatch_review_linked: {meta.get('auto_dispatch_review_linked', 'false')}
- auto_decision: {meta.get('auto_decision', '') or 'none'}
- auto_closed: {meta.get('auto_closed', 'false')}
- auto_retro_created: {meta.get('auto_retro_created', 'false')}
- auto_postprocess_reason: {meta.get('auto_postprocess_reason', '') or 'none'}
- return_record: {safe_preview(task_section(text, 'Return Record'), 240)}
- next: {exec_next_action(record)}"""


def build_exec_list_reply() -> str:
    records = exec_records()[:10]
    if not records:
        return "Execution sessions:\n- none."
    lines = ["Execution sessions:"]
    for record in records:
        stale = " stale" if record.get("stale") else ""
        lines.append(
            f"- {record['exec_id']} | {record.get('status')}{stale} | dispatch={record.get('dispatch_id')} | task={record.get('task_id')} | target={record.get('target_executor')} | updated={record.get('updated_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def build_exec_dashboard_reply() -> str:
    records = exec_records()
    counts = exec_counts(records)
    by_executor: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for record in records:
        by_executor[record.get("target_executor") or "unknown"] = by_executor.get(record.get("target_executor") or "unknown", 0) + 1
        by_project[record.get("project_id") or "unassigned"] = by_project.get(record.get("project_id") or "unassigned", 0) + 1
    lines = [
        "Atlas Execution Dashboard",
        f"- execution_count: {counts['execution_count']}",
        f"- prepared_count: {counts['execution_prepared_count']}",
        f"- started_count: {counts['execution_started_count']}",
        f"- opened_count: {counts['execution_opened_count']}",
        f"- copied_count: {counts['execution_copied_count']}",
        f"- returned_count: {counts['execution_returned_count']}",
        f"- needs_manual_start_count: {counts['execution_needs_manual_start_count']}",
        f"- failed_count: {counts['execution_failed_count']}",
        f"- stale_count: {counts['execution_stale_count']}",
        "- human_confirm_required: true",
        "- external_execution_enabled: false",
        "- auto_execute_enabled: false",
        f"- read_only_auto_exec_enabled: {str(READ_ONLY_AUTO_EXEC_ENABLED).lower()}",
        "",
        "by_executor:",
    ]
    lines.extend([f"- {name}: {count}" for name, count in sorted(by_executor.items())] or ["- none"])
    lines.append("")
    lines.append("by_project:")
    lines.extend([f"- {name}: {count}" for name, count in sorted(by_project.items())] or ["- none"])
    lines.append("")
    lines.append("Recent pending:")
    pending = [record for record in records if record.get("status") in {"prepared", "started", "opened", "copied", "needs_manual_start"}][:10]
    lines.extend([f"- {record['exec_id']} | {record.get('status')} | next={exec_next_action(record)}" for record in pending] or ["- none"])
    return "\n".join(lines)


def build_exec_stale_reply() -> str:
    stale = [record for record in exec_records() if record.get("stale")]
    if not stale:
        return "Stale execution sessions:\n- none. Rule: status=prepared/opened/copied and updated time older than 24 hours."
    lines = ["Stale execution sessions (> 24h):"]
    for record in stale[:20]:
        lines.append(
            f"- {record['exec_id']} | status={record.get('status')} | dispatch={record.get('dispatch_id')} | updated={record.get('updated_at')} | next=/exec receive {record['exec_id']} OR /exec fail {record['exec_id']} <note>"
        )
    return "\n".join(lines)


def handle_exec_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/exec":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_exec_help_reply()
        if subcommand == "prepare":
            return build_exec_prepare_reply(tail)
        if subcommand == "start":
            return build_exec_start_reply(tail)
        if subcommand == "approve":
            return build_exec_approve_reply(tail)
        if subcommand == "approve-latest":
            return build_exec_approve_latest_reply(tail)
        if subcommand == "package":
            return build_exec_package_reply(tail)
        if subcommand == "mark":
            return build_exec_mark_reply(tail)
        if subcommand == "receive":
            receive_parts = tail.split(maxsplit=1)
            if not receive_parts:
                return "Usage: /exec receive <exec_id>\n<pasted return report>"
            exec_id = receive_parts[0]
            inline = receive_parts[1] if len(receive_parts) > 1 else ""
            body = "\n".join(lines[1:]).strip()
            return build_exec_receive_reply(exec_id, body or inline)
        if subcommand == "cancel":
            return build_exec_terminal_reply(tail, "cancelled")
        if subcommand == "fail":
            return build_exec_terminal_reply(tail, "failed")
        if subcommand == "show":
            return build_exec_show_reply(tail)
        if subcommand == "list":
            return build_exec_list_reply()
        if subcommand == "dashboard":
            return build_exec_dashboard_reply()
        if subcommand == "stale":
            return build_exec_stale_reply()
        return build_exec_help_reply()
    except FileNotFoundError as exc:
        return f"exec source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"exec operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"exec operation failed: {safe_preview(str(exc), 180)}"


def generate_pilot_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("PILOT-%Y%m%d-%H%M%S")
    if not pilot_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not pilot_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique pilot_id")


def read_pilot(pilot_id: str) -> str:
    path = pilot_path(pilot_id)
    if not path.exists():
        raise FileNotFoundError(f"pilot not found: {pilot_id}")
    return path.read_text(encoding="utf-8")


def write_pilot(pilot_id: str, text: str) -> None:
    pilot_path(pilot_id).write_text(sanitize_sensitive_text(text), encoding="utf-8")


def pilot_title_from_text(pilot_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {pilot_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "untitled pilot"


def build_pilot_markdown(pilot_id: str, project_id: str, title: str) -> str:
    clean_project_id = validate_project_id(project_id)
    project_text = read_project(clean_project_id)
    now = iso_now()
    clean_title = sanitize_title(title)
    return sanitize_sensitive_text(f"""# {pilot_id} {clean_title}

status: active
created_at: {now}
updated_at: {now}
project_id: {clean_project_id}
mode: consultation
external_execution_enabled: false

## Goal
- Use the existing Atlas Workbench loop on a real project and record whether it improves delivery clarity, evidence quality, and coordination speed.
- project: {clean_project_id} {project_title_from_text(clean_project_id, project_text)}

## Scope
- Record operational metrics for project/task/context/dispatch usage.
- Keep all records inside workbench/pilots and existing workbench ledgers.
- Do not call Codex/Kiro automatically, do not run commands, do not modify external projects.

## Baseline
- live_debt: Octo UI live validation has been skipped in earlier phases and must be tracked as an evidence gap until manually verified.
- project_status: {project_metadata(project_text).get('status', 'unknown')}
- pilot_started_at: {now}

## Metrics
- task_count: 0
- dispatch_count: 0
- returned_count: 0
- qa_pass_count: 0
- needs_evidence_count: 0
- closed_count: 0
- evidence_gap_count: 0
- context_pack_count: 0
- manual_copy_count: 0
- estimated_time_saved: 0 min
- main_friction: none yet

## Tasks Included
- none.

## Dispatches
- none.

## Reports
- none yet.

## Evidence Gaps
- live_debt: Octo UI live validation still needs a real user check.

## Decisions
- none yet.

## Time Saved Estimate
- 0 min rough estimate; update with /pilot metrics after tasks and dispatches are linked.

## Friction Log
- none yet.

## Lessons Learned
- none yet.

## Next Actions
- Add project tasks with /pilot add-task {pilot_id} <task_id>.
- Add manual dispatches with /pilot add-dispatch {pilot_id} <dispatch_id>.
- Check metrics with /pilot metrics {pilot_id}.
""")


def create_pilot(project_id: str, title: str) -> tuple[str, str]:
    clean_project_id = validate_project_id(project_id)
    read_project(clean_project_id)
    pilot_id = generate_pilot_id()
    text = build_pilot_markdown(pilot_id, clean_project_id, title)
    write_pilot(pilot_id, text)
    log_event("pilot_started", pilot_id=pilot_id, project_id=clean_project_id)
    return pilot_id, text


def pilot_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(PILOTS_DIR.glob("PILOT-*.md")):
        pilot_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = task_metadata(text)
        records.append(
            {
                "pilot_id": pilot_id,
                "title": pilot_title_from_text(pilot_id, text),
                "status": meta.get("status", "unknown"),
                "created_at": meta.get("created_at", ""),
                "updated_at": meta.get("updated_at", ""),
                "project_id": meta.get("project_id", ""),
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def ids_from_section(text: str, heading: str, pattern: str) -> list[str]:
    found = []
    for match in re.finditer(pattern, task_section(text, heading)):
        value = match.group(0)
        if value not in found:
            found.append(value)
    return found


def pilot_task_ids(text: str) -> list[str]:
    return ids_from_section(text, "Tasks Included", r"OHB-\d{8}-\d{6}(?:-\d{2})?")


def pilot_dispatch_ids(text: str) -> list[str]:
    return ids_from_section(text, "Dispatches", r"DISPATCH-\d{8}-\d{6}(?:-\d{2})?")


def last_meaningful_line(section: str) -> str:
    for line in reversed(section.splitlines()):
        stripped = line.strip()
        if stripped and stripped not in {"- none.", "- none yet."}:
            return stripped
    return ""


def validate_pilot_project_link(pilot_text: str, project_id: str) -> str:
    pilot_project_id = task_metadata(pilot_text).get("project_id", "")
    if validate_project_id(project_id) != validate_project_id(pilot_project_id):
        raise ValueError(f"item belongs to project {project_id}, not pilot project {pilot_project_id}")
    return pilot_project_id


def add_unique_pilot_line(text: str, heading: str, item_id: str, line: str) -> str:
    current = task_section(text, heading)
    if item_id in current:
        return text
    existing = [
        item for item in current.splitlines()
        if item.strip() and item.strip() not in {"- none.", "- none yet."}
    ]
    existing.append(line)
    return set_section_body(text, heading, "\n".join(existing))


def pilot_metrics_data(pilot_id: str) -> dict:
    normalized_pilot_id = normalize_pilot_id(pilot_id)
    text = read_pilot(normalized_pilot_id)
    meta = task_metadata(text)
    project_id = meta.get("project_id", "")
    task_ids = pilot_task_ids(text)
    dispatch_ids = pilot_dispatch_ids(text)
    tasks = []
    for task_id in task_ids:
        try:
            task_text = read_task(task_id)
        except Exception:
            continue
        task_meta = task_metadata(task_text)
        tasks.append({"task_id": task_id, "text": task_text, "meta": task_meta})
    dispatches = []
    for dispatch_id in dispatch_ids:
        try:
            dispatch_text = read_dispatch(dispatch_id)
        except Exception:
            continue
        dispatch_meta = task_metadata(dispatch_text)
        dispatches.append({"dispatch_id": dispatch_id, "text": dispatch_text, "meta": dispatch_meta})
    returned_count = sum(
        1 for item in dispatches
        if item["meta"].get("status") in {"returned", "qa_ready", "reviewed", "needs_evidence", "closed"}
        or "### Return at" in task_section(item["text"], "Return Report")
    )
    qa_pass_count = sum(
        1 for item in dispatches
        if item["meta"].get("status") == "qa_ready"
        or ("pass" in task_section(item["text"], "QA Result").lower() and "needs_evidence" not in task_section(item["text"], "QA Result").lower())
    )
    needs_evidence_count = sum(1 for item in tasks if item["meta"].get("status") == "needs_evidence") + sum(
        1 for item in dispatches if item["meta"].get("status") == "needs_evidence"
    )
    closed_count = sum(1 for item in tasks if item["meta"].get("status") in {"passed", "archived", "cancelled"}) + sum(
        1 for item in dispatches if item["meta"].get("status") == "closed"
    )
    evidence_gap_count = 0
    for item in tasks:
        try:
            if evidence_analysis(item["task_id"]).get("has_gaps"):
                evidence_gap_count += 1
        except Exception:
            evidence_gap_count += 1
    context_pack_count = sum(
        1 for record in context_records()
        if record.get("source_task_id") in set(task_ids) or (project_id and record.get("source_project_id") == project_id)
    )
    manual_copy_count = sum(
        1 for item in dispatches
        if item["meta"].get("status") in {"sent", "returned", "qa_ready", "reviewed", "needs_evidence", "closed"}
        or "manual_copy_only: true" in task_section(item["text"], "Sent Record")
    )
    linked_collections = [
        record for record in collection_records()
        if record.get("task_id") in set(task_ids) or (project_id and record.get("project_id") == project_id)
    ]
    smoke_collection_count = sum(1 for record in linked_collections if record.get("kind") == "smoke")
    failed_collection_count = sum(1 for record in linked_collections if record.get("smoke_failed"))
    auto_evidence_count = len(linked_collections)
    linked_executions = [
        record for record in exec_records()
        if record.get("dispatch_id") in set(dispatch_ids)
        or record.get("task_id") in set(task_ids)
        or (project_id and record.get("project_id") == project_id)
    ]
    copied_execution_count = sum(1 for record in linked_executions if record.get("status") in {"copied", "returned"})
    returned_execution_count = sum(1 for record in linked_executions if record.get("status") == "returned")
    stale_execution_count = sum(1 for record in linked_executions if record.get("stale"))
    manual_evidence_count = 0
    for task_id in task_ids:
        try:
            manual_evidence_count += sum(1 for record in evidence_records(task_id) if record.get("source") != "collector")
        except Exception:
            continue
    estimated_minutes = len(task_ids) * 15 + len(dispatch_ids) * 10 + context_pack_count * 5 + returned_count * 5
    friction = last_meaningful_line(task_section(text, "Friction Log")) or "none recorded"
    return {
        "pilot_id": normalized_pilot_id,
        "text": text,
        "meta": meta,
        "task_ids": task_ids,
        "dispatch_ids": dispatch_ids,
        "task_count": len(task_ids),
        "dispatch_count": len(dispatch_ids),
        "returned_count": returned_count,
        "qa_pass_count": qa_pass_count,
        "needs_evidence_count": needs_evidence_count,
        "closed_count": closed_count,
        "evidence_gap_count": evidence_gap_count,
        "context_pack_count": context_pack_count,
        "manual_copy_count": manual_copy_count,
        "collection_count": len(linked_collections),
        "smoke_collection_count": smoke_collection_count,
        "failed_collection_count": failed_collection_count,
        "auto_evidence_count": auto_evidence_count,
        "manual_evidence_count": manual_evidence_count,
        "execution_count": len(linked_executions),
        "copied_count": copied_execution_count,
        "returned_execution_count": returned_execution_count,
        "stale_execution_count": stale_execution_count,
        "estimated_minutes": estimated_minutes,
        "estimated_time_saved": f"{estimated_minutes} min",
        "main_friction": safe_preview(friction, 220),
    }


def build_pilot_metrics_body(data: dict) -> str:
    return f"""- task_count: {data['task_count']}
- dispatch_count: {data['dispatch_count']}
- returned_count: {data['returned_count']}
- qa_pass_count: {data['qa_pass_count']}
- needs_evidence_count: {data['needs_evidence_count']}
- closed_count: {data['closed_count']}
- evidence_gap_count: {data['evidence_gap_count']}
- context_pack_count: {data['context_pack_count']}
- manual_copy_count: {data['manual_copy_count']}
- collection_count: {data.get('collection_count', 0)}
- smoke_collection_count: {data.get('smoke_collection_count', 0)}
- failed_collection_count: {data.get('failed_collection_count', 0)}
- auto_evidence_count: {data.get('auto_evidence_count', 0)}
- manual_evidence_count: {data.get('manual_evidence_count', 0)}
- execution_count: {data.get('execution_count', 0)}
- copied_count: {data.get('copied_count', 0)}
- returned_execution_count: {data.get('returned_execution_count', 0)}
- stale_execution_count: {data.get('stale_execution_count', 0)}
- estimated_time_saved: {data['estimated_time_saved']}
- main_friction: {data['main_friction']}"""


def build_pilot_help_reply() -> str:
    return """Atlas Pilot commands
- /pilot help
- /pilot start <project_id> <title>
- /pilot list
- /pilot show <pilot_id>
- /pilot add-task <pilot_id> <task_id>
- /pilot add-dispatch <pilot_id> <dispatch_id>
- /pilot note <pilot_id> <single-line note>
- /pilot metrics <pilot_id>
- /pilot complete <pilot_id> <note>
- /pilot dashboard

Boundary: pilot is an operational record layer only. It writes workbench/pilots, reads existing workbench ledgers, does not read .env, does not call Codex/Kiro, and does not run commands."""


def build_pilot_start_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /pilot start <project_id> <title>"
    project_id = validate_project_id(parts[0])
    pilot_id, _text = create_pilot(project_id, parts[1])
    return f"""Pilot started: {pilot_id}
- status: active
- project_id: {project_id}
- path: workbench/pilots/{pilot_id}.md
- external_execution_enabled: false
- next: run real Workbench tasks, then /pilot add-task {pilot_id} <task_id> and /pilot metrics {pilot_id}"""


def build_pilot_list_reply() -> str:
    records = pilot_records()
    if not records:
        return "Pilots:\n- none."
    lines = ["Pilots:"]
    for record in records[:10]:
        lines.append(
            f"- {record['pilot_id']} | {record.get('status')} | project={record.get('project_id')} | updated={record.get('updated_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def build_pilot_show_reply(pilot_id: str) -> str:
    normalized_pilot_id = normalize_pilot_id(pilot_id)
    data = pilot_metrics_data(normalized_pilot_id)
    meta = data["meta"]
    return f"""Pilot summary: {normalized_pilot_id}
- status: {meta.get('status', 'unknown')}
- title: {pilot_title_from_text(normalized_pilot_id, data['text'])}
- project_id: {meta.get('project_id', '')}
- updated_at: {meta.get('updated_at', '')}
- goal: {safe_preview(task_section(data['text'], 'Goal'), 240)}
- scope: {safe_preview(task_section(data['text'], 'Scope'), 240)}
{build_pilot_metrics_body(data)}
- next_actions: {safe_preview(task_section(data['text'], 'Next Actions'), 240)}"""


def build_pilot_add_task_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /pilot add-task <pilot_id> <task_id>"
    pilot_id = normalize_pilot_id(parts[0])
    task_id = normalize_task_id(parts[1])
    text = read_pilot(pilot_id)
    task_text = read_task(task_id)
    task_meta = task_metadata(task_text)
    task_project = task_meta.get("project_id", "")
    if task_project:
        validate_pilot_project_link(text, task_project)
    now = iso_now()
    line = f"- {task_id} | {task_meta.get('status', 'unknown')} | {task_title_from_text(task_id, task_text)}"
    text = add_unique_pilot_line(text, "Tasks Included", task_id, line)
    text = replace_task_field(text, "updated_at", now)
    text = append_to_section(text, "Reports", f"- {now} task linked: {task_id} status={task_meta.get('status', 'unknown')}.")
    write_pilot(pilot_id, text)
    log_event("pilot_task_added", pilot_id=pilot_id, task_id=task_id)
    return f"""Pilot task linked: {pilot_id}
- task_id: {task_id}
- task_status: {task_meta.get('status', 'unknown')}
- next: /pilot metrics {pilot_id}"""


def build_pilot_add_dispatch_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /pilot add-dispatch <pilot_id> <dispatch_id>"
    pilot_id = normalize_pilot_id(parts[0])
    dispatch_id = normalize_dispatch_id(parts[1])
    text = read_pilot(pilot_id)
    dispatch_text = read_dispatch(dispatch_id)
    dispatch_meta = task_metadata(dispatch_text)
    dispatch_project = dispatch_meta.get("project_id", "")
    if dispatch_project:
        validate_pilot_project_link(text, dispatch_project)
    now = iso_now()
    line = (
        f"- {dispatch_id} | {dispatch_meta.get('status', 'unknown')} | "
        f"task={dispatch_meta.get('task_id', '')} | target={dispatch_meta.get('target_executor', '')}"
    )
    text = add_unique_pilot_line(text, "Dispatches", dispatch_id, line)
    text = replace_task_field(text, "updated_at", now)
    text = append_to_section(text, "Reports", f"- {now} dispatch linked: {dispatch_id} status={dispatch_meta.get('status', 'unknown')}.")
    write_pilot(pilot_id, text)
    log_event("pilot_dispatch_added", pilot_id=pilot_id, dispatch_id=dispatch_id)
    return f"""Pilot dispatch linked: {pilot_id}
- dispatch_id: {dispatch_id}
- dispatch_status: {dispatch_meta.get('status', 'unknown')}
- target_executor: {dispatch_meta.get('target_executor', '')}
- next: /pilot metrics {pilot_id}"""


def build_pilot_note_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /pilot note <pilot_id> <single-line note>"
    pilot_id = normalize_pilot_id(parts[0])
    note = sanitize_sensitive_text(parts[1].splitlines()[0]).strip()
    if not note:
        return "Pilot note is empty; nothing written."
    now = iso_now()
    text = read_pilot(pilot_id)
    text = append_to_section(text, "Friction Log", f"- {now} {note}")
    text = replace_task_field(text, "updated_at", now)
    write_pilot(pilot_id, text)
    log_event("pilot_note_added", pilot_id=pilot_id)
    return f"""Pilot note recorded: {pilot_id}
- note: {safe_preview(note, 160)}
- path: workbench/pilots/{pilot_id}.md"""


def build_pilot_metrics_reply(pilot_id: str) -> str:
    normalized_pilot_id = normalize_pilot_id(pilot_id)
    data = pilot_metrics_data(normalized_pilot_id)
    now = iso_now()
    text = data["text"]
    body = build_pilot_metrics_body(data)
    text = set_section_body(text, "Metrics", body)
    text = set_section_body(text, "Time Saved Estimate", f"- {data['estimated_time_saved']} rough estimate based on linked tasks, dispatches, contexts, and return reports.")
    text = replace_task_field(text, "updated_at", now)
    write_pilot(normalized_pilot_id, text)
    return f"""Pilot metrics: {normalized_pilot_id}
{body}

Notes:
- estimated_time_saved is a rough operational estimate, not a verified time log.
- report quality and evidence gaps still require /dispatch qa, /task review, and user decision."""


def build_pilot_complete_summary(pilot_id: str, note: str, data: dict) -> str:
    useful = ["/task new", "/dispatch create", "/dispatch package", "/dispatch receive", "/dispatch qa", "/task review", "/task decide", "/pilot metrics"]
    if data["context_pack_count"]:
        useful.insert(1, "/context pack task")
    unused = []
    if not data["context_pack_count"]:
        unused.append("/context pack task")
    if data["qa_pass_count"] == 0:
        unused.append("qa_pass outcome")
    if data["closed_count"] == 0:
        unused.append("/task close or /dispatch close")
    worth = "yes, continue real-use validation" if data["task_count"] and data["dispatch_count"] else "not enough data yet"
    if data["evidence_gap_count"] or data["needs_evidence_count"]:
        worth = "yes, but prioritize evidence-gap reduction before new features"
    return sanitize_sensitive_text(f"""### Pilot completed at {iso_now()}

completion_note: {note}

哪些地方提效:
- Work orders and dispatch packages reduced manual restructuring; linked tasks={data['task_count']}, dispatches={data['dispatch_count']}.
- Context and dispatch records made handoff/return status easier to inspect; context_pack_count={data['context_pack_count']}.
- Estimated time saved: {data['estimated_time_saved']}.

哪些地方仍然麻烦:
- main_friction: {data['main_friction']}
- evidence_gap_count: {data['evidence_gap_count']}
- live_debt: Octo UI live validation still needs manual proof if not already recorded.

哪些命令最有用:
- {', '.join(useful)}

哪些命令没用上:
- {', '.join(unused) if unused else 'none obvious from this pilot'}

是否值得继续扩展:
- {worth}

下一阶段建议:
- Run one more real project pilot before adding new automation.
- Close live debt with explicit Octo UI evidence.
- Improve only the commands that caused recorded friction, not the execution boundary.
""")


def build_pilot_complete_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "Usage: /pilot complete <pilot_id> <note>"
    pilot_id = normalize_pilot_id(parts[0])
    note = sanitize_sensitive_text(parts[1]).strip() or "completed"
    data = pilot_metrics_data(pilot_id)
    summary = build_pilot_complete_summary(pilot_id, note, data)
    text = data["text"]
    now = iso_now()
    text = set_section_body(text, "Metrics", build_pilot_metrics_body(data))
    text = append_to_section(text, "Lessons Learned", summary)
    text = set_section_body(
        text,
        "Next Actions",
        "- Review pilot evidence gaps and live debt.\n- Decide whether another real pilot is needed before adding features.",
    )
    text = replace_task_field(text, "status", "completed")
    text = replace_task_field(text, "updated_at", now)
    write_pilot(pilot_id, text)
    log_event("pilot_completed", pilot_id=pilot_id)
    return f"""Pilot completed: {pilot_id}
- status: completed

{summary}"""


def build_pilot_dashboard_reply() -> str:
    records = [record for record in pilot_records() if record.get("status") in {"active", "completed"}]
    if not records:
        return "Pilot dashboard:\n- none."
    lines = ["Pilot dashboard:"]
    for record in records[:20]:
        try:
            data = pilot_metrics_data(record["pilot_id"])
            metrics = f"tasks={data['task_count']} dispatches={data['dispatch_count']} returned={data['returned_count']} gaps={data['evidence_gap_count']} saved={data['estimated_time_saved']}"
        except Exception:
            metrics = "metrics=unavailable"
        lines.append(
            f"- {record['pilot_id']} | {record.get('status')} | project={record.get('project_id')} | {metrics} | {record.get('title')}"
        )
    return "\n".join(lines)


def handle_pilot_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/pilot":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_pilot_help_reply()
        if subcommand == "start":
            return build_pilot_start_reply(tail)
        if subcommand == "list":
            return build_pilot_list_reply()
        if subcommand == "show":
            return build_pilot_show_reply(tail)
        if subcommand == "add-task":
            return build_pilot_add_task_reply(tail)
        if subcommand == "add-dispatch":
            return build_pilot_add_dispatch_reply(tail)
        if subcommand == "note":
            return build_pilot_note_reply(tail)
        if subcommand == "metrics":
            return build_pilot_metrics_reply(tail)
        if subcommand == "complete":
            return build_pilot_complete_reply(tail)
        if subcommand == "dashboard":
            return build_pilot_dashboard_reply()
        return build_pilot_help_reply()
    except FileNotFoundError as exc:
        return f"pilot source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"pilot operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"pilot operation failed: {safe_preview(str(exc), 180)}"


def generate_collection_id() -> str:
    ensure_workbench_dirs()
    base = datetime.now().strftime("COLLECT-%Y%m%d-%H%M%S")
    if not collection_path(base).exists():
        return base
    for index in range(1, 100):
        candidate = f"{base}-{index:02d}"
        if not collection_path(candidate).exists():
            return candidate
    raise RuntimeError("could not generate unique collection_id")


def read_collection(collection_id: str) -> str:
    path = collection_path(collection_id)
    if not path.exists():
        raise FileNotFoundError(f"collection not found: {collection_id}")
    return path.read_text(encoding="utf-8")


def write_collection(collection_id: str, text: str) -> None:
    path = collection_path(collection_id)
    path.write_text(collect_clean_text(text), encoding="utf-8")


def collection_title_from_text(collection_id: str, text: str) -> str:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    prefix = f"# {collection_id} "
    if first_line.startswith(prefix):
        return first_line[len(prefix):].strip()
    return "read-only evidence collection"


def collection_records() -> list[dict]:
    ensure_workbench_dirs()
    records = []
    for path in sorted(COLLECTIONS_DIR.glob("COLLECT-*.md")):
        collection_id = path.stem
        try:
            text = path.read_text(encoding="utf-8")
            normalize_collection_id(collection_id)
        except (OSError, ValueError):
            continue
        meta = task_metadata(text)
        records.append(
            {
                "collection_id": collection_id,
                "title": collection_title_from_text(collection_id, text),
                "created_at": meta.get("created_at", ""),
                "task_id": meta.get("task_id", ""),
                "project_id": meta.get("project_id", ""),
                "profile": meta.get("profile", ""),
                "kind": meta.get("kind", "snapshot"),
                "status": meta.get("status", "observed"),
                "smoke_failed": meta.get("smoke_failed", "false") == "true",
                "path": path,
                "text": text,
            }
        )
    return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)


def collections_for_task(task_id: str) -> list[dict]:
    normalized_task_id = normalize_task_id(task_id)
    return [record for record in collection_records() if record.get("task_id") == normalized_task_id]


def collections_for_project(project_id: str) -> list[dict]:
    clean_project_id = validate_project_id(project_id)
    task_ids = {record["task_id"] for record in project_task_records(clean_project_id)}
    return [
        record for record in collection_records()
        if record.get("project_id") == clean_project_id or record.get("task_id") in task_ids
    ]


def collection_counts() -> dict:
    records = collection_records()
    return {
        "collection_count": len(records),
        "latest_collection_id": records[0]["collection_id"] if records else "",
        "smoke_collection_count": sum(1 for record in records if record.get("kind") == "smoke"),
        "failed_collection_count": sum(1 for record in records if record.get("smoke_failed")),
    }


def collect_clean_text(text: str, limit: int | None = None) -> str:
    cleaned = sanitize_sensitive_text(str(text or ""))
    cleaned = re.sub(r"(?i)(^|[\\/\s])\.env(?:\.[A-Za-z0-9_-]+)?", r"\1[REDACTED_ENV_FILE]", cleaned)
    cleaned = cleaned.replace("\x00", "")
    if limit is not None and len(cleaned) > limit:
        cleaned = cleaned[: max(0, limit - 60)] + "\n...[truncated by read-only collector]..."
    return cleaned


def collect_profile(profile_name: str) -> dict:
    profile = str(profile_name or "").strip().lower()
    if profile not in COLLECT_PROFILES:
        raise ValueError("unknown collect profile; use /collect profiles")
    data = dict(COLLECT_PROFILES[profile])
    data["name"] = profile
    data["root"] = Path(data["root"])
    return data


def collect_command_result(label: str, args: list[str], cwd: Path, timeout: int = COLLECT_COMMAND_TIMEOUT) -> dict:
    started = iso_now()
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
        return {
            "label": label,
            "command": " ".join(args),
            "returncode": completed.returncode,
            "stdout": collect_clean_text(completed.stdout, COLLECT_OUTPUT_LIMIT),
            "stderr": collect_clean_text(completed.stderr, COLLECT_OUTPUT_LIMIT),
            "started_at": started,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "label": label,
            "command": " ".join(args),
            "returncode": "timeout",
            "stdout": collect_clean_text(exc.stdout or "", COLLECT_OUTPUT_LIMIT),
            "stderr": collect_clean_text(exc.stderr or "", COLLECT_OUTPUT_LIMIT),
            "started_at": started,
            "timed_out": True,
        }
    except Exception as exc:
        return {
            "label": label,
            "command": " ".join(args),
            "returncode": "error",
            "stdout": "",
            "stderr": collect_clean_text(f"{type(exc).__name__}: {exc}", 1000),
            "started_at": started,
            "timed_out": False,
        }


def collect_git_evidence(root: Path) -> list[dict]:
    if not root.exists():
        return [{
            "label": "profile_root",
            "command": "root_exists",
            "returncode": "missing",
            "stdout": "",
            "stderr": f"profile root not found: {collect_clean_text(str(root), 400)}",
            "started_at": iso_now(),
            "timed_out": False,
        }]
    commands = [
        ("git_branch", ["git", "branch", "--show-current"]),
        ("git_head", ["git", "rev-parse", "HEAD"]),
        ("git_status_short", ["git", "status", "--short"]),
        ("git_log_last", ["git", "log", "-1", "--oneline"]),
        ("git_diff_check", ["git", "diff", "--check"]),
    ]
    return [collect_command_result(label, args, root) for label, args in commands]


def collect_file_tail(path: Path, root: Path, max_lines: int = COLLECT_TAIL_LINES) -> dict:
    clean_path = Path(path)
    if ".env" in clean_path.name.lower():
        return {"path": "[REDACTED_ENV_FILE]", "exists": False, "summary": "refused to read env-like file"}
    try:
        resolved = clean_path.resolve()
        root_resolved = root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            return {"path": collect_clean_text(str(clean_path), 400), "exists": False, "summary": "refused: outside profile root"}
        if not resolved.exists():
            return {"path": collect_clean_text(str(clean_path), 400), "exists": False, "summary": "not found"}
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-max_lines:])
        return {
            "path": collect_clean_text(str(clean_path), 400),
            "exists": True,
            "line_count": len(lines),
            "tail": collect_clean_text(tail, COLLECT_OUTPUT_LIMIT),
        }
    except Exception as exc:
        return {"path": collect_clean_text(str(clean_path), 400), "exists": False, "summary": collect_clean_text(f"{type(exc).__name__}: {exc}", 1000)}


def collect_runtime_evidence(paths: list[Path], root: Path) -> list[dict]:
    return [collect_file_tail(path, root, 80) for path in paths]


def collect_workbench_evidence() -> dict:
    counts = workbench_counts()
    collect_view = collection_counts()
    recent_tasks = task_records()[:5]
    recent_dispatches = dispatch_records()[:5]
    recent_pilots = pilot_records()[:5]
    return {
        "counts": {**counts, **collect_view},
        "recent_tasks": recent_tasks,
        "recent_dispatches": recent_dispatches,
        "recent_pilots": recent_pilots,
    }


def collect_process_port_evidence(profile_name: str) -> list[dict]:
    if profile_name != "kiro-gateway":
        return []
    commands = [
        (
            "port_8080_listeners",
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,State,OwningProcess | Format-Table -AutoSize | Out-String",
            ],
        ),
        (
            "python_process_summary",
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' -or $_.CommandLine -like '*uvicorn*' } | Select-Object ProcessId,Name,CommandLine | Format-List | Out-String",
            ],
        ),
    ]
    return [collect_command_result(label, args, Path.cwd(), timeout=10) for label, args in commands]


def collect_smoke_evidence(root: Path) -> list[dict]:
    results = []
    for script_name in OCTO_BRIDGE_SMOKE_ALLOWLIST:
        script_path = root / script_name
        if not script_path.exists():
            results.append(
                {
                    "label": script_name,
                    "command": f"{sys.executable} {script_name}",
                    "returncode": "missing",
                    "stdout": "",
                    "stderr": "smoke script not found",
                    "started_at": iso_now(),
                    "timed_out": False,
                }
            )
            continue
        results.append(
            collect_command_result(
                script_name,
                [sys.executable, script_name],
                root,
                timeout=COLLECT_SMOKE_TIMEOUT,
            )
        )
    return results


def collect_section_from_commands(results: list[dict]) -> str:
    if not results:
        return "- none"
    lines = []
    for item in results:
        lines.append(f"- label: {item.get('label')}")
        lines.append(f"  command: {item.get('command')}")
        lines.append(f"  returncode: {item.get('returncode')}")
        lines.append(f"  timed_out: {str(bool(item.get('timed_out'))).lower()}")
        stdout = item.get("stdout") or ""
        stderr = item.get("stderr") or ""
        if stdout:
            lines.append("  stdout:")
            lines.extend(f"    {line}" for line in stdout.splitlines()[:80])
        if stderr:
            lines.append("  stderr:")
            lines.extend(f"    {line}" for line in stderr.splitlines()[:80])
    return "\n".join(lines)


def git_status_changed_files(git_results: list[dict]) -> list[str]:
    status_text = ""
    for item in git_results:
        if item.get("label") == "git_status_short":
            status_text = item.get("stdout", "")
            break
    files = []
    for line in status_text.splitlines():
        value = line.strip()
        if not value:
            continue
        path_part = value[3:].strip() if len(value) > 3 else value
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        files.append(collect_clean_text(path_part, 300))
    return files[:50]


def git_status_is_clean(git_results: list[dict]) -> bool:
    for item in git_results:
        if item.get("label") == "git_status_short":
            return not str(item.get("stdout", "")).strip() and item.get("returncode") == 0
    return False


def git_diff_check_ok(git_results: list[dict]) -> bool:
    for item in git_results:
        if item.get("label") == "git_diff_check":
            return item.get("returncode") == 0
    return False


def smoke_all_pass(smoke_results: list[dict]) -> bool:
    return bool(smoke_results) and all(item.get("returncode") == 0 for item in smoke_results)


def collect_sensitive_scan(*parts: object) -> dict:
    text = "\n".join(str(part or "") for part in parts)
    findings = detect_sensitive_findings(text)
    return {
        "sensitive_risk": bool(findings),
        "finding_count": len(findings),
        "findings": findings[:10],
        "zero_hit_ok": detect_sensitive_zero_hit_ok(text),
    }


def build_standard_collection_report(task_id: str, collection_id: str, profile: str, kind: str, git_results: list[dict], smoke_results: list[dict], log_results: list[dict], runtime_results: list[dict], process_results: list[dict], workbench_data: dict, sensitive_scan: dict) -> str:
    modified_files = git_status_changed_files(git_results)
    modified_block = "\n".join(f"- {item}" for item in modified_files) if modified_files else "- none / read-only collection"
    command_lines = [f"- {item.get('command')} | returncode={item.get('returncode')}" for item in git_results + smoke_results + process_results]
    if not command_lines:
        command_lines = ["- none"]
    smoke_lines = [f"- {item.get('label')}: returncode={item.get('returncode')}" for item in smoke_results] or ["- no smoke run for this collection"]
    git_clean = git_status_is_clean(git_results)
    diff_ok = git_diff_check_ok(git_results)
    log_lines = []
    for item in log_results:
        log_lines.append(f"- {item.get('path')}: exists={str(bool(item.get('exists'))).lower()} lines={item.get('line_count', 'n/a')}")
    for item in runtime_results:
        log_lines.append(f"- {item.get('path')}: exists={str(bool(item.get('exists'))).lower()} lines={item.get('line_count', 'n/a')}")
    if not log_lines:
        log_lines = ["- no log/runtime files collected"]
    observed = [
        f"- profile: {profile}",
        f"- collection_kind: {kind}",
        f"- git_clean: {str(git_clean).lower()}",
        f"- git_diff_check_ok: {str(diff_ok).lower()}",
        f"- smoke_all_pass: {str(smoke_all_pass(smoke_results)).lower() if smoke_results else 'not_run'}",
        f"- workbench_collection_count: {workbench_data.get('counts', {}).get('collection_count', 0)}",
    ]
    risks = []
    if sensitive_scan.get("sensitive_risk"):
        risks.append("- sensitive-looking values were found and redacted; inspect collection before trusting it.")
    if smoke_results and not smoke_all_pass(smoke_results):
        risks.append("- one or more smoke scripts failed or timed out.")
    if not diff_ok:
        risks.append("- git diff --check returned non-zero or was unavailable.")
    if not risks:
        risks.append("- none observed in read-only collection output.")
    return f"""Task id: {task_id}
Collection id: {collection_id}

Execution summary:
- Read-only whitelist evidence collection completed for profile={profile}, kind={kind}.
- No user-provided command was executed.
- No files were modified by the collector except Workbench collection/evidence/task records.

Modified files:
{modified_block}

Commands:
{chr(10).join(command_lines)}

Test results:
{chr(10).join(smoke_lines)}
- git_diff_check_ok: {str(diff_ok).lower()}

Key logs or screenshots:
{chr(10).join(log_lines)}

Observed:
{chr(10).join(observed)}

Verified:
- false; collection output is observed evidence only.

Unverified:
- Human verification is still required with /evidence mark <task_id> <evidence_id> verified <reason>.

Unresolved risks:
{chr(10).join(risks)}

Rollback notes:
- No project files were modified by the read-only collector; no rollback needed for target code.
- Workbench records can be archived or superseded if the collection is not useful.

Sensitive Information Handling:
- Outputs were sanitized before writing.
- Redaction labels avoid original token prefixes.
- .env files were not read.
"""


def build_collection_markdown(collection_id: str, task_id: str, profile: str, kind: str, git_results: list[dict], smoke_results: list[dict], log_results: list[dict], runtime_results: list[dict], process_results: list[dict], workbench_data: dict, sensitive_scan: dict, standard_report: str) -> str:
    task_text = read_task(task_id)
    task_meta = task_metadata(task_text)
    project_id = task_meta.get("project_id", "")
    title = f"{profile} {kind} read-only evidence"
    smoke_failed = bool(smoke_results) and not smoke_all_pass(smoke_results)
    missing = []
    if not git_results:
        missing.append("- git evidence not collected")
    if kind == "smoke" and not smoke_results:
        missing.append("- smoke evidence not collected")
    if sensitive_scan.get("sensitive_risk"):
        missing.append("- sensitive risk requires inspection")
    if not missing:
        missing.append("- none")
    risks = []
    if smoke_failed:
        risks.append("- smoke_failed: one or more allowlisted smoke scripts did not pass")
    if sensitive_scan.get("sensitive_risk"):
        risks.append("- sensitive_risk: output was redacted; inspect before verification")
    if not git_diff_check_ok(git_results):
        risks.append("- git_diff_check_nonzero_or_unavailable")
    if not risks:
        risks.append("- none observed")
    command_results = git_results + smoke_results + process_results
    log_lines = []
    for item in log_results:
        log_lines.append(f"- path: {item.get('path')} | exists={str(bool(item.get('exists'))).lower()} | lines={item.get('line_count', 'n/a')}")
        if item.get("tail"):
            log_lines.append("  tail:")
            log_lines.extend(f"    {line}" for line in str(item.get("tail")).splitlines()[:80])
    runtime_lines = []
    for item in runtime_results:
        runtime_lines.append(f"- path: {item.get('path')} | exists={str(bool(item.get('exists'))).lower()} | lines={item.get('line_count', 'n/a')}")
        if item.get("tail"):
            runtime_lines.append("  tail:")
            runtime_lines.extend(f"    {line}" for line in str(item.get("tail")).splitlines()[:80])
    workbench_lines = [
        f"- {key}: {value}" for key, value in sorted((workbench_data.get("counts") or {}).items())
    ]
    observed = [
        f"- git_clean: {str(git_status_is_clean(git_results)).lower()}",
        f"- git_diff_check_ok: {str(git_diff_check_ok(git_results)).lower()}",
        f"- smoke_all_pass: {str(smoke_all_pass(smoke_results)).lower() if smoke_results else 'not_run'}",
        f"- profile_root: {collect_clean_text(str(COLLECT_PROFILES[profile]['root']), 400)}",
    ]
    finding_lines = [
        f"- {item.get('finding')} severity={item.get('severity')} reason={item.get('reason')}"
        for item in sensitive_scan.get("findings", [])
    ] or ["- none"]
    return f"""# {collection_id} {title}

collection_id: {collection_id}
status: observed
kind: {kind}
created_at: {iso_now()}
task_id: {task_id}
project_id: {project_id}
profile: {profile}
mode: read_only_collect
executor: atlas-bridge
verified: false
smoke_failed: {str(smoke_failed).lower()}
runtime_injection_enabled: false
external_execution_enabled: false

## Scope
- Read-only whitelist collection for task {task_id}.
- Profile root is fixed by code; user paths and user commands are not accepted.
- This is observed evidence only and must not be treated as verified.

## Commands Run
{collect_section_from_commands(command_results)}

## Git Evidence
{collect_section_from_commands(git_results)}

## Smoke Evidence
{collect_section_from_commands(smoke_results) if smoke_results else '- not run for this collection.'}

## Log Evidence
{chr(10).join(log_lines) if log_lines else '- no logs collected.'}

## Runtime Evidence
{chr(10).join(runtime_lines) if runtime_lines else '- no runtime files collected.'}

## Workbench Evidence
{chr(10).join(workbench_lines) if workbench_lines else '- no workbench summary available.'}

## Sensitive Scan
- sensitive_risk: {str(bool(sensitive_scan.get('sensitive_risk'))).lower()}
- finding_count: {sensitive_scan.get('finding_count', 0)}
- zero_hit_ok: {str(bool(sensitive_scan.get('zero_hit_ok'))).lower()}
{chr(10).join(finding_lines)}

## Observed Facts
{chr(10).join(observed)}

## Missing Evidence
{chr(10).join(missing)}

## Risks
{chr(10).join(risks)}

## Standard Return Report
{standard_report}

## Do Not Treat As Verified
- This collection is automatic observed evidence.
- It is not verified evidence.
- It does not prove task completion.
- The user must run /evidence mark <task_id> <evidence_id> verified <reason> before Atlas Review treats it as verified.
- The user must still decide with /task decide <task_id> pass|needs_evidence|blocked|cancelled <reason>.
"""


def create_collection(task_id: str, profile_name: str, kind: str = "snapshot") -> tuple[str, str, str]:
    normalized_task_id = normalize_task_id(task_id)
    read_task(normalized_task_id)
    profile = collect_profile(profile_name)
    profile_name = profile["name"]
    if kind not in {"snapshot", "smoke"}:
        raise ValueError("collection kind must be snapshot or smoke")
    if kind == "smoke" and profile_name != "octo-bridge":
        raise ValueError("/collect smoke currently supports octo-bridge only")
    root = profile["root"]
    git_results = collect_git_evidence(root)
    smoke_results = collect_smoke_evidence(root) if kind == "smoke" else []
    log_results = [collect_file_tail(path, root) for path in profile.get("logs", [])]
    runtime_results = collect_runtime_evidence(profile.get("runtime", []), root)
    process_results = collect_process_port_evidence(profile_name)
    workbench_data = collect_workbench_evidence() if profile_name == "octo-bridge" else {"counts": {}, "recent_tasks": [], "recent_dispatches": [], "recent_pilots": []}
    sensitive_scan = collect_sensitive_scan(
        git_results,
        smoke_results,
        log_results,
        runtime_results,
        process_results,
        workbench_data,
    )
    collection_id = generate_collection_id()
    standard_report = build_standard_collection_report(
        normalized_task_id,
        collection_id,
        profile_name,
        kind,
        git_results,
        smoke_results,
        log_results,
        runtime_results,
        process_results,
        workbench_data,
        sensitive_scan,
    )
    markdown = build_collection_markdown(
        collection_id,
        normalized_task_id,
        profile_name,
        kind,
        git_results,
        smoke_results,
        log_results,
        runtime_results,
        process_results,
        workbench_data,
        sensitive_scan,
        standard_report,
    )
    write_collection(collection_id, markdown)
    evidence_id = attach_collection_to_task(normalized_task_id, collection_id)
    log_event("collection_created", collection_id=collection_id, task_id=normalized_task_id, profile=profile_name, kind=kind)
    return collection_id, evidence_id, standard_report


def attach_collection_to_task(task_id: str, collection_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    normalized_collection_id = normalize_collection_id(collection_id)
    task_text = read_task(normalized_task_id)
    collection_text = read_collection(normalized_collection_id)
    report = task_section(collection_text, "Standard Return Report")
    if not report:
        report = f"Collection id: {normalized_collection_id}\n- observed evidence only."
    evidence_body = f"""Collection evidence attached.

collection_id: {normalized_collection_id}
collection_file: workbench/collections/{normalized_collection_id}.md
verified: false

{report}
"""
    evidence_id = create_evidence_entry(normalized_task_id, "collection", evidence_body, source="collector", verified="no", sync_task=False)
    now = iso_now()
    task_text = append_to_section(task_text, "Execution Report", f"### Collection {normalized_collection_id} at {now}\n{report}")
    task_text = append_to_section(task_text, "Timeline", f"- {now} collection {normalized_collection_id} attached as evidence {evidence_id}.")
    task_text = update_task_status(task_text, "reported")
    write_task(normalized_task_id, task_text)
    sync_task_evidence_state(normalized_task_id)
    log_event("collection_attached", collection_id=normalized_collection_id, task_id=normalized_task_id, evidence_id=evidence_id)
    return evidence_id


def build_collect_help_reply() -> str:
    return """Atlas read-only collection commands
- /collect help
- /collect profiles
- /collect snapshot <task_id> <octo-bridge|kiro-gateway>
- /collect smoke <task_id> octo-bridge
- /collect list
- /collect show <collection_id>
- /collect report <collection_id>
- /collect attach <task_id> <collection_id>

Safety:
- read-only collection only
- runs only static allowlisted commands
- does not accept user shell commands
- does not read .env
- does not call Codex/Kiro
- does not mark evidence verified
- does not decide pass"""


def build_collect_profiles_reply() -> str:
    lines = ["Collect profiles:"]
    for name, profile in COLLECT_PROFILES.items():
        smokes = ", ".join(profile.get("smokes", [])) if profile.get("smokes") else "none"
        lines.append(f"- {name} | root={collect_clean_text(str(profile.get('root')), 400)} | smoke_allowlist={smokes}")
    lines.append("- arbitrary_command_enabled: false")
    lines.append("- collect_mode: read_only_whitelist")
    return "\n".join(lines)


def build_collect_snapshot_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if len(parts) != 2:
        return "Usage: /collect snapshot <task_id> <profile>"
    collection_id, evidence_id, report = create_collection(parts[0], parts[1], kind="snapshot")
    return f"""Collection snapshot created: {collection_id}
- task_id: {normalize_task_id(parts[0])}
- profile: {parts[1].lower()}
- evidence_id: {evidence_id}
- path: workbench/collections/{collection_id}.md
- verified: false
- arbitrary_command_enabled: false

Standard report preview:
{safe_preview(report, 900)}

Next:
- /collect show {collection_id}
- /collect report {collection_id}
- /evidence mark {normalize_task_id(parts[0])} {evidence_id} verified <reason>"""


def build_collect_smoke_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if len(parts) != 2:
        return "Usage: /collect smoke <task_id> octo-bridge"
    collection_id, evidence_id, report = create_collection(parts[0], parts[1], kind="smoke")
    text = read_collection(collection_id)
    meta = task_metadata(text)
    return f"""Collection smoke created: {collection_id}
- task_id: {normalize_task_id(parts[0])}
- profile: {parts[1].lower()}
- evidence_id: {evidence_id}
- smoke_failed: {meta.get('smoke_failed', 'false')}
- whitelist_count: {len(OCTO_BRIDGE_SMOKE_ALLOWLIST)}
- path: workbench/collections/{collection_id}.md
- verified: false
- arbitrary_command_enabled: false

Standard report preview:
{safe_preview(report, 900)}

Next:
- /collect report {collection_id}
- /task qa {normalize_task_id(parts[0])}"""


def build_collect_list_reply() -> str:
    records = collection_records()[:10]
    if not records:
        return "Collections:\n- none."
    lines = ["Collections:"]
    for record in records:
        lines.append(
            f"- {record['collection_id']} | {record.get('kind')} | profile={record.get('profile')} | task={record.get('task_id')} | smoke_failed={str(bool(record.get('smoke_failed'))).lower()} | {record.get('created_at')} | {record.get('title')}"
        )
    return "\n".join(lines)


def build_collect_show_reply(collection_id: str) -> str:
    normalized_collection_id = normalize_collection_id(collection_id)
    text = read_collection(normalized_collection_id)
    meta = task_metadata(text)
    return f"""Collection summary: {normalized_collection_id}
- title: {collection_title_from_text(normalized_collection_id, text)}
- created_at: {meta.get('created_at', '')}
- task_id: {meta.get('task_id', '')}
- project_id: {meta.get('project_id', '') or 'unassigned'}
- profile: {meta.get('profile', '')}
- kind: {meta.get('kind', '')}
- verified: {meta.get('verified', 'false')}
- smoke_failed: {meta.get('smoke_failed', 'false')}
- git: {safe_preview(task_section(text, 'Git Evidence'), 260)}
- smoke: {safe_preview(task_section(text, 'Smoke Evidence'), 260)}
- runtime: {safe_preview(task_section(text, 'Runtime Evidence'), 260)}
- workbench: {safe_preview(task_section(text, 'Workbench Evidence'), 260)}
- risks: {safe_preview(task_section(text, 'Risks'), 260)}
- next: /collect report {normalized_collection_id}"""


def build_collect_report_reply(collection_id: str) -> str:
    normalized_collection_id = normalize_collection_id(collection_id)
    text = read_collection(normalized_collection_id)
    report = task_section(text, "Standard Return Report")
    if not report:
        report = f"Task id: {task_metadata(text).get('task_id', '')}\nCollection id: {normalized_collection_id}\n\nExecution summary:\n- collection report unavailable"
    return report


def build_collect_attach_reply(tail: str) -> str:
    parts = str(tail or "").strip().split()
    if len(parts) != 2:
        return "Usage: /collect attach <task_id> <collection_id>"
    evidence_id = attach_collection_to_task(parts[0], parts[1])
    return f"""Collection attached: {normalize_collection_id(parts[1])}
- task_id: {normalize_task_id(parts[0])}
- evidence_id: {evidence_id}
- verified: false
- next: /evidence mark {normalize_task_id(parts[0])} {evidence_id} verified <reason>"""


def handle_collect_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/collect":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_collect_help_reply()
        if subcommand == "profiles":
            return build_collect_profiles_reply()
        if subcommand == "snapshot":
            return build_collect_snapshot_reply(tail)
        if subcommand == "smoke":
            return build_collect_smoke_reply(tail)
        if subcommand == "list":
            return build_collect_list_reply()
        if subcommand == "show":
            return build_collect_show_reply(tail)
        if subcommand == "report":
            return build_collect_report_reply(tail)
        if subcommand == "attach":
            return build_collect_attach_reply(tail)
        return build_collect_help_reply()
    except FileNotFoundError as exc:
        return f"collection source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"collection operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"collection operation failed: {safe_preview(str(exc), 180)}"


def build_project_help_reply() -> str:
    return """Atlas 项目索引命令
- /project help：查看本说明。
- /project new <project_id> <项目名称>：创建项目索引。
- /project list：列出项目。
- /project show <project_id>：查看项目摘要。
- /project set <project_id> status <active|paused|archived>：更新项目状态。
- /project set <project_id> priority <P0|P1|P2|P3>：更新项目优先级。
- /project note <project_id> <单行备注>：追加项目备注。
- /project attach <project_id> <task_id>：把已有任务关联到项目，不覆盖已有归属。
- /project tasks <project_id>：列出项目任务。
- /project brief <project_id>：生成项目简报。
- /project dashboard：查看跨项目看板。

project_id 规则：只允许小写字母、数字、-、_。Atlas/Bridge 只做索引和调度，不执行命令。"""


def build_project_new_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/project new <project_id> <项目名称>"
    project_id, text = create_project(parts[0], parts[1])
    return f"""项目已创建：{project_id}

路径：workbench/projects/{project_id}.md

项目索引：
{text}

下一步：
- 创建项目任务：/task new <标题> --project {project_id}
- 或关联已有任务：/project attach {project_id} <task_id>"""


def build_project_list_reply() -> str:
    records = project_records()
    if not records:
        return "项目列表：暂无。"
    lines = ["项目列表："]
    for record in records[:20]:
        task_count = len(project_task_records(record["project_id"]))
        lines.append(
            f"- {record['project_id']} | {record['status']} | {record['priority'] or 'P?'} | tasks={task_count} | {record['updated_at']} | {record['title']}"
        )
    return "\n".join(lines)


def build_project_show_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    text = read_project(clean_project_id)
    meta = project_metadata(text)
    tasks = project_task_records(clean_project_id)
    active_count = sum(1 for item in tasks if item.get("status") in OPEN_TASK_STATUSES)
    blocked_count = sum(1 for item in tasks if item.get("status") == "blocked")
    next_actions = safe_preview(task_section(text, "Next Actions"), 220) or "未填写"
    current_state = safe_preview(task_section(text, "Current State"), 220) or "未填写"
    notes = safe_preview(task_section(text, "Notes"), 180) or "暂无"
    return f"""项目摘要：{clean_project_id}
- title：{project_title_from_text(clean_project_id, text)}
- status：{meta.get('status', 'unknown')}
- priority：{meta.get('priority', '')}
- updated_at：{meta.get('updated_at', '')}
- active_tasks：{active_count}
- blocked_tasks：{blocked_count}
- current_state：{current_state}
- next_actions：{next_actions}
- notes：{notes}"""


def build_project_set_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=2)
    if len(parts) != 3:
        return "用法：/project set <project_id> status <active|paused|archived> 或 /project set <project_id> priority <P0|P1|P2|P3>"
    project_id, field, value = parts
    clean_project_id = validate_project_id(project_id)
    field = field.lower()
    normalized_value = value.strip()
    if field == "status":
        normalized_value = normalized_value.lower()
        if normalized_value not in {"active", "paused", "archived"}:
            return "status 无效。可用：active、paused、archived。"
    elif field == "priority":
        normalized_value = normalized_value.upper()
        if normalized_value not in {"P0", "P1", "P2", "P3"}:
            return "priority 无效。可用：P0、P1、P2、P3。"
    else:
        return "字段无效。可用：status、priority。"

    text = read_project(clean_project_id)
    text = replace_task_field(text, field, normalized_value)
    text = replace_task_field(text, "updated_at", iso_now())
    write_project(clean_project_id, text)
    log_event("project_set", project_id=clean_project_id, field=field, value=normalized_value)
    return f"""项目已更新：{clean_project_id}
- {field}：{normalized_value}
- 路径：workbench/projects/{clean_project_id}.md"""


def build_project_note_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/project note <project_id> <单行备注>"
    clean_project_id = validate_project_id(parts[0])
    note = sanitize_sensitive_text(parts[1].splitlines()[0]).strip()
    if not note:
        return "项目备注为空，未写入。"
    now = iso_now()
    text = read_project(clean_project_id)
    text = append_to_section(text, "Notes", f"- {now} {note}")
    text = replace_task_field(text, "updated_at", now)
    write_project(clean_project_id, text)
    log_event("project_note_added", project_id=clean_project_id)
    return f"""项目备注已追加：{clean_project_id}
- 路径：workbench/projects/{clean_project_id}.md"""


def build_project_attach_reply(tail: str) -> str:
    parts = str(tail or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return "用法：/project attach <project_id> <task_id>"
    clean_project_id = validate_project_id(parts[0])
    task_id = attach_task_to_project(clean_project_id, parts[1])
    return f"""任务已关联项目：
- project：{clean_project_id}
- task_id：{task_id}
- 去重：若项目中已有该 task_id，不会重复添加。"""


def build_project_tasks_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    read_project(clean_project_id)
    records = project_task_records(clean_project_id)
    if not records:
        return f"项目任务：{clean_project_id}\n- 暂无关联任务。"
    lines = [f"项目任务：{clean_project_id}"]
    for record in records[:30]:
        lines.append(f"- {record['task_id']} | {record['status']} | {record['updated_at']} | {record['title']}")
    return "\n".join(lines)


def build_project_brief_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    text = read_project(clean_project_id)
    meta = project_metadata(text)
    records = project_task_records(clean_project_id)
    counts = {}
    for record in records:
        status = record.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    evidence_gap_tasks = []
    live_skipped_tasks = []
    for record in records:
        try:
            analysis = evidence_analysis(record["task_id"])
        except Exception:
            continue
        if analysis["has_gaps"]:
            evidence_gap_tasks.append(record)
        if analysis["live_skipped"]:
            live_skipped_tasks.append(record)
    active_tasks = [record for record in records if record.get("status") in OPEN_TASK_STATUSES][:10]
    blocked_tasks = [record for record in records if record.get("status") == "blocked"][:10]
    needs_tasks = [record for record in records if record.get("status") == "needs_evidence"][:10]
    project_retros = [record for record in retro_records() if record.get("project_id") == clean_project_id]
    recent_lessons = " ".join(task_section(record.get("text", ""), "Lessons Learned") for record in project_retros[:3])
    candidate_improvements = " ".join(task_section(record.get("text", ""), "Candidate Improvements") for record in project_retros[:3])
    learning_project = learning_project_counts(clean_project_id)
    apply_project = project_apply_counts(clean_project_id)
    project_contexts = [record for record in context_records() if record.get("source_project_id") == clean_project_id]
    latest_context_pack = project_contexts[0]["context_id"] if project_contexts else "none"
    playbook_advisory_count = sum(1 for line in playbook_advisory_for_project(clean_project_id).splitlines() if line.startswith("- ") and "no relevant" not in line)
    project_learning = [record for record in learning_records() if record.get("source_project_id") == clean_project_id]
    retro_task_ids = {record["task_id"] for record in project_retros}
    suggested_retro_tasks = [
        record for record in records
        if record.get("status") in {"passed", "archived", "needs_evidence", "blocked"} and record["task_id"] not in retro_task_ids
    ][:10]
    lines = [
        f"项目简报：{clean_project_id}",
        f"- title：{project_title_from_text(clean_project_id, text)}",
        f"- status：{meta.get('status', 'unknown')}",
        f"- priority：{meta.get('priority', '')}",
        f"- updated_at：{meta.get('updated_at', '')}",
        f"- 任务统计：{', '.join(f'{key}={value}' for key, value in sorted(counts.items())) if counts else '暂无任务'}",
        f"- evidence_gaps：{len(evidence_gap_tasks)}",
        f"- needs_evidence：{len(needs_tasks)}",
        f"- live_skipped：{len(live_skipped_tasks)}",
        f"- retro_count：{len(project_retros)}",
        f"- learning_proposal_count：{learning_project['proposal_count']}",
        f"- approved_learning_count：{learning_project['approved_count']}",
        f"- deferred_learning_count：{learning_project['deferred_count']}",
        f"- not_applied_learning_count：{learning_project['not_applied_count']}",
        f"- project_playbook_entries: {apply_project['playbook_entry_count']}",
        f"- applied_learnings: {apply_project['applied_learning_count']}",
        f"- reverted_learnings: {apply_project['reverted_learning_count']}",
        f"- suggested_not_applied_learnings: {apply_project['suggested_not_applied_learnings']}",
        f"- latest_context_pack: {latest_context_pack}",
        f"- playbook_advisory_count: {playbook_advisory_count}",
        f"- suggested_context_for_next_task: /context pack task <task_id>",
        "",
        "当前状态：",
        f"- {safe_preview(task_section(text, 'Current State'), 260) or '未填写'}",
        "",
        "活跃任务：",
    ]
    lines.extend(
        [f"- {record['task_id']} | {record['status']} | {record['title']}" for record in active_tasks]
        or ["- 暂无。"]
    )
    lines.append("")
    lines.append("阻塞任务：")
    lines.extend(
        [f"- {record['task_id']} | {record['status']} | {record['title']}" for record in blocked_tasks]
        or ["- 暂无。"]
    )
    lines.append("")
    lines.append("证据缺口任务：")
    lines.extend(
        [f"- {record['task_id']} | {record['status']} | {record['title']}" for record in evidence_gap_tasks[:10]]
        or ["- 暂无。"]
    )
    lines.append("")
    lines.append("live 验收跳过任务：")
    lines.extend(
        [f"- {record['task_id']} | {record['status']} | {record['title']}" for record in live_skipped_tasks[:10]]
        or ["- 暂无。"]
    )
    lines.append("")
    lines.append("Retro 视角：")
    lines.append(f"- 最近 Lessons Learned：{safe_preview(recent_lessons, 360) or '暂无。'}")
    lines.append(f"- 候选改进项：{safe_preview(candidate_improvements, 360) or '暂无。'}")
    lines.append("- 建议复盘的任务：")
    lines.extend(
        [f"- {record['task_id']} | {record['status']} | {record['title']}" for record in suggested_retro_tasks]
        or ["- 暂无。"]
    )
    lines.append("")
    lines.append("Learning 视角：")
    lines.append(f"- 本项目 proposal 数：{learning_project['proposal_count']}")
    lines.append(f"- approved：{learning_project['approved_count']}；deferred：{learning_project['deferred_count']}；not_applied：{learning_project['not_applied_count']}")
    if project_learning:
        lines.append(f"- 最近 learning：{project_learning[0]['learn_id']} | {project_learning[0]['status']} | {project_learning[0]['title']}")
        lines.append("- 建议：进入 /learn review，确认 evidence、rollback、acceptance test 后再 approve。")
    elif candidate_improvements:
        lines.append("- 建议：已有 candidate improvements，可执行 /learn propose retro <task_id>。")
    else:
        lines.append("- 建议：暂无 learning review 候选。")
    lines.append("")
    lines.append("Playbook view:")
    lines.append(f"- project_playbook_entries: {apply_project['playbook_entry_count']}")
    lines.append(f"- applied_learnings: {apply_project['applied_learning_count']}")
    lines.append(f"- reverted_learnings: {apply_project['reverted_learning_count']}")
    lines.append(f"- suggested_not_applied_learnings: {apply_project['suggested_not_applied_learnings']}")
    lines.append("")
    lines.append("Context view:")
    lines.append(f"- latest_context_pack: {latest_context_pack}")
    lines.append(f"- playbook_advisory_count: {playbook_advisory_count}")
    lines.append("- suggested_context_for_next_task: /context pack task <task_id>")
    lines.extend(
        [
            "",
            "下一步：",
            f"- {safe_preview(task_section(text, 'Next Actions'), 260) or '优先补 evidence gaps，再按任务闭环推进。'}",
        ]
    )
    if evidence_gap_tasks:
        lines.append(f"- 推荐今日补证据任务：{evidence_gap_tasks[0]['task_id']}。")
    return "\n".join(lines)


def build_project_dashboard_reply() -> str:
    projects = project_records()
    tasks = task_records()
    status_counts: dict[str, int] = {}
    for project in projects:
        status = project.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    unassigned = [task for task in tasks if not task.get("project_id") and task.get("status") in OPEN_TASK_STATUSES]
    evidence_gap_count = 0
    live_skipped_count = 0
    needs_evidence_count = sum(1 for task in tasks if task.get("status") == "needs_evidence")
    retros = retro_records()
    approved_retros = [record for record in retros if record.get("status") == "approved"]
    candidate_improvement_count = sum(1 for record in retros if task_section(record.get("text", ""), "Candidate Improvements").strip())
    learn_counts = learning_counts()
    apply_view = apply_counts()
    context_view = context_counts()
    task_analysis_cache = {}
    for task in tasks:
        try:
            task_analysis_cache[task["task_id"]] = evidence_analysis(task["task_id"])
        except Exception:
            continue
        if task_analysis_cache[task["task_id"]]["has_gaps"]:
            evidence_gap_count += 1
        if task_analysis_cache[task["task_id"]]["live_skipped"]:
            live_skipped_count += 1
    lines = [
        "Atlas 跨项目看板",
        f"- projects：{len(projects)}",
        f"- active：{status_counts.get('active', 0)}",
        f"- paused：{status_counts.get('paused', 0)}",
        f"- archived：{status_counts.get('archived', 0)}",
        f"- unassigned_open_tasks：{len(unassigned)}",
        f"- evidence_gap_count：{evidence_gap_count}",
        f"- needs_evidence_count：{needs_evidence_count}",
        f"- live_skipped_count：{live_skipped_count}",
        f"- retro_count：{len(retros)}",
        f"- approved_retro_count：{len(approved_retros)}",
        f"- unresolved_evidence_gap_count：{evidence_gap_count}",
        f"- candidate_improvement_count：{candidate_improvement_count}",
        f"- learning_proposal_count：{learn_counts['learning_proposals']}",
        f"- approved_learning_count：{learn_counts['learning_approved']}",
        f"- deferred_learning_count：{learn_counts['learning_deferred']}",
        f"- not_applied_learning_count：{learn_counts['learning_not_applied']}",
        f"- playbook_entry_count: {apply_view['playbook_entries']}",
        f"- applied_learning_count: {apply_view['applied_to_workbench_playbook']}",
        f"- pending_apply_count: {apply_view['planned_count']}",
        f"- context_pack_count: {context_view['context_pack_count']}",
        f"- projects_with_context: {context_view['projects_with_context']}",
        f"- projects_missing_context: {context_view['projects_missing_context']}",
        "",
        "项目：",
    ]
    if not projects:
        lines.append("- 暂无项目。")
    for project in projects[:15]:
        project_tasks = project_task_records(project["project_id"])
        open_count = sum(1 for task in project_tasks if task.get("status") in OPEN_TASK_STATUSES)
        needs_count = sum(1 for task in project_tasks if task.get("status") == "needs_evidence")
        project_gap_count = sum(1 for task in project_tasks if task_analysis_cache.get(task["task_id"], {}).get("has_gaps"))
        project_live_count = sum(1 for task in project_tasks if task_analysis_cache.get(task["task_id"], {}).get("live_skipped"))
        project_retro_count = sum(1 for record in retros if record.get("project_id") == project["project_id"])
        project_learning = learning_project_counts(project["project_id"])
        project_apply = project_apply_counts(project["project_id"])
        lines.append(
            f"- {project['project_id']} | {project['status']} | {project['priority'] or 'P?'} | open={open_count} | needs_evidence={needs_count} | evidence_gaps={project_gap_count} | live_skipped={project_live_count} | retro_count={project_retro_count} | learning_proposal_count={project_learning['proposal_count']} | approved_learning_count={project_learning['approved_count']} | deferred_learning_count={project_learning['deferred_count']} | not_applied_learning_count={project_learning['not_applied_count']} | playbook_entry_count={project_apply['playbook_entry_count']} | applied_learning_count={project_apply['applied_learning_count']} | pending_apply_count={project_apply['pending_apply_count']} | {project['title']}"
        )
        if project.get("priority") in {"P0", "P1"} and project_gap_count:
            lines.append(f"  priority_hint: P0/P1 项目存在证据缺口，优先补 {project['project_id']}。")
    if unassigned:
        lines.append("")
        lines.append("未归属任务：")
        for task in unassigned[:10]:
            lines.append(f"- {task['task_id']} | {task['status']} | {task['title']}")
    candidates_for_retro = [
        task for task in tasks
        if task.get("status") in {"passed", "archived", "needs_evidence", "blocked"} and not retro_exists(task["task_id"])
    ]
    lines.append("")
    lines.append("今日建议：")
    if candidates_for_retro:
        lines.append(f"- 优先复盘任务：{candidates_for_retro[0]['task_id']} | {candidates_for_retro[0]['title']}")
    else:
        lines.append("- 暂无必须复盘任务。")
    return "\n".join(lines)


_build_project_brief_reply_base = build_project_brief_reply


def build_project_brief_reply(project_id: str) -> str:
    clean_project_id = validate_project_id(project_id)
    base = _build_project_brief_reply_base(clean_project_id)
    records = dispatches_for_project(clean_project_id)
    project_collections = collections_for_project(clean_project_id)
    latest_collection_id = project_collections[0]["collection_id"] if project_collections else "none"
    smoke_collection_count = sum(1 for record in project_collections if record.get("kind") == "smoke")
    failed_collection_count = sum(1 for record in project_collections if record.get("smoke_failed"))
    ready = [record for record in records if record.get("status") == "ready"]
    sent = [record for record in records if record.get("status") == "sent"]
    returned = [record for record in records if record.get("status") == "returned"]
    failed = [record for record in records if record.get("status") == "failed"]
    lines = [
        "",
        "Dispatch view:",
        f"- ready: {len(ready)}",
        f"- sent_not_returned: {len(sent)}",
        f"- returned_pending_qa: {len(returned)}",
        f"- failed: {len(failed)}",
        "- suggested_next_actions:",
    ]
    if ready:
        lines.append(f"- /dispatch package {ready[0]['dispatch_id']}")
    if sent:
        lines.append(f"- /dispatch receive {sent[0]['dispatch_id']} when report is back")
    if returned:
        lines.append(f"- /dispatch qa {returned[0]['dispatch_id']}")
    if failed:
        lines.append(f"- inspect or recreate dispatch: {failed[0]['dispatch_id']}")
    if not records:
        lines.append("- create dispatch for active task: /dispatch create <task_id> codex --with-context")
    lines.extend(
        [
            "",
            "Collection view:",
            f"- latest_collection_id: {latest_collection_id}",
            f"- collection_count: {len(project_collections)}",
            f"- smoke_collection_count: {smoke_collection_count}",
            f"- failed_collection_count: {failed_collection_count}",
        ]
    )
    return base + "\n" + "\n".join(lines)


_build_project_dashboard_reply_base = build_project_dashboard_reply


def build_project_dashboard_reply() -> str:
    base = _build_project_dashboard_reply_base()
    counts = dispatch_counts()
    lines = [
        "",
        "Dispatch summary:",
        f"- dispatch_ready_count: {counts['dispatch_ready_count']}",
        f"- dispatch_sent_count: {counts['dispatch_sent_count']}",
        f"- dispatch_returned_count: {counts['dispatch_returned_count']}",
        f"- dispatch_failed_count: {counts['dispatch_failed_count']}",
        f"- dispatch_stale_count: {counts['dispatch_stale_count']}",
        "- external_execution_enabled: false",
    ]
    return base + "\n" + "\n".join(lines)


def handle_project_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/project":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_project_help_reply()
        if subcommand == "new":
            return build_project_new_reply(tail)
        if subcommand == "list":
            return build_project_list_reply()
        if subcommand == "show":
            return build_project_show_reply(tail)
        if subcommand == "set":
            return build_project_set_reply(tail)
        if subcommand == "note":
            inline_tail = tail
            if len(lines) > 1:
                inline_tail = f"{tail} {' '.join(line.strip() for line in lines[1:] if line.strip())}"
            return build_project_note_reply(inline_tail)
        if subcommand == "attach":
            return build_project_attach_reply(tail)
        if subcommand == "tasks":
            return build_project_tasks_reply(tail)
        if subcommand == "brief":
            return build_project_brief_reply(tail)
        if subcommand == "dashboard":
            return build_project_dashboard_reply()
        return build_project_help_reply()
    except FileNotFoundError as exc:
        return f"项目或任务不存在：{safe_preview(str(exc), 180)}"
    except FileExistsError as exc:
        return f"项目已存在：{safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"项目索引操作被拒绝：{safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"项目索引操作失败：{safe_preview(str(exc), 180)}"


def build_daily_brief_reply() -> str:
    ensure_workbench_dirs()
    today = datetime.now().strftime("%Y-%m-%d")
    records = [
        record for record in task_records()
        if record.get("status") in OPEN_TASK_STATUSES and (record.get("created_at", "").startswith(today) or record.get("updated_at", "").startswith(today))
    ]
    lines = [f"Atlas 今日简报：{today}"]
    if not records:
        lines.append("- 今日暂无 open/reported/reviewed/needs_evidence 任务。")
    else:
        grouped: dict[str, list[dict]] = {}
        for record in records[:50]:
            grouped.setdefault(record.get("project_id") or "unassigned", []).append(record)
        for project_id in sorted(grouped, key=lambda item: (item == "unassigned", item)):
            if project_id == "unassigned":
                label = "unassigned"
            else:
                try:
                    label = f"{project_id} {project_title_from_text(project_id, read_project(project_id))}"
                except Exception:
                    label = project_id
            lines.append(f"\n项目：{label}")
            for record in grouped[project_id][:20]:
                lines.append(f"- {record['task_id']} | {record['status']} | {record['title']}")
    lines.extend(
        [
            "",
            "今日决策建议：",
            "- reported：优先 /task review。",
            "- reviewed：等待用户 /task decide。",
            "- needs_evidence：补证据后再报告。",
        ]
    )
    brief = "\n".join(lines)
    (DAILY_DIR / f"{datetime.now().strftime('%Y%m%d')}.md").write_text(sanitize_sensitive_text(brief), encoding="utf-8")
    return brief


def handle_task_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "/task":
        return None
    subcommand = parts[1].lower()
    tail = parts[2] if len(parts) > 2 else ""
    try:
        if subcommand == "help":
            return build_task_help_reply()
        if subcommand == "new":
            return build_task_new_reply(tail)
        if subcommand == "list":
            return build_task_list_reply()
        if subcommand == "show":
            return build_task_show_reply(tail)
        if subcommand == "handoff":
            handoff_parts = tail.split(maxsplit=1)
            if len(handoff_parts) != 2:
                return "用法：/task handoff <task_id> codex|kiro [--with-context]"
            return build_task_handoff_reply(handoff_parts[0], handoff_parts[1])
        if subcommand == "report":
            report_parts = tail.split(maxsplit=1)
            if not report_parts:
                return "用法：/task report <task_id>\\n<粘贴 Codex/Kiro 回传报告>"
            task_id = report_parts[0]
            inline = report_parts[1] if len(report_parts) > 1 else ""
            body = "\n".join(lines[1:]).strip()
            report = body or inline
            return build_task_report_reply(task_id, report)
        if subcommand == "qa":
            return build_task_qa_reply(tail)
        if subcommand == "review":
            return build_task_review_reply(tail)
        if subcommand == "next":
            return build_task_next_reply(tail)
        if subcommand == "accept-evidence":
            accept_parts = tail.split(maxsplit=1)
            if not accept_parts:
                return "Usage: /task accept-evidence <task_id> <reason>"
            note = accept_parts[1] if len(accept_parts) > 1 else "\n".join(lines[1:]).strip()
            if not note.strip():
                return "Usage: /task accept-evidence <task_id> <reason>"
            return build_task_accept_evidence_reply(accept_parts[0], note)
        if subcommand == "decide":
            decision_parts = tail.split(maxsplit=2)
            if len(decision_parts) < 2:
                return "用法：/task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>"
            note = decision_parts[2] if len(decision_parts) > 2 else ""
            return build_task_decide_reply(decision_parts[0], decision_parts[1], note)
        if subcommand == "close":
            return build_task_close_reply(tail)
        return build_task_help_reply()
    except FileNotFoundError as exc:
        return f"任务不存在：{safe_preview(str(exc), 180)}"
    except Exception as exc:
        return f"任务账本操作失败：{safe_preview(str(exc), 180)}"


def handle_daily_command(user_text: str) -> str | None:
    parts = user_text.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].lower() == "/daily" and parts[1].strip().lower() == "brief":
        try:
            return build_daily_brief_reply()
        except Exception as exc:
            return f"今日简报生成失败：{safe_preview(str(exc), 180)}"
    return None


def read_template(name: str) -> str:
    path = TEMPLATES_DIR / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return f"模板缺失：templates/{name}"


def template_reply(kind: str) -> str:
    aliases = {
        "wo": "work_order.md",
        "work_order": "work_order.md",
        "report": "codex_return_report.md",
        "codex": "codex_return_report.md",
        "kiro": "codex_return_report.md",
        "review": "review_checklist.md",
        "checklist": "review_checklist.md",
        "daily": "daily_brief.md",
        "brief": "daily_brief.md",
    }
    filename = aliases.get(kind.strip().lower())
    if not filename:
        return """可用模板
- /template wo：工作单模板
- /template report：Codex/Kiro 回传模板
- /template review：审查清单模板"""
    return read_template(filename)


def build_workflow_reply() -> str:
    return """Atlas 工作流闭环

1. 用户发目标
- 说清楚想达成什么、当前上下文、限制条件和验收方式。

2. Atlas 生成工作单
- 把自然语言目标整理成目标、范围、执行边界、验收标准、风险点和回传证据。
- Atlas/Bridge 只做咨询和调度，不运行命令、不修改文件。

3. Codex/Kiro 回传报告
- 回传修改文件、执行命令、测试结果、证据、未解决风险。

4. Atlas 审查结果
- 必须区分已验证、未验证、风险、下一步。
- 没有证据时不能说完成。

5. 用户最终确认
- 用户决定通过、补证据、继续下一步、回滚或暂停。"""


def build_work_order_reply(user_text: str) -> str:
    goal = safe_preview(user_text) or "根据用户最新请求完成任务拆解"
    return f"""咨询模式工作单

目标：
- 根据用户请求推进：{goal}

范围：
- 仅生成可交给执行端的任务说明和检查清单。
- 只覆盖用户本次描述的目标，不扩展到未授权系统或外部服务。

执行边界：
- Bridge 不运行命令、不修改文件、不删除文件、不提交代码、不发布内容。
- Bridge 不自动调用任何执行端；需要用户确认后再由执行端处理。
- 没有可验证输出或用户提供的证据前，不声称任务已经完成。

验收标准：
- 执行端给出实际修改文件、命令输出或检查结果。
- 结果能对应用户目标，并明确哪些内容已完成、哪些仍未验证。
- 如涉及代码或文件，执行端需要说明验证命令和结果。

风险点：
- 用户请求可能包含隐含范围，需要执行端先核对工作目录和边界。
- 执行命令、写文件、提交或发布都有副作用，需要确认后再做。
- 证据不足时只能给出建议或计划，不能报告完成。

回传证据：
- 修改文件：
- 执行命令：
- 测试结果：
- 关键日志或截图：
- 未解决风险：
- 下一步建议："""


def strip_intent_prefix(user_text: str, prefixes: list[str]) -> str:
    text = user_text.strip()
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix.lower()):
            return text[len(prefix):].lstrip(" ：:，,")
    return text


def is_work_order_request(user_text: str) -> bool:
    text = user_text.strip().lower()
    prefixes = [
        "生成工作单",
        "创建工作单",
        "整理成工作单",
        "把这个报错整理成 kiro/codex 可执行任务",
        "把这个报错整理成 codex/kiro 可执行任务",
        "把这个报错整理成 codex 可执行任务",
        "把这个报错整理成 kiro 可执行任务",
    ]
    return any(text.startswith(prefix.lower()) for prefix in prefixes)


def is_review_request(user_text: str) -> bool:
    text = user_text.strip().lower()
    prefixes = [
        "审查这份 codex 返回报告",
        "审查这份 kiro 返回报告",
        "审查这份返回报告",
        "根据以下证据判断是否完成",
        "判断以下证据是否完成",
    ]
    return any(text.startswith(prefix.lower()) for prefix in prefixes)


def is_priority_request(user_text: str) -> bool:
    text = user_text.strip().lower()
    return text.startswith("判断下一步优先级") or text.startswith("判断下一步")


def build_review_reply(user_text: str) -> str:
    evidence = strip_intent_prefix(
        user_text,
        [
            "审查这份 Codex 返回报告",
            "审查这份 Kiro 返回报告",
            "审查这份返回报告",
            "根据以下证据判断是否完成",
            "判断以下证据是否完成",
        ],
    )
    evidence_preview = safe_preview(evidence, 260) or "未提供可审查证据"
    has_evidence_markers = any(
        marker in evidence
        for marker in ("修改文件", "执行命令", "测试结果", "通过", "日志", "截图", "路径", "输出")
    )
    decision = "待补证据" if not has_evidence_markers else "需要逐项核对后再确认"
    return f"""Atlas 审查结论：{decision}

已验证：
- 当前输入中可见证据摘要：{evidence_preview if has_evidence_markers else "未看到足够的文件、命令、测试或日志证据。"}

未验证：
- 是否完全满足原工作单验收标准仍需逐条对照。
- 若缺少修改文件、命令输出、测试结果或日志位置，不能判定完成。

风险：
- 只有结论但没有证据时，可能误判为完成。
- 若执行范围、回滚方式或敏感信息处理未说明，需要补充。

下一步决策：
- {decision}。
- 请执行端补充：修改文件、执行命令、测试结果、关键日志或截图、未解决风险。
- 用户确认前，不进入下一阶段。"""


def build_priority_reply(user_text: str) -> str:
    target = strip_intent_prefix(user_text, ["判断下一步优先级", "判断下一步"])
    target = safe_preview(target, 240) or "未提供候选事项"
    return f"""Atlas 下一步决策

当前事项：
- {target}

优先级判断：
- 先处理能解除阻塞、能补齐验收证据、或能降低安全/回滚风险的事项。
- 暂缓没有证据、边界不清、或需要用户确认的执行动作。

下一步：
- 若目标尚未形成工作单：先生成工作单。
- 若已有执行报告：先审查已验证、未验证、风险和下一步。
- 若证据不足：要求补证据，不声称完成。"""


def build_work_order_from_intent(user_text: str) -> str:
    target = strip_intent_prefix(
        user_text,
        [
            "生成工作单",
            "创建工作单",
            "整理成工作单",
            "把这个报错整理成 Kiro/Codex 可执行任务",
            "把这个报错整理成 Codex/Kiro 可执行任务",
            "把这个报错整理成 Codex 可执行任务",
            "把这个报错整理成 Kiro 可执行任务",
        ],
    )
    return build_work_order_reply(target)



def format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def build_status_reply(context: dict) -> str:
    state = context.get("state") or {}
    processed = state.get(PROCESSED_STATE_KEY) or []
    runtime_info = context.get("runtime_info") or {}
    heartbeat = context.get("heartbeat") or {}
    counts = workbench_counts()
    registered = "已注册" if context.get("registered") else "未注册"
    last_error = state.get("last_error") or heartbeat.get("last_error")
    lines = [
        "Octo-Hermes Bridge 状态",
        "- 模式：咨询/调度",
        f"- 注册：{registered}",
        f"- run_id：{runtime_info.get('run_id') or heartbeat.get('run_id') or 'unknown'}",
        f"- pid：{runtime_info.get('pid') or heartbeat.get('pid') or os.getpid()}",
        f"- 启动方式：{runtime_info.get('startup_method') or heartbeat.get('startup_method') or startup_method()}",
        f"- 运行时长：{format_uptime(time.time() - STARTED_AT)}",
        f"- heartbeat 更新时间：{heartbeat.get('updated_at') or 'not_written'}",
        f"- 单实例锁：{runtime_info.get('lock_status') or heartbeat.get('lock_status') or 'unknown'}",
        f"- last_seq：{context.get('last_seq', 0)}",
        f"- 去重缓存：{len(processed)} 条",
        f"- open_tasks：{counts['open_tasks']}",
        f"- reported_tasks：{counts['reported_tasks']}",
        f"- needs_evidence_tasks：{counts['needs_evidence_tasks']}",
        f"- active_projects：{counts['active_projects']}",
        f"- paused_projects：{counts['paused_projects']}",
        f"- archived_projects：{counts['archived_projects']}",
        f"- learning_proposals：{counts['learning_proposals']}",
        f"- learning_approved：{counts['learning_approved']}",
        f"- learning_not_applied：{counts['learning_not_applied']}",
        f"- apply_plans: {counts['apply_plans']}",
        f"- playbook_entries: {counts['playbook_entries']}",
        f"- applied_to_workbench_playbook: {counts['applied_to_workbench_playbook']}",
        f"- context_pack_count: {counts['context_pack_count']}",
        f"- latest_context_id: {counts['latest_context_id'] or 'none'}",
        f"- dispatch_count: {counts['dispatch_count']}",
        f"- dispatch_sent: {counts['dispatch_sent']}",
        f"- dispatch_returned: {counts['dispatch_returned']}",
        f"- dispatch_needs_evidence: {counts['dispatch_needs_evidence']}",
        f"- dispatch_ready_count: {counts['dispatch_ready_count']}",
        f"- dispatch_failed_count: {counts['dispatch_failed_count']}",
        f"- dispatch_stale_count: {counts['dispatch_stale_count']}",
        f"- execution_count: {counts['execution_count']}",
        f"- execution_returned: {counts['execution_returned']}",
        f"- execution_prepared_count: {counts['execution_prepared_count']}",
        f"- execution_started_count: {counts['execution_started_count']}",
        f"- execution_opened_count: {counts['execution_opened_count']}",
        f"- execution_copied_count: {counts['execution_copied_count']}",
        f"- execution_needs_manual_start_count: {counts['execution_needs_manual_start_count']}",
        f"- execution_failed_count: {counts['execution_failed_count']}",
        f"- execution_stale_count: {counts['execution_stale_count']}",
        f"- latest_exec_id: {counts['latest_exec_id'] or 'none'}",
        f"- collection_count: {counts['collection_count']}",
        f"- latest_collection_id: {counts['latest_collection_id'] or 'none'}",
        f"- smoke_collection_count: {counts['smoke_collection_count']}",
        f"- failed_collection_count: {counts['failed_collection_count']}",
        f"- collect_enabled: {str(COLLECT_ENABLED).lower()}",
        f"- collect_mode: {COLLECT_MODE}",
        f"- arbitrary_command_enabled: {str(ARBITRARY_COMMAND_ENABLED).lower()}",
        "- learning_dir：workbench/learning/",
        f"- application_enabled：{str(APPLICATION_ENABLED).lower()}",
        f"- runtime_injection_enabled: {str(RUNTIME_INJECTION_ENABLED).lower()}",
        f"- external_application_enabled: {str(EXTERNAL_APPLICATION_ENABLED).lower()}",
        f"- external_execution_enabled: {str(EXTERNAL_EXECUTION_ENABLED).lower()}",
        f"- read_only_auto_exec_enabled: {str(READ_ONLY_AUTO_EXEC_ENABLED).lower()}",
        f"- human_confirm_required: {str(HUMAN_CONFIRM_REQUIRED).lower()}",
        f"- auto_execute_enabled: {str(AUTO_EXECUTE_ENABLED).lower()}",
        f"- 最近 task_id：{counts['recent_task_id'] or 'none'}",
        f"- 日志：logs/bridge.log",
        "- heartbeat：runtime/heartbeat.json",
        "- workbench：workbench/",
        "- 安全边界：Bridge 只生成咨询回复或工作单，不执行命令、不修改文件。",
    ]
    if context.get("robot_id"):
        lines.append(f"- robot_id：{short_id(context.get('robot_id'))}")
    if context.get("owner_channel_id"):
        lines.append(f"- owner_channel_id：{short_id(context.get('owner_channel_id'))}")
    if last_error:
        lines.append(f"- 最近错误：{safe_preview(str(last_error), 120)}")
    else:
        lines.append("- 最近错误：无")
    return "\n".join(lines)


def build_help_reply() -> str:
    return """Octo-Hermes Bridge 使用说明
- /status：查看 bridge 注册、last_seq、去重缓存、日志路径、heartbeat 和安全边界。
- /help：查看本说明。
- /workflow：查看 Atlas 工作流闭环说明。
- /project help：查看项目索引和跨任务看板命令。
- /task help：查看本地任务账本命令。
- /evidence help：查看证据链命令。
- /retro help：查看任务复盘命令。
- /learn help：查看受控学习循环命令。
- /apply help: view Workbench-only apply commands.
- /playbook help: view local Playbook reference commands.
- /context help: view Context Pack commands.
- /daily brief：汇总今日待处理任务。
- /template wo：返回工作单模板。
- /template report：返回 Codex/Kiro 回传模板。
- /template review：返回审查清单模板。
- 普通消息：进入 Atlas 咨询/调度模式，用于理解目标、拆解任务、审查方案。
- 推荐用法：生成工作单：检查 Kiro 反代当前状态
- 推荐用法：审查这份 Codex 返回报告：<粘贴报告>
- 推荐用法：判断下一步优先级：<粘贴候选事项>
- 推荐用法：把这个报错整理成 Kiro/Codex 可执行任务：<粘贴报错>
- 推荐用法：根据以下证据判断是否完成：<粘贴证据>
- 执行类请求：只生成工作单和验收标准，不运行命令、不修改文件、不提交或发布。"""


def build_help_reply() -> str:
    return """Octo-Hermes Bridge help
- /status: bridge status, heartbeat, workbench counts, and safety boundary.
- /help: show this help.
- /workflow: Atlas workflow overview.
- /project help: project index and cross-task dashboard.
- /task help: local task ledger commands.
- /dispatch help: manual dispatch queue and execution session ledger.
- /exec help: semi-auto execution session ledger; human confirmation required.
- /run help: one-command task -> dispatch -> exec start helper.
- /collect help: read-only whitelist evidence collection.
- /pilot help: real-project pilot metrics and operating notes.
- /context help: Context Pack commands.
- /playbook help: local Playbook reference commands.
- /evidence help: evidence chain commands.
- /retro help: task retro commands.
- /learn help: controlled learning loop commands.
- /apply help: Workbench-only apply commands.
- /daily brief: summarize today's active tasks.
- /template wo/report/review: workflow templates.
- 推荐用法：生成工作单：检查 Kiro 反代当前状态
- 推荐用法：审查这份 Codex 返回报告：<粘贴报告>
- 推荐用法：判断下一步优先级：<粘贴候选事项>
- 推荐用法：把这个报错整理成 Kiro/Codex 可执行任务：<粘贴报错>
- 推荐用法：根据以下证据判断是否完成：<粘贴证据>
- æŽ¨èç”¨æ³•ï¼šç”Ÿæˆå·¥ä½œå•ï¼šæ£€æŸ¥ Kiro åä»£å½“å‰çŠ¶æ€
- æŽ¨èç”¨æ³•ï¼šå®¡æŸ¥è¿™ä»½ Codex è¿”å›žæŠ¥å‘Šï¼š<ç²˜è´´æŠ¥å‘Š>
- æŽ¨èç”¨æ³•ï¼šåˆ¤æ–­ä¸‹ä¸€æ­¥ä¼˜å…ˆçº§ï¼š<ç²˜è´´å€™é€‰äº‹é¡¹>
- æŽ¨èç”¨æ³•ï¼šæŠŠè¿™ä¸ªæŠ¥é”™æ•´ç†æˆ Kiro/Codex å¯æ‰§è¡Œä»»åŠ¡ï¼š<ç²˜è´´æŠ¥é”™>
- æŽ¨èç”¨æ³•ï¼šæ ¹æ®ä»¥ä¸‹è¯æ®åˆ¤æ–­æ˜¯å¦å®Œæˆï¼š<ç²˜è´´è¯æ®>
- Recommended: /dispatch create <task_id> codex --with-context
- Recommended: /dispatch package <dispatch_id>
- Recommended: /dispatch receive <dispatch_id> then paste report.
- Recommended: /dispatch qa <dispatch_id>, /task review <task_id>, /task decide ...
- Recommended: /pilot start <project_id> <title>, then /pilot metrics <pilot_id>.
- Ordinary execution requests only create work orders. /run uses the guarded execution ledger flow; Bridge does not modify project files, run arbitrary commands, or claim completion without evidence."""


def build_template_help_reply() -> str:
    return """模板命令
- /template wo：工作单模板
- /template report：Codex/Kiro 回传模板
- /template review：审查清单模板"""



def build_auto_pipeline_closure_reply(run_reply: str, source_command: str = "/auto") -> str:
    """Best-effort auto closure for /auto runs. Does not execute shell commands."""
    reply_text = sanitize_sensitive_text(str(run_reply or ""))
    task_id = reply_field(reply_text, "task_id")
    dispatch_id = reply_field(reply_text, "dispatch_id")
    exec_id = reply_field(reply_text, "exec_id")

    def _block(
        status: str,
        reason: str,
        final_task_status: str = "unknown",
        retro_status: str = "not_run",
        sync_task: bool = True,
    ) -> str:
        try:
            analysis = (sync_task_evidence_state(task_id) if sync_task else evidence_analysis(task_id)) if task_id else {}
            gap_risk = str(bool(analysis.get("has_gaps"))).lower() if analysis else "unknown"
            closure_state = evidence_closure_state(analysis) if analysis else "unknown"
        except Exception:
            gap_risk = "unknown"
            closure_state = "unknown"
        no_op_lines = "\n- ledger_write: none\n- duplicate_closure_skipped: true" if status == "already_closed" else ""
        return sanitize_sensitive_text(f"""{reply_text}

Auto Pipeline Closure:
- auto_pipeline_enabled: true
- auto_pipeline_status: {status}
- task_id: {task_id or 'none'}
- dispatch_id: {dispatch_id or 'none'}
- exec_id: {exec_id or 'none'}
- final_task_status: {final_task_status}
- retro_status: {retro_status}
- evidence_gap_risk: {gap_risk}
- evidence_closure_state: {closure_state}
- stop_reason: {safe_preview(reason, 240)}
{no_op_lines}
""")

    def _retro_status_for_task() -> str:
        if not task_id or not retro_exists(task_id):
            return "not_run"
        try:
            return task_metadata(read_retro(task_id)).get("status", "exists")
        except Exception:
            return "exists"

    if not task_id or not dispatch_id or not exec_id or exec_id == "none":
        return _block("needs_human_review", "missing task/dispatch/exec id; cannot auto-close")

    try:
        exec_meta = task_metadata(read_exec(exec_id))
    except Exception as exc:
        return _block("needs_human_review", f"cannot read exec metadata: {exc}")

    try:
        task_text = read_task(task_id)
        current_task_status = task_metadata(task_text).get("status", "unknown")
    except Exception as exc:
        return _block("needs_human_review", f"cannot read task metadata: {exc}")

    exec_auto_closed = str(exec_meta.get("auto_closed", "")).lower() == "true"
    task_has_closure = bool(task_section(task_text, "Closure Evidence").strip())
    task_has_decision = task_has_user_decision(task_text)
    if current_task_status in TERMINAL_TASK_STATUSES and (exec_auto_closed or task_has_closure or task_has_decision):
        indicators = []
        if exec_auto_closed:
            indicators.append("exec_auto_closed")
        if task_has_closure:
            indicators.append("closure_evidence")
        if task_has_decision:
            indicators.append("user_decision")
        return _block(
            "already_closed",
            f"terminal task already has closure marker ({', '.join(indicators)}); no duplicate review/decide/close/retro ledger writes",
            final_task_status=current_task_status,
            retro_status=_retro_status_for_task(),
            sync_task=False,
        )

    owner_write = str(exec_meta.get("owner_write_policy", "")).lower() == "true"
    owner_status = str(exec_meta.get("owner_write_policy_status", "")).lower()

    gates = [
        ("exec_status", exec_meta.get("status") == "returned"),
        ("returncode", str(exec_meta.get("returncode", "")) == "0"),
        ("timed_out", str(exec_meta.get("timed_out", "")).lower() == "false"),
        ("completion_state", exec_meta.get("completion_state") == "completed"),
        ("payload_state", exec_meta.get("payload_state", "payload_seen") in {"payload_seen", ""}),
    ]
    if owner_write:
        gates.extend([
            ("owner_write_policy_status", owner_status == "returned"),
            ("write_target_fidelity", exec_meta.get("write_target_fidelity") == "passed"),
            ("post_run_target_fidelity", exec_meta.get("post_run_target_fidelity") == "passed"),
        ])
    if str(exec_meta.get("review_required_by", "")).strip().lower() == "codex":
        gates.append(("codex_review", exec_meta.get("codex_review_status") == "pass_candidate"))

    failed = [name for name, ok in gates if not ok]
    if failed:
        return _block("needs_human_review", "gate failed: " + ", ".join(failed))

    try:
        accept_reply = build_task_accept_evidence_reply(
            task_id,
            f"auto pipeline accepted eligible observed evidence after {source_command}",
        )
        review_reply = build_task_review_reply(task_id)
        link_reply = build_dispatch_link_review_reply(dispatch_id)
        review_reply = build_task_review_reply(task_id)

        review_ready = (
            "remaining_gaps: none" in review_reply
            and ("?????pass" in review_reply or "recommendation: ready_for_review" in review_reply or "recommendation: pass" in review_reply)
        )
        if not review_ready:
            return _block("needs_human_review", "review still has remaining gaps after evidence acceptance")

        task_status = task_metadata(read_task(task_id)).get("status", "")
        if task_status not in {"passed", "archived"}:
            build_task_decide_reply(task_id, "pass", f"auto pipeline pass after {source_command}")
            task_status = task_metadata(read_task(task_id)).get("status", "")

        if task_status != "archived":
            build_task_close_reply(task_id)
            task_status = task_metadata(read_task(task_id)).get("status", "")

        retro_status = "not_run"
        try:
            build_retro_create_reply(task_id)
            retro_reply = build_retro_approve_reply(task_id, f"auto pipeline retro approved after {source_command}")
            retro_status = "approved" if "approved" in retro_reply.lower() or "?????" in retro_reply else "created"
        except Exception as exc:
            retro_status = "needs_human_review"
            return _block("needs_human_review", f"retro closure failed: {exc}", final_task_status=task_status, retro_status=retro_status)

        return _block("pass", "all gates passed", final_task_status=task_status, retro_status=retro_status)
    except Exception as exc:
        try:
            task_status = task_metadata(read_task(task_id)).get("status", "unknown")
        except Exception:
            task_status = "unknown"
        return _block("needs_human_review", f"auto closure failed: {exc}", final_task_status=task_status)



def handle_run_command(user_text: str) -> str | None:
    lines = user_text.strip().splitlines()
    first_line = lines[0] if lines else ""
    parts = first_line.split(maxsplit=2)
    if not parts or parts[0].lower() != "/run":
        return None
    subcommand = parts[1].lower() if len(parts) > 1 else "help"
    tail = parts[2] if len(parts) > 2 else ""
    body = "\n".join(lines[1:]).strip()
    if body:
        tail = f"{tail}\n{body}".strip()
    try:
        if subcommand == "help":
            return build_run_help_reply()
        if subcommand == "codex":
            return build_run_codex_reply(tail)
        if subcommand in {"codex-write", "codex-owner-write"}:
            return build_run_codex_reply(tail, owner_write_policy=True)
        if subcommand in {"claude-write", "claude-owner-write"}:
            # Claude writes under owner write policy; Codex reviews read-only
            # before any auto close. Plain "/run claude" stays read-only via
            # the standard exec start path.
            return build_run_codex_reply(tail, owner_write_policy=True, target="claude")
        if subcommand in {"auto-write", "auto-owner-write"}:
            # Default routing policy: Claude writes code, Codex reviews.
            # Explicit /run codex-write and /run claude-write stay unchanged.
            resolved_target, routing_reason = route_auto_executor(tail, owner_write=True)
            return build_run_codex_reply(
                tail,
                owner_write_policy=True,
                target=resolved_target,
                requested_executor="auto",
                routing_reason=routing_reason,
            )
        return build_run_help_reply()
    except FileNotFoundError as exc:
        return f"run source not found: {safe_preview(str(exc), 180)}"
    except ValueError as exc:
        return f"run operation refused: {safe_preview(str(exc), 220)}"
    except Exception as exc:
        return f"run operation failed: {safe_preview(str(exc), 180)}"


def handle_local_command(user_text: str, context: dict) -> str | None:
    parts = user_text.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    if command == "/run":
        return handle_run_command(user_text)
    if command == "/apply":
        return handle_apply_command(user_text)
    if command == "/playbook":
        return handle_playbook_command(user_text)
    if command == "/context":
        return handle_context_command(user_text)
    if command == "/dispatch":
        return handle_dispatch_command(user_text)
    if command == "/exec":
        return handle_exec_command(user_text)
    if command == "/pilot":
        return handle_pilot_command(user_text)
    if command == "/collect":
        return handle_collect_command(user_text)
    if command == "/learn":
        return handle_learn_command(user_text)
    if command == "/retro":
        return handle_retro_command(user_text)
    if command == "/evidence":
        return handle_evidence_command(user_text)
    if command == "/project":
        return handle_project_command(user_text)
    if command == "/task":
        return handle_task_command(user_text)
    if command == "/daily":
        return handle_daily_command(user_text)
    if command == "/status":
        return build_status_reply(context)
    if command == "/help":
        return build_help_reply()
    if command == "/workflow":
        return build_workflow_reply()
    if command == "/template":
        if len(parts) == 1:
            return build_template_help_reply()
        return template_reply(parts[1])
    return None


def prepare_reply(user_text: str, context: dict) -> tuple[str, str]:
    # OHB-AUTO-033A minimal /auto alias.
    # Keep this intentionally thin: /auto delegates to the already sealed /run paths.
    auto_text = user_text.strip()
    auto_lower = auto_text.lower()

    def _auto_tail(prefix: str):
        if auto_lower == prefix:
            return ""
        if auto_lower.startswith(prefix + " "):
            return auto_text[len(prefix):].strip()
        return None

    auto_codex_write_tail = _auto_tail("/auto codex-write")
    if auto_codex_write_tail is not None:
        if not auto_codex_write_tail:
            return "Usage: /auto codex-write <task title> [--project project_id]", "auto_help"
        run_reply, run_route = prepare_reply(f"/run codex-write {auto_codex_write_tail}", context)
        return build_auto_pipeline_closure_reply(run_reply, "/auto codex-write"), run_route

    auto_codex_tail = _auto_tail("/auto codex")
    if auto_codex_tail is not None:
        if not auto_codex_tail:
            return "Usage: /auto codex <task title> [--project project_id]", "auto_help"
        run_reply, run_route = prepare_reply(f"/run codex {auto_codex_tail}", context)
        return build_auto_pipeline_closure_reply(run_reply, "/auto codex"), run_route

    if auto_lower == "/auto" or auto_lower.startswith("/auto "):
        return (
            "Auto command help\n"
            "- /auto codex <task title> [--project project_id]\n"
            "- /auto codex-write <task title> [--project project_id]\n"
            "Current behavior: /auto delegates to the sealed /run pipeline.",
            "auto_help",
        )

    local_reply = handle_local_command(user_text, context)
    if local_reply is not None:
        return local_reply, "local_command"
    if is_work_order_request(user_text):
        return build_work_order_from_intent(user_text), "work_order"
    if is_review_request(user_text):
        return build_review_reply(user_text), "review"
    if is_priority_request(user_text):
        return build_priority_reply(user_text), "decision"
    if is_execution_request(user_text):
        return build_work_order_reply(user_text), "work_order"
    return call_hermes(user_text), "hermes"


def send_text(headers: dict, channel_id: str, channel_type: int, content: str, client_msg_no: str) -> None:
    body = {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "payload": {
            "type": 1,
            "content": content,
        },
        "client_msg_no": client_msg_no,
    }
    post_json("/v1/bot/sendMessage", body, headers)


def main() -> int:
    setup_logging()
    guard: SingleInstanceGuard | None = None
    state = {PROCESSED_STATE_KEY: []}
    last_seq = 0
    robot_id = ""
    owner_channel_id = ""
    registered = False
    heartbeat: dict = {}
    runtime_info: dict = {}

    try:
        try:
            guard = acquire_single_instance()
        except AlreadyRunningError as exc:
            existing_pid = exc.lock_info.get("pid", "unknown")
            log_event("already_running", pid=existing_pid, lock=LOCK_FILE.relative_to(ROOT))
            print(f"[bridge] {exc}")
            return 2

        clear_stop_request()
        ensure_workbench_dirs()

        runtime_info = {
            "run_id": guard.run_id,
            "pid": guard.pid,
            "started_at": STARTED_AT_TEXT,
            "startup_method": startup_method(),
            "lock_status": lock_status(guard),
        }

        log_event(
            "startup",
            cwd=ROOT,
            log_file=LOG_FILE.relative_to(ROOT),
            run_id=guard.run_id,
            pid=guard.pid,
            startup_method=runtime_info["startup_method"],
            mode="consultation",
        )

        state = load_state()
        last_seq = int(state.get("last_seq", 0))
        state["last_seq"] = last_seq
        heartbeat = write_heartbeat(runtime_info, state, last_seq, registered)

        try:
            load_env()
        except BaseException as exc:
            state["last_error"] = f"startup {type(exc).__name__}: {redact(exc)}"
            save_state(state)
            write_heartbeat(runtime_info, state, last_seq, registered)
            log_error("startup_failed", exc)
            raise

        token = os.environ.get("OCTO_BOT_TOKEN", "").strip()
        if not token.startswith("bf_"):
            state["last_error"] = "startup: OCTO_BOT_TOKEN missing or invalid"
            save_state(state)
            write_heartbeat(runtime_info, state, last_seq, registered)
            log_error("startup_failed", reason="OCTO_BOT_TOKEN missing or invalid")
            raise SystemExit("OCTO_BOT_TOKEN missing or invalid. It should start with bf_.")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        reg = post_json(
            "/v1/bot/register",
            {
                "agent_platform": "hermes-bridge",
                "agent_version": "local",
                "plugin_version": "mvp-0.1",
            },
            headers,
        )

        robot_id = str(reg["robot_id"])
        owner_channel_id = str(reg["owner_channel_id"])
        registered = True
        poll_seconds = float(os.environ.get("POLL_SECONDS", "2"))
        limit = processed_limit()
        processed_set = set(state.get(PROCESSED_STATE_KEY, []))
        heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)

        log_event(
            "registered",
            robot_id=short_id(robot_id),
            owner_channel_id=short_id(owner_channel_id),
            last_seq=last_seq,
        )

        if last_seq == 0:
            log_event("baseline_start", limit=50)
            sync = post_json(
                "/v1/bot/messages/sync",
                {
                    "channel_id": owner_channel_id,
                    "channel_type": 1,
                    "limit": 50,
                    "start_message_seq": 0,
                    "end_message_seq": 0,
                    "pull_mode": 1,
                },
                headers,
            )
            messages = sync.get("messages") or []
            if messages:
                last_seq = max(int(m.get("message_seq") or 0) for m in messages)
                state["last_seq"] = last_seq
                for msg in messages:
                    remember_processed(state, message_key(msg), limit)
                save_state(state)
                processed_set = set(state.get(PROCESSED_STATE_KEY, []))
            heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
            log_event("baseline_set", last_seq=last_seq, seen=len(messages))
            log_event("ready", poll_seconds=poll_seconds)

        while True:
            try:
                if stop_requested():
                    clear_stop_request()
                    log_event("stop_requested")
                    return 0

                sync = post_json(
                    "/v1/bot/messages/sync",
                    {
                        "channel_id": owner_channel_id,
                        "channel_type": 1,
                        "limit": 50,
                        "start_message_seq": 0,
                        "end_message_seq": 0,
                        "pull_mode": 1,
                    },
                    headers,
                )

                messages = sync.get("messages") or []
                messages = sorted(messages, key=lambda m: int(m.get("message_seq") or 0))

                for msg in messages:
                    seq = int(msg.get("message_seq") or 0)
                    key = message_key(msg)
                    if seq <= last_seq or key in processed_set:
                        continue

                    last_seq = seq
                    state["last_seq"] = last_seq
                    remember_processed(state, key, limit)
                    save_state(state)
                    processed_set = set(state.get(PROCESSED_STATE_KEY, []))
                    heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)

                    from_uid = str(msg.get("from_uid") or "")
                    channel_type = int(msg.get("channel_type") or 1)

                    if from_uid == robot_id:
                        log_event("inbound_skip_self", seq=seq)
                        continue

                    user_text = decode_content(msg.get("payload"))
                    if not user_text:
                        log_event("inbound_empty", seq=seq, from_uid=short_id(from_uid))
                        continue

                    log_event("inbound", seq=seq, from_uid=short_id(from_uid), channel_type=channel_type, content_len=len(user_text))
                    if channel_type == 1:
                        reply_channel_id = from_uid
                    else:
                        reply_channel_id = str(msg.get("channel_id") or owner_channel_id)

                    runtime_info["lock_status"] = lock_status(guard)
                    heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
                    context = {
                        "registered": registered,
                        "robot_id": robot_id,
                        "owner_channel_id": owner_channel_id,
                        "last_seq": last_seq,
                        "state": state,
                        "runtime_info": runtime_info,
                        "heartbeat": heartbeat,
                    }

                    try:
                        reply, route = prepare_reply(user_text, context)
                        client_msg_no = stable_client_msg_no(key)
                        send_text(headers, reply_channel_id, channel_type, reply, client_msg_no)
                        log_event(
                            "outbound",
                            seq=seq,
                            route=route,
                            channel_id=short_id(reply_channel_id),
                            channel_type=channel_type,
                            content_len=len(reply),
                            client_msg_no=short_id(client_msg_no),
                        )
                    except Exception as exc:
                        state["last_error"] = f"seq={seq} {type(exc).__name__}: {redact(exc)}"
                        save_state(state)
                        heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
                        log_error("message_failed", exc, seq=seq)

                runtime_info["lock_status"] = lock_status(guard)
                heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
                if stop_requested():
                    clear_stop_request()
                    log_event("stop_requested")
                    return 0
                time.sleep(poll_seconds)

            except KeyboardInterrupt:
                log_event("stopped")
                return 0
            except Exception as exc:
                state["last_error"] = f"loop {type(exc).__name__}: {redact(exc)}"
                save_state(state)
                runtime_info["lock_status"] = lock_status(guard)
                heartbeat = write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
                log_error("loop_error", exc)
                time.sleep(5)
    finally:
        if guard:
            guard.release()
            if runtime_info:
                runtime_info["lock_status"] = lock_status(None)
                try:
                    write_heartbeat(runtime_info, state, last_seq, registered, robot_id, owner_channel_id)
                except Exception:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
