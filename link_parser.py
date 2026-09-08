"""
115 分享链接提取与解析
"""
import re
from urllib.parse import parse_qs, urlsplit


# 匹配各种 115 域名的分享链接
LINK_RE = re.compile(
    r"https?://(?:115\.com|115cdn\.com|anxia\.com)/s/[A-Za-z0-9]+(?:[?#][^\s<>\"]+)?",
    re.IGNORECASE,
)
# 去除零宽字符
CTRL_RE = re.compile(r"[\u200b\u200c\u200d\u2066-\u2069\ufeff]")
# 尾部多余字符
TRAIL_RE = re.compile(r"[^A-Za-z0-9?=&:/._-]+$")


def extract_115_links(text: str, entities=None) -> list[dict]:
    """
    从文本和 Telegram 实体中提取 115 分享链接。
    
    返回: [{"url": str, "code": str, "password": str}, ...]
    """
    clean_text = CTRL_RE.sub("", text or "")
    candidates = list(LINK_RE.findall(clean_text))
    
    # 也从 Telegram URL 实体中提取（有些链接在 text 里被隐藏了）
    for entity in entities or []:
        entity_url = getattr(entity, "url", None)
        if entity_url and LINK_RE.search(entity_url):
            candidates.append(entity_url)
    
    found = []
    seen = set()
    for candidate in candidates:
        url = TRAIL_RE.sub("", candidate).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        parsed = _parse_115_url(url)
        if parsed:
            found.append(parsed)
    return found


def _parse_115_url(url: str) -> dict | None:
    """解析单个 115 URL，提取 code 和 password。"""
    m = re.search(r"/(?:115\.com|115cdn\.com|anxia\.com)/s/([A-Za-z0-9]+)", url)
    if not m:
        return None
    
    code = m.group(1)
    password = ""
    try:
        query = parse_qs(urlsplit(url).query)
        for key in ("password", "pwd", "passcode"):
            values = query.get(key, [])
            if values:
                password = values[0]
                break
    except Exception:
        pass
    
    return {"url": url, "code": code, "password": password}


def parse_115_url(url: str) -> dict | None:
    """公开接口：解析单个 URL。"""
    return _parse_115_url(url)
