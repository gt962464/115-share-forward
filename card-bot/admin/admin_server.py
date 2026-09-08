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
import aiohttp

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
MOVER_ENABLED = os.getenv("MOVER_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
MOVER_STATE_FILE = PROJECT_DIR / "cd2-mount-mover" / "state" / "state.json"
MOVER_PIPELINE_DIR = PROJECT_DIR / "state" / "mount-pipeline-queue"
MOVER_SOURCE_DIR = os.getenv("MOVER_SOURCE_DIR", "/vol2/1000/转存")
MOVER_TARGET_DIR = os.getenv("MOVER_TARGET_DIR", "/vol2/1000/CloudDrive/自动转存")
MANAGED_SERVICES = ["p115-card-bot", "tg-user-monitor"] + ([MOVER_CONTAINER] if MOVER_ENABLED else [])
BACKUP_DIR = DATA_DIR / "backups"
SESSION_COOKIE = "card_admin_session"
SESSIONS: dict[str, float] = {}
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_KEYS = {"P115_COOKIE", "TG_API_HASH", "TG_BOT_TOKEN", "RECYCLE_PASSWORD", "ADMIN_PASSWORD", "ADMIN_SECRET"}
EDITABLE_CARD_KEYS = ["P115_COOKIE", "TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_USER_ID", "TG_ALLOW_CHATS", "TG_SUBMITTER_IDS", "TMDB_API_KEY", "TMDB_LANG", "TG_PROXY", "APP_HTTP_PROXY", "LOG_LEVEL", "CARD_BOT_TEST_MODE", "P115_SAVE_DIR", "RECYCLE_PASSWORD", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"]
LLM_CARD_KEYS = ["LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"]
LLM_PROMPT_FILE = DATA_DIR / "llm_prompt.json"
EDITABLE_MONITOR_KEYS = ["TG_API_ID", "TG_API_HASH", "TG_BOT_TOKEN", "TG_PHONE", "TG_SOURCE_CHAT", "TG_FORWARD_TO", "TG_PROXY", "TG_START_FROM", "TG_FORWARD_TIMEOUT", "TG_MONITOR_WEB_PORT", "TG_MONITOR_WEB_SECRET", "LOG_LEVEL"]
EDITABLE_ROOT_KEYS = ["P115_COOKIE", "TG_BOT_TOKEN", "TG_CHANNEL_ID", "TG_USER_ID", "TG_ALLOW_CHATS", "TMDB_API_KEY", "TMDB_LANG", "LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL", "TG_PROXY", "APP_HTTP_PROXY", "P115_SAVE_DIR", "LOG_LEVEL", "AUTO_PROCESS_115_LINKS", "AUTO_DELETE_AFTER", "RECYCLE_PASSWORD", "ADMIN_PASSWORD", "ADMIN_SECRET"]


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


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(
            status=204,
            headers={
                "Access-Control-Allow-Origin": request.headers.get("Origin", "*"),
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Max-Age": "86400",
            },
        )
    response = await handler(request)
    origin = request.headers.get("Origin")
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


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
        # Auto-create .env from .env.example template if available
        example = path.parent / ".env.example"
        if example.exists():
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
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
    def request_bytes(self, method: str, path: str, body: bytes = b"", timeout: int = 30) -> tuple[int, bytes]:
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
        return status, payload

    def request(self, method: str, path: str, body: bytes = b"", timeout: int = 30) -> tuple[int, str]:
        status, payload = self.request_bytes(method, path, body, timeout)
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


def demux_docker_log(payload: bytes) -> str:
    """Demux Docker multiplexed stream frames (8-byte header per frame) and clean control characters."""
    if not payload:
        return ""
    output = []
    offset = 0
    n = len(payload)
    while offset < n:
        if offset + 8 <= n and payload[offset] in (0, 1, 2) and payload[offset+1:offset+4] == b"\x00\x00\x00":
            size = int.from_bytes(payload[offset+4:offset+8], byteorder="big")
            frame_end = offset + 8 + size
            if frame_end <= n:
                output.append(payload[offset+8:frame_end])
                offset = frame_end
                continue
            else:
                output.append(payload[offset+8:])
                break
        else:
            next_hdr = -1
            for candidate in (b"\x01\x00\x00\x00", b"\x02\x00\x00\x00"):
                pos = payload.find(candidate, offset + 1)
                if pos != -1 and (next_hdr == -1 or pos < next_hdr):
                    next_hdr = pos
            if next_hdr != -1:
                output.append(payload[offset:next_hdr])
                offset = next_hdr
            else:
                output.append(payload[offset:])
                break
    text = b"".join(output).decode("utf-8", "replace")
    clean_chars = [ch for ch in text if ord(ch) in (9, 10, 13) or ord(ch) >= 32]
    return "".join(clean_chars)


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
        status, payload = DOCKER.request_bytes("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail=300", timeout=timeout)
        log_text = demux_docker_log(payload)
        return (0 if status < 300 else 1), log_text
    return 1, f"unsupported docker operation: {args}"


def service_status(name: str) -> dict[str, str]:
    code, output = docker("inspect", "-f", "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}", name)
    if code != 0:
        return {"name": name, "status": "not_found", "detail": output[-300:]}
    status, started, restart = (output.split("|", 2) + ["", ""])[:3]
    return {"name": name, "status": status, "started": started, "restart_count": restart}


def mount_mover_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "enabled": MOVER_ENABLED,
        "service": service_status(MOVER_CONTAINER) if MOVER_ENABLED else {"name": MOVER_CONTAINER, "status": "disabled"},
        "source": MOVER_SOURCE_DIR if MOVER_ENABLED else "",
        "target": MOVER_TARGET_DIR if MOVER_ENABLED else "",
        "baseline_done": False,
        "tracked": 0,
        "counts": {"baseline": 0, "waiting": 0, "moving": 0, "completed": 0},
        "files": [],
        "pipeline_counts": {"pending": 0, "processing": 0, "failed": 0, "done": 0},
        "pipeline_jobs": [],
    }
    if not MOVER_ENABLED:
        return snapshot
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


LOGIN_PAGE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>115 分享转发 · 管理后台</title>
<style>
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-hover: #f1f5f9;
  --surface-active: #e2e8f0;
  --border: #e2e8f0;
  --border-subtle: #f1f5f9;
  --border-focus: #2563eb;
  --text: #0f172a;
  --text-secondary: #475569;
  --text-muted: #94a3b8;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-light: #eff6ff;
  --primary-border: #bfdbfe;
  --success: #16a34a;
  --success-bg: #f0fdf4;
  --success-border: #bbf7d0;
  --danger: #dc2626;
  --danger-bg: #fef2f2;
  --danger-border: #fecaca;
  --warning: #d97706;
  --warning-bg: #fffbeb;
  --warning-border: #fde68a;
  --radius-sm: 6px;
  --radius: 8px;
  --radius-lg: 12px;
  --shadow-sm: 0 1px 2px 0 rgba(0,0,0,0.05);
  --shadow: 0 1px 3px 0 rgba(0,0,0,0.08), 0 1px 2px -1px rgba(0,0,0,0.04);
  --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.07), 0 2px 4px -2px rgba(0,0,0,0.04);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-sans);
  font-size: 13.5px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}
button, input, select, textarea { font-family: inherit; }

