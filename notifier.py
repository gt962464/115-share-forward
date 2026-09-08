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
    TG_BOT_TOKEN, TG_CHANNEL_ID, TG_ADMIN_IDS, TG_ALLOW_SUBMIT_IDS,
    set_config, get_config, format_config_list,
    CONFIG_SCHEMA,
)
from link_parser import extract_115_links
from pipeline import process_link, format_size
from monitor import get_monitor

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


def _can_submit(user_id: int) -> bool:
    """是否允许提交链接：管理员 + 白名单。"""
    return _is_admin(user_id) or str(user_id) in TG_ALLOW_SUBMIT_IDS


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
#  公共文本构建（按钮与命令共用，避免重复）
# ══════════════════════════════════════════════

def _help_text() -> str:
    return (
        "📖 使用说明\n\n"
        "📋 基本用法:\n"
        "• 直接发 115 链接 → 自动转存+重命名+生成永久链接\n"
        "• 也可以点「📥 提交链接」按钮\n\n"
        "⚙️ 配置管理:\n"
        "• 点「⚙️ 查看配置」查看当前配置\n"
        "• 用 /set <KEY> <VALUE> 修改配置\n"
        "• /status 运行状态 · /log [N] 最近 N 条日志\n"
        "• /monitor 监听状态 / 增删目标 / 切模式\n\n"
        "🔗 支持域名: 115.com / 115cdn.com / anxia.com\n"
        "📡 监听: Postedia_bot 等解锁机器人的消息"
    )


async def _status_text() -> str:
    from pipeline import get_svc
    from config import TG_MONITOR_TARGETS
    try:
        svc = await get_svc()
        status = "✅ 正常" if svc and svc.client else "❌ 未连接"
        busy = "繁忙" if svc and svc.is_busy else "空闲"
    except Exception:
        status = "❌ 初始化失败"
        busy = "未知"
    return (
        "📊 运行状态\n\n"
        f"115 客户端: {status}\n"
        f"处理队列: {busy}\n"
        f"监听目标: {len(TG_MONITOR_TARGETS)} 个\n"
        f"输出频道: {TG_CHANNEL_ID or '未配置'}"
    )


def _log_text(n: int = 20) -> tuple:
    """读取最近 n 条日志。返回 (text, use_markdown)。"""
    from pathlib import Path
    log_file = Path(os.getenv("LOG_FILE", "/data/bot.log"))
    if not log_file.exists():
        return "📝 暂无日志文件", False
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:]
        body = "\n".join(recent).replace("```", "``")
        text = f"📝 最近 {len(recent)} 条日志:\n\n```\n{body}\n```"
        if len(text) > 3800:
            text = text[-3800:]
        return text, True
    except Exception as e:
        return f"读取日志失败: {e}", False


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
        await query.edit_message_text(_help_text(), reply_markup=_back())

    # ── 运行状态 ──
    elif data == "menu_status":
        await query.edit_message_text(await _status_text(), reply_markup=_back())

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
        text, is_md = _log_text()
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN if is_md else None, reply_markup=_back(),
        )

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
#  /help /status /log /stats /restart 命令
# ══════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), reply_markup=_back())


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await _status_text(), reply_markup=_back())


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = 20
    if context.args:
        try:
            n = max(1, min(int(context.args[0]), 200))
        except ValueError:
            pass
    text, is_md = _log_text(n)
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN if is_md else None, reply_markup=_back(),
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from config import TG_MONITOR_TARGETS, AUTO_RENAME
    from pipeline import get_svc
    try:
        svc = await get_svc()
        connected = bool(svc and svc.client)
    except Exception:
        connected = False
    lines = [
        "📈 统计信息\n",
        f"115 客户端: {'✅ 已连接' if connected else '❌ 未连接'}",
        f"监听目标数: {len(TG_MONITOR_TARGETS)}",
        f"输出频道: {TG_CHANNEL_ID or '未配置'}",
        f"自动重命名: {'开' if AUTO_RENAME else '关'}",
    ]
    await update.message.reply_text("\n".join(lines), reply_markup=_back())


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令", reply_markup=_back())
        return
    await update.message.reply_text("🔄 正在重启，几秒后恢复...")
    logger.info("收到 /restart 命令，退出进程以触发容器重启")
    import threading
    threading.Timer(1.5, lambda: os._exit(0)).start()


async def cmd_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可使用此命令", reply_markup=_back())
        return

    monitor = get_monitor()
    if not monitor:
        await update.message.reply_text(
            "📡 监听器未启用。\n需在 .env 配置 TG_API_ID / TG_API_HASH / TG_MONITOR_TARGETS 后重启。",
            reply_markup=_back(),
        )
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(monitor.status_text(), reply_markup=_back())
        return

    sub = args[0].lower()
    if sub in ("add", "+"):
        if len(args) < 2:
            await update.message.reply_text("用法: /monitor add <目标用户名或频道>", reply_markup=_back())
            return
        result = await monitor.add_target(args[1])
        await update.message.reply_text(result, reply_markup=_back())
    elif sub in ("remove", "rm", "-"):
        if len(args) < 2:
            await update.message.reply_text("用法: /monitor remove <目标>", reply_markup=_back())
            return
        result = await monitor.remove_target(args[1])
        await update.message.reply_text(result, reply_markup=_back())
    elif sub == "mode":
        if len(args) < 2:
            await update.message.reply_text("用法: /monitor mode <private|channel>", reply_markup=_back())
            return
        result = await monitor.set_mode(args[1])
        await update.message.reply_text(result, reply_markup=_back())
    else:
        await update.message.reply_text(
            "用法:\n"
            "/monitor — 查看监听状态\n"
            "/monitor add <目标> — 添加监听目标\n"
            "/monitor remove <目标> — 移除监听目标\n"
            "/monitor mode <private|channel> — 切换监听模式",
            reply_markup=_back(),
        )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _can_submit(update.effective_user.id):
        await update.message.reply_text("⛔ 你没有操作权限", reply_markup=_back())
        return
    from pipeline import request_cancel
    request_cancel()
    await update.message.reply_text("🛑 已请求取消，进行中的步骤会尽快停止。", reply_markup=_back())


# ══════════════════════════════════════════════
#  自动识别 115 链接 + 转存结果
# ══════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text or ""

    # 1) 验证码/密码填入回路（监听器等待登录输入时，直接吃掉这条消息）
    monitor = get_monitor()
    if monitor and monitor.is_waiting_login:
        if monitor.provide_login_input(text):
            await update.message.reply_text("✅ 已收到，正在验证...")
        return

    # 2) 提取链接
    links = extract_115_links(text, update.message.entities)
    if not links:
        return

    # 3) 提交权限检查
    if not _can_submit(user_id):
        await update.message.reply_text(
            "⛔ 你没有提交链接的权限。\n"
            "请联系管理员把你的 ID 加入 TG_ALLOW_SUBMIT_IDS。",
            reply_markup=_back(),
        )
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
    elif result["status"] == "cancelled":
        await status_msg.edit_text("🛑 任务已取消", reply_markup=_back())
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
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("monitor", cmd_monitor))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    _app = app
    return app
