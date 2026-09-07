#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
115 -> Telegram 资源卡片机器人（方案 B：独立卡片中间层）

职责：
  1. 接收 Telegram 私聊/群组里的 115 分享链接
  2. 用用户自己的 115 Cookie 调 P115-Share 内置的 P115Service 完成「转存 + 生成长期分享」
     （复用 proven 的 p115client 逻辑，不重写 115 接口，不碰 P115-Share 数据库）
  3. 解析文件名得到标题/画质/集数/编码
  4. 可选查 TMDB（海报/类型/评分/年份/简介）与豆瓣评分
  5. 下载海报，渲染成资源卡片，发到指定 Telegram 频道

部署：基于 listeningltg/p115-share 镜像运行，覆盖 entrypoint 为 python cardbot.py。
敏感配置（115 Cookie / TG Bot Token）只从环境变量或 /cardbot/.env 读取，绝不写进代码。
"""

import os
import re
import sys
import gc
import asyncio
import logging
import json
import hashlib
import time
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# 兜底：基于 p115-share 镜像运行时，/app 是项目根；确保可 import app.*
_APP_ROOT = os.getenv("CARD_BOT_APP_ROOT", "/app")
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)
if "/cardbot" not in sys.path:
    sys.path.insert(0, "/cardbot")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"),
           format="{time:HH:mm:ss} | {level} | {message}")

# ── 配置 ──────────────────────────────────────────────
P115_COOKIE = os.getenv("P115_COOKIE", "").strip()
P115_SAVE_DIR = os.getenv("P115_SAVE_DIR", "115-Share").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "").strip()       # 卡片发布频道
TG_USER_ID = os.getenv("TG_USER_ID", "").strip()             # 管理员 ID（始终拥有投稿权限）
TG_ALLOW_CHATS = [c.strip() for c in os.getenv("TG_ALLOW_CHATS", "").split(",") if c.strip()]
TG_SUBMITTER_IDS = {
    value for value in re.split(r"[\s,;]+", os.getenv("TG_SUBMITTER_IDS", "").strip()) if value
}
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TG_PROXY = os.getenv("TG_PROXY", "").strip()                 # 例: socks5://127.0.0.1:7890
TMDB_LANG = os.getenv("TMDB_LANG", "zh-CN").strip()
APP_HTTP_PROXY = os.getenv("APP_HTTP_PROXY", "").strip()     # 给 TMDB/豆瓣/海报下载用的代理
AUTO_PROCESS_115_LINKS = os.getenv("AUTO_PROCESS_115_LINKS", "1").strip().lower() in {"1", "true", "yes", "on"}
RECYCLE_PASSWORD = os.getenv("RECYCLE_PASSWORD", "").strip()  # 仅用于永久清空回收站，不写入代码/日志
# 115 创建分享后可能进入"文件正在系统处理中/生成快照"；等待审核完成再推频道。
SHARE_AUDIT_WAIT_TIMEOUT = int(os.getenv("SHARE_AUDIT_WAIT_TIMEOUT", str(15 * 60)))
SHARE_AUDIT_POLL_INTERVAL = int(os.getenv("SHARE_AUDIT_POLL_INTERVAL", "30"))

# ── LLM 辅助识别（OpenAI 兼容接口，用于文件名歧义/误写修正）──
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://apihub.agnes-ai.com/v1").strip()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "agnes-2.5-flash").strip()
# 限流：30 RPM / 200.0M TPM / 20 并发

# 发送成功后自动清理：到期移入回收站并清空回收站。
# 频道发送成功后立即清理任务目录；回收站随后使用 RECYCLE_PASSWORD 清空。
AUTO_DELETE_AFTER = int(os.getenv("AUTO_DELETE_AFTER", "0"))
CLEANUP_STATE_FILE = os.getenv("CLEANUP_STATE_FILE", "/cardbot-state/cleanup_queue.json")
_cleanup_lock = asyncio.Lock()
_cleanup_queue = []

# 测试模式开关：1=纯解析+私聊出卡片（不调用115转存、不发频道），2=同上但仍然调用115仅做信息查询不发频道
CARD_BOT_TEST_MODE = os.getenv("CARD_BOT_TEST_MODE", "0").strip()

# ── 115 客户端（复用 P115-Share 的 p115client 封装）────
# 关键：P115-Share 的 settings 是 Pydantic BaseSettings，会读取环境变量 P115_COOKIE；
# 若保留该环境变量，模块级单例会在导入时自动 init_client()，而此时没有运行中的事件循环会报错。
# 这里在导入前清掉它——cardbot 已在上面用 os.getenv 拿到自己的副本，不影响本脚本。
for _k in ("P115_COOKIE", "P115_SAVE_DIR"):
    os.environ.pop(_k, None)

from app.services.p115 import P115Service


async def _ensure_app_database_schema():
    """卡片机器人绕过原始 Web 启动入口时，主动创建 P115-Share 缺失表。

    尤其是 pending_links：审核轮询首次写入时若表不存在，会每60秒重复报
    sqlite3.OperationalError，导致任务永远无法推进。
    """
    try:
        from app.core.database import engine, Base
        import app.models.schema  # noqa: F401 - 注册全部 ORM 模型
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ P115-Share 数据库表结构已确认")
    except Exception as e:
        logger.error(f"❌ P115-Share 数据库表结构初始化失败: {e}")
        raise


class _CardAccount:
    """最小账号对象，仅供 P115Service 使用（不依赖数据库）。"""
    def __init__(self, cookie: str, save_dir: str):
        self.id = "cardbot"
        self.cookie = cookie
        self.save_dir = save_dir
        self.name = "cardbot"
        self.priority = 1
        self.enabled = True
        self.recycle_password = RECYCLE_PASSWORD
        self.restriction_until = 0.0
        self.last_used_at = 0.0
        self.share_file_limit = 10000  # 默认分享文件上限


_svc = None
_pipeline_svc = None


def get_svc() -> P115Service:
    global _svc
    if _svc is None:
        acc = _CardAccount(P115_COOKIE, P115_SAVE_DIR)
        _svc = P115Service(account=acc)
        _svc.init_client(P115_COOKIE)
    return _svc


def get_pipeline_svc() -> P115Service:
    """Return the separate 115 client used by the mounted-file pipeline."""
    global _pipeline_svc
    if _pipeline_svc is None:
        if not PIPELINE_P115_COOKIE_FILE.is_file():
            raise RuntimeError(f"闭环 Cookie 文件不存在: {PIPELINE_P115_COOKIE_FILE}")
        cookie = PIPELINE_P115_COOKIE_FILE.read_text(encoding="utf-8").strip()
        if not cookie:
            raise RuntimeError("闭环 Cookie 文件为空")
        account = _CardAccount(cookie, PIPELINE_115_ROOT)
        account.id = "mount-pipeline"
        _pipeline_svc = P115Service(account=account)
        _pipeline_svc.init_client(cookie)
        logger.info("✅ 115 挂载闭环客户端初始化完成")
    return _pipeline_svc


def _load_cleanup_queue():
    global _cleanup_queue
    try:
        with open(CLEANUP_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cleanup_queue = data if isinstance(data, list) else []
    except FileNotFoundError:
        _cleanup_queue = []
    except Exception as e:
        logger.warning(f"自动清理队列读取失败: {e}")
        _cleanup_queue = []


def _save_cleanup_queue():
    try:
        os.makedirs(os.path.dirname(CLEANUP_STATE_FILE), exist_ok=True)
        tmp = CLEANUP_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cleanup_queue, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CLEANUP_STATE_FILE)
    except Exception as e:
        logger.warning(f"自动清理队列保存失败: {e}")


def _schedule_cleanup(cid, name, share_link, *, service="main", delay=None):
    """仅在频道发送成功后调用；历史目录不会自动进入此队列。"""
    if not cid:
        return
    cid = str(cid)
    if any(str(x.get("cid")) == cid and x.get("service", "main") == service for x in _cleanup_queue):
        return
    delay = max(AUTO_DELETE_AFTER if delay is None else int(delay), 0)
    _cleanup_queue.append({
        "cid": cid, "name": name or "", "share_link": share_link or "",
        "service": service,
        "delete_at": time.time() + delay,
        "cleanup_mode": "channel_success_immediate",
        "created_at": time.time(),
    })
    _save_cleanup_queue()
    logger.info(f"🕒 已安排频道发布后自动移入回收站（延迟 {delay} 秒）: {name} (CID: {cid})")


async def _cleanup_worker():
    """后台将已成功发送且到期的新任务目录移入 115 回收站。"""
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            due = [x for x in _cleanup_queue if x.get("delete_at", 0) <= now]
            if not due:
                continue
            async with _cleanup_lock:
                for item in due:
                    cid = item.get("cid")
                    try:
                        svc = get_pipeline_svc() if item.get("service") == "pipeline" else get_svc()
                        result = await svc.client.fs_delete(cid, async_=True)
                        state = result.get("state") if isinstance(result, dict) else None
                        if state is False:
                            raise RuntimeError(result.get("error") or result.get("message") or str(result))
                        logger.info(f"✅ 已移入115回收站，准备清空回收站: {item.get('name')} (CID: {cid})")
                        # 用户明确要求：移入回收站后立即用密码永久清空回收站。
                        clean_result = await svc.client.recyclebin_clean_app(
                            {}, password=RECYCLE_PASSWORD, async_=True,
                        )
                        clean_state = clean_result.get("state") if isinstance(clean_result, dict) else None
                        if clean_state is False:
                            raise RuntimeError(clean_result.get("error") or clean_result.get("message") or str(clean_result))
                        _cleanup_queue.remove(item)
                        _save_cleanup_queue()
                        logger.info(f"✅ 已自动移入115回收站: {item.get('name')} (CID: {cid})")
                    except Exception as e:
                        logger.warning(f"⚠️ 自动移入回收站失败，60秒后重试: {item.get('name')} (CID: {cid}) | {e}")
                        item["delete_at"] = time.time() + 60
                        _save_cleanup_queue()
        except Exception as e:
            logger.warning(f"自动清理 worker 异常: {e}")


# ── TMDB 类型映射（避免额外请求）──────────────────────
TMDB_GENRES = {
    28: "动作", 12: "冒险", 16: "动画", 35: "喜剧", 80: "犯罪",
    99: "纪录", 18: "剧情", 10751: "家庭", 14: "奇幻", 36: "历史",
    27: "恐怖", 10402: "音乐", 9648: "悬疑", 10749: "爱情", 878: "科幻",
    10770: "电视电影", 53: "惊悚", 10752: "战争", 37: "西部",
    10759: "动作冒险", 10762: "儿童", 10763: "新闻", 10764: "真人秀",
    10765: "科幻奇幻", 10766: "肥皂剧", 10767: "脱口秀", 10768: "综艺",
}


# ── 文件名解析 ────────────────────────────────────────
QUALITY_RE = re.compile(r"(2160p|1440p|1080p|720p|480p|4k|2160P|1080P)", re.I)
SOURCE_RE = re.compile(r"(WEB[- ]?DL|WEBRip|BluRay|BDRip|HDTV|REMUX|HDRip|DVDRip|HDrip)", re.I)
EP_RE = re.compile(r"(?:S(\d{1,2})E(\d{1,3})(?:[-~]?E?(\d{1,3}))?)|(?:第\s*(\d{1,3})\s*集)|(?:E(\d{1,3})(?:[-~]?E?(\d{1,3}))?)", re.I)
ENC_RE = re.compile(r"\b(x264|x265|HEVC|H\.264|H\.265|AVC|AV1)\b", re.I)
YEAR_RE = re.compile(r"(19|20)\d{2}")
CLEAN_RE = re.compile(r"[\[\]【】()（）{}（）]|（[^）]*）|\[[^\]]*\]|【[^】]*】")
# HiveWeb 英文文件名别名：避免国产/AI 作品被按直译英文名发卡。
TITLE_ALIASES = {
    "theferrymanbutterflydream": ("灵魂摆渡·蝴蝶梦", "2026"),
    "theferrymanthedreamofthecelestialmaiden": ("灵魂摆渡·天女之梦", "2026"),
    "thefame": ("名利游戏", "2026"),
    "cicada": ("蝉", "2026"),
    "renyu": ("人鱼", "2026"),
    # HiveWeb 等发布组把中文脱口秀综艺直译为英文：Stand.up.Comedy 实为《脱口秀和Ta的朋友们》
    "standupcomedy": ("脱口秀和Ta的朋友们", "2024"),
    # HiveWeb/Pure 等发布组把国产剧《醒来》(2026) 直译为英文 Awaken
    "awaken": ("醒来", "2026"),
    # 发布组把《欢迎来龙餐馆》(2026) 误写作「欢迎来到龙餐厅」，纠正为官方正确名
    "欢迎来到龙餐厅": ("欢迎来龙餐馆", "2026"),
    # 荒野独居（真人秀节目，2015年首播）
    "荒野独居": ("荒野独居", "2015"),
}


def _title_alias(title: str, year: str = "") -> tuple[str, str]:
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(title or "").lower())
    alias = TITLE_ALIASES.get(key)
    if not alias:
        return title, year
    return alias[0], year or alias[1]


def _title_tmdb_id(title: str) -> int | None:
    key = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(title or "").lower())
    if key == "cicada":
        return 285250
    if key == "renyu":
        return 273119
    if key == "standupcomedy":
        return 261471
    if key == "awaken":
        return 289761
    if key == "荒野独居":
        return 63726
    return None


# ── LLM 辅助识别（OpenAI 兼容接口，处理文件名歧义/误写）──
_LLM_PARSE_PROMPT = """你是一个专业的媒体文件名解析器。

