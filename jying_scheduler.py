"""聚影自动签到调度器 — 纯新增文件，通过 APScheduler 调度。"""
import logging
from datetime import datetime

logger = logging.getLogger("jying_scheduler")


async def auto_checkin():
    """每天自动签到，由 APScheduler 调用。"""
    try:
        import jying
        result = await jying.checkin()
        if result.get("status") == "success":
            logger.info(
                f"✅ 聚影自动签到成功: +{result.get('points_awarded', 0)}积分, "
                f"累计{result.get('my_total_days', 0)}天"
            )
        else:
            logger.warning(f"聚影自动签到失败: {result.get('message', '未知错误')}")
    except Exception as e:
        logger.error(f"聚影自动签到异常: {e}")
