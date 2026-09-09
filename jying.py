"""聚影 API 封装 — 纯新增文件，不依赖原有代码。"""
import os
import logging
import httpx

logger = logging.getLogger("jying")

APP_ID = os.getenv("JYING_APP_ID", "").strip()
APP_KEY = os.getenv("JYING_APP_KEY", "").strip()
BASE = "https://www.jying.top/api/dev"


def _headers():
    return {
        "X-App-Id": APP_ID,
        "X-App-Key": APP_KEY,
    }


def _ok():
    return bool(APP_ID and APP_KEY)


# ── 签到 ──
async def checkin():
    """POST /checkin/do/"""
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{BASE}/checkin/do/", headers=_headers())
            return r.json()
    except Exception as e:
        logger.error(f"聚影签到异常: {e}")
        return {"ok": False, "error": str(e)}


# ── 签到统计 ──
async def checkin_stats():
    """GET /checkin/stats/"""
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/checkin/stats/", headers=_headers())
            return r.json()
    except Exception as e:
        logger.error(f"聚影签到统计异常: {e}")
        return {"ok": False, "error": str(e)}


# ── 搜索 ──
async def search(keyword: str):
    """GET /search/aggregate/?q=keyword"""
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{BASE}/search/aggregate/", headers=_headers(), params={"q": keyword})
            data = r.json()
            if data.get("status") != "success":
                return data
            # 只保留115链接资源
            resources = data.get("resources", [])
            filtered = [res for res in resources if _is_115_resource(res)]
            data["resources"] = filtered
            data["summary"]["resources"] = len(filtered)
            return data
    except Exception as e:
        logger.error(f"聚影搜索异常: {e}")
        return {"ok": False, "error": str(e)}


def _is_115_resource(res: dict) -> bool:
    """判断资源是否为115网盘链接"""
    link = (res.get("link") or res.get("share_link") or res.get("url") or "").lower()
    res_type = (res.get("resource_type") or res.get("cloud_type") or "").lower()
    return "115" in link or "115" in res_type


# ── 上传资源 ──
async def upload_resource(title: str, year: str, tmdb_id, link: str, filename: str):
    """POST /resources/upload/
    tmdb_id 为 None 时省略。
    """
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}
    payload = {
        "title": title,
        "year": year,
        "link": link,
        "filename": filename,
    }
    if tmdb_id:
        payload["tmdb_id"] = str(tmdb_id)
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BASE}/resources/upload/", headers=_headers(), json=payload)
            return r.json()
    except Exception as e:
        logger.error(f"聚影上传异常: {e}")
        return {"ok": False, "error": str(e)}


# ── 上传记录 ──
async def submissions():
    """GET /resources/submissions/"""
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/resources/submissions/", headers=_headers())
            return r.json()
    except Exception as e:
        logger.error(f"聚影提交记录查询异常: {e}")
        return {"ok": False, "error": str(e)}
