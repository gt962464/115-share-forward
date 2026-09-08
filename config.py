"""
配置管理 - 从环境变量读取，支持 Bot 动态修改并持久化到 .env
"""
import os
import threading
from pathlib import Path

_lock = threading.Lock()

# .env 文件路径
_ENV_PATH = Path(os.getenv("ENV_FILE", "/data/.env"))
if not _ENV_PATH.parent.exists():
    _ENV_PATH = Path(__file__).parent / ".env"

# 所有可配置项的定义: key -> (默认值, 描述, 是否敏感)
CONFIG_SCHEMA = {
    "P115_COOKIE":           ("",     "115 Cookie", True),
    "P115_SAVE_DIR":         ("自动转存", "转存保存目录", False),
    "TG_BOT_TOKEN":          ("",     "Bot Token", True),
    "TG_CHANNEL_ID":         ("",     "输出频道 ID", False),
    "TG_USER_ID":            ("",     "管理员 ID", False),
    "TG_ADMIN_IDS":          ("",     "额外管理员 ID（逗号分隔）", False),
    "TG_ALLOW_SUBMIT_IDS":   ("",     "提交白名单 ID（逗号分隔，空=仅管理员）", False),
    "TG_API_ID":             ("",     "Telethon API ID", False),
    "TG_API_HASH":           ("",     "Telethon API Hash", True),
    "TG_PHONE":              ("",     "手机号（登录用）", True),
    "TG_MONITOR_TARGETS":    ("",     "监听目标（逗号分隔）", False),
    "TG_MONITOR_MODE":       ("private", "监听模式 private/channel", False),
    "TG_PROXY":              ("",     "TG 代理 socks5://...", True),
    "APP_HTTP_PROXY":        ("",     "HTTP 代理", True),
    "TMDB_API_KEY":          ("",     "TMDB API Key", True),
    "TMDB_LANG":             ("zh-CN", "TMDB 语言", False),
    "LLM_API_BASE":          ("https://apihub.agnes-ai.com/v1", "OpenAI 兼容 API Base", False),
    "LLM_API_KEY":           ("",     "OpenAI API Key", True),
    "LLM_MODEL":             ("agnes-2.5-flash", "OpenAI 模型", False),
    "LLM_PROMPT":            ("",     "自定义识别提示词（空=内置默认）", False),
    "LOG_LEVEL":             ("INFO", "日志级别", False),
    "AUTO_RENAME":           ("1",    "自动重命名 0/1", False),
    "AUTO_DELETE_AFTER":     ("0",    "发卡成功后自动删除源文件延迟秒数（0=不删除）", False),
    "RECYCLE_PASSWORD":      ("",     "115 回收站密码（清空回收站用）", True),
}


def _load_env_file():
    """从 .env 文件加载配置（只在环境变量未设置时）。"""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and not os.environ.get(key):
            os.environ[key] = value


def _save_env_file(updates: dict = None):
    """将当前配置写入 .env 文件。保留注释，更新/新增键值。"""
    lines = []
    existing_keys = set()

    if _ENV_PATH.exists():
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                lines.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            existing_keys.add(key)
            # 用新值覆盖
            val = os.environ.get(key, "")
            lines.append(f"{key}={val}")

    # 追加新增的 key
    if updates:
        for key in updates:
            if key not in existing_keys:
                val = os.environ.get(key, "")
                lines.append(f"{key}={val}")

    _ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_config(key: str, value: str) -> str:
    """
    设置一个配置项并持久化到 .env。
    返回 human-readable 的结果消息。
    """
    key = key.upper().strip()
    if key not in CONFIG_SCHEMA:
        valid = ", ".join(sorted(CONFIG_SCHEMA.keys()))
        return f"❌ 未知配置项: {key}\n\n可配置项:\n{valid}"

    with _lock:
        os.environ[key] = value
        _save_env_file({key: value})

    _, desc, sensitive = CONFIG_SCHEMA[key]
    display_val = "***" if sensitive and value else value
    return f"✅ 已设置 {desc} ({key}) = {display_val}"


def get_config(key: str = None) -> dict | str:
    """
    获取配置。key=None 返回全部，否则返回单个。
    敏感值会被遮掩。
    """
    if key:
        key = key.upper().strip()
        if key not in CONFIG_SCHEMA:
            return f"❌ 未知配置项: {key}"
        val = os.environ.get(key, CONFIG_SCHEMA[key][0])
        _, desc, sensitive = CONFIG_SCHEMA[key]
        return {key: val, "desc": desc, "sensitive": sensitive}

    result = {}
    for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
        val = os.environ.get(k, default)
        result[k] = {
            "value": val,
            "desc": desc,
            "sensitive": sensitive,
            "set": bool(val),
        }
    return result


def format_config_list() -> str:
    """格式化当前配置为可读文本（敏感值遮掩）。"""
    lines = ["⚙️ 当前配置:\n"]
    for k, (default, desc, sensitive) in CONFIG_SCHEMA.items():
        val = os.environ.get(k, default)
        if sensitive and val:
            display = f"{val[:4]}***{val[-4:]}" if len(val) > 8 else "***"
        elif val:
            display = val if len(val) <= 60 else val[:57] + "..."
        else:
            display = "(未设置)"
        lines.append(f"  {desc}: {display}")
    return "\n".join(lines)


# ── 初始化 ──

load_env_file = _load_env_file
load_env_file()

# 加载剩余的配置
P115_COOKIE = os.getenv("P115_COOKIE", "").strip()
P115_SAVE_DIR = (os.getenv("P115_SAVE_DIR") or "自动转存").strip()
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHANNEL_ID = os.getenv("TG_CHANNEL_ID", "").strip()
TG_USER_ID = os.getenv("TG_USER_ID", "").strip()
TG_API_ID = int(os.getenv("TG_API_ID") or "0")
TG_API_HASH = os.getenv("TG_API_HASH") or ""
TG_PHONE = os.getenv("TG_PHONE", "").strip()
TG_SESSION = os.getenv("TG_SESSION", "/data/user.session")
TG_PROXY = os.getenv("TG_PROXY", "").strip()
TG_MONITOR_TARGETS = [
    v.strip() for v in os.getenv("TG_MONITOR_TARGETS", "").split(",") if v.strip()
]
TG_MONITOR_MODE = os.getenv("TG_MONITOR_MODE", "private").strip()
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_LANG = (os.getenv("TMDB_LANG") or "zh-CN").strip()
APP_HTTP_PROXY = os.getenv("APP_HTTP_PROXY", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip()
AUTO_RENAME = os.getenv("AUTO_RENAME", "1").strip().lower() in {"1", "true", "yes"}
SHARE_AUDIT_WAIT_TIMEOUT = int(os.getenv("SHARE_AUDIT_WAIT_TIMEOUT", str(15 * 60)))
SHARE_AUDIT_POLL_INTERVAL = int(os.getenv("SHARE_AUDIT_POLL_INTERVAL", "30"))
RETRY_INTERVAL = int(os.getenv("RETRY_INTERVAL", "60"))
MAX_RETRY = int(os.getenv("MAX_RETRY", "3"))

# 管理员 IDs
TG_ADMIN_IDS = {
    v for v in os.getenv("TG_ADMIN_IDS", "").replace(",", " ").split() if v.strip()
}
if TG_USER_ID:
    TG_ADMIN_IDS.add(TG_USER_ID)

# 提交链接白名单（空 = 仅管理员可提交）
TG_ALLOW_SUBMIT_IDS = {
    v for v in os.getenv("TG_ALLOW_SUBMIT_IDS", "").replace(",", " ").split() if v.strip()
}
