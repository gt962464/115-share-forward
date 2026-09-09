"""
OpenAI 辅助识别 + TMDB 搜索校验（移植自 card-bot 的 LLM 流程）。

方案 A+C：英文原名优先搜 TMDB → LLM 辅助翻译兜底 → OpenAI 二次校验闸防误识别。

配置全部动态读环境变量（/set 或 Bot「🤖 OpenAI」按钮改了立即生效，无需重启）：
  LLM_API_BASE / LLM_API_KEY / LLM_MODEL / LLM_PROMPT
  TMDB_API_KEY / TMDB_LANG / APP_HTTP_PROXY
"""
import os
import re
import json
import asyncio
import logging

import aiohttp

logger = logging.getLogger("identifier")


# ── 动态配置 ──

def _llm_base() -> str:
    return (os.getenv("LLM_API_BASE") or "https://apihub.agnes-ai.com/v1").strip().rstrip("/")


def _llm_key() -> str:
    return os.getenv("LLM_API_KEY", "").strip()


def _llm_model() -> str:
    return (os.getenv("LLM_MODEL") or "agnes-2.5-flash").strip()


def _llm_prompt() -> str:
    return os.getenv("LLM_PROMPT", "").strip() or _DEFAULT_PARSE_PROMPT


def _tmdb_key() -> str:
    return os.getenv("TMDB_API_KEY", "").strip()


def _tmdb_lang() -> str:
    return (os.getenv("TMDB_LANG") or "zh-CN").strip()


def _proxy() -> str | None:
    return os.getenv("APP_HTTP_PROXY", "").strip() or None


# ── 内置默认提示词（用户可通过 LLM_PROMPT 覆盖）──

_DEFAULT_PARSE_PROMPT = """你是一个专业的媒体文件名解析器。

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

【重要区分】你必须区分电影和电视剧：
- 电视剧：文件名通常包含 S01/S02/E01/E02 等季集标记
- 电影：文件名通常不包含季集标记，可能包含 IMAX/DV/HDR 等标记
- 当同名作品有电影和电视剧版本时，根据文件名中的季集标记选择正确的版本

【输出格式】只返回这一个 JSON 对象，例如：
{"name":"脱口秀和Ta的朋友们","year":"2024","season":2,"episode":null,"resolution":"2160p"}

如果某个字段无法确定，返回 null。

【速度要求】请尽快给出结论，不要展开长篇推理，直接输出最终的 JSON 对象。"""

# LLM 判定为"无有效片名"时的占位值，命中即视为未识别
_LLM_INVALID_NAMES = {
    "", "null", "none", "nil", "n/a", "na", "unknown", "未知", "无",
}

# ── LLM 调用基础设施：缓存 + 全局限流 + 重试退避 ──
# 后端常为推理模型，单次响应 30s+；网关限流 30 RPM，必须节流 + 429 退避。
_llm_cache: dict = {}
_LLM_CACHE_MAX = 200
_LLM_MAX_ATTEMPTS = 3
_LLM_TIMEOUTS = (45, 60, 60)      # 逐次放宽
_LLM_MIN_INTERVAL = 2.5           # 全局最小请求间隔（秒）
_LLM_BACKOFF = {"429": 12.0, "5xx": 5.0, "other": 4.0}
_llm_last_call_ts = 0.0
_llm_rate_lock: asyncio.Lock | None = None
_session_obj: aiohttp.ClientSession | None = None


def _session() -> aiohttp.ClientSession:
    global _session_obj
    if _session_obj is None or _session_obj.closed:
        _session_obj = aiohttp.ClientSession()
    return _session_obj


def _llm_rate_lock_obj() -> asyncio.Lock:
    """延迟创建锁：避免在事件循环外实例化 asyncio.Lock。"""
    global _llm_rate_lock
    if _llm_rate_lock is None:
        _llm_rate_lock = asyncio.Lock()
    return _llm_rate_lock


