#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 用户账号监听器 + 本地网页登录授权控制台。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from typing import Optional

import aiohttp
from aiohttp import web
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, PasswordHashInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError
from pan123_link_parser import extract_123_links

try:
    from aiohttp_socks import ProxyConnector
except ImportError:
    ProxyConnector = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("tg-user-monitor")


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必填配置: {name}")
    return value


TG_API_ID = int(required("TG_API_ID"))
TG_API_HASH = required("TG_API_HASH")
TG_PHONE = os.getenv("TG_PHONE", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "/tg-monitor-state/user")
TG_SOURCE_CHAT = required("TG_SOURCE_CHAT")
TG_FORWARD_TO = required("TG_FORWARD_TO")
TG_BOT_TOKEN = required("TG_BOT_TOKEN")
TG_PROXY = os.getenv("TG_PROXY", "").strip()
TG_START_FROM = int(os.getenv("TG_START_FROM", "0"))
FORWARD_TIMEOUT = float(os.getenv("TG_FORWARD_TIMEOUT", "30"))
WEB_HOST = os.getenv("TG_MONITOR_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("TG_MONITOR_WEB_PORT", "18800"))
WEB_SECRET = os.getenv("TG_MONITOR_WEB_SECRET", "").strip()

CTRL_RE = re.compile(r"[\u200b\u200c\u200d\u2066-\u2069\ufeff]")
LINK_RE = re.compile(r"https?://(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9]+(?:[?#][^\s]+)?", re.IGNORECASE)
TAIL_RE = re.compile(r"[^A-Za-z0-9?=&:/._-]+$")


def make_proxy() -> Optional[tuple]:
    if not TG_PROXY:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(TG_PROXY)
    if parsed.scheme.lower() not in {"socks5", "socks4", "http"}:
        raise RuntimeError("TG_PROXY 仅支持 socks5://、socks4:// 或 http://")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("TG_PROXY 缺少主机或端口")
    return ({"socks5": "socks5", "socks4": "socks4", "http": "http"}[parsed.scheme.lower()], parsed.hostname, parsed.port, True, parsed.username, parsed.password)


def make_bot_session() -> aiohttp.ClientSession:
    if not TG_PROXY:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=FORWARD_TIMEOUT))
    if ProxyConnector is None:
        raise RuntimeError("使用 TG_PROXY 时需要安装 aiohttp-socks")
    return aiohttp.ClientSession(connector=ProxyConnector.from_url(TG_PROXY), timeout=aiohttp.ClientTimeout(total=FORWARD_TIMEOUT))


async def user_send_message(client: TelegramClient, target, text: str) -> None:
    """用已授权的 Telegram 用户账号发送；Bot API 禁止机器人给机器人发消息。"""
    await client.send_message(target, text)


def extract_links(text: str, entities=None) -> list[str]:
    clean = CTRL_RE.sub("", text or "")
    values = list(LINK_RE.findall(clean))
    # 转发机器人常把真实115地址放在“点击文字”实体中，正文只显示“直达链接”。
    for entity in entities or []:
        entity_url = getattr(entity, "url", None)
        if entity_url:
            values.append(entity_url)
    return list(dict.fromkeys(TAIL_RE.sub("", value) for value in values if LINK_RE.search(value)))


async def resolve_entity(client: TelegramClient, value: str):
    try:
        return await client.get_entity(int(value.strip()))
    except ValueError:
        return await client.get_entity(value.strip())


