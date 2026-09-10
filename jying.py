"""聚影 API 封装 — 纯新增文件，不依赖原有代码。"""
import os
import logging
import httpx
import uuid
import asyncio
import re

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


def _normalize(s: str) -> str:
    """标准化字符串用于比较：去空格、转小写、去标点"""
    return re.sub(r'[\s\-_.,:;!?，。：；！？·\(\)\[\]{}]', '', (s or '').lower())


async def _find_existing_movie(title: str, year: str, original_title: str = ""):
    """在聚影上搜索已有影片。
    同时用中文名和英文名搜索，任一匹配即返回 movie_id。
    """
    if not _ok():
        return None

    # 收集所有要搜索的关键词（去重）
    search_terms = []
    if title and title.strip():
        search_terms.append(title.strip())
    if original_title and original_title.strip() and original_title.strip() != title.strip():
        search_terms.append(original_title.strip())

    year_str = str(year).strip()

    try:
        async with httpx.AsyncClient(timeout=20) as c:
            for term in search_terms:
                r = await c.get(f"{BASE}/search/aggregate/", headers=_headers(), params={"q": term})
                data = r.json()
                if data.get("status") != "success":
                    continue
                movies = data.get("movies", [])
                for m in movies:
                    m_title = (m.get("title") or "").strip()
                    m_year = str(m.get("release_year") or "").strip()
                    m_original = (m.get("original_title") or "").strip()

                    # 匹配条件：标题相同（中文或英文）+ 年份相同
                    title_match = (
                        _normalize(m_title) == _normalize(title)
                        or _normalize(m_original) == _normalize(title)
                        or (original_title and _normalize(m_title) == _normalize(original_title))
                        or (original_title and _normalize(m_original) == _normalize(original_title))
                    )
                    if title_match and m_year == year_str:
                        movie_id = m.get("id")
                        logger.info(f"🎯 聚影匹配已有影片: {m_title} ({m_year}) -> movie_id={movie_id}")
                        return movie_id
            return None
    except Exception as e:
        logger.warning(f"聚影搜索已有影片异常: {e}")
        return None


# ── 上传资源 ──
async def upload_resource(title: str, year: str, tmdb_id, link: str, filename: str, original_title: str = "", file_size: str = ""):
    """POST /resources/upload/
    先搜索聚影是否有同名影片，有则补充资源（传 movie_id），无则新建。
    返回 dict: {status, submission_id, submission_status, ...}
    """
    if not _ok():
        return {"ok": False, "error": "聚影凭证未配置"}

    # 1) 搜索已有影片（同时用中文名和英文名）
    movie_id = await _find_existing_movie(title, year, original_title)

    payload = {
        "title": title,
        "year": year,
        "link": link,
        # filename 作为「资源对版证据」交给服务端核验
        "filename": filename,
        # description 是未公开字段，聚影用它作为资源说明（优先于链接里的文件夹名）
        "description": filename,
    }
    if file_size:
        payload["file_size"] = file_size
    logger.info(f"📤 聚影上传 payload: {payload}")
    if tmdb_id:
        payload["tmdb_id"] = str(tmdb_id)
    if movie_id:
        payload["movie_id"] = movie_id
        logger.info(f"📎 聚影补充资源到已有影片 (movie_id={movie_id}): {title} ({year})")
    else:
        logger.info(f"🆕 聚影新建影片: {title} ({year})")

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            headers = _headers()
            headers["Idempotency-Key"] = str(uuid.uuid4())
            r = await c.post(f"{BASE}/resources/upload/", headers=headers, json=payload)
            resp = r.json()
            # 提取 submission_id 和状态
            submission = resp.get("submission") or {}
            resp["submission_id"] = submission.get("id") or submission.get("submission_id")
            resp["submission_status"] = submission.get("status", "unknown")
            return resp
    except Exception as e:
        logger.error(f"聚影上传异常: {e}")
        return {"ok": False, "error": str(e)}


# ── 查询单条提交状态 ──
async def get_submission_status(submission_id: str):
    """GET /resources/submissions/?page=1 查询指定 submission 的状态"""
    if not _ok():
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BASE}/resources/submissions/", headers=_headers(), params={"page": 1})
            data = r.json()
            for sub in data.get("results", []):
                if sub.get("id") == submission_id or sub.get("submission_id") == submission_id:
                    return {
                        "status": sub.get("status", "unknown"),
                        "published_items": sub.get("published_items", 0),
                        "pending_items": sub.get("pending_items", 0),
                        "rejected_items": sub.get("rejected_items", 0),
                    }
            return None
    except Exception as e:
        logger.warning(f"查询聚影提交状态异常: {e}")
        return None


# ── 轮询审核状态 ──
async def poll_until_reviewed(submission_id: str, max_wait: int = 300, interval: int = 15):
    """轮询 submission 直到审核完成（published/rejected）或超时。
    返回 dict: {status, published_items, pending_items, rejected_items, timed_out}
    """
    if not submission_id:
        return {"status": "unknown", "timed_out": False}

    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(interval)
        elapsed += interval

        info = await get_submission_status(submission_id)
        if not info:
            continue

        status = info.get("status", "unknown")
        logger.info(f"🔄 聚影审核轮询 [{elapsed}s/{max_wait}s]: {status} "
                     f"(published={info.get('published_items',0)}, "
                     f"pending={info.get('pending_items',0)}, "
                     f"rejected={info.get('rejected_items',0)})")

        # 审核完成（全部 published 或全部 rejected）
        if status in ("published", "rejected"):
            info["timed_out"] = False
            return info

        # 还有 pending 但已经没有新的待审了（rejected 全部处理完）
        if info.get("pending_items", 0) == 0 and info.get("published_items", 0) > 0:
            info["timed_out"] = False
            return info

    # 超时
    logger.warning(f"⏰ 聚影审核轮询超时 ({max_wait}s): submission_id={submission_id}")
    info = await get_submission_status(submission_id) or {}
    info["timed_out"] = True
    return info


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
