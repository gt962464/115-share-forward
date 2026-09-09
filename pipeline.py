"""
核心流水线：115 分享 → 转存 → 重命名 → 生成永久分享

复用 p115-share 镜像内的 P115Service 封装。
"""
import os
import re
import sys
import json
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

# ── P115 API 地址替换：支持 P115_API_BASE 环境变量 ──
# 当 api.115.com 被墙时（如美国 VPS），通过此变量指定可用端点
# 示例：P115_API_BASE=https://webapi.115.com 或 https://115cdn.com/webapi
_P115_API_BASE = os.getenv("P115_API_BASE", "").strip()
if _P115_API_BASE:
    try:
        import p115client.util as _p115_util
        _orig_complete_url = _p115_util.complete_url
        def _patched_complete_url(path="", base_url=None, app="", version="", force_app=False, domain="", query=()):
            if base_url is None or base_url == "":
                base_url = _P115_API_BASE
            return _orig_complete_url(path, base_url=base_url, app=app, version=version, force_app=force_app, domain=domain, query=query)
        _p115_util.complete_url = _patched_complete_url
        logging.getLogger("pipeline").info(f"✅ P115 API 已切换到: {_P115_API_BASE}")
    except Exception as _e:
        logging.getLogger("pipeline").warning(f"⚠️ P115 API 切换失败（将用默认）: {_e}")

# Apply fs_rename GET patch
try:
    import fix_rename  # noqa: F401
except Exception as _e:
    logging.getLogger("pipeline").warning(f"fix_rename import failed: {_e}")

from config import (
    P115_COOKIE, P115_SAVE_DIR, AUTO_RENAME,
    SHARE_AUDIT_WAIT_TIMEOUT, SHARE_AUDIT_POLL_INTERVAL,
    RETRY_INTERVAL, MAX_RETRY,
)

logger = logging.getLogger("pipeline")


VIDEO_EXTS = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv", ".rmvb", ".webm", ".m4v")


def video_ext(name: str) -> str:
    """返回视频文件扩展名（小写），不是视频文件返回 ''。"""
    n = str(name or "").lower()
    for e in VIDEO_EXTS:
        if n.endswith(e):
            return e
    return ""


def extract_episode(name: str):
    """从文件名提取 (season, ep)，拿不到返回 None。"""
    m = re.search(r"[Ss](\d{1,2})[.\s_-]?[Ee](\d{1,3})", name)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(?:^|[.\s_-])[Ee](\d{1,3})", name)
    if m:
        return 1, int(m.group(1))
    return None


def extract_hdr(name: str) -> str:
    """提取 HDR/DV 标记（排除视频编码 H.264/H.265）。"""
    toks = re.findall(r"\b(DV|HDR10\+?|HDR)\b", name, re.I)
    out = []
    for t in toks:
        t = t.upper()
        if t in ("DV", "HDR", "HDR10", "HDR10+") and t not in out:
            out.append(t)
    return ".".join(out)


# 分享根名里可能的 TMDB id 标记：如 "Z 遮天{tmdbid-224839}." / "xxx{tmdb:123456}"
_TMDBID_MARKER_RE = re.compile(r"\{\s*tmdb(?:[-_]?id)?\s*[-:]\s*(\d{3,8})\s*\}", re.I)


def parse_tmdbid_marker(name: str):
    """从分享根名/标题提取 {tmdbid-XXXX} 标记里的 TMDB id，拿不到返回 None。"""
    if not name:
        return None
    m = _TMDBID_MARKER_RE.search(name)
    return int(m.group(1)) if m else None


VIDEO_CODEC_RE = re.compile(
    r"\b(H\.26[45]|HEVC|AVC|x26[45]|AV1|VC-?1|MPEG-?[24]|XviD|DivX)\b", re.I)


def extract_video_codec(name: str) -> str:
    """仅提取视频编码（H.265/H.264/x265/x264/HEVC/AV1…），排除音频编码。"""
    m = VIDEO_CODEC_RE.search(name)
    return m.group(1).upper() if m else ""