/* ── Login Page ── */
#login {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
}
.login-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 36px 32px;
  width: 100%;
  max-width: 380px;
  box-shadow: var(--shadow-md);
}
.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
}
.login-logo {
  width: 40px;
  height: 40px;
  background: var(--primary);
  color: #fff;
  font-weight: 700;
  font-size: 16px;
  border-radius: var(--radius);
  display: flex;
  align-items: center;
  justify-content: center;
  letter-spacing: -0.5px;
}
.login-brand h1 {
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.3;
}
.login-brand p {
  font-size: 12px;
  color: var(--text-muted);
}
.login-group {
  margin-bottom: 14px;
}
.login-group label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
  margin-bottom: 5px;
}
.login-input-wrap {
  position: relative;
  display: flex;
  align-items: center;
}
.login-input-wrap input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 13.5px;
  outline: none;
  background: #fff;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.login-input-wrap input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(37,99,235,0.12);
}
.login-btn {
  width: 100%;
  padding: 10px;
  background: var(--primary);
  color: #fff;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  margin-top: 18px;
  transition: background 0.15s;
}
.login-btn:hover { background: var(--primary-hover); }
.login-msg {
  color: var(--danger);
  font-size: 12px;
  margin-top: 10px;
  min-height: 18px;
  text-align: center;
}

/* ── App Layout ── */
#app { display: none; min-height: 100vh; }
.header {
  background: rgba(255,255,255,0.95);
  border-bottom: 1px solid var(--border);
  height: 52px;
  padding: 0 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(8px);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-logo {
  width: 28px;
  height: 28px;
  background: var(--primary);
  color: #fff;
  font-weight: 700;
  font-size: 13px;
  border-radius: var(--radius-sm);
  display: flex;
  align-items: center;
  justify-content: center;
}
.header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.header-badge {
  font-size: 11px;
  font-weight: 500;
  padding: 2px 7px;
  border-radius: 4px;
  background: var(--surface-hover);
  color: var(--text-secondary);
  border: 1px solid var(--border);
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.conn-pill {
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 20px;
  background: var(--success-bg);
  color: var(--success);
  border: 1px solid var(--success-border);
  font-weight: 500;
}
.conn-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}
.btn-icon {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 12px;
  font-size: 12.5px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}
.btn-icon:hover {
  background: var(--surface-hover);
  color: var(--text);
  border-color: var(--border-hover, #cbd5e1);
}
.btn-icon.danger:hover {
  background: var(--danger-bg);
  color: var(--danger);
  border-color: var(--danger-border);
}

/* ── Main Container & Navigation Tabs ── */
.main {
  max-width: 1280px;
  margin: 0 auto;
  padding: 20px 24px;
}
.nav-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 20px;
  overflow-x: auto;
}
.nav-tab {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  font-size: 13.5px;
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, border-color 0.15s;
  white-space: nowrap;
}
.nav-tab:hover { color: var(--text); }
.nav-tab.active {
  color: var(--primary);
  border-bottom-color: var(--primary);
  font-weight: 600;
}
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* ── Cards & Grid ── */
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.grid-3 {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 14px;
  margin-bottom: 16px;
}
@media(max-width: 900px) {
  .grid-2 { grid-template-columns: 1fr; }
  .grid-3 { grid-template-columns: 1fr; }
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow-sm);
  margin-bottom: 16px;
}
.card:last-child { margin-bottom: 0; }
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.card-title {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.card-desc {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: -10px;
  margin-bottom: 16px;
}

/* ── Stats Tile ── */
.stat-tile {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 18px;
  box-shadow: var(--shadow-sm);
}
.stat-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}
.stat-value {
  font-size: 20px;
  font-weight: 700;
  color: var(--text);
  margin-top: 4px;
}
.stat-sub {
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 2px;
}

/* ── Services List ── */
.svc-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: #fafbfc;
  margin-bottom: 10px;
  transition: all 0.15s;
}
.svc-item:last-child { margin-bottom: 0; }
.svc-item:hover {
  background: #fff;
  border-color: var(--border);
}
.svc-left {
  display: flex;
  align-items: center;
  gap: 10px;
}
.svc-name {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--text);
}
.svc-meta {
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 2px;
}
.svc-right {
  display: flex;
  align-items: center;
  gap: 10px;
}
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 20px;
  text-transform: capitalize;
}
.badge.running {
  background: var(--success-bg);
  color: var(--success);
  border: 1px solid var(--success-border);
}
.badge.stopped {
  background: var(--danger-bg);
  color: var(--danger);
  border: 1px solid var(--danger-border);
}
.badge.not_found {
  background: var(--warning-bg);
  color: var(--warning);
  border: 1px solid var(--warning-border);
}
.badge.unknown {
  background: var(--surface-hover);
  color: var(--text-muted);
  border: 1px solid var(--border);
}

/* ── Action Buttons ── */
.btn-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 14px;
}
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  font-size: 12.5px;
  font-weight: 500;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.btn:active { transform: scale(0.99); }
.btn-primary {
  background: var(--primary);
  color: #fff;
}
.btn-primary:hover { background: var(--primary-hover); }
.btn-outline {
  background: #fff;
  border-color: var(--border);
  color: var(--text-secondary);
}
.btn-outline:hover {
  background: var(--surface-hover);
  color: var(--text);
  border-color: var(--border-hover, #cbd5e1);
}
.btn-danger-outline {
  background: #fff;
  border-color: var(--danger-border);
  color: var(--danger);
}
.btn-danger-outline:hover {
  background: var(--danger-bg);
}
.btn-sm {
  padding: 4px 9px;
  font-size: 11.5px;
}

/* ── Form Fields & Config ── */
.subtabs {
  display: flex;
  gap: 6px;
  background: var(--surface-hover);
  padding: 3px;
  border-radius: var(--radius-sm);
  margin-bottom: 18px;
  width: fit-content;
}
.subtab {
  padding: 6px 14px;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text-secondary);
  border-radius: 4px;
  cursor: pointer;
  transition: all 0.15s;
}
.subtab:hover { color: var(--text); }
.subtab.active {
  background: #fff;
  color: var(--text);
  box-shadow: 0 1px 2px rgba(0,0,0,0.06);
  font-weight: 600;
}
.subpane { display: none; }
.subpane.active { display: block; }

