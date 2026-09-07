"""123 云盘分享链接的只读识别与规范化。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit


SUPPORTED_HOSTS = {
    "123pan.com",
    "123865.com",
    "123912.com",
    "123pan.cn",
}
SHARE_HOST_RE = re.compile(r"^[0-9]+\.share\.123pan\.cn$", re.IGNORECASE)
CTRL_RE = re.compile(r"[\u200b\u200c\u200d\u2066-\u2069\ufeff]")
LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:[0-9]+\.share\.)?(?:123pan\.com|123865\.com|123912\.com|123pan\.cn)"
    r"/(?:s|123pan)/[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?(?:[?#][^\s<>\"]+)?",
    re.IGNORECASE,
)
CODE_RE = re.compile(
    r"(?:提取码|访问码|密码|pwd|passcode|share[_ -]?code)\s*[:：=：]?\s*([A-Za-z0-9]{2,24})",
    re.IGNORECASE,
)
TRAILING_CHARS = ".,;:!?)]}>，。；：！？）》」』"


def _clean_url(value: str) -> str:
    return CTRL_RE.sub("", str(value or "")).strip().rstrip(TRAILING_CHARS)


def _is_supported_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().removeprefix("www.")
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (host in SUPPORTED_HOSTS or SHARE_HOST_RE.fullmatch(host) is not None)
    )


def _code_from_url(value: str) -> str:
    try:
        query = parse_qs(urlsplit(value).query)
    except ValueError:
        return ""
    for key in ("pwd", "password", "passcode", "code", "share_code"):
        values = query.get(key) or []
        if values and re.fullmatch(r"[A-Za-z0-9]{2,24}", values[0]):
            return values[0]
    return ""


def parse_123_link(value: str, source_text: str = "") -> dict[str, str]:
    """返回规范化链接、分享标识和可选访问码；不访问网络。"""
    url = _clean_url(value)
    if not _is_supported_url(url):
        return {}
    path = urlsplit(url).path.rstrip("/")
    match = re.search(r"/(?:s|123pan)/([A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)", path, re.IGNORECASE)
    if not match:
        return {}
    access_code = _code_from_url(url)
    if not access_code:
        code_match = CODE_RE.search(source_text or "")
        access_code = code_match.group(1) if code_match else ""
    return {
        "url": url,
        "share_id": match.group(1),
        "access_code": access_code,
        "host": (urlsplit(url).hostname or "").lower().removeprefix("www."),
    }


def extract_123_links(text: str, entities=None) -> list[dict[str, str]]:
    """从正文和 Telegram URL 实体提取去重后的 123 链接。"""
    clean_text = CTRL_RE.sub("", text or "")
    candidates = list(LINK_RE.findall(clean_text))
    for entity in entities or []:
        entity_url = getattr(entity, "url", None)
        if entity_url:
            candidates.append(entity_url)

    found = []
    seen = set()
    for candidate in candidates:
        item = parse_123_link(candidate, clean_text)
        if item and item["url"] not in seen:
            seen.add(item["url"])
            found.append(item)
    return found