def build_canonical_name(title_cn, season, ep_start, ep_end, year, quality,
                         source, audio, hdr, encode, ext="", suffix=""):
    """规范命名：{中文标题}.S{季}E{集}.{年}.{画质}.{源}.{音频}.{HDR}.{视频编码}[-suffix].{ext}"""
    season_tag = ""
    if season is not None:
        season_tag = f"S{season:02d}"
        if ep_end:
            season_tag += f"E{ep_start:02d}-E{ep_end:02d}"
        elif ep_start:
            season_tag += f"E{ep_start:02d}"
    parts = [title_cn]
    if season_tag:
        parts.append(season_tag)
    if year:
        parts.append(str(year))
    if quality:
        parts.append(quality)
    if source:
        parts.append(source)
    if audio:
        parts.append(audio)
    if hdr:
        parts.append(hdr)
    if encode:
        parts.append(encode)
    name = ".".join(p for p in parts if p)
    if suffix:
        name += f"-{suffix}"
    if ext:
        name += ext if ext.startswith(".") else "." + ext
    return name


QUALITY_RE = re.compile(r"(2160p|1440p|1080p|720p|480p|4k|2160P|1080P)", re.I)
SOURCE_RE = re.compile(r"(WEB[- ]?DL|WEBRip|BluRay|BDRip|HDTV|REMUX|HDRip|DVDRip|HDrip)", re.I)
EP_RE = re.compile(r"(?:S(\d{1,2})E(\d{1,3})(?:[-~]?E?(\d{1,3}))?)|(?:第\s*(\d{1,3})\s*集)|(?:E(\d{1,3})(?:[-~]?E?(\d{1,3}))?)", re.I)
ENC_RE = re.compile(r"\b(x264|x265|HEVC|H\.264|H\.265|AVC|AV1)\b", re.I)
CLEAN_RE = re.compile(r"[\[\]【】()（）{}（）]|（[^）]*）|\[[^\]]*\]|【[^】]*】")
AUDIO_RE = re.compile(r"(DDP\d+(?:\.\d+)?|DTS\d+(?:\.\d+)?|TrueHD|Atmos|AC3|AAC\d?|FLAC|LPCM)", re.I)


def infer_audio_codec(video_names):
    """从真实视频文件名提取音频编码（取出现次数最多的）。"""
    cnt = {}
    for fn in video_names:
        m = AUDIO_RE.search(fn)
        if m:
            k = m.group(1).upper()
            cnt[k] = cnt.get(k, 0) + 1
    if not cnt:
        return ""
    return max(cnt, key=cnt.get)


async def _rename_video_tree(svc, folder_id, title_cn, year, tmdb_id, suffix="Cxuan"):
    """递归重命名目录树中的全部视频；保留 Season 等原有子目录名。"""
    ok = fail = total = 0
    sub = await _get_dir_items(svc, folder_id)
    sub = list({str(x["id"]): x for x in sub}.values())
    vids = [x for x in sub if not x.get("is_dir") and video_ext(x["name"])]
    child_dirs = [x for x in sub if x.get("is_dir")]
    if vids:
        v0 = parse_filename(vids[0]["name"])
        quality = v0.get("quality") or ""
        source = v0.get("source") or ""
        encode = extract_video_codec(vids[0]["name"])
        audio = infer_audio_codec([x["name"] for x in vids])
        hdr = extract_hdr(vids[0]["name"])
        used = set()
        jobs = []
        for v in vids:
            e = extract_episode(v["name"])
            if e:
                season, ep = e
                new_name = build_canonical_name(
                    title_cn, season, ep, None, year, quality, source, audio, hdr, encode,
                    ext=video_ext(v["name"]), suffix=suffix,
                )
            else:
                new_name = build_canonical_name(
                    title_cn, None, None, None, year, quality, source, audio, hdr, encode,
                    ext=video_ext(v["name"]), suffix=suffix,
                )
            if new_name in used:
                stem, ext = os.path.splitext(new_name)
                new_name = f"{stem}-{len(used) + 1}{ext}"
            used.add(new_name)
            jobs.append((v["id"], new_name))
        for fid, new_name in jobs:
            total += 1
            try:
                r = await svc.client.fs_rename((fid, new_name), async_=True)
                if isinstance(r, dict) and r.get("state") is False:
                    fail += 1
                    logger.warning(f"⚠️ 重命名失败 {fid} -> {new_name}: {r.get('error') or r.get('message')}")
                else:
                    ok += 1
            except Exception as ex:
                fail += 1
                logger.warning(f"⚠️ 重命名异常 {fid} -> {new_name}: {ex}")
            await asyncio.sleep(0.12)
    for child in child_dirs:
        c_ok, c_fail, c_total = await _rename_video_tree(
            svc, child["id"], title_cn, year, tmdb_id, suffix=suffix,
        )
        ok += c_ok
        fail += c_fail
        total += c_total
    return ok, fail, total