【核心规则】你必须严格只返回一个 JSON 对象，禁止返回任何解释、分析、推理过程、markdown 格式。
不要使用 ```json 代码块，不要添加任何前缀或后缀文本。

【你的任务】解析用户提供的影视文件名，提取以下字段：
- name: 影视作品的真实中文名称（如文件名是英文直译，需识别对应的中文原名）
- year: 发布年份（4位数字字符串）
- season: 季数（数字，仅电视剧有）
- episode: 集数信息（如 "E01-E12"，仅电视剧有）
- resolution: 分辨率（如 "2160p"）

【最重要·译名规则】name 必须是该作品在豆瓣 / TMDB 中文站真实使用的中文名。
- 只有当你能确认官方或通用的中文译名时，才返回中文名。
- 如果你**不能确认**中文译名，**必须原样返回文件名中的英文原名**（去掉点号、保留空格），
  严禁自行直译、意译或编造中文名！错误的译名会导致后续完全搜不到该作品，
  而保留英文原名至少能被正确检索到。
- 反面示例：文件名 "See.You.Later.Maybe.S01E01" 若你无法确认其中文译名，
  name 必须返回 "See You Later Maybe"，**绝不能**返回自行直译的 "后会有期"。
- 中文文件名的作品（如 "荒野求生系列(10部)"）请去掉"系列""合集""(10部)"等整理者后缀，
  返回干净的片名 "荒野求生"。

【关键能力】你拥有联网搜索能力，可以查询和验证不确定的影视作品信息。
当文件名中的标题看起来是英文直译、拼音、或可能有误写时，必须联网搜索确认真实中文片名。
绝不允许使用既有数据库的资源，以免导致在验证结果时出现无法验证确实存在但早于数据库时间的内容。

【重要区分】你必须区分电影和电视剧：
- 电视剧：文件名通常包含 S01/S02/E01/E02 等季集标记
- 电影：文件名通常不包含季集标记，可能包含 IMAX/DV/HDR 等标记
- 当同名作品有电影和电视剧版本时，根据文件名中的季集标记选择正确的版本

【输出格式】只返回这一个 JSON 对象，例如：
{"name":"脱口秀和Ta的朋友们","year":"2024","season":2,"episode":null,"resolution":"2160p"}

如果某个字段无法确定，返回 null。

【速度要求】请尽快给出结论，不要展开长篇推理，直接输出最终的 JSON 对象。"""

# 管理后台（card-admin）保存的自定义提示词路径。
# 两个容器共享宿主机目录 /vol2/1000/docker/115/card-bot：
#   card-admin  → /project（含 admin/data）
#   cardbot     → /cardbot
# 因此 cardbot 能直接读到后台编辑的提示词，网页改完立即生效。
_LLM_PROMPT_FILES = [
    os.getenv("LLM_PROMPT_FILE", "").strip(),
    "/cardbot/admin/data/llm_prompt.json",
    "/cardbot-state/llm_prompt.json",
]

_llm_prompt_cache: dict = {"mtime": -1.0, "text": ""}


def _load_llm_prompt() -> str:
    """读取用户在管理后台自定义的提示词；未配置则使用内置默认提示词。"""
    for path in _LLM_PROMPT_FILES:
        if not path or not os.path.exists(path):
            continue
        try:
            mtime = os.path.getmtime(path)
            if _llm_prompt_cache["mtime"] == mtime and _llm_prompt_cache["text"]:
                return _llm_prompt_cache["text"]
            with open(path, "r", encoding="utf-8") as f:
                txt = (json.load(f) or {}).get("prompt", "").strip()
            if txt:
                _llm_prompt_cache["mtime"] = mtime
                _llm_prompt_cache["text"] = txt
                return txt
        except Exception as e:
            logger.warning(f"读取自定义 LLM 提示词失败（{path}）: {e}")
    return _LLM_PARSE_PROMPT


# 已知的 LLM 识别结果缓存（文件名 hash → 解析结果），避免重复调用
_llm_cache: dict[str, dict | None] = {}
_LLM_CACHE_MAX = 200  # 最多缓存 200 条
# LLM 调用失败重试配置
#   - 后端实际是推理模型（mimo-v2.5），单次响应常要 30s+，超时给足
#   - 限流档位 30 RPM：必须用全局节流 + 429 退避，否则重试必被限流
_LLM_MAX_ATTEMPTS = 3
_LLM_TIMEOUTS = (45, 60, 60)      # 逐次放宽
_LLM_MIN_INTERVAL = 2.5           # 全局最小请求间隔（秒）≈ 24 RPM，留余量
_LLM_BACKOFF = {"429": 12.0, "5xx": 5.0, "other": 4.0}
_llm_last_call_ts = 0.0
_llm_rate_lock: asyncio.Lock | None = None


def _llm_rate_lock_obj() -> asyncio.Lock:
    """延迟创建锁：避免在事件循环外实例化 asyncio.Lock。"""
    global _llm_rate_lock
    if _llm_rate_lock is None:
        _llm_rate_lock = asyncio.Lock()
    return _llm_rate_lock


async def _llm_throttle():
    """全局节流：保证 LLM 请求速率不超过 30 RPM 限流档位。"""
    global _llm_last_call_ts
    async with _llm_rate_lock_obj():
        now = asyncio.get_event_loop().time()
        wait = _LLM_MIN_INTERVAL - (now - _llm_last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _llm_last_call_ts = asyncio.get_event_loop().time()


def _llm_cache_key(filename: str) -> str:
    """生成 LLM 缓存键：取文件名前 128 字符做 hash。"""
    import hashlib
    return hashlib.md5(filename[:128].encode("utf-8")).hexdigest()


async def llm_parse_filename(raw_filename: str) -> dict | None:
    """调用 LLM 辅助识别媒体文件名，返回 {name, year, season, episode, resolution} 或 None。

    【重要】输出格式必须严格遵循：
    {"name":"中文剧名"|"英文原名"|null,"year":"年份"|null,"season":季数|游null,"episode":"E01-E12"|null,"resolution":"2160p"|null}

    规则：
    1. 如果文件名已经包含英文原名（如 "Serenade of Peaceful Joy"），
       name 字段应保留英文原名，不要强行翻译成中文（翻译可能出错）
    2. 只有当文件名是纯中文或拼音/直译时，才尝试翻译成中文
    3. year/season/episode/resolution 能解析就填，不能解析填 null
    4. 严禁输出思考过程、markdown 代码块、任何解释文字
    5. 示例错误：把 "Serenade of Peaceful Joy" 译成 "风犬少年的天空" 或 "山河月明" 都是错的
       正确做法：name 保留 "Serenade of Peaceful Joy"，让后续 TMDB 搜索用英文查找
    """
    if not LLM_API_KEY or not raw_filename:
        return None

    cache_key = _llm_cache_key(raw_filename)
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _load_llm_prompt()},
            {"role": "user", "content": (
                f"请解析以下文件名，严格按 JSON 格式输出：\n{raw_filename}\n\n"
                f"格式要求：{{\"name\":\"英文原名或中文剧名\",\"year\":\"年份\"或null,\"season\":季数或null,\"episode\":\"集数\"或null,\"resolution\":\"分辨率\"或null}}\n"
                f"如果文件名已有英文原名，name 字段保留英文；只有无法确定时才翻译成中文。\n"
                f"不要输出任何解释，直接返回 JSON。"
            )},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    }, ensure_ascii=False)

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{LLM_API_BASE}/chat/completions"

    # 失败重试：推理模型常超时、网关 30 RPM 限流，需节流 + 退避，
    # 否则 LLM 永远失败 → 回退正则路径 → 这才是误识别的真正来源。
    _fatal = False  # True 表示请求本身有问题（配置/鉴权），重试无意义
    for _attempt in range(_LLM_MAX_ATTEMPTS):
        _timeout = _LLM_TIMEOUTS[min(_attempt, len(_LLM_TIMEOUTS) - 1)]
        _tag = f"第 {_attempt + 1}/{_LLM_MAX_ATTEMPTS} 次"
        await _llm_throttle()
        try:
            async with _session().post(
                url, data=payload.encode("utf-8"), headers=headers,
                proxy=APP_HTTP_PROXY or None,
                timeout=aiohttp.ClientTimeout(total=_timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"LLM 识别 HTTP {resp.status}（{_tag}）: {raw_filename[:50]}")
                    if resp.status == 429:
                        # 限流：优先按服务端 Retry-After 退避（上限 30s），否则默认退避
                        _retry_after = resp.headers.get("Retry-After")
                        try:
                            _wait = min(float(_retry_after), 30.0) if _retry_after else _LLM_BACKOFF["429"]
                        except (TypeError, ValueError):
                            _wait = _LLM_BACKOFF["429"]
                        await asyncio.sleep(_wait)
                        continue
                    if 500 <= resp.status < 600:
                        await asyncio.sleep(_LLM_BACKOFF["5xx"])
                        continue
                    # 其余 4xx（401/403/400…）是配置或请求问题，重试无意义
                    _fatal = True
                    break
                body = await resp.json()
                msg = body.get("choices", [{}])[0].get("message", {})
                content = msg.get("content") or ""
                # 某些模型（如 mimo）返回 reasoning_content 而非 content
                if not content.strip():
                    content = msg.get("reasoning_content") or ""
        except asyncio.TimeoutError:
            logger.warning(f"LLM 识别超时（{_timeout}s，{_tag}）: {raw_filename[:50]}")
            continue
        except Exception as e:
            logger.warning(f"LLM 识别异常（{_tag}）: {e}")
            await asyncio.sleep(_LLM_BACKOFF["other"])
            continue

        # 从响应中提取 JSON（可能被 markdown 代码块包裹，或混在文本中）
        content = content.strip()
        # 去除 markdown 代码块包裹
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)
        content = content.strip()

        # 尝试直接解析
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            # 直接解析失败：尝试从文本中提取 JSON 对象
            json_match = re.search(r'\{[^{}]*"name"\s*:\s*[^{}]*\}', content, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group(0))
                except json.JSONDecodeError:
                    logger.warning(f"LLM 识别 JSON 解析失败（{_tag}）: {content[:120]}")
                    await asyncio.sleep(_LLM_BACKOFF["other"])
                    continue
            else:
                logger.warning(f"LLM 识别无法提取 JSON（{_tag}）: {content[:120]}")
                await asyncio.sleep(_LLM_BACKOFF["other"])
                continue
        if not isinstance(result, dict):
            await asyncio.sleep(_LLM_BACKOFF["other"])
            continue

        # 规范化结果：确保字段存在且类型正确
        normalized = {
            "name": result.get("name") if isinstance(result.get("name"), str) else None,
            "year": result.get("year") if isinstance(result.get("year"), str) else None,
            "season": result.get("season"),
            "episode": result.get("episode") if isinstance(result.get("episode"), str) else None,
            "resolution": result.get("resolution") if isinstance(result.get("resolution"), str) else None,
        }

        # 缓存结果（限制缓存大小）
        if len(_llm_cache) >= _LLM_CACHE_MAX:
            # 简单策略：清掉一半旧缓存
            keys = list(_llm_cache.keys())
            for k in keys[: _LLM_CACHE_MAX // 2]:
                _llm_cache.pop(k, None)
        _llm_cache[cache_key] = normalized

        return normalized

    # 关键：只有"请求本身有问题"（鉴权/参数错误）才缓存失败，避免反复无效请求；
    # 超时 / 429 限流 / 5xx / 网络抖动属于瞬时故障 —— 一旦缓存 None，
    # 该文件名将永久走回旧的正则路径，误识别就再也不会被修正。
    if _fatal:
        logger.warning(f"LLM 识别请求被拒（不可重试），回退正则解析: {raw_filename[:50]}")
        _llm_cache[cache_key] = None
    else:
        logger.warning(f"LLM 识别暂时不可用（稍后自动重试），本次回退正则解析: {raw_filename[:50]}")
    return None


async def llm_verify_match(source_name: str, info: dict) -> bool | None:
    """OpenAI 二次校验：TMDB 搜到的条目是否真的对应源文件名。

    返回 True=确认匹配 / False=确认不匹配（调用方必须放弃该候选）/
    None=LLM 不可用或无法判断（不阻塞，保留候选）。

    这是"全部优先 OpenAI 识别"的最后一道闸：即使 LLM 片名解析对了，
    TMDB 打分仍可能选中同名错版（电影/剧集、翻拍版、年份错位），
    由 LLM 对最终候选做裁决，杜绝错误识别进卡片。
    """
    if not LLM_API_KEY or not source_name or not info:
        return None
    tmdb_id = info.get("tmdb_id")
    cache_key = f"verify::{_llm_cache_key(source_name)}::{tmdb_id}"
    if cache_key in _llm_cache:
        cached = _llm_cache[cache_key]
        return cached.get("match") if isinstance(cached, dict) else None

    prompt = (
        "你是影视资源校验员。判断【TMDB 候选条目】是否就是【源文件名】所指的那部作品。\n"
        "只返回 JSON：{\"match\": true 或 false, \"reason\": \"一句话理由\"}\n"
        "\n"
        "【重要规则】\n"
        "1. 英文原名和中文译名对照是正常情况，不要因此否决！\n"
        "   例如：\"Remnants of Gold\" 对应 \"金色\" 是正确的，应该返回 true。\n"
        "   例如：\"Pull Strings\" 对应 \"师兄太稳健\" 是正确的，应该返回 true。\n"
        "   例如：\"See You Later Maybe\" 对应 \"囧徒之预演告别\" 是正确的，应该返回 true。\n"
        "\n"
        "2. 只有以下情况才返回 false：\n"
        "   - 年份差异超过 5 年（如 2026 年的资源匹配到 2016 年的作品）\n"
        "   - 类型完全不同（如源文件是剧集 S01E01，但 TMDB 返回的是电影）\n"
        "   - 题材/内容完全无关（如源文件是抗战剧，TMDB 返回的是喜剧）\n"
        "   - 标题完全不同且无任何关联\n"
        "   - 【重要】是衍生剧/外传/前传/续集，但不是同一部作品（如《Reacher》和《Neagley》是不同的剧）\n"
        "\n"
        "3. 如果不确定，返回 {\"match\": true, \"reason\": \"不确定，放行\"}。\n"
        "4. 宁可信其有，不要误杀正确的匹配。"
    )
    cand = {
        "title": info.get("title", ""),
        "year": info.get("year", ""),
        "tmdb_id": tmdb_id,
        "genres": info.get("genres", ""),
        "overview": (info.get("overview") or "")[:200],
    }
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (
                f"源文件名：{source_name}\n"
                f"TMDB 候选：{json.dumps(cand, ensure_ascii=False)}"
            )},
        ],
        "temperature": 0.0,
        "max_tokens": 2000,
    }, ensure_ascii=False)
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    content = ""
    # 校验要"决定性"：429 限流时按 Retry-After 退避重试一次，不轻易放行
    for _attempt in range(2):
        try:
            await _llm_throttle()
            async with _session().post(
                f"{LLM_API_BASE}/chat/completions",
                data=payload.encode("utf-8"), headers=headers,
                proxy=APP_HTTP_PROXY or None,
                timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUTS[0]),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"LLM 校验 HTTP {resp.status}（第 {_attempt + 1}/2 次）: {source_name[:50]}")
                    if resp.status == 429 and _attempt == 0:
                        _retry_after = resp.headers.get("Retry-After")
                        try:
                            _wait = min(float(_retry_after), 30.0) if _retry_after else _LLM_BACKOFF["429"]
                        except (TypeError, ValueError):
                            _wait = _LLM_BACKOFF["429"]
                        await asyncio.sleep(_wait)
                        continue
                    return None
                body = await resp.json()
                msg = body.get("choices", [{}])[0].get("message", {})
                content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                break
        except Exception as e:
            logger.warning(f"LLM 校验异常（第 {_attempt + 1}/2 次）: {e}")
            if _attempt == 0:
                await asyncio.sleep(_LLM_BACKOFF["other"])
                continue
            return None
    if not content:
        logger.warning(f"LLM 校验无有效响应，放行候选: {source_name[:50]}")
        return None

    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()

    # 关键修复：推理模型（如 agnes-2.5-flash）会把思考过程（含复述提示词示例 JSON）
    # 也吐进 content。示例 JSON 通常有更多字段（如 "title"、"source_name"），
    # 而真正的校验结果只包含 {"match": bool, "reason": "..."}。
    # 策略：
    #   1. 先尝试直接解析整个 content（模型若正确输出，整个就是 JSON）
    #   2. 若失败，用正则查找所有候选 JSON，从后往前取第一个"紧���型"的（仅含 match+reason）
    #   3. 排除包含 title/name 等字段的"示例"JSON
    def _is_valid_match_json(obj: dict) -> bool:
        """判断是否是有效校验 JSON：必须有 match（bool），reason 可选，不能有其他字段。"""
        if not isinstance(obj.get("match"), bool):
            return False
        # 排除有额外字段的示例 JSON（如包含 "title"、"source_name" 等）
        allowed_keys = {"match", "reason"}
        return set(obj.keys()) <= allowed_keys

    result = None
    try:
        parsed_obj = json.loads(content)
        if isinstance(parsed_obj, dict) and _is_valid_match_json(parsed_obj):
            result = parsed_obj
    except json.JSONDecodeError:
        pass

    if result is None:
        matches = list(re.finditer(
            r'\{[^{}]*"match"\s*:\s*(?:true|false)[^{}]*\}', content, re.DOTALL
        ))
        for m in reversed(matches):
            try:
                _obj = json.loads(m.group(0))
                if isinstance(_obj, dict) and _is_valid_match_json(_obj):
                    result = _obj
                    break
            except json.JSONDecodeError:
                continue
        if result is None:
            logger.warning(f"LLM 校验无法提取有效 JSON（放行候选）: {content[:120]}")
            return None
    verdict = result.get("match")
    if not isinstance(verdict, bool):
        logger.warning(f"LLM 校验响应缺少 match 字段（放行候选）: {content[:120]}")
        return None
    # 校验结论是确定性判断，可以缓存（True/False 都缓存）
    if len(_llm_cache) >= _LLM_CACHE_MAX:
        keys = list(_llm_cache.keys())
        for k in keys[: _LLM_CACHE_MAX // 2]:
            _llm_cache.pop(k, None)
    _llm_cache[cache_key] = {"match": verdict}
    reason = str(result.get("reason") or "")[:80]
    if verdict:
        logger.info(f"✅ OpenAI 校验通过: {source_name[:40]} ≈ {info.get('title')!r}（{reason}）")
    else:
        logger.warning(f"🚫 OpenAI 校验否决: {source_name[:40]} ≠ {info.get('title')!r}（{reason}）")
    return verdict


# LLM 判定为"无有效片名"时的占位值，命中即视为未识别
_LLM_INVALID_NAMES = {
    "", "null", "none", "nil", "n/a", "na", "unknown", "未知", "无",
    "不知道", "无法确定", "unknown title", "-", "?",
}


async def _resolve_identity(raw_name: str, parsed: dict) -> tuple[str, str, str, str, int | None]:
    """统一识别入口：**英文原名优先搜 TMDB**，LLM 仅作辅助翻译。

    【方案 A+C】流程：
    1. 提取文件名中的英文原名（带点号），直接用英文名搜 TMDB（命中率高、不依赖 LLM 翻译）
    2. 若搜到，验证 TMDB.original_name ≈ 文件名英文名（确保是同一部作品）
    3. 年份对不上时，返回英文原名而非错误的中文名
    4. LLM 仅作辅助：当英文名搜不到时，尝试翻译成中文再搜

    返回 (search_title, search_year, fallback_title, fallback_year, season)
      - search_title / search_year：主用标题（英文原名优先）
      - fallback_title / fallback_year：备用标题（中文翻译结果）
      - season：季数提示
    """
    regex_title = (parsed.get("title") or "").strip()
    regex_year = str(parsed.get("year") or "").strip()

    # 季数提示：正则先取（S01E01 这类标记最可靠）
    _se = extract_episode(raw_name)
    season = _se[0] if _se else None

    # ── 步骤 1：提取英文原名 ─────────────────────────────────────────────
    # 文件名常见格式：Serenade.of.Peaceful.Joy.2020.S01... 或 Mystic.Nine.S01E01...
    # 规则：取第一个单词开头的大写英文片段（不含年份和季集号）
    _eng_match = re.match(r"^([A-Z][a-zA-Z0-9]+(?:\.[A-Z][a-zA-Z0-9]+)+)", regex_title)
    eng_title = _eng_match.group(1).replace(".", " ") if _eng_match else ""

    # ── 步骤 2：尝试用英文名搜 TMDB（方案 A）─────────────────────────────
    tmdb_by_eng = None
    if eng_title:
        logger.info(f"🔍 优先用英文名搜 TMDB: {eng_title!r}")
        tmdb_by_eng = await tmdb_search(eng_title, regex_year, season,
                                        source_name=raw_name) if TMDB_API_KEY else None

    # ── 步骤 3：LLM 辅助翻译（方案 C 兜底）──────────────────────────────
    llm_info = await llm_parse_filename(raw_name) if LLM_API_KEY else None
    llm_name = ((llm_info or {}).get("name") or "").strip()
    llm_year = str((llm_info or {}).get("year") or "").strip()

    # ── 步骤 4：决策逻辑 ─────────────────────────────────────────────────
    if tmdb_by_eng:
        # 英文名搜到了，优先使用
        tmdb_orig = (tmdb_by_eng or {}).get("original_name", "")
        tmdb_name = (tmdb_by_eng or {}).get("name", "")
        # 关键修复：TMDB 可能返回中文结果（如 "金色"），此时 original_name 也是中文
        # 不能用英文原名直接匹配中文原名，应该信任 TMDB 的排序（最相关的排第一）
        # 只需验证年份是否合理
        tmdb_year_str = (tmdb_by_eng or {}).get("year", "")
        if tmdb_year_str and regex_year:
            try:
                diff = abs(int(regex_year) - int(tmdb_year_str))
                if diff > 5:
                    logger.warning(
                        f"⚠️ 年份差异过大（文件:{regex_year} vs TMDB:{tmdb_year_str}）"
                    )
                    return eng_title, regex_year, llm_name or regex_title, llm_year or regex_year, season
            except ValueError:
                pass
        # 年份合理，使用 TMDB 结果
        parsed["title"] = tmdb_name or tmdb_orig
        parsed["year"] = tmdb_year_str
        logger.info(f"✅ 英文名 TMDB 命中: {eng_title!r} → {parsed['title']!r} ({parsed['year']})")
        return parsed["title"], parsed["year"], llm_name or regex_title, llm_year or regex_year, season

    # 英文名没搜到，尝试 LLM 翻译
    if llm_name and llm_name.strip().lower() not in _LLM_INVALID_NAMES:
        if llm_name != regex_title:
            logger.info(f"🤖 LLM 优先识别: {regex_title!r} -> {llm_name!r}{f' ({llm_year})' if llm_year else ''}")
        else:
            logger.info(f"🤖 LLM 识别确认: {llm_name!r}")
        parsed["title"] = llm_name
        if llm_year:
            parsed["year"] = llm_year
        return llm_name, (llm_year or regex_year), regex_title, regex_year, season

    # LLM 不可用 / 超时 / 无结果 → 回退：正则 + 别名映射
    alias_title, alias_year = _title_alias(regex_title, regex_year)
    if alias_title != regex_title:
        logger.info(f"🔤 LLM 未命中，回退别名映射: {regex_title} -> {alias_title}")
        parsed["title"], parsed["year"] = alias_title, alias_year
        return alias_title, alias_year, alias_title, alias_year, season
    logger.info(f"⚠️ LLM 未命中，使用正则解析标题: {regex_title!r}")
    return regex_title, regex_year, regex_title, regex_year, season


def _norm_title(s: str) -> str:
    """标题归一化：去标点/空格、转小写，用于精确比较与包含判断。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())


def _title_similarity(a: str, b: str) -> float:
    """计算标题相似度，用于拦截明显错配的自定义标题/TMDB候选。"""
    from difflib import SequenceMatcher
    aa, bb = _norm_title(a), _norm_title(b)
    if not aa or not bb:
        return 0.0
    if aa in bb or bb in aa:
        return 1.0
    return SequenceMatcher(None, aa, bb).ratio()


def _custom_name_conflicts(custom: str, source: str) -> bool:
    """识别转发消息中的旧标题，避免它覆盖真实视频文件名。"""
    if not custom or not source:
        return False
    custom_year = parse_filename(custom).get("year")
    source_year = parse_filename(source).get("year")
    if custom_year and source_year and custom_year != source_year:
        return True
    # 仅对双方都主要是英文/数字的标题做相似度拦截，避免误伤中文自定义名。
    c_ascii = len(re.findall(r"[A-Za-z]", custom)) >= 3
    s_ascii = len(re.findall(r"[A-Za-z]", source)) >= 3
    return bool(c_ascii and s_ascii and _title_similarity(custom, source) < 0.28)


def _select_source_name(video_names: list, root_name: str = "", user_name: str = "") -> str:
    """选择元数据源：真实视频文件名绝对优先，备注不覆盖实际内容。"""
    names = [n for n in (video_names or []) if n and (video_ext(n) or is_image_resource(n))]
    if names:
        source = names[0]
        if user_name:
            logger.info(f"ℹ️ 忽略自定义标题，元数据以真实视频文件名为准: {user_name!r} -> {source!r}")
        return source
    return root_name or user_name


def parse_filename(name: str):
    """从文件名/标题提取 画质 / 视频源 / 集数 / 编码 / 干净标题。"""
    # 先剥离视频扩展名：不剥的话 "遮天 背棺战王腾.2026.mkv" 尾部年份正则被 .mkv 挡住，
    # 标题会残留 "2026 mkv"。所有解析基于去掉扩展名的 base。
    base = name
    ext = video_ext(name)
    if ext:
        base = name[: -len(ext)]
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
    # 反复剥离尾部季标签和年份：真实文件名常见 `Blood.Sacrifice.2026.S01`，
    # 必须先去 S01 再去年份，否则年份不会位于字符串末尾而残留进标题。
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
    # 年份：从去掉扩展名的文件名提取 4 位年份（排除 1080P/2160P 分辨率、S06E42 季集号等干扰）
    # 要求前后都不是字母数字：Do.2019 → 匹配；S06E42 的 06/42 前是字母 → 跳过；2160p 后是 p → 跳过
    ym = re.search(r"(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![\dpPiI])", base)
    year = ym.group(1) if ym else ""
    return {
        "title": title_raw or name,
        "quality": quality,
        "source": source,
        "encode": enc,
        "episode": ep_text,
        "year": year,
    }


# ── 规范命名（转存后重命名用）──
VIDEO_EXTS = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv", ".rmvb", ".webm", ".m4v")
# ISO 是整盘镜像，不是视频文件；单独纳入资源识别，不能因此进入视频集数逻辑。
IMAGE_EXTS = (".iso", ".img", ".nrg", ".mdf", ".mds", ".bin", ".cue")


def media_ext(name: str) -> str:
    lowered = str(name or "").lower()
    for ext in VIDEO_EXTS + IMAGE_EXTS:
        if lowered.endswith(ext):
            return ext
    return ""


def video_ext(name: str) -> str:
    n = str(name or "").lower()
    for e in VIDEO_EXTS:
        if n.endswith(e):
            return e
    return ""


def is_image_resource(name: str) -> bool:
    return str(name or "").lower().endswith(IMAGE_EXTS)


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
_TMDBID_MARKER_RE = re.compile(r"\{\s*tmdb[-_]?id\s*[-:]\s*(\d{3,8})\s*\}", re.I)


def parse_tmdbid_marker(name: str):
    """从分享根名/标题提取 {tmdbid-XXXX} 标记里的 TMDB id，拿不到返回 None。

    这是分享者给的最精确信号（如 "Z 遮天{tmdbid-224839}."），比搜索消歧可靠得多。
    """
    if not name:
        return None
    m = _TMDBID_MARKER_RE.search(name)
    return int(m.group(1)) if m else None


VIDEO_CODEC_RE = re.compile(
    r"\b(H\.26[45]|HEVC|AVC|x26[45]|AV1|VC-?1|MPEG-?[24]|XviD|DivX)\b", re.I)


def extract_video_codec(name: str) -> str:
    """仅提取视频编码（H.265/H.264/x265/x264/HEVC/AV1…），排除音频编码（DDP5.1/DTS…）。"""
    m = VIDEO_CODEC_RE.search(name)
    return m.group(1).upper() if m else ""


def build_canonical_name(title_cn, season, ep_start, ep_end, year, quality,
                         source, audio, hdr, encode, ext="", suffix=""):
    """规范命名：{中文标题}.S{季}E{集}.{年}.{画质}.{源}.{音频}.{HDR}.{视频编码}[-suffix].{ext}

    - ep_end 有值（未完结）→ 范围 S{季}E{起}-E{止}
    - ep_end 为 None 且 ep_start 有值 → 单集 S{季}E{NN}
    - ep_start/ep_end 均 None（完结）→ 仅 S{季}
    - season 为 None（电影/无集数）→ 不带 S/E 段
    - suffix 非空：编码与扩展名之间插入 "-{suffix}"（如 -Cxuan）
    """
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


def format_size(b: int) -> str:
    if not b:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.2f}{unit}" if unit != "B" else f"{int(b)}{unit}"
        b /= 1024
    return f"{b:.2f}PB"


# ── TMDB / 豆瓣 / 海报（用镜像自带的 aiohttp，避免额外依赖）──
import aiohttp
from html import escape as html_escape

_aio_session = None


def _session() -> aiohttp.ClientSession:
    global _aio_session
    if _aio_session is None or _aio_session.closed:
        _aio_session = aiohttp.ClientSession(
            trust_env=False,
            timeout=aiohttp.ClientTimeout(total=20),
        )
    return _aio_session


async def _aio_get(url: str, params: dict = None, headers: dict = None) -> tuple:
    """GET 请求，网络失败自动重试 3 次（1/3/7 秒退避）。

    元数据（TMDB/豆瓣）单次失败会让整张卡片回退英文分享名、类型/评分全空，
    所以必须有重试——代理临时抖动时仍能拿到正确中文标题与元数据。
    """
    for attempt, delay in enumerate((0, 1, 3, 7), 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with _session().get(url, params=params, headers=headers or {},
                                      proxy=APP_HTTP_PROXY or None) as r:
                return r.status, await r.text()
        except Exception as e:
            logger.warning(f"HTTP GET 失败 {url}（第 {attempt}/4 次）: {e}")
    return 0, ""


async def _aio_get_bytes(url: str) -> bytes:
    """下载海报，网络失败自动重试 3 次（1/3/7 秒退避）。"""
    if not url:
        return b""
    for attempt, delay in enumerate((0, 1, 3, 7), 1):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with _session().get(url, proxy=APP_HTTP_PROXY or None) as r:
                if r.status == 200:
                    data = await r.read()
                    if data:
                        return data
                logger.warning(f"海报下载 HTTP {r.status}（第 {attempt}/4 次）: {url}")
        except Exception as e:
            logger.warning(f"海报下载失败（第 {attempt}/4 次）{url}: {e}")
    return b""


# 翻拍/本地版标记：多版本同名剧（如 Heart Signal 韩版/日版/中国版）用来降权
REMAKE_MARK_RE = re.compile(
    r"日版|日本版|中国版|内地版|台版|港版|美版|英版|韩版|泰版|翻拍|Remake|"
    r"(?:^|[ ._-])(?:US|Japan|Japanese|Chinese|American|British|Korean|Thai)\b",
    re.I,
)


def _score_tmdb_item(it, year: str, query_title: str = "") -> float:
    """多版本同名剧打分：年份一致、标题相似优先，翻拍/本地版降权。"""
    s = 0.0
    it_year = (it.get("first_air_date") or it.get("release_date") or "")[:4]
    if year and it_year and it_year == year:
        s += 100
    name = it.get("name") or it.get("title") or ""
    orig = it.get("original_name") or it.get("original_title") or ""
    if query_title:
        s += _title_similarity(query_title, orig or name) * 25
        # 标题"完全相等"必须明显高于"只是被包含"：
        # 搜「荒野求生」时，「异星怪兽之荒野求生」也包含该词，但它是另一部作品。
        _q = _norm_title(query_title)
        _n, _o = _norm_title(name), _norm_title(orig)
        if _q and (_q == _n or _q == _o):
            s += 60                      # 标题完全一致：最可信
        elif _q and ((_q in _n and len(_n) - len(_q) >= 2) or (_q in _o and len(_o) - len(_q) >= 2)):
            s -= 15                      # 查询词只是候选名的一小截：大概率是同名衍生作品
    if REMAKE_MARK_RE.search(name) or REMAKE_MARK_RE.search(orig):
        s -= 30
    # 人气贡献：大幅提升人气高的条目权重，避免被低人气衍生作品抢走
    popularity = float(it.get("popularity") or 0)
    s += min(popularity / 50, 20)  # 从 max 5 提升到 max 20
    return s


async def _tmdb_season_count(item) -> int:
    """查询 TV 条目总季数（搜索列表不含该字段，需详情接口）。失败返回 None。"""
    try:
        import json as _json
        st, text = await _aio_get(
            f"https://api.themoviedb.org/3/tv/{item.get('id')}",
            params={"api_key": TMDB_API_KEY, "language": TMDB_LANG},
        )
        if st != 200:
            return None
        return _json.loads(text).get("number_of_seasons")
    except Exception:
        return None


async def _pick_tmdb_item(results: list, year: str, season, query_title: str = "") -> dict:
    """从搜索结果里选最可能是目标版本的条目。

    1. 打分排序：年份一致 +100（决定性）；名字/原名含翻拍标记（日版/中国版/Remake…）-30；人气微调。
    2. 若文件名带季数（如 S01/S09）：优先选 TV 类型（电影和电视剧同名时，带季数的是电视剧）。
    3. 若文件名带季数且季数 > 1：对分数最高的前 3 个候选查详情季数，
       优先选 number_of_seasons >= 文件季数 的条目（Heart Signal 韩版 9 季 vs 日版 2 季/中国版 5 季）。
    4. 都不满足退回分数最高。

    【年份容差】资源文件名年份可能是上传年份而非首播年份，允许 ±2 年容差。
    只有当年份差异 > 5 年（明显错误）时才严格拒绝。
    """
    cands = results[:10]
    if len(cands) <= 1:
        return cands[0]

    # 提取文件名的年份（如果有的话）
    file_year = int(year) if year and year.isdigit() else None

    # 计算年份容差：±2 年（资源发布年份可能晚于首播年份）
    YEAR_TOLERANCE = 2
    MAX_YEAR_DIFF = 5  # 超过这个差距认为明显错误

    if file_year:
        # 在容差范围内筛选候选
        tolerant_cands = []
        for it in cands:
            it_year_str = (it.get("first_air_date") or it.get("release_date") or "")[:4]
            if not it_year_str:
                continue
            try:
                it_year = int(it_year_str)
                diff = abs(file_year - it_year)
                if diff <= YEAR_TOLERANCE:
                    # 年份完全匹配或容差内，保留并加分
                    it["_year_score_bonus"] = 100 - diff * 10
                    tolerant_cands.append(it)
            except ValueError:
                pass

        if tolerant_cands:
            # 容差内有匹配，用这些候选继续筛选
            logger.info(f"ℹ️ 年份容差匹配: 文件{file_year}年, 找到{len(tolerant_cands)}个候选")
            cands = tolerant_cands
        elif file_year:
            # 容差外无匹配，检查是否明显错误（> 5 年）
            all_years = []
            for it in cands:
                y_str = (it.get("first_air_date") or it.get("release_date") or "")[:4]
                if y_str and y_str.isdigit():
                    all_years.append(int(y_str))
            if all_years:
                closest_year = min(all_years, key=lambda x: abs(x - file_year))
                if abs(file_year - closest_year) > MAX_YEAR_DIFF:
                    logger.warning(
                        f"⚠️ TMDB 年份差异过大（文件:{file_year} vs TMDB最近:{closest_year}），拒绝误匹配: {query_title!r}"
                    )
                    return {}
                # 年份差异在 5 年内但不满足容差，放宽限制使用所有候选
                logger.info(f"ℹ️ 年份不在容差内（文件:{file_year} vs TMDB最近:{closest_year}），放宽筛选: {query_title!r}")
            else:
                # 没有年份信息的候选，使用所有候选
                logger.info(f"ℹ️ TMDB候选无年份信息，放宽筛选: {query_title!r}")

    scored = sorted(
        ((_score_tmdb_item(it, year, query_title) + it.get("_year_score_bonus", 0), it)
         for it in cands),
        key=lambda x: -x[0]
    )
    # 打印排序结果用于调试
    logger.info(f"🔍 TMDB 候选排序 (top3):")
    for i, (score, it) in enumerate(scored[:3]):
        it_year = (it.get("first_air_date") or it.get("release_date") or "")[:4]
        logger.info(f"   {i+1}. id={it.get('id')}, name={it.get('name', '')}, year={it_year}, score={score:.1f}, type={it.get('media_type', 'unknown')}")
    # 关键优化：当文件名有季数标记（S01/S02等）时，即使 season=1 也优先选择 TV 类型
    # 这解决了同名电影和电视剧的歧义（如 "See You Later Maybe" → 电视剧《囧徒之预演告别》 vs 电影《再见，也许不再见》）
    if season is not None and season >= 1:
        for _score, it in scored[:3]:
            if it.get("media_type") == "tv":
                logger.info(f"✅ 选中 TV 结果: id={it.get('id')}, name={it.get('name')}")
                return it
        # 如果前3个都没有 TV 类型，退回用分数最高
        logger.warning(f"⚠️ 前3个候选都不是 TV 类型，退回使用分数最高")
    if not season or season <= 1:
        return scored[0][1]
    for _score, it in scored[:3]:
        if it.get("media_type") != "tv":
            continue
        ns = await _tmdb_season_count(it)
        if ns is not None and ns >= season:
            return it
    return scored[0][1]


async def _tmdb_detail(media_type: str, item_id, item: dict = None):
    """按 media_type/id 拉 TMDB 详情并组装卡片元数据。失败返回 None。

    item 为搜索命中项（可为 None），用于兜底 name/海报/人气。
    """
    import json as _json
    st2, dt = await _aio_get(
        f"https://api.themoviedb.org/3/{media_type}/{item_id}",
        params={"api_key": TMDB_API_KEY, "language": TMDB_LANG},
    )
    if st2 != 200:
        return None
    det = _json.loads(dt)
    item = item or {}
    # 选显示名：中文（zh）剧/电影用原始名，否则用本地化名
    orig_lang = det.get("original_language", "")
    if media_type == "tv":
        name_local = det.get("name", "") or item.get("name", "")
        name_orig = det.get("original_name", "") or item.get("original_name", "")
    else:
        name_local = det.get("title", "") or item.get("title", "")
        name_orig = det.get("original_title", "") or item.get("original_title", "")
    if orig_lang == "zh" and name_orig:
        display_name = name_orig
    else:
        display_name = name_local or name_orig
    # 海报 / 年份 / 评分 / 简介 / 类型
    poster_path = det.get("poster_path") or item.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""
    year_full = (det.get("release_date") or det.get("first_air_date")
                 or item.get("release_date") or item.get("first_air_date") or "")
    year = year_full[:4]
    rating_val = det.get("vote_average")
    if rating_val is None:
        rating_val = item.get("vote_average") or 0
    rating = f"{float(rating_val):.1f}/10" if rating_val else "暂无评分"
    overview = det.get("overview") or item.get("overview") or ""
    genres_raw = det.get("genres") or []
    if genres_raw:
        genres = [g.get("name", "") for g in genres_raw]
    else:
        gid = item.get("genre_ids") or []
        genres = [TMDB_GENRES.get(g, "") for g in gid if g in TMDB_GENRES]
    genres_text = "、".join([g for g in genres if g]) or "暂无"
    return {
        "title": display_name,
        "tmdb_id": item_id,
        "poster_url": poster_url,
        "year": year,
        "genres": genres_text,
        "rating": rating,
        "overview": overview,
    }


async def tmdb_search(title: str, year: str = "", season: int = None, tmdb_id=None,
                      source_name: str = ""):
    """返回 dict（含 title=中文优先的显示名）/ None。中文剧/电影用 TMDB original_name。

    tmdb_id（可选，来自分享根名 {tmdbid-XXXX} 标记）：最精确信号，直接按 id 查详情，
    完全不依赖搜索——可避免混合分享包/多版本同名导致的搜索歧义。
    year/season（可选）：多版本同名剧优先匹配年份一致、季数足够的条目，避免误中翻拍版。
    source_name（可选，原始文件名）：传入后，搜索命中的候选会先交给 OpenAI 校验
    （llm_verify_match），确认不是同名错版才返回；OpenAI 明确否决时返回 None，
    让调用方回退备用标题或放弃，杜绝错误识别。用户人工指定的 tmdb_id 不做校验。
    """
    if not TMDB_API_KEY:
        return None
    import json as _json

    # 常见前缀：TMDB 上不注册这些前缀，搜索时需去掉
    _TMDB_STRIP_PREFIXES = re.compile(
        r"^(?:央视|CCTV|中国|大陆|内地|国产|正版|官方|高清|蓝光|4K|8K)\s*"
    )

    async def _do_search(q: str, media_type: str = None):
        """执行一次 TMDB 搜索，返回 (results_list, item_or_None)。

        media_type: 可选，指定搜索类型（"tv" 或 "movie"）。
        当文件名有季数标记时，优先用 tv 搜索避免选到电影。
        """
        endpoint = f"https://api.themoviedb.org/3/search/{media_type}" if media_type else "https://api.themoviedb.org/3/search/multi"
        st, text = await _aio_get(
            endpoint,
            params={"api_key": TMDB_API_KEY, "query": q, "language": TMDB_LANG, "page": 1},
        )
        if st != 200:
            return [], None
        results = (_json.loads(text).get("results") or [])
        if not results:
            return [], None
        item = await _pick_tmdb_item(results, year, season, query_title=q)
        return results, item

    try:
        # 0) 有精确 TMDB id 标记 → 直接按 id 查（绕过搜索）
        if tmdb_id:
            det = await _tmdb_detail("tv", tmdb_id) or await _tmdb_detail("movie", tmdb_id)
            if det:
                return det

        # 1) 首次搜索：有季数标记时优先用 tv 搜索
        use_tv_search = season is not None and season >= 1
        search_type = "tv" if use_tv_search else None
        results, item = await _do_search(title, media_type=search_type)
        logger.info(f"🔍 TMDB 搜索: {title!r} (type={search_type or 'multi'})")

        # 2) 无结果时，去掉常见前缀重试（如 "央视百家讲坛" → "百家讲坛"）
        if not results and title:
            stripped = _TMDB_STRIP_PREFIXES.sub("", title).strip()
            if stripped and stripped != title:
                logger.info(f"🔍 TMDB 前缀剥离重试: {title!r} -> {stripped!r}")
                results, item = await _do_search(stripped)

        if not results or not item:
            return None

        # 智能判断 media_type：如果搜索结果中没有，根据文件名特征推断
        media_type = item.get("media_type")
        if not media_type:
            # 有季数标记 → 大概率是电视剧
            if season is not None and season >= 1:
                media_type = "tv"
                logger.info(f"ℹ️ 根据季数标记推断 media_type=tv: {title!r}")
            else:
                media_type = "movie"
                logger.info(f"ℹ️ 无季数标记，默认 media_type=movie: {title!r}")

        item_id = item.get("id")
        if not item_id:
            return None
        # 3) 详情：拿中文原始名 / 准确类型 / 简介
        det = await _tmdb_detail(media_type, item_id, item)
        # 4) OpenAI 终审：搜索命中的候选必须经 LLM 确认与源文件名是同一部作品。
        #    明确否决（False）→ 返回 None 走回退；LLM 不可用/不确定（None）→ 放行。
        if det and source_name:
            verdict = await llm_verify_match(source_name, det)
            if verdict is False:
                logger.warning(
                    f"🚫 OpenAI 否决 TMDB 候选，放弃: {title!r} -> {det.get('title')!r} "
                    f"(源文件: {source_name[:50]})"
                )
                return None
        return det
    except Exception as e:
        logger.warning(f"TMDB 查询失败: {e}")
        return None


async def douban_rating(title: str):
    """尽力而为：豆瓣无官方公开 API，失败返回 ''。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://movie.douban.com/"}
        from urllib.parse import quote
        st, text = await _aio_get(
            f"https://movie.douban.com/j/subject_suggest?q={quote(title)}", headers=headers)
        if st != 200:
            return ""
        items = __import__("json").loads(text) or []
        if not items:
            return ""
        url = items[0].get("url")
        if not url:
            return ""
        pst, ptext = await _aio_get(url, headers=headers)
        if pst != 200:
            return ""
        m = re.search(r'property="v:average"[^>]*content="([\d.]+)"', ptext)
        if m:
            return f"{float(m.group(1)):.1f}"
        return ""
    except Exception as e:
        logger.warning(f"豆瓣评分获取失败: {e}")
        return ""


async def fetch_poster_bytes(poster_url: str):
    if not poster_url:
        return None
    data = await _aio_get_bytes(poster_url)
    return data or None


# ── 115 分享信息查询（标题/大小），失败则降级 ─────────
def _parse_share_url(share_url: str):
    m = re.search(r"/(?:115\.com|115cdn\.com|anxia\.com)/s/([A-Za-z0-9]+)", share_url)
    if not m:
        return None, None
    code = m.group(1)
    rc = ""
    pm = re.search(r"[?&]password=([A-Za-z0-9]+)", share_url)
    if pm:
        rc = pm.group(1)
    return code, rc


def _walk_share_files(sfs, dir_id):
    """按目录 ID 递归遍历 115 分享中的所有文件，返回 [(name, size), ...]。"""
    out = []
    try:
        items = list(sfs.iterdir(dir_id))
    except Exception as e:
        logger.warning(f"iterdir 失败 id={dir_id}: {e}")
        return out
    for it in items:
        name = it.get("name") or it.get("file_name") or ""
        size = int(it.get("size") or it.get("file_size") or 0)
        is_dir = bool(it.get("is_dir")) or it.get("type") in (1, "1", "folder") \
                 or (it.get("file_id") is None and it.get("cid") is not None) \
                 or (it.get("category_id") and it.get("file_id") is None)
        if is_dir:
            child_id = it.get("file_id") or it.get("id") or it.get("cid")
            if child_id is None:
                continue
            out.extend(_walk_share_files(sfs, child_id))
        else:
            out.append((name, size))
    return out


async def fetch_share_info(share_url: str):
    """直接用 p115client.share_snap_app（登录鉴权，更不容易 405）取分享根名+总大小。"""
    code, rc = _parse_share_url(share_url)
    if not code:
        return None, None
    client = get_svc().client
    # 优先用登录鉴权的 app 端点；失败回退到 webapi
    snap = None
    for fn_name in ("share_snap_app", "share_snap"):
        try:
            method = getattr(client, fn_name, None)
            if not method:
                continue
            payload = {
                "share_code": code,
                "receive_code": rc or "",
                "cid": 0,
                "limit": 1000,
                "offset": 0,
            }
            cor = method(payload, async_=True)
            data = await cor
            if isinstance(data, dict):
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                if inner and (inner.get("shareinfo") or inner.get("list") is not None or inner.get("count") is not None):
                    snap = inner
                    break
                if inner and inner.get("state") is False:
                    logger.warning(f"{fn_name} 返回 state=false: {inner.get('error') or inner.get('message')}")
                else:
                    snap = inner
                    break
        except Exception as e:
            logger.warning(f"{fn_name} 失败: {e}")
            continue

    if not isinstance(snap, dict):
        logger.warning("获取 115 分享信息失败（两个端点都不可用）")
        return None, None

    shareinfo = snap.get("shareinfo") or snap.get("share_info") or {}
    top_name = shareinfo.get("share_title") or shareinfo.get("share_name") or ""
    total = int(shareinfo.get("file_size") or 0)
    return (top_name or None), (total or None)


# ── 真实文件遍历（按 115 分享内实际视频文件数识别集数）──
VIDEO_EXT = (".mkv", ".mp4", ".ts", ".m2ts", ".webm", ".avi", ".mov", ".m4v", ".iso", ".img", ".nrg", ".mdf", ".mds", ".bin", ".cue")
SE_RE = re.compile(r"S(\d{1,2})E(\d{1,3})", re.I)
EONLY_RE = re.compile(r"(?:^|[\s._-])E(\d{1,3})(?:[\s._-]|$)", re.I)
# 完结标记：分享根名/视频文件名含这些 → 该季写 Sxx 不带 E
COMPLETE_KW = re.compile(r"complete|finished|\bfin\b|完结|全集|全季", re.I)
AUDIO_RE = re.compile(r"(DDP\d+(?:\.\d+)?|DTS\d+(?:\.\d+)?|TrueHD|Atmos|AC3|AAC\d?|FLAC|LPCM)", re.I)


async def fetch_share_video_files(share_url: str, max_files: int = 3000,
                                  include_scan_status: bool = False):
    """递归列出 115 分享里的真实视频文件名（含子文件夹）。

    默认返回 (video_names, root_name)；include_scan_status=True 时额外返回扫描状态：
    complete（完整）、unavailable（取消/删除等终态）、error（临时失败）。
    临时扫描失败不能等同于分享中没有视频。
    走已验证的登录端点 share_snap_app（带 cid 翻页），规避 P115ShareFileSystem.from_url 的 405 频控。
    """
    vids = []
    root_name = ""

    def result(scan_status: str):
        if include_scan_status:
            return vids, root_name, scan_status
        return vids, root_name

    code, rc = _parse_share_url(share_url)
    if not code:
        return result("error")
    try:
        client = get_svc().client
        if client is None:
            return result("error")

        async def list_items(cid):
            items = []
            offset = 0
            page_limit = 1000
            seen_pages = set()
            while True:
                data = await client.share_snap_app({
                    "share_code": code, "receive_code": rc or "", "cid": cid,
                    "limit": page_limit, "offset": offset
                }, async_=True)
                if not isinstance(data, dict):
                    raise RuntimeError("115 分享目录接口返回了无效响应")
                inner = data.get("data") if isinstance(data.get("data"), dict) else data
                if not isinstance(inner, dict):
                    raise RuntimeError("115 分享目录接口缺少 data")
                if data.get("state") is False or inner.get("state") is False:
                    reason = inner.get("error") or inner.get("message") \
                        or data.get("error") or data.get("message") or "未知错误"
                    raise RuntimeError(f"115 分享目录扫描失败: {reason}")
                if "list" not in inner:
                    raise RuntimeError("115 分享目录接口缺少文件列表")
                page = inner.get("list") or []
                if not isinstance(page, list):
                    raise RuntimeError("115 分享目录文件列表格式无效")
                if not page:
                    break
                signature = tuple(
                    (it.get("fid"), it.get("fn")) for it in page if isinstance(it, dict)
                )
                if signature in seen_pages:
                    raise RuntimeError(f"115 分享目录分页重复: cid={cid}, offset={offset}")
                seen_pages.add(signature)
                items.extend(page)
                offset += len(page)
                try:
                    total = int(inner.get("count") or 0)
                except (TypeError, ValueError):
                    total = 0
                if (total and offset >= total) or (not total and len(page) < page_limit):
                    break
            return items

        root_list = await list_items(0)
        root_name = (root_list[0].get("fn") if root_list else "") or ""
        visited_cids = set()

        async def walk(cid):
            if cid in visited_cids or len(vids) >= max_files:
                return
            visited_cids.add(cid)
            for it in await list_items(cid):
                fn = it.get("fn") or ""
                fc = it.get("fc")
                is_folder = (fc in (0, "0")) or bool(it.get("is_dir"))
                if is_folder:
                    await walk(it.get("fid"))
                elif video_ext(fn) or is_image_resource(fn):
                    vids.append(fn)
                    if len(vids) >= max_files:
                        return

        for it in root_list:
            fc = it.get("fc")
            is_folder = (fc in (0, "0")) or bool(it.get("is_dir"))
            if is_folder:
                await walk(it.get("fid"))
            elif video_ext(it.get("fn") or "") or is_image_resource(it.get("fn") or ""):
                vids.append(it.get("fn"))
        return result("complete")
    except Exception as e:
        logger.warning(f"fetch_share_video_files 失败: {e}")
        scan_status = "unavailable" if _is_unrecoverable_reason(str(e)) else "error"
        return result(scan_status)


def _has_no_usable_video(video_names, scan_status: str) -> bool:
    return bool(not video_names and scan_status in {"complete", "unavailable"})


def infer_episode_range(video_names, root_name=""):
    """根据真实视频文件名推断集数显示串。

    - 含 SxxEyy：按季分组，取每季最小/最大集。
    - 无完结标记：S01E01-E28（按真实文件首末集）。
    - 含完结标记（文件名/根名有 complete/完结/全集/全季 等）：整季直接写 S01，不带 E。
    - 仅 E 编号：E01-E28。
    """
    pairs = []
    for fn in video_names:
        m = SE_RE.search(fn)
        if m:
            pairs.append((int(m.group(1)), int(m.group(2))))
    if not pairs:
        eonly = []
        for fn in video_names:
            m = EONLY_RE.search(fn)
            if m:
                eonly.append(int(m.group(1)))
        if eonly:
            lo, hi = min(eonly), max(eonly)
            return f"E{lo:02d}" + (f"-E{hi:02d}" if hi > lo else "")
        return ""

    seasons = {}
    for s, e in pairs:
        seasons.setdefault(s, set()).add(e)
    complete = bool(COMPLETE_KW.search(root_name)) or any(COMPLETE_KW.search(fn) for fn in video_names)
    parts = []
    for s in sorted(seasons):
        es = sorted(seasons[s])
        if complete:
            parts.append(f"S{s:02d}")
        else:
            lo, hi = es[0], es[-1]
            parts.append(f"S{s:02d}E{lo:02d}" + (f"-E{hi:02d}" if hi > lo else ""))
    return " ".join(parts)


def infer_audio_codec(video_names):
    """从真实视频文件名提取音频编码（取出现次数最多的，如 DDP5.1/DTS5.1/TrueHD）。"""
    cnt = {}
    for fn in video_names:
        m = AUDIO_RE.search(fn)
        if m:
            k = m.group(1).upper()
            cnt[k] = cnt.get(k, 0) + 1
    if not cnt:
        return ""
    return max(cnt, key=cnt.get)


# ── 卡片渲染 ──────────────────────────────────────────
def build_card(title, year, genres, tmdb_rating, douban_rating, quality,
               source, size_text, episode, encode, audio, share_link, overview):
    """构造 Telegram HTML 卡片文本（发图时作为 caption）。"""
    def H(s): return html_escape(str(s) if s is not None else "", quote=False)
    def HURL(s): return html_escape(str(s) if s is not None else "", quote=True)

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
    # extra 行：真实集数 + 画质 + 源 + 视频编码 + 音频编码（全部来自 115 真实文件）
    extra = " ".join(x for x in [episode, quality, source, encode, audio] if x)
    if extra:
        # 用 <pre> 包整行：Telegram 会渲染成等宽字体并带「复制」按钮（一键复制分辨率/集数/编码）
        lines.append(f"<pre>{H(extra)}</pre>")
    lines.append("")
    # 「115网盘」作为可点击蓝色锚
    lines.append(f'🔗 链接：<a href="{HURL(share_link)}">115网盘</a>')
    if overview:
        lines.append("")
        lines.append("📖 简介：")
        ov = overview.strip()
        if len(ov) > 300:
            ov = ov[:300] + "…"
        # 简介也用 <pre> 包，带「复制」按钮方便整段带走
        lines.append(f"<pre>{H(ov)}</pre>")
    # 标签
    tags = [f"#{H(title)}"]
    if genres and genres != "暂无":
        tags += [f"#{H(g)}" for g in genres.split("、") if g]
    lines.append("")
    lines.append("🏷 标签：" + " ".join(tags))
    text = "\n".join(lines)
    # Telegram caption 上限 1024
    if len(text.encode("utf-8")) > 1020:
        text = text[:1010] + "…"
    return text


# ── Telegram Bot ─────────────────────────────────────
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BufferedInputFile

DEDUP_WINDOW = 60
_processing = {}

# 全局转存串行锁：用户批量发链接时并发打 115 接口会触发"操作太频繁"风控，
# 串行排队逐个转存（每条约 10-30s），从根源上避免并发频控。
_SAVE_LOCK = asyncio.Lock()
_SAVE_RETRY_DELAYS = (10, 20, 30)  # 115 频控退避重试间隔（秒）

# ── 全自动失败重试队列 ──
# 转存+重命名+分享失败的项目自动进入队列，后台 worker 每 60s 自动重试，
# 成功后自动发卡片并通知用户——全自动流程，不需要用户手动重发。
_RETRY_QUEUE = []          # list[dict]：待重试的分享任务
_RETRY_MAX = 240            # 最大重试轮次（240分钟=4小时），超过后放弃
# 注意：115 审核有时需要 1-2 小时，但 90 分钟已经足够；超时后用户可手动重发
_RETRY_INTERVAL = 60       # worker 扫描间隔（秒）
_RETRY_405_PAUSE = 1800    # 检测到连续405错误时，暂停重试 30 分钟（让115限流解除）
RETRY_STATE_FILE = os.getenv("RETRY_STATE_FILE", "/cardbot-state/retry_queue.json")
PROCESSED_STATE_FILE = os.getenv("PROCESSED_STATE_FILE", "/cardbot-state/processed_links.json")
PIPELINE_QUEUE_DIR = Path(os.getenv("PIPELINE_QUEUE_DIR", "/cardbot-state/mount-pipeline-queue"))
PIPELINE_P115_COOKIE_FILE = Path(os.getenv(
    "PIPELINE_P115_COOKIE_FILE",
    "/cardbot/cd2-mount-mover/secrets/p115_pipeline_cookie.txt",
))
PIPELINE_115_ROOT = os.getenv("PIPELINE_115_ROOT", "/自动转存").strip().rstrip("/") or "/"
PIPELINE_INTERVAL = max(15, int(os.getenv("PIPELINE_INTERVAL", "60")))
PIPELINE_GROUP_DELAY = max(60, int(os.getenv("PIPELINE_GROUP_DELAY", "300")))
PIPELINE_SERIES_DELAY = max(60, int(os.getenv("PIPELINE_SERIES_DELAY", "1800")))
PIPELINE_TASK_TIMEOUT = max(60, int(os.getenv("PIPELINE_TASK_TIMEOUT", "180")))
_PROCESSED_LINKS = set()
_bot = None                # worker 发送卡片用的全局 bot（main 启动时赋值）


def _safe_log_url(url: str) -> str:
    """日志只保留分享码，避免把访问密码/中文参数写入日志。"""
    try:
        parts = urlsplit(url or "")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "(share-url)"


def _retry_reason(res) -> str:
    """统一识别审核中、空间不足等可恢复状态。"""
    if isinstance(res, dict):
        if res.get("error_type") == "share_audit_pending":
            return str(res.get("message") or "115 分享仍在系统处理中，继续轮询")
        if res.get("reason") == "no_space":
            return "115账号空间不足，暂停重复转存，等待清理"
        if res.get("message"):
            return str(res["message"])
        if res.get("error_type"):
            return str(res["error_type"])
    return "未知错误"


def _is_no_space_reason(reason: str) -> bool:
    text = str(reason or "")
    return "空间不足" in text or "扩容" in text or "no_space" in text


def _is_unrecoverable_reason(reason: str) -> bool:
    """检查是否为不可恢复的错误，应直接取消重试。"""
    text = str(reason or "")
    if "no_video_files" in text or "没有可用视频文件" in text:
        return True
    # 违规文件/审核未通过：资源本身有问题，重试无意义
    if "违规文件" in text or "审核未通过" in text:
        return True
    # 分享已取消：用户或系统取消了分享
    if "分享已取消" in text or "已取消" in text:
        return True
    # 分享不存在/已删除
    if "分享不存在" in text or "分享已失效" in text or "已删除" in text or "not found" in text.lower():
        return True
    return False


def _load_retry_queue() -> None:
    """从持久化文件恢复待重试队列，避免重启后重置/丢失轮次。"""
    global _RETRY_QUEUE
    try:
        with open(RETRY_STATE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        loaded_queue = data if isinstance(data, list) else []
        _RETRY_QUEUE = []
        migrated = False
        seen_keys = set()
        for item in loaded_queue:
            item_key = _link_key(item.get("url"))
            if item_key in seen_keys:
                migrated = True
                continue
            seen_keys.add(item_key)
            _RETRY_QUEUE.append(item)
            item["retry_count"] = int(item.get("retry_count", 0))
            # 旧版本在空间不足时会把原因记成“未知错误”，重启后立即重复转存。
            # 先延迟一次，给清理 worker 留出完成时间，避免再次制造失败目录。
            if item.get("retry_count", 0) > 0 and not item.get("next_retry_at") and item.get("fail_reason") == "未知错误":
                item["next_retry_at"] = time.time() + 15 * 60
                item["fail_reason"] = "待清理空间后重试"
                migrated = True
        if migrated:
            _save_retry_queue()
        logger.info(f"🔁 自动重试队列已恢复：{len(_RETRY_QUEUE)} 项")
    except FileNotFoundError:
        _RETRY_QUEUE = []
    except Exception as e:
        logger.warning(f"⚠️ 自动重试队列读取失败，使用空队列: {e}")
        _RETRY_QUEUE = []


def _save_retry_queue() -> None:
    """原子保存重试队列；失败不影响当前内存任务继续执行。"""
    try:
        os.makedirs(os.path.dirname(RETRY_STATE_FILE), exist_ok=True)
        tmp = RETRY_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(_RETRY_QUEUE, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, RETRY_STATE_FILE)
    except Exception as e:
        logger.warning(f"⚠️ 自动重试队列保存失败: {e}")


def _link_key(url: str) -> str:
    """按分享码规范化 115 链接，忽略域名、大小写和 password 参数差异。"""
    value = str(url or "").strip()
    if value.lower().startswith("ed2k://"):
        return "ed2k:" + urllib.parse.unquote(value).lower()
    try:
        parts = urlsplit(value)
        match = re.search(r"/s/([A-Za-z0-9]+)", parts.path, re.I)
        if match:
            return "115:" + match.group(1).lower()
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    except Exception:
        return value.lower()


def _load_processed_links() -> None:
    """恢复已完成链接，避免机器人重启后再次处理相同分享。"""
    global _PROCESSED_LINKS
    try:
        with open(PROCESSED_STATE_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        links = data.get("links", []) if isinstance(data, dict) else data
        _PROCESSED_LINKS = {_link_key(item) for item in links if item}
        logger.info(f"✅ 已完成链接记录已恢复：{len(_PROCESSED_LINKS)} 项")
    except FileNotFoundError:
        _PROCESSED_LINKS = set()
    except Exception as e:
        logger.warning(f"⚠️ 已完成链接记录读取失败，使用空记录: {e}")
        _PROCESSED_LINKS = set()


def _save_processed_links() -> None:
    """原子保存已完成链接记录。"""
    try:
        directory = os.path.dirname(PROCESSED_STATE_FILE) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = PROCESSED_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(sorted(_PROCESSED_LINKS), fp, ensure_ascii=False, indent=2)
        os.replace(tmp, PROCESSED_STATE_FILE)
    except Exception as e:
        logger.warning(f"⚠️ 已完成链接记录保存失败: {e}")


def _mark_processed_links(*urls: str) -> None:
    """记录原始分享和机器人生成的分享，防止两种链接互相触发重复处理。"""
    keys = {_link_key(url) for url in urls if url}
    new_keys = keys - _PROCESSED_LINKS
    if new_keys:
        _PROCESSED_LINKS.update(new_keys)
        _save_processed_links()


def _retry_queue_has_url(url: str) -> bool:
    """避免原始分享或已生成的待审核分享重复进入处理流程。"""
    key = _link_key(url)
    return any(
        key in {_link_key(item.get("url")), _link_key(item.get("pending_share_link"))}
        for item in _RETRY_QUEUE
    )


def make_bot():
    session = None
    if TG_PROXY:
        session = AiohttpSession(proxy=TG_PROXY)
    return Bot(token=TG_BOT_TOKEN, session=session)


def _message_sender_id(message) -> str:
    sender = getattr(message, "from_user", None)
    return str(getattr(sender, "id", "") or "")


def _can_submit(message) -> bool:
    """管理员和投稿白名单可处理资源；旧配置完全为空时保持开放。"""
    if not TG_USER_ID and not TG_SUBMITTER_IDS:
        return True
    sender_id = _message_sender_id(message)
    return bool(sender_id and (sender_id == TG_USER_ID or sender_id in TG_SUBMITTER_IDS))


async def _require_submit_permission(message) -> bool:
    if _can_submit(message):
        return True
    sender_id = _message_sender_id(message) or "无法识别"
    logger.warning(f"⛔ 无投稿权限的消息已拒绝: user_id={sender_id}")
    try:
        await message.answer(
            f"⛔ 您暂时没有投稿权限。\n用户 ID：<code>{sender_id}</code>\n请联系管理员添加权限。",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"投稿权限拒绝提示发送失败: {e}")
    return False


LINK_RE = re.compile(r"https?://(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9]+(?:[\?#][^\s<>]+)?", re.I)
# ed2k://|file|文件名|文件大小|哈希|/，文件名通常已 URL 编码，允许 %、点号和空格编码。
ED2K_RE = re.compile(r"ed2k://\|file\|[^|\r\n]+\|\d+\|[A-Fa-f0-9]{32}\|/", re.I)

# Telegram 转发消息常在链接前后/中间插入零宽空格(\u200b)、RTL 隔离符(\u2066-\u2069)、
# BOM(\ufeff) 等控制字符：夹在链接中间会让 URL 提取被截断（判无效链接），
# 跟在 [^\s]+ 后会被吞入（URL 带乱码）。提取前统一清除。
_CTRL_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u2066-\u2069\ufeff]")
_URL_TAIL_RE = re.compile(r"[^A-Za-z0-9?=&:/._-]+$")
_ED2K_TAIL_RE = re.compile(r"[\s。，,；;！!？?#]+$")


def _clean_share_text(text: str) -> str:
    """清除链接提取前的控制字符，避免 URL 被截断/污染。"""
    return _CTRL_CHARS_RE.sub("", text or "")


def _extract_links(text: str) -> list:
    """同时提取 115 与 ed2k 链接，并清理转发消息尾部标点。"""
    clean = _clean_share_text(text)
    links = [_URL_TAIL_RE.sub("", u) for u in LINK_RE.findall(clean)]
    ed2k_candidates = ED2K_RE.findall(clean)
    for encoded in re.findall(r"ed2k://[^\s<>]+", clean, re.I):
        decoded = urllib.parse.unquote(encoded)
        if ED2K_RE.fullmatch(decoded):
            ed2k_candidates.append(decoded)
    links += [_ED2K_TAIL_RE.sub("", u) for u in ed2k_candidates]
    # 保持消息中的出现顺序，避免多个链接时顺序变化。
    return sorted(set(links), key=clean.find)


def _telegram_entity_text(text: str, entity) -> str:
    """按 Telegram 的 UTF-16 code unit 偏移提取实体文本。"""
    try:
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        encoded = (text or "").encode("utf-16-le")
        return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le")
    except (UnicodeError, ValueError, TypeError):
        return ""


def _extract_message_links(message) -> list:
    """提取消息可见文本、隐藏文字链接和内联按钮中的分享链接。"""
    links = []

    def append_from(value):
        for link in _extract_links(str(value or "")):
            if link not in links:
                links.append(link)

    for text, entities in (
        (getattr(message, "text", None), getattr(message, "entities", None)),
        (getattr(message, "caption", None), getattr(message, "caption_entities", None)),
    ):
        append_from(text)
        for entity in entities or []:
            entity_type = getattr(entity, "type", "")
            entity_type = getattr(entity_type, "value", entity_type)
            if entity_type == "text_link":
                append_from(getattr(entity, "url", ""))
            elif entity_type == "url":
                append_from(_telegram_entity_text(text or "", entity))

    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "inline_keyboard", None) or []:
        for button in row:
            append_from(getattr(button, "url", ""))
    return links


def _ed2k_group_key(url: str):
    """同一消息内按剧名+年份+季数合并 ED2K；无季数的单文件不合并。"""
    try:
        data = _parse_ed2k(url)
        parsed = parse_filename(data["name"])
        m = re.search(r"[Ss](\d{1,2})[Ee]\d{1,3}", data["name"])
        if not m:
            return None
        return (
            re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", parsed["title"].lower()),
            parsed.get("year", ""),
            int(m.group(1)),
        )
    except Exception:
        return None


def _parse_ed2k(url: str) -> dict:
    """解析 ed2k 文件链接，不下载文件、不连接 115。"""
    url = urllib.parse.unquote(str(url or "").strip())
    parts = url.split("|")
    if len(parts) != 6 or parts[0].lower() != "ed2k://" or parts[1].lower() != "file":
        raise ValueError("ed2k 链接格式不完整")
    name = urllib.parse.unquote(parts[2])
    size = int(parts[3])
    file_hash = parts[4].upper()
    if not name or size < 0 or not re.fullmatch(r"[A-F0-9]{32}", file_hash, re.I):
        raise ValueError("ed2k 文件名、大小或哈希无效")
    return {"url": url, "name": name, "size": size, "hash": file_hash}


async def handle_message(message: types.Message):
    chat_id = str(message.chat.id)
    if TG_ALLOW_CHATS and chat_id not in TG_ALLOW_CHATS:
        return
    if not await _require_submit_permission(message):
        return

    full = _clean_share_text(message.caption or message.text or "")
    links = _extract_message_links(message)
    if not AUTO_PROCESS_115_LINKS:
        links = [link for link in links if link.lower().startswith("ed2k://")]
        if not links:
            return
    if not links:
        if full.startswith("/"):
            return
        try:
            await message.answer("⚠️ 请发送有效的 115 或 ed2k 分享链接。\n支持域名: 115.com, 115cdn.com, anxia.com、ed2k://")
        except Exception as e:
            logger.warning(f"回复失败（不影响）: {e}")
        return

    # 已完成、已排队或正在处理的链接都在入口直接跳过，避免重复识别、转存和提示。
    real = []
    for u in links:
        key = _link_key(u)
        if key in _PROCESSED_LINKS or _retry_queue_has_url(u) or key in _processing:
            logger.info(f"⏭️ 重复链接已跳过: {_safe_log_url(u)}")
            continue
        _processing[key] = asyncio.get_event_loop().time()
        real.append(u)
    if not real:
        return

    # 自定义名称：仅对 115 链接支持 "链接 | 名称" 或 "名称 | 链接"。
    # ed2k 自身使用 | 分隔，不能参与这段拆分，否则文件名会被误当作 user_name。
    user_name = ""
    if not any(u.lower().startswith("ed2k://") for u in links):
        parts = re.split(r"\s*\|\s*", full)
        if len(parts) >= 2:
            for p in parts:
                if LINK_RE.search(p):
                    continue
                if p.strip():
                    user_name = p.strip()
                    break
    # 防 Postedia/HDHive 等转发机器人在 caption 里写 "💰 免费资源 | 耗时 0.8s 🚗 永V..."
    # 把占位字符串当 user_name 污染 base_name。命中占位关键词则忽略
    if user_name and SHARE_PLACEHOLDER_KW.search(user_name):
        logger.info(f"user_name 含占位关键词，忽略: {user_name!r}")
        user_name = ""

    # ⚠️ 关键：answer 失败绝不能吞掉后续处理。Telegram API 网络抖动时 aiohttp 会抛
    # TelegramNetworkError，若不捕获整个 handler 直接退出，real 里的链接一个都不会转存。
    # status 为 None 时 process_one 内部 edit_text 已有容错。
    try:
        status = await message.answer(f"⌛️ 正在处理 {len(real)} 个链接，请稍候…")
    except Exception as e:
        logger.warning(f"状态回复失败（继续处理）: {e}")
        status = None
    # 同一消息内同剧同季的 ED2K 合并为一组，其他链接保持独立。
    ed2k_groups = {}
    for u in real:
        key = _ed2k_group_key(u) if u.lower().startswith("ed2k://") else None
        if key:
            ed2k_groups.setdefault(key, []).append(u)
    handled_groups = set()
    for url in real:
        try:
            key = _ed2k_group_key(url) if url.lower().startswith("ed2k://") else None
            grouped = ed2k_groups.get(key) if key and len(ed2k_groups.get(key, [])) > 1 else None
            if grouped:
                if key in handled_groups:
                    continue
                handled_groups.add(key)
            outcome = await process_one(url, user_name, message, status, grouped_urls=grouped)
            if isinstance(outcome, dict) and outcome.get("status") == "success" and outcome.get("dedup"):
                _mark_processed_links(url, *(outcome.get("dedup_links") or []))
        except Exception as e:
            logger.error(f"处理失败 {url}: {e}")
            try:
                await message.reply(f"❌ 处理失败：{url}\n{str(e)[:200]}")
            except Exception:
                pass
        finally:
            # 任务结束后释放本次运行锁；已完成/已排队状态由持久化记录控制。
            release_urls = grouped or [url]
            for release_url in release_urls:
                _processing.pop(_link_key(release_url), None)


async def _render_and_send_card(
    bot, target_chat: int, *, title, year, genres, tmdb_rating, douban_rating,
    quality, source, size_text, episode, encode, audio, share_link, overview,
    poster: bytes = None, private_to_user_id: int = None,
):
    """渲染卡片并发送。private_to_user_id 不为空时仅私聊发到该用户。

    海报只存在于内存(bytes)，从不写磁盘；发送完成后立即释放内存引用并强制回收，
    任何情况下都不留图片缓存。
    """
    card = build_card(
        title=title, year=year, genres=genres, tmdb_rating=tmdb_rating,
        douban_rating=douban_rating, quality=quality, source=source,
        size_text=size_text, episode=episode, encode=encode, audio=audio,
        share_link=share_link, overview=overview,
    )

    # aiogram 3 的 send_photo 不接受裸 bytes，必须包成 InputFile（纯内存，无落盘）
    photo_arg = BufferedInputFile(poster, filename="poster.jpg") if poster else None

    target = private_to_user_id or target_chat
    try:
        if photo_arg:
            await bot.send_photo(chat_id=target, photo=photo_arg,
                                 caption=card, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=target, text=card, parse_mode="HTML")
        return True
    except Exception as e:
        logger.error(f"发卡片失败: {e}")
        return False
    finally:
        # 发送完成（无论成败）立刻销毁海报内存，绝不留缓存
        del photo_arg
        del poster
        gc.collect()
async def _get_ready_dir_items(svc, cid, *, min_wait: float = 8, max_wait: float = 40,
                               expected_count: int = 0) -> list:
    """等待 115 转存目录落齐后再列目录，并按 ID 去重。

    expected_count > 0 时必须收集到该数量才提前结束，否则一直等到 max_wait。
    """
    started = asyncio.get_event_loop().time()
    last_count = -1
    stable = 0
    best = {}
    while True:
        try:
            items = await svc._get_dir_items(cid, strict=True)
        except Exception as e:
            logger.warning(f"⚠️ 等待目录 {cid} 列表失败: {e}")
            items = []
        current = {str(x["id"]): x for x in items if x.get("id")}
        if len(current) >= len(best):
            best.update(current)
        count = len(best)
        stable = stable + 1 if count == last_count else 0
        last_count = count
        elapsed = asyncio.get_event_loop().time() - started
        enough = (not expected_count) or count >= expected_count
        if elapsed >= min_wait and stable >= 1 and enough:
            logger.info(f"✅ 目录 {cid} 文件列表稳定：{count} 项（期望 {expected_count or '-'}，等待 {elapsed:.1f}s）")
            return list(best.values())
        if elapsed >= max_wait:
            logger.warning(f"⚠️ 目录 {cid} 等待超时：收集到 {count} 项，继续处理")
            return list(best.values())
        await asyncio.sleep(2)


async def _rename_video_tree(svc, folder_id, title_cn, year, tmdb_id, suffix="Cxuan"):
    """递归重命名目录树中的全部视频；保留 Season 等原有子目录名。"""
    ok = fail = total = 0
    sub = await _get_ready_dir_items(svc, folder_id, min_wait=2, max_wait=40)
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

    返回 (ok, fail, total)。
    ⚠️ 关键坑：p115client 的 fs_rename 每次只能改 1 个文件（payload 为单个
    (file_id, file_name) 元组/字典），一次性传整个 list 只生效前 ~12 项。必须逐条调用。
    """
    ok = fail = total = 0
    new_cid = None
    try:
        top = await _get_ready_dir_items(svc, to_cid, min_wait=8, max_wait=40)
        # 以 ID 去重，避免 115 分页回显重复页造成重复改名和 -N 后缀污染
        top = list({str(x["id"]): x for x in top}.values())
        dirs = [it for it in top if it.get("is_dir")]
        files = [it for it in top if not it.get("is_dir")]
        if not dirs and files:
            # ── 分享根无文件夹（单文件电影/无剧集目录）：自动建规范文件夹并移入 ──
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
            # 顶层文件夹按规范命名；视频递归扫描，支持 Season 1/Season 2 等多层结构。
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


def _is_rate_limited(res) -> bool:
    """判断是否为 115 频控/风控错误（"操作太频繁/频繁/请稍候再试"）。"""
    try:
        msg = ""
        if isinstance(res, dict):
            msg = f"{res.get('message') or ''} {res.get('error_type') or ''} {res.get('error') or ''}"
        else:
            msg = str(res)
        return ("频繁" in msg or "稍候再试" in msg or "操作太频繁" in msg
                or "频率" in msg or "被限制" in msg or "限流" in msg)
    except Exception:
        return False


async def _save_share_with_retry(svc, url: str, metadata: dict):
    """转存 _save_share_link_internal，遇 115 频控按 _SAVE_RETRY_DELAYS 退避重试。"""
    last = None
    for attempt in range(len(_SAVE_RETRY_DELAYS) + 1):
        try:
            res = await svc._save_share_link_internal(url, metadata, create_task_subdir=True)
            if res and res.get("status") == "success":
                return res
            last = res
            if not _is_rate_limited(res):
                return res  # 非频控错误，直接返回让上层处理
            logger.warning(f"⚠️ 转存频控（第 {attempt + 1} 次）: {res}")
        except Exception as e:
            last = e
            if not _is_rate_limited(e):
                raise
            logger.warning(f"⚠️ 转存频控异常（第 {attempt + 1} 次）: {e}")
        if attempt < len(_SAVE_RETRY_DELAYS) - 1:
            await asyncio.sleep(_SAVE_RETRY_DELAYS[attempt])
        else:
            break
    if isinstance(last, Exception):
        raise last
    return last


async def _refresh_retry_metadata(item: dict, share_link: str) -> None:
    """审核完成后从最终分享重新读取文件名，覆盖队列中的旧元数据。"""
    try:
        names, root = await fetch_share_video_files(share_link)
        if not names:
            return
        source = _select_source_name(names, root, "")
        parsed = parse_filename(source)
        real_ep = infer_episode_range(names, root)
        audio = infer_audio_codec(names)
        if real_ep:
            parsed["episode"] = real_ep
        # 统一识别：LLM 优先，正则 + 别名兜底
        _st_r, _sy_r, _ft_r, _fy_r, _season_r = await _resolve_identity(source, parsed)
        info = await tmdb_search(
            _st_r, _sy_r, _season_r, source_name=source
        ) if TMDB_API_KEY else None
        if not info and _st_r != _ft_r:
            info = await tmdb_search(
                _ft_r, _fy_r, _season_r, source_name=source
            ) if TMDB_API_KEY else None
        if not info:
            _hid_r = _title_tmdb_id(_st_r) or _title_tmdb_id(_ft_r)
            if _hid_r:
                logger.info(f"🔗 搜索未命中，使用内置绑定 ID 兜底: {_hid_r}")
                info = await tmdb_search(
                    _st_r, _sy_r, _season_r, tmdb_id=_hid_r
                ) if TMDB_API_KEY else None
        item.update({
            "display_title": (info or {}).get("title") or parsed["title"],
            "year": (info or {}).get("year") or parsed.get("year", ""),
            "tmdb_id": (info or {}).get("tmdb_id"),
            "poster_url": (info or {}).get("poster_url", ""),
            "genres": (info or {}).get("genres", "暂无"),
            "tmdb_rating": (info or {}).get("rating", "暂无评分"),
            "overview": (info or {}).get("overview", ""),
            "quality": parsed["quality"], "source": parsed["source"],
            "episode": parsed["episode"], "encode": parsed["encode"],
            "audio": audio,
            "size_text": item.get("size_text", ""),
        })
        logger.info(
            f"✅ 审核完成前复核元数据: {source} -> {item['display_title']} "
            f"(TMDB {item.get('tmdb_id') or '-'})"
        )
    except Exception as e:
        logger.warning(f"⚠️ 审核完成前复核元数据失败，保留原值: {e}")


async def _wait_share_audit_ready(svc, share_link: str, timeout: int = None,
                                   poll_interval: int = None) -> tuple[bool, str]:
    """等待 115 分享页的系统审核/文件快照处理完成。

    115 新建分享后可能短暂显示“文件正在系统处理中”。此时链接虽然已经生成，
    但分享页仍不可用，不能提前推送到频道。share_state=0 或 is_pending=True
    视为处理中；share_state=1 且未过期/未违规才允许发布。
    """
    timeout = SHARE_AUDIT_WAIT_TIMEOUT if timeout is None else max(int(timeout), 0)
    poll_interval = SHARE_AUDIT_POLL_INTERVAL if poll_interval is None else max(int(poll_interval), 5)
    deadline = asyncio.get_event_loop().time() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            status = await svc.get_share_status(share_link)
        except Exception as e:
            status = None
            logger.warning(f"⚠️ 115分享审核状态查询异常（第 {attempt} 次）: {e}")

        if status:
            if status.get("is_expired"):
                return False, "115 分享已失效"
            if status.get("is_prohibited"):
                return False, "115 分享包含违规文件，审核未通过"
            if not status.get("is_pending") and status.get("share_state") not in (0, None):
                logger.info(f"✅ 115 分享审核完成，允许推送频道: {_safe_log_url(share_link)}")
                return True, ""
            logger.info(
                f"⏳ 115 分享仍在系统处理中（本次查询第 {attempt} 次，状态={status.get('share_state')}），"
                f"{_safe_log_url(share_link)}"
            )

        # timeout=0 表示“只检查一次”：用于主流程快速返回并交给后台无限轮询，
        # 不能把它误报成“0分钟超时”。
        if timeout == 0:
            return False, "115 分享仍在系统处理中，继续轮询"

        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return False, f"115 分享审核等待超时（{timeout // 60} 分钟）"
        await asyncio.sleep(min(poll_interval, remaining))


async def save_rename_share(url: str, metadata: dict, title_cn: str, year: str, tmdb_id=None,
                            expected_video_count: int = 0):
    """转存 + 重命名（顶层文件夹与每个视频）+ 创建分享。返回与 save_and_share 同构结果。

    复用 P115Service._save_share_link_internal（转存）与 create_share_link（分享），
    中间用 _get_dir_items（带 405 fallback）拿真实文件 ID，再批量 fs_rename。
    转存遇 115 频控自动退避重试（10/20/30s）。
    """
    svc = get_svc()
    # 1) 转存（带频控重试）
    save_res = await _save_share_with_retry(svc, url, metadata)
    if not save_res or save_res.get("status") != "success":
        # P115Service 对原始分享审核中返回 pending/auditing；统一转换为卡片机器人可识别的轮询状态。
        if isinstance(save_res, dict) and save_res.get("status") == "pending":
            reason = str(save_res.get("reason") or "")
            if reason in {"auditing", "snapshotting"}:
                return {
                    "status": "error",
                    "error_type": "share_audit_pending",
                    "message": "原始115分享正在审核中，等待审核完成后继续转存",
                }
        return save_res
    to_cid = save_res.get("to_cid")
    if not to_cid:
        return save_res
    # 外层任务目录 CID：自动清理时必须删除整个任务目录，而不是只删分享中的内层规范文件夹。
    cleanup_cid = str(to_cid)
    cleanup_name = (save_res.get("names") or [title_cn])[0] if isinstance(save_res.get("names"), list) else title_cn

    # 2) 重命名：fs_rename 每次只能处理一个文件，统一走逐条重命名 helper
    _, _, _, new_cid = await _rename_saved(
        svc, to_cid, title_cn, year, tmdb_id, expected_video_count=expected_video_count,
    )
    # 分享根无文件夹时，_rename_saved 已在任务目录下创建规范文件夹并移入视频。
    # 保留任务目录作为分享父级，names 指向新文件夹；若把 to_cid 切到新文件夹，
    # create_share_link 会把视频当作顶层项目与单个 names 比较，导致校验永远失败。
    if new_cid:
        save_res = dict(save_res)
        save_res["names"] = [build_folder_name(title_cn, year, tmdb_id)]

    # 3) 创建分享（复用 P115Service 的 margin 重试逻辑）
    share_res = await svc.create_share_link(save_res)
    share_url = save_res.get("share_url") or url
    if isinstance(share_res, str):
        # 只做一次即时检查；若仍在115系统处理中，交给后台队列每60秒轮询，
        # 不长时间占用全局转存锁，避免后续任务被审核中的分享阻塞。
        ready, audit_reason = await _wait_share_audit_ready(svc, share_res, timeout=0)
        if not ready:
            terminal = ("失效" in audit_reason or "违规" in audit_reason)
            return {
                "status": "error",
                "error_type": "share_audit_terminal" if terminal else "share_audit_pending",
                "message": audit_reason, "share_url": share_url,
                "share_link": share_res, "cleanup_cid": cleanup_cid,
                "cleanup_name": cleanup_name,
            }
        return {"status": "success", "share_link": share_res, "share_url": share_url,
                "cleanup_cid": cleanup_cid, "cleanup_name": cleanup_name}
    if isinstance(share_res, dict) and share_res.get("status") == "margin_limited":
        return {
            "status": "margin_limited",
            "message": share_res.get("message") or "分享暂受限或接口风控，文件已转存，将自动补推",
            "share_url": share_url,
            "metadata": metadata or {},
            "limit_reason": share_res.get("limit_reason"),
        }
    if isinstance(share_res, dict) and share_res.get("status") == "error":
        return {
            "status": "error",
            "error_type": share_res.get("error_type", "share_failed"),
            "message": share_res.get("message", "生成分享链接失败"),
            "share_url": share_url,
        }
    return {"status": "error", "error_type": "share_failed", "message": "转存成功但生成分享链接失败", "share_url": share_url}


async def _retry_worker():
    """全自动后台重试：每 60s 扫描失败队列，逐个重跑转存+重命名+分享。

    - 成功：发卡片到频道 + 私聊通知用户，从队列移除
    - 临时失败/审核处理中：每 60 秒无限轮询，直到成功或得到失效/违规等终态结果
    - 绝不用原始链接发卡片；整个过程无需用户介入
    """
    global _bot
    while True:
        try:
            await asyncio.sleep(_RETRY_INTERVAL)
            if not _RETRY_QUEUE:
                continue
            item = _RETRY_QUEUE[0]
            if float(item.get("next_retry_at", 0) or 0) > time.time():
                _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                _save_retry_queue()
                continue
            try:
                # 已创建分享但仍在115审核：只查状态，不重复转存/创建分享。
                pending_link = item.get("pending_share_link") or ""
                retry_video_names, _, retry_scan_status = await fetch_share_video_files(
                    item["url"], include_scan_status=True,
                )
                if _has_no_usable_video(retry_video_names, retry_scan_status):
                    res = {
                        "status": "error",
                        "error_type": "no_video_files",
                        "message": "原分享中没有可用视频文件，可能资源已违规或被删除",
                    }
                elif pending_link:
                    svc = get_svc()
                    ready, pending_reason = await _wait_share_audit_ready(
                        svc, pending_link, timeout=0, poll_interval=SHARE_AUDIT_POLL_INTERVAL,
                    )
                    res = {
                        "status": "success" if ready else "error",
                        "share_link": pending_link if ready else "",
                        "message": pending_reason,
                        "error_type": "share_audit_pending" if not ready and pending_reason.startswith("115 分享仍在") else "",
                        "cleanup_cid": item.get("pending_cleanup_cid"),
                        "cleanup_name": item.get("pending_cleanup_name") or item["display_title"],
                    }
                else:
                    if retry_video_names:
                        item["expected_video_count"] = len(retry_video_names)
                    async with _SAVE_LOCK:
                        res = await save_rename_share(
                            item["url"],
                            {"description": item["user_name"] or item["url"],
                             "full_text": item["user_name"] or item["url"],
                             "command_mode": "share"},
                            item["display_title"], item["year"], item["tmdb_id"],
                            expected_video_count=item.get("expected_video_count", 0),
                        )
                ok = bool(res and res.get("status") == "success" and res.get("share_link"))
                share_link = res.get("share_link") if ok else ""
                if ok:
                    # 审核完成后以最终分享内真实文件名复核元数据，避免旧队列标题错配。
                    await _refresh_retry_metadata(item, share_link)
                    # 重新下载海报并推卡片到频道（发送后立即释放内存）
                    poster = await fetch_poster_bytes(item["poster_url"]) if item.get("poster_url") else None
                    try:
                        sent = await _render_and_send_card(
                            _bot, int(TG_CHANNEL_ID) if str(TG_CHANNEL_ID).lstrip("-").isdigit() else 0,
                            title=item["display_title"], year=item["year"], genres=item["genres"],
                            tmdb_rating=item["tmdb_rating"], douban_rating=item["douban"],
                            quality=item["quality"], source=item["source"], size_text=item["size_text"],
                            episode=item["episode"], encode=item["encode"], audio=item["audio"],
                            share_link=share_link, overview=item["overview"], poster=poster,
                        )
                    finally:
                        del poster
                    _mark_processed_links(item.get("url"), item.get("pending_share_link"), share_link)
                    logger.info(f"✅ 自动重试成功（第 {item['retry_count'] + 1} 轮）: {item['url']} -> {share_link}")
                    if sent and res.get("cleanup_cid"):
                        _schedule_cleanup(res.get("cleanup_cid"), res.get("cleanup_name") or item["display_title"], share_link)
                    if item.get("user_id"):
                        try:
                            await _bot.send_message(
                                item["user_id"],
                                f"✅ 之前失败的链接已自动重试成功\n"
                                f"原链接：{item['url']}\n"
                                f"新分享链接：{share_link}"
                                + ("" if sent else "\n（频道推送失败，请手动检查）"),
                            )
                        except Exception as e:
                            logger.warning(f"重试成功通知失败: {e}")
                    _RETRY_QUEUE.pop(0)
                    _save_retry_queue()
                    continue
                if isinstance(res, dict) and res.get("error_type") == "share_audit_pending" and res.get("share_link"):
                    item["pending_share_link"] = res.get("share_link")
                    item["pending_cleanup_cid"] = res.get("cleanup_cid")
                    item["pending_cleanup_name"] = res.get("cleanup_name")
                    item["fail_reason"] = str(res.get("message") or "新分享正在审核中")
                    item["next_retry_at"] = time.time() + 60
                    _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                    _save_retry_queue()
                    logger.info(f"⏳ 已保存待审核新分享，后续只检查状态: {_safe_log_url(item['url'])} -> {_safe_log_url(res.get('share_link'))}")
                    continue
                reason = _retry_reason(res)
            except Exception as e:
                reason = str(e)[:150]
            if reason.startswith("原始115分享正在审核中"):
                item["fail_reason"] = reason
                item["next_retry_at"] = time.time() + 5 * 60
                _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                _save_retry_queue()
                logger.info(f"⏸️ 原始分享审核中，5分钟后再检查: {_safe_log_url(item['url'])}")
                continue
            if _is_no_space_reason(reason):
                # 空间不足时不要每分钟重复转存，保留任务等待清理。
                item["retry_count"] = int(item.get("retry_count", 0))
                item["fail_reason"] = reason
                item["next_retry_at"] = time.time() + 15 * 60
                _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                _save_retry_queue()
                logger.warning(f"⏸️ 检测到空间不足，暂停该任务15分钟后再检查: {_safe_log_url(item['url'])} | {reason}")
                continue
            
            # 不可恢复的错误：直接取消重试，不再轮询
            if _is_unrecoverable_reason(reason):
                _RETRY_QUEUE.pop(0)
                _save_retry_queue()
                logger.warning(f"🚫 不可恢复错误，取消重试: {_safe_log_url(item['url'])} | {reason}")
                if item.get("user_id"):
                    try:
                        await _bot.send_message(
                            item["user_id"],
                            f"🚫 链接已取消重试（不可恢复错误）：{item['url']}\n原因：{reason}\n可稍后手动重发。",
                        )
                    except Exception:
                        pass
                continue
            
            # 本轮仍失败：计数 +1，超限放弃，否则移到队尾等下一轮。
            # retry_count 统计“后台扫描轮次”，而不是 _wait_share_audit_ready
            # 的单次状态查询次数，避免日志出现第88轮后新任务第1轮而造成误判。
            item["retry_count"] = int(item.get("retry_count", 0)) + 1
            # 检测连续405错误，暂停重试让115限流解除
            if "405" in str(reason) or "风控" in str(reason):
                item.setdefault("_405_count", 0)
                item["_405_count"] = item.get("_405_count", 0) + 1
                if item["_405_count"] >= 5:
                    pause_until = int(asyncio.get_event_loop().time()) + _RETRY_405_PAUSE
                    item["_pause_until"] = pause_until
                    logger.warning(
                        f"🧊 检测到连续115 API 405限流，暂停重试 {item['url']} "
                        f"30分钟（预计 {pause_until}）"
                    )
            else:
                item.pop("_405_count", None)
                item.pop("_pause_until", None)
            # 检查是否在暂停期内
            if item.get("_pause_until") and int(asyncio.get_event_loop().time()) < item["_pause_until"]:
                remaining = item["_pause_until"] - int(asyncio.get_event_loop().time())
                logger.info(
                    f"⏸️ 重试暂停中，剩余 {remaining//60}分钟: "
                    f"{_safe_log_url(item['url'])} | {reason}"
                )
                _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                _save_retry_queue()
                continue
            if _RETRY_MAX > 0 and item["retry_count"] >= _RETRY_MAX:
                _RETRY_QUEUE.pop(0)
                _save_retry_queue()
                # 审核长期未完成/失败时，避免转存目录永久占用空间；沿用统一回收站清理流程。
                if item.get("pending_share_link") and item.get("pending_cleanup_cid"):
                    _schedule_cleanup(
                        item["pending_cleanup_cid"],
                        item.get("pending_cleanup_name") or item["display_title"],
                        item.get("pending_share_link"),
                    )
                logger.error(f"⛔ 自动重试 {_RETRY_MAX} 轮仍失败，放弃: {item['url']} | {reason}")
                if item.get("user_id"):
                    try:
                        await _bot.send_message(
                            item["user_id"],
                            f"⛔ 链接自动重试 {_RETRY_MAX} 轮仍失败，已放弃：{item['url']}\n原因：{reason}\n可稍后手动重发。",
                        )
                    except Exception:
                        pass
            else:
                _RETRY_QUEUE.append(_RETRY_QUEUE.pop(0))
                _save_retry_queue()
                logger.warning(
                    f"🔁 自动重试第 {item['retry_count']} 轮仍失败，继续排队: "
                    f"{_safe_log_url(item['url'])} | {reason}"
                )
        except Exception as e:
            logger.warning(f"重试 worker 异常: {e}")


# ── 123 → 115 挂载闭环 ─────────────────────────────────
class _PipelinePending(Exception):
    """The 115 share exists but is still being audited."""


class _PipelineTerminal(Exception):
    """The 115 share reached a terminal state and must not be retried."""


def _pipeline_task_paths(suffix: str) -> list[Path]:
    PIPELINE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(PIPELINE_QUEUE_DIR.glob(f"*.{suffix}.json"))


def _pipeline_write(path: Path, task: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".pipeline.", dir=PIPELINE_QUEUE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(task, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _pipeline_read(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("闭环任务格式错误")
    return data


def _pipeline_find_completed_duplicate(relative_path: str, size: int = 0) -> dict | None:
    """Return an existing successful task for a duplicate mover event."""
    for path in _pipeline_task_paths("done"):
        try:
            candidate = _pipeline_read(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if candidate.get("relative_path") != relative_path or not candidate.get("card_sent"):
            continue
        if size and int(candidate.get("size", 0) or 0) != int(size):
            continue
        return candidate
    return None


def _pipeline_has_folder_task(relative_path: str) -> bool:
    """Whether a top-level directory already has an aggregate task."""
    parts = Path(relative_path).parts
    if len(parts) < 2:
        return False
    root = parts[0]
    for suffix in ("pending", "processing", "failed", "done"):
        for path in _pipeline_task_paths(suffix):
            try:
                task = _pipeline_read(path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if task.get("kind") == "folder" and str(task.get("relative_path", "")).strip("/") == root:
                return True
    return False


def _pipeline_series_identity(task: dict) -> str | None:
    """Return a stable show/season key for one root-level episode task."""
    relative = str(task.get("relative_path", "")).strip().strip("/").replace("\\", "/")
    if not relative or "/" in relative:
        return None
    filename = Path(relative).name
    episode = re.search(r"(?:^|[.\s_-])[Ss](\d{1,2})[.\s_-]?[Ee](\d{1,3})", filename)
    if not episode:
        return None
    season = int(episode.group(1))
    tmdb_id = task.get("tmdb_id")
    if tmdb_id:
        return f"tmdb:{tmdb_id}:s{season:02d}"

    title = str(task.get("display_title") or "").strip()
    if not title or title == filename:
        title = filename[:episode.start()]
    title = re.sub(r"\{[^{}]*tmdb[^{}]*\}", " ", title, flags=re.I)
    title = re.sub(r"[\s._-]*(19\d{2}|20\d{2})[\s._-]*$", " ", title)
    title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", title).lower()
    if not title:
        return None
    year = str(task.get("year") or "")
    if not year:
        match = re.search(r"(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![\dpPiI])", filename)
        year = match.group(1) if match else ""
    return f"title:{title}:{year}:s{season:02d}"


def _pipeline_series_group_ready(tasks: list[dict], now: float | None = None) -> bool:
    """Wait for the mover's expected batch or a quiet fallback window."""
    if len(tasks) < 2:
        return False
    expected = max(int(task.get("batch_expected_count", 0) or 0) for task in tasks)
    if expected and len({str(task.get("id")) for task in tasks}) >= expected:
        return True
    if all(task.get("share_link") for task in tasks):
        return True
    now = time.time() if now is None else now
    latest = max(
        float(task.get("last_seen_at", task.get("created_at", 0)) or 0)
        for task in tasks
    )
    return latest + PIPELINE_SERIES_DELAY <= now


def _pipeline_prepare_series_groups() -> int:
    """Collapse root-level episodes into durable aggregate queue tasks."""
    grouped = {}
    for suffix in ("pending", "failed"):
        for path in _pipeline_task_paths(suffix):
            try:
                task = _pipeline_read(path)
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if task.get("kind") in {"folder", "series"} or task.get("card_sent"):
                continue
            key = task.get("series_key") or _pipeline_series_identity(task)
            if key:
                grouped.setdefault(str(key), []).append((path, task))

    created = 0
    for key, entries in grouped.items():
        tasks = [task for _, task in entries]
        if not _pipeline_series_group_ready(tasks):
            continue
        member_ids = sorted(str(task.get("id") or path.stem) for path, task in entries)
        group_id = hashlib.sha256((key + "\0" + "\0".join(member_ids)).encode("utf-8")).hexdigest()[:32]
        base = PIPELINE_QUEUE_DIR / group_id
        existing = next(
            (Path(f"{base}.{suffix}.json") for suffix in ("pending", "processing", "failed", "done")
             if Path(f"{base}.{suffix}.json").exists()),
            None,
        )
        if existing is None:
            representative = next((task for task in tasks if task.get("canonical_name")), tasks[0])
            aggregate = {
                key_name: representative[key_name]
                for key_name in (
                    "display_title", "year", "tmdb_id", "poster_url", "genres", "tmdb_rating",
                    "douban", "overview", "quality", "source", "encode", "audio", "canonical_name",
                )
                if key_name in representative
            }
            aggregate.update({
                "id": group_id,
                "kind": "series",
                "series_key": key,
                "relative_path": representative.get("display_title") or Path(representative["relative_path"]).stem,
                "members": tasks,
                "member_count": len(tasks),
                "batch_expected_count": max(int(task.get("batch_expected_count", 0) or 0) for task in tasks),
                "size": sum(int(task.get("size", 0) or 0) for task in tasks),
                "episode": infer_episode_range([str(task.get("relative_path", "")) for task in tasks]),
                "created_at": min(float(task.get("created_at", 0) or 0) for task in tasks),
                "last_seen_at": max(float(task.get("last_seen_at", task.get("created_at", 0)) or 0) for task in tasks),
            })
            _pipeline_write(Path(f"{base}.pending.json"), aggregate)
            created += 1

        for path, task in entries:
            task["absorbed_by"] = group_id
            task["completed_at"] = time.time()
            task.pop("next_retry_at", None)
            done = path.with_name(path.name.replace(".pending.json", ".done.json").replace(".failed.json", ".done.json"))
            _pipeline_write(done, task)
            path.unlink(missing_ok=True)
        logger.info(f"📦 同批剧集已合并为一个分享任务: {key}（{len(tasks)} 集）")
    return created


def _pipeline_candidate_priority(path: Path) -> tuple[int, float, str]:
    """Audit existing links first so slow filesystem work cannot block publishing."""
    try:
        task = _pipeline_read(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return 3, 0, path.name
    if task.get("share_link"):
        priority = 0
    elif task.get("kind") == "series":
        priority = 1
    else:
        priority = 2
    return priority, float(task.get("created_at", 0) or 0), path.name


def _pipeline_series_wait_until(task: dict) -> float:
    """Return the fallback deadline for an ungrouped root episode."""
    if task.get("kind") == "series":
        return 0
    key = task.get("series_key") or _pipeline_series_identity(task)
    expected = int(task.get("batch_expected_count", 0) or 0)
    if not key or expected == 1 or task.get("share_link"):
        return 0
    seen_at = float(task.get("last_seen_at", task.get("created_at", 0)) or 0)
    return seen_at + PIPELINE_SERIES_DELAY


def _pipeline_recover_processing() -> None:
    """Return tasks interrupted by a card-bot restart to the retry queue."""
    for path in _pipeline_task_paths("processing"):
        failed = path.with_name(path.name.replace(".processing.json", ".failed.json"))
        try:
            os.replace(path, failed)
        except OSError as exc:
            logger.warning(f"闭环任务恢复失败: {path.name} | {exc}")


async def _pipeline_metadata(filename: str) -> dict:
    parsed = parse_filename(filename)
    search_title, search_year, fallback_title, fallback_year, season = await _resolve_identity(
        filename, parsed
    )
    marker_id = parse_tmdbid_marker(filename)
    info = await tmdb_search(
        search_title, search_year, season, tmdb_id=marker_id, source_name=filename,
    ) if TMDB_API_KEY else None
    if not info and search_title != fallback_title:
        info = await tmdb_search(
            fallback_title, fallback_year, season, tmdb_id=marker_id, source_name=filename,
        ) if TMDB_API_KEY else None
    if not info:
        fallback_id = _title_tmdb_id(search_title) or _title_tmdb_id(fallback_title)
        if fallback_id:
            info = await tmdb_search(search_title, search_year, season, tmdb_id=fallback_id)
    display_title = (info or {}).get("title") or parsed["title"]
    year = (info or {}).get("year") or parsed.get("year", "")
    episode = extract_episode(filename)
    episode_season = episode[0] if episode else None
    episode_number = episode[1] if episode else None
    audio = infer_audio_codec([filename])
    hdr = extract_hdr(filename)
    canonical_name = build_canonical_name(
        display_title, episode_season, episode_number, None, year,
        parsed["quality"], parsed["source"], audio, hdr, parsed["encode"],
        ext=video_ext(filename), suffix="Cxuan",
    )
    return {
        "display_title": display_title,
        "year": year,
        "tmdb_id": (info or {}).get("tmdb_id"),
        "poster_url": (info or {}).get("poster_url", ""),
        "genres": (info or {}).get("genres", "暂无"),
        "tmdb_rating": (info or {}).get("rating", "暂无评分"),
        "douban": await douban_rating(display_title) if display_title else "",
        "overview": (info or {}).get("overview", ""),
        "quality": parsed["quality"],
        "source": parsed["source"],
        "episode": parsed["episode"],
        "encode": parsed["encode"],
        "audio": audio,
        "canonical_name": canonical_name,
    }


async def _pipeline_collect_videos(svc, folder_id: int) -> list[dict]:
    """Recursively collect video entries below a mounted series folder."""
    videos = []
    seen_dirs = set()

    async def walk(cid):
        key = str(cid)
        if key in seen_dirs:
            return
        seen_dirs.add(key)
        items = await _get_ready_dir_items(svc, cid, min_wait=4, max_wait=30)
        for item in items:
            if item.get("is_dir"):
                await walk(item["id"])
            elif video_ext(item.get("name", "")):
                videos.append(item)

    await walk(folder_id)
    return videos


async def _pipeline_require_share_ready(svc, task: dict, share_link: str) -> None:
    ready, reason = await _wait_share_audit_ready(svc, share_link, timeout=0)
    if ready:
        return
    task["pending_share_link"] = share_link
    if _is_unrecoverable_reason(reason):
        raise _PipelineTerminal(reason)
    raise _PipelinePending(reason or "115 分享仍在系统处理中")


async def _pipeline_send_card(task: dict, share_link: str) -> None:
    if task.get("card_sent"):
        return
    poster = await fetch_poster_bytes(task["poster_url"]) if task.get("poster_url") else None
    try:
        channel_id = int(TG_CHANNEL_ID) if str(TG_CHANNEL_ID).lstrip("-").isdigit() else 0
        if not channel_id:
            raise RuntimeError("TG_CHANNEL_ID 未配置，无法发布闭环卡片")
        sent = await _render_and_send_card(
            _bot, channel_id,
            title=task["display_title"], year=task["year"], genres=task["genres"],
            tmdb_rating=task["tmdb_rating"], douban_rating=task["douban"],
            quality=task["quality"], source=task["source"],
            size_text=format_size(int(task.get("size", 0) or 0)), episode=task["episode"],
            encode=task["encode"], audio=task["audio"], share_link=share_link,
            overview=task["overview"], poster=poster,
        )
    finally:
        del poster
    if not sent:
        raise RuntimeError("闭环卡片发布失败")
    task["card_sent"] = True
    task["completed_at"] = time.time()
    task.pop("last_error", None)
    task.pop("next_retry_at", None)
    task.pop("pending_share_link", None)


async def _pipeline_process_series_task(task: dict, svc) -> dict:
    """Share multiple root-level episodes as one channel card and one link."""
    members = list(task.get("members") or [])
    if len(members) < 2:
        raise ValueError("剧集合并任务至少需要两个文件")
    sample = Path(str(members[0].get("relative_path", ""))).name
    if not task.get("canonical_name"):
        task.update(await _pipeline_metadata(sample))
    task["episode"] = infer_episode_range(
        [str(member.get("relative_path", "")) for member in members]
    ) or task.get("episode", "")
    task["size"] = sum(int(member.get("size", 0) or 0) for member in members)

    file_ids = []
    for member in members:
        relative = str(member.get("relative_path", "")).strip().strip("/")
        file_id = member.get("file_id")
        if not file_id:
            remote_path = f"{PIPELINE_115_ROOT}/{relative}" if PIPELINE_115_ROOT != "/" else f"/{relative}"
            file_id = await svc.fs.get_id(remote_path, refresh=True, async_=True)
            if not file_id:
                raise FileNotFoundError(f"115 挂载剧集文件尚未出现: {remote_path}")
            member["file_id"] = int(file_id)
        file_ids.append(int(file_id))

        if not member.get("renamed_name"):
            filename = Path(relative).name
            parsed = parse_filename(filename)
            episode = extract_episode(filename)
            season = episode[0] if episode else None
            episode_number = episode[1] if episode else None
            renamed_name = build_canonical_name(
                task["display_title"], season, episode_number, None, task.get("year", ""),
                parsed["quality"], parsed["source"], infer_audio_codec([filename]),
                extract_hdr(filename), parsed["encode"], ext=video_ext(filename), suffix="Cxuan",
            )
            result = await svc.client.fs_rename((file_id, renamed_name), async_=True)
            if isinstance(result, dict) and result.get("state") is False:
                raise RuntimeError(result.get("error") or result.get("message") or "115 剧集文件重命名失败")
            member["renamed_name"] = renamed_name

    share_link = task.get("share_link")
    if not share_link:
        share_link = await svc._share_fids_direct(file_ids)
        if not share_link:
            raise RuntimeError("115 剧集合并永久分享链接生成失败")
        task["share_link"] = share_link
        logger.info(f"✅ 闭环剧集合并永久分享已生成: {_safe_log_url(share_link)}")
    await _pipeline_require_share_ready(svc, task, share_link)
    await _pipeline_send_card(task, share_link)
    if not task.get("cleanup_scheduled"):
        for member in members:
            _schedule_cleanup(
                member.get("file_id"), member.get("renamed_name") or task["display_title"],
                share_link, service="pipeline", delay=0,
            )
        task["cleanup_scheduled"] = True
    logger.info(f"✅ 123→115 同批剧集合并闭环完成: {task['display_title']}（{len(file_ids)} 集）")
    return task


async def _pipeline_process_folder_task(task: dict, svc) -> dict:
    """Rename and share one top-level series directory as a single link."""
    relative = str(task.get("relative_path", "")).strip().strip("/")
    remote_path = f"{PIPELINE_115_ROOT}/{relative}" if PIPELINE_115_ROOT != "/" else f"/{relative}"
    folder_id = task.get("folder_id") or await svc.fs.get_id(remote_path, refresh=True, async_=True)
    if not folder_id:
        raise FileNotFoundError(f"115 挂载剧集目录尚未出现: {remote_path}")
    task["folder_id"] = int(folder_id)

    share_link = task.get("share_link")
    if share_link and task.get("canonical_name"):
        await _pipeline_require_share_ready(svc, task, share_link)
        await _pipeline_send_card(task, share_link)
        if not task.get("cleanup_scheduled"):
            _schedule_cleanup(
                folder_id, task.get("renamed_name") or task["display_title"], share_link,
                service="pipeline", delay=0,
            )
            task["cleanup_scheduled"] = True
        logger.info(f"✅ 123→115 已有剧集分享补推完成: {task['display_title']}")
        return task

    videos = await _pipeline_collect_videos(svc, folder_id)
    if not videos:
        raise ValueError("闭环目录中未检测到任何视频")
    sample = videos[0].get("name", "")
    metadata = await _pipeline_metadata(sample) if not task.get("canonical_name") else task
    task.update(metadata)
    names = [video.get("name", "") for video in videos]
    task["episode"] = infer_episode_range(names, relative) or task.get("episode", "")
    task["size"] = sum(int(video.get("size", 0) or 0) for video in videos)

    folder_name = build_folder_name(task["display_title"], task["year"], task.get("tmdb_id"))
    if not task.get("renamed_name"):
        result = await svc.client.fs_rename((folder_id, folder_name), async_=True)
        if isinstance(result, dict) and result.get("state") is False:
            raise RuntimeError(result.get("error") or result.get("message") or "115 剧集目录重命名失败")
        task["renamed_name"] = folder_name
        logger.info(f"✅ 闭环剧集目录重命名完成: {relative} -> {folder_name}")
        await _rename_video_tree(
            svc, folder_id, task["display_title"], task["year"], task.get("tmdb_id"), suffix="Cxuan",
        )

    share_link = task.get("share_link")
    if not share_link:
        share_link = await svc._share_fids_direct([folder_id])
        if not share_link:
            raise RuntimeError("115 剧集目录永久分享链接生成失败")
        task["share_link"] = share_link
        logger.info(f"✅ 闭环剧集永久分享已生成: {_safe_log_url(share_link)}")

    await _pipeline_require_share_ready(svc, task, share_link)
    await _pipeline_send_card(task, share_link)
    if not task.get("cleanup_scheduled"):
        _schedule_cleanup(
            folder_id, task.get("renamed_name") or task["display_title"], share_link,
            service="pipeline", delay=0,
        )
        task["cleanup_scheduled"] = True
    logger.info(f"✅ 123→115 剧集合并闭环完成: {task['display_title']}（{len(videos)} 集）")
    return task


async def _pipeline_process_task(task: dict) -> dict:
    svc = get_pipeline_svc()
    if task.get("kind") == "folder":
        return await _pipeline_process_folder_task(task, svc)
    if task.get("kind") == "series":
        return await _pipeline_process_series_task(task, svc)
    relative = str(task.get("relative_path", "")).strip().strip("/")
    if not relative:
        raise ValueError("闭环任务缺少 relative_path")
    filename = Path(relative).name

    if not task.get("file_id"):
        duplicate = _pipeline_find_completed_duplicate(relative, int(task.get("size", 0) or 0))
        if duplicate:
            task.update(duplicate)
            task["duplicate_of"] = duplicate.get("id")
            return task

    task["display_title"] = task.get("display_title") or filename
    metadata = await _pipeline_metadata(filename) if not task.get("canonical_name") else task
    task.update(metadata)

    file_id = task.get("file_id")
    if not file_id:
        remote_path = f"{PIPELINE_115_ROOT}/{relative}" if PIPELINE_115_ROOT != "/" else f"/{relative}"
        file_id = await svc.fs.get_id(remote_path, refresh=True, async_=True)
        if not file_id:
            raise FileNotFoundError(f"115 挂载文件尚未出现在账号目录: {remote_path}")
        task["file_id"] = int(file_id)

    if not task.get("renamed_name"):
        renamed_name = str(task["canonical_name"])
        result = await svc.client.fs_rename((task["file_id"], renamed_name), async_=True)
        if isinstance(result, dict) and result.get("state") is False:
            raise RuntimeError(result.get("error") or result.get("message") or "115 重命名失败")
        task["renamed_name"] = renamed_name
        logger.info(f"✅ 闭环重命名完成: {filename} -> {renamed_name}")

    share_link = task.get("share_link")
    if not share_link:
        share_link = await svc._share_fids_direct([task["file_id"]])
        if not share_link:
            raise RuntimeError("115 永久分享链接生成失败")
        task["share_link"] = share_link
        logger.info(f"✅ 闭环永久分享已生成: {_safe_log_url(share_link)}")

    await _pipeline_require_share_ready(svc, task, share_link)
    await _pipeline_send_card(task, share_link)
    if not task.get("cleanup_scheduled"):
        _schedule_cleanup(
            task["file_id"], task.get("renamed_name") or task["display_title"],
            share_link, service="pipeline", delay=0,
        )
        task["cleanup_scheduled"] = True
    logger.info(f"✅ 123→115 闭环完成: {task['display_title']}")
    return task


async def _pipeline_worker():
    """Consume files moved from the 123 mount and finish the 115 share flow."""
    await asyncio.sleep(5)
    _pipeline_recover_processing()
    while True:
        try:
            _pipeline_prepare_series_groups()
            candidates = _pipeline_task_paths("pending") + _pipeline_task_paths("failed")
            candidates.sort(key=_pipeline_candidate_priority)
            for path in candidates:
                try:
                    task = _pipeline_read(path)
                    if float(task.get("next_retry_at", 0) or 0) > time.time():
                        continue
                    series_wait_until = _pipeline_series_wait_until(task)
                    if series_wait_until > time.time():
                        task["next_retry_at"] = series_wait_until
                        _pipeline_write(path, task)
                        continue
                    if task.get("kind") != "folder" and _pipeline_has_folder_task(task.get("relative_path", "")):
                        continue
                    if task.get("kind") == "folder":
                        quiet_until = (
                            float(task.get("last_seen_at", task.get("created_at", 0)) or 0)
                            + PIPELINE_GROUP_DELAY
                        )
                        if quiet_until > time.time():
                            task["next_retry_at"] = quiet_until
                            _pipeline_write(path, task)
                            continue
                    processing = path.with_name(
                        path.name.replace(path.suffixes[-2] + path.suffix, ".processing.json")
                    )
                    os.replace(path, processing)
                except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as exc:
                    logger.warning(f"闭环任务领取失败: {path.name} | {exc}")
                    continue
                try:
                    async with _SAVE_LOCK:
                        result = await asyncio.wait_for(
                            _pipeline_process_task(task), timeout=PIPELINE_TASK_TIMEOUT,
                        )
                    done = processing.with_name(processing.name.replace(".processing.json", ".done.json"))
                    _pipeline_write(done, result)
                    processing.unlink(missing_ok=True)
                except _PipelinePending as exc:
                    task["last_error"] = str(exc)
                    task["next_retry_at"] = time.time() + SHARE_AUDIT_POLL_INTERVAL
                    failed = processing.with_name(processing.name.replace(".processing.json", ".failed.json"))
                    _pipeline_write(failed, task)
                    processing.unlink(missing_ok=True)
                    logger.info(f"⏳ 闭环分享审核中，稍后继续: {task.get('relative_path', '')}")
                except _PipelineTerminal as exc:
                    task["last_error"] = str(exc)
                    task["terminal_failed"] = True
                    task["completed_at"] = time.time()
                    task.pop("next_retry_at", None)
                    done = processing.with_name(processing.name.replace(".processing.json", ".done.json"))
                    _pipeline_write(done, task)
                    processing.unlink(missing_ok=True)
                    logger.warning(
                        f"⛔ 闭环分享已进入终态，退出重试队列: "
                        f"{task.get('relative_path', '')} | {exc}"
                    )
                except Exception as exc:
                    task["attempt"] = int(task.get("attempt", 0) or 0) + 1
                    task["last_error"] = str(exc)[:300]
                    task["next_retry_at"] = time.time() + min(300, 30 * task["attempt"])
                    failed = processing.with_name(processing.name.replace(".processing.json", ".failed.json"))
                    _pipeline_write(failed, task)
                    processing.unlink(missing_ok=True)
                    logger.warning(
                        f"⚠️ 闭环任务失败，自动重试: {task.get('relative_path', '')} | {exc}"
                    )
        except Exception as exc:
            logger.warning(f"闭环 worker 异常: {exc}")
        await asyncio.sleep(PIPELINE_INTERVAL)


# ── 分享占位标题检测（HDHive 等转发机器人创建的临时分享）──
# 它们的 share_title 先是占位（"耗时 1.6s 🚗 永V免费解锁：0/0 剩余0"），
# 转存完成后才更新为真实剧名。看到这些关键词就要轮询等，不要用乱码出卡片。
SHARE_PLACEHOLDER_KW = re.compile(r"耗时|解锁|剩余\d|剩余\s*$|免费资源|HDHive 解锁", re.I)


async def process_one(url: str, user_name: str, message: types.Message, status: types.Message,
                     grouped_urls: list[str] = None):
    bot = message.bot
    test_mode = CARD_BOT_TEST_MODE in ("1", "2", "true", "True", "yes")
    is_mode1 = test_mode and CARD_BOT_TEST_MODE in ("1", "true", "True", "yes")

    # ed2k 完整流程：解析文件名 → TMDB → 海报 → 私聊+频道卡片。
    # ed2k 不属于 115 转存体系，严禁进入 save_rename_share；成功发送后直接结束。
    if url.lower().startswith("ed2k://"):
        data = _parse_ed2k(url)
        grouped_urls = grouped_urls or [url]
        parsed = parse_filename(data["name"])
        # 统一识别：LLM 优先，正则 + 别名兜底
        _st_ed, _sy_ed, _ft_ed, _fy_ed, _ep_ed = await _resolve_identity(data["name"], parsed)
        info = await tmdb_search(_st_ed, _sy_ed,
                                 _ep_ed, source_name=data["name"]) if TMDB_API_KEY else None
        if not info and _st_ed != _ft_ed:
            info = await tmdb_search(_ft_ed, _fy_ed,
                                     _ep_ed, source_name=data["name"]) if TMDB_API_KEY else None
        if not info:
            _hid_ed = _title_tmdb_id(_st_ed) or _title_tmdb_id(_ft_ed)
            if _hid_ed:
                logger.info(f"🔗 搜索未命中，使用内置绑定 ID 兜底: {_hid_ed}")
                info = await tmdb_search(_st_ed, _sy_ed,
                                         _ep_ed, tmdb_id=_hid_ed) if TMDB_API_KEY else None
        title = (info or {}).get("title") or parsed["title"]
        year = (info or {}).get("year") or parsed.get("year", "")
        poster = await fetch_poster_bytes((info or {}).get("poster_url", "")) if info else None
        group_data = [_parse_ed2k(u) for u in grouped_urls]
        total_size = sum(x["size"] for x in group_data)
        episodes = sorted(
            int(m.group(1)) for x in group_data
            if (m := re.search(r"[Ss]\d{1,2}[Ee](\d{1,3})", x["name"]))
        )
        episode_text = parsed["episode"]
        if len(episodes) > 1:
            season = re.search(r"[Ss](\d{1,2})[Ee]", data["name"])
            episode_text = f"S{int(season.group(1)):02d}E{episodes[0]:02d}-E{episodes[-1]:02d}" if season else parsed["episode"]
        links_block = "\n".join(
            f"{i}. {html_escape(u, quote=False)}" for i, u in enumerate(grouped_urls, 1)
        )
        ed2k_card = build_card(
            title=title, year=year, genres=(info or {}).get("genres", "暂无"),
            tmdb_rating=(info or {}).get("rating", "暂无评分"), douban_rating="暂无评分",
            quality=parsed["quality"], source=parsed["source"],
            size_text=format_size(total_size), episode=episode_text,
            encode=parsed["encode"], audio="", share_link="", overview=(info or {}).get("overview", ""),
        )
        # Telegram 图片说明最多约1024字节，完整ED2K很长；卡片只放合并摘要，
        # 完整链接列表随后作为同一组的文本消息发送，避免被截断。
        ed2k_card = ed2k_card.replace(
            "🔗 链接：<a href=\"\">115网盘</a>",
            f"🔗 ED2K 链接：已合并 {len(grouped_urls)} 个文件（下方发送完整链接列表）"
        )
        # 单个 ISO 也必须发送完整 ed2k 链接，不能只在卡片里写摘要。
        ed2k_links_message = (
            f"📎 {title}｜{season_label if 'season_label' in locals() else 'ISO'} 完整 ED2K 链接（{len(grouped_urls)}个）：\n"
            f"<pre>{links_block}</pre>"
        )
        season_match = re.search(r"[Ss](\d{1,2})[Ee]", data["name"])
        season_label = f"S{int(season_match.group(1)):02d}" if season_match else "本季"
        ed2k_links_message = (
            f"📎 {title}｜{season_label} 完整 ED2K 链接（{len(grouped_urls)}个）：\n"
            f"<pre>{links_block}</pre>"
        )
        # 直接发送 ed2k 卡片（不复用 _render_and_send_card 的 115 href）。
        # 频道和私聊分别自动重试，避免一端失败导致另一端重复发送。
        target = int(TG_CHANNEL_ID) if str(TG_CHANNEL_ID).lstrip("-").isdigit() else 0
        user_id = message.from_user.id if message.from_user else None

        async def _send_ed2k_to(chat_id):
            last = None
            for attempt, delay in enumerate((0, 3, 8, 15), 1):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    if poster:
                        await bot.send_photo(
                            chat_id, BufferedInputFile(poster, filename="poster.jpg"),
                            caption=ed2k_card, parse_mode="HTML",
                        )
                    else:
                        await bot.send_message(chat_id, ed2k_card, parse_mode="HTML")
                    # 单个/多个 ed2k 都发送完整链接列表，便于复制和下载。
                    await bot.send_message(chat_id, ed2k_links_message, parse_mode="HTML")
                    return True
                except Exception as ex:
                    last = ex
                    logger.warning(f"ed2k 卡片发送失败 chat={chat_id} 第 {attempt}/4 次: {ex}")
            logger.error(f"ed2k 卡片发送最终失败 chat={chat_id}: {last}")
            return False

        try:
            channel_ok = await _send_ed2k_to(target) if target else False
            private_ok = await _send_ed2k_to(user_id) if user_id and user_id != target else channel_ok
            try:
                await status.edit_text(
                    f"{'✅' if channel_ok and private_ok else '⚠️'} ed2k 卡片发送完成（未转存）\n"
                    f"频道：{'成功' if channel_ok else '失败'}｜私聊：{'成功' if private_ok else '失败'}"
                )
            except Exception:
                pass
        finally:
            del poster
            gc.collect()
        return {"status": "success", "dedup": False}

    # 1) 信息收集（mode=1 纯URL测试不碰 115）
    share_link = None
    pending_share_link = None
    pending_cleanup_cid = None
    pending_cleanup_name = None
    top_name, total = None, None
    video_names, root_name = [], ""
    video_scan_status = "error"
    if is_mode1:
        share_link = url
    else:
        # HDHive 等转发机器人创建的临时分享：share_title 先是占位（"耗时 1.6s 🚗 ..."），
        # 转存完成后才更新为真实剧名。轮询等真实标题出现再处理，避免乱码卡片。
        for attempt in range(4):
            try:
                top_name, total = await fetch_share_info(url)
            except Exception as e:
                logger.warning(f"fetch_share_info 异常: {e}")
            if top_name and not SHARE_PLACEHOLDER_KW.search(top_name):
                break
            if attempt < 3:
                await asyncio.sleep(8)
        try:
            video_names, root_name, video_scan_status = await fetch_share_video_files(
                url, include_scan_status=True,
            )
        except Exception as e:
            logger.warning(f"fetch_share_video_files 异常: {e}")
        if _has_no_usable_video(video_names, video_scan_status):
            fail_reason = "原分享中没有可用视频文件，可能资源已违规或被删除"
            logger.warning(f"🚫 {fail_reason}，停止处理: {_safe_log_url(url)}")
            try:
                await status.edit_text(f"❌ {fail_reason}，已停止处理。")
            except Exception:
                await message.reply(f"❌ {fail_reason}，已停止处理。")
            return {"status": "failed", "error_type": "no_video_files", "dedup": False}
        if not top_name or SHARE_PLACEHOLDER_KW.search(top_name):
            await message.reply(
                f"⏳ 这个分享还在 HDHive 处理中（标题仍是占位「{top_name or ''}」），"
                f"请 1-2 分钟后再发链接。"
            )
            return {"status": "deferred", "dedup": False}

    # 标题/元数据解析优先级：用户自定名 > 真实视频文件名（兜底乱码 share_title） > 分享根名 > URL
    # 真实视频文件名包含完整剧名/画质/源/编码，是最可靠的元数据源。
    # 默认以真实视频文件名为主；只有自定义名与真实文件名不冲突时才采用。
    base_name = _select_source_name(video_names, root_name, user_name) or top_name or url
    if base_name.lower().endswith(IMAGE_EXTS):
        logger.info(f"💿 识别到光盘镜像资源: {base_name}")
    parsed = parse_filename(base_name)
    size_text = format_size(total) if total else ""

    # 混合分享包（如 遮天 169 视频 = 167 集主体 + 2 个剧场版散件）：文件名结构众数
    # 才是主体（多含 SxxEyy/完整元数据），孤立散件（如 "遮天 背棺战王腾.2026.mkv"）
    # 不应作为标题/元数据来源。取出现次数最多的"结构指纹"里第一个文件。
    if video_names and len(video_names) > 3:
        import collections
        struct_cnt = collections.Counter()
        for n in video_names:
            m = re.search(r"(?:[Ss]\d{1,2}[Ee]\d{1,3}|第\d+集|\d{4})", n)
            struct_cnt[m.group(0) if m else "other"] += 1
        if struct_cnt:
            main_mark, _ = struct_cnt.most_common(1)[0]
            if main_mark != "other":
                main_vids = [n for n in video_names if main_mark in n]
                if main_vids:
                    # 主体视频确定后再次校验自定义标题，避免旧标题污染混合包。
                    base_name = main_vids[0]
                    parsed = parse_filename(base_name)

    # 真实文件覆盖集数显示（S01E01-E28），并提取真实音频编码
    real_ep = infer_episode_range(video_names, root_name) if video_names else ""
    audio = infer_audio_codec(video_names) if video_names else ""
    if real_ep:
        parsed["episode"] = real_ep
    # 2) 统一识别：LLM（OpenAI 兼容）优先，正则 + 别名仅作兜底
    _search_title, _search_year, _fallback_title, _fallback_year, _season = await _resolve_identity(
        base_name, parsed
    )

    # 3) TMDB / 豆瓣 / 海报（提前查：转存重命名需要中文标题与年份）
    # 优先用分享根名里的 {tmdbid-XXXX} 精确标记（比搜索可靠得多），
    # 其次按文件名年份+季数做多版本消歧（如 Heart Signal 韩版 vs 日版 vs 中国版）
    _marker_id = parse_tmdbid_marker(top_name or root_name or "")
    # 第 1 优先：分享名里的 {tmdbid-XXXX} 人工标记（用户自己指定的最可信）
    info = await tmdb_search(_search_title, _search_year, _season,
                             tmdb_id=_marker_id, source_name=base_name) if TMDB_API_KEY else None
    # 第 2 优先：主标题在 TMDB 搜不到，回退到备用标题（正则/别名结果）再搜一次
    if not info and _search_title != _fallback_title:
        logger.info(f"↩️ 主标题 TMDB 未命中，回退备用标题: {_fallback_title!r}")
        info = await tmdb_search(_fallback_title, _fallback_year, _season,
                                 tmdb_id=_marker_id, source_name=base_name) if TMDB_API_KEY else None
    # 最后兜底：内置绑定表（仅在搜索与标记都失败时才用，不覆盖 LLM 的判定）
    if not info:
        _hid = _title_tmdb_id(_search_title) or _title_tmdb_id(_fallback_title)
        if _hid:
            logger.info(f"🔗 搜索未命中，使用内置绑定 ID 兜底: {_hid}")
            info = await tmdb_search(_search_title, _search_year, _season,
                                     tmdb_id=_hid) if TMDB_API_KEY else None
    # 豆瓣用 TMDB 命中的权威中文名查询（比正则/LLM 猜测名更准）
    _douban_title = (info or {}).get("title") or _search_title or parsed.get("title", "")
    douban = await douban_rating(_douban_title) if _douban_title else ""
    poster = await fetch_poster_bytes((info or {}).get("poster_url", "")) if info and info.get("poster_url") else None
    year = (info or {}).get("year", "")
    genres = (info or {}).get("genres", "暂无")
    tmdb_rating = (info or {}).get("rating", "暂无评分")
    overview = (info or {}).get("overview", "")
    # 标题用 TMDB 中文名（九门），查不到再用真实视频文件名原名（Mystic Nine）兜底
    # 画质/源/编码/集数仍从真实视频文件名 parse（base_name 已优先用 video_names[0]）
    display_title = (info or {}).get("title") or parsed["title"]
    tmdb_id = (info or {}).get("tmdb_id")
    user_id = message.from_user.id if message.from_user else None
    cleanup_cid = None
    cleanup_name = display_title

    # 3) 转存 + 重命名 + 分享（仅正常模式）
    if not is_mode1:
        if test_mode:  # mode=2
            share_link = url
            logger.info("[测试模式=2] 跳过转存，仅取信息")
        else:
            # ⚠️ 全局串行锁：批量链接排队逐个转存，避免并发打 115 触发"操作太频繁"风控
            fail_reason = ""
            async with _SAVE_LOCK:
                try:
                    res = await save_rename_share(
                        url,
                        {"description": user_name or url, "full_text": user_name or url, "command_mode": "share"},
                        display_title, year, tmdb_id,
                        expected_video_count=len(video_names),
                    )
                    if res and res.get("status") == "success" and res.get("share_link"):
                        share_link = res.get("share_link")
                        cleanup_cid = res.get("cleanup_cid")
                        cleanup_name = res.get("cleanup_name") or display_title
                    else:
                        fail_reason = (res or {}).get("message") or (res or {}).get("error_type") or "未知错误"
                        pending_share_link = (
                            (res or {}).get("share_link") or ""
                            if (res or {}).get("error_type") == "share_audit_pending" else ""
                        )
                        pending_cleanup_cid = (res or {}).get("cleanup_cid")
                        pending_cleanup_name = (res or {}).get("cleanup_name")
                        logger.error(f"save_rename_share 失败: {fail_reason}")
                except Exception as e:
                    fail_reason = str(e)[:150]
                    logger.error(f"save_rename_share 异常: {fail_reason}")

    if not share_link:
        if _retry_queue_has_url(url):
            logger.info(f"🔁 原始链接已在自动重试队列，跳过重复入队: {_safe_log_url(url)}")
            try:
                await status.edit_text("⏳ 该链接已在自动轮询队列中，继续等待115处理完成。")
            except Exception:
                pass
            del poster
            return {"status": "queued", "dedup": False}
        # ⛔ 严禁：转存+重命名+分享失败时绝不用原始链接发卡片。
        # 如果分享已创建但仍在115系统审核，保存 pending_share_link，后台只轮询状态，
        # 绝不重复转存/重复创建分享；审核完成后才发频道。
        _RETRY_QUEUE.append({
            "url": url, "user_name": user_name,
            "display_title": display_title, "year": year, "tmdb_id": tmdb_id,
            "poster_url": (info or {}).get("poster_url", ""),
            "genres": genres, "tmdb_rating": tmdb_rating, "douban": douban,
            "overview": overview, "quality": parsed["quality"], "source": parsed["source"],
            "episode": parsed["episode"], "encode": parsed["encode"], "audio": audio,
            "size_text": size_text, "user_id": user_id, "retry_count": 0,
            "expected_video_count": len(video_names),
            "pending_share_link": pending_share_link,
            "pending_cleanup_cid": pending_cleanup_cid,
            "pending_cleanup_name": pending_cleanup_name,
            "fail_reason": fail_reason or "未知错误",
        })
        _save_retry_queue()
        logger.warning(
            f"🔁 转存失败已加入自动重试队列（当前 {len(_RETRY_QUEUE)} 项待重试）: "
            f"{_safe_log_url(url)} | {fail_reason}"
        )
        try:
            if pending_share_link:
                status_text = (
                    f"⏳ 115 分享已创建，但页面仍显示系统处理中：{fail_reason or '审核中'}\n"
                    f"机器人会每 60 秒检查一次，审核完成后自动推送频道，不会重复转存。"
                )
            else:
                status_text = (
                    f"⏳ 转存+重命名+分享暂时失败：{fail_reason or '未知错误'}\n"
                    f"已自动加入轮询队列（每 60 秒检查一次），有结果后自动推送卡片，无需重发。"
                )
            await status.edit_text(status_text)
        except Exception:
            pass
        del poster
        return {"status": "queued", "dedup": False}

    # 4) 发送卡片
    if test_mode:
        sent = await _render_and_send_card(
            bot, 0,
            title=display_title, year=year, genres=genres, tmdb_rating=tmdb_rating,
            douban_rating=douban, quality=parsed["quality"], source=parsed["source"],
            size_text=size_text, episode=parsed["episode"], encode=parsed["encode"],
            audio=audio, share_link=share_link, overview=overview, poster=poster,
            private_to_user_id=user_id,
        )
        del poster  # 发送完成，立即释放海报内存，绝不落盘/留缓存
        try:
            await status.edit_text(
                f"{'✅ 已私聊出卡片（测试模式）' if sent else '⚠️ 卡片发送失败'}\n"
                f"分享链接：{share_link}\n"
                f"标题：{parsed['title']}\n"
                f"真实文件数：{len(video_names)} 个视频\n"
                f"解析：{parsed['quality'] or '-'} / {parsed['source'] or '-'} / {parsed['episode'] or '-'} / {parsed['encode'] or '-'} / {audio or '-'}"
            )
        except Exception:
            pass
        return {"status": "success", "dedup": False}

    # 正常模式：私聊 + 频道
    sent = await _render_and_send_card(
        bot, int(TG_CHANNEL_ID) if TG_CHANNEL_ID.lstrip("-").isdigit() else 0,
        title=display_title, year=year, genres=genres, tmdb_rating=tmdb_rating,
        douban_rating=douban, quality=parsed["quality"], source=parsed["source"],
        size_text=size_text, episode=parsed["episode"], encode=parsed["encode"],
        audio=audio, share_link=share_link, overview=overview, poster=poster,
    )
    del poster  # 发送完成，立即释放海报内存，绝不落盘/留缓存
    try:
        await status.edit_text(
            f"{'✅ 已发布到频道' if sent else '⚠️ 频道发布失败'}\n"
            f"分享链接：{share_link}"
        )
    except Exception:
        pass
    if sent and cleanup_cid:
        _schedule_cleanup(cleanup_cid, cleanup_name, share_link)
    await message.reply(f"✅ 已生成卡片。\n分享链接：{share_link}")
    return {"status": "success", "dedup": True, "dedup_links": [share_link]}


async def handle_card_text(message: types.Message):
    """/card <文件名>：纯文本测试模板，不碰 115。"""
    if not await _require_submit_permission(message):
        return
    text = (message.text or "").split(maxsplit=1)
    if len(text) < 2 or not text[1].strip():
        await message.answer("用法：/card 九门.S01E01-E28.2160p.WEB-DL.DDP5.1")
        return
    fake_name = text[1].strip()
    parsed = parse_filename(fake_name)
    # 统一识别：LLM 优先，正则 + 别名兜底
    _st_card, _sy_card, _ft_card, _fy_card, _season_card = await _resolve_identity(fake_name, parsed)
    user_id = message.from_user.id if message.from_user else message.chat.id
    info = await tmdb_search(_st_card, _sy_card,
                             _season_card, source_name=fake_name) if TMDB_API_KEY else None
    if not info and _st_card != _ft_card:
        info = await tmdb_search(_ft_card, _fy_card,
                                 _season_card, source_name=fake_name) if TMDB_API_KEY else None
    if not info:
        _hid_card = _title_tmdb_id(_st_card) or _title_tmdb_id(_ft_card)
        if _hid_card:
            logger.info(f"🔗 搜索未命中，使用内置绑定 ID 兜底: {_hid_card}")
            info = await tmdb_search(_st_card, _sy_card,
                                     _season_card, tmdb_id=_hid_card) if TMDB_API_KEY else None
    _douban_title_card = (info or {}).get("title") or _st_card or parsed.get("title", "")
    douban = await douban_rating(_douban_title_card) if _douban_title_card else ""
    poster = await fetch_poster_bytes((info or {}).get("poster_url", "")) if info and info.get("poster_url") else None
    year = (info or {}).get("year", "")
    genres = (info or {}).get("genres", "暂无")
    tmdb_rating = (info or {}).get("rating", "暂无评分")
    overview = (info or {}).get("overview", "")
    display_title = (info or {}).get("title") or parsed["title"]
    await _render_and_send_card(
        message.bot, 0,
        title=display_title, year=year, genres=genres, tmdb_rating=tmdb_rating,
        douban_rating=douban, quality=parsed["quality"], source=parsed["source"],
        size_text="", episode=parsed["episode"], encode=parsed["encode"],
        audio="", share_link="(测试卡·无 115 链接)", overview=overview, poster=poster,
        private_to_user_id=user_id,
    )
    del poster  # 发送完成，立即释放海报内存，绝不落盘/留缓存


async def main():
    if not P115_COOKIE:
        logger.error("缺少 P115_COOKIE，无法驱动 115 转存。请在环境变量/.env 中配置。")
        return
    if not TG_BOT_TOKEN:
        logger.error("缺少 TG_BOT_TOKEN，无法启动 Telegram 机器人。")
        return

    # 先补齐 P115-Share ORM 表（cardbot 直接启动时不会经过原 Web 入口）
    try:
        await _ensure_app_database_schema()
    except Exception:
        return

    # 预热 115 客户端
    try:
        get_svc()
        logger.info("✅ 115 客户端初始化完成")
    except Exception as e:
        logger.error(f"115 客户端初始化失败: {e}")

    bot = make_bot()
    global _bot
    _bot = bot  # 供后台自动重试 worker 使用
    dp = Dispatcher()
    dp.message(Command("start"))(lambda m: m.answer(
        "👋 115 资源卡片机器人\n"
        "• 发 115 分享链接 → 转存 + 生成卡片发频道\n"
        "• /card <文件名> → 纯文本试模板（不碰 115）\n"
        f"• 测试模式: CARD_BOT_TEST_MODE={CARD_BOT_TEST_MODE}\n"
        "可选：链接 | 自定义名称"))
    dp.message(Command("help"))(lambda m: m.answer(
        "📖 用法\n"
        "1) 发 115 链接 → 自动转存并出卡片\n"
        "2) /card 九门.S01E01-E28.2160p.WEB-DL.DDP5.1 → 纯文本试模板\n"
        "3) 链接 | 自定义名（自定义标题可选）\n"
        "支持域名: 115.com / 115cdn.com / anxia.com / ed2k://"))
    dp.message(Command("id"))(lambda m: m.answer(
        f"用户 ID: `{_message_sender_id(m) or '无法识别'}`\n聊天 ID: `{m.chat.id}`",
        parse_mode="Markdown",
    ))
    dp.message(Command("card"))(handle_card_text)
    dp.message()(handle_message)

    _load_cleanup_queue()
    _load_processed_links()
    _load_retry_queue()
    logger.info(f"🧹 自动清理队列已加载：{len(_cleanup_queue)} 项（仅新成功发送任务，历史目录不在队列）")
    logger.info(f"🔁 自动重试队列已加载：{len(_RETRY_QUEUE)} 项")
    logger.info("🚀 卡片机器人启动中…")
    # 启动后台 worker：失败重试 + 123→115 挂载闭环 + 发送成功后清理
    if AUTO_PROCESS_115_LINKS:
        asyncio.create_task(_retry_worker())
    else:
        logger.info("⏸️ 115 链接自动识别与自动重试已关闭")
    asyncio.create_task(_pipeline_worker())
    asyncio.create_task(_cleanup_worker())
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