.fields-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px 20px;
  width: 100%;
  box-sizing: border-box;
}
@media(max-width: 860px) {
  .fields-grid { grid-template-columns: minmax(0, 1fr); }
}
.field-item {
  display: flex;
  flex-direction: column;
  min-width: 0;
  box-sizing: border-box;
}
.field-label-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
  gap: 8px;
  min-width: 0;
}
.field-label {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.field-key {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-muted);
  flex-shrink: 0;
}
.field-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-width: 0;
}
.field-input-wrap {
  position: relative;
  display: flex;
  align-items: center;
  flex: 1;
  min-width: 0;
  width: 100%;
}
.field-input-wrap input {
  width: 100%;
  min-width: 0;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 13px;
  outline: none;
  background: #fff;
  color: var(--text);
  transition: border-color 0.15s, box-shadow 0.15s;
  box-sizing: border-box;
}
.field-input-wrap input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(37,99,235,0.12);
}
.field-input-wrap.is-secret input {
  padding-right: 36px;
  font-family: var(--font-mono);
}
.field-eye-btn {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  background: transparent;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
  color: var(--text-muted);
  font-size: 13px;
  line-height: 1;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2;
}
.field-eye-btn:hover { color: var(--text); }
.btn-test {
  flex-shrink: 0;
  padding: 0 12px;
  height: 35px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: #fff;
  color: var(--primary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
}
.btn-test:hover {
  background: var(--primary-light);
  border-color: var(--primary-border);
}
.btn-test:active { transform: scale(0.98); }
.btn-test:disabled { opacity: 0.6; cursor: not-allowed; }

/* ── LLM Prompt Editor ── */
.prompt-box {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.prompt-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.prompt-len {
  font-size: 12px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
.prompt-textarea {
  width: 100%;
  min-height: 380px;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-family: var(--font-mono);
  font-size: 12.5px;
  line-height: 1.65;
  background: #fafbfc;
  color: #1e293b;
  outline: none;
  resize: vertical;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.prompt-textarea:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px rgba(37,99,235,0.12);
  background: #fff;
}

/* ── Realtime Logs Terminal ── */
.log-card {
  display: flex;
  flex-direction: column;
}
.log-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
}
.log-toolbar-left, .log-toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.log-select {
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  outline: none;
  background: #fff;
  color: var(--text);
  cursor: pointer;
}
.log-select:focus { border-color: var(--primary); }
.log-live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--success);
  display: inline-block;
  animation: pulse 2s infinite;
}
@keyframes pulse {
  0% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.3; transform: scale(0.85); }
  100% { opacity: 1; transform: scale(1); }
}
.log-live-dot.paused {
  background: var(--text-muted);
  animation: none;
}
.terminal-window {
  background: #0d1117;
  border: 1px solid #21262d;
  border-radius: var(--radius-sm);
  padding: 14px 16px;
  height: 520px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  color: #c9d1d9;
  white-space: pre-wrap;
  word-break: break-all;
  position: relative;
}
.terminal-window::-webkit-scrollbar { width: 8px; height: 8px; }
.terminal-window::-webkit-scrollbar-track { background: #0d1117; }
.terminal-window::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
.terminal-window::-webkit-scrollbar-thumb:hover { background: #484f58; }

/* ── Mount Mover & Queue ── */
.mover-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}
@media(max-width: 700px) {
  .mover-grid { grid-template-columns: repeat(2, 1fr); }
}
.mover-stat {
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 10px;
  text-align: center;
  background: #fafbfc;
}
.mover-stat-num {
  font-size: 18px;
  font-weight: 700;
  color: var(--text);
}
.mover-stat-lbl {
  font-size: 11.5px;
  color: var(--text-muted);
  margin-top: 2px;
}
.code-block {
  background: #0d1117;
  color: #c9d1d9;
  border: 1px solid #21262d;
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.55;
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
}

/* ── Toast ── */
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}
.toast-item {
  background: #0f172a;
  color: #fff;
  padding: 10px 18px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  display: flex;
  align-items: center;
  gap: 8px;
  opacity: 0;
  transform: translateY(10px);
  transition: all 0.2s ease;
  pointer-events: auto;
}
.toast-item.show { opacity: 1; transform: translateY(0); }
.toast-item.ok { background: #15803d; }
.toast-item.err { background: #b91c1c; }
.toast-item.info { background: #1d4ed8; }
</style>
</head>
<body>

<!-- Login Screen -->
<div id="login">
  <div class="login-card">
    <div class="login-brand">
      <div class="login-logo">115</div>
      <div>
        <h1>115 分享转发</h1>
        <p>轻量管理后台 · 局域网控制面板</p>
      </div>
    </div>
    <form id="loginForm" onsubmit="event.preventDefault(); doLogin();">
      <div class="login-group" id="urlGroup" style="display:none">
        <label>后端 API 地址</label>
        <div class="login-input-wrap" style="display:flex;gap:8px;align-items:center">
          <input id="login-url" type="text" placeholder="http://127.0.0.1:18810" style="flex:1">
          <button type="button" class="btn btn-outline btn-sm" id="testConnBtn" onclick="testConnection()">测试连接</button>
        </div>
        <div id="testConnMsg" style="font-size:12px;margin-top:6px"></div>
      </div>
      <div class="login-group">
        <label>管理员访问密码</label>
        <div class="login-input-wrap">
          <input id="login-pwd" type="password" placeholder="请输入管理密码" autofocus autocomplete="current-password">
        </div>
      </div>
      <div class="login-msg" id="loginMsg"></div>
      <button class="login-btn" id="loginBtn" type="submit">登 录</button>
    </form>
  </div>
</div>

<!-- Main App Screen -->
<div id="app">
  <header class="header">
    <div class="header-left">
      <div class="header-logo">115</div>
      <span class="header-title">115 分享转发 · 管理后台</span>
      <span class="header-badge">v2.0 极简版</span>
    </div>
    <div class="header-right">
      <span class="conn-pill" id="connPill"><span class="conn-dot"></span><span id="connText">已连接</span></span>
      <button class="btn-icon" onclick="refreshOverview(true)" title="刷新页面数据">🔄 刷新</button>
      <button class="btn-icon danger" onclick="doLogout()" title="退出当前登录">退出</button>
    </div>
  </header>

  <main class="main">
    <!-- Top Navigation Tabs -->
    <nav class="nav-tabs">
      <div class="nav-tab active" onclick="switchNavTab('overview')">📊 服务概览</div>
      <div class="nav-tab" onclick="switchNavTab('config')">⚙️ 配置管理</div>
      <div class="nav-tab" onclick="switchNavTab('llm')">🤖 LLM 提示词</div>
      <div class="nav-tab" onclick="switchNavTab('logs')">📜 实时日志</div>
      <div class="nav-tab" onclick="switchNavTab('queue')">🔁 队列与转存</div>
    </nav>

    <!-- Tab 1: Overview -->
    <section id="pane-overview" class="tab-pane active">
      <div class="grid-3">
        <div class="stat-tile">
          <div class="stat-label">核心容器</div>
          <div class="stat-value" id="stat-containers">- / -</div>
          <div class="stat-sub" id="stat-containers-sub">检查中...</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">重试队列</div>
          <div class="stat-value" id="stat-queue">-</div>
          <div class="stat-sub">待重试错误链接数</div>
        </div>
        <div class="stat-tile">
          <div class="stat-label">挂载转存</div>
          <div class="stat-value" id="stat-mover" style="font-size:16px;font-weight:600">-</div>
          <div class="stat-sub" id="stat-mover-sub">本地目录移动流水线</div>
        </div>
      </div>

      <div class="grid-2">
        <div class="card">
          <div class="card-header">
            <h2 class="card-title">🖥️ 核心服务状态</h2>
            <button class="btn btn-outline btn-sm" onclick="loadOverview()">刷新</button>
          </div>
          <div id="services-list"><div style="color:var(--text-muted);text-align:center;padding:24px">加载中...</div></div>
        </div>

        <div class="card">
          <div class="card-header">
            <h2 class="card-title">⚡ 常用快捷操作</h2>
          </div>
          <div class="btn-group" style="margin-top:0">
            <button class="btn btn-outline" onclick="doAction('backup')">📦 备份当前配置</button>
            <button class="btn btn-outline" onclick="doAction('clear-retry')">🗑️ 清空重试队列</button>
            <button class="btn btn-outline" onclick="doAction('compose-up')">🚀 启动全部服务</button>
            <button class="btn btn-danger-outline" onclick="doAction('compose-down')">⏹️ 停止全部服务</button>
          </div>
          <div style="margin-top:16px;padding:12px;border:1px solid var(--border-subtle);border-radius:var(--radius-sm);background:#fafbfc">
            <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:4px">💡 温馨提示</div>
            <p style="font-size:12px;color:var(--text-secondary);line-height:1.6">
              • 修改配置后点击“保存并重启”，容器将自动重启加载新配置。<br>
              • 敏感字段如 Cookie、Token 在后台已脱敏保护，输入框留空保存将保持原值不变。
            </p>
          </div>
        </div>
      </div>
    </section>

    <!-- Tab 2: Configuration -->
    <section id="pane-config" class="tab-pane">
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">⚙️ 服务配置中心</h2>
          <span style="font-size:12px;color:var(--text-muted)">带 * 号项为必填核心凭据</span>
        </div>
        <div class="subtabs">
          <div class="subtab active" onclick="switchConfigSubtab('card')">卡片机器人 (cardbot)</div>
          <div class="subtab" onclick="switchConfigSubtab('monitor')">Telegram 监听器 (monitor)</div>
          <div class="subtab" onclick="switchConfigSubtab('root')">全局与环境变量 (root .env)</div>
        </div>

        <div id="subpane-card" class="subpane active">
          <div class="fields-grid" id="fields-card"></div>
          <div class="btn-group">
            <button class="btn btn-primary" onclick="saveConfig('card')">💾 保存卡片机器人配置并重启</button>
          </div>
        </div>

        <div id="subpane-monitor" class="subpane">
          <div class="fields-grid" id="fields-monitor"></div>
          <div class="btn-group">
            <button class="btn btn-primary" onclick="saveConfig('monitor')">💾 保存 Telegram 监听配置并重启</button>
          </div>
        </div>

        <div id="subpane-root" class="subpane">
          <div class="fields-grid" id="fields-root"></div>
          <div class="btn-group">
            <button class="btn btn-primary" onclick="saveConfig('root')">💾 保存全局环境变量</button>
          </div>
        </div>
      </div>
    </section>

    <!-- Tab 3: LLM Prompt -->
    <section id="pane-llm" class="tab-pane">
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">🤖 LLM 影视名识别参数</h2>
          <button class="btn btn-primary btn-sm" onclick="saveConfig('llm')">保存 LLM 配置</button>
        </div>
        <div class="fields-grid" id="fields-llm" style="margin-bottom:14px"></div>
      </div>

      <div class="card">
        <div class="prompt-box">
          <div class="prompt-header">
            <div>
              <h2 class="card-title">📝 识别 System Prompt 提示词</h2>
              <div style="font-size:12px;color:var(--text-muted);margin-top:2px">用于引导大模型提取规范中文名、年份、季集及分辨率</div>
            </div>
            <div style="display:flex;align-items:center;gap:10px">
              <span class="prompt-len" id="promptLen">0 字</span>
              <button class="btn btn-outline btn-sm" onclick="resetLlmPrompt()">↩ 恢复默认</button>
              <button class="btn btn-primary btn-sm" onclick="saveLlmPrompt()">💾 保存提示词</button>
            </div>
          </div>
          <textarea class="prompt-textarea" id="llmPrompt" placeholder="正在加载提示词..."></textarea>
        </div>
      </div>
    </section>

    <!-- Tab 4: Live Logs -->
    <section id="pane-logs" class="tab-pane">
      <div class="card log-card">
        <div class="log-toolbar">
          <div class="log-toolbar-left">
            <span style="font-weight:600;font-size:13px">服务选择:</span>
            <select class="log-select" id="logServiceSelect" onchange="loadLogs(true)">
              <option value="p115-card-bot">p115-card-bot (卡片机器人)</option>
              <option value="tg-user-monitor">tg-user-monitor (TG 监听器)</option>
              <option value="cd2-mount-mover">cd2-mount-mover (挂载转存)</option>
            </select>
            <select class="log-select" id="logIntervalSelect" onchange="changeLogInterval()">
              <option value="0">自动刷新: 关</option>
              <option value="3000">自动刷新: 3秒</option>
              <option value="5000" selected>自动刷新: 5秒</option>
              <option value="15000">自动刷新: 15秒</option>
            </select>
            <span class="log-live-dot" id="logLiveDot" title="正在实时拉取"></span>
          </div>
          <div class="log-toolbar-right">
            <span style="font-size:11.5px;color:var(--text-muted);font-family:var(--font-mono)" id="logStatusText">共 0 行</span>
            <button class="btn btn-outline btn-sm" onclick="loadLogs(true)">🔄 刷新</button>
            <button class="btn btn-outline btn-sm" onclick="copyLogs()">📋 复制</button>
            <button class="btn btn-outline btn-sm" onclick="clearLogsView()">🧹 清屏</button>
            <button class="btn btn-outline btn-sm" onclick="scrollLogToBottom()">⬇️ 滚到底</button>
          </div>
        </div>
        <div class="terminal-window" id="terminalBox">正在加载日志...</div>
      </div>
    </section>

    <!-- Tab 5: Queue & Mover -->
    <section id="pane-queue" class="tab-pane">
      <div class="card">
        <div class="card-header">
          <h2 class="card-title">🔁 自动重试队列 (retry_queue.json)</h2>
          <div style="display:flex;gap:8px">
            <button class="btn btn-outline btn-sm" onclick="loadQueue()">刷新</button>
            <button class="btn btn-danger-outline btn-sm" onclick="doAction('clear-retry')">清空队列</button>
          </div>
        </div>
        <p class="card-desc">转存失败或未匹配到 TMDB 的链接将自动在此重试，默认指数退避重试。</p>
        <pre class="code-block" id="queueContent">加载中...</pre>
      </div>

      <div class="card">
        <div class="card-header">
          <h2 class="card-title">📂 本地挂载转存流水线 (cd2-mount-mover)</h2>
          <button class="btn btn-outline btn-sm" onclick="loadMover()">刷新状态</button>
        </div>
        <div id="moverContent">加载中...</div>
      </div>
    </section>
  </main>
</div>

<div class="toast-container" id="toastBox"></div>

<script>
// ── State & Config ──
let API_BASE = '';
let activeNav = 'overview';
let activeConfigSub = 'card';
let logTimer = null;
let logIsScrolledUp = false;
let editingConfig = false;
let currentEnvData = { card: {}, monitor: {}, root: {} };
let currentRawLogs = '';

const labelMaps = {
  P115_COOKIE: '115 账号 Cookie',
  TG_BOT_TOKEN: 'Telegram Bot Token',
  TG_CHANNEL_ID: '卡片发布频道 ID',
  TG_USER_ID: '管理员用户 ID',
  TG_ALLOW_CHATS: '允许触发的聊天/群组 ID',
  TG_SUBMITTER_IDS: '投稿白名单用户 ID',
  TMDB_API_KEY: 'TMDB API 密钥',
  TMDB_LANG: 'TMDB 语言偏好',
  TG_PROXY: 'Telegram 代理 (SOCKS5/HTTP)',
  APP_HTTP_PROXY: 'HTTP 代理 (下载海报/请求TMDB)',
  LOG_LEVEL: '日志级别 (INFO/DEBUG/WARN)',
  CARD_BOT_TEST_MODE: '测试模式 (0:发频道, 1:纯私聊, 2:查网盘私聊)',
  P115_SAVE_DIR: '115 保存目录名',
  RECYCLE_PASSWORD: '115 回收站永久清空密码',
  AUTO_PROCESS_115_LINKS: '自动识别 115 链接 (1:开启, 0:关闭)',
  AUTO_DELETE_AFTER: '发送后自动清理延迟秒数 (0:不清理)',
  ADMIN_PASSWORD: '管理后台密码',
  ADMIN_SECRET: '管理后台密钥',
  LLM_API_BASE: 'LLM API 地址',
  LLM_API_KEY: 'LLM API 密钥',
  LLM_MODEL: 'LLM 模型名称',
  TG_API_ID: 'Telegram API ID',
  TG_API_HASH: 'Telegram API Hash',
  TG_PHONE: 'TG 登录手机号',
  TG_SOURCE_CHAT: '监控来源聊天 ID / 频道',
  TG_FORWARD_TO: '转发目标聊天 ID',
  TG_START_FROM: '起始消息 ID',
  TG_FORWARD_TIMEOUT: '转发超时时间 (秒)',
  TG_MONITOR_WEB_PORT: '监听器网页端口',
  TG_MONITOR_WEB_SECRET: '监听器网页密钥'
};

const cardKeys = ['P115_COOKIE','TG_BOT_TOKEN','TG_CHANNEL_ID','TG_USER_ID','TG_ALLOW_CHATS','TG_SUBMITTER_IDS','TMDB_API_KEY','TMDB_LANG','TG_PROXY','APP_HTTP_PROXY','P115_SAVE_DIR','LOG_LEVEL','CARD_BOT_TEST_MODE','RECYCLE_PASSWORD'];
const llmKeys = ['LLM_API_BASE','LLM_API_KEY','LLM_MODEL'];
const monitorKeys = ['TG_API_ID','TG_API_HASH','TG_BOT_TOKEN','TG_PHONE','TG_SOURCE_CHAT','TG_FORWARD_TO','TG_PROXY','TG_START_FROM','TG_FORWARD_TIMEOUT','TG_MONITOR_WEB_PORT','TG_MONITOR_WEB_SECRET','LOG_LEVEL'];
const rootKeys = ['P115_COOKIE','TG_BOT_TOKEN','TG_CHANNEL_ID','TG_USER_ID','TG_ALLOW_CHATS','TMDB_API_KEY','TMDB_LANG','LLM_API_BASE','LLM_API_KEY','LLM_MODEL','TG_PROXY','APP_HTTP_PROXY','P115_SAVE_DIR','LOG_LEVEL','AUTO_PROCESS_115_LINKS','AUTO_DELETE_AFTER','RECYCLE_PASSWORD','ADMIN_PASSWORD','ADMIN_SECRET'];
const secretKeys = new Set(['P115_COOKIE','TG_BOT_TOKEN','TG_API_HASH','RECYCLE_PASSWORD','ADMIN_PASSWORD','ADMIN_SECRET','LLM_API_KEY']);

const testKindMap = {
  P115_COOKIE: 'p115',
  TG_BOT_TOKEN: 'telegram',
  TMDB_API_KEY: 'tmdb',
  LLM_API_BASE: 'llm',
  TG_PROXY: 'proxy',
  APP_HTTP_PROXY: 'proxy'
};

// ── Helpers ──
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toast(msg, type = 'ok') {
  const box = document.getElementById('toastBox');
  const item = document.createElement('div');
  item.className = 'toast-item ' + type;
  item.innerHTML = (type === 'ok' ? '✅ ' : type === 'err' ? '❌ ' : 'ℹ️ ') + esc(msg);
  box.appendChild(item);
  requestAnimationFrame(() => item.classList.add('show'));
  setTimeout(() => {
    item.classList.remove('show');
    setTimeout(() => item.remove(), 250);
  }, 3200);
}

async function req(path, opts = {}) {
  const url = API_BASE ? API_BASE.replace(/\/$/, '') + path : path;
  const res = await fetch(url, { credentials: 'include', ...opts });
  if (res.status === 401) {
    showLogin();
    throw new Error('未登录或会话已过期');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.message || '请求失败 (' + res.status + ')');
  return data;
}

// ── Tab Switching ──
function switchNavTab(tabId) {
  activeNav = tabId;
  document.querySelectorAll('.nav-tab').forEach((el, idx) => {
    const ids = ['overview', 'config', 'llm', 'logs', 'queue'];
    el.classList.toggle('active', ids[idx] === tabId);
  });
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  const target = document.getElementById('pane-' + tabId);
  if (target) target.classList.add('active');

  if (tabId === 'logs') {
    loadLogs(true);
  } else if (tabId === 'llm') {
    loadLlmPrompt();
  } else if (tabId === 'queue') {
    loadQueue();
    loadMover();
  }
}

function switchConfigSubtab(subId) {
  activeConfigSub = subId;
  const subs = ['card', 'monitor', 'root'];
  document.querySelectorAll('.subtab').forEach((el, idx) => {
    el.classList.toggle('active', subs[idx] === subId);
  });
  document.querySelectorAll('.subpane').forEach(el => el.classList.remove('active'));
  const target = document.getElementById('subpane-' + subId);
  if (target) target.classList.add('active');
}

// ── Auth ──
function showLogin() {
  document.getElementById('login').style.display = 'flex';
  document.getElementById('app').style.display = 'none';
  clearInterval(logTimer);
}

function showApp() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('app').style.display = 'block';
  changeLogInterval();
}

async function doLogin() {
  const btn = document.getElementById('loginBtn');
  const msg = document.getElementById('loginMsg');
  const pwdInput = document.getElementById('login-pwd');
  const urlInput = document.getElementById('login-url');

  if (urlInput && urlInput.value.trim()) {
    API_BASE = urlInput.value.trim().replace(/\/$/, '');
    localStorage.setItem('115_api_url', API_BASE);
  }

  const pwd = pwdInput.value;
  if (!pwd) {
    msg.textContent = '请输入管理员密码';
    return;
  }
  msg.textContent = '';
  btn.disabled = true;
  btn.textContent = '正在验证...';

  try {
    const d = await req('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd })
    });
    if (d.ok) {
      showApp();
      refreshOverview();
      toast('登录成功');
    } else {
      msg.textContent = d.error || '登录失败';
    }
  } catch (err) {
    msg.textContent = '登录失败: ' + err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '登 录';
  }
}

async function testConnection() {
  const urlInput = document.getElementById('login-url');
  const msg = document.getElementById('testConnMsg');
  const base = (urlInput && urlInput.value.trim()) || API_BASE;
  if (!base) {
    if (msg) { msg.textContent = '请先填写后端地址'; msg.style.color = '#ef4444'; }
    return;
  }
  const url = base.replace(/\/$/, '') + '/api/health';
  if (msg) { msg.textContent = '正在连接...'; msg.style.color = '#64748b'; }
  try {
    const res = await fetch(url, { method: 'GET' });
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok !== false) {
      if (msg) { msg.textContent = '✅ 连接成功（' + (data.service || 'card-admin') + '）'; msg.style.color = '#16a34a'; }
    } else {
      if (msg) { msg.textContent = '服务返回 HTTP ' + res.status; msg.style.color = '#ef4444'; }
    }
  } catch (e) {
    if (msg) { msg.textContent = '❌ 连接失败：' + e.message; msg.style.color = '#ef4444'; }
  }
}

