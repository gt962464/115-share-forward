"""审核违规时用英文名重命名重试分享"""
import logging
import asyncio
import re

_log = logging.getLogger("pipeline")

async def _en_retry_share(svc, pipeline_module, save_res, to_cid, display_title, year, tmdb_id, ident_det, video_names, parsed):
    """审核违规后用英文名重命名并重新分享。返回新的share_res或None。"""
    english_name = (ident_det.get("original_name") or ident_det.get("original_title") or "").strip()
    if not english_name:
        _log.warning("⚠️ 无英文原名，跳过英文重试")
        return None

    _log.info(f"🔄 审核违规，尝试英文名重试: {english_name}")

    # 重命名顶层目录为英文名
    folder_name = f"{english_name} ({year}) Cxuan" if year else f"{english_name} Cxuan"
    try:
        r = await svc.client.fs_rename((to_cid, folder_name), async_=True)
        if (r or {}).get("state"):
            _log.info(f"✅ 已重命名为英文名: {folder_name}")
        else:
            _log.warning(f"⚠️ 英文重命名失败: {r}")
            return None
    except Exception as e:
        _log.warning(f"⚠️ 英文重命名异常: {e}")
        return None

    # 更新 save_res 里的 names
    if isinstance(save_res, dict):
        save_res["names"] = [folder_name]

    # 重新创建分享
    new_share = await svc.create_share_link(save_res)
    if not (isinstance(new_share, str) and new_share.startswith("http")):
        _log.warning(f"⚠️ 英文名重试分享失败: {new_share}")
        return None

    _log.info(f"🔗 英文名新分享已创建: {new_share}")

    # 再次等待审核
    audit2 = await pipeline_module._wait_share_audit(svc, new_share)
    if isinstance(audit2, dict) and audit2.get("ok"):
        _log.info(f"✅ 英文名分享审核通过: {new_share}")
        return new_share

    _log.warning(f"❌ 英文名分享仍被审核拒绝: {audit2}")
    return None

_log.info("✅ fix_en_share loaded: 审核违规时英文名重试")
