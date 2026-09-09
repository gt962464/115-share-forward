"""聚影缓存 — 保存最近一次转存信息，供同步按钮使用。纯新增文件。"""
import threading

_lock = threading.Lock()
_last_share = {}


def set_last_share(title: str, year: str, tmdb_id, share_link: str, filename: str = ""):
    global _last_share
    with _lock:
        _last_share = {
            "title": title,
            "year": year,
            "tmdb_id": tmdb_id,
            "share_link": share_link,
            "filename": filename,
        }


def get_last_share() -> dict:
    with _lock:
        return dict(_last_share) if _last_share else {}
