from pathlib import Path

from core.database import Video, VideoRepository
from core.path_utils import to_file_uri
from core.title_translation import (
    apply_translation,
    repath_translation_history,
    rollback_translation,
    sync_nfo_title,
    translation_history_keys,
)


def _insert_video(db_path: Path, path: str) -> VideoRepository:
    repo = VideoRepository(db_path)
    repo.upsert(
        Video(
            path=path,
            number="ABC-123",
            title="日本語タイトル",
            original_title="",
            actresses=["梓ヒカリ"],
        )
    )
    return repo


def test_apply_and_rollback_are_transactional(temp_db):
    path = "file:///D:/Videos/JAV/ABC-123.mp4"
    repo = _insert_video(temp_db, path)

    state = apply_translation(
        temp_db,
        path=path,
        number="ABC-123",
        expected_title="日本語タイトル",
        expected_original_title="",
        title="中文标题 梓光莉",
        original_title="日本語タイトル",
        source="test",
    )

    assert state == "updated"
    assert repo.get_by_path(path).title == "中文标题 梓光莉"
    assert path in translation_history_keys(temp_db)[0]

    history = rollback_translation(temp_db, path=path, number="ABC-123")

    assert history["old_title"] == "日本語タイトル"
    assert repo.get_by_path(path).title == "日本語タイトル"
    assert translation_history_keys(temp_db) == (set(), set())


def test_apply_rejects_stale_title_without_writing_history(temp_db):
    path = "file:///D:/Videos/JAV/ABC-123.mp4"
    repo = _insert_video(temp_db, path)

    state = apply_translation(
        temp_db,
        path=path,
        number="ABC-123",
        expected_title="stale value",
        expected_original_title="",
        title="translated",
        original_title="日本語タイトル",
        source="test",
    )

    assert state == "conflict"
    assert repo.get_by_path(path).title == "日本語タイトル"
    assert translation_history_keys(temp_db) == (set(), set())


def test_translation_history_supports_multiple_rollback_levels(temp_db):
    path = "file:///D:/Videos/JAV/ABC-123.mp4"
    repo = _insert_video(temp_db, path)
    first = apply_translation(
        temp_db,
        path=path,
        number="ABC-123",
        expected_title="日本語タイトル",
        expected_original_title="",
        title="第一次翻译",
        original_title="日本語タイトル",
        source="test",
    )
    second = apply_translation(
        temp_db,
        path=path,
        number="ABC-123",
        expected_title="第一次翻译",
        expected_original_title="日本語タイトル",
        title="第二次翻译",
        original_title="日本語タイトル",
        source="test",
    )

    assert (first, second) == ("updated", "updated")
    rollback_translation(temp_db, path=path, number="ABC-123")
    assert repo.get_by_path(path).title == "第一次翻译"
    rollback_translation(temp_db, path=path, number="ABC-123")
    assert repo.get_by_path(path).title == "日本語タイトル"


def test_translation_history_follows_a_renamed_video(temp_db):
    old_path = "file:///D:/Videos/JAV/ABC-123/ABC-123.mp4"
    new_path = "file:///D:/Videos/JAV/ABC-123-title/ABC-123-title.mp4"
    repo = _insert_video(temp_db, old_path)
    apply_translation(
        temp_db,
        path=old_path,
        number="ABC-123",
        expected_title="日本語タイトル",
        expected_original_title="",
        title="中文标题",
        original_title="日本語タイトル",
        source="test",
    )
    assert repo.repath_path_only(old_path, new_path) is True

    assert repath_translation_history(temp_db, old_path, new_path) == 1
    history = rollback_translation(temp_db, path=new_path, number="ABC-123")

    assert history is not None
    assert repo.get_by_path(new_path).title == "日本語タイトル"


def test_number_fallback_refuses_ambiguous_multiversion_history(temp_db):
    old_paths = [
        "file:///D:/Videos/JAV/version-a/ABC-123.mp4",
        "file:///D:/Videos/JAV/version-b/ABC-123.mp4",
    ]
    new_paths = [path.replace("ABC-123.mp4", "ABC-123-renamed.mp4") for path in old_paths]
    repo = VideoRepository(temp_db)
    for old_path, new_path in zip(old_paths, new_paths, strict=True):
        repo.upsert(Video(path=old_path, number="ABC-123", title="日本語タイトル"))
        apply_translation(
            temp_db,
            path=old_path,
            number="ABC-123",
            expected_title="日本語タイトル",
            expected_original_title="",
            title="中文标题",
            original_title="日本語タイトル",
            source="test",
        )
        assert repo.repath_path_only(old_path, new_path) is True

    history_paths, history_numbers = translation_history_keys(temp_db)
    assert history_paths == set(old_paths)
    assert "ABC-123" not in history_numbers
    assert rollback_translation(temp_db, path=new_paths[0], number="ABC-123") is None
    assert repo.get_by_path(new_paths[0]).title == "中文标题"


def test_sync_nfo_title_updates_both_fields(tmp_path):
    video_path = tmp_path / "ABC-123.mp4"
    video_path.write_bytes(b"video")
    nfo_path = video_path.with_suffix(".nfo")
    nfo_path.write_text(
        "<?xml version=\"1.0\" encoding=\"utf-8\"?><movie><title>old</title></movie>",
        encoding="utf-8",
    )

    mtime = sync_nfo_title(
        to_file_uri(str(video_path)),
        "中文标题",
        "日本語タイトル",
        {},
    )
    content = nfo_path.read_text(encoding="utf-8")

    assert mtime == nfo_path.stat().st_mtime
    assert "<title>中文标题</title>" in content
    assert "<originaltitle>日本語タイトル</originaltitle>" in content
