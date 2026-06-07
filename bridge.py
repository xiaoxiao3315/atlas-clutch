from __future__ import annotations

import base64
import atexit
import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
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
DECISIONS_DIR = WORKBENCH_DIR / "decisions"
DAILY_DIR = WORKBENCH_DIR / "daily"
ARCHIVE_DIR = WORKBENCH_DIR / "archive"
PROCESSED_STATE_KEY = "processed_message_keys"
DEFAULT_PROCESSED_LIMIT = 500
STARTED_AT = time.time()
STARTED_AT_TEXT = datetime.now().astimezone().isoformat(timespec="seconds")
OPEN_TASK_STATUSES = {"draft", "open", "reported", "reviewed", "needs_evidence"}
DECISION_STATUSES = {"pass", "needs_evidence", "blocked", "cancelled"}
EVIDENCE_TYPES = {"file", "command", "log", "screenshot", "ui", "live", "smoke", "report", "decision", "other"}
EVIDENCE_MARK_STATUSES = {"verified", "partial", "rejected"}

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
    sanitized = str(text or "")
    sanitized = re.sub(r"bf_[A-Za-z0-9._-]+", "[REDACTED_BF_TOKEN]", sanitized)
    sanitized = re.sub(r"sk-[A-Za-z0-9._-]+", "[REDACTED_SK_KEY]", sanitized)
    sanitized = sanitized.replace("bf_", "[REDACTED_BF_PREFIX]")
    sanitized = sanitized.replace("sk-", "[REDACTED_SK_PREFIX]")
    sanitized = re.sub(r"(?im)^(\s*Authorization\s*:\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*Cookie\s*:\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*password\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*api_key\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*secret\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    return sanitized


def ensure_workbench_dirs() -> None:
    for path in (WORKBENCH_DIR, TASKS_DIR, PROJECTS_DIR, EVIDENCE_DIR, RETROS_DIR, DECISIONS_DIR, DAILY_DIR, ARCHIVE_DIR):
        path.mkdir(exist_ok=True)


def ensure_inside_workbench(path: Path) -> Path:
    resolved = path.resolve()
    base = WORKBENCH_DIR.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"refusing to write outside workbench: {path}")
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
    for line in text.splitlines()[1:20]:
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


def report_has_claim(text: str) -> bool:
    value = str(text or "").lower()
    return any(marker in str(text or "") for marker in ("完成", "通过", "已修复", "验收通过", "OK")) or any(
        marker in value for marker in ("done", "passed", "pass", "completed", "fixed")
    )


def detect_evidence_type_from_report(report: str) -> str:
    value = str(report or "").lower()
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
    if evidence_markers_present(body) or evidence_type in {"file", "command", "log", "screenshot", "ui", "live", "smoke"}:
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
    }


def build_evidence_gaps_text(task_id: str, analysis: dict | None = None) -> str:
    data = analysis or evidence_analysis(task_id)
    claimed_lines = [f"- {item}" for item in data["claimed"]] or ["- 无。"]
    observed_lines = [
        f"- {record['evidence_id']} observed but verified={record.get('verified', 'no')}：{safe_preview(record.get('observed') or record.get('notes'), 140)}"
        for record in data["observed"]
    ] or ["- 无。"]
    missing_lines = [f"- {item}" for item in data["missing"]] or ["- 无。"]
    risk_lines = [f"- {item}" for item in data["risks"]] or ["- 无。"]
    next_step = "补充 evidence 或 mark verified 后再 review。"
    if data["live_skipped"]:
        next_step = "补 Octo UI live 验收证据；当前只能写本地通过，live 待补。"
    elif data["observed"] and not data["verified"]:
        next_step = "人工核对 observed 证据后执行 /evidence mark <task_id> <evidence_id> verified|partial|rejected <说明>。"
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
    return counts


def build_task_help_reply() -> str:
    return """Atlas 任务账本命令
- /task help：查看本说明。
- /task new <标题>：创建任务工作单，只写 workbench，不执行。
- /task new <标题> --project <project_id>：创建任务并归属到项目。
- /task list：列出最近 10 个任务。
- /task show <task_id>：查看任务摘要、状态和下一步。
- /task handoff <task_id> codex|kiro：生成可复制给执行端的标准交接包。
- /task report <task_id>：下一行粘贴 Codex/Kiro 回传报告，状态改为 reported。
- /task qa <task_id>：检查回传报告是否满足最小证据要求，不替代 Atlas 审查。
- /task review <task_id>：读取任务与回传报告，生成 Atlas 审查。
- /task next <task_id>：根据当前状态给出下一步建议。
- /task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>：记录用户决策。
- /task close <task_id>：仅 passed/cancelled 可关闭，状态改为 archived。
- /evidence add/list/show/mark/gaps：维护任务证据链。
- /daily brief：汇总今日 open/reported/reviewed/needs_evidence 任务。"""


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


