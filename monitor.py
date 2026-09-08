"""
Telethon 频道/私聊监听器 - 监控指定 Bot 的消息
用于捕获 Postedia_bot 等解锁机器人发出的 115 链接
"""
import asyncio
import logging
import os
import re
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError, PhoneCodeInvalidError,
    SessionPasswordNeededError, PasswordHashInvalidError,
)

from config import (
    TG_API_ID, TG_API_HASH, TG_PHONE, TG_SESSION,
    TG_PROXY, TG_MONITOR_TARGETS, TG_MONITOR_MODE,
)
from link_parser import extract_115_links

logger = logging.getLogger("monitor")


def make_proxy():
    """解析代理配置。"""
    if not TG_PROXY:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(TG_PROXY)
    scheme = parsed.scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise RuntimeError(f"不支持的代理协议: {scheme}")
    return (scheme, parsed.hostname, parsed.port, True, parsed.username, parsed.password)


class Monitor:
    """Telegram 用户账号监听器。"""
    
    def __init__(self, on_link_found=None):
        """
        on_link_found: async callback(link: str, source_name: str, message)
        当发现 115 链接时调用。
        """
        self.client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH, proxy=make_proxy())
        self.on_link_found = on_link_found
        self._running = False
        self._processed_keys: set = set()
    
    async def start(self):
        """启动监听。"""
        logger.info("🚀 Telethon 监听器启动中...")
        
        await self.client.start(phone=TG_PHONE)
        
        if not await self.client.is_user_authorized():
            logger.warning("⚠️ Telethon 未授权，请先运行登录流程")
            await self._login_flow()
        
        me = await self.client.get_me()
        logger.info(f"✅ Telethon 登录成功: {me.first_name} (ID: {me.id})")
        
        # 根据监听模式注册 handler
        if TG_MONITOR_MODE == "private":
            await self._setup_private_monitor()
        else:
            await self._setup_channel_monitor()
        
        self._running = True
        logger.info(f"👀 开始监听 {len(TG_MONITOR_TARGETS)} 个目标")
    
    async def _setup_private_monitor(self):
        """监听与指定 Bot 的私聊。"""
        target_ids = set()
        for target in TG_MONITOR_TARGETS:
            try:
                entity = await self.client.get_entity(target)
                target_ids.add(entity.id)
                name = getattr(entity, "first_name", None) or target
                logger.info(f"  📌 已解析监听目标: {name} (ID: {entity.id})")
            except Exception as e:
                logger.warning(f"  ⚠️ 无法解析监听目标 {target}: {e}")
        
        if not target_ids:
            logger.error("❌ 没有有效的监听目标")
            return
        
        @self.client.on(events.NewMessage(chats=target_ids))
        async def on_new_message(event):
            await self._handle_message(event, "new")
        
        @self.client.on(events.MessageEdited(chats=target_ids))
        async def on_edited(event):
            await self._handle_message(event, "edited")
        
        logger.info(f"  ✅ 已注册私聊监听: {list(TG_MONITOR_TARGETS)}")
    
    async def _setup_channel_monitor(self):
        """监听频道消息。"""
        target_ids = set()
        for target in TG_MONITOR_TARGETS:
            try:
                entity = await self.client.get_entity(target)
                target_ids.add(entity.id)
                name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or target
                logger.info(f"  📌 已解析监听频道: {name} (ID: {entity.id})")
            except Exception as e:
                logger.warning(f"  ⚠️ 无法解析监听频道 {target}: {e}")
        
        if not target_ids:
            logger.error("❌ 没有有效的监听频道")
            return
        
        @self.client.on(events.NewMessage(chats=target_ids))
        async def on_new(event):
            await self._handle_message(event, "new")
        
        @self.client.on(events.MessageEdited(chats=target_ids))
        async def on_edited(event):
            await self._handle_message(event, "edited")
        
        logger.info(f"  ✅ 已注册频道监听: {list(TG_MONITOR_TARGETS)}")
    
    async def _handle_message(self, event, event_type: str):
        """处理监听到的消息。"""
        text = event.raw_text or ""
        entities = event.message.entities
        
        links = extract_115_links(text, entities)
        if not links:
            return
        
        for link_info in links:
            key = (event.id, link_info["url"])
            if key in self._processed_keys:
                continue
            self._processed_keys.add(key)
            
            # 防内存泄漏
            if len(self._processed_keys) > 5000:
                self._processed_keys = set(list(self._processed_keys)[-2500:])
            
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
    
    async def _login_flow(self):
        """交互式登录流程（优先读环境变量，其次 stdin，最后提示手动登录）。"""
        if not TG_PHONE:
            raise RuntimeError("需要 TG_PHONE 配置才能登录")

        logger.info(f"📱 向 {TG_PHONE} 发送验证码...")
        result = await self.client.send_code_request(TG_PHONE)

        # 优先从环境变量读验证码（脚本化登录，避免 Docker 里 input() 卡死）
        code = os.getenv("TG_LOGIN_CODE", "").strip()
        if not code:
            try:
                code = input("请输入验证码: ").strip()
            except (EOFError, OSError):
                logger.error(
                    "❌ 非交互环境无法读取验证码（docker logs 是只读的，无法输入）。\n"
                    "二选一：\n"
                    "1) 设置环境变量 TG_LOGIN_CODE=<验证码> 后重启容器；\n"
                    "2) 手动登录一次（session 持久化到 data/user.session）：\n"
                    '''   docker exec -it 115-bot python -c "from telethon import TelegramClient; from config import TG_API_ID, TG_API_HASH, TG_PHONE, TG_SESSION; c=TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH); c.start(phone=TG_PHONE); print('登录成功')"'''
                )
                raise RuntimeError("需要交互式登录（见上方指引）")

        try:
            await self.client.sign_in(TG_PHONE, code, phone_code_hash=result.phone_code_hash)
        except SessionPasswordNeededError:
            password = os.getenv("TG_LOGIN_PASSWORD", "").strip()
            if not password:
                try:
                    password = input("需要二步验证密码: ").strip()
                except (EOFError, OSError):
                    logger.error(
                        "❌ 需要二步验证密码，但无法在非交互环境读取。\n"
                        "请设置环境变量 TG_LOGIN_PASSWORD=<密码> 后重启，\n"
                        "或用 docker exec -it 手动登录。"
                    )
                    raise RuntimeError("需要二步验证密码（见上方指引）")
            await self.client.sign_in(password=password)

        logger.info("✅ 登录成功!")
    
    async def stop(self):
        """停止监听。"""
        self._running = False
        if self.client.is_connected():
            await self.client.disconnect()
        logger.info("🛑 Telethon 监听器已停止")
    
    @property
    def is_running(self) -> bool:
        return self._running
