"""
核心流水线：115 分享 → 转存 → 重命名 → 生成永久分享

复用 p115-share 镜像内的 P115Service 封装。
"""
import os
import re
import sys
import asyncio
import time
import logging
from pathlib import Path
from typing import Optional

# 确保能 import p115-share 的 app 模块
_APP_ROOT = os.getenv("CARD_BOT_APP_ROOT", "/app")
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
if "/cardbot" not in sys.path:
    sys.path.insert(0, "/cardbot")

from config import (
    P115_COOKIE, P115_SAVE_DIR, AUTO_RENAME,
    SHARE_AUDIT_WAIT_TIMEOUT, SHARE_AUDIT_POLL_INTERVAL,
    RETRY_INTERVAL, MAX_RETRY,
)

logger = logging.getLogger("pipeline")


# ── 取消机制 ──
_cancel_flag = False


def request_cancel():
    global _cancel_flag
    _cancel_flag = True


def reset_cancel():
    global _cancel_flag
    _cancel_flag = False


def is_cancelled() -> bool:
    return _cancel_flag


# ── P115Service 初始化（复用 p115client）──

class _CardAccount:
    """最小账号对象，仅供 P115Service 使用（不依赖数据库）。"""
    def __init__(self, cookie: str, save_dir: str):
        self.id = "cardbot"
        self.cookie = cookie
        self.save_dir = save_dir
        self.name = "cardbot"
        self.priority = 1
        self.enabled = True
        self.recycle_password = ""
        self.restriction_until = 0.0
        self.last_used_at = 0.0
        self.share_file_limit = 10000


_svc = None
_svc_lock = asyncio.Lock()


def _clean_115_env():
    """清除 p115client 初始化用的环境变量（我们自己管 Cookie）。"""
    for k in ("P115_COOKIE", "P115_SAVE_DIR"):
        os.environ.pop(k, None)


async def get_svc():
    """获取或初始化 P115Service 单例。"""
    global _svc
    if _svc and _svc.client:
        return _svc
    async with _svc_lock:
        if _svc and _svc.client:
            return _svc
        _clean_115_env()
        from app.services.p115 import P115Service
        acc = _CardAccount(P115_COOKIE, P115_SAVE_DIR)
        _svc = P115Service(account=acc)
        _svc.init_client(P115_COOKIE)
        logger.info("✅ P115Service 初始化完成")
        return _svc


# ── 工具函数 ──

def format_size(size_bytes: int) -> str:
    """字节 → 可读大小。"""
    if not size_bytes:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def parse_filename(name: str) -> dict:
    """从文件名解析标题、画质、编码、集数等信息。"""
    result = {
        "title": name, "quality": "", "source": "",
        "encode": "", "episode": "", "year": "",
    }
    # 去除常见后缀
    clean = re.sub(r"\.(mkv|mp4|ts|m2ts|avi|mov|flv|wmv|rmvb|webm|m4v)$", "", name, flags=re.IGNORECASE)
    # 画质
    q = re.search(r"(2160p|1080p|720p|480p|4K|UHD)", clean, re.IGNORECASE)
    if q:
        result["quality"] = q.group(1).upper()
    # 编码
    e = re.search(r"(REMUX|BluRay|WEB-?DL|WEB-?RIP|HDTV|DVDRip|BDRip)", clean, re.IGNORECASE)
    if e:
        result["source"] = e.group(1)
    # 音频
    a = re.search(r"(DTS|TrueHD|ATMOS|DDP?5\.1|AAC|FLAC|DTS-HD)", clean, re.IGNORECASE)
    if a:
        result["encode"] = a.group(1)
    # 集数
    ep = re.search(r"(?:S\d{1,2})?E(\d{1,3})(?:[-–]E?(\d{1,3}))?", clean, re.IGNORECASE)
    if ep:
        result["episode"] = ep.group(0)
    # 年份
    yr = re.search(r"[\.\s](\d{4})[\.\s]", clean)
    if yr:
        result["year"] = yr.group(1)
    return result


def build_folder_name(title_cn: str, year: str, tmdb_id=None) -> str:
    """构建重命名后的文件夹名。"""
    parts = [title_cn]
    if year:
        parts[0] = f"{title_cn} ({year})"
    if tmdb_id:
        parts.append(f"[tmdbid-{tmdb_id}]")
    return " ".join(parts)