def build_task_report_reply(task_id: str, report: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_task(normalized_task_id)
    clean_report = sanitize_sensitive_text(report).strip() or "- 空报告：需要补充执行证据。"
    now = iso_now()
    addition = f"### Report at {now}\n{clean_report}"
    text = text.replace("## Execution Report\n- 尚未回传.", "## Execution Report")
    text = append_to_section(text, "Execution Report", addition)
    text = append_to_section(text, "Timeline", f"- {now} report appended; status reported.")
    text = update_task_status(text, "reported")
    write_task(normalized_task_id, text)
    evidence_type = detect_evidence_type_from_report(clean_report)
    evidence_id = create_evidence_entry(
        normalized_task_id,
        evidence_type,
        clean_report,
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
{gap_line}
{live_line}
- 下一步：发送 /task qa {normalized_task_id}，再发送 /task review {normalized_task_id}"""


def evidence_markers_present(text: str) -> bool:
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


def build_task_close_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    text = read_task(normalized_task_id)
    status = task_metadata(text).get("status", "unknown")
    if status not in {"passed", "cancelled"}:
        return f"不能关闭：{normalized_task_id} 当前 status={status}。只有 passed/cancelled 可以关闭。"
    now = iso_now()
    text = append_to_section(text, "Timeline", f"- {now} task archived from status {status}.")
    text = update_task_status(text, "archived")
    write_task(normalized_task_id, text)
    log_event("task_archived", task_id=normalized_task_id)
    retro_line = f"- retro：尚未生成，建议发送 /retro create {normalized_task_id}。"
    if retro_exists(normalized_task_id):
        try:
            retro_meta = task_metadata(read_retro(normalized_task_id))
            retro_line = f"- retro：workbench/retros/{normalized_task_id}.md status={retro_meta.get('status', 'unknown')}"
        except Exception:
            retro_line = f"- retro：workbench/retros/{normalized_task_id}.md"
    return f"""任务已关闭：{normalized_task_id}
- status：archived
- 文件保留：workbench/tasks/{normalized_task_id}.md
{retro_line}"""


def task_status(task_id: str) -> str:
    return task_metadata(read_task(task_id)).get("status", "unknown")


def build_task_handoff_reply(task_id: str, platform: str) -> str:
    target = platform.strip().lower()
    if target not in {"codex", "kiro"}:
        return "用法：/task handoff <task_id> codex|kiro"
    text = read_task(task_id)
    title = task_title_from_text(task_id, text)
    display_platform = "Codex" if target == "codex" else "Kiro"
    goal = task_section(text, "Goal") or "- 未填写。"
    scope = task_section(text, "Scope") or "- 未填写。"
    boundary = task_section(text, "Execution Boundary") or "- 未填写。"
    acceptance = task_section(text, "Acceptance Criteria") or "- 未填写。"
    risks = task_section(text, "Risks") or "- 未填写。"
    evidence_required = task_section(text, "Evidence Required") or "- 未填写。"
    return sanitize_sensitive_text(f"""# {display_platform} 执行交接包

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


def qa_report_items(report: str, acceptance: str) -> dict:
    clean_report = sanitize_sensitive_text(report)
    raw_sensitive = report != clean_report or any(
        marker in report for marker in ("Authorization:", "Cookie:", "password:", "api_key:", "secret:")
    )
    lowered_report = report.lower()
    checks = {
        "修改文件": any(marker in report for marker in ("修改文件", "文件：", ".py", ".md", ".ts", ".tsx", ".js", ".json")) or "modified files" in lowered_report,
        "执行命令": any(marker in report for marker in ("执行命令", "命令：", "python ", "npm ", "pnpm ", "git ", "测试命令")) or any(marker in lowered_report for marker in ("commands", "executed commands")),
        "测试结果": any(marker in report for marker in ("测试结果", "通过", "失败", "未运行", "OK", "pass")) or any(marker in lowered_report for marker in ("test results", "tests", "passed", "failed")),
        "关键日志或截图": any(marker in report for marker in ("关键日志", "截图", "日志", "输出", "证据")) or any(marker in lowered_report for marker in ("logs", "screenshots", "evidence")),
        "未解决风险": any(marker in report for marker in ("未解决风险", "风险", "blocked", "未完成")) or any(marker in lowered_report for marker in ("unresolved risks", "risks", "blockers")),
        "未验证说明": any(marker in report for marker in ("未验证", "未运行", "未覆盖", "待补", "无法验证")) or any(marker in lowered_report for marker in ("unverified", "not verified", "not run")),
        "敏感信息风险": not raw_sensitive,
        "支撑验收标准": bool(report.strip()) and (evidence_markers_present(report) or bool(acceptance.strip())),
    }
    return checks


def build_task_qa_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    sync_task_evidence_state(normalized_task_id)
    text = read_task(normalized_task_id)
    report = task_section(text, "Execution Report")
    acceptance = task_section(text, "Acceptance Criteria")
    has_report = bool(report.strip()) and "### Report at" in report
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

    conclusion = "pass" if has_report and not missing and checks.get("敏感信息风险") and not analysis["live_skipped"] else "needs_evidence"
    recommended = "pass" if conclusion == "pass" else ("blocked" if not has_report else "needs_evidence")
    next_step = (
        f"建议继续 /task review {normalized_task_id}，再由用户按 review 结论决策。"
        if conclusion == "pass"
        else f"建议补齐缺失项后重新 /task report {normalized_task_id}，或 /evidence add {normalized_task_id} <type>。"
    )
    claimed_lines = [f"- {item}" for item in analysis["claimed"]] or ["- 无。"]
    observed_lines = [
        f"- {record['evidence_id']} | {record.get('type', '')} | verified={record.get('verified', 'no')} | {safe_preview(record.get('observed') or record.get('notes'), 140)}"
        for record in analysis["observed"]
    ] or ["- 无。"]
    chain_missing_lines = [f"- {item}" for item in analysis["missing"]] or ["- 无。"]
    return f"""回传质检：{task_id}

质检结论：{conclusion}

claimed：
{chr(10).join(claimed_lines)}

observed：
{chr(10).join(observed_lines)}

missing：
{chr(10).join(chain_missing_lines)}

sensitive_risk：{str(not checks.get('敏感信息风险')).lower()}

已满足项：
{chr(10).join('- ' + item for item in satisfied) if satisfied else '- 无'}

缺失项：
{chr(10).join('- ' + item for item in missing) if missing else '- 无'}

风险项：
{chr(10).join('- ' + item for item in risks) if risks else '- 未发现明显风险'}

建议下一步：
- {next_step}

recommendation：{recommended}
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


def build_evidence_help_reply() -> str:
    return """Atlas 证据链命令
- /evidence help：查看本说明。
- /evidence add <task_id> <type>：下一行开始粘贴证据正文，只记录不执行。
- /evidence list <task_id>：列出任务证据。
- /evidence show <task_id> <evidence_id>：显示证据摘要。
- /evidence mark <task_id> <evidence_id> <verified|partial|rejected> <说明>：人工标记证据可用性。
- /evidence gaps <task_id>：按验收标准和证据链输出缺口。

type 可用：file、command、log、screenshot、ui、live、smoke、report、decision、other。
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
- 下一步：/evidence gaps {normalized_task_id} 或 /evidence mark {normalized_task_id} {evidence_id} verified <说明>"""


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


def build_evidence_gaps_reply(task_id: str) -> str:
    normalized_task_id = normalize_task_id(task_id)
    sync_task_evidence_state(normalized_task_id)
    analysis = evidence_analysis(normalized_task_id)
    return f"""证据缺口：{normalized_task_id}

{build_evidence_gaps_text(normalized_task_id, analysis)}

recommendation：{analysis['recommendation']}
evidence_gap_count：{analysis['evidence_gap_count']}
live_skipped：{str(analysis['live_skipped']).lower()}"""


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
        lines.append(
            f"- {project['project_id']} | {project['status']} | {project['priority'] or 'P?'} | open={open_count} | needs_evidence={needs_count} | evidence_gaps={project_gap_count} | live_skipped={project_live_count} | retro_count={project_retro_count} | {project['title']}"
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
                return "用法：/task handoff <task_id> codex|kiro"
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


def build_template_help_reply() -> str:
    return """模板命令
- /template wo：工作单模板
- /template report：Codex/Kiro 回传模板
- /template review：审查清单模板"""


def handle_local_command(user_text: str, context: dict) -> str | None:
    parts = user_text.strip().split(maxsplit=1)
    command = parts[0].lower() if parts else ""
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
