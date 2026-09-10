"""
Telegram Bot API 交互器 - 全 InlineKeyboard 面板
"""
import os
import asyncio
import logging
from typing import Optional
from html import escape as html_escape
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
from link_parser import extract_115_links, extract_ed2k_links, parse_ed2k
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
    if not _bot or not chat_id:
        return
    try:
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"发送私聊消息失败: {e}")


async def send_channel(text: str, parse_mode: str = None):
    if not _bot:
        logger.warning("⚠️ send_channel: _bot 未初始化")
        return
    if not TG_CHANNEL_ID:
        logger.warning("⚠️ send_channel: TG_CHANNEL_ID 未设置")
        return
    try:
        target = int(TG_CHANNEL_ID) if TG_CHANNEL_ID.lstrip("-").isdigit() else TG_CHANNEL_ID
        logger.info(f"📤 正在发送到频道: {target}")
        await _bot.send_message(chat_id=target, text=text, parse_mode=parse_mode)
        logger.info(f"✅ 频道消息发送成功")
    except Exception as e:
        logger.warning(f"❌ 发送频道消息失败: {e}")



async def sync_to_jying(result: dict, chat_id: int = None):
    """同步成功的 115 转存结果到聚影，并记录结果。"""
    title = result.get("title", "")
    share_link = result.get("share_link", "")
    det = result.get("det") or {}
    year = det.get("year", "")
    tmdb_id = result.get("tmdb_id") or det.get("id")
    original_title = det.get("original_title", "") or result.get("original_title", "")

    # 打印完整 result 排除大对象
    _dbg = {k: v for k, v in result.items() if k not in ("det", "video_names")}
    logger.info(f"🔧 sync_to_jying result: {_dbg}")

    # ── 构建 filename：优先从 result 字段取 ──
    filename = " ".join(x for x in [
        result.get("episode", ""),
        result.get("quality", ""),
        result.get("source", ""),
        result.get("encode", ""),
        result.get("audio", ""),
    ] if x)

    # ── filename 兜底：如果为空，从 video_names 重新解析 ──
    if not filename:
        video_names = result.get("video_names") or []
        if video_names:
            from pipeline import parse_filename
            parsed = parse_filename(video_names[0] if video_names else "")
            filename = " ".join(x for x in [
                result.get("episode", ""),
                parsed.get("quality", ""),
                parsed.get("source", ""),
                parsed.get("encode", ""),
                parsed.get("audio", ""),
            ] if x)
            logger.info(f"🔧 filename 兜底解析: {video_names[0] if video_names else ''} -> '{filename}'")

    logger.info(f"🔧 聚影 filename 最终: '{filename}' (original_title={original_title})")

    try:
        from _jying_cache import set_last_share
        set_last_share(
            title=title, year=year, tmdb_id=tmdb_id,
            share_link=share_link, filename=filename,
        )
    except Exception as e:
        logger.warning(f"⚠️ 聚影缓存写入失败（不影响同步）: {e}")

    try:
        import jying
        if not jying._ok():
            logger.info(f"⏭️ 聚影凭证未配置，跳过自动同步: {title}")
            return {"ok": False, "skipped": True, "error": "聚影凭证未配置"}

        logger.info(f"📤 开始聚影同步: {title}")
        response = await jying.upload_resource(
            title=title, year=year, tmdb_id=tmdb_id,
            link=share_link, filename=filename,
            original_title=original_title,
        )
        if response.get("status") == "success":
            submission = response.get("submission") or {}
            status = submission.get("status", "unknown")
            submission_id = response.get("submission_id") or submission.get("id")

            # ── 审核轮询：pending_review 时等待审核结果 ──
            if status in ("pending_review", "pending") and submission_id:
                logger.info(f"⏳ 聚影审核中，开始轮询... ({title})")
                if chat_id:
                    await send_private(chat_id, f"⏳ 聚影审核中，等待结果...\n📺 {title} ({year})")
                poll_result = await jying.poll_until_reviewed(submission_id, max_wait=300, interval=15)
                status = poll_result.get("status", status)
                timed_out = poll_result.get("timed_out", False)
                if timed_out:
                    status_text = "⏳ 审核中（超时未完成）"
                elif status == "published":
                    status_text = "✅ 已发布"
                elif status == "rejected":
                    status_text = "❌ 审核拒绝"
                else:
                    status_text = status
            else:
                status_text = {"published": "✅已发布", "approved": "✅已发布"}.get(status, status)

            logger.info(f"📤 聚影同步完成: {title} (状态: {status_text})")
            if chat_id:
                detail = (
                    f"https://www.jying.top/profile/#/resource/{submission_id}"
                    if submission_id else ""
                )
                message = f"📤 聚影同步完成\n📺 {title} ({year})\n📋 状态：{status_text}"
                if detail:
                    message += f"\n📄 {detail}"
                await send_private(chat_id, message)
            return {"ok": True, "response": response, "final_status": status}

        error = response.get("message") or response.get("error") or "未知错误"
        logger.warning(f"📤 聚影同步失败: {title} | {error}")
        if chat_id:
            await send_private(chat_id, f"📤 聚影同步失败：{error}\n📺 {title}")
        return {"ok": False, "response": response, "error": error}
    except Exception as e:
        logger.warning(f"⚠️ 聚影同步异常（不影响主流程）: {title} | {e}", exc_info=True)
        if chat_id:
            await send_private(chat_id, f"📤 聚影同步异常：{e}\n📺 {title}")
        return {"ok": False, "error": str(e)}


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
        [InlineKeyboardButton("📥 提交链接", callback_data="menu_link"),
         InlineKeyboardButton("📊 运行状态", callback_data="menu_status")],
        [InlineKeyboardButton("📡 监听管理", callback_data="menu_monitor"),
         InlineKeyboardButton("📝 查看日志", callback_data="menu_log")],
        [InlineKeyboardButton("⚙️ 查看配置", callback_data="menu_config"),
         InlineKeyboardButton("📋 可配置项", callback_data="menu_setlist")],
        [InlineKeyboardButton("🤖 OpenAI", callback_data="menu_openai"),
         InlineKeyboardButton("🔄 重启 Bot", callback_data="menu_restart")],
        [InlineKeyboardButton("🛑 取消任务", callback_data="menu_cancel"),
         InlineKeyboardButton("🧹 清空回收站", callback_data="menu_clean")],
        [InlineKeyboardButton("🎬 聚影", callback_data="menu_jying"),
         InlineKeyboardButton("❓ 帮助", callback_data="menu_help")],
    ])


