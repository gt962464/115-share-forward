"""
链接投递去重：同一条 115 链接重复发多次，只处理第一次。

现状问题：
- monitor.py 的 _processed_keys 用 `f"{event.id}:{url}"` 做 key，
  带消息 ID 前缀 → 同一条链接在不同消息里 key 不同 → 完全挡不住重复投递。
- link_history 表实测 0 行，说明这条链路从来没生效过。

本模块：
- 以「链接路径（share_code）+ 提取码」为唯一 key，跨消息、跨重启生效。
- 记录投递次数：第 1 次正常处理；第 2 次起只累加计数并静默跳过。
- 可通过 DUP_LINK_MAX 控制「累计投递几次后停止记录」。
"""
import json
import os
import re
import time
from pathlib import Path

_FILE = Path(os.getenv("DUP_LINK_FILE", "/data/dup_links.json"))
_MAX_RECORDS = 50000

# url → 归一化 key
_URL_RE = re.compile(r"/s/([A-Za-z0-9]+)")


def _key(url: str) -> str:
    """share_code 为主键；提取码不同视为不同分享（内容可能已换）。"""
    if not url:
        return ""
    u = str(url).strip()
    m = _URL_RE.search(u)
    code = m.group(1) if m else u.split("?")[0]
    pwd = ""
    pm = re.search(r"(?:[?&](?:password|pwd|passcode)=([A-Za-z0-9]+))", u)
    if pm:
        pwd = pm.group(1)
    return f"{code}:{pwd}" if pwd else code


class _Store:
    """线程/协程安全的去重存储，落盘 JSON。"""

    def __init__(self):
        self._data = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            if _FILE.exists():
                raw = json.loads(_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except Exception:
            self._data = {}

    def _save(self):
        try:
            _FILE.parent.mkdir(parents=True, exist_ok=True)
            if len(self._data) > _MAX_RECORDS:
                # 丢掉最旧的一半
                items = sorted(self._data.items(), key=lambda kv: kv[1].get("last", 0))
                self._data = dict(items[-_MAX_RECORDS // 2:])
            tmp = _FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_FILE)
        except Exception:
            pass

    def hit(self, url: str, limit: int = 0):
        """返回 (是否首次, 累计次数)。

        limit > 0 时为封顶计数：达到上限后不再累加（避免长尾重复投递
        把无意义的数字越滚越大，同时保持返回值的可读性）。
        """
        self._load()
        k = _key(url)
        if not k:
            return True, 0
        rec = self._data.get(k)
        now = time.time()
        if rec is None:
            self._data[k] = {"count": 1, "first": now, "last": now}
            self._save()
            return True, 1
        cur = int(rec.get("count", 1))
        if limit and cur >= limit:
            rec["last"] = now
            self._save()
            return False, cur
        rec["count"] = cur + 1
        rec["last"] = now
        self._save()
        return False, rec["count"]

    def peek(self, url: str):
        """只查询不写入 → (是否已处理过, 累计次数)。"""
        self._load()
        k = _key(url)
        rec = self._data.get(k) if k else None
        if not rec:
            return False, 0
        return True, int(rec.get("count", 1))

    def stats(self):
        self._load()
        return len(self._data)

    def purge(self):
        self._loaded = True
        self._data = {}
        self._save()


_store = _Store()


def check_and_mark(url: str):
    """
    投递去重入口。返回 (should_process, count)。

    should_process=True  → 第一次投递，正常处理
    should_process=False → 重复投递，已累加计数并跳过
    count = 累计投递次数
    """
    limit = max(1, int(os.getenv("DUP_LINK_MAX", "3")))
    first, count = _store.hit(url, limit=limit)
    if first:
        return True, 1
    return False, count


def is_seen(url: str):
    return _store.peek(url)


def stats() -> int:
    return _store.stats()


def purge():
    _store.purge()