async function doLogout() {
  try { await req('/api/logout', { method: 'POST' }); } catch (e) {}
  showLogin();
  toast('已退出登录', 'info');
}

// ── Overview & Services ──
async function refreshOverview(manual = false) {
  try {
    await loadOverview();
    if (manual) toast('数据已刷新');
  } catch (e) {
    toast(e.message, 'err');
  }
}

async function loadOverview() {
  const d = await req('/api/overview');
  renderServices(d.services || []);
  currentEnvData.card = d.card_env || {};
  currentEnvData.monitor = d.monitor_env || {};
  currentEnvData.root = d.root_env || {};

  // Render fields only if user is not actively editing
  if (!editingConfig) {
    renderFields('fields-card', currentEnvData.card, cardKeys);
    renderFields('fields-monitor', currentEnvData.monitor, monitorKeys);
    renderFields('fields-root', currentEnvData.root, rootKeys);
    renderFields('fields-llm', currentEnvData.card, llmKeys);
  }

  // Update stat tiles
  const services = d.services || [];
  const running = services.filter(s => s.status === 'running').length;
  document.getElementById('stat-containers').textContent = running + ' / ' + services.length;
  document.getElementById('stat-containers-sub').textContent = running === services.length ? '全部服务正常运行' : '存在未启动服务';

  // Check mover & queue softly
  loadQueueStat();
  loadMoverStat();
}

