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
DECISIONS_DIR = WORKBENCH_DIR / "decisions"
DAILY_DIR = WORKBENCH_DIR / "daily"
ARCHIVE_DIR = WORKBENCH_DIR / "archive"
PROCESSED_STATE_KEY = "processed_message_keys"
DEFAULT_PROCESSED_LIMIT = 500
STARTED_AT = time.time()
STARTED_AT_TEXT = datetime.now().astimezone().isoformat(timespec="seconds")
OPEN_TASK_STATUSES = {"draft", "open", "reported", "reviewed", "needs_evidence"}
DECISION_STATUSES = {"pass", "needs_evidence", "blocked", "cancelled"}

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
    sanitized = re.sub(r"(?im)^(\s*Authorization\s*:\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*Cookie\s*:\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*password\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*api_key\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?im)^(\s*secret\s*[:=]\s*).*$", r"\1[REDACTED]", sanitized)
    return sanitized


def ensure_workbench_dirs() -> None:
    for path in (WORKBENCH_DIR, TASKS_DIR, DECISIONS_DIR, DAILY_DIR, ARCHIVE_DIR):
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


def build_task_markdown(task_id: str, title: str) -> str:
    now = iso_now()
    clean_title = sanitize_title(title)
    return f"""# {task_id} {clean_title}

status: open
created_at: {now}
updated_at: {now}
source: octo
mode: consultation

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


def update_task_status(text: str, status: str) -> str:
    text = replace_task_field(text, "status", status)
    text = replace_task_field(text, "updated_at", iso_now())
    return text


def create_task(title: str) -> tuple[str, str]:
    ensure_workbench_dirs()
    task_id = generate_task_id()
    text = build_task_markdown(task_id, title)
    write_task(task_id, text)
    log_event("task_created", task_id=task_id)
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
                "path": path,
            }
        )
    return sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)


def workbench_counts() -> dict:
    counts = {
        "open_tasks": 0,
        "reported_tasks": 0,
        "needs_evidence_tasks": 0,
        "recent_task_id": "",
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
    return counts


def build_task_help_reply() -> str:
    return """Atlas 任务账本命令
- /task help：查看本说明。
- /task new <标题>：创建任务工作单，只写 workbench，不执行。
- /task list：列出最近 10 个任务。
- /task show <task_id>：查看任务摘要、状态和下一步。
- /task report <task_id>：下一行粘贴 Codex/Kiro 回传报告，状态改为 reported。
- /task review <task_id>：读取任务与回传报告，生成 Atlas 审查。
- /task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>：记录用户决策。
- /task close <task_id>：仅 passed/cancelled 可关闭，状态改为 archived。
- /daily brief：汇总今日 open/reported/reviewed/needs_evidence 任务。"""


def build_task_new_reply(title: str) -> str:
    task_id, text = create_task(title)
    return f"""任务已创建：{task_id}

状态：open
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
    return f"""任务摘要：{task_id}
- status：{status}
- title：{task_title_from_text(task_id, text)}
- updated_at：{meta.get('updated_at', '')}
- goal：{goal}
- atlas_review：{review or '尚未审查'}
- user_decision：{decision or '尚未决策'}
- 下一步：{next_step}"""


def build_task_report_reply(task_id: str, report: str) -> str:
    text = read_task(task_id)
    clean_report = sanitize_sensitive_text(report).strip() or "- 空报告：需要补充执行证据。"
    now = iso_now()
    addition = f"### Report at {now}\n{clean_report}"
    text = append_to_section(text, "Execution Report", addition)
    text = append_to_section(text, "Timeline", f"- {now} report appended; status reported.")
    text = update_task_status(text, "reported")
    write_task(task_id, text)
    log_event("task_reported", task_id=task_id)
    return f"""任务已记录回传：{task_id}
- status：reported
- 已追加到：workbench/tasks/{task_id}.md
- 下一步：发送 /task review {task_id}"""


def evidence_markers_present(text: str) -> bool:
    return any(marker in text for marker in ("修改文件", "执行命令", "测试结果", "通过", "日志", "截图", "路径", "输出"))


