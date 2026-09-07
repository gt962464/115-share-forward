import importlib.util
import json
import tempfile
from pathlib import Path


PROJECT_DIR = Path(__file__).parents[1]
SOURCE_FILE = PROJECT_DIR / "card-bot" / "cd2-mount-mover" / "sync_mount.py"


def load_mover():
    spec = importlib.util.spec_from_file_location("mount_mover_for_test", SOURCE_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_series_identity_separates_shows_and_seasons():
    mover = load_mover()
    first = "忙忙碌碌寻宝藏.2024.S02E01.2160p.WEB-DL.mp4"
    second = "忙忙碌碌寻宝藏.2024.S02E02.2160p.WEB-DL {tmdb-259700}.mp4"
    other_season = "忙忙碌碌寻宝藏.2024.S01E01.2160p.WEB-DL.mp4"
    other_show = "另一部剧.2024.S02E01.2160p.WEB-DL.mp4"
    assert mover.pipeline_series_identity(first) == mover.pipeline_series_identity(second)
    assert mover.pipeline_series_identity(first) != mover.pipeline_series_identity(other_season)
    assert mover.pipeline_series_identity(first) != mover.pipeline_series_identity(other_show)

    title_with_digits = "再見1987.2026.S01E04.1080p.WEB-DL.mkv"
    title_with_digits_next = "再見1987.2026.S01E05.1080p.WEB-DL.mkv"
    assert mover.pipeline_series_identity(title_with_digits) == mover.pipeline_series_identity(title_with_digits_next)


def test_enqueue_records_expected_batch_count():
    mover = load_mover()
    with tempfile.TemporaryDirectory() as temporary:
        mover.PIPELINE_QUEUE_DIR = Path(temporary)
        relative = "忙忙碌碌寻宝藏.2024.S02E01.2160p.WEB-DL.mp4"
        info = {"size": 123, "mtime_ns": 456}
        key = mover.pipeline_series_identity(relative)
        assert mover.enqueue_pipeline(
            relative,
            info,
            series_key=key,
            batch_expected_count=35,
        )
        task_path = next(Path(temporary).glob("*.pending.json"))
        task = json.loads(task_path.read_text(encoding="utf-8"))
        assert task["series_key"] == key
        assert task["batch_expected_count"] == 35


if __name__ == "__main__":
    test_series_identity_separates_shows_and_seasons()
    test_enqueue_records_expected_batch_count()
    print("MOUNT MOVER BATCH TESTS OK")