function renderServices(services) {
  const box = document.getElementById('services-list');
  if (!services.length) {
    box.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:16px">无可用服务</div>';
    return;
  }
  box.innerHTML = services.map(s => {
    const isRunning = s.status === 'running';
    return `
      <div class="svc-item">
        <div class="svc-left">
          <span class="badge ${esc(s.status)}">${esc(s.status)}</span>
          <div>
            <div class="svc-name">${esc(s.name)}</div>
            <div class="svc-meta">启动: ${esc(s.started || '-')} · 重启计数: ${esc(s.restart_count || '0')}</div>
          </div>
        </div>
        <div class="svc-right">
          <button class="btn btn-outline btn-sm" onclick="svcAction('${esc(s.name)}', 'restart')" title="重启服务">🔄 重启</button>
          ${isRunning 
            ? `<button class="btn btn-danger-outline btn-sm" onclick="svcAction('${esc(s.name)}', 'stop')" title="停止容器">⏹️ 停止</button>`
            : `<button class="btn btn-outline btn-sm" onclick="svcAction('${esc(s.name)}', 'start')" title="启动容器">▶️ 启动</button>`
          }
        </div>
      </div>
    `;
  }).join('');
}

async function svcAction(name, action) {
  try {
    toast(`正在${action === 'restart' ? '重启' : action === 'stop' ? '停止' : '启动'} ${name}...`, 'info');
    await req('/api/service/' + encodeURIComponent(name) + '/' + action, { method: 'POST' });
    toast(`${name} ${action} 完成`);
    setTimeout(loadOverview, 1500);
  } catch (e) {
    toast(`操作失败: ${e.message}`, 'err');
  }
}