def build_atlas_review_for_task(task_id: str, text: str) -> str:
    report = task_section(text, "Execution Report")
    acceptance = task_section(text, "Acceptance Criteria")
    has_report = "尚未回传" not in report and bool(report.strip())
    has_markers = evidence_markers_present(report)
    verified = "未发现足够证据。" if not has_markers else "回传中出现了文件、命令、测试或日志类证据，需要按验收标准逐项核对。"
    unverified = "缺少回传报告。" if not has_report else "仍需确认每条验收标准是否有对应证据。"
    if not has_markers:
        unverified = "缺少修改文件、执行命令、测试结果或日志位置，不能判定完成。"
    next_step = "补充证据" if not has_markers else "用户确认是否 pass，或要求补充缺口证据"
    return f"""### Review at {iso_now()}

已验证：
- {verified}
- 验收标准摘要：{safe_preview(acceptance, 220) or '未读取到验收标准。'}

未验证：
- {unverified}

风险：
- 没有证据时不能声称完成。
- 若执行范围、回滚方式、敏感信息处理未说明，需要补充。

待补证据：
- 修改文件。
- 执行命令。
- 测试结果。
- 关键日志或截图。
- 未解决风险说明。

下一步建议：
- {next_step}。
- 用户确认前不关闭任务。"""


def build_task_review_reply(task_id: str) -> str:
    text = read_task(task_id)
    review = build_atlas_review_for_task(task_id, text)
    now = iso_now()
    text = append_to_section(text, "Atlas Review", review)
    text = append_to_section(text, "Timeline", f"- {now} Atlas review generated; status reviewed.")
    text = update_task_status(text, "reviewed")
    write_task(task_id, text)
    log_event("task_reviewed", task_id=task_id)
    return f"""Atlas 审查已生成：{task_id}

{review}

状态：reviewed
下一步：
- /task decide {task_id} needs_evidence <说明>
- 或 /task decide {task_id} pass <说明>"""


def build_task_decide_reply(task_id: str, decision: str, note: str) -> str:
    normalized = decision.strip().lower()
    if normalized not in DECISION_STATUSES:
        return "决策无效。可用：pass、needs_evidence、blocked、cancelled。"
    status = "passed" if normalized == "pass" else normalized
    clean_note = sanitize_sensitive_text(note).strip() or "未填写说明。"
    now = iso_now()
    text = read_task(task_id)
    decision_text = f"### Decision at {now}\n- decision：{normalized}\n- status：{status}\n- note：{clean_note}"
    text = append_to_section(text, "User Decision", decision_text)
    text = append_to_section(text, "Timeline", f"- {now} user decision {normalized}; status {status}.")
    text = update_task_status(text, status)
    write_task(task_id, text)
    decision_record = f"# {task_id} decision\n\n{decision_text}\n"
    decision_path(task_id).write_text(sanitize_sensitive_text(decision_record), encoding="utf-8")
    log_event("task_decided", task_id=task_id, decision=normalized)
    return f"""用户决策已记录：{task_id}
- decision：{normalized}
- status：{status}
- 下一步：{('/task close ' + task_id) if status in {'passed', 'cancelled'} else '按决策补证据、处理阻塞或暂停'}"""


def build_task_close_reply(task_id: str) -> str:
    text = read_task(task_id)
    status = task_metadata(text).get("status", "unknown")
    if status not in {"passed", "cancelled"}:
        return f"不能关闭：{task_id} 当前 status={status}。只有 passed/cancelled 可以关闭。"
    now = iso_now()
    text = append_to_section(text, "Timeline", f"- {now} task archived from status {status}.")
    text = update_task_status(text, "archived")
    write_task(task_id, text)
    log_event("task_archived", task_id=task_id)
    return f"""任务已关闭：{task_id}
- status：archived
- 文件保留：workbench/tasks/{task_id}.md"""


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
        for record in records[:20]:
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
        if subcommand == "report":
            report_parts = tail.split(maxsplit=1)
            if not report_parts:
                return "用法：/task report <task_id>\\n<粘贴 Codex/Kiro 回传报告>"
            task_id = report_parts[0]
            inline = report_parts[1] if len(report_parts) > 1 else ""
            body = "\n".join(lines[1:]).strip()
            report = body or inline
            return build_task_report_reply(task_id, report)
        if subcommand == "review":
            return build_task_review_reply(tail)
        if subcommand == "decide":
            decision_parts = tail.split(maxsplit=2)
            if len(decision_parts) < 2:
                return "用法：/task decide <task_id> <pass|needs_evidence|blocked|cancelled> <说明>"
            note = decision_parts[2] if len(decision_parts) > 2 else ""
            return build_task_decide_reply(decision_parts[0], decision_parts[1], note)
        if subcommand == "close":
            return build_task_close_reply(tail)
        return build_task_help_reply()
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
- /task help：查看本地任务账本命令。
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
