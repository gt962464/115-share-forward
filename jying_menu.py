"""聚影 Bot 菜单 — 纯新增文件，不修改原有函数。"""
import html
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger("jying_menu")


# ══════════════════════════════════════════════
#  菜单构建
# ══════════════════════════════════════════════

def jying_main_menu():
    """聚影一级菜单"""
    kb = [
        [InlineKeyboardButton("📤 同步到聚影", callback_data="jy_sync"),
         InlineKeyboardButton("📋 同步记录", callback_data="jy_records")],
        [InlineKeyboardButton("🔍 搜索聚影", callback_data="jy_search"),
         InlineKeyboardButton("✅ 每日签到", callback_data="jy_checkin")],
        [InlineKeyboardButton("📊 签到状态", callback_data="jy_stats")],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_main")],
    ]
    return InlineKeyboardMarkup(kb)


def jying_search_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 继续搜索", callback_data="jy_search")],
        [InlineKeyboardButton("🔙 返回聚影", callback_data="menu_jying")],
    ])


# ══════════════════════════════════════════════
#  回调处理（供 notifier 注册）
# ══════════════════════════════════════════════

async def handle_jying_callback(query, data: str) -> bool:
    """处理 jy_ 前缀的回调，返回 True 表示已处理。"""
    user_id = query.from_user.id

    if data == "menu_jying":
        await query.edit_message_text("🎬 聚影功能", reply_markup=jying_main_menu())
        return True

    if data == "jy_sync":
        await _handle_sync(query)
        return True

    if data == "jy_records":
        await _handle_records(query)
        return True

    if data == "jy_search":
        await _handle_search_prompt(query)
        return True

    if data == "jy_checkin":
        await _handle_checkin(query)
        return True

    if data == "jy_stats":
        await _handle_stats(query)
        return True

    return False


# ══════════════════════════════════════════════
#  各按钮逻辑
# ══════════════════════════════════════════════

async def _handle_sync(query):
    """同步到聚影 — 使用最近一次转存的信息，结果发私聊通知"""
    user_id = query.from_user.id
    await query.edit_message_text("⏳ 正在同步到聚影...")
    try:
        import jying
        from _jying_cache import get_last_share
        last = get_last_share()
        if not last:
            await query.edit_message_text(
                "⚠️ 暂无最近转存记录\n\n请先通过 bot 转存一个115链接，再点同步。",
                reply_markup=jying_search_back()
            )
            return

        result = await jying.upload_resource(
            title=last["title"],
            year=last["year"],
            tmdb_id=last.get("tmdb_id"),
            link=last["share_link"],
            filename=last.get("filename", ""),
        )

        # 按钮消息改为完成状态
        await query.edit_message_text("✅ 同步操作已完成", reply_markup=jying_search_back())

        # 构建私聊通知消息（合并成一条）
        chat_id = user_id
        try:
            from notifier import send_private
        except ImportError:
            send_private = None

        if result.get("status") == "success":
            submission = result.get("submission", {})
            status = submission.get("status", "unknown")
            status_text = {"approved": "✅ 已发布", "pending": "⏳ 审核中", "rejected": "❌ 已拒绝"}.get(status, f"❓ {status}")

            lines = [
                "📤 聚影同步结果",
                "",
                f"📺 {last['title']} ({last['year']})",
                f"🔗 {last['share_link']}",
                f"📋 状态：{status_text}",
            ]
            if submission.get("id"):
                lines.append(f"📄 详情：https://www.jying.top/profile/#/resource/{submission['id']}")
            if submission.get("tmdb_id"):
                lines.append(f"🎬 TMDB：https://www.themoviedb.org/movie/{submission['tmdb_id']}")
            notify_text = "\n".join(lines)
        else:
            notify_text = (
                f"📤 聚影同步结果\n\n"
                f"❌ 同步失败：{result.get('message', '未知错误')}\n"
                f"📺 {last['title']} ({last['year']})\n"
                f"🔗 {last['share_link']}"
            )

        if send_private:
            await send_private(chat_id, notify_text)
        else:
            from telegram import Bot
            from config import TG_BOT_TOKEN
            bot = Bot(TG_BOT_TOKEN)
            await bot.send_message(chat_id, notify_text)

    except Exception as e:
        logger.error(f"聚影同步回调异常: {e}", exc_info=True)
        await query.edit_message_text(f"❌ 同步异常: {e}", reply_markup=jying_search_back())