async def _rename_saved(svc, to_cid, title_cn: str, year: str, tmdb_id, suffix: str = "Cxuan",
                        expected_video_count: int = 0):
    """对已转存到 to_cid 的目录树做规范重命名：顶层文件夹 + 每个视频文件。

    文件夹命名：`{名字} ({年}) (tmdb-{id})`
    单集命名：保留全部技术元数据 + `-{suffix}` 后缀
                 e.g. `九门.S01E01.2026.2160P.WEB-DL.DTS5.1.DV.H.265-Cxuan.mp4`
    电影/无集数：`{名字}.{年}.{画质}.{源}.{音频}.{HDR}.{编码}-Cxuan.mkv`（不带 S/E）

    若分享根没有文件夹（如单文件电影），自动建规范文件夹并把文件移入，
    返回 (ok, fail, total, new_cid)，new_cid 非空表示新建了文件夹。
    """
    ok = fail = total = 0
    new_cid = None
    try:
        top = await _get_dir_items(svc, to_cid)
        top = list({str(x["id"]): x for x in top}.values())
        dirs = [it for it in top if it.get("is_dir")]
        files = [it for it in top if not it.get("is_dir")]
        if not dirs and files:
            folder_name = build_folder_name(title_cn, year, tmdb_id)
            try:
                mk = await svc.client.fs_mkdir(folder_name, to_cid, async_=True)
                if isinstance(mk, dict):
                    new_cid = int(mk.get("cid") or (mk.get("data") or {}).get("cid") or 0)
                if new_cid:
                    await svc.client.fs_move([f["id"] for f in files], new_cid, async_=True)
                    logger.info(f"📁 分享根无文件夹，已自动创建并移入: {folder_name} (CID: {new_cid}, {len(files)} 个文件)")
                    top = [{"id": new_cid, "name": folder_name, "is_dir": True}]
                else:
                    logger.warning(f"⚠️ 自动建文件夹失败，跳过移动: {mk}")
            except Exception as e:
                logger.warning(f"⚠️ 自动建文件夹异常（不影响后续）: {e}")
        for it in top:
            if not it.get("is_dir"):
                continue
            folder_id = it["id"]
            folder_raw = it["name"]
            fp = parse_filename(folder_raw)
            folder_name = build_folder_name(title_cn, year, tmdb_id)
            total += 1
            try:
                r = await svc.client.fs_rename((folder_id, folder_name), async_=True)
                if isinstance(r, dict) and r.get("state") is False:
                    fail += 1
                    logger.warning(f"⚠️ 重命名失败 {folder_id} -> {folder_name}: {r.get('error') or r.get('message')}")
                else:
                    ok += 1
            except Exception as ex:
                fail += 1
                logger.warning(f"⚠️ 重命名异常 {folder_id} -> {folder_name}: {ex}")
            v_ok, v_fail, v_total = await _rename_video_tree(
                svc, folder_id, title_cn, year, tmdb_id, suffix=suffix,
            )
            ok += v_ok
            fail += v_fail
            total += v_total
    except Exception as e:
        logger.warning(f"⚠️ 重命名流程异常（不影响分享）: {e}")
    if total:
        logger.info(f"✅ 重命名完成：成功 {ok} 项，失败 {fail} 项（共 {total} 项）")
    return ok, fail, total, new_cid


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
        self.recycle_password = os.getenv("RECYCLE_PASSWORD", "").strip()
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
    """从文件名/标题提取 画质 / 视频源 / 集数 / 编码 / 干净标题。"""
    base = name
    ext = video_ext(name)
    if ext:
        base = name[:-len(ext)]
    quality = QUALITY_RE.search(base)
    source = SOURCE_RE.search(base)
    enc = ENC_RE.search(base)
    quality = quality.group(1).upper() if quality else ""
    source = source.group(1).replace(" ", "-").upper() if source else ""
    enc = enc.group(1).upper() if enc else ""

    ep_text = ""
    m = EP_RE.search(base)
    if m:
        if m.group(1):  # S01E01
            s, e1, e2 = m.group(1), m.group(2), m.group(3)
            ep_text = f"S{int(s):02d}E{int(e1):02d}" + (f"-E{int(e2):02d}" if e2 else "")
        elif m.group(4):  # 第xx集
            ep_text = f"第{m.group(4)}集"
        elif m.group(5):  # E01
            e1, e2 = m.group(5), m.group(6)
            ep_text = f"E{int(e1):02d}" + (f"-E{int(e2):02d}" if e2 else "")

    # 干净标题：取画质/集数/编码之前的部分
    cut = len(base)
    for rgx in (QUALITY_RE, EP_RE, SOURCE_RE, ENC_RE):
        mm = rgx.search(base)
        if mm and mm.start() < cut:
            cut = mm.start()
    title_raw = base[:cut]
    title_raw = CLEAN_RE.sub(" ", title_raw)
    title_raw = re.sub(r"[-_.\s]+", " ", title_raw).strip(" -_.")
    # 反复剥离尾部季标签和年份：如 `Blood.Sacrifice.2026.S01`
    for _ in range(3):
        before = title_raw
        title_raw = re.sub(
            r"[\s._-]*S\d{1,2}(?:-S?\d{1,2})?$", "", title_raw, flags=re.I
        ).strip(" -_.")
        title_raw = re.sub(
            r"[\s._-]*[(（]?(19\d{2}|20\d{2})[)）]?[\s._-]*$", "", title_raw
        ).strip(" -_.")
        if title_raw == before:
            break
    # 年份：从去掉扩展名的文件名提取 4 位年份（排除 1080P/2160P 等干扰）
    ym = re.search(r"(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![\dpPiI])", base)
    year = ym.group(1) if ym else ""
    # 提取 {tmdbid-xxx} / {tmdb-xxx} / {tmbid-xxx}（复用已有正则）
    _tid_m = _TMDBID_MARKER_RE.search(base)
    tmdb_id = int(_tid_m.group(1)) if _tid_m else None
    return {
        "title": title_raw or name,
        "quality": quality,
        "source": source,
        "encode": enc,
        "episode": ep_text,
        "year": year,
        "tmdb_id": tmdb_id,
    }