def _back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")]
    ])


def _status_menu() -> InlineKeyboardMarkup:
    """运行状态二级菜单：刷新 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新状态", callback_data="status_refresh")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _config_menu() -> InlineKeyboardMarkup:
    """查看配置二级菜单：跳转可配置项 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 查看可配置项", callback_data="menu_setlist")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _setlist_menu() -> InlineKeyboardMarkup:
    """可配置项二级菜单：跳转当前配置 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ 查看当前配置", callback_data="menu_config")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _log_menu() -> InlineKeyboardMarkup:
    """查看日志二级菜单：选条数。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("近 10 条", callback_data="log_10"),
         InlineKeyboardButton("近 50 条", callback_data="log_50"),
         InlineKeyboardButton("近 100 条", callback_data="log_100")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _monitor_menu() -> InlineKeyboardMarkup:
    """监听管理二级菜单：登录 / 模式 / 刷新 / 增删目标。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 登录/启动监听", callback_data="mon_login"),
         InlineKeyboardButton("🔄 刷新状态", callback_data="mon_refresh")],
        [InlineKeyboardButton("🔀 切换监听模式", callback_data="mon_mode"),
         InlineKeyboardButton("➕ 添加目标", callback_data="mon_add")],
        [InlineKeyboardButton("➖ 移除目标", callback_data="mon_remove")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _monitor_back_menu() -> InlineKeyboardMarkup:
    """监听管理的三级菜单通用「返回监听管理」按钮。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 返回监听管理", callback_data="mon_back")]
    ])


def _monitor_mode_menu(current_mode: str) -> InlineKeyboardMarkup:
    """监听模式三级菜单：私聊 / 频道。"""
    priv = "👤 私聊模式" + (" ✅" if current_mode == "private" else "")
    chan = "📢 频道模式" + (" ✅" if current_mode == "channel" else "")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(priv, callback_data="mon_mode_private")],
        [InlineKeyboardButton(chan, callback_data="mon_mode_channel")],
        [InlineKeyboardButton("🔙 返回监听管理", callback_data="mon_back")],
    ])


def _monitor_remove_menu(targets: list) -> InlineKeyboardMarkup:
    """移除目标三级菜单：动态列出当前目标。"""
    rows = [
        [InlineKeyboardButton(f"❌ {t}", callback_data=f"mon_rm_{i}")]
        for i, t in enumerate(targets)
    ]
    rows.append([InlineKeyboardButton("🔙 返回监听管理", callback_data="mon_back")])
    return InlineKeyboardMarkup(rows)


def _cancel_confirm_menu() -> InlineKeyboardMarkup:
    """取消任务二级菜单：确认 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认取消", callback_data="cancel_confirm"),
         InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _restart_confirm_menu() -> InlineKeyboardMarkup:
    """重启 Bot 二级菜单：确认 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认重启", callback_data="restart_confirm"),
         InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _clean_confirm_menu() -> InlineKeyboardMarkup:
    """清空回收站二级菜单：确认 + 返回。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认清空", callback_data="clean_confirm"),
         InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _openai_menu() -> InlineKeyboardMarkup:
    """OpenAI 设置二级菜单：四项可编辑。"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ API Base", callback_data="oa_set_LLM_API_BASE"),
         InlineKeyboardButton("✏️ API Key", callback_data="oa_set_LLM_API_KEY")],
        [InlineKeyboardButton("✏️ 模型", callback_data="oa_set_LLM_MODEL"),
         InlineKeyboardButton("✏️ 提示词", callback_data="oa_set_LLM_PROMPT")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_back")],
    ])


def _openai_text() -> str:
    """OpenAI 设置概览。"""
    base = os.getenv("LLM_API_BASE") or "https://apihub.agnes-ai.com/v1"
    key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL") or "agnes-2.5-flash"
    prompt = os.getenv("LLM_PROMPT", "").strip()
    key_disp = f"{key[:6]}***{key[-4:]}" if len(key) > 10 else ("已设置" if key else "（未设置）")
    prompt_disp = (prompt[:60] + "…") if len(prompt) > 60 else (prompt or "（内置默认）")
    return (
        "🤖 OpenAI 辅助识别设置\n\n"
        f"状态: {'✅ 已启用' if key else '⬜ 未配置 Key（走正则识别）'}\n"
        f"API Base: {base}\n"
        f"API Key: {key_disp}\n"
        f"模型: {model}\n"
        f"提示词: {prompt_disp}\n\n"
        "点下面按钮后直接回复新值即可修改（立即生效，无需重启）。"
    )


# ══════════════════════════════════════════════
#  公共文本构建（按钮与命令共用，避免重复）
# ══════════════════════════════════════════════

