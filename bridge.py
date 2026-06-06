from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "state.json"
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "bridge.log"
PROCESSED_STATE_KEY = "processed_message_keys"
DEFAULT_PROCESSED_LIMIT = 500
STARTED_AT = time.time()

LOGGER = logging.getLogger("octo-hermes-bridge")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    if LOGGER.handlers:
        return

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(stream_handler)


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
    tmp_file = STATE_FILE.with_suffix(".json.tmp")
    tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_file, STATE_FILE)


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
- 证据不足时只能给出建议或计划，不能报告完成。"""


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
    registered = "已注册" if context.get("registered") else "未注册"
    last_error = state.get("last_error")
    lines = [
        "Octo-Hermes Bridge 状态",
        f"- 模式：咨询/调度",
        f"- 注册：{registered}",
        f"- 运行时长：{format_uptime(time.time() - STARTED_AT)}",
        f"- last_seq：{context.get('last_seq', 0)}",
        f"- 去重缓存：{len(processed)} 条",
        f"- 日志：logs/bridge.log",
        "- 安全边界：Bridge 只生成咨询回复或工作单，不执行命令、不修改文件。",
    ]
    if context.get("robot_id"):
        lines.append(f"- robot_id：{short_id(context.get('robot_id'))}")
    if context.get("owner_channel_id"):
        lines.append(f"- owner_channel_id：{short_id(context.get('owner_channel_id'))}")
    if last_error:
        lines.append(f"- 最近错误：{safe_preview(str(last_error), 120)}")
    return "\n".join(lines)


def build_help_reply() -> str:
    return """Octo-Hermes Bridge 使用说明
- /status：查看 bridge 注册、last_seq、去重缓存、日志路径和安全边界。
- /help：查看本说明。
- 普通消息：进入 Atlas 咨询/调度模式，用于理解目标、拆解任务、审查方案。
- 执行类请求：只生成工作单和验收标准，不运行命令、不修改文件、不提交或发布。"""


def handle_local_command(user_text: str, context: dict) -> str | None:
    command = user_text.strip().split(maxsplit=1)[0].lower() if user_text.strip() else ""
    if command == "/status":
        return build_status_reply(context)
    if command == "/help":
        return build_help_reply()
    return None


def prepare_reply(user_text: str, context: dict) -> tuple[str, str]:
    local_reply = handle_local_command(user_text, context)
    if local_reply is not None:
        return local_reply, "local_command"
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
    log_event("startup", cwd=ROOT, log_file=LOG_FILE.relative_to(ROOT), mode="consultation")

    try:
        load_env()
    except BaseException as exc:
        log_error("startup_failed", exc)
        raise

    token = os.environ.get("OCTO_BOT_TOKEN", "").strip()
    if not token.startswith("bf_"):
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
    poll_seconds = float(os.environ.get("POLL_SECONDS", "2"))

    state = load_state()
    last_seq = int(state.get("last_seq", 0))
    state["last_seq"] = last_seq
    limit = processed_limit()
    processed_set = set(state.get(PROCESSED_STATE_KEY, []))

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
        log_event("baseline_set", last_seq=last_seq, seen=len(messages))
        log_event("ready", poll_seconds=poll_seconds)

    while True:
        try:
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

                context = {
                    "registered": True,
                    "robot_id": robot_id,
                    "owner_channel_id": owner_channel_id,
                    "last_seq": last_seq,
                    "state": state,
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
                    log_error("message_failed", exc, seq=seq)

            time.sleep(poll_seconds)

        except KeyboardInterrupt:
            log_event("stopped")
            return 0
        except Exception as exc:
            state["last_error"] = f"loop {type(exc).__name__}: {redact(exc)}"
            save_state(state)
            log_error("loop_error", exc)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