def build_folder_name(title_cn, year, tmdb_id):
    """顶层文件夹名：`{名字} ({年}) (tmdb-{id})`，缺字段时容错。"""
    parts = []
    if title_cn:
        parts.append(title_cn)
    if year:
        parts.append(f"({year})")
    if tmdb_id:
        parts.append(f"(tmdb-{tmdb_id})")
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
                total += int(item.get("size", 0) or item.get("file_size", 0) or item.get("fs", 0) or 0)
            # 从分享标题提取 tmdb_id（如 "这都不是事儿 (2026) {tmdb-334298}"）
            share_tmdb_id = None
            _tid_m = _TMDBID_MARKER_RE.search(title)
            if _tid_m:
                share_tmdb_id = int(_tid_m.group(1))
            return title, total, share_tmdb_id
        except Exception as e:
            logger.warning(f"fetch_share_info ({fn_name}) 失败: {e}")
    return None, None, None


async def _walk_share_items(svc, share_url: str, receive_code: str = "") -> list:
    """遍历分享全部文件条目（新版 p115client API）。

    share_iterdir_walk(client, share_code或链接, receive_code, async_=True)
    每次 yield (pid, 目录列表, 文件列表) 三元组；
    文件为 normalize 后的 dict（name/size/id/is_dir...）。
    """
    from p115client.tool import share_iterdir_walk
    items = []
    async for _pid, _dirs, files in share_iterdir_walk(
        svc.client, share_url, receive_code, async_=True,
    ):
        items.extend(files)
    return items


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
        for item in await _walk_share_items(svc, share_url, rc):
            if item.get("is_dir"):
                continue
            name = item.get("name") or item.get("n", "") or item.get("fn", "")
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
    """带重试的转存（只转存，返回含 to_cid 的原始结果）。

    注意必须用 save_share_link 而不是 save_and_share：
    后者是「转存+创建分享」全套封装，成功时只返回 share_link、不含 to_cid，
    会导致后续重命名/自建分享流程拿不到目标目录。
    """
    for attempt in range(max_retries):
        if is_cancelled():
            return {"status": "cancelled", "message": "任务已取消"}
        try:
            if hasattr(svc, "save_share_link"):
                res = await svc.save_share_link(url, metadata or {}, create_task_subdir=True)
            else:
                res = await svc._save_share_link_internal(url, metadata or {}, None, False, None, True)
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
    
    top_name, total, share_tmdb_id = await fetch_share_info(url)
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
    
    # ── 3) 解析元数据（正则 → OpenAI+TMDB 辅助识别）──
    base_name = title_override or video_names[0] or top_name
    parsed = parse_filename(base_name)
    display_title = parsed["title"]
    tmdb_id = None

    _se = re.search(r"[Ss](\d{1,2})[Ee]\d{1,3}", base_name)
    season = int(_se.group(1)) if _se else None
    ident_det = {}
    try:
        from identifier import resolve_title
        ident = await resolve_title(base_name, parsed["title"], parsed.get("year", ""), season, parsed.get("tmdb_id") or share_tmdb_id)
        if ident.get("title"):
            display_title = ident["title"]
            parsed["year"] = ident.get("year") or parsed.get("year", "")
            tmdb_id = ident.get("tmdb_id")
            ident_det = ident.get("det") or {}
            if ident.get("source") not in (None, "regex"):
                logger.info(f"🤖 识别({ident['source']}): {display_title!r} ({parsed.get('year')})")
    except Exception as e:
        logger.warning(f"OpenAI 识别失败，使用正则结果: {e}")
    
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
    
    # ── 5) 等待目录内容就位（115 异步复制可能延迟）──
    if AUTO_RENAME:
        for _wait in range(60):
            items = await _get_dir_items(svc, to_cid)
            if items:
                logger.info(f"✅ 目录已就位，{len(items)} 个项目 (等待 {_wait*2}s)")
                break
            await asyncio.sleep(2)
        else:
            logger.warning(f"⚠️ 等待 120s 后目录仍为空，跳过重命名")

    # ── 5b) 重命名：对齐 zip 版逻辑（顶层文件夹 + 每个视频文件）──
    if AUTO_RENAME:
        if on_progress:
            await on_progress(f"✏️ 重命名: {display_title}")
        try:
            _, _, _, new_cid = await _rename_saved(
                svc, to_cid, display_title, parsed.get("year", ""), tmdb_id,
                expected_video_count=len(video_names),
            )
            # 分享根无文件夹时，_rename_saved 已自动创建规范文件夹
            if new_cid:
                save_res = dict(save_res)
                save_res["names"] = [build_folder_name(display_title, parsed.get("year", ""), tmdb_id)]
                to_cid = new_cid
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
            # ── 基于所有 video_names 统计真实集数范围 ──
            episode = parsed.get("episode", "")
            ep_numbers = []
            for vn in video_names:
                em = re.search(r"[Ss](\d{1,2})[Ee](\d{2,3})", vn)
                if em:
                    ep_numbers.append((int(em.group(1)), int(em.group(2))))
            if ep_numbers:
                seasons = set(s for s, _ in ep_numbers)
                if len(seasons) == 1:
                    s = list(seasons)[0]
                    eps = sorted(set(e for _, e in ep_numbers))
                    if len(eps) == 1:
                        episode = f"S{s:02d}E{eps[0]:02d}"
                    else:
                        episode = f"S{s:02d}E{eps[0]:02d}-E{eps[-1]:02d}"
                else:
                    parts = []
                    for s in sorted(seasons):
                        eps = sorted(set(e for ss, e in ep_numbers if ss == s))
                        parts.append(f"S{s:02d}E{eps[0]:02d}-E{eps[-1]:02d}")
                    episode = " ".join(parts)
            logger.info(f"📊 集数统计: {len(ep_numbers)} 集, 结果: {episode or '(电影/无集数)'}")

            return {
                "status": "success",
                "share_link": share_res,
                "title": display_title,
                "quality": parsed["quality"],
                "source": parsed["source"],
                "episode": episode,
                "encode": parsed.get("encode", ""),
                "size": format_size(total or 0),
                "file_count": len(video_names),
                "det": ident_det,
                "to_cid": to_cid,
                "video_names": video_names,
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
    """获取目录下的文件/文件夹列表，使用 p115Service 的带重试方法。"""
    try:
        if hasattr(svc, "_get_dir_items"):
            items = await svc._get_dir_items(cid)
            return [{"id": it["id"], "name": it["name"], "is_dir": it["is_dir"]} for it in items]
        resp = await svc.client.fs_files({"cid": cid, "limit": 1000, "offset": 0, "show_dir": 1}, async_=True)
        data = (resp or {}).get("data", {})
        if isinstance(data, dict):
            data = data.get("list", [])
        return data or []
    except Exception as e:
        logger.warning(f"⚠️ _get_dir_items 异常: {e}")
        return []


async def _wait_share_audit(svc, share_url: str, timeout: int = None) -> bool:
    """等待分享审核完成。返回 True=审核通过可发布，False=失效/违规。无超时，直到有明确结果。"""
    poll_interval = SHARE_AUDIT_POLL_INTERVAL
    poll_count = 0
    while True:
        if is_cancelled():
            return False
        try:
            status = await svc.get_share_status(share_url)
            if status is None:
                await asyncio.sleep(poll_interval)
                continue
            if status.get("is_expired"):
                logger.warning(f"❌ 分享已失效: {share_url}")
                return False
            if status.get("is_prohibited"):
                logger.warning(f"❌ 分享违规未通过: {share_url}")
                return False
            if not status.get("is_pending"):
                logger.info(f"✅ 115 分享审核完成，允许推送: {share_url}")
                return True
            poll_count += 1
            logger.info(f"⏳ 115 分享仍在系统处理中... (第{poll_count}次轮询)")
        except Exception as e:
            logger.warning(f"⚠️ 审核状态查询异常: {e}")
        await asyncio.sleep(poll_interval)


# ── 自动清理（分享+发卡成功后删除源文件 + 清空回收站）──
# 与 NAS card-bot 的 AUTO_DELETE_AFTER/RECYCLE_PASSWORD 机制对齐：
# 卡片发送成功后，到期把任务目录移入 115 回收站并立即用密码清空。
_CLEANUP_STATE_FILE = Path(os.getenv("CLEANUP_STATE_FILE", "/data/cleanup_queue.json"))
_cleanup_queue: list = []
_cleanup_lock = asyncio.Lock()
_cleanup_worker_task = None


def _load_cleanup_queue():
    global _cleanup_queue
    try:
        if _CLEANUP_STATE_FILE.exists():
            data = json.loads(_CLEANUP_STATE_FILE.read_text(encoding="utf-8"))
            _cleanup_queue = data if isinstance(data, list) else []
    except Exception as e:
        logger.warning(f"清理队列读取失败: {e}")
        _cleanup_queue = []


def _save_cleanup_queue():
    try:
        _CLEANUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_CLEANUP_STATE_FILE) + ".tmp"
        Path(tmp).write_text(
            json.dumps(_cleanup_queue, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        os.replace(tmp, _CLEANUP_STATE_FILE)
    except Exception as e:
        logger.warning(f"清理队列保存失败: {e}")


def schedule_cleanup(cid, name: str = "", share_link: str = "", delay=None):
    """卡片发送成功后调用：安排到期删除任务目录并清空回收站。

    AUTO_DELETE_AFTER=0（默认）表示不自动清理。
    ⚠️ 注意：删除源文件后，基于这些文件创建的分享链接会随之失效。
    """
    if not cid:
        return
    auto = int(os.getenv("AUTO_DELETE_AFTER") or "0")
    if delay is None and auto <= 0:
        return
    delay = max(auto if delay is None else int(delay), 0)
    if any(x.get("cid") == int(cid) for x in _cleanup_queue):
        return
    _cleanup_queue.append({
        "cid": int(cid), "name": name or "", "share_link": share_link or "",
        "delete_at": time.time() + delay, "created_at": time.time(),
    })
    _save_cleanup_queue()
    logger.info(f"🕒 已安排自动清理（{delay} 秒后移入回收站并清空）: {name} (CID: {cid})")


async def empty_recycle_bin() -> tuple[bool, str]:
    """立即清空 115 回收站（用 RECYCLE_PASSWORD，/set 后立即生效）。"""
    try:
        svc = await get_svc()
        pwd = os.getenv("RECYCLE_PASSWORD", "").strip()
        resp = await svc.client.recyclebin_clean_app({}, password=pwd, async_=True)
        state = resp.get("state") if isinstance(resp, dict) else None
        if state is False:
            return False, f"❌ 清空回收站失败: {resp.get('error') or resp.get('message') or resp}"
        logger.info("✅ 回收站已清空")
        return True, "✅ 回收站已清空"
    except Exception as e:
        return False, f"❌ 清空回收站出错: {e}"


async def cleanup_worker():
    """后台 worker：到期任务目录移入回收站并立即清空。"""
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            due = [x for x in _cleanup_queue if x.get("delete_at", 0) <= now]
            if not due:
                continue
            async with _cleanup_lock:
                for item in due:
                    try:
                        svc = await get_svc()
                        result = await svc.client.fs_delete(item["cid"], async_=True)
                        state = result.get("state") if isinstance(result, dict) else None
                        if state is False:
                            raise RuntimeError(
                                result.get("error") or result.get("message") or str(result)
                            )
                        logger.info(
                            f"✅ 已移入 115 回收站: {item.get('name')} (CID: {item['cid']})，准备清空回收站"
                        )
                        ok, msg = await empty_recycle_bin()
                        if not ok:
                            raise RuntimeError(msg)
                        _cleanup_queue.remove(item)
                        _save_cleanup_queue()
                        logger.info(f"✅ 自动清理完成: {item.get('name')} (CID: {item['cid']})")
                    except Exception as e:
                        logger.warning(f"⚠️ 自动清理失败，60 秒后重试: {item.get('name')} | {e}")
                        item["delete_at"] = time.time() + 60
                        _save_cleanup_queue()
        except Exception as e:
            logger.warning(f"清理 worker 异常: {e}")


def start_cleanup_worker():
    """启动清理 worker（幂等，重启后从持久化队列恢复）。"""
    global _cleanup_worker_task
    _load_cleanup_queue()
    if _cleanup_worker_task is None or _cleanup_worker_task.done():
        _cleanup_worker_task = asyncio.create_task(cleanup_worker())
        logger.info(f"🧹 自动清理 worker 已启动（待清理 {len(_cleanup_queue)} 项）")
