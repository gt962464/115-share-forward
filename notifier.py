"""
Telegram Bot API 交互器 - 用户操作 + 配置管理 + 通知
"""
import os
import logging
from typing import Optional
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)
from telegram.constants import ParseMode

from config import (
    TG_BOT_TOKEN, TG_CHANNEL_ID, TG_ADMIN_IDS,
    set_config, get_config, format_config_list,
    CONFIG_SCHEMA,
)
from link_parser import extract_115_links
from pipeline import process_link, format_size

logger = logging.getLogger("notifier")

# 全局 bot 实例（由 main.py 设置）
_bot: Optional[Bot] = None
_app: Optional[Application] = None


def set_bot(bot: Bot):
    global _bot
    _bot = bot


def get_bot() -> Optional[Bot]:
    return _bot


async def send_private(chat_id: int, text: str, parse_mode: str = None):
    """发送私聊消息。"""
    if not _bot:
        return
    try:
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"发送私聊消息失败: {e}")


async def send_channel(text: str, parse_mode: str = None):
    """发送到频道。"""
    if not _bot or not TG_CHANNEL_ID:
        return
    try:
        target = int(TG_CHANNEL_ID) if TG_CHANNEL_ID.lstrip("-").isdigit() else TG_CHANNEL_ID
        await _bot.send_message(chat_id=target, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"发送频道消息失败: {e}")


def _is_admin(user_id: int) -> bool:
    return str(user_id) in TG_ADMIN_IDS


# ══════════════════════════════════════════════
#  命令处理器
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 115 分享转存机器人\n\n"
        "📋 基本用法:\n"
        "• 直接发 115 链接 → 自动转存+重命名+生成永久链接\n"
        "• /link <url> → 手动提交链接\n"
        "• /status → 运行状态\n"
        "• /log [N] → 最近日志\n\n"
        "⚙️ 配置管理:\n"
        "• /config → 查看所有配置\n"
        "• /set <KEY> <VALUE> → 修改配置\n"
        "• /setlist → 可配置项列表\n\n"
        "🔗 支持域名: 115.com / 115cdn.com / anxia.com"
    )
    await update.message.reply_text(text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from pipeline import get_svc
    try:
        svc = await get_svc()
        status = "✅ 正常" if svc and svc.client else "❌ 未连接"
        busy = "繁忙" if svc and svc.is_busy else "空闲"
    except Exception:
        status = "❌ 初始化失败"
        busy = "未知"

    from config import TG_MONITOR_TARGETS
    text = (
        f"📊 运行状态\n\n"
        f"115 客户端: {status}\n"
        f"处理队列: {busy}\n"
        f"监听目标: {len(TG_MONITOR_TARGETS)} 个\n"
        f"输出频道: {TG_CHANNEL_ID or '未配置'}"
    )
    await update.message.reply_text(text)


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from pathlib import Path
    log_file = Path("/data/bot.log")
    if not log_file.exists():
        await update.message.reply_text("📝 暂无日志文件")
        return
    try:
        args = context.args or []
        n = int(args[0]) if args else 20
        n = min(n, 50)
        lines = log_file.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:]
        text = f"📝 最近 {len(recent)} 条日志:\n\n" + "\n".join(recent)
        if len(text) > 4000:
            text = text[-4000:]
        await update.message.reply_text(f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"读取日志失败: {e}")


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法: /link <115分享链接>")
        return
    url = context.args[0]
    await _handle_link(update.message, url)


# ══════════════════════════════════════════════
#  配置管理命令
# ══════════════════════════════════════════════

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看所有配置。"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令")
        return
    text = format_config_list()
    await update.message.reply_text(text)


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置配置: /set KEY value"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "用法: /set <配置项> <值>\n\n"
            "示例:\n"
            "  /set P115_COOKIE 你的cookie\n"
            "  /set TG_CHANNEL_ID -1001234567890\n"
            "  /set TG_MONITOR_TARGETS postedia_bot,OtherBot\n"
            "  /set AUTO_RENAME 0\n\n"
            "发送 /setlist 查看所有可配置项"
        )
        return

    key = context.args[0].upper()
    value = " ".join(context.args[1:])

    result = set_config(key, value)
    await update.message.reply_text(result)

    # 如果改了关键配置，提醒重启
    restart_keys = {"TG_BOT_TOKEN", "TG_API_ID", "TG_API_HASH", "TG_MONITOR_TARGETS"}
    if key in restart_keys:
        await update.message.reply_text("⚠️ 此配置需要重启 Bot 才能生效。\n发送 /restart 重启。")


async def cmd_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """显示所有可配置项。"""
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令")
        return

    lines = ["📋 可配置项:\n"]
    for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
        val = os.environ.get(k, default)
        mark = "✅" if val else "⬜"
        lines.append(f"{mark} {k} — {desc}")
    lines.append("\n用法: /set <KEY> <VALUE>")
    await update.message.reply_text("\n".join(lines))


# ══════════════════════════════════════════════
#  链接处理
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通消息 - 自动识别 115 链接。"""
    text = update.message.text or ""
    links = extract_115_links(text, update.message.entities)
    if not links:
        return
    for link in links:
        await _handle_link(update.message, link["url"])


async def _handle_link(message, url: str):
    """处理一个 115 链接。"""
    status_msg = await message.reply_text(f"⏳ 收到链接，开始处理...\n🔗 {url[:60]}...")

    async def on_progress(text: str):
        try:
            await status_msg.edit_text(f"{text}\n🔗 {url[:60]}...")
        except Exception:
            pass

    result = await process_link(url, on_progress=on_progress)

    if result["status"] == "success":
        share_link = result["share_link"]
        title = result.get("title", "")
        quality = result.get("quality", "")
        size = result.get("size", "")
        parts = ["✅ 转存成功!"]
        if title:
            parts.append(f"📺 {title}")
        if quality:
            parts.append(f"🎨 {quality}")
        if size:
            parts.append(f"💾 {size}")
        parts.append(f"🔗 永久分享: {share_link}")
        await status_msg.edit_text("\n".join(parts))

        # 发送到频道
        if TG_CHANNEL_ID:
            ch = f"📺 {title} [{quality}]\n💾 {size}\n🔗 {share_link}" if quality else f"📺 {title}\n🔗 {share_link}"
            await send_channel(ch)
        logger.info(f"✅ 处理成功: {title} → {share_link}")

    elif result["status"] == "pending":
        await status_msg.edit_text(
            f"⏳ {result.get('message', '处理中...')}\n"
            f"🔗 {result.get('share_link', url)}\n完成后会自动通知。"
        )
    else:
        await status_msg.edit_text(f"❌ 处理失败: {result.get('message', '未知错误')}")


# ══════════════════════════════════════════════
#  Bot 初始化
# ══════════════════════════════════════════════

def setup_bot() -> Application:
    global _app

    app = Application.builder().token(TG_BOT_TOKEN).build()

    # 基本命令
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("link", cmd_link))

    # 配置管理
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("setlist", cmd_setlist))

    # 自动识别 115 链接
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _app = app
    return app