def _parse_share_url(url: str):
    """从 URL 提取 share_code 和 receive_code。"""
    m = re.search(r"/(?:115\.com|115cdn\.com|anxia\.com)/s/([A-Za-z0-9]+)", url)
    if not m:
        return None, None
    code = m.group(1)
    rc = ""
    pm = re.search(r"[?&]password=([A-Za-z0-9]+)", url)
    if pm:
        rc = pm.group(1)
    return code, rc


# ── 核心流程 ──

async def fetch_share_info(share_url: str) -> tuple[Optional[str], Optional[int]]:
    """获取分享链接的根名和总大小。"""
    svc = await get_svc()
    code, rc = _parse_share_url(share_url)
    if not code:
        return None, None
    
    for fn_name in ("share_snap_app", "share_snap"):
        try:
            method = getattr(svc.client, fn_name, None)
            if not method:
                continue
            payload = {"share_code": code, "receive_code": rc or "", "cid": 0, "limit": 1000, "offset": 0}
            resp = await method(payload, async_=True)
            data = (resp or {}).get("data", {})
            info = data.get("shareinfo", data.get("share_info", {}))
            title = info.get("share_title", "")
            # 估算总大小
            total = 0
            for item in data.get("list", []):
                total += int(item.get("size", 0) or item.get("file_size", 0) or 0)
            return title, total
        except Exception as e:
            logger.warning(f"fetch_share_info ({fn_name}) 失败: {e}")
    return None, None


VIDEO_EXTS = (
    ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv",
    ".rmvb", ".webm", ".m4v", ".mpg", ".mpeg", ".vob", ".3gp", ".f4v",
    ".rm", ".asf", ".divx",
)


async def fetch_share_video_files(share_url: str) -> tuple[list[str], str]:
    """列出分享中的视频文件名。返回 (视频列表, 诊断信息)。"""
    svc = await get_svc()
    code, rc = _parse_share_url(share_url)
    if not code:
        return [], "链接解析失败"

    videos = []
    total_files = 0
    samples = []
    try:
        from p115client.util import share_extract_payload
        from p115client.tool import share_iterdir_walk
        payload = share_extract_payload(share_url)
        async for item in share_iterdir_walk(svc.client, payload, async_=True):
            # 115 分享条目：文件带 fid，目录带 cid
            is_dir = item.get("cid") is not None and item.get("fid") is None
            if is_dir:
                continue
            name = item.get("n", "") or item.get("fn", "") or item.get("name", "")
            if not name:
                continue
            total_files += 1
            if len(samples) < 3:
                samples.append(name)
            if name.lower().endswith(VIDEO_EXTS):
                videos.append(name)
    except Exception as e:
        logger.warning(f"fetch_share_video_files 失败: {e}")
        return [], f"扫描分享文件出错: {e}"

    diag = f"共扫描到 {total_files} 个文件"
    if samples:
        diag += f"，示例: {' / '.join(samples)}"
    return videos, diag


async def _save_share_with_retry(svc, url: str, metadata: dict = None, max_retries: int = 3) -> dict:
    """带重试的转存。"""
    for attempt in range(max_retries):
        if is_cancelled():
            return {"status": "cancelled", "message": "任务已取消"}
        try:
            res = await svc.save_and_share(url, metadata or {})
            if res and res.get("status") == "success":
                return res
            # 频控退避
            if res and "频繁" in str(res.get("message", "")):
                delay = [10, 20, 30][min(attempt, 2)]
                logger.warning(f"115 频控，等待 {delay}s 后重试 ({attempt+1}/{max_retries})")
                await asyncio.sleep(delay)
                continue
            return res
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"转存异常，重试 ({attempt+1}/{max_retries}): {e}")
                await asyncio.sleep(10)
            else:
                return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "超过最大重试次数"}