// ── Fields & Form ──
function renderFields(containerId, envData, keys) {
  const box = document.getElementById(containerId);
  if (!box) return;
  box.innerHTML = keys.map(k => {
    const val = envData[k] || '';
    const isSecret = secretKeys.has(k) || val === '********';
    const label = labelMaps[k] || k;
    const reqMark = (k === 'P115_COOKIE' || k === 'TG_BOT_TOKEN' || k === 'TG_CHANNEL_ID') ? ' *' : '';
    const testKind = (typeof testKindMap !== 'undefined') ? testKindMap[k] : null;
    return `
      <div class="field-item">
        <div class="field-label-row">
          <label class="field-label" title="${esc(label)}">${esc(label)}${reqMark}</label>
          <span class="field-key">${esc(k)}</span>
        </div>
        <div class="field-input-row">
          <div class="field-input-wrap ${isSecret ? 'is-secret' : ''}">
            <input 
              data-key="${esc(k)}" 
              type="${isSecret ? 'password' : 'text'}" 
              value="${isSecret && val === '********' ? '' : esc(val)}"
              placeholder="${isSecret ? '保持现有不变 (留空不修改)' : '请输入 ' + esc(label)}"
              data-secret="${isSecret ? '1' : '0'}"
              onfocus="editingConfig = true"
              onblur="setTimeout(() => { if (!document.activeElement || document.activeElement.tagName !== 'INPUT') editingConfig = false; }, 300)"
            >
            ${isSecret ? `<button type="button" class="field-eye-btn" onclick="togglePass(this)" title="显示/隐藏密码">👁️</button>` : ''}
          </div>
          ${testKind ? `<button type="button" class="btn-test" onclick="testField('${testKind}', '${esc(k)}', this)" title="测试此项连接">测试</button>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function togglePass(btn) {
  const input = btn.parentElement.querySelector('input');
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = '🔒';
  } else {
    input.type = 'password';
    btn.textContent = '👁️';
  }
}

function inputVal(key) {
  const els = document.querySelectorAll('input[data-key="' + key + '"]');
  for (const el of els) {
    if (el.value && el.value.trim()) return el.value.trim();
  }
  return '';
}

async function testField(kind, key, btn) {
  const origText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  const payload = {};
  if (kind === 'p115') payload.cookie = inputVal(key);
  else if (kind === 'telegram') payload.token = inputVal(key);
  else if (kind === 'tmdb') payload.api_key = inputVal(key);
  else if (kind === 'llm') { payload.base = inputVal('LLM_API_BASE'); payload.key = inputVal('LLM_API_KEY'); payload.model = inputVal('LLM_MODEL'); }
  else if (kind === 'proxy') payload.proxy = inputVal(key);
  toast('正在测试连接...', 'info');
  try {
    const d = await req('/api/test/' + kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    toast(d.message || (d.ok ? '连接成功' : (d.error || '连接失败')), d.ok ? 'ok' : 'err');
  } catch (e) {
    toast('测试失败: ' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = origText; }
  }
}

async function saveConfig(kind) {
  const containerId = kind === 'card' ? 'fields-card' : kind === 'monitor' ? 'fields-monitor' : kind === 'llm' ? 'fields-llm' : 'fields-root';
  const inputs = document.querySelectorAll('#' + containerId + ' input');
  const updates = {};
  inputs.forEach(inp => {
    const key = inp.dataset.key;
    const val = inp.value.trim();
    if (inp.dataset.secret === '1') {
      if (val) updates[key] = val; // Only update if user typed new value
    } else {
      updates[key] = val;
    }
  });

  try {
    toast('正在保存配置...', 'info');
    await req('/api/env/' + (kind === 'llm' ? 'llm' : kind), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ updates })
    });
    toast('配置已成功保存！相关服务已自动重启生效');
    editingConfig = false;
    setTimeout(loadOverview, 2000);
  } catch (e) {
    toast('保存失败: ' + e.message, 'err');
  }
}

// ── LLM Prompt ──
async function loadLlmPrompt() {
  try {
    const d = await req('/api/llm-prompt');
    const area = document.getElementById('llmPrompt');
    area.value = d.prompt || '';
    updatePromptLen();
    area.oninput = updatePromptLen;
  } catch (e) {
    toast('加载提示词失败: ' + e.message, 'err');
  }
}

function updatePromptLen() {
  const area = document.getElementById('llmPrompt');
  document.getElementById('promptLen').textContent = (area.value || '').length + ' 字';
}

async function saveLlmPrompt() {
  const prompt = document.getElementById('llmPrompt').value;
  try {
    await req('/api/llm-prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt })
    });
    toast('LLM 提示词已保存生效');
  } catch (e) {
    toast('保存提示词失败: ' + e.message, 'err');
  }
}

async function resetLlmPrompt() {
  if (!confirm('确定要恢复默认的影视解析提示词吗？现有修改将被覆盖。')) return;
  try {
    await req('/api/llm-prompt/reset', { method: 'POST' });
    toast('已恢复为默认提示词');
    loadLlmPrompt();
  } catch (e) {
    toast('恢复失败: ' + e.message, 'err');
  }
}