class MonitorRuntime:
    def __init__(self):
        self.client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH, proxy=make_proxy())
        self.bot_session: Optional[aiohttp.ClientSession] = None
        self.source = None
        self.target = None
        self.status = "等待启动"
        self.detail = ""
        self.phone_code_hash = None
        self.lock = asyncio.Lock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.stop_event = asyncio.Event()
        # 同一条消息可能先发“正在解析”，再编辑成解锁成功；同一链接只转交一次。
        self._forwarded_keys: set[tuple[int, str]] = set()

    def snapshot(self) -> dict:
        return {"status": self.status, "detail": self.detail, "authorized": self.client.is_connected() and self.phone_code_hash is None and self.status == "监听中"}

    async def request_code(self) -> None:
        async with self.lock:
            self.status, self.detail = "正在发送验证码", "请稍候"
            if not TG_PHONE:
                raise RuntimeError("配置中没有 TG_PHONE")
            await self.client.connect()
            if await self.client.is_user_authorized():
                self.status, self.detail = "已登录", "当前 session 已授权，请直接启动监听"
                return
            result = await self.client.send_code_request(TG_PHONE)
            self.phone_code_hash = result.phone_code_hash
            self.status, self.detail = "等待验证码", "验证码已发送到 Telegram，请在网页输入"

    async def verify_code(self, code: str, password: str = "") -> None:
        async with self.lock:
            if not self.phone_code_hash:
                raise RuntimeError("请先点击“发送验证码”")
            try:
                await self.client.sign_in(TG_PHONE, code.strip(), phone_code_hash=self.phone_code_hash)
            except PhoneCodeInvalidError:
                raise RuntimeError("验证码错误，请重新输入")
            except Exception as exc:
                if not isinstance(exc, SessionPasswordNeededError):
                    raise
                if not password.strip():
                    self.status, self.detail = "等待二级密码", "请输入 Telegram 二步验证密码"
                    raise RuntimeError("需要二步验证密码")
                try:
                    await self.client.sign_in(password=password)
                except PasswordHashInvalidError:
                    raise RuntimeError("二步验证密码错误")
            self.phone_code_hash = None
            self.status, self.detail = "已登录", "授权成功，可以启动监听"

    async def start_monitor(self) -> None:
        async with self.lock:
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("尚未登录，请先完成验证码和二步验证")
            self.source = await resolve_entity(self.client, TG_SOURCE_CHAT)
            self.target = await resolve_entity(self.client, TG_FORWARD_TO)
            # S7 是机器人账号，必须由已授权用户账号直接发送，不能走 Bot API。
            if str(TG_FORWARD_TO).lstrip("@").lower().endswith("bot"):
                self.bot_session = None
            else:
                self.bot_session = make_bot_session()
            log.info(
                "监听源实体已解析: %s (%s, id=%s)",
                TG_SOURCE_CHAT, type(self.source).__name__, getattr(self.source, "id", "?")
            )

            async def handle_source_event(event, event_name: str):
                if TG_START_FROM and event.id <= TG_START_FROM:
                    return
                raw_text = event.raw_text or ""
                pan123_links = extract_123_links(raw_text, event.message.entities)
                for item in pan123_links:
                    log.info(
                        "检测到123云盘链接（只读阶段，不转存/不转发）: event=%s msg_id=%s "
                        "share_id=%s access_code=%s url=%s",
                        event_name,
                        event.id,
                        item["share_id"],
                        item["access_code"] or "未提供",
                        item["url"],
                    )
                links = extract_links(raw_text, event.message.entities)
                if not links:
                    if pan123_links:
                        return
                    log.info(
                        "收到来源消息但未提取到115/123链接: event=%s msg_id=%s text=%r entities=%s",
                        event_name, event.id, raw_text[:300],
                        [type(x).__name__ for x in (event.message.entities or [])],
                    )
                    return
                for link in links:
                    key = (event.id, link)
                    if key in self._forwarded_keys:
                        continue
                    try:
                        if self.bot_session is None:
                            await user_send_message(self.client, self.target, link)
                        else:
                            await bot_send_message(self.bot_session, self.target, link)
                        self._forwarded_keys.add(key)
                        # 只保留最近消息，避免长期运行内存增长。
                        if len(self._forwarded_keys) > 5000:
                            self._forwarded_keys = set(list(self._forwarded_keys)[-2500:])
                        log.info("已转交115链接: event=%s msg_id=%s link=%s", event_name, event.id, link)
                    except FloodWaitError as exc:
                        await asyncio.sleep(exc.seconds)
                    except Exception:
                        log.exception("转交失败: event=%s msg_id=%s link=%s", event_name, event.id, link)

            @self.client.on(events.NewMessage(chats=self.source))
            async def on_message(event):
                await handle_source_event(event, "new")

            @self.client.on(events.MessageEdited(chats=self.source))
            async def on_message_edited(event):
                # HDHive 常先发“正在解析”，数秒后编辑同一条消息补上115链接。
                await handle_source_event(event, "edited")

            self.status, self.detail = "监听中", f"来源 {TG_SOURCE_CHAT} → 目标 {TG_FORWARD_TO}"

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.client.connect()
        if await self.client.is_user_authorized():
            try:
                await self.start_monitor()
            except Exception as exc:
                self.status, self.detail = "登录成功但启动失败", str(exc)
        else:
            self.status, self.detail = "未登录", "请通过网页发送验证码"
        await self.stop_event.wait()


