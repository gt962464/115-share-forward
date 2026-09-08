"""
115 分享链接 + ed2k 链接提取与解析
"""
import re
import urllib.parse
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
# ed2k://|file|文件名|文件大小|哈希|/，文件名通常已 URL 编码
ED2K_RE = re.compile(r"ed2k://\|file\|[^|\r\n]+\|\d+\|[A-Fa-f0-9]{32}\|/", re.I)
# ed2k 尾部标点
_ED2K_TAIL_RE = re.compile(r"[\s。，,；;！!？?#]+$")


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


# ── ed2k 链接 ──

def extract_ed2k_links(text: str) -> list[str]:
    """从文本中提取 ed2k 链接（兼容 URL 编码形式）。"""
    clean = CTRL_RE.sub("", text or "")
    candidates = ED2K_RE.findall(clean)
    for encoded in re.findall(r"ed2k://[^\s<>]+", clean, re.I):
        decoded = urllib.parse.unquote(encoded)
        if ED2K_RE.fullmatch(decoded):
            candidates.append(decoded)
    seen, out = set(), []
    for u in candidates:
        u = _ED2K_TAIL_RE.sub("", u)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def parse_ed2k(url: str) -> dict:
    """解析 ed2k 文件链接 → {url, name, size, hash}。不下载、不连 115。"""
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
