#!/usr/bin/env python3
"""Move completed files between two mounted cloud filesystems."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import tempfile
import time
from pathlib import Path, PurePosixPath


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("cd2-mount-mover")

SOURCE_DIR = Path(os.getenv("SOURCE_DIR", "/source")).resolve()
TARGET_DIR = Path(os.getenv("TARGET_DIR", "/target")).resolve()
STATE_FILE = Path(os.getenv("STATE_FILE", "/state/state.json"))
PIPELINE_QUEUE_DIR = (
    Path(os.getenv("PIPELINE_QUEUE_DIR", ""))
    if os.getenv("PIPELINE_QUEUE_DIR", "").strip()
    else None
)
PIPELINE_GROUP_DELAY = max(60, int(os.getenv("PIPELINE_GROUP_DELAY", "300")))
POLL_INTERVAL = max(5, int(os.getenv("POLL_INTERVAL", "30")))
STABLE_POLLS = max(2, int(os.getenv("STABLE_POLLS", "2")))
BUFFER_SIZE = 16 * 1024 * 1024

running = True


def stop(_signum, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def load_state() -> dict:
    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict) and isinstance(state.get("files"), dict):
            return state
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        log.exception("读取状态文件失败，将创建新状态: %s", STATE_FILE)
    return {"version": 1, "baseline_done": False, "files": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state.", dir=STATE_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, STATE_FILE)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def scan_files() -> dict[str, dict]:
    found = {}
    for root, _directories, filenames in os.walk(SOURCE_DIR, followlinks=False):
        for filename in filenames:
            path = Path(root) / filename
            try:
                stat = path.stat()
            except OSError as exc:
                log.warning("读取文件状态失败，稍后重试: %s (%s)", path, exc)
                continue
            relative = path.relative_to(SOURCE_DIR).as_posix()
            found[relative] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return found


def signature(info: dict) -> tuple[int, int]:
    return int(info["size"]), int(info["mtime_ns"])


def pipeline_group(relative: str) -> tuple[str, bool]:
    parts = PurePosixPath(relative).parts
    if len(parts) >= 2:
        return parts[0], True
    return relative, False


def pipeline_series_identity(relative: str) -> str | None:
    if len(PurePosixPath(relative).parts) != 1:
        return None
    episode = re.search(r"(?:^|[.\s_-])[Ss](\d{1,2})[.\s_-]?[Ee](\d{1,3})", relative)
    if not episode:
        return None
    season = int(episode.group(1))
    title = relative[:episode.start()]
    title = re.sub(r"[\s._-]*(19\d{2}|20\d{2})[\s._-]*$", " ", title)
    title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", title).lower()
    if not title:
        return None
    year_match = re.search(r"(?<![A-Za-z0-9])(19\d{2}|20\d{2})(?![\dpPiI])", relative)
    year = year_match.group(1) if year_match else ""
    return f"title:{title}:{year}:s{season:02d}"


def enqueue_pipeline(
    relative: str,
    info: dict,
    *,
    series_key: str | None = None,
    batch_expected_count: int = 0,
) -> bool:
    if PIPELINE_QUEUE_DIR is None:
        return True
    try:
        PIPELINE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        group_relative, is_group = pipeline_group(relative)
        task_id = hashlib.sha256(
            (
                f"folder\0{group_relative}"
                if is_group
                else f"file\0{relative}\0{info['size']}\0{info['mtime_ns']}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        base = PIPELINE_QUEUE_DIR / task_id
        existing = next(
            (
                Path(f"{base}.{suffix}.json")
                for suffix in ("pending", "processing", "failed", "done")
                if Path(f"{base}.{suffix}.json").exists()
            ),
            None,
        )
        if existing:
            if is_group and existing.name.endswith((".pending.json", ".failed.json")):
                try:
                    task = json.loads(existing.read_text(encoding="utf-8"))
                    task["last_seen_at"] = time.time()
                    task["next_retry_at"] = time.time() + PIPELINE_GROUP_DELAY
                    task["member_count"] = int(task.get("member_count", 0) or 0) + 1
                    temporary = PIPELINE_QUEUE_DIR / f".{task_id}.tmp"
                    temporary.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
                    os.replace(temporary, existing)
                except (OSError, json.JSONDecodeError):
                    log.warning("更新闭环分组任务失败，保留原任务: %s", group_relative)
            return True
        now = time.time()
        task = {
            "id": task_id,
            "kind": "folder" if is_group else "file",
            "relative_path": group_relative,
            "source_relative_path": relative,
            "size": int(info["size"]),
            "mtime_ns": int(info["mtime_ns"]),
            "created_at": now,
            "last_seen_at": now,
            "member_count": 1,
        }
        if is_group:
            task["next_retry_at"] = now + PIPELINE_GROUP_DELAY
        elif series_key:
            task["series_key"] = series_key
            task["batch_expected_count"] = max(1, int(batch_expected_count or 0))
        temporary = PIPELINE_QUEUE_DIR / f".{task_id}.tmp"
        temporary.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, PIPELINE_QUEUE_DIR / f"{task_id}.pending.json")
        log.info("已加入115闭环队列: %s", relative)
        return True
    except OSError as exc:
        log.warning("闭环队列写入失败，保留源文件并稍后重试: %s (%s)", relative, exc)
        return False


def move_file(
    relative: str,
    info: dict,
    *,
    series_key: str | None = None,
    batch_expected_count: int = 0,
) -> bool:
    source = SOURCE_DIR / relative
    target = TARGET_DIR / relative
    try:
        if not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size == info["size"]:
                if not enqueue_pipeline(
                    relative,
                    info,
                    series_key=series_key,
                    batch_expected_count=batch_expected_count,
                ):
                    return False
                source.unlink()
                log.info("目标已存在且大小一致，已删除源文件: %s", relative)
                return True
            log.error("目标文件冲突，保留源文件: %s", target)
            return False

        temporary_path = None
        try:
            fd, temporary_path = tempfile.mkstemp(
                prefix=f".cd2sync.{os.getpid()}.", suffix=".part", dir=target.parent
            )
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                while chunk := input_file.read(BUFFER_SIZE):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            temporary = Path(temporary_path)
            if temporary.stat().st_size != info["size"]:
                raise OSError("目标临时文件大小校验失败")
            os.replace(temporary, target)
            temporary_path = None
            if target.stat().st_size != info["size"]:
                raise OSError("目标文件大小校验失败")
            if not enqueue_pipeline(
                relative,
                info,
                series_key=series_key,
                batch_expected_count=batch_expected_count,
            ):
                return False
            source.unlink()
            log.info("已移动并校验: %s (%d bytes)", relative, info["size"])
            return True
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        log.warning("移动失败，保留源文件并稍后重试: %s (%s)", relative, exc)
        return False


def initialize_baseline(state: dict, current: dict[str, dict]) -> None:
    if state.get("baseline_done"):
        return
    for relative, info in current.items():
        state["files"][relative] = {
            "size": info["size"],
            "mtime_ns": info["mtime_ns"],
            "status": "baseline",
        }
    state["baseline_done"] = True
    save_state(state)
    log.info("首次基线建立完成，跳过现有文件: %d", len(current))


def process(current: dict[str, dict], state: dict) -> None:
    changed = False
    series_counts = {}
    for relative, info in current.items():
        old = state["files"].get(relative)
        if old and old.get("status") in {"baseline", "completed"} and signature(old) == signature(info):
            continue
        key = pipeline_series_identity(relative)
        if key:
            series_counts[key] = series_counts.get(key, 0) + 1

    for relative, info in current.items():
        old = state["files"].get(relative)
        current_signature = signature(info)
        if old and old.get("status") == "baseline" and signature(old) == current_signature:
            continue
        if old and old.get("status") == "completed" and signature(old) == current_signature:
            continue
        stable_polls = (
            int(old.get("stable_polls", 1)) + 1
            if old and signature(old) == current_signature
            else 1
        )
        entry = {
            "size": info["size"],
            "mtime_ns": info["mtime_ns"],
            "stable_polls": stable_polls,
            "status": "waiting",
        }
        if stable_polls >= STABLE_POLLS:
            entry["status"] = "moving"
            series_key = pipeline_series_identity(relative)
            if move_file(
                relative,
                info,
                series_key=series_key,
                batch_expected_count=series_counts.get(series_key, 0),
            ):
                entry["status"] = "completed"
            else:
                entry["status"] = "waiting"
            entry["stable_polls"] = 0 if entry["status"] == "completed" else stable_polls
        state["files"][relative] = entry
        changed = True
    if changed:
        save_state(state)


def main() -> None:
    if not SOURCE_DIR.is_dir() or not TARGET_DIR.is_dir():
        raise SystemExit(f"源目录或目标目录不存在: {SOURCE_DIR} -> {TARGET_DIR}")
    state = load_state()
    initialize_baseline(state, scan_files())
    log.info(
        "开始监控: %s -> %s, interval=%ss, stable_polls=%s",
        SOURCE_DIR,
        TARGET_DIR,
        POLL_INTERVAL,
        STABLE_POLLS,
    )
    while running:
        try:
            process(scan_files(), state)
        except OSError:
            log.exception("扫描挂载目录失败，稍后重试")
        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)
    log.info("同步服务已停止")


if __name__ == "__main__":
    main()
