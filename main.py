"""
115 分享转存机器人 v2 - 入口文件

启动:
1. Telegram Bot API (交互 + 通知)
2. Telethon 监听器 (监控目标 Bot 消息 → 自动转存)
"""
import os
import sys
import asyncio
import logging
from pathlib import Path

# ── 日志配置 ──
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "/data/bot.log")
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


async def main():
    from config import (
        TG_BOT_TOKEN, TG_API_ID, TG_API_HASH,
        TG_MONITOR_TARGETS, TG_USER_ID,
    )

    logger.info("=" * 50)
    logger.info("🚀 115 分享转存机器人 v2 启动中...")
    logger.info("=" * 50)

    # 检查配置，如果 TG_BOT_TOKEN 为空则进入「等待配置」模式
    waiting_mode = not TG_BOT_TOKEN
    if waiting_mode:
        logger.warning("⚠️ 未配置 TG_BOT_TOKEN，进入等待配置模式...")
        logger.warning("⚠️ 请通过其他方式设置 TG_BOT_TOKEN 后重启")

    # ── 1) 启动 Bot API ──
    from notifier import setup_bot, set_bot

    app = setup_bot()
    bot = app.bot
    set_bot(bot)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info(f"✅ Bot 启动成功: @{me.username} ({me.first_name})")

    if waiting_mode:
        # 通知管理员配置缺失
        for admin_id in (TG_USER_ID or "").split():
            if admin_id.strip():
                try:
                    await bot.send_message(
                        int(admin_id.strip()),
                        "⚠️ Bot 已启动但缺少关键配置。\n"
                        "请发送 /set TG_BOT_TOKEN <token> 设置 Token\n"
                        "然后发送 /restart 重启。"
                    )
                except Exception:
                    pass

    # ── 2) 启动 Telethon 监听器 ──
    monitor = None
    if TG_API_ID and TG_API_HASH and TG_MONITOR_TARGETS:
        from monitor import Monitor
        from notifier import send_private
        from pipeline import process_link

        async def on_link_found(url: str, source_name: str, event):
            logger.info(f"🔗 监听到 115 链接: {url[:60]}... (来源: {source_name})")

            if TG_USER_ID:
                await send_private(
                    int(TG_USER_ID),
                    f"🔔 监听到新链接!\n来源: {source_name}\n🔗 {url[:80]}...\n⏳ 正在自动转存..."
                )

            try:
                result = await process_link(url)

                if result["status"] == "success":
                    text = (
                        f"✅ 自动转存成功!\n"
                        f"📺 {result.get('title', '')}\n"
                        f"🎨 {result.get('quality', '')}\n"
                        f"💾 {result.get('size', '')}\n"
                        f"🔗 {result['share_link']}"
                    )
                    if TG_USER_ID:
                        await send_private(int(TG_USER_ID), text)
                    logger.info(f"✅ 自动转存成功: {result.get('title')} → {result['share_link']}")

                elif result["status"] == "pending":
                    if TG_USER_ID:
                        await send_private(
                            int(TG_USER_ID),
                            f"⏳ 转存中（审核中）: {result.get('message', '')}\n🔗 {result.get('share_link', url)}"
                        )
                else:
                    if TG_USER_ID:
                        await send_private(
                            int(TG_USER_ID),
                            f"❌ 自动转存失败: {result.get('message', '未知错误')}\n🔗 {url}"
                        )
            except Exception as e:
                logger.error(f"自动转存异常: {e}")
                if TG_USER_ID:
                    await send_private(int(TG_USER_ID), f"❌ 自动转存异常: {e}\n🔗 {url}")

        monitor = Monitor(on_link_found=on_link_found)
        try:
            await monitor.start()
        except Exception as e:
            logger.error(f"Telethon 监听器启动失败: {e}")
            logger.info("💡 请确保已配置 TG_API_ID / TG_API_HASH / TG_PHONE")

    logger.info("=" * 50)
    logger.info("✅ 所有组件启动完成!")
    logger.info(f"  - Bot: @{me.username}")
    logger.info(f"  - 监听器: {'运行中' if monitor and monitor.is_running else '未启用'}")
    logger.info("=" * 50)

    # 保持运行
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("🛑 正在关闭...")
        if monitor:
            await monitor.stop()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("👋 已关闭")


if __name__ == "__main__":
    asyncio.run(main())
