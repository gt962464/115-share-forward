#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""115 卡片项目第二阶段管理后台。

仅设计为 NAS 内网使用：通过 Docker socket 控制两个项目容器，编辑白名单环境变量，
查看日志/重试队列，保存配置备份。敏感配置只在 NAS 项目目录读取和写入，不写入响应。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from html import escape
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from aiohttp import web

HOST = os.getenv("ADMIN_HOST", "0.0.0.0")
PORT = int(os.getenv("ADMIN_PORT", "18810"))
PROJECT_DIR = Path(os.getenv("PROJECT_DIR", "/project"))
DATA_DIR = Path(os.getenv("ADMIN_DATA_DIR", "/admin-data"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
CARD_ENV = PROJECT_DIR / ".env"
MONITOR_ENV = PROJECT_DIR / "tg-monitor" / ".env"
ROOT_ENV = PROJECT_DIR.parent / ".env"
MOVER_CONTAINER = "cd2-mount-mover"
MOVER_STATE_FILE = PROJECT_DIR / "cd2-mount-mover" / "state" / "state.json"
MOVER_PIPELINE_DIR = PROJECT_DIR / "state" / "mount-pipeline-queue"
MOVER_SOURCE_DIR = "/vol2/1000/转存"
MOVER_TARGET_DIR = "/vol2/1000/CloudDrive/自动转存"
MANAGED_SERVICES = ["p115-card-bot", "tg-user-monitor", MOVER_CONTAINER]
BACKUP_DIR = DATA_DIR / "backups"
SESSION_COOKIE = "card_admin_session"
SESSIONS: dict[str, float] = {}
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_KEYS = {"P115_COOKIE", "TG_API_HASH", "TG_BOT_TOKEN", "RECYCLE_PASSWORD", "ADMIN_PASSWORD", "ADMIN_SECRET"}
EDITABLE_CARD_KEYS = ["P115_COOKIE", "TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_USER_ID", "TG_ALLOW_CHATS", "TMDB_API_KEY", "TMDB_LANG", "TG_PROXY", "APP_HTTP_PROXY", "LOG_LEVEL", "CARD_BOT_TEST_MODE", "P115_SAVE_DIR", "RECYCLE_PASSWORD", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"]
LLM_CARD_KEYS = ["LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"]
LLM_PROMPT_FILE = DATA_DIR / "llm_prompt.json"
EDITABLE_MONITOR_KEYS = ["TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "TG_PHONE", "TG_SOURCE_CHAT", "TG_FORWARD_TO", "TG_PROXY", "TG_START_FROM", "TG_FORWARD_TIMEOUT", "TG_MONITOR_WEB_PORT", "TG_MONITOR_WEB_SECRET", "LOG_LEVEL"]
EDITABLE_ROOT_KEYS = ["P115_COOKIE", "TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_USER_ID", "TG_ALLOW_CHATS", "TMDB_API_KEY", "TMDB_LANG", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL", "TG_PROXY", "APP_HTTP_PROXY", "P115_SAVE_DIR", "LOG_LEVEL", "AUTO_PROCESS_115_LINKS", "AUTO_DELETE_AFTER", "RECYCLE_PASSWORD", "ADMIN_PASSWORD", "ADMIN_SECRET"]

# 字段中文说明（大白话）
FIELD_LABELS = {
    "P115_SAVE_DIR": "保存目录名",
    "TG_CHANNEL_ID": "频道ID",
    "TG_USER_ID": "管理员ID",
    "TG_ALLOW_CHATS": "允许的聊天ID",
    "TMDB_API_KEY": "TMDB密钥",
    "TMDB_LANG": "语言",
    "TG_PROXY": "TG代理",
    "APP_HTTP_PROXY": "HTTP代理",
    "LOG_LEVEL": "日志级别",
    "CARD_BOT_TEST_MODE": "测试模式",
    "LLM_API_BASE": "API地址",
    "LLM_API_KEY": "API密钥",
    "LLM_MODEL": "模型名称",
    "TG_PHONE": "手机号",
    "TG_SOURCE_CHAT": "来源聊天",
    "TG_FORWARD_TO": "转发目标",
    "TG_START_FROM": "起始消息ID",
    "TG_FORWARD_TIMEOUT": "转发超时",
    "TG_MONITOR_WEB_PORT": "网页端口",
    "TG_MONITOR_WEB_SECRET": "网页密钥",
}


def now() -> float:
    return time.time()


def authorized(request: web.Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    return bool(token and SESSIONS.get(token, 0) > now())


def require_auth(request: web.Request) -> None:
    if not authorized(request):
        raise web.HTTPUnauthorized(text="需要先登录")


def json_body(request: web.Request) -> dict[str, Any]:
    return request["json_body"]


@web.middleware
async def body_middleware(request: web.Request, handler):
    if request.can_read_body and request.content_type == "application/json":
        try:
            request["json_body"] = await request.json()
        except Exception:
            request["json_body"] = {}
    else:
        request["json_body"] = {}
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)[:300]}, status=500)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(path: Path, updates: dict[str, str], allowed: list[str]) -> None:
    if not path.exists():
        raise RuntimeError(f"配置文件不存在: {path}")
    allowed_set = set(allowed)
    clean = {k: str(v) for k, v in updates.items() if k in allowed_set and ENV_KEY_RE.match(k)}
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        raw = line
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in clean:
                result.append(f"{key}={clean[key]}")
                seen.add(key)
                continue
        result.append(raw)
    for key, value in clean.items():
        if key not in seen:
            result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def redact(values: dict[str, str]) -> dict[str, str]:
    return {k: ("********" if k in SENSITIVE_KEYS and v else v) for k, v in values.items()}


class DockerSocket:
    """Minimal Docker Engine API client over the mounted Unix socket."""
    def request(self, method: str, path: str, body: bytes = b"", timeout: int = 30) -> tuple[int, str]:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect("/var/run/docker.sock")
            headers = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
            if body:
                headers += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
            headers.append("\r\n")
            sock.sendall(("\r\n".join(headers)).encode() + body)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        raw = b"".join(chunks)
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.splitlines()[0].decode("latin1") if head else "HTTP/1.1 500"
        status = int(status_line.split()[1])
        header_text = head.decode("latin1").lower()
        if "transfer-encoding: chunked" in header_text:
            payload = self._decode_chunked(payload)
        return status, payload.decode("utf-8", "replace")

    @staticmethod
    def _decode_chunked(payload: bytes) -> bytes:
        output = bytearray()
        offset = 0
        while offset < len(payload):
            end = payload.find(b"\r\n", offset)
            if end < 0:
                break
            try:
                size = int(payload[offset:end].split(b";", 1)[0], 16)
            except ValueError:
                break
            offset = end + 2
            if size == 0:
                break
            output.extend(payload[offset:offset + size])
            offset += size + 2
        return bytes(output)

    def json(self, method: str, path: str, body: dict | None = None, timeout: int = 30) -> tuple[int, dict | list | str]:
        raw = json.dumps(body).encode() if body is not None else b""
        status, payload = self.request(method, path, raw, timeout)
        try:
            return status, json.loads(payload) if payload else {}
        except Exception:
            return status, payload


DOCKER = DockerSocket()


def docker(*args: str, timeout: int = 30) -> tuple[int, str]:
    """Compatibility wrapper backed by Docker Engine HTTP API."""
    if not args:
        return 1, "missing docker command"
    command = args[0]
    if command == "inspect" and len(args) >= 4 and args[1] == "-f":
        name = args[3]
        status, data = DOCKER.json("GET", f"/containers/{name}/json", timeout=timeout)
        if status >= 300 or not isinstance(data, dict):
            return 1, str(data)
        state = data.get("State", {})
        return 0, f"{state.get('Status','')}|{state.get('StartedAt','')}|{state.get('RestartCount',0)}"
    if command in {"start", "stop", "restart"} and len(args) >= 2:
        status, data = DOCKER.json("POST", f"/containers/{args[1]}/{command}", timeout=timeout)
        return (0 if status < 300 else 1), str(data)
    if command == "logs" and len(args) >= 4:
        name = args[-1]
        status, payload = DOCKER.request("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail=300", timeout=timeout)
        return (0 if status < 300 else 1), payload
    return 1, f"unsupported docker operation: {args}"


def service_status(name: str) -> dict[str, str]:
    code, output = docker("inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}", name)
    if code != 0:
        return {"name": name, "status": "not_found", "detail": output[-300:]}
    status, started, restart = (output.split("|", 2) + ["", ""])[:3]
    return {"name": name, "status": status, "started": started, "restart_count": restart}


def mount_mover_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "service": service_status(MOVER_CONTAINER),
        "source": MOVER_SOURCE_DIR,
        "target": MOVER_TARGET_DIR,
        "baseline_done": False,
        "tracked": 0,
        "counts": {"baseline": 0, "waiting": 0, "moving": 0, "completed": 0},
        "files": [],
        "pipeline_counts": {"pending": 0, "processing": 0, "failed": 0, "done": 0},
        "pipeline_jobs": [],
    }
    try:
        data = json.loads(MOVER_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return snapshot
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return snapshot
    snapshot["baseline_done"] = bool(data.get("baseline_done"))
    files = data["files"]
    snapshot["tracked"] = len(files)
    for relative, entry in files.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "unknown"))
        snapshot["counts"][status] = snapshot["counts"].get(status, 0) + 1
        snapshot["files"].append({
            "path": str(relative),
            "status": status,
            "size": int(entry.get("size", 0) or 0),
            "stable_polls": int(entry.get("stable_polls", 0) or 0),
        })
    snapshot["files"] = snapshot["files"][-30:]
    for path in sorted(MOVER_PIPELINE_DIR.glob("*.json")):
        status = next((value for value in ("pending", "processing", "failed", "done") if path.name.endswith(f".{value}.json")), "unknown")
        if status == "unknown":
            continue
        snapshot["pipeline_counts"][status] += 1
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            task = {}
        if not isinstance(task, dict):
            task = {}
        snapshot["pipeline_jobs"].append({
            "status": status,
            "path": str(task.get("relative_path", path.name)),
            "title": str(task.get("display_title", "")),
            "share_link": str(task.get("share_link", "")),
            "error": str(task.get("last_error", "")),
        })
    snapshot["pipeline_jobs"] = snapshot["pipeline_jobs"][-30:]
    return snapshot


def backup_config() -> str:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = BACKUP_DIR / stamp
    target.mkdir()
    for source in [CARD_ENV, MONITOR_ENV]:
        if source.exists():
            shutil.copy2(source, target / source.name)
    return stamp


def render_field(key: str, value: str) -> str:
    safe = escape(str(value or ""), quote=True)
    placeholder = "保持不变" if value == "********" else ""
    return f'<label>{escape(key)}<input name="{escape(key)}" value="{safe}" placeholder="{placeholder}"></label>'


def render_admin_page(message: str = "") -> str:
    card = parse_env(CARD_ENV)
    monitor = parse_env(MONITOR_ENV)
    services = [service_status("p115-card-bot"), service_status("tg-user-monitor")]
    service_html = "".join(f'<div class="service"><b>{escape(s["name"])}</b><span class="{escape(s["status"])}">{escape(s["status"])}</span></div>' for s in services)
    card_html = "".join(render_field(k, "********" if k in SENSITIVE_KEYS and card.get(k) else card.get(k, "")) for k in EDITABLE_CARD_KEYS)
    monitor_html = "".join(render_field(k, "********" if k in SENSITIVE_KEYS and monitor.get(k) else monitor.get(k, "")) for k in EDITABLE_MONITOR_KEYS)
    log_code, log_text = docker("logs", "--tail", "120", "tg-user-monitor")
    queue_path = PROJECT_DIR / "state" / "retry_queue.json"
    queue_text = queue_path.read_text(encoding="utf-8") if queue_path.exists() else "[]"
    notice = f'<div class="notice">{escape(message)}</div>' if message else ""
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>115 分享转发 · 管理后台</title><style>body{{font:14px system-ui;margin:0;background:#f3f6fb;color:#152238}}header{{background:#fff;padding:18px 24px;border-bottom:1px solid #dce5f1}}main{{max-width:1100px;margin:18px auto;padding:0 14px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}section{{background:#fff;border:1px solid #dce5f1;border-radius:12px;padding:16px}}.wide{{grid-column:1/-1}}h2{{font-size:16px;margin:0 0 12px}}label{{display:block;color:#60718a;font-size:12px;margin:7px 0}}input{{display:block;width:100%;box-sizing:border-box;padding:9px;border:1px solid #cbd7e7;border-radius:8px;margin-top:4px}}button{{border:0;border-radius:8px;background:#1769e0;color:#fff;padding:9px 12px;margin:5px 5px 0 0}}button.red{{background:#c13b45}}.service{{display:flex;justify-content:space-between;padding:10px;border-bottom:1px solid #edf1f6}}.running{{color:#16865b}}.stopped,.not_found{{color:#c13b45}}pre{{background:#111b2b;color:#d8e6ff;padding:12px;border-radius:8px;max-height:240px;overflow:auto;white-space:pre-wrap;word-break:break-word}}.notice{{background:#eaf5ee;color:#146b49;padding:10px;border-radius:8px;margin-bottom:14px}}@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}}}</style><header><b>115 分享转发 · 管理后台</b></header><main>{notice}<div class="grid"><section><h2>服务状态</h2>{service_html}<form method="post" action="/action"><button name="action" value="refresh">刷新状态</button><button name="action" value="backup">备份配置</button></form></section><section><h2>Telegram 监听配置</h2><form method="post" action="/env/monitor">{monitor_html}<button type="submit">保存并重启监听器</button></form></section><section><h2>卡片机器人配置</h2><form method="post" action="/env/card">{card_html}<button type="submit">保存并重启卡片机器人</button></form></section><section><h2>服务操作</h2><form method="post" action="/action"><button name="action" value="compose-up">启动全部服务</button><button name="action" value="compose-down" class="red">停止全部服务</button><button name="action" value="clear-retry">清空重试队列</button></form></section><section class="wide"><h2>实时日志</h2><pre>{escape(log_text[-4000:])}</pre></section><section class="wide"><h2>自动重试队列</h2><pre>{escape(queue_text[-4000:])}</pre></section></div></main></body></html>'''


LOGIN_PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>115 项目管理后台</title><link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"><style>
:root{--bg:#fafbfc;--surface:#ffffff;--surface-hover:#f8f9fa;--border:#e5e7eb;--border-light:#f0f1f3;--text:#111827;--text-secondary:#6b7280;--text-muted:#9ca3af;--primary:#3b82f6;--primary-hover:#2563eb;--primary-light:#eff6ff;--success:#10b981;--success-bg:#ecfdf5;--danger:#ef4444;--danger-bg:#fef2f2;--warning:#f59e0b;--warning-bg:#fffbeb;--radius:12px;--radius-sm:8px;--shadow-sm:0 1px 2px 0 rgba(0,0,0,0.05);--shadow:0 1px 3px 0 rgba(0,0,0,0.1),0 1px 2px -1px rgba(0,0,0,0.1);--shadow-md:0 4px 6px -1px rgba(0,0,0,0.1),0 2px 4px -2px rgba(0,0,0,0.1);--font-sans:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;--font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,monospace}*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--text);font-family:var(--font-sans);font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
#login{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.login-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px;width:100%;max-width:400px;box-shadow:var(--shadow-md)}
.login-card h1{font-size:24px;font-weight:700;margin-bottom:8px;background:linear-gradient(135deg,var(--primary),#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.login-card .subtitle{color:var(--text-secondary);font-size:14px;margin-bottom:32px}
.login-card input{width:100%;padding:12px 16px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:14px;font-family:var(--font-sans);transition:border-color 0.2s,box-shadow 0.2s}
.login-card input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-light)}
.login-card button{width:100%;padding:12px;background:var(--primary);color:#fff;border:none;border-radius:var(--radius-sm);font-size:14px;font-weight:600;font-family:var(--font-sans);cursor:pointer;transition:background 0.2s,transform 0.1s;margin-top:16px}
.login-card button:hover{background:var(--primary-hover)}
.login-card button:active{transform:scale(0.98)}
#loginMsg{color:var(--danger);font-size:13px;margin-top:12px;text-align:center;min-height:20px}
#app{display:none;min-height:100vh}
.header{background:var(--surface);border-bottom:1px solid var(--border);padding:16px 32px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;backdrop-filter:blur(8px);background:rgba(255,255,255,0.9)}
.header-left{display:flex;align-items:center;gap:16px}
.header-logo{width:36px;height:36px;background:linear-gradient(135deg,var(--primary),#8b5cf6);border-radius:10px;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:16px}
.header-title{font-size:18px;font-weight:700;color:var(--text)}
.header-subtitle{font-size:12px;color:var(--text-muted);margin-top:2px}
.btn-logout{padding:8px 16px;background:transparent;color:var(--text-secondary);border:1px solid var(--border);border-radius:var(--radius-sm);font-size:13px;font-weight:500;cursor:pointer;transition:all 0.2s;font-family:var(--font-sans)}
.btn-logout:hover{background:var(--danger-bg);color:var(--danger);border-color:var(--danger)}
.main{max-width:1400px;margin:0 auto;padding:24px 32px}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px}
.wide{grid-column:1/-1}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:24px;transition:box-shadow 0.2s}
.card:hover{box-shadow:var(--shadow)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.card-title{font-size:16px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:10px}
.card-title::before{content:'';width:3px;height:18px;background:linear-gradient(180deg,var(--primary),#8b5cf6);border-radius:2px}
.card-badge{font-size:12px;font-weight:500;padding:4px 10px;border-radius:20px;background:var(--primary-light);color:var(--primary)}
.service{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border:1px solid var(--border-light);border-radius:var(--radius-sm);margin-bottom:10px;background:var(--surface-hover);transition:all 0.2s}
.service:last-child{margin-bottom:0}
.service:hover{background:var(--surface);border-color:var(--border)}
.service-info{display:flex;flex-direction:column;gap:4px}
.service-name{font-weight:600;font-size:14px}
.service-meta{font-size:12px;color:var(--text-muted)}
.status-badge{font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px}
.status-running{background:var(--success-bg);color:var(--success)}
.status-stopped{background:var(--danger-bg);color:var(--danger)}
.fields{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12px;font-weight:500;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px}
.field input,.field textarea{padding:10px 14px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:14px;font-family:var(--font-sans);transition:border-color 0.2s,box-shadow 0.2s;background:var(--surface)}
.field input:focus,.field textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-light)}
.field input::placeholder{color:var(--text-muted)}
.prompt-area{display:flex;flex-direction:column;gap:12px}
.prompt-header{display:flex;justify-content:space-between;align-items:center}
.prompt-label{font-size:13px;font-weight:600;color:var(--text-secondary)}
.prompt-textarea{width:100%;min-height:280px;padding:16px;border:1px solid var(--border);border-radius:var(--radius-sm);font-family:var(--font-mono);font-size:13px;line-height:1.7;resize:vertical;transition:border-color 0.2s,box-shadow 0.2s;background:var(--surface)}
.prompt-textarea:focus{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px var(--primary-light)}
.btn-group{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
.btn{padding:10px 18px;border-radius:var(--radius-sm);font-size:13px;font-weight:500;font-family:var(--font-sans);cursor:pointer;transition:all 0.2s;display:inline-flex;align-items:center;gap:6px;border:none}
.btn:active{transform:scale(0.98)}
.btn-primary{background:var(--primary);color:#fff}
.btn-primary:hover{background:var(--primary-hover)}
.btn-secondary{background:var(--surface);color:var(--text-secondary);border:1px solid var(--border)}
.btn-secondary:hover{background:var(--surface-hover);color:var(--text);border-color:var(--text-muted)}
.btn-danger{background:var(--danger);color:#fff}
.btn-danger:hover{background:#dc2626}
.btn-sm{padding:6px 12px;font-size:12px}
.log-controls{display:flex;gap:10px;align-items:center;margin-bottom:12px}
.log-controls select{padding:8px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);font-size:13px;font-family:var(--font-sans);background:var(--surface);cursor:pointer}
pre{background:#1e293b;color:#e2e8f0;padding:16px;border-radius:var(--radius-sm);font-family:var(--font-mono);font-size:12px;line-height:1.6;max-height:400px;overflow:auto;white-space:pre-wrap;word-break:break-word}
pre::-webkit-scrollbar{width:8px;height:8px}
pre::-webkit-scrollbar-track{background:#334155;border-radius:4px}
pre::-webkit-scrollbar-thumb{background:#475569;border-radius:4px}
pre::-webkit-scrollbar-thumb:hover{background:#64748b}
 .hint{font-size:12px;color:var(--text-muted);margin-top:8px}
 .danger{color:var(--danger)}
 .notice{background:var(--success-bg);color:var(--success);padding:12px 16px;border-radius:var(--radius-sm);margin-bottom:20px;font-size:13px;font-weight:500}
 .flow{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:12px;margin-bottom:20px}
 .flow-node{border:1px solid var(--border);border-radius:var(--radius-sm);padding:14px;background:var(--surface-hover)}
 .flow-node strong{display:block;font-size:14px;margin-bottom:4px}
 .flow-node span{display:block;color:var(--text-secondary);font-size:12px;word-break:break-all}
 .flow-arrow{font-size:22px;color:var(--primary);font-weight:700}
 .mover-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px}
 .mover-stat{border:1px solid var(--border-light);border-radius:var(--radius-sm);padding:10px;background:var(--surface-hover)}
 .mover-stat strong{display:block;font-size:20px;color:var(--text)}
 .mover-stat span{font-size:12px;color:var(--text-secondary)}
 .mover-files{max-height:280px}
 .status-not_found{background:var(--danger-bg);color:var(--danger)}
 @media(max-width:1024px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.fields{grid-template-columns:1fr}}
 @media(max-width:640px){.header{padding:12px 16px}.main{padding:16px}.card{padding:20px}.prompt-textarea{min-height:200px}.flow{grid-template-columns:1fr}.flow-arrow{transform:rotate(90deg);text-align:center}.mover-stats{grid-template-columns:repeat(2,1fr)}}
</style></head><body>
<div id="login">
<div class="login-card">
<h1>115 分享转发</h1>
<p class="subtitle">管理后台 · 仅限内网访问</p>
<form id="loginForm">
<input id="pwd" name="password" type="password" placeholder="输入管理员密码" autocomplete="current-password">
<button type="submit">登录</button>
</form>
<p id="loginMsg"></p>
</div>
</div>
<div id="app">
<header class="header">
<div class="header-left">
<div class="header-logo">115</div>
<div>
<div class="header-title">115 分享转发 · 管理后台</div>
<div class="header-subtitle">Card Bot & Telegram Monitor</div>
</div>
</div>
<button class="btn-logout" onclick="logout()">退出登录</button>
</header>
<main class="main">
<div id="notice"></div>
<div class="grid">
<section class="card">
<div class="card-header"><h2 class="card-title">服务状态</h2></div>
<div id="services"></div>
<div class="btn-group"><button class="btn btn-primary" onclick="refresh()">刷新状态</button><button class="btn btn-secondary" onclick="act('backup')">备份配置</button></div>
</section>
<section class="card wide">
<div class="card-header"><h2 class="card-title">挂载转存流程</h2><span id="moverBadge" class="card-badge">cd2-mount-mover</span></div>
<div class="flow"><div class="flow-node"><strong>123 云盘挂载</strong><span id="moverSource">加载中...</span></div><div class="flow-arrow">→</div><div class="flow-node"><strong>115 云盘挂载</strong><span id="moverTarget">加载中...</span></div></div>
<div id="moverStats" class="mover-stats"></div>
<div class="btn-group"><button class="btn btn-primary btn-sm" onclick="mountMover()">刷新流程</button><button class="btn btn-secondary btn-sm" onclick="restart('cd2-mount-mover')">重启转存服务</button></div>
<pre id="moverFiles" class="mover-files">加载中...</pre>
<pre id="moverPipeline" class="mover-files">闭环队列加载中...</pre>
</section>
<section class="card">
<div class="card-header"><h2 class="card-title">卡片机器人配置</h2><span class="card-badge">p115-card-bot</span></div>
<div class="fields" id="cardFields"></div>
<div class="btn-group"><button class="btn btn-primary" onclick="saveEnv('card')">保存配置</button><button class="btn btn-secondary" onclick="restart('p115-card-bot')">重启机器人</button></div>
</section>
<section class="card wide">
<div class="card-header"><h2 class="card-title">LLM 辅助识别</h2><span class="card-badge">AI Powered</span></div>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
<div>
<h3 style="font-size:14px;font-weight:600;margin-bottom:16px;color:var(--text)">基础配置</h3>
<div class="fields" id="llmFields"></div>
<div class="btn-group"><button class="btn btn-primary" onclick="saveEnv('llm')">保存 LLM 配置</button><button class="btn btn-secondary" onclick="restart('p115-card-bot')">重启生效</button></div>
</div>
<div>
<div class="prompt-area">
<div class="prompt-header"><span class="prompt-label">辅助识别提示词</span><div style="display:flex;gap:8px"><button class="btn btn-secondary btn-sm" onclick="resetLlmPrompt()">恢复默认</button><button class="btn btn-primary btn-sm" onclick="saveLlmPrompt()">保存提示词</button></div></div>
<textarea id="llmPrompt" class="prompt-textarea" placeholder="加载中..."></textarea>
<p class="hint">修改提示词后点击"保存提示词"立即生效，无需重启。</p>
</div>
</div>
</div>
</section>
<section class="card">
<div class="card-header"><h2 class="card-title">Telegram 监听</h2><span class="card-badge">tg-user-monitor</span></div>
<div class="fields" id="monitorFields"></div>
<div class="btn-group"><button class="btn btn-primary" onclick="saveEnv('monitor')">保存配置</button><button class="btn btn-secondary" onclick="restart('tg-user-monitor')">重启监听器</button></div>
<p class="hint">S7 目标使用你的用户账号直发；不要把 Token 填进这里。</p>
</section>
<section class="card">
<div class="card-header"><h2 class="card-title">操作提示</h2></div>
<div style="font-size:13px;color:var(--text-secondary);line-height:1.8">
<p>• 修改配置后要点击对应的<b>重启按钮</b>才会生效</p>
<p>• 敏感值只显示掩码；后台不提供直接查看 Cookie、Token 的功能</p>
<p style="margin-top:16px;display:flex;gap:10px"><button class="btn btn-secondary" onclick="act('compose-up')">启动全部服务</button><button class="btn btn-danger" onclick="act('compose-down')">停止全部服务</button></p>
</div>
</section>
<section class="card wide">
<div class="card-header"><h2 class="card-title">实时日志</h2></div>
<div class="log-controls"><select id="logService"><option value="p115-card-bot">卡片机器人</option><option value="tg-user-monitor">Telegram 监听器</option><option value="cd2-mount-mover">挂载转存</option></select><button class="btn btn-primary btn-sm" onclick="logs()">刷新日志</button></div>
<pre id="log">加载中...</pre>
</section>
<section class="card wide">
<div class="card-header"><h2 class="card-title">自动重试队列</h2></div>
<div class="btn-group" style="margin-bottom:12px"><button class="btn btn-primary btn-sm" onclick="retryQueue()">刷新队列</button><button class="btn btn-danger btn-sm" onclick="act('clear-retry')">清空队列</button></div>
<pre id="queue">加载中...</pre>
</section>
</div>
</main>
</div>
<script>
const cardKeys=['P115_SAVE_DIR','TG_CHANNEL_ID','TG_USER_ID','TG_ALLOW_CHATS','TMDB_API_KEY','TMDB_LANG','TG_PROXY','APP_HTTP_PROXY','LOG_LEVEL','CARD_BOT_TEST_MODE'];
const llmKeys=['LLM_API_BASE','LLM_API_KEY','LLM_MODEL'];
const monitorKeys=['TG_PHONE','TG_SOURCE_CHAT','TG_FORWARD_TO','TG_PROXY','TG_START_FROM','TG_FORWARD_TIMEOUT','TG_MONITOR_WEB_PORT','TG_MONITOR_WEB_SECRET','LOG_LEVEL'];
const fieldLabels={'P115_SAVE_DIR':'保存目录名','TG_CHANNEL_ID':'频道ID','TG_USER_ID':'管理员ID','TG_ALLOW_CHATS':'允许的聊天ID','TMDB_API_KEY':'TMDB密钥','TMDB_LANG':'语言','TG_PROXY':'TG代理','APP_HTTP_PROXY':'HTTP代理','LOG_LEVEL':'日志级别','CARD_BOT_TEST_MODE':'测试模式','LLM_API_BASE':'API地址','LLM_API_KEY':'API密钥','LLM_MODEL':'模型名称','TG_PHONE':'手机号','TG_SOURCE_CHAT':'来源聊天','TG_FORWARD_TO':'转发目标','TG_START_FROM':'起始消息ID','TG_FORWARD_TIMEOUT':'转发超时','TG_MONITOR_WEB_PORT':'网页端口','TG_MONITOR_WEB_SECRET':'网页密钥'};
let state={};
async function req(path,opts={}){let r=await fetch(path,{credentials:'same-origin',...opts});let j=await r.json().catch(()=>({}));if(r.status===401){showLogin();throw Error('未登录')}if(!r.ok)throw Error(j.error||j.message||'请求失败');return j}
document.querySelector('#loginForm').addEventListener('submit',e=>{e.preventDefault();login()});
async function login(){let msg=document.querySelector('#loginMsg');let password=document.querySelector('#pwd').value;if(!password){msg.textContent='请输入管理员密码';return}msg.textContent='';try{await req('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});showApp();await refresh()}catch(e){msg.textContent='登录失败：'+e.message}}
async function logout(){await req('/api/logout',{method:'POST'});showLogin()}
function showLogin(){document.querySelector('#login').style.display='flex';document.querySelector('#app').style.display='none'}
function showApp(){document.querySelector('#login').style.display='none';document.querySelector('#app').style.display='block'}
function fields(id,values,keys){document.querySelector('#'+id).innerHTML=keys.map(k=>{let val=values[k]||'';let isSecret=val==='********';let label=fieldLabels[k]||k;return '<div class="field"><label>'+label+'</label><input data-key="'+k+'" type="'+(isSecret?'password':'text')+'" value="'+(isSecret?'':val)+'" placeholder="'+(isSecret?'保持不变':'请输入'+label)+'" '+(isSecret?'data-secret="1"':'')+'></div>'}).join('')}
async function refresh(){try{let j=await req('/api/overview');state=j;document.querySelector('#services').innerHTML=j.services.map(s=>'<div class="service"><div class="service-info"><div class="service-name">'+s.name+'</div><div class="service-meta">启动: '+(s.started||'-')+'　重启: '+(s.restart_count||'-')+'</div></div><span class="status-badge status-'+s.status+'">'+s.status+'</span></div>').join('');fields('cardFields',j.card_env,cardKeys);fields('llmFields',j.card_env,llmKeys);fields('monitorFields',j.monitor_env,monitorKeys);await loadLlmPrompt();await logs();await retryQueue();await mountMover();showApp()}catch(e){if(e.message==='未登录')showLogin()}}
async function saveEnv(kind){let id=kind==='card'?'cardFields':kind==='llm'?'llmFields':'monitorFields';let updates={};document.querySelectorAll('#'+id+' input').forEach(x=>{if(x.dataset.secret&&x.value){updates[x.dataset.key]=x.value}else if(!x.dataset.secret&&x.value&&x.value!=='********'){updates[x.dataset.key]=x.value}});try{await req('/api/env/'+kind,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({updates})});alert('配置已保存');refresh()}catch(e){alert(e.message)}}
async function restart(name){try{await req('/api/service/'+encodeURIComponent(name)+'/restart',{method:'POST'});alert('已重启 '+name);refresh()}catch(e){alert(e.message)}}
async function loadLlmPrompt(){try{let j=await req('/api/llm-prompt');document.querySelector('#llmPrompt').value=j.prompt||''}catch(e){}}
async function saveLlmPrompt(){let prompt=document.querySelector('#llmPrompt').value;try{await req('/api/llm-prompt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt})});alert('提示词已保存')}catch(e){alert(e.message)}}
async function resetLlmPrompt(){if(!confirm('确定恢复默认提示词？'))return;try{await req('/api/llm-prompt/reset',{method:'POST'});alert('已恢复默认');await loadLlmPrompt()}catch(e){alert(e.message)}}
async function logs(){try{let j=await req('/api/logs?name='+encodeURIComponent(document.querySelector('#logService').value));document.querySelector('#log').textContent=j.log||'(暂无日志)'}catch(e){document.querySelector('#log').textContent=e.message}}
async function retryQueue(){try{let j=await req('/api/retry-queue');document.querySelector('#queue').textContent=JSON.stringify(j.queue,null,2)}catch(e){document.querySelector('#queue').textContent=e.message}}
function bytes(n){n=Number(n||0);if(n<1024)return n+' B';let u=['KB','MB','GB','TB'],i=-1;do{n/=1024;i++}while(n>=1024&&i<u.length-1);return n.toFixed(2)+' '+u[i]}
async function mountMover(){try{let j=await req('/api/mount-mover');let s=j.service||{};document.querySelector('#moverBadge').textContent='状态：'+(s.status||'unknown');document.querySelector('#moverSource').textContent=j.source;document.querySelector('#moverTarget').textContent=j.target;let c=j.counts||{};document.querySelector('#moverStats').innerHTML=[['已跟踪',j.tracked||0],['基线',c.baseline||0],['等待稳定',c.waiting||0],['处理中',c.moving||0],['已移动',c.completed||0]].map(x=>'<div class="mover-stat"><strong>'+x[1]+'</strong><span>'+x[0]+'</span></div>').join('');let files=j.files||[];document.querySelector('#moverFiles').textContent=files.length?files.map(x=>x.status+' | '+bytes(x.size)+' | '+x.path).join('\n'):'暂无转存记录';let counts=j.pipeline_counts||{};let jobs=j.pipeline_jobs||[];document.querySelector('#moverPipeline').textContent='待处理: '+(counts.pending||0)+' | 处理中: '+(counts.processing||0)+' | 失败重试: '+(counts.failed||0)+' | 已发布: '+(counts.done||0)+'\n'+(jobs.length?jobs.map(x=>x.status+' | '+(x.title||x.path)+(x.share_link?' | '+x.share_link:'')+(x.error?' | '+x.error:'')).join('\n'):'暂无闭环任务')}catch(e){document.querySelector('#moverFiles').textContent=e.message;document.querySelector('#moverPipeline').textContent=e.message}}
async function act(kind){try{let j=await req('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:kind})});alert(j.message||'操作完成');refresh()}catch(e){alert(e.message)}}
req('/api/me').then(()=>{showApp();refresh()}).catch(()=>{showLogin()});
setInterval(()=>{if(!document.querySelector('#app').classList.contains('hidden'))refresh()},15000)
</script></body></html>'''


async def index(request: web.Request):
    # 统一使用 LOGIN_PAGE（新版UI），通过 JavaScript 动态加载数据
    response = web.Response(text=LOGIN_PAGE, content_type="text/html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


async def login(request: web.Request):
    if not ADMIN_PASSWORD or ADMIN_PASSWORD == "change-this-password":
        return web.json_response({"ok": False, "error": "后台 ADMIN_PASSWORD 尚未设置安全密码"}, status=503)
    if request.content_type == "application/x-www-form-urlencoded":
        data = await request.post()
        password = str(data.get("password", ""))
    else:
        password = str(json_body(request).get("password", ""))
    if not hmac.compare_digest(password, ADMIN_PASSWORD):
        if request.content_type == "application/x-www-form-urlencoded":
            return web.Response(text=LOGIN_PAGE, content_type="text/html", status=401)
        return web.json_response({"ok": False, "error": "密码错误"}, status=401)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = now() + 8 * 3600
    if request.content_type == "application/x-www-form-urlencoded":
        response = web.HTTPFound("/")
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", max_age=8 * 3600)
        raise response
    response = web.json_response({"ok": True})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="Strict", max_age=8 * 3600)
    return response


async def logout(request: web.Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    SESSIONS.pop(token, None)
    response = web.json_response({"ok": True})
    response.del_cookie(SESSION_COOKIE)
    return response


async def me(request: web.Request):
    require_auth(request)
    return web.json_response({"ok": True})


async def overview(request: web.Request):
    require_auth(request)
    return web.json_response({"services": [service_status(name) for name in MANAGED_SERVICES], "card_env": redact(parse_env(CARD_ENV)), "monitor_env": redact(parse_env(MONITOR_ENV)), "root_env": redact(parse_env(ROOT_ENV))})


async def mount_mover(request: web.Request):
    require_auth(request)
    return web.json_response(mount_mover_snapshot())


async def service_action(request: web.Request):
    require_auth(request)
    service = request.match_info["service"]
    action_name = request.match_info["action"]
    if service not in set(MANAGED_SERVICES):
        raise web.HTTPBadRequest(text="不允许操作此容器")
    if action_name not in {"start", "stop", "restart"}:
        raise web.HTTPBadRequest(text="未知服务操作")
    code, output = docker(action_name, service, timeout=30)
    return web.json_response({"ok": code == 0, "message": output[-500:]}, status=200 if code == 0 else 500)


async def update_env(request: web.Request):
    require_auth(request)
    kind = request.match_info["kind"]
    if request.content_type == "application/x-www-form-urlencoded":
        data = await request.post()
        updates = dict(data)
    else:
        updates = json_body(request).get("updates", {})
    if kind == "card":
        write_env(CARD_ENV, updates, EDITABLE_CARD_KEYS)
        docker("restart", "p115-card-bot")
    elif kind == "llm":
        write_env(CARD_ENV, updates, EDITABLE_CARD_KEYS)
        docker("restart", "p115-card-bot")
    elif kind == "monitor":
        write_env(MONITOR_ENV, updates, EDITABLE_MONITOR_KEYS)
        docker("restart", "tg-user-monitor")
    elif kind == "root":
        write_env(ROOT_ENV, updates, EDITABLE_ROOT_KEYS)
    else:
        raise web.HTTPNotFound(text="未知配置类型")
    if request.content_type == "application/x-www-form-urlencoded":
        raise web.HTTPFound("/?message=配置已保存并重启")
    return web.json_response({"ok": True})


async def restart(request: web.Request):
    require_auth(request)
    name = str(json_body(request).get("name", ""))
    if name not in set(MANAGED_SERVICES):
        raise web.HTTPBadRequest(text="不允许重启此容器")
    code, output = docker("restart", name, timeout=30)
    return web.json_response({"ok": code == 0, "message": output[-500:]}, status=200 if code == 0 else 500)


async def logs(request: web.Request):
    name = request.query.get("name", "tg-user-monitor")
    if name not in set(MANAGED_SERVICES):
        raise web.HTTPBadRequest(text="不允许查看此容器")
    code, output = docker("logs", "--tail", "300", name, timeout=30)
    return web.json_response({"ok": code == 0, "log": output[-50000:]})


async def retry_queue(request: web.Request):
    queue_path = PROJECT_DIR / "state" / "retry_queue.json"
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else []
    except Exception:
        queue = []
    return web.json_response({"ok": True, "queue": queue})


async def action(request: web.Request):
    require_auth(request)
    action_name = json_body(request).get("action", "")
    if action_name == "refresh":
        pass
    elif action_name == "backup":
        stamp = backup_config()
        if request.content_type == "application/x-www-form-urlencoded":
            raise web.HTTPFound("/?message=" + f"已备份到 {stamp}")
        return web.json_response({"ok": True, "message": f"已备份到 {stamp}"})
    elif action_name == "compose-up":
        results = [(name, *docker("start", name, timeout=30)) for name in MANAGED_SERVICES]
        ok = all(code == 0 for _, code, _output in results)
        message = "\n".join(f"{name}: {output}" for name, _code, output in results)
        return web.json_response({"ok": ok, "message": message})
    elif action_name == "compose-down":
        codes, outputs = [], []
        for name in MANAGED_SERVICES:
            code, output = docker("stop", name, timeout=30)
            codes.append(code)
            outputs.append(f"{name}: {output}")
        code, output = (0 if all(c == 0 for c in codes) else 1), "\n".join(outputs)
    elif action_name == "clear-retry":
        path = PROJECT_DIR / "state" / "retry_queue.json"
        if path.exists():
            path.write_text("[]\n", encoding="utf-8")
        if request.content_type == "application/x-www-form-urlencoded":
            raise web.HTTPFound("/?message=重试队列已清空")
        return web.json_response({"ok": True, "message": "重试队列已清空"})
    else:
        raise web.HTTPBadRequest(text="未知操作")
    if request.content_type == "application/x-www-form-urlencoded":
        raise web.HTTPFound("/?message=" + ("操作完成" if code == 0 else "操作失败"))
    return web.json_response({"ok": code == 0, "message": output[-1000:]}, status=200 if code == 0 else 500)


# ── LLM 提示词管理 ──
DEFAULT_LLM_PROMPT = """你是一个专业的媒体文件名解析器。

【核心规则】你必须严格只返回一个 JSON 对象，禁止返回任何解释、分析、推理过程、markdown 格式。
不要使用 ```json 代码块，不要添加任何前缀或后缀文本。

【你的任务】解析用户提供的影视文件名，提取以下字段：
- name: 影视作品的真实中文名称（如文件名是英文直译，需识别对应的中文原名）
- year: 发布年份（4位数字字符串）
- season: 季数（数字）
- episode: 集数信息（如 "E01-E12"）
- resolution: 分辨率（如 "2160p"）

【最重要·译名规则】name 必须是该作品在豆瓣 / TMDB 中文站真实使用的中文名。
- 只有当你能确认官方或通用的中文译名时，才返回中文名。
- 如果你**不能确认**中文译名，**必须原样返回文件名中的英文原名**（去掉点号、保留空格），
  严禁自行直译、意译或编造中文名！错误的译名会导致后续完全搜不到该作品，
  而保留英文原名至少能被正确检索到。
- 反面示例：文件名 "See.You.Later.Maybe.S01E01" 若你无法确认其中文译名，
  name 必须返回 "See You Later Maybe"，**绝不能**返回自行直译的 "后会有期"。
- 中文文件名的作品（如 "荒野求生系列(10部)"）请去掉"系列""合集""(10部)"等整理者后缀，
  返回干净的片名 "荒野求生"。

【关键能力】你拥有联网搜索能力，可以查询和验证不确定的影视作品信息。
当文件名中的标题看起来是英文直译、拼音、或可能有误写时，必须联网搜索确认真实中文片名。
绝不允许使用既有数据库的资源，以免导致在验证结果时出现无法验证确实存在但早于数据库时间的内容。

【重要区分】你必须区分电影和电视剧：
- 电视剧：文件名通常包含 S01/S02/E01/E02 等季集标记
- 电影：文件名通常不包含季集标记，可能包含 IMAX/DV/HDR 等标记
- 当同名作品有电影和电视剧版本时，根据文件名中的季集标记选择正确的版本

【输出格式】只返回这一个 JSON 对象，例如：
{"name":"脱口秀和Ta的朋友们","year":"2024","season":2,"episode":null,"resolution":"2160p"}

如果某个字段无法确定，返回 null。

【速度要求】请尽快给出结论，不要展开长篇推理，直接输出最终的 JSON 对象。"""


def load_llm_prompt() -> str:
    if LLM_PROMPT_FILE.exists():
        try:
            data = json.loads(LLM_PROMPT_FILE.read_text(encoding="utf-8"))
            return data.get("prompt", DEFAULT_LLM_PROMPT)
        except Exception:
            pass
    return DEFAULT_LLM_PROMPT


def save_llm_prompt(prompt: str) -> None:
    LLM_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LLM_PROMPT_FILE.write_text(json.dumps({"prompt": prompt}, ensure_ascii=False, indent=2), encoding="utf-8")


async def get_llm_prompt(request: web.Request):
    require_auth(request)
    return web.json_response({"ok": True, "prompt": load_llm_prompt()})


async def update_llm_prompt(request: web.Request):
    require_auth(request)
    prompt = str(json_body(request).get("prompt", ""))
    if not prompt.strip():
        raise web.HTTPBadRequest(text="提示词不能为空")
    save_llm_prompt(prompt)
    return web.json_response({"ok": True, "message": "提示词已保存"})


async def reset_llm_prompt(request: web.Request):
    require_auth(request)
    save_llm_prompt(DEFAULT_LLM_PROMPT)
    return web.json_response({"ok": True, "message": "已恢复默认提示词"})


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[body_middleware])
    app.add_routes([
        web.get("/", index), web.post("/api/login", login), web.post("/api/logout", logout), web.get("/api/me", me),
        web.get("/api/overview", overview), web.post("/api/env/{kind}", update_env), web.post("/api/restart", restart),
        web.post("/api/service/{service}/{action}", service_action),
        web.get("/api/logs", logs), web.get("/api/retry-queue", retry_queue), web.get("/api/mount-mover", mount_mover), web.post("/api/action", action),
        web.get("/api/llm-prompt", get_llm_prompt), web.post("/api/llm-prompt", update_llm_prompt),
        web.post("/api/llm-prompt/reset", reset_llm_prompt),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"card-admin listening on {HOST}:{PORT}", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