def _help_text() -> str:
    return (
        "📖 使用说明\n\n"
        "📋 基本用法:\n"
        "• 直接发 115 链接 → 自动转存+重命名+生成永久链接\n"
        "• 也可以点「📥 提交链接」按钮\n\n"
        "🔐 监听登录（全程 Bot 内完成）:\n"
        "• /set TG_API_ID <id> 和 /set TG_API_HASH <hash>\n"
        "• 点「📡 监听管理 → 🔐 登录/启动监听」\n"
        "• 手机号/验证码/二步验证密码直接私聊回复即可\n\n"
        "⚙️ 配置管理:\n"
        "• 点「⚙️ 查看配置」查看当前配置\n"
        "• 用 /set <KEY> <VALUE> 修改配置\n"
        "• /status 运行状态 · /log [N] 最近 N 条日志\n"
        "• /monitor 监听状态 / 增删目标 / 切模式\n"
        "• /restart 重启 Bot（主菜单也有按钮）\n"
        "• /set AUTO_DELETE_AFTER 秒数 — 发卡后自动删源文件+清回收站\n"
        "• /set RECYCLE_PASSWORD xxx — 回收站密码\n\n"
        "🔗 支持: 115.com / 115cdn.com / anxia.com 分享链接 + ed2k:// 链接\n"
        "🤖 OpenAI 识别: 主菜单「🤖 OpenAI」可换 API/Key/模型/提示词\n"
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

async def _render_monitor(query):
    """渲染监听管理二级菜单（含状态概览）。"""
    monitor = get_monitor()
    if not monitor:
        await query.edit_message_text(
            "📡 监听器未启用。\n\n"
            "点「🔐 登录/启动监听」可直接在 Bot 内完成登录（无需重启）。\n"
            "或配置 TG_API_ID / TG_API_HASH / TG_MONITOR_TARGETS 后重启。",
            reply_markup=_monitor_menu(),
        )
        return
    await query.edit_message_text(
        monitor.status_text() + "\n\n点按钮管理监听目标 / 切换模式。",
        reply_markup=_monitor_menu(),
    )


async def _auto_process_and_notify(chat_id: int, url: str, source_name: str):
    print('>>> _auto_process_and_notify ENTERED', flush=True)
    """监听到链接后的自动转存 + 卡片私聊 + 卡片频道 + 自动清理（闭环）。"""
    await send_private(chat_id, f"🔔 监听到新链接!\n来源: {source_name}\n🔗 {url[:80]}...\n⏳ 正在自动转存...")
    try:
        result = await process_link(url)
        if result["status"] == "success":
            share_link = result["share_link"]
            title = result.get("title", "")
            det = result.get("det") or {}

            # ── 海报 + 评分 + 卡片 ──
            poster = None
            douban = ""
            try:
                from identifier import douban_rating, fetch_poster_bytes
                poster = await fetch_poster_bytes(det.get("poster_url", "")) if det else None
            except Exception as e:
                logger.warning(f"⚠️ 海报下载失败（不影响转存）: {e}")
            try:
                from identifier import douban_rating as _dr
                douban = await _dr(title) if det else ""
            except Exception:
                pass

            tmdb_rating = f"{float(det['rating']):.1f}/10" if det.get("rating") else "暂无评分"
            try:
                card = _build_card(
                    title=title, year=det.get("year", ""),
                    genres=det.get("genres") or "暂无",
                    tmdb_rating=tmdb_rating,
                    douban_rating=douban or "暂无评分",
                    quality=result.get("quality", ""),
                    source=result.get("source", ""),
                    size_text=result.get("size", ""),
                    episode=result.get("episode", ""),
                    encode=result.get("encode", ""),
                    audio="",
                    link_line=f'🔗 链接：<a href="{html_escape(share_link, quote=True)}">115网盘</a>',
                    overview=det.get("overview", ""),
                )
            except Exception as e:
                logger.warning(f"⚠️ 卡片构建失败，降级为纯文本: {e}")
                card = f"✅ 自动转存成功: {title}\n🔗 {share_link}"

            # ── 私聊卡片 ──
            await _send_card_to(chat_id, card, poster=poster)
            # ── 频道卡片（闭环）──
            target = _channel_target()
            logger.warning(f"🔧 DEBUG 频道推送: target={target}, has_poster={poster is not None}, card_len={len(card)}")
            if target:
                try:
                    ch_ok = await _send_card_to(target, card, poster=poster)
                    logger.warning(f"🔧 DEBUG 频道推送结果: {ch_ok}")
                except Exception as ch_e:
                    logger.error(f"❌ 频道推送异常: {ch_e}", exc_info=True)
            # ── 安排自动清理源文件 ──
            to_cid = result.get("to_cid")
            if to_cid:
                from pipeline import schedule_cleanup
                schedule_cleanup(to_cid, name=title, share_link=share_link)
            await sync_to_jying(result, chat_id=chat_id)
            logger.info(f"✅ 自动转存成功(闭环): {title} → {share_link}")

        elif result["status"] == "pending":
            # 审核中：只私聊通知，不推频道（等审核通过后再推）
            share_link = result.get("share_link", url)
            text = f"⏳ 115审核中，已加入轮询队列（通过后自动处理）\n🔗 {share_link}"
            await send_private(chat_id, text)
        else:
            text = f"❌ 自动转存失败: {result.get('message', '未知错误')}\n🔗 {url}"
            await send_private(chat_id, text)

    except Exception as e:
        logger.error(f"自动转存异常: {e}", exc_info=True)
        await send_private(chat_id, f"❌ 自动转存异常: {e}\n🔗 {url}")


async def _start_login_flow(query, user_id: int):
    """从 Bot 内发起/继续 Telethon 登录（手机号/验证码/二步验证全部私聊完成）。"""
    if not os.getenv("TG_API_ID") or not os.getenv("TG_API_HASH"):
        await query.edit_message_text(
            "🔐 登录 TG 监听账号\n\n"
            "还缺少 TG_API_ID / TG_API_HASH，请先设置：\n"
            "/set TG_API_ID 你的api_id\n"
            "/set TG_API_HASH 你的api_hash\n\n"
            "（在 my.telegram.org 申请）\n设置完再点本按钮，无需重启。",
            reply_markup=_monitor_back_menu(),
        )
        return

    monitor = get_monitor()

    if monitor and monitor.is_running:
        await query.edit_message_text(
            "✅ 监听器已在运行，无需重复登录。\n\n" + monitor.status_text(),
            reply_markup=_monitor_menu(),
        )
        return

    login_task = getattr(monitor, "_login_task", None) if monitor else None
    if login_task and not login_task.done():
        await query.edit_message_text(
            "⏳ 登录流程已在进行中。\n\n请按我的私聊提示，直接回复手机号/验证码/二步验证密码。",
            reply_markup=_monitor_back_menu(),
        )
        return

    async def on_login_prompt(prompt: str):
        await send_private(user_id, prompt)

    if monitor is None:
        from monitor import Monitor, set_monitor

        async def on_link_found(url: str, source_name: str, event):
            logger.info(f"🔗 监听到 115 链接: {url[:60]}... (来源: {source_name})")
            await _auto_process_and_notify(user_id, url, source_name)

        try:
            monitor = Monitor(on_link_found=on_link_found, on_login_prompt=on_login_prompt)
        except Exception as e:
            await query.edit_message_text(f"❌ 初始化监听器失败: {e}", reply_markup=_monitor_back_menu())
            return
        set_monitor(monitor)
    else:
        # 复用已有实例，登录提示发给本次点击的人
        monitor.on_login_prompt = on_login_prompt

    await query.edit_message_text(
        "🔐 已发起登录流程。\n\n"
        "接下来如果需要手机号 / 验证码 / 二步验证密码，我会发消息问你，**直接回复内容即可**。\n\n"
        "完成后我会通知你。",
        reply_markup=_monitor_back_menu(),
    )

    async def _run():
        try:
            await monitor.start()
            await send_private(user_id, "✅ TG 监听账号登录成功，监听器已启动。\n\n" + monitor.status_text())
        except Exception as e:
            logger.error(f"登录流程失败: {e}")
            await send_private(user_id, f"❌ 登录失败: {e}\n\n可到「📡 监听管理 → 🔐 登录/启动监听」重试。")

    monitor._login_task = asyncio.create_task(_run())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    logger.info(f"📩 收到按钮回调: data={data!r} user={user_id}")
    try:
        await query.answer()
    except Exception as e:
        logger.warning(f"⚠️ query.answer() 失败: {e}")

    # ── 聚影菜单 ──
    if data.startswith("jy_") or data == "menu_jying":
        try:
            from jying_menu import handle_jying_callback
            if await handle_jying_callback(query, data):
                return
        except Exception as e:
            logger.error(f"聚影回调异常: {e}", exc_info=True)
            await query.edit_message_text(f"❌ 聚影功能异常: {e}", reply_markup=_back())
            return

    # ── 返回主菜单 ──
    if data == "menu_back":
        context.user_data.pop("pending_action", None)
        await query.edit_message_text(
            "🤖 115 分享转存机器人\n\n点选下面的功能按钮：",
            reply_markup=_main_menu(),
        )

    # ── 帮助 ──
    elif data == "menu_help":
        await query.edit_message_text(_help_text(), reply_markup=_back())

    # ── 运行状态（二级：刷新）──
    elif data == "menu_status":
        await query.edit_message_text(await _status_text(), reply_markup=_status_menu())

    elif data == "status_refresh":
        await query.edit_message_text(await _status_text(), reply_markup=_status_menu())

    # ── 查看配置（二级：跳转可配置项）──
    elif data == "menu_config":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可查看配置", reply_markup=_back())
            return
        await query.edit_message_text(format_config_list(), reply_markup=_config_menu())

    # ── 可配置项（二级：跳转当前配置）──
    elif data == "menu_setlist":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可查看配置项", reply_markup=_back())
            return
        lines = ["📋 可配置项:\n"]
        for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
            val = os.environ.get(k, default)
            mark = "✅" if val else "⬜"
            lines.append(f"{mark} {k} — {desc}")
        lines.append("\n用法: /set <KEY> <VALUE>")
        await query.edit_message_text("\n".join(lines), reply_markup=_setlist_menu())

    # ── 查看日志（二级选条数 → 三级展示）──
    elif data == "menu_log":
        await query.edit_message_text(
            "📝 查看日志\n\n选择查看最近多少条日志：",
            reply_markup=_log_menu(),
        )

    elif data in ("log_10", "log_50", "log_100"):
        n = int(data.split("_")[1])
        text, is_md = _log_text(n)
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.MARKDOWN if is_md else None,
            reply_markup=_log_menu(),
        )

    # ── 监听管理 ──
    elif data == "menu_monitor":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可查看监听管理", reply_markup=_back())
            return
        await _render_monitor(query)

    elif data == "mon_refresh":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        await _render_monitor(query)

    elif data == "mon_back":
        context.user_data.pop("pending_action", None)
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        await _render_monitor(query)

    # 三级：登录/启动监听（Bot 内完成手机号/验证码/二步验证）
    elif data == "mon_login":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        await _start_login_flow(query, user_id)

    # 三级：切换监听模式
    elif data == "mon_mode":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        monitor = get_monitor()
        if not monitor:
            await query.edit_message_text("📡 监听器未启用", reply_markup=_back())
            return
        await query.edit_message_text(
            f"🔀 切换监听模式\n\n当前模式: {monitor.mode}\n\n选择要切换到的模式：",
            reply_markup=_monitor_mode_menu(monitor.mode),
        )

    elif data in ("mon_mode_private", "mon_mode_channel"):
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        monitor = get_monitor()
        if not monitor:
            await query.edit_message_text("📡 监听器未启用", reply_markup=_back())
            return
        mode = "private" if data == "mon_mode_private" else "channel"
        result = await monitor.set_mode(mode)
        await query.edit_message_text(
            result + "\n\n" + monitor.status_text(), reply_markup=_monitor_menu(),
        )

    # 三级：添加目标（等待用户回复目标名）
    elif data == "mon_add":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        context.user_data["pending_action"] = "add_target"
        await query.edit_message_text(
            "➕ 添加监听目标\n\n"
            "请直接回复要监听的目标用户名（可带 @，如 postedia_bot）：\n\n"
            "我会自动添加并注册监听。点下方按钮可取消。",
            reply_markup=_monitor_back_menu(),
        )

    # 三级：移除目标（动态列出）
    elif data == "mon_remove":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        monitor = get_monitor()
        if not monitor:
            await query.edit_message_text("📡 监听器未启用", reply_markup=_back())
            return
        targets = monitor.targets
        if not targets:
            await query.edit_message_text(
                "📡 当前没有监听目标。\n\n可用 /monitor add <目标> 添加。",
                reply_markup=_monitor_back_menu(),
            )
            return
        await query.edit_message_text(
            "➖ 移除监听目标\n\n点击要移除的目标：",
            reply_markup=_monitor_remove_menu(targets),
        )

    elif data.startswith("mon_rm_"):
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        monitor = get_monitor()
        if not monitor:
            await query.edit_message_text("📡 监听器未启用", reply_markup=_back())
            return
        try:
            idx = int(data[len("mon_rm_"):])
            name = monitor.targets[idx]
        except (ValueError, IndexError):
            await query.edit_message_text("⚠️ 目标已变更，请重新进入。", reply_markup=_monitor_back_menu())
            return
        result = await monitor.remove_target(name)
        await query.edit_message_text(
            result + "\n\n" + monitor.status_text(), reply_markup=_monitor_menu(),
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

    # ── 取消任务（二级确认 → 三级执行）──
    elif data == "menu_cancel":
        if not _can_submit(user_id):
            await query.edit_message_text("⛔ 你没有操作权限", reply_markup=_back())
            return
        await query.edit_message_text(
            "🛑 取消任务\n\n确定要取消当前正在进行的转存任务吗？\n\n"
            "注意：已完成的步骤无法回退。",
            reply_markup=_cancel_confirm_menu(),
        )

    elif data == "cancel_confirm":
        if not _can_submit(user_id):
            await query.edit_message_text("⛔ 你没有操作权限", reply_markup=_back())
            return
        from pipeline import request_cancel
        request_cancel()
        await query.edit_message_text("🛑 已请求取消，进行中的步骤会尽快停止。", reply_markup=_back())

    # ── OpenAI 设置（二级：四项可编辑 → 三级回复新值）──
    elif data == "menu_openai":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可配置 OpenAI", reply_markup=_back())
            return
        await query.edit_message_text(_openai_text(), reply_markup=_openai_menu())

    elif data.startswith("oa_set_"):
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可配置 OpenAI", reply_markup=_back())
            return
        key = data[len("oa_set_"):]
        context.user_data["pending_action"] = f"setcfg:{key}"
        tips = {
            "LLM_API_BASE": "请回复新的 API Base（如 https://api.openai.com/v1）：",
            "LLM_API_KEY": "请回复新的 API Key（sk-...）：",
            "LLM_MODEL": "请回复新的模型名（如 agnes-2.0-flash / gpt-4o-mini）：",
            "LLM_PROMPT": "请回复新的提示词全文（支持多行）。\n回复「默认」两个字可恢复内置提示词。",
        }
        await query.edit_message_text(
            f"✏️ 修改 {key}\n\n{tips.get(key, '请回复新值：')}",
            reply_markup=_back(),
        )

    # ── 重启 Bot（二级确认 → 三级执行）──
    elif data == "menu_restart":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可重启", reply_markup=_back())
            return
        await query.edit_message_text(
            "🔄 重启 Bot\n\n确定要重启吗？\n\n进程退出后容器会自动拉起，几秒内恢复。",
            reply_markup=_restart_confirm_menu(),
        )

    elif data == "restart_confirm":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可重启", reply_markup=_back())
            return
        await query.edit_message_text("🔄 正在重启，几秒后恢复...\n\n恢复后发 /start 继续使用。")
        logger.info("收到重启按钮指令，退出进程以触发容器重启")
        import threading
        threading.Timer(1.5, lambda: os._exit(0)).start()

    # ── 清空回收站（二级确认 → 三级执行）──
    elif data == "menu_clean":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        pwd_set = "✅ 已设置" if os.getenv("RECYCLE_PASSWORD", "").strip() else "⬜ 未设置（/set RECYCLE_PASSWORD xxx）"
        await query.edit_message_text(
            "🧹 清空 115 回收站\n\n"
            "⚠️ 此操作会永久删除回收站内容，不可恢复！\n\n"
            f"回收站密码: {pwd_set}\n\n"
            "确定要清空吗？",
            reply_markup=_clean_confirm_menu(),
        )

    elif data == "clean_confirm":
        if not _is_admin(user_id):
            await query.edit_message_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        from pipeline import empty_recycle_bin
        ok, msg = await empty_recycle_bin()
        await query.edit_message_text(msg, reply_markup=_back())

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
    # 用原始消息文本取值：保留多行（提示词等场景），避免 context.args 把换行吃掉
    raw = (update.message.text or "").split(None, 2)
    value = raw[2] if len(raw) >= 3 else " ".join(context.args[1:])
    result = set_config(key, value)

    # TG_API_* / 监听相关配置：到「监听管理 → 登录/启动监听」立即生效，无需重启
    extra = ""
    if key == "TG_BOT_TOKEN":
        extra = "\n\n⚠️ 此配置需要重启 Bot 才能生效（主菜单有 🔄 重启按钮）。"
    elif key in ("TG_API_ID", "TG_API_HASH", "TG_PHONE", "TG_MONITOR_TARGETS", "TG_MONITOR_MODE"):
        extra = "\n\n💡 无需重启：到「📡 监听管理 → 🔐 登录/启动监听」即可生效。"

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
        if _is_admin(user_id) and monitor.provide_login_input(text):
            await update.message.reply_text("✅ 已收到，正在验证...")
        return

    # 1.5) 待处理的交互动作（添加监听目标 / 修改 OpenAI 配置）
    pending = context.user_data.get("pending_action")
    if pending == "add_target":
        context.user_data.pop("pending_action", None)
        if not _is_admin(user_id):
            await update.message.reply_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        mon = get_monitor()
        if not mon:
            await update.message.reply_text("📡 监听器未启用", reply_markup=_back())
            return
        result = await mon.add_target(text.strip())
        await update.message.reply_text(
            result + "\n\n" + mon.status_text(), reply_markup=_monitor_menu(),
        )
        return
    if pending and pending.startswith("setcfg:"):
        context.user_data.pop("pending_action", None)
        if not _is_admin(user_id):
            await update.message.reply_text("⛔ 仅管理员可操作", reply_markup=_back())
            return
        key = pending[len("setcfg:"):]
        value = text.strip()
        if key == "LLM_PROMPT" and value in ("默认", "default"):
            value = ""
        result = set_config(key, value)
        await update.message.reply_text(
            f"{result}\n\n" + _openai_text(), reply_markup=_openai_menu(),
        )
        return

    # 2) 提取链接（115 分享 + ed2k）
    links = extract_115_links(text, update.message.entities)
    ed2k_links = extract_ed2k_links(text)
    if not links and not ed2k_links:
        return

    # 3) 提交权限检查
    if not _can_submit(user_id):
        await update.message.reply_text(
            "⛔ 你没有提交链接的权限。\n"
            "请联系管理员把你的 ID 加入 TG_ALLOW_SUBMIT_IDS。",
            reply_markup=_back(),
        )
        return

    # 4) 处理链接 — 外层 catch 防止 handler 静默崩溃
    try:
        if ed2k_links:
            await _handle_ed2k(update.message, ed2k_links)
    except Exception as e:
        logger.error(f"❌ ed2k 链接处理异常: {e}", exc_info=True)
        try:
            await update.message.reply_text(f"❌ ed2k 处理出错: {e}", reply_markup=_back())
        except Exception:
            pass

    # 用后台任务处理链接，避免审核等待阻塞整个 bot
    _msg = update.message
    async def _process_all_links():
        try:
            for link in links:
                await _handle_link(_msg, link["url"])
        except Exception as e:
            logger.error(f"❌ 115 链接处理异常: {e}", exc_info=True)
            try:
                await _msg.reply_text(f"❌ 链接处理出错: {e}", reply_markup=_back())
            except Exception:
                pass
    asyncio.create_task(_process_all_links())


# ══════════════════════════════════════════════
#  影视卡片（带海报，移植自 card-bot build_card）
# ══════════════════════════════════════════════

def _build_card(title, year, genres, tmdb_rating, douban_rating, quality,
                source, size_text, episode, encode, audio, link_line, overview) -> str:
    """构造 Telegram HTML 卡片文本（发图时作为 caption，上限 1024 字节）。"""
    def H(s): return html_escape(str(s) if s is not None else "", quote=False)

    t = H(title) + (f" ({H(year)})" if year else "")
    lines = [f"🎥 {t}", ""]
    lines.append(f"🎬 类型：{H(genres) or '暂无'}")
    lines.append(f"⭐️ TMDB 评分：{H(tmdb_rating) or '暂无评分'}")
    lines.append(f"🍿 豆瓣评分：{H(douban_rating) or '暂无评分'}")
    if quality:
        lines.append(f"📺 画质：{H(quality)}")
    if source:
        lines.append(f"📼 视频：{H(source)}")
    if size_text:
        lines.append(f"💾 大小：{H(size_text)}")
    # extra 行：集数 + 画质 + 源 + 编码，<pre> 包裹带「复制」按钮
    extra = " ".join(x for x in [episode, quality, source, encode, audio] if x)
    if extra:
        lines.append(f"<pre>{H(extra)}</pre>")
    lines.append("")
    lines.append(link_line)
    if overview:
        lines.append("")
        lines.append("📖 简介：")
        ov = overview.strip()
        if len(ov) > 300:
            ov = ov[:300] + "…"
        lines.append(f"<pre>{H(ov)}</pre>")
    tags = [f"#{H(title)}"]
    if genres and genres != "暂无":
        tags += [f"#{H(g)}" for g in genres.split("、") if g]
    lines.append("")
    lines.append("🏷 标签：" + " ".join(tags))
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > 1020:
        text = text[:1010] + "…"
    return text


async def _send_card_to(chat_id, card: str, links_message: str = None,
                        poster: bytes = None) -> bool:
    """发送影视卡片（有海报发图，无海报发文），可带重试。links_message 随后单独发。"""
    if not _bot:
        return False
    for attempt, delay in enumerate((0, 3, 8), 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            if poster:
                await _bot.send_photo(chat_id, photo=poster, caption=card,
                                      parse_mode=ParseMode.HTML)
            else:
                await _bot.send_message(chat_id, card, parse_mode=ParseMode.HTML)
            if links_message:
                await _bot.send_message(chat_id, links_message, parse_mode=ParseMode.HTML)
            return True
        except Exception as e:
            logger.warning(f"卡片发送失败 chat={chat_id} 第 {attempt}/3 次: {e}")
    return False


def _channel_target():
    if not TG_CHANNEL_ID:
        return None
    return int(TG_CHANNEL_ID) if TG_CHANNEL_ID.lstrip("-").isdigit() else TG_CHANNEL_ID


async def _handle_ed2k(message, ed2k_links: list):
    """ed2k 链接：解析 → OpenAI/TMDB 识别 → 海报卡片发私聊+频道（不进 115 转存）。"""
    import re as _re
    from pipeline import parse_filename, format_size, VIDEO_EXTS
    from identifier import resolve_title, douban_rating, fetch_poster_bytes

    items = []
    for url in ed2k_links:
        try:
            items.append(parse_ed2k(url))
        except Exception as e:
            await message.reply_text(f"⚠️ ed2k 链接无效: {e}", reply_markup=_back())
            return

    # 用视频文件做识别基准（没有视频就用第一个文件）
    videos = [it for it in items if it["name"].lower().endswith(VIDEO_EXTS)]
    base = (videos or items)[0]
    parsed = parse_filename(base["name"])
    _se = _re.search(r"[Ss](\d{1,2})[Ee]\d{1,3}", base["name"])
    season = int(_se.group(1)) if _se else None

    status_msg = await message.reply_text(f"🔍 识别 ed2k 资源: {base['name'][:60]}...")

    try:
        ident = await resolve_title(
            base["name"], parsed["title"], parsed.get("year", ""), season,
            parsed.get("tmdb_id"),
        )
    except Exception as e:
        logger.warning(f"ed2k 识别失败: {e}")
        ident = {"title": parsed["title"], "year": parsed.get("year", ""), "tmdb_id": None, "det": {}}

    det = ident.get("det") or {}
    title = ident["title"]
    year = ident.get("year", "")

    # 海报和评分独立 catch，不互相影响
    poster = None
    try:
        poster = await fetch_poster_bytes(det.get("poster_url", "")) if det else None
    except Exception as e:
        logger.warning(f"⚠️ ed2k 海报下载失败: {e}")

    douban = ""
    try:
        douban = await douban_rating(title) if det else ""
    except Exception as e:
        logger.warning(f"⚠️ ed2k 豆瓣评分失败: {e}")

    total = sum(it["size"] for it in items)
    # 多集合并：S01E01-E12
    episodes = sorted(
        int(m.group(1)) for x in items
        if (m := _re.search(r"[Ss]\d{1,2}[Ee](\d{1,3})", x["name"]))
    )
    episode_text = parsed["episode"]
    if len(episodes) > 1:
        _sm = _re.search(r"[Ss](\d{1,2})[Ee]", base["name"])
        episode_text = f"S{int(_sm.group(1)):02d}E{episodes[0]:02d}-E{episodes[-1]:02d}" if _sm else parsed["episode"]

    tmdb_rating = f"{float(det['rating']):.1f}/10" if det.get("rating") else "暂无评分"
    try:
        card = _build_card(
            title=title, year=year,
            genres=det.get("genres") or "暂无",
            tmdb_rating=tmdb_rating,
            douban_rating=douban or "暂无评分",
            quality=parsed["quality"], source=parsed["source"],
            size_text=format_size(total), episode=episode_text,
            encode=parsed["encode"], audio="",
            link_line=f"🔗 ED2K 链接：共 {len(items)} 个文件（下方发完整链接）",
            overview=det.get("overview", ""),
        )
    except Exception as e:
        logger.warning(f"⚠️ ed2k 卡片构建失败，降级为纯文本: {e}")
        card = f"🎬 {title} ({year})\n🔗 ED2K 链接：共 {len(items)} 个文件"

    links_block = "\n".join(
        f"{i}. {html_escape(u, quote=False)}" for i, u in enumerate(ed2k_links, 1)
    )
    links_message = (
        f"📎 {title}｜完整 ED2K 链接（{len(ed2k_links)}个）：\n<pre>{links_block}</pre>"
    )

    # 私聊（回复给提交者）+ 频道分别发送，各自 catch
    try:
        private_ok = await _send_card_to(message.chat.id, card, links_message, poster)
    except Exception as e:
        logger.error(f"❌ ed2k 私聊卡片发送异常: {e}", exc_info=True)
        private_ok = False

    channel_ok = False
    try:
        target = _channel_target()
        channel_ok = await _send_card_to(target, card, links_message, poster) if target else False
    except Exception as e:
        logger.error(f"❌ ed2k 频道卡片发送异常: {e}", exc_info=True)

    try:
        await status_msg.edit_text(
            f"{'✅' if private_ok else '⚠️'} ed2k 卡片发送完成（未转存）\n"
            f"私聊：{'成功' if private_ok else '失败'}"
            + (f"｜频道：{'成功' if channel_ok else '失败'}" if target else "｜频道：未配置"),
            reply_markup=_back(),
        )
    except Exception:
        pass


async def _handle_link(message, url: str):
    """处理 115 分享链接：转存 → 重命名 → 创建分享 → 发送卡片。全程 try/except 防止静默崩溃。"""
    status_msg = await message.reply_text(f"⏳ 收到链接，开始处理...\n🔗 {url[:60]}...")

    async def on_progress(text: str):
        try:
            await status_msg.edit_text(f"{text}\n🔗 {url[:60]}...")
        except Exception:
            pass

    # ── process_link 本身也要 catch ──
    try:
        result = await process_link(url, on_progress=on_progress)
    except Exception as e:
        logger.error(f"❌ process_link 异常: {e}", exc_info=True)
        try:
            await status_msg.edit_text(
                f"❌ 处理异常: {e}\n🔗 {url[:60]}...", reply_markup=_back(),
            )
        except Exception:
            pass
        return

    if result["status"] == "success":
        share_link = result["share_link"]
        title = result.get("title", "")
        det = result.get("det") or {}

        # ── 海报 + 评分 + 卡片（每一步独立 catch，任一失败不影响已成功的转存）──
        poster = None
        douban = ""
        try:
            from identifier import douban_rating, fetch_poster_bytes
            poster = await fetch_poster_bytes(det.get("poster_url", "")) if det else None
        except Exception as e:
            logger.warning(f"⚠️ 海报下载失败（不影响转存）: {e}")

        try:
            from identifier import douban_rating as _dr
            douban = await _dr(title) if det else ""
        except Exception as e:
            logger.warning(f"⚠️ 豆瓣评分获取失败（不影响转存）: {e}")

        tmdb_rating = f"{float(det['rating']):.1f}/10" if det.get("rating") else "暂无评分"
        try:
            card = _build_card(
                title=title, year=det.get("year", ""),
                genres=det.get("genres") or "暂无",
                tmdb_rating=tmdb_rating,
                douban_rating=douban or "暂无评分",
                quality=result.get("quality", ""), source=result.get("source", ""),
                size_text=result.get("size", ""), episode=result.get("episode", ""),
                encode=result.get("encode", ""), audio="",
                link_line=f'🔗 链接：<a href="{html_escape(share_link, quote=True)}">115网盘</a>',
                overview=det.get("overview", ""),
            )
        except Exception as e:
            logger.warning(f"⚠️ 卡片构建失败，降级为纯文本: {e}")
            card = f"✅ 转存成功: {title}\n🔗 永久分享: {share_link}"

        # 先更新状态消息
        try:
            await status_msg.edit_text(
                f"✅ 转存成功: {title}\n🔗 永久分享: {share_link}", reply_markup=_back(),
            )
        except Exception as e:
            logger.warning(f"⚠️ 更新状态消息失败: {e}")

        # 发送卡片（私聊）
        try:
            private_ok = await _send_card_to(message.chat.id, card, poster=poster)
        except Exception as e:
            logger.error(f"❌ 私聊卡片发送异常: {e}", exc_info=True)
            private_ok = False

        # 发送卡片（频道）
        channel_ok = False
        try:
            target = _channel_target()
            logger.warning(f"🔧 DEBUG 频道推送: target={target}, has_poster={poster is not None}, card_len={len(card)}")
            channel_ok = await _send_card_to(target, card, poster=poster) if target else private_ok
            logger.warning(f"🔧 DEBUG 频道推送结果: {channel_ok}")
        except Exception as e:
            logger.error(f"❌ 频道卡片发送异常: {e}", exc_info=True)

        # 发卡成功后：AUTO_DELETE_AFTER>0 时安排自动删除源文件 + 清空回收站
        try:
            if channel_ok:
                from pipeline import schedule_cleanup
                schedule_cleanup(result.get("to_cid"), title, share_link)
        except Exception as e:
            logger.warning(f"⚠️ 安排清理任务失败（不影响转存）: {e}")

        # auto sync to jying
        try:
            await sync_to_jying(result, chat_id=message.chat.id)
        except Exception as e:
            logger.warning(f"Jying sync error (non-fatal): {e}")

    elif result["status"] == "pending":
        try:
            await status_msg.edit_text(
                f"⏳ {result.get('message', '处理中...')}\n"
                f"🔗 {result.get('share_link', url)}\n完成后会自动通知。",
                reply_markup=_back(),
            )
        except Exception:
            pass
    elif result["status"] == "cancelled":
        try:
            await status_msg.edit_text("🛑 任务已取消", reply_markup=_back())
        except Exception:
            pass
    else:
        try:
            await status_msg.edit_text(
                f"❌ 处理失败: {result.get('message', '未知错误')}",
                reply_markup=_back(),
            )
        except Exception:
            pass


# ══════════════════════════════════════════════
#  Bot 初始化
# ══════════════════════════════════════════════

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理：记录日志并尽量给用户可见反馈。"""
    logger.error(f"❌ 处理更新出错: {context.error}", exc_info=context.error)
    try:
        if isinstance(update, Update):
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    f"❌ 操作出错: {context.error}", reply_markup=_back(),
                )
            elif update.message:
                await update.message.reply_text(
                    f"❌ 处理出错: {context.error}", reply_markup=_back(),
                )
    except Exception:
        pass


def setup_bot() -> Application:
    global _app
    app = Application.builder().token(TG_BOT_TOKEN).build()

    # 全局错误处理：捕获所有 handler 未处理的异常，给用户可见反馈
    app.add_error_handler(_error_handler)

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


# ── 恢复审核中的链接轮询（启动时调用）──
async def _recover_pending_tasks():
    """从数据库恢复审核中的链接，启动无限期轮询"""
    import sys
    sys.path.insert(0, "/app")
    try:
        from app.core.database import async_session
        from app.models.schema import PendingLink
        from sqlalchemy import select
    except ImportError:
        logger.warning("⚠️ 无法导入 pending_links 模型，跳过恢复")
        return

    try:
        async with async_session() as session:
            result = await session.execute(
                select(PendingLink).where(
                    PendingLink.status.in_(["auditing", "snapshotting", "restricted", "no_space"])
                )
            )
            tasks = result.scalars().all()
            if not tasks:
                logger.info("📭 无待恢复的审核链接")
                return

            logger.info(f"📋 发现 {len(tasks)} 条待恢复的审核链接")
            for task in tasks:
                pending_info = {
                    "share_url": task.share_url,
                    "metadata": task.metadata_json,
                    "db_id": task.id,
                    "reason": task.status,
                }
                logger.info(f"  ↳ {task.share_url} (状态: {task.status})")
                # 创建 MockMessage 用于回复
                import asyncio
                asyncio.create_task(_recovered_poll_task(pending_info))
    except Exception as e:
        logger.error(f"❌ 恢复 pending tasks 异常: {e}", exc_info=True)


async def _recovered_poll_task(pending_info: dict):
    """恢复轮询任务（静默模式，不发消息给用户）"""
    import asyncio
    import sys
    sys.path.insert(0, "/app")
    try:
        from app.services.p115 import P115Service
        from app.core.config import settings
    except ImportError:
        logger.warning("⚠️ 无法导入 p115 服务，跳过恢复")
        return

    share_url = pending_info["share_url"]
    metadata = pending_info.get("metadata", {})
    reason = pending_info.get("reason", "auditing")

    logger.info(f"🔄 开始恢复轮询: {share_url} (原因: {reason})")

    # 间隔：审核中 5 分钟，其他 30 分钟
    if reason == "auditing":
        interval = 300
    elif reason == "snapshotting":
        interval = 1800
    elif reason == "restricted":
        interval = 3600
    else:
        interval = 300

    attempt = 0
    while True:
        attempt += 1
        await asyncio.sleep(interval)

        try:
            # 获取 share status
            from app.services.p115 import p115_service
            status_info = await p115_service.get_share_status(share_url)

            if status_info is None:
                logger.warning(f"⚠️ 无法获取状态，继续轮询: {share_url}")
                continue

            logger.info(f"🔄 恢复轮询第 {attempt} 次: {share_url} -> state={status_info.get('share_state')}, auditing={status_info.get('is_auditing')}, pending={status_info.get('is_pending')}")

            if status_info.get("is_prohibited"):
                logger.warning(f"⚠️ 链接违规: {share_url}")
                continue

            if status_info.get("is_expired"):
                logger.warning(f"⏰ 链接过期: {share_url}")
                # 删除 pending 记录
                try:
                    from app.core.database import async_session
                    from app.models.schema import PendingLink
                    from sqlalchemy import delete as sql_delete
                    async with async_session() as session:
                        await session.execute(sql_delete(PendingLink).where(PendingLink.id == pending_info.get("db_id")))
                        await session.commit()
                except Exception:
                    pass
                return

            if status_info.get("is_prohibited"):
                # 链接违规，停止轮询
                logger.warning(f"⚠️ 链接违规，停止轮询: {share_url}")
                try:
                    from app.core.database import async_session
                    from app.models.schema import PendingLink
                    from sqlalchemy import delete as sql_delete
                    async with async_session() as session:
                        await session.execute(sql_delete(PendingLink).where(PendingLink.id == pending_info.get("db_id")))
                        await session.commit()
                except Exception:
                    pass
                # 通知用户
                tg_user_id = settings.TG_USER_ID
                if tg_user_id:
                    from notifier import send_private
                    await send_private(int(tg_user_id), f"❌ 链接违规，已停止轮询\n🔗 {share_url}")
                return

            if not status_info.get("is_pending"):
                # 审核通过！开始处理
                logger.info(f"🎉 审核通过，开始处理: {share_url}")

                # 获取 TG_USER_ID 来发送通知
                tg_user_id = settings.TG_USER_ID
                if tg_user_id:
                    from notifier import send_private
                    await send_private(int(tg_user_id), f"✅ 115审核通过！开始处理...\n🔗 {share_url}")

                # 调用完整的处理流程
                try:
                    from notifier import _auto_process_and_notify
                    chat_id = int(tg_user_id) if tg_user_id else None
                    await _auto_process_and_notify(chat_id, share_url, "恢复轮询")
                except Exception as e:
                    logger.error(f"❌ 恢复后处理异常: {e}", exc_info=True)
                    if tg_user_id:
                        from notifier import send_private
                        await send_private(int(tg_user_id), f"❌ 处理失败: {e}\n🔗 {share_url}")

                # 删除 pending 记录
                try:
                    from app.core.database import async_session
                    from app.models.schema import PendingLink
                    from sqlalchemy import delete as sql_delete
                    async with async_session() as session:
                        await session.execute(sql_delete(PendingLink).where(PendingLink.id == pending_info.get("db_id")))
                        await session.commit()
                except Exception:
                    pass
                return

        except Exception as e:
            logger.error(f"❌ 恢复轮询异常: {e}", exc_info=True)