async def _handle_records(query):
    """查看上传记录"""
    await query.edit_message_text("⏳ 查询中...")
    try:
        import jying
        result = await jying.submissions()
        if result.get("status") != "success":
            await query.edit_message_text(
                f"❌ 查询失败: {result.get('message', '未知错误')}",
                reply_markup=jying_search_back()
            )
            return

        items = result.get("submissions", result.get("results", []))
        if not items:
            await query.edit_message_text(
                "📋 暂无上传记录",
                reply_markup=jying_search_back()
            )
            return

        lines = ["📋 上传记录：\n"]
        for item in items[:10]:
            title = item.get("title", "未知")
            status = item.get("status", "unknown")
            icon = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(status, "❓")
            lines.append(f"{icon} {title} — {status}")
        if len(items) > 10:
            lines.append(f"\n... 共 {len(items)} 条")

        await query.edit_message_text("\n".join(lines), reply_markup=jying_search_back())

    except Exception as e:
        logger.error(f"聚影记录查询异常: {e}", exc_info=True)
        await query.edit_message_text(f"❌ 查询异常: {e}", reply_markup=jying_search_back())


async def _handle_search_prompt(query):
    """搜索 — 提示输入关键词"""
    await query.edit_message_text(
        "🔍 请输入搜索关键词：\n\n仅返回115网盘资源",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 返回聚影", callback_data="menu_jying")],
        ])
    )


async def handle_search_input(update, keyword: str) -> bool:
    """处理搜索输入，返回 True 表示已处理。"""
    try:
        import jying
        result = await jying.search(keyword)
        if result.get("status") != "success":
            await update.message.reply_text(f"❌ 搜索失败: {result.get('message', '未知错误')}")
            return True

        movies = result.get("movies", [])
        resources = result.get("resources", [])
        summary = result.get("summary", {})

        if not movies and not resources:
            await update.message.reply_text(f"🔍 未找到「{keyword}」相关资源", reply_markup=jying_search_back())
            return True

        lines = [f"🔍 搜索「{keyword}」结果：\n"]
        lines.append(f"📺 影片: {summary.get('movies', 0)} 部 | 资源: {summary.get('resources', 0)} 条（仅115）\n")

        for m in movies[:5]:
            year = m.get("release_year", "")
            rcount = m.get("resource_count", 0)
            lines.append(f"🎬 {m.get('title', '')} ({year}) — {rcount} 条资源")

        if resources:
            lines.append("\n📁 115资源：")
            for r in resources[:5]:
                title = r.get("title", r.get("movie_title", "未知"))
                link = r.get("link", r.get("share_link", ""))
                lines.append(f"  • {title}")
                if link:
                    lines.append(f"    🔗 {link[:60]}")

        await update.message.reply_text("\n".join(lines), reply_markup=jying_search_back())

    except Exception as e:
        logger.error(f"聚影搜索异常: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 搜索异常: {e}")

    return True


async def _handle_checkin(query):
    """手动签到"""
    await query.edit_message_text("⏳ 签到中...")
    try:
        import jying
        result = await jying.checkin()
        if result.get("status") == "success":
            pts = result.get("points_awarded", 0)
            days = result.get("my_total_days", 0)
            msg = (
                f"✅ 签到成功！\n\n"
                f"🎁 获得 {pts} 积分\n"
                f"📅 累计签到 {days} 天\n"
                f"👥 今日签到人数: {result.get('today_checkin_count', 0)}\n"
                f"⏰ 下次重置: {result.get('next_reset_at', '')}"
            )
        else:
            msg = f"❌ 签到失败: {result.get('message', '未知错误')}"
        await query.edit_message_text(msg, reply_markup=jying_search_back())

    except Exception as e:
        logger.error(f"聚影签到回调异常: {e}", exc_info=True)
        await query.edit_message_text(f"❌ 签到异常: {e}", reply_markup=jying_search_back())


async def _handle_stats(query):
    """签到状态"""
    await query.edit_message_text("⏳ 查询中...")
    try:
        import jying
        result = await jying.checkin_stats()
        if result.get("status") == "success":
            checked = "✅ 已签到" if result.get("checked_today") else "❌ 未签到"
            msg = (
                f"📊 签到状态\n\n"
                f"📅 今日状态: {checked}\n"
                f"📆 累计天数: {result.get('my_total_days', 0)} 天\n"
                f"🎁 奖励积分: {result.get('reward_points', 0)}\n"
                f"👥 今日签到: {result.get('today_checkin_count', 0)} 人\n"
                f"⏰ 下次重置: {result.get('next_reset_at', '')}"
            )
        else:
            msg = f"❌ 查询失败: {result.get('message', '未知错误')}"
        await query.edit_message_text(msg, reply_markup=jying_search_back())

    except Exception as e:
        logger.error(f"聚影统计回调异常: {e}", exc_info=True)
        await query.edit_message_text(f"❌ 查询异常: {e}", reply_markup=jying_search_back())
