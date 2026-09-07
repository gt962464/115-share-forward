import ast
import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1]
SOURCE_FILE = PROJECT_DIR / "card-bot" / "cardbot.py"


class Logger:
    def warning(self, _message):
        pass

    def info(self, _message):
        pass


def load_pipeline_helpers(queue_dir):
    wanted = {
        "_pipeline_task_paths",
        "_pipeline_write",
        "_pipeline_read",
        "_pipeline_series_identity",
        "_pipeline_series_group_ready",
        "_pipeline_prepare_series_groups",
        "_pipeline_candidate_priority",
        "_pipeline_series_wait_until",
        "_pipeline_recover_processing",
        "_is_unrecoverable_reason",
    }
    tree = ast.parse(SOURCE_FILE.read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    namespace = {
        "Path": Path,
        "PIPELINE_QUEUE_DIR": queue_dir,
        "tempfile": tempfile,
        "os": os,
        "json": json,
        "hashlib": hashlib,
        "re": re,
        "time": time,
        "logger": Logger(),
        "PIPELINE_SERIES_DELAY": 1800,
        "infer_episode_range": lambda names: "S02E01-E02" if len(names) == 2 else "",
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE_FILE), "exec"), namespace)
    return namespace


def test_pipeline_queue_write_read_and_restart_recovery():
    with tempfile.TemporaryDirectory() as temporary:
        queue_dir = Path(temporary)
        helpers = load_pipeline_helpers(queue_dir)
        processing = queue_dir / "task.processing.json"
        task = {"id": "task", "relative_path": "节目.mkv"}

        helpers["_pipeline_write"](processing, task)
        assert helpers["_pipeline_read"](processing) == task

        helpers["_pipeline_recover_processing"]()
        failed = queue_dir / "task.failed.json"
        assert failed.is_file()
        assert not processing.exists()
        assert helpers["_pipeline_task_paths"]("failed") == [failed]


def test_pipeline_worker_is_started_by_main():
    tree = ast.parse(SOURCE_FILE.read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "main")
    calls = [ast.unparse(node) for node in ast.walk(main) if isinstance(node, ast.Call)]
    assert "asyncio.create_task(_pipeline_worker())" in calls


def test_root_episode_identity_groups_only_same_show_and_season():
    with tempfile.TemporaryDirectory() as temporary:
        helpers = load_pipeline_helpers(Path(temporary))
        identity = helpers["_pipeline_series_identity"]
        first = {"relative_path": "忙忙碌碌寻宝藏.2024.S02E01.2160p.mp4", "tmdb_id": 259700}
        second = {"relative_path": "忙忙碌碌寻宝藏.2024.S02E02.2160p.mp4", "tmdb_id": 259700}
        other_season = {"relative_path": "忙忙碌碌寻宝藏.2024.S01E01.2160p.mp4", "tmdb_id": 259700}
        other_show = {"relative_path": "另一部剧.2024.S02E01.2160p.mp4", "tmdb_id": 123456}

        assert identity(first) == identity(second)
        assert identity(first) != identity(other_season)
        assert identity(first) != identity(other_show)
        assert identity({"relative_path": "电影.2026.2160p.mkv"}) is None


def test_expected_batch_is_ready_without_quiet_delay():
    with tempfile.TemporaryDirectory() as temporary:
        helpers = load_pipeline_helpers(Path(temporary))
        ready = helpers["_pipeline_series_group_ready"]
        tasks = [
            {"id": "a", "batch_expected_count": 2, "created_at": time.time()},
            {"id": "b", "batch_expected_count": 2, "created_at": time.time()},
        ]
        assert ready(tasks)
        tasks[0]["batch_expected_count"] = 3
        assert not ready(tasks)


def test_single_episode_waits_for_batch_but_single_file_batch_does_not():
    with tempfile.TemporaryDirectory() as temporary:
        helpers = load_pipeline_helpers(Path(temporary))
        wait_until = helpers["_pipeline_series_wait_until"]
        task = {
            "relative_path": "节目.2026.S01E01.2160p.mp4",
            "batch_expected_count": 8,
            "last_seen_at": 100,
        }
        assert wait_until(task) == 1900
        task["batch_expected_count"] = 1
        assert wait_until(task) == 0
        task["batch_expected_count"] = 8
        task["share_link"] = "https://115.com/s/test"
        assert wait_until(task) == 0
        task.pop("share_link")
        task["kind"] = "series"
        assert wait_until(task) == 0