// ── Clean Terminal & Logs (Crucial Display Fix) ──
function formatLogHtml(text) {
  if (!text) return '<span style="color:#8b949e">(当前无日志)</span>';
  const lines = text.split('\n');
  return lines.map(line => {
    const escaped = esc(line);
    if (!escaped.trim()) return '<div style="min-height:1.2em"></div>';
    if (/error|traceback|syntaxerror|exception|critical|fatal/i.test(line)) {
      return `<div style="color:#f87171;background:rgba(239,68,68,0.1);padding:1px 4px;border-radius:3px">${escaped}</div>`;
    } else if (/warn|warning/i.test(line)) {
      return `<div style="color:#fbbf24">${escaped}</div>`;
    } else if (/success|completed|完成|已连接|初始化完成/i.test(line)) {
      return `<div style="color:#4ade80">${escaped}</div>`;
    }
    return `<div>${escaped}</div>`;
  }).join('');
}

function sanitizeLogText(text) {
  if (!text) return '';
  // 1. Strip Docker 8-byte frame multiplex headers (pattern: \x01/\x02 followed by \x00\x00\x00)
  text = text.replace(/[\x00-\x02]\x00\x00\x00[\s\S]{4}/g, '');
  // 2. Strip non-printable ASCII control characters except \t, \n, \r
  text = text.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '');
  // 3. Strip ANSI escape sequences (e.g. \x1B[31m, \x1B[0m)
  text = text.replace(/\x1B\[[0-9;]*[a-zA-Z]/g, '');
  return text;
}

const terminalBox = document.getElementById('terminalBox');
terminalBox.addEventListener('scroll', () => {
  const threshold = 40;
  const atBottom = terminalBox.scrollHeight - terminalBox.scrollTop - terminalBox.clientHeight <= threshold;
  logIsScrolledUp = !atBottom;
});

async function loadLogs(forceScroll = false) {
  const svc = document.getElementById('logServiceSelect').value;
  const box = document.getElementById('terminalBox');
  try {
    const d = await req('/api/logs?name=' + encodeURIComponent(svc));
    let raw = d.log || '(当前无日志)';
    let cleaned = sanitizeLogText(raw);
    currentRawLogs = cleaned;
    
    // Count lines
    const lines = cleaned.split('\n');
    document.getElementById('logStatusText').textContent = '共 ' + lines.length + ' 行';
    
    box.innerHTML = formatLogHtml(cleaned);
    
    // Auto-scroll logic: only scroll to bottom if user hasn't scrolled up or force requested
    if (forceScroll || !logIsScrolledUp) {
      box.scrollTop = box.scrollHeight;
    }
  } catch (e) {
    box.textContent = '获取日志失败: ' + e.message;
  }
}

function clearLogsView() {
  document.getElementById('terminalBox').textContent = '(已清屏)';
  document.getElementById('logStatusText').textContent = '共 0 行';
}

function copyLogs() {
  const text = currentRawLogs || document.getElementById('terminalBox').textContent;
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    toast('日志已复制到剪贴板');
  }).catch(() => {
    toast('复制失败，请手动选取复制', 'err');
  });
}

function scrollLogToBottom() {
  const box = document.getElementById('terminalBox');
  box.scrollTop = box.scrollHeight;
  logIsScrolledUp = false;
}

function changeLogInterval() {
  clearInterval(logTimer);
  const ms = parseInt(document.getElementById('logIntervalSelect').value, 10);
  const dot = document.getElementById('logLiveDot');
  if (ms > 0) {
    dot.classList.remove('paused');
    dot.title = `每 ${ms / 1000} 秒自动刷新`;
    logTimer = setInterval(() => {
      if (activeNav === 'logs') loadLogs(false);
    }, ms);
  } else {
    dot.classList.add('paused');
    dot.title = '自动刷新已暂停';
  }
}

// ── Queue & Mover ──
async function loadQueue() {
  try {
    const d = await req('/api/retry-queue');
    const q = d.queue || [];
    document.getElementById('queueContent').textContent = q.length ? JSON.stringify(q, null, 2) : '[] (当前无待重试任务)';
  } catch (e) {
    document.getElementById('queueContent').textContent = '加载失败: ' + e.message;
  }
}

async function loadQueueStat() {
  try {
    const d = await req('/api/retry-queue');
    const q = d.queue || [];
    document.getElementById('stat-queue').textContent = q.length + ' 个';
  } catch (e) {
    document.getElementById('stat-queue').textContent = '-';
  }
}

async function loadMover() {
  const box = document.getElementById('moverContent');
  try {
    const d = await req('/api/mount-mover');
    if (d.enabled === false) {
      box.innerHTML = `
        <div style="padding:16px;border:1px solid var(--border-subtle);border-radius:var(--radius-sm);background:#fafbfc;color:var(--text-secondary)">
          <div style="font-weight:600;margin-bottom:4px;color:var(--text)">ℹ️ 挂载转存服务未启用</div>
          <p style="font-size:12.5px;color:var(--text-muted);line-height:1.6">
            此功能专为 NAS 本地 CD2 挂载路径（123盘 → 115）自动搬移设计。服务器部署环境已自动跳过。<br>
            如需开启，请在环境变量中设置 <code>MOVER_ENABLED=1</code>。
          </p>
        </div>
      `;
      return;
    }
    const c = d.counts || {};
    box.innerHTML = `
      <div class="mover-grid">
        <div class="mover-stat"><div class="mover-stat-num">${d.tracked || 0}</div><div class="mover-stat-lbl">已跟踪</div></div>
        <div class="mover-stat"><div class="mover-stat-num">${c.waiting || 0}</div><div class="mover-stat-lbl">等待稳定</div></div>
        <div class="mover-stat"><div class="mover-stat-num">${c.moving || 0}</div><div class="mover-stat-lbl">处理中</div></div>
        <div class="mover-stat"><div class="mover-stat-num">${c.completed || 0}</div><div class="mover-stat-lbl">已移动</div></div>
        <div class="mover-stat"><div class="mover-stat-num">${(d.pipeline_jobs || []).length}</div><div class="mover-stat-lbl">闭环任务</div></div>
      </div>
      <div style="font-size:12px;color:var(--text-secondary);margin-bottom:8px">
        <div>来源路径: <code>${esc(d.source || '-')}</code></div>
        <div>目标路径: <code>${esc(d.target || '-')}</code></div>
      </div>
    `;
  } catch (e) {
    box.textContent = '加载失败: ' + e.message;
  }
}

async function loadMoverStat() {
  try {
    const d = await req('/api/mount-mover');
    const el = document.getElementById('stat-mover');
    if (d.enabled === false) {
      el.textContent = '未启用';
      document.getElementById('stat-mover-sub').textContent = '服务器环境 (无需挂载)';
    } else {
      el.textContent = '运行中';
      document.getElementById('stat-mover-sub').textContent = `已跟踪 ${d.tracked || 0} 个文件`;
    }
  } catch (e) {
    document.getElementById('stat-mover').textContent = '-';
  }
}

// ── Actions ──
async function doAction(action) {
  const names = {
    'backup': '备份当前配置',
    'clear-retry': '清空重试队列',
    'compose-up': '启动全部服务',
    'compose-down': '停止全部服务'
  };
  if ((action === 'clear-retry' || action === 'compose-down') && !confirm(`确定要${names[action] || action}吗？`)) {
    return;
  }
  try {
    toast(`正在执行: ${names[action] || action}...`, 'info');
    const d = await req('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action })
    });
    toast(d.message || '操作成功完成');
    setTimeout(refreshOverview, 1500);
  } catch (e) {
    toast('操作失败: ' + e.message, 'err');
  }
}

