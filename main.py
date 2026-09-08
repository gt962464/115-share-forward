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

    # ── 1) 启动 Bot API（无 token 则跳过，进入等待配置模式）──
    app = None
    bot = None
    me = None

    if TG_BOT_TOKEN:
        from notifier import setup_bot, set_bot

        app = setup_bot()
        bot = app.bot
        set_bot(bot)

        await app.initialize()
        await app.start()

        # 清除可能残留的 webhook（webhook 与 getUpdates 互斥，残留会吞掉所有更新）
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ 已确认无 webhook（纯轮询模式）")
        except Exception as e:
            logger.warning(f"⚠️ 清除 webhook 失败: {e}")

        import telegram
        logger.info(f"python-telegram-bot 版本: {telegram.__version__}")

        await app.updater.start_polling(drop_pending_updates=True)

        me = await bot.get_me()
        logger.info(f"✅ Bot 启动成功: @{me.username} ({me.first_name})")
    else:
        logger.warning("⚠️ 未配置 TG_BOT_TOKEN，Bot API 未启动（等待配置模式）。")
        logger.warning("⚠️ 请在 .env 里填好 TG_BOT_TOKEN 后重启容器。")

    # ── 2) 启动 Telethon 监听器（无 Bot Token 时也可运行，只是通知发不出去）──
    monitor = None
    if TG_API_ID and TG_API_HASH and TG_MONITOR_TARGETS:
        from monitor import Monitor, set_monitor
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

        async def on_login_prompt(prompt: str):
            if TG_USER_ID:
                await send_private(int(TG_USER_ID), prompt)

        monitor = Monitor(on_link_found=on_link_found, on_login_prompt=on_login_prompt)
        set_monitor(monitor)
        try:
            await monitor.start()
        except Exception as e:
            logger.error(f"Telethon 监听器启动失败: {e}")
            logger.info("💡 请确保已配置 TG_API_ID / TG_API_HASH / TG_PHONE")

    # ── 3) 自动清理 worker（发卡成功后到期删除源文件 + 清空回收站）──
    from pipeline import start_cleanup_worker
    start_cleanup_worker()

    logger.info("=" * 50)
    logger.info("✅ 启动流程完成!")
    if me:
        logger.info(f"  - Bot: @{me.username}")
    else:
        logger.info("  - Bot: 未启动（缺少 TG_BOT_TOKEN）")
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
        if app:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        logger.info("👋 已关闭")


if __name__ == "__main__":
    asyncio.run(main())