async def process_link(
    url: str,
    title_override: str = "",
    on_progress=None,
) -> dict:
    """
    完整处理流程：获取信息 → 转存 → 重命名 → 创建分享
    
    返回:
        {"status": "success", "share_link": "...", "title": "...", "size": "..."}
        {"status": "error", "message": "..."}
        {"status": "pending", "message": "..."}  # 审核中
        {"status": "cancelled", "message": "..."}  # 已取消
    """
    reset_cancel()  # 新任务开始，清空上一次的取消标志

    svc = await get_svc()
    
    # ── 1) 获取分享信息 ──
    if on_progress:
        await on_progress("🔍 获取分享信息...")
    
    top_name, total = await fetch_share_info(url)
    if not top_name:
        return {"status": "error", "message": "无法获取分享信息，链接可能无效或已过期"}
    
    # ── 2) 获取视频文件列表 ──
    if is_cancelled():
        return {"status": "cancelled", "message": "任务已取消"}
    if on_progress:
        await on_progress("📂 扫描文件列表...")
    
    video_names, scan_diag = await fetch_share_video_files(url)
    if not video_names:
        return {"status": "error", "message": f"分享中没有视频文件（{scan_diag}）"}
    
    # ── 3) 解析元数据 ──
    base_name = title_override or video_names[0] or top_name
    parsed = parse_filename(base_name)
    display_title = parsed["title"]
    
    logger.info(
        f"📋 处理: {display_title} | 画质: {parsed['quality']} | "
        f"编码: {parsed['source']} | 文件数: {len(video_names)} | "
        f"大小: {format_size(total or 0)}"
    )
    
    # ── 4) 转存 ──
    if on_progress:
        await on_progress(f"📥 转存中... ({len(video_names)} 个文件)")
    
    save_res = await _save_share_with_retry(svc, url, {
        "description": base_name, "full_text": base_name,
    })
    
    if not save_res or save_res.get("status") != "success":
        msg = (save_res or {}).get("message", "未知错误")
        status = (save_res or {}).get("status", "unknown")
        if status == "pending":
            return {"status": "pending", "message": f"115 审核中: {msg}"}
        return {"status": "error", "message": f"转存失败: {msg}"}
    
    to_cid = save_res.get("to_cid")
    if not to_cid:
        return {"status": "error", "message": "转存成功但未返回目标目录"}
    
    # ── 5) 重命名 ──
    if AUTO_RENAME:
        if on_progress:
            await on_progress(f"✏️ 重命名: {display_title}")
        try:
            new_name = build_folder_name(display_title, parsed.get("year", ""))
            # 获取任务目录下的文件列表
            items = await _get_dir_items(svc, to_cid)
            if items:
                # 重命名顶层目录
                await svc.client.fs_rename(to_cid, new_name, async_=True)
                logger.info(f"✅ 已重命名目录: {new_name}")
        except Exception as e:
            logger.warning(f"重命名失败（不影响分享）: {e}")
    
    # ── 6) 创建分享 ──
    if on_progress:
        await on_progress("🔗 创建永久分享链接...")
    
    share_res = await svc.create_share_link(save_res)
    
    if isinstance(share_res, str) and share_res.startswith("http"):
        # 检查审核状态
        ready = await _wait_share_audit(svc, share_res)
        if ready:
            return {
                "status": "success",
                "share_link": share_res,
                "title": display_title,
                "quality": parsed["quality"],
                "source": parsed["source"],
                "size": format_size(total or 0),
                "file_count": len(video_names),
            }
        else:
            return {
                "status": "pending",
                "share_link": share_res,
                "message": "115 分享正在审核中，稍后自动完成",
                "title": display_title,
            }
    
    if isinstance(share_res, dict):
        if share_res.get("status") == "margin_limited":
            return {
                "status": "pending",
                "message": share_res.get("message", "分享受限，稍后重试"),
                "title": display_title,
            }
        return {"status": "error", "message": share_res.get("message", "创建分享失败")}
    
    return {"status": "error", "message": "创建分享失败：未知响应"}


async def _get_dir_items(svc, cid: int) -> list:
    """获取目录下的文件/文件夹列表。"""
    try:
        resp = await svc.client.fs_files({"cid": cid, "limit": 1000, "offset": 0}, async_=True)
        data = (resp or {}).get("data", {})
        return data.get("list", [])
    except Exception:
        return []


async def _wait_share_audit(svc, share_url: str, timeout: int = None) -> bool:
    """等待分享审核完成。"""
    timeout = timeout or SHARE_AUDIT_WAIT_TIMEOUT
    code, rc = _parse_share_url(share_url)
    if not code:
        return False
    
    start = time.time()
    while time.time() - start < timeout:
        if is_cancelled():
            return False
        try:
            resp = await svc.client.share_snap_app(
                {"share_code": code, "receive_code": rc or "", "cid": 0, "limit": 10, "offset": 0},
                async_=True,
            )
            data = (resp or {}).get("data", {})
            info = data.get("shareinfo", data.get("share_info", {}))
            state = info.get("share_state", info.get("status"))
            if state == 1:
                return True
            if state == 7:
                return False  # 过期
        except Exception:
            pass
        await asyncio.sleep(SHARE_AUDIT_POLL_INTERVAL)
    return False