runtime = MonitorRuntime()


HTML = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Telegram 115 监听器</title><style>body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f7fb;color:#172033;margin:0;padding:28px}.wrap{max-width:680px;margin:auto}.card{background:#fff;border:1px solid #dfe6f0;border-radius:18px;padding:24px;box-shadow:0 10px 30px #20304b12}h1{margin:0 0 8px;font-size:25px}p{color:#61708a}.status{margin:18px 0;padding:14px;border-radius:12px;background:#eef5ff}.status b{color:#1264d8}label{display:block;margin:14px 0 6px;font-weight:600}input{width:100%;box-sizing:border-box;padding:12px;border:1px solid #cbd6e5;border-radius:10px;font-size:16px}button{margin-top:16px;padding:12px 16px;border:0;border-radius:10px;background:#1264d8;color:white;font-size:15px;cursor:pointer}button.secondary{background:#e9eff8;color:#18304f;margin-left:8px}.hint{font-size:13px;color:#71809a;margin-top:14px}.danger{color:#a33b3b}</style></head><body><div class=\"wrap\"><div class=\"card\"><h1>Telegram 115 链接监听器</h1><p>按下面步骤登录一次，之后服务会自动监听来源机器人私聊。</p><div class=\"status\">状态：<b id=\"status\">读取中</b><div id=\"detail\"></div></div><label>验证码</label><input id=\"code\" inputmode=\"numeric\" placeholder=\"Telegram 发来的验证码\"><label>二步验证密码（如提示需要）</label><input id=\"password\" type=\"password\" placeholder=\"只在本页面输入，不会保存\"><button onclick=\"sendCode()\">1. 发送验证码</button><button class=\"secondary\" onclick=\"verify()\">2. 提交验证码并登录</button><br><button onclick=\"start()\">3. 启动监听</button><div class=\"hint\">来源：@S7_nanshare_bot<br>转交给：现有卡片机器人（目标 ID 已配置）</div><div class=\"hint danger\">请只在 NAS 内网打开此页面，不要将端口暴露到公网。</div></div></div><script>async function call(path,body){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});let j=await r.json();alert(j.message||j.detail||'完成');refresh()}async function sendCode(){call('/api/send-code')}async function verify(){call('/api/verify',{code:document.querySelector('#code').value,password:document.querySelector('#password').value})}async function start(){call('/api/start')}async function refresh(){let j=await fetch('/api/status').then(r=>r.json());document.querySelector('#status').textContent=j.status;document.querySelector('#detail').textContent=j.detail||''}refresh();setInterval(refresh,3000)</script></body></html>"""


def check_secret(request: web.Request):
    if WEB_SECRET and request.headers.get("X-Monitor-Secret") != WEB_SECRET:
        raise web.HTTPUnauthorized(text="Unauthorized")


async def index(request):
    check_secret(request)
    return web.Response(text=HTML, content_type="text/html")


async def status(request):
    check_secret(request)
    return web.json_response(runtime.snapshot())


async def send_code(request):
    check_secret(request)
    try:
        await runtime.request_code()
        return web.json_response({"message": "验证码已发送，请查看 Telegram"})
    except Exception as exc:
        runtime.status, runtime.detail = "操作失败", str(exc)
        return web.json_response({"message": str(exc)}, status=400)


async def verify(request):
    check_secret(request)
    data = await request.json()
    try:
        await runtime.verify_code(str(data.get("code", "")), str(data.get("password", "")))
        return web.json_response({"message": "登录成功"})
    except Exception as exc:
        runtime.detail = str(exc)
        return web.json_response({"message": str(exc)}, status=400)


async def start_monitor(request):
    check_secret(request)
    try:
        await runtime.start_monitor()
        return web.json_response({"message": "监听已启动"})
    except Exception as exc:
        runtime.status, runtime.detail = "启动失败", str(exc)
        return web.json_response({"message": str(exc)}, status=400)


async def web_main():
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/api/status", status), web.post("/api/send-code", send_code), web.post("/api/verify", verify), web.post("/api/start", start_monitor)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
    log.info("网页登录控制台：http://%s:%s", WEB_HOST, WEB_PORT)


async def main():
    await web_main()
    await runtime.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