def test_existing_episode_shares_are_collapsed_into_one_task():
    with tempfile.TemporaryDirectory() as temporary:
        queue_dir = Path(temporary)
        helpers = load_pipeline_helpers(queue_dir)
        for task_id, episode in (("a", 1), ("b", 2)):
            task = {
                "id": task_id,
                "kind": "file",
                "relative_path": f"忙忙碌碌寻宝藏.2024.S02E{episode:02d}.2160p.mp4",
                "tmdb_id": 259700,
                "display_title": "忙忙碌碌寻宝藏",
                "year": "2024",
                "canonical_name": f"episode-{episode}",
                "share_link": f"https://115.com/s/{task_id}",
                "size": episode * 100,
                "created_at": float(episode),
                "last_seen_at": float(episode),
            }
            helpers["_pipeline_write"](queue_dir / f"{task_id}.failed.json", task)

        assert helpers["_pipeline_prepare_series_groups"]() == 1
        aggregate_paths = helpers["_pipeline_task_paths"]("pending")
        assert len(aggregate_paths) == 1
        aggregate = helpers["_pipeline_read"](aggregate_paths[0])
        assert aggregate["kind"] == "series"
        assert aggregate["member_count"] == 2
        assert aggregate["size"] == 300
        assert aggregate["episode"] == "S02E01-E02"
        assert len(helpers["_pipeline_task_paths"]("done")) == 2


def test_existing_share_has_worker_priority():
    with tempfile.TemporaryDirectory() as temporary:
        queue_dir = Path(temporary)
        helpers = load_pipeline_helpers(queue_dir)
        plain = queue_dir / "plain.pending.json"
        shared = queue_dir / "shared.failed.json"
        helpers["_pipeline_write"](plain, {"id": "plain", "created_at": 1})
        helpers["_pipeline_write"](shared, {"id": "shared", "share_link": "https://115.com/s/x", "created_at": 2})
        paths = [plain, shared]
        paths.sort(key=helpers["_pipeline_candidate_priority"])
        assert paths == [shared, plain]


def test_terminal_share_states_do_not_retry():
    with tempfile.TemporaryDirectory() as temporary:
        helpers = load_pipeline_helpers(Path(temporary))
        terminal = helpers["_is_unrecoverable_reason"]
        assert terminal("115 分享包含违规文件，审核未通过")
        assert terminal("115 分享已失效")
        assert terminal("分享已取消")
        assert not terminal("115 分享仍在系统处理中，继续轮询")


def test_existing_share_folder_skips_recursive_scan():
    tree = ast.parse(SOURCE_FILE.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_pipeline_process_folder_task"
    )
    calls = []

    async def share_ready(_svc, _task, _link):
        calls.append("audit")

    async def send_card(_task, _link):
        calls.append("send")

    async def collect_videos(_svc, _folder_id):
        raise AssertionError("existing share must not recursively scan the folder")

    namespace = {
        "PIPELINE_115_ROOT": "/自动转存",
        "_pipeline_require_share_ready": share_ready,
        "_pipeline_send_card": send_card,
        "_pipeline_collect_videos": collect_videos,
        "_schedule_cleanup": lambda *args, **kwargs: calls.append("cleanup"),
        "logger": Logger(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE_FILE), "exec"), namespace)
    task = {
        "kind": "folder",
        "relative_path": "节目",
        "folder_id": 123,
        "share_link": "https://115.com/s/test",
        "canonical_name": "节目.S01E01.mkv",
        "display_title": "节目",
    }
    result = asyncio.run(namespace["_pipeline_process_folder_task"](task, object()))
    assert result["cleanup_scheduled"] is True
    assert calls == ["audit", "send", "cleanup"]


if __name__ == "__main__":
    test_pipeline_queue_write_read_and_restart_recovery()
    test_pipeline_worker_is_started_by_main()
    test_root_episode_identity_groups_only_same_show_and_season()
    test_expected_batch_is_ready_without_quiet_delay()
    test_single_episode_waits_for_batch_but_single_file_batch_does_not()
    test_existing_episode_shares_are_collapsed_into_one_task()
    test_existing_share_has_worker_priority()
    test_terminal_share_states_do_not_retry()
    test_existing_share_folder_skips_recursive_scan()
    print("MOUNT PIPELINE WORKER TESTS OK")