async def _llm_throttle():
    """全局节流：保证 LLM 请求速率不超过限流档位。"""
    global _llm_last_call_ts
    async with _llm_rate_lock_obj():
        now = asyncio.get_event_loop().time()
        wait = _LLM_MIN_INTERVAL - (now - _llm_last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _llm_last_call_ts = asyncio.get_event_loop().time()


def _llm_cache_key(filename: str) -> str:
    import hashlib
    return hashlib.md5(filename[:128].encode("utf-8")).hexdigest()


def _cache_put(key: str, value):
    if len(_llm_cache) >= _LLM_CACHE_MAX:
        keys = list(_llm_cache.keys())
        for k in keys[: _LLM_CACHE_MAX // 2]:
            _llm_cache.pop(k, None)
    _llm_cache[key] = value


def _extract_json_object(content: str, required_key: str) -> dict | None:
    """从模型输出中提取 JSON 对象（容忍 markdown 包裹 / 混入解释文字）。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        content = content.strip()
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*"' + re.escape(required_key) + r'"\s*:[^{}]*\}', content, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return None


# ── LLM 文件名解析 ──

async def llm_parse_filename(raw_filename: str) -> dict | None:
    """调用 LLM 解析媒体文件名，返回 {name, year, season, episode, resolution} 或 None。"""
    if not _llm_key() or not raw_filename:
        return None

    cache_key = _llm_cache_key(raw_filename)
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    payload = json.dumps({
        "model": _llm_model(),
        "messages": [
            {"role": "system", "content": _llm_prompt()},
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
    headers = {"Authorization": f"Bearer {_llm_key()}", "Content-Type": "application/json"}
    url = f"{_llm_base()}/chat/completions"

    _fatal = False  # True=请求本身有问题（鉴权/参数），重试无意义
    for _attempt in range(_LLM_MAX_ATTEMPTS):
        _timeout = _LLM_TIMEOUTS[min(_attempt, len(_LLM_TIMEOUTS) - 1)]
        _tag = f"第 {_attempt + 1}/{_LLM_MAX_ATTEMPTS} 次"
        await _llm_throttle()
        try:
            async with _session().post(
                url, data=payload.encode("utf-8"), headers=headers,
                proxy=_proxy(), timeout=aiohttp.ClientTimeout(total=_timeout),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"LLM 识别 HTTP {resp.status}（{_tag}）: {raw_filename[:50]}")
                    if resp.status == 429:
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
                    _fatal = True
                    break
                body = await resp.json()
                msg = body.get("choices", [{}])[0].get("message", {})
                content = msg.get("content") or ""
                # 某些推理模型返回 reasoning_content 而非 content
                if not content.strip():
                    content = msg.get("reasoning_content") or ""
        except asyncio.TimeoutError:
            logger.warning(f"LLM 识别超时（{_timeout}s，{_tag}）: {raw_filename[:50]}")
            continue
        except Exception as e:
            logger.warning(f"LLM 识别异常（{_tag}）: {e}")
            await asyncio.sleep(_LLM_BACKOFF["other"])
            continue

        result = _extract_json_object(content, "name")
        if result is None:
            logger.warning(f"LLM 识别无法提取 JSON（{_tag}）: {content[:120]}")
            await asyncio.sleep(_LLM_BACKOFF["other"])
            continue

        normalized = {
            "name": result.get("name") if isinstance(result.get("name"), str) else None,
            "year": result.get("year") if isinstance(result.get("year"), str) else None,
            "season": result.get("season"),
            "episode": result.get("episode") if isinstance(result.get("episode"), str) else None,
            "resolution": result.get("resolution") if isinstance(result.get("resolution"), str) else None,
        }
        _cache_put(cache_key, normalized)
        return normalized

    # 只有"请求本身有问题"才缓存失败；超时/429/5xx 属瞬时故障，不缓存 None，
    # 否则该文件名将永久回退正则路径。
    if _fatal:
        logger.warning(f"LLM 识别请求被拒（不可重试），回退正则解析: {raw_filename[:50]}")
        _cache_put(cache_key, None)
    else:
        logger.warning(f"LLM 识别暂时不可用（稍后自动重试），本次回退正则解析: {raw_filename[:50]}")
    return None


# ── OpenAI 二次校验闸 ──

async def llm_verify_match(source_name: str, info: dict) -> bool | None:
    """OpenAI 二次校验：TMDB 搜到的条目是否真的对应源文件名。

    返回 True=确认匹配 / False=确认不匹配（调用方必须放弃该候选）/
    None=LLM 不可用或无法判断（不阻塞，保留候选）。
    """
    if not _llm_key() or not source_name or not info:
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
        "\n"
        "2. 只有以下情况才返回 false：\n"
        "   - 年份差异超过 5 年（如 2026 年的资源匹配到 2016 年的作品）\n"
        "   - 类型完全不同（如源文件是剧集 S01E01，但 TMDB 返回的是电影）\n"
        "   - 题材/内容完全无关\n"
        "   - 标题完全不同且无任何关联\n"
        "   - 是衍生剧/外传/前传/续集，但不是同一部作品\n"
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
        "model": _llm_model(),
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
    headers = {"Authorization": f"Bearer {_llm_key()}", "Content-Type": "application/json"}

    content = ""
    # 校验要"决定性"：429 限流时按 Retry-After 退避重试一次，不轻易放行
    for _attempt in range(2):
        try:
            await _llm_throttle()
            async with _session().post(
                f"{_llm_base()}/chat/completions",
                data=payload.encode("utf-8"), headers=headers,
                proxy=_proxy(), timeout=aiohttp.ClientTimeout(total=_LLM_TIMEOUTS[0]),
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

    # 推理模型会把思考过程（含复述提示词示例 JSON）吐进 content：
    # 从后往前找第一个"紧致型"的 {"match": bool, "reason": ...}，排除示例 JSON
    def _is_valid_match_json(obj: dict) -> bool:
        if not isinstance(obj.get("match"), bool):
            return False
        return set(obj.keys()) <= {"match", "reason"}

    result = _extract_json_object(content, "match")
    if result is not None and not _is_valid_match_json(result):
        result = None
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
        return None
    _cache_put(cache_key, {"match": verdict})
    reason = str(result.get("reason") or "")[:80]
    if verdict:
        logger.info(f"✅ OpenAI 校验通过: {source_name[:40]} ≈ {info.get('title')!r}（{reason}）")
    else:
        logger.warning(f"🚫 OpenAI 校验否决: {source_name[:40]} ≠ {info.get('title')!r}（{reason}）")
    return verdict


# ── TMDB 搜索（含 OpenAI 校验闸）──

async def _aio_get(url: str, params: dict = None, headers: dict = None) -> tuple[int, str]:
    try:
        async with _session().get(
            url, params=params, headers=headers, proxy=_proxy(),
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            return resp.status, await resp.text()
    except Exception as e:
        logger.warning(f"HTTP GET 失败 {url}: {e}")
        return 0, ""


async def _tmdb_detail(media_type: str, item_id, item: dict = None) -> dict | None:
    """按 media_type/id 拉 TMDB 详情并组装元数据。失败返回 None。"""
    st, dt = await _aio_get(
        f"https://api.themoviedb.org/3/{media_type}/{item_id}",
        params={"api_key": _tmdb_key(), "language": _tmdb_lang()},
    )
    if st != 200:
        return None
    det = json.loads(dt)
    item = item or {}
    orig_lang = det.get("original_language", "")
    if media_type == "tv":
        name_local = det.get("name", "") or item.get("name", "")
        name_orig = det.get("original_name", "") or item.get("original_name", "")
    else:
        name_local = det.get("title", "") or item.get("title", "")
        name_orig = det.get("original_title", "") or item.get("original_title", "")
    display_name = name_orig if (orig_lang == "zh" and name_orig) else (name_local or name_orig)
    year_full = (det.get("release_date") or det.get("first_air_date")
                 or item.get("release_date") or item.get("first_air_date") or "")
    genres = [g.get("name", "") for g in (det.get("genres") or [])]
    poster_path = det.get("poster_path") or item.get("poster_path")
    return {
        "title": display_name,
        "original_name": name_orig,
        "tmdb_id": item_id,
        "year": year_full[:4],
        "genres": "、".join([g for g in genres if g]),
        "rating": det.get("vote_average") or item.get("vote_average") or 0,
        "overview": det.get("overview") or item.get("overview") or "",
        "poster_url": f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else "",
    }


async def _pick_tmdb_item(results: list, year: str, season, query_title: str = "") -> dict:
    """从搜索结果里选最可能的条目：年份容差 ±2，>5 年拒绝；带季数优先 TV。"""
    cands = results[:10]
    if len(cands) <= 1:
        return cands[0] if cands else {}

    if season is not None and season >= 1:
        tv = [it for it in cands if it.get("media_type") == "tv"]
        if tv:
            cands = tv

    file_year = int(year) if year and str(year).isdigit() else None
    if file_year:
        tolerant = []
        for it in cands:
            ys = (it.get("first_air_date") or it.get("release_date") or "")[:4]
            if ys.isdigit() and abs(file_year - int(ys)) <= 2:
                tolerant.append(it)
        if tolerant:
            return tolerant[0]
        years = [
            int((it.get("first_air_date") or it.get("release_date") or "")[:4])
            for it in cands
            if (it.get("first_air_date") or it.get("release_date") or "")[:4].isdigit()
        ]
        if years:
            closest = min(years, key=lambda x: abs(x - file_year))
            if abs(file_year - closest) > 5:
                logger.warning(
                    f"⚠️ TMDB 年份差异过大（文件:{file_year} vs TMDB最近:{closest}），拒绝误匹配: {query_title!r}"
                )
                return {}
    return cands[0]


_TMDB_STRIP_PREFIXES = re.compile(r"^(?:央视|CCTV|中国|大陆|内地|国产|正版|官方|高清|蓝光|4K|8K)\s*")


async def tmdb_search(title: str, year: str = "", season: int = None,
                      source_name: str = "") -> dict | None:
    """TMDB 搜索 + 详情 + OpenAI 校验闸。命中返回详情 dict，否则 None。

    source_name（原始文件名）：传入后搜索命中的候选先交 OpenAI 校验，
    明确否决（False）→ 返回 None 走回退；不可用/不确定（None）→ 放行。
    """
    if not _tmdb_key() or not title:
        return None

    async def _do_search(q: str, media_type: str = None):
        endpoint = (f"https://api.themoviedb.org/3/search/{media_type}" if media_type
                    else "https://api.themoviedb.org/3/search/multi")
        st, text = await _aio_get(
            endpoint,
            params={"api_key": _tmdb_key(), "query": q, "language": _tmdb_lang(), "page": 1},
        )
        if st != 200:
            return [], None
        results = json.loads(text).get("results") or []
        if not results:
            return [], None
        return results, await _pick_tmdb_item(results, year, season, query_title=q)

    try:
        use_tv = season is not None and season >= 1
        results, item = await _do_search(title, media_type="tv" if use_tv else None)
        logger.info(f"🔍 TMDB 搜索: {title!r} (type={'tv' if use_tv else 'multi'})")

        if not results and title:
            stripped = _TMDB_STRIP_PREFIXES.sub("", title).strip()
            if stripped and stripped != title:
                logger.info(f"🔍 TMDB 前缀剥离重试: {title!r} -> {stripped!r}")
                results, item = await _do_search(stripped)

        if not results or not item:
            return None

        media_type = item.get("media_type") or ("tv" if use_tv else "movie")
        item_id = item.get("id")
        if not item_id:
            return None
        det = await _tmdb_detail(media_type, item_id, item)
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


# ── 统一识别入口（方案 A+C）──

def _year_ok(file_year: str, tmdb_year: str) -> bool:
    """年份差异 <= 5 年视为合理。"""
    if not file_year or not tmdb_year:
        return True
    try:
        return abs(int(file_year) - int(tmdb_year)) <= 5
    except (ValueError, TypeError):
        return True


async def resolve_title(raw_name: str, regex_title: str, regex_year: str = "",
                        season: int = None, tmdb_id: int = None) -> dict:
    """统一识别入口。返回 {"title", "year", "tmdb_id", "source", "det"}。

    source: tmdb_id（文件名含 {tmbid-xxx} 直接按 ID 拉详情）/
            tmdb_eng（英文名直搜命中）/ tmdb_llm（LLM 译名搜索命中）/
            llm（LLM 片名，未搜 TMDB）/ eng_fallback（英文原名兜底）/ regex（正则兜底）

    流程：
    1. 文件名含 {tmbid-xxx} → 直接按 ID 拉 TMDB 详情（最快最准）
    2. 提取文件名英文原名，直接搜 TMDB（命中率最高，不依赖 LLM 翻译）
    3. 英文名搜不到 → LLM 解析（翻译中文名），再搜 TMDB
    4. 都失败 → 正则结果兜底
    """
    regex_title = (regex_title or "").strip()
    regex_year = str(regex_year or "").strip()

    # 优先：文件名含 {tmbid-xxx}，直接用 ID 拉详情（无需搜索）
    if tmdb_id and _tmdb_key():
        for media_type in ("tv", "movie"):
            det = await _tmdb_detail(media_type, tmdb_id)
            if det:
                logger.info(f"✅ TMDB ID 直接命中: {tmdb_id} → {det['title']!r}")
                return {
                    "title": det["title"],
                    "year": det.get("year") or regex_year,
                    "tmdb_id": tmdb_id,
                    "source": "tmdb_id",
                    "det": det,
                }

    # 提取英文原名：Serenade.of.Peaceful.Joy.2020.S01 → "Serenade of Peaceful Joy"
    _eng_match = re.match(r"^([A-Z][a-zA-Z0-9]+(?:\.[A-Z][a-zA-Z0-9]+)+)", regex_title)
    eng_title = _eng_match.group(1).replace(".", " ") if _eng_match else ""

    if eng_title and _tmdb_key():
        det = await tmdb_search(eng_title, regex_year, season, source_name=raw_name)
        if det:
            if _year_ok(regex_year, det.get("year", "")):
                logger.info(f"✅ 英文名 TMDB 命中: {eng_title!r} → {det['title']!r} ({det.get('year')})")
                return {
                    "title": det["title"],
                    "year": det.get("year") or regex_year,
                    "tmdb_id": det.get("tmdb_id"),
                    "source": "tmdb_eng",
                    "det": det,
                }
            logger.warning(f"⚠️ 年份差异过大，回退英文原名: {eng_title!r}")
            return {"title": eng_title, "year": regex_year, "tmdb_id": None, "source": "eng_fallback"}

    # ── 中文名直接搜 TMDB（跳过 LLM 翻译，避免误译） ──
    _has_chinese = re.search(r"[一-鿿]", regex_title)
    if _has_chinese and _tmdb_key():
        det = await tmdb_search(regex_title, regex_year, season, source_name=raw_name)
        if det:
            if _year_ok(regex_year, det.get("year", "")):
                logger.info(f"✅ 中文名 TMDB 直搜命中: {regex_title!r} → {det['title']!r} ({det.get('year')})")
                return {
                    "title": det["title"],
                    "year": det.get("year") or regex_year,
                    "tmdb_id": det.get("tmdb_id"),
                    "source": "tmdb_cn",
                    "det": det,
                }
            logger.warning(f"⚠️ 中文名直搜年份差异过大: {regex_title!r} vs {det.get('year')}")

    llm_info = await llm_parse_filename(raw_name) if _llm_key() else None
    llm_name = ((llm_info or {}).get("name") or "").strip()
    llm_year = str((llm_info or {}).get("year") or "").strip()
    if llm_name and llm_name.lower() not in _LLM_INVALID_NAMES:
        if _tmdb_key():
            det = await tmdb_search(llm_name, llm_year or regex_year, season, source_name=raw_name)
            if det:
                logger.info(f"✅ LLM 译名 TMDB 命中: {llm_name!r} → {det['title']!r}")
                return {
                    "title": det["title"],
                    "year": det.get("year") or llm_year or regex_year,
                    "tmdb_id": det.get("tmdb_id"),
                    "source": "tmdb_llm",
                    "det": det,
                }
        logger.info(f"🤖 LLM 识别: {regex_title!r} -> {llm_name!r}")
        return {"title": llm_name, "year": llm_year or regex_year, "tmdb_id": None, "source": "llm"}

    logger.info(f"⚠️ LLM/TMDB 未命中，使用正则标题: {regex_title!r}")
    return {"title": regex_title, "year": regex_year, "tmdb_id": None, "source": "regex"}


# ── 豆瓣评分 + 海报下载（卡片用，尽力而为）──

async def douban_rating(title: str) -> str:
    """豆瓣评分（无官方 API，尽力而为，失败返回 ''）。"""
    if not title:
        return ""
    try:
        from urllib.parse import quote
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://movie.douban.com/"}
        st, text = await _aio_get(
            f"https://movie.douban.com/j/subject_suggest?q={quote(title)}", headers=headers,
        )
        if st != 200:
            return ""
        items = json.loads(text) or []
        if not items:
            return ""
        url = items[0].get("url")
        if not url:
            return ""
        pst, ptext = await _aio_get(url, headers=headers)
        if pst != 200:
            return ""
        m = re.search(r'property="v:average"[^>]*content="([\d.]+)"', ptext)
        return f"{float(m.group(1)):.1f}" if m else ""
    except Exception as e:
        logger.warning(f"豆瓣评分获取失败: {e}")
        return ""


async def fetch_poster_bytes(poster_url: str) -> bytes | None:
    """下载 TMDB 海报图片字节。"""
    if not poster_url:
        return None
    try:
        async with _session().get(
            poster_url, proxy=_proxy(), timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.warning(f"海报下载失败: {e}")
    return None
