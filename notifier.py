"""
Telegram Bot API 交互器 - 全 InlineKeyboard 面板
"""
import os
import logging
from typing import Optional
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
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

_bot: Optional[Bot] = None
_app: Optional[Application] = None


def set_bot(bot: Bot):
    global _bot
    _bot = bot


def get_bot() -> Optional[Bot]:
    return _bot


async def send_private(chat_id: int, text: str, parse_mode: str = None):
    if not _bot:
        return
    try:
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"发送私聊消息失败: {e}")


async def send_channel(text: str, parse_mode: str = None):
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
#  Keyboard 构建器
# ══════════════════════════════════════════════

def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 提交链接", callback_data="menu_link")],
        [InlineKeyboardButton("📊 运行状态", callback_data="menu_status")],
        [InlineKeyboardButton("⚙️ 查看配置", callback_data="menu_config")],
        [InlineKeyboardButton("📋 可配置项", callback_data="menu_setlist")],
        [InlineKeyboardButton("📝 查看日志", callback_data="menu_log")],
        [InlineKeyboardButton("❓ 帮助", callback_data="menu_help")],
    ])


def _back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")]
    ])


def _back_and_help() -> InlineKeyboardMarkup:
    """返回主菜单 + 帮助 按钮。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


# ══════════════════════════════════════════════
#  /start → 主菜单
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 115 分享转存机器人\n\n点选下面的功能按钮：",
        reply_markup=_main_menu(),
    )


# ══════════════════════════════════════════════
#  所有按钮点击
# ══════════════════════════════════════════════

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # ── 返回主菜单 ──
    if data == "menu_back":
        await query.edit_message_text(
            "🤖 115 分享转存机器人\n\n点选下面的功能按钮：",
            reply_markup=_main_menu(),
        )

    # ── 帮助 ──
    elif data == "menu_help":
        await query.edit_message_text(
            "📖 使用说明\n\n"
            "📋 基本用法:\n"
            "• 直接发 115 链接 → 自动转存+重命名+生成永久链接\n"
            "• 也可以点「📥 提交链接」按钮\n\n"
            "⚙️ 配置管理:\n"
            "• 点「⚙️ 查看配置」查看当前配置\n"
            "• 用 /set <KEY> <VALUE> 修改配置\n\n"
            "🔗 支持域名: 115.com / 115cdn.com / anxia.com\n"
            "📡 监听: Postedia_bot 等解锁机器人的消息",
            reply_markup=_back(),
        )

    # ── 运行状态 ──
    elif data == "menu_status":
        from pipeline import get_svc
        from config import TG_MONITOR_TARGETS
        try:
            svc = await get_svc()
            status = "✅ 正常" if svc and svc.client else "❌ 未连接"
            busy = "繁忙" if svc and svc.is_busy else "空闲"
        except Exception:
            status = "❌ 初始化失败"
            busy = "未知"
        await query.edit_message_text(
            f"📊 运行状态\n\n"
            f"115 客户端: {status}\n"
            f"处理队列: {busy}\n"
            f"监听目标: {len(TG_MONITOR_TARGETS)} 个\n"
            f"输出频道: {TG_CHANNEL_ID or '未配置'}",
            reply_markup=_back(),
        )

    # ── 查看配置 ──
    elif data == "menu_config":
        if not _is_admin(query.from_user.id):
            await query.edit_message_text("⛔ 仅管理员可查看配置", reply_markup=_back())
            return
        await query.edit_message_text(format_config_list(), reply_markup=_back())

    # ── 可配置项 ──
    elif data == "menu_setlist":
        if not _is_admin(query.from_user.id):
            await query.edit_message_text("⛔ 仅管理员可查看配置项", reply_markup=_back())
            return
        lines = ["📋 可配置项:\n"]
        for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
            val = os.environ.get(k, default)
            mark = "✅" if val else "⬜"
            lines.append(f"{mark} {k} — {desc}")
        lines.append("\n用法: /set <KEY> <VALUE>")
        await query.edit_message_text("\n".join(lines), reply_markup=_back())

    # ── 查看日志 ──
    elif data == "menu_log":
        from pathlib import Path
        log_file = Path("/data/bot.log")
        if not log_file.exists():
            await query.edit_message_text("📝 暂无日志文件", reply_markup=_back())
            return
        try:
            lines = log_file.read_text(encoding="utf-8").splitlines()
            recent = lines[-20:]
            text = f"📝 最近 {len(recent)} 条日志:\n\n" + "\n".join(recent)
            if len(text) > 3800:
                text = text[-3800:]
            await query.edit_message_text(
                f"```\n{text}\n```", parse_mode=ParseMode.MARKDOWN, reply_markup=_back(),
            )
        except Exception as e:
            await query.edit_message_text(f"读取日志失败: {e}", reply_markup=_back())

    # ── 提交链接（提示用户发送）──
    elif data == "menu_link":
        await query.edit_message_text(
            "📥 提交 115 链接\n\n"
            "请直接发送 115 分享链接给我：\n\n"
            "支持格式:\n"
            "• https://115.com/s/xxxxx\n"
            "• https://115cdn.com/s/xxxxx?password=xxx\n\n"
            "或者直接粘贴链接到对话框，我会自动识别。",
            reply_markup=_back(),
        )

    else:
        await query.edit_message_text("未知操作", reply_markup=_back())


# ══════════════════════════════════════════════
#  /link 命令
# ══════════════════════════════════════════════

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "📥 用法: /link <115分享链接>\n\n"
            "或者直接发送链接到对话框，我会自动识别。",
            reply_markup=_back(),
        )
        return
    await _handle_link(update.message, context.args[0])


# ══════════════════════════════════════════════
#  /config 命令
# ══════════════════════════════════════════════

async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令", reply_markup=_back())
        return
    await update.message.reply_text(format_config_list(), reply_markup=_back())


# ══════════════════════════════════════════════
#  /set 命令
# ══════════════════════════════════════════════

async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令", reply_markup=_back())
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "⚙️ 用法: /set <配置项> <值>\n\n"
            "示例:\n"
            "  /set P115_COOKIE 你的cookie\n"
            "  /set TG_CHANNEL_ID -1001234567890\n"
            "  /set TG_MONITOR_TARGETS postedia_bot,OtherBot\n"
            "  /set AUTO_RENAME 0\n\n"
            "点「📋 可配置项」查看所有配置",
            reply_markup=_back(),
        )
        return

    key = context.args[0].upper()
    value = " ".join(context.args[1:])
    result = set_config(key, value)

    restart_keys = {"TG_BOT_TOKEN", "TG_API_ID", "TG_API_HASH", "TG_MONITOR_TARGETS"}
    extra = "\n\n⚠️ 此配置需要重启 Bot 才能生效。" if key in restart_keys else ""

    await update.message.reply_text(f"{result}{extra}", reply_markup=_back())


# ══════════════════════════════════════════════
#  /setlist 命令
# ══════════════════════════════════════════════

async def cmd_setlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令", reply_markup=_back())
        return
    lines = ["📋 可配置项:\n"]
    for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
        val = os.environ.get(k, default)
        mark = "✅" if val else "⬜"
        lines.append(f"{mark} {k} — {desc}")
    lines.append("\n用法: /set <KEY> <VALUE>")
    await update.message.reply_text("\n".join(lines), reply_markup=_back())


# ══════════════════════════════════════════════
#  自动识别 115 链接 + 转存结果
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    links = extract_115_links(text, update.message.entities)
    if not links:
        return
    for link in links:
        await _handle_link(update.message, link["url"])


async def _handle_link(message, url: str):
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
        await status_msg.edit_text("\n".join(parts), reply_markup=_back())
        if TG_CHANNEL_ID:
            ch = f"📺 {title} [{quality}]\n💾 {size}\n🔗 {share_link}" if quality else f"📺 {title}\n🔗 {share_link}"
            await send_channel(ch)

    elif result["status"] == "pending":
        await status_msg.edit_text(
            f"⏳ {result.get('message', '处理中...')}\n"
            f"🔗 {result.get('share_link', url)}\n完成后会自动通知。",
            reply_markup=_back(),
        )
    else:
        await status_msg.edit_text(
            f"❌ 处理失败: {result.get('message', '未知错误')}",
            reply_markup=_back(),
        )


# ══════════════════════════════════════════════
#  Bot 初始化
# ══════════════════════════════════════════════

def setup_bot() -> Application:
    global _app
    app = Application.builder().token(TG_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("setlist", cmd_setlist))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _app = app
    return app