// ── Init ──
(function init() {
  const savedUrl = localStorage.getItem('115_api_url');
  if (savedUrl) {
    API_BASE = savedUrl;
  } else if (window.location.port === '18811') {
    API_BASE = window.location.protocol + '//' + window.location.hostname + ':18810';
  }
  const inp = document.getElementById('login-url');
  if (inp && API_BASE) inp.value = API_BASE;
  if (window.location.port !== '18810' && window.location.port !== '') {
    const urlGroup = document.getElementById('urlGroup');
    if (urlGroup) urlGroup.style.display = 'block';
  }
  // Background light polling for overview when idle
  setInterval(() => {
    if (activeNav === 'overview' && !editingConfig && document.getElementById('app').style.display !== 'none') {
      loadOverview();
    }
  }, 20000);

  // Check auth
  req('/api/me').then(() => {
    showApp();
    refreshOverview();
  }).catch(() => {
    showLogin();
  });
})();
</script>
</body>
</html>
'''


async def index(request: web.Request):
    # 统一使用 LOGIN_PAGE（新版UI），通过 JavaScript 动态加载数据
    response = web.Response(text=LOGIN_PAGE, content_type="text/html")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


async def login(request: web.Request):
    if not ADMIN_PASSWORD:
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
    require_auth(request)
    name = request.query.get("name", "p115-card-bot")
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


# ── 连接测试 ──────────────────────────────────────────
async def _http_fetch(url: str, *, headers: dict | None = None, proxy: str | None = None, timeout: int = 12):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers or {}, proxy=proxy or None,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return resp.status, (await resp.text())[:500]
    except Exception as exc:
        return 0, str(exc)[:500]


def _resolve_value(body: dict, body_key: str, env_key: str) -> str:
    value = str(body.get(body_key) or "").strip()
    if value:
        return value
    for path in (CARD_ENV, ROOT_ENV, MONITOR_ENV):
        value = parse_env(path).get(env_key, "").strip()
        if value:
            return value
    return ""


async def health(request: web.Request):
    return web.json_response({"ok": True, "service": "card-admin"})


async def test_telegram(request: web.Request):
    require_auth(request)
    token = _resolve_value(json_body(request), "token", "TG_BOT_TOKEN")
    if not token:
        return web.json_response({"ok": False, "error": "未提供 Bot Token（请填写后测试，或先在配置中保存）"})
    status, text = await _http_fetch(f"https://api.telegram.org/bot{token}/getMe")
    if status != 200:
        return web.json_response({"ok": False, "error": f"Telegram 返回 {status}: {text[:200]}"})
    username = ""
    try:
        username = (json.loads(text).get("result") or {}).get("username", "")
    except Exception:
        pass
    return web.json_response({"ok": True, "message": "Bot 连接成功" + (f"（@{username}）" if username else "")})


async def test_tmdb(request: web.Request):
    require_auth(request)
    key = _resolve_value(json_body(request), "api_key", "TMDB_API_KEY")
    if not key:
        return web.json_response({"ok": False, "error": "未提供 TMDB API Key"})
    status, text = await _http_fetch(f"https://api.themoviedb.org/3/configuration?api_key={key}")
    if status != 200:
        return web.json_response({"ok": False, "error": f"TMDB 返回 {status}: {text[:200]}"})
    return web.json_response({"ok": True, "message": "TMDB API 连接成功"})


async def test_llm(request: web.Request):
    require_auth(request)
    body = json_body(request)
    base = str(body.get("base") or parse_env(CARD_ENV).get("LLM_API_BASE") or parse_env(ROOT_ENV).get("LLM_API_BASE") or "").strip().rstrip("/")
    key = str(body.get("key") or parse_env(CARD_ENV).get("LLM_API_KEY") or parse_env(ROOT_ENV).get("LLM_API_KEY") or "").strip()
    model = str(body.get("model") or parse_env(CARD_ENV).get("LLM_MODEL") or parse_env(ROOT_ENV).get("LLM_MODEL") or "").strip()
    if not base:
        return web.json_response({"ok": False, "error": "未提供 LLM API 地址"})
    if not model:
        return web.json_response({"ok": False, "error": "未提供 LLM 模型名"})
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/chat/completions", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                text = await resp.text()
    except Exception as e:
        return web.json_response({"ok": False, "error": f"LLM 请求失败: {str(e)[:200]}"})
    if status in (401, 403):
        return web.json_response({"ok": False, "error": f"LLM 鉴权失败（{status}），请检查 API Key"})
    if status == 200:
        return web.json_response({"ok": True, "message": "LLM API 连接成功" + (f"（模型 {model}）" if model else "")})
    if status == 404:
        return web.json_response({"ok": False, "error": f"LLM 接口 404，地址可能不对：{base}"})
    return web.json_response({"ok": False, "error": f"LLM 接口返回 {status}: {text[:200]}"})


async def test_p115(request: web.Request):
    require_auth(request)
    cookie = _resolve_value(json_body(request), "cookie", "P115_COOKIE")
    if not cookie:
        return web.json_response({"ok": False, "error": "未提供 115 Cookie"})
    try:
        from p115client import P115Client
        client = P115Client(cookie)
        info = await client.user_info(async_=True)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"115 连接失败: {str(e)[:200]}"})
    if isinstance(info, dict) and info.get("state"):
        data = info.get("data") or {}
        name = data.get("user_name") or data.get("user_id") or ""
        return web.json_response({"ok": True, "message": "115 Cookie 有效" + (f"（{name}）" if name else "")})
    return web.json_response({"ok": False, "error": "115 Cookie 无效或已过期"})


async def test_proxy(request: web.Request):
    require_auth(request)
    proxy = str(json_body(request).get("proxy") or "").strip()
    if not proxy:
        proxy = parse_env(CARD_ENV).get("TG_PROXY", "").strip() or parse_env(CARD_ENV).get("APP_HTTP_PROXY", "").strip()
    if not proxy:
        return web.json_response({"ok": False, "error": "未提供代理地址"})
    status, text = await _http_fetch("https://api.telegram.org", proxy=proxy, timeout=12)
    if status in (200, 301, 302, 404, 405):
        return web.json_response({"ok": True, "message": "代理连通正常"})
    return web.json_response({"ok": False, "error": f"代理测试失败: {text[:200]}"})


async def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[cors_middleware, body_middleware])
    app.add_routes([
        web.get("/", index), web.post("/api/login", login), web.post("/api/logout", logout), web.get("/api/me", me),
        web.get("/api/health", health),
        web.get("/api/overview", overview), web.post("/api/env/{kind}", update_env), web.post("/api/restart", restart),
        web.post("/api/service/{service}/{action}", service_action),
        web.get("/api/logs", logs), web.get("/api/retry-queue", retry_queue), web.get("/api/mount-mover", mount_mover), web.post("/api/action", action),
        web.get("/api/llm-prompt", get_llm_prompt), web.post("/api/llm-prompt", update_llm_prompt),
        web.post("/api/llm-prompt/reset", reset_llm_prompt),
        web.post("/api/test/telegram", test_telegram), web.post("/api/test/tmdb", test_tmdb),
        web.post("/api/test/llm", test_llm), web.post("/api/test/p115", test_p115),
        web.post("/api/test/proxy", test_proxy),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    print(f"card-admin listening on {HOST}:{PORT}", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
