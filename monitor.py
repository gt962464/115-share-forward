"""
Telethon 频道/私聊监听器 - 监控指定 Bot 的消息
用于捕获 Postedia_bot 等解锁机器人发出的 115 链接
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, PhoneCodeInvalidError,
    SessionPasswordNeededError, PasswordHashInvalidError,
)

from config import TG_SESSION, set_config
from link_parser import extract_115_links

logger = logging.getLogger("monitor")

_PROCESSED_FILE = Path(os.getenv("PROCESSED_FILE", "/data/processed_keys.json"))
_MAX_PROCESSED = 5000


def make_proxy():
    """解析代理配置（动态读环境变量，/set 后新建客户端即可生效）。"""
    proxy_url = os.getenv("TG_PROXY", "").strip()
    if not proxy_url:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(proxy_url)
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise RuntimeError(f"不支持的代理协议: {scheme}")
    return (scheme, parsed.hostname, parsed.port, True, parsed.username, parsed.password)


# ── 全局单例（供 notifier 查询状态 / 填入验证码 / 热切换）──
_monitor_instance = None


def set_monitor(monitor):
    global _monitor_instance
    _monitor_instance = monitor


def get_monitor():
    return _monitor_instance


class Monitor:
    """Telegram 用户账号监听器。"""

    def __init__(self, on_link_found=None, on_login_prompt=None):
        """
        on_link_found: async callback(link: str, source_name: str, message)
        当发现 115 链接时调用。

        on_login_prompt: async callback(prompt: str)
        需要用户输入手机号/验证码/密码时调用（用于私聊 Bot 通知管理员）。

        配置全部动态读环境变量：/set 之后新建 Monitor 即可生效，无需重启。
        """
        self.api_id = int(os.getenv("TG_API_ID") or "0")
        self.api_hash = os.getenv("TG_API_HASH") or ""
        self.phone = os.getenv("TG_PHONE", "").strip()
        if not self.api_id or not self.api_hash:
            raise RuntimeError("缺少 TG_API_ID / TG_API_HASH，请先 /set 配置")

        self.client = TelegramClient(TG_SESSION, self.api_id, self.api_hash, proxy=make_proxy())
        self.on_link_found = on_link_found
        self.on_login_prompt = on_login_prompt
        self._running = False

        # 运行时状态（动态读当前环境变量，热切换时更新并持久化回 .env）
        self.targets: list = [
            v.strip() for v in os.getenv("TG_MONITOR_TARGETS", "").split(",") if v.strip()
        ]
        self.mode: str = (os.getenv("TG_MONITOR_MODE", "private").strip() or "private")

        # 去重（持久化到磁盘，重启不丢）
        self._processed_keys: set = self._load_processed()

        # 解析失败的目标（用于状态展示）
        self._failed_targets: dict = {}

        # handler 回调引用（热切换时 remove 旧的）
        self._handler_cbs = []

        # 登录交互状态（等待用户私聊输入验证码/密码）
        self._login_future: Optional[asyncio.Future] = None

        self._me = None

    # ── 去重持久化 ──
    def _load_processed(self) -> set:
        try:
            if _PROCESSED_FILE.exists():
                data = json.loads(_PROCESSED_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return set(data)
        except Exception as e:
            logger.warning(f"加载去重记录失败: {e}")
        return set()

    def _save_processed(self):
        try:
            _PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
            items = list(self._processed_keys)[-_MAX_PROCESSED:]
            _PROCESSED_FILE.write_text(json.dumps(items), encoding="utf-8")
        except Exception as e:
            logger.warning(f"保存去重记录失败: {e}")

    def _mark_processed(self, key: str):
        self._processed_keys.add(key)
        if len(self._processed_keys) > _MAX_PROCESSED:
            self._processed_keys = set(list(self._processed_keys)[-_MAX_PROCESSED // 2:])
        self._save_processed()

    # ── 启动与登录 ──
    async def start(self):
        if self._running:
            return
        logger.info("🚀 Telethon 监听器启动中...")
        # 用 connect() 而非 client.start(phone=...)：
        # 后者在未授权时会走 telethon 内置的 input() 交互，Docker 里直接卡死
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.warning("⚠️ Telethon 未授权，进入登录流程")
            await self._login_flow()

        self._me = await self.client.get_me()
        logger.info(f"✅ Telethon 登录成功: {self._me.first_name} (ID: {self._me.id})")

        await self._apply_handlers()
        self._running = True
        logger.info(f"👀 开始监听 {len(self.targets)} 个目标（mode={self.mode}）")

    async def _login_flow(self):
        # 手机号也可以走 Bot 私聊输入，实现「完全在 Bot 内登录」
        if not self.phone:
            self.phone = (await self._ask(
                "请输入 Telegram 手机号（带国家区号，如 +8613812345678）",
                env_key="TG_PHONE", input_prompt="请输入手机号: ",
            )).strip()
            set_config("TG_PHONE", self.phone)

        logger.info(f"📱 向 {self.phone} 发送验证码...")
        result = await self.client.send_code_request(self.phone)

        # 验证码允许错 3 次；遇到二步验证再问密码
        last_err = None
        for attempt in range(3):
            code = await self._ask(
                f"请输入登录验证码（已发送到 {self.phone} 的 Telegram，请直接回复数字）",
                # 环境变量只在第一次尝试用，避免拿过期验证码反复撞
                env_key="TG_LOGIN_CODE" if attempt == 0 else "__NO_ENV__",
                input_prompt="请输入验证码: ",
            )
            try:
                await self.client.sign_in(
                    self.phone, code, phone_code_hash=result.phone_code_hash,
                )
                last_err = None
                break
            except SessionPasswordNeededError:
                password = await self._ask(
                    "请输入二步验证密码",
                    env_key="TG_LOGIN_PASSWORD" if attempt == 0 else "__NO_ENV__",
                    input_prompt="需要二步验证密码: ",
                )
                await self.client.sign_in(password=password)
                last_err = None
                break
            except PhoneCodeInvalidError as e:
                last_err = e
                logger.warning(f"⚠️ 验证码错误（{attempt + 1}/3）")
                if self.on_login_prompt and attempt < 2:
                    await self.on_login_prompt(f"⚠️ 验证码错误（{attempt + 1}/3），请重新回复正确的验证码。")
        if last_err is not None:
            raise RuntimeError("验证码多次错误，登录失败，请重新发起登录")

        logger.info("✅ 登录成功!")

    async def _ask(self, what: str, env_key: str, input_prompt: str) -> str:
        """获取一段用户输入：环境变量 → 私聊 Bot → stdin。"""
        env_val = os.getenv(env_key, "").strip()
        if env_val:
            return env_val

        if self.on_login_prompt:
            loop = asyncio.get_running_loop()
            self._login_future = loop.create_future()
            try:
                await self.on_login_prompt(f"🔐 {what}\n\n请直接回复本消息（只发验证码/密码本身，不要加其他文字）。")
                return await asyncio.wait_for(self._login_future, timeout=300)
            except asyncio.TimeoutError:
                raise RuntimeError(f"等待「{what}」超时（5 分钟）")
            finally:
                self._login_future = None

        try:
            return input(input_prompt).strip()
        except (EOFError, OSError):
            raise RuntimeError(
                f"非交互环境无法读取「{what}」。请设置环境变量 {env_key}，或通过 Bot 私聊输入。"
            )

    # ── 验证码填入（由 notifier 调用）──
    @property
    def is_waiting_login(self) -> bool:
        return self._login_future is not None and not self._login_future.done()

    def provide_login_input(self, text: str) -> bool:
        """填入验证码/密码。返回是否成功。"""
        if self.is_waiting_login:
            self._login_future.set_result(text.strip())
            return True
        return False

    # ── handler 管理（支持热切换）──
    async def _resolve_targets(self) -> set:
        ids = set()
        self._failed_targets = {}
        for target in self.targets:
            try:
                entity = await self.client.get_entity(target)
                ids.add(entity.id)
            except Exception as e:
                self._failed_targets[target] = str(e)
                logger.warning(f"⚠️ 无法解析监听目标 {target}: {e}")
        return ids

    async def _apply_handlers(self):
        # 移除旧 handler
        for cb in self._handler_cbs:
            try:
                self.client.remove_event_handler(cb)
            except Exception:
                pass
        self._handler_cbs.clear()

        target_ids = await self._resolve_targets()
        if not target_ids:
            logger.error("❌ 没有有效的监听目标")
            return

        async def on_new(event):
            await self._handle_message(event, "new")

        async def on_edited(event):
            await self._handle_message(event, "edited")

        self.client.add_event_handler(on_new, events.NewMessage(chats=target_ids))
        self.client.add_event_handler(on_edited, events.MessageEdited(chats=target_ids))
        self._handler_cbs.extend([on_new, on_edited])
        logger.info(f"✅ 已注册监听（mode={self.mode}）: {self.targets}")

    # ── 热切换操作 ──
    async def add_target(self, name: str) -> str:
        name = name.strip().lstrip("@")
        if not name:
            return "❌ 目标不能为空"
        if name in self.targets:
            return f"⚠️ 目标 {name} 已存在"
        self.targets.append(name)
        self._persist_targets()
        await self._apply_handlers()
        return f"✅ 已添加监听目标: {name}"

    async def remove_target(self, name: str) -> str:
        name = name.strip().lstrip("@")
        if name not in self.targets:
            return f"⚠️ 目标 {name} 不在监听列表"
        self.targets.remove(name)
        self._persist_targets()
        await self._apply_handlers()
        return f"✅ 已移除监听目标: {name}"

    async def set_mode(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in {"private", "channel"}:
            return "❌ 模式只能是 private 或 channel"
        if mode == self.mode:
            return f"⚠️ 已是 {mode} 模式"
        self.mode = mode
        set_config("TG_MONITOR_MODE", mode)
        await self._apply_handlers()
        return f"✅ 已切换到 {mode} 模式"

    def _persist_targets(self):
        set_config("TG_MONITOR_TARGETS", ",".join(self.targets))

    # ── 状态查询 ──
    def status_text(self) -> str:
        if not self._running:
            return "📡 监听器状态\n\n状态: ❌ 未运行"
        lines = ["📡 监听器状态\n"]
        lines.append("状态: ✅ 运行中")
        if self._me:
            username = getattr(self._me, "username", None) or self._me.id
            lines.append(f"账号: {getattr(self._me, 'first_name', '')} (@{username})")
        lines.append(f"监听模式: {self.mode}")
        lines.append(f"监听目标 ({len(self.targets)}):")
        if self.targets:
            for t in self.targets:
                if t in self._failed_targets:
                    lines.append(f"  ⚠️ {t} — {self._failed_targets[t]}")
                else:
                    lines.append(f"  ✅ {t}")
        else:
            lines.append("  （无）")
        lines.append(f"去重记录: {len(self._processed_keys)} 条")
        return "\n".join(lines)

    # ── 消息处理 ──
    async def _handle_message(self, event, event_type: str):
        text = event.raw_text or ""
        entities = event.message.entities
        links = extract_115_links(text, entities)
        if not links:
            return

        for link_info in links:
            key = f"{event.id}:{link_info['url']}"
            if key in self._processed_keys:
                continue
            self._mark_processed(key)

            source = "unknown"
            try:
                sender = await event.get_sender()
                source = getattr(sender, "first_name", None) or getattr(sender, "username", None) or "unknown"
            except Exception:
                pass

            logger.info(f"🔗 监听到 115 链接: [{event_type}] 来自 {source}: {link_info['url'][:60]}...")

            if self.on_link_found:
                try:
                    await self.on_link_found(link_info["url"], source, event)
                except Exception as e:
                    logger.error(f"处理链接回调失败: {e}")

    async def stop(self):
        self._running = False
        if self.client.is_connected():
            await self.client.disconnect()
        logger.info("🛑 Telethon 监听器已停止")

    @property
    def is_running(self) -> bool:
        return self._running

    async def check_authorized(self) -> bool:
        """当前是否已登录授权。"""
        try:
            return self.client.is_connected() and await self.client.is_user_authorized()
        except Exception:
            return False
