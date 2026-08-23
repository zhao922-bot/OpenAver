from pathlib import Path

import pytest

from core.database import Video
from core.path_utils import to_file_uri
from core import video_rename


def _config():
    return {
        "scraper": {
            "suffix_keywords": ["-cd1", "-4k"],
            "video_extensions": [".mp4", ".mkv"],
        }
    }


def _video(path: Path) -> Video:
    return Video(
        path=to_file_uri(str(path)),
        number="ABC-123",
        title="中文标题",
        original_title="日本語タイトル",
        actresses=["梓ヒカリ"],
    )


@pytest.fixture
def media_folder(tmp_path, monkeypatch):
    folder = tmp_path / "ABC-123"
    folder.mkdir()
    video_path = folder / "ABC-123.mp4"
    video_path.write_bytes(b"video")
    (folder / "ABC-123-poster.jpg").write_bytes(b"poster")
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    (folder / "ABC-123.nfo").write_text(
        "<movie><title>日本語タイトル</title><thumb>ABC-123-poster.jpg</thumb></movie>",
        encoding="utf-8",
    )
    monkeypatch.setattr(video_rename, "load_config", _config)
    monkeypatch.setattr(
        video_rename,
        "load_actress_alias_groups",
        lambda: [("梓光莉", ["梓光莉", "梓ヒカリ"])],
    )
    monkeypatch.setattr(video_rename, "repath_translation_history", lambda *_args: 0)
    return folder, video_path


def test_plan_renames_video_sidecars_and_folder(media_folder):
    folder, video_path = media_folder
    plan = video_rename.plan_video_rename(_video(video_path), {})

    assert plan["new_base"] == "ABC-123 - 日本語タイトル 梓光莉"
    assert Path(plan["new_dir"]).name == plan["new_base"]
    assert {Path(move["from"]).name for move in plan["file_moves"]} == {
        "ABC-123.mp4",
        "ABC-123.nfo",
        "ABC-123-poster.jpg",
    }
    assert folder / "notes.txt" not in {Path(move["from"]) for move in plan["file_moves"]}


def test_apply_and_rollback_restore_files_and_nfo(media_folder, monkeypatch):
    folder, video_path = media_folder
    events = {}

    def append_event(event):
        value = {**event, "id": "event-1"}
        events[value["id"]] = value
        return value

    def update_event(event_id, **changes):
        events[event_id].update(changes)
        return True

    monkeypatch.setattr(video_rename.rename_journal, "append_event", append_event)
    monkeypatch.setattr(video_rename.rename_journal, "update_event", update_event)
    monkeypatch.setattr(video_rename.rename_journal, "mark_rolled_back", lambda _event_id: True)
    monkeypatch.setattr(video_rename, "try_inflow_upsert", lambda *_args: "synced")
    monkeypatch.setattr(video_rename, "_restore_library_path", lambda _plan: True)
    monkeypatch.setattr(video_rename, "_invalidate_thumbnails", lambda _plan: None)

    applied = video_rename.apply_video_rename(_video(video_path), {})
    new_path = Path(applied["new_path"])
    assert new_path.is_file()
    assert not folder.exists()
    assert "ABC-123 - 日本語タイトル 梓光莉-poster.jpg" in new_path.with_suffix(".nfo").read_text(
        encoding="utf-8"
    )

    rolled_back = video_rename.rollback_video_rename(events["event-1"], {})
    assert rolled_back["restored"] is True
    assert video_path.is_file()
    assert (folder / "notes.txt").is_file()
    assert (folder / "ABC-123.nfo").read_text(encoding="utf-8") == (
        "<movie><title>日本語タイトル</title><thumb>ABC-123-poster.jpg</thumb></movie>"
    )


def test_library_sync_failure_restores_original_state(media_folder, monkeypatch):
    folder, video_path = media_folder
    original_nfo = (folder / "ABC-123.nfo").read_text(encoding="utf-8")
    monkeypatch.setattr(
        video_rename.rename_journal,
        "append_event",
        lambda event: {**event, "id": "event-2"},
    )
    monkeypatch.setattr(video_rename.rename_journal, "update_event", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(video_rename, "try_inflow_upsert", lambda *_args: "failed")
    monkeypatch.setattr(video_rename, "_invalidate_thumbnails", lambda _plan: None)
    monkeypatch.setattr(video_rename.VideoRepository, "get_by_path", lambda *_args: None)

    with pytest.raises(video_rename.RenameError) as exc_info:
        video_rename.apply_video_rename(_video(video_path), {})

    assert exc_info.value.code == "library_sync_failed"
    assert video_path.is_file()
    assert (folder / "ABC-123.nfo").read_text(encoding="utf-8") == original_nfo


def test_journal_finalize_failure_keeps_completed_rename_recoverable(media_folder, monkeypatch):
    _folder, video_path = media_folder
    monkeypatch.setattr(
        video_rename.rename_journal,
        "append_event",
        lambda event: {**event, "id": "event-3"},
    )
    monkeypatch.setattr(video_rename.rename_journal, "update_event", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(video_rename, "try_inflow_upsert", lambda *_args: "synced")
    monkeypatch.setattr(video_rename, "_invalidate_thumbnails", lambda _plan: None)

    result = video_rename.apply_video_rename(_video(video_path), {})

    assert result["status"] == "completed"
    assert result["journal_status"] == "pending"
    assert result["warning"] == "journal_finalize_failed"
    assert Path(result["new_path"]).is_file()


def test_multiple_videos_are_rejected(media_folder):
    folder, video_path = media_folder
    (folder / "second.mkv").write_bytes(b"video")

    with pytest.raises(video_rename.RenameError) as exc_info:
        video_rename.plan_video_rename(_video(video_path), {})

    assert exc_info.value.code == "multiple_videos"


def test_apply_rejects_a_stale_preview_before_mutating_files(media_folder, monkeypatch):
    folder, video_path = media_folder
    journal_called = False

    def append_event(_event):
        nonlocal journal_called
        journal_called = True

    monkeypatch.setattr(video_rename.rename_journal, "append_event", append_event)

    with pytest.raises(video_rename.RenameError) as exc_info:
        video_rename.apply_video_rename(
            _video(video_path),
            {},
            expected_new_path=str(folder.parent / "different" / "different.mp4"),
        )

    assert exc_info.value.code == "preview_stale"
    assert journal_called is False
    assert video_path.is_file()


def test_rollback_rejects_when_old_and_new_videos_both_exist(media_folder):
    _folder, video_path = media_folder
    new_folder = video_path.parent.parent / "renamed"
    new_folder.mkdir()
    new_path = new_folder / "renamed.mp4"
    new_path.write_bytes(b"other-video")
    event = {
        "id": "event-collision",
        "status": "completed",
        "entries": [{
            "old_path": str(video_path),
            "new_path": str(new_path),
            "old_dir": str(video_path.parent),
            "new_dir": str(new_folder),
            "file_moves": [],
        }],
    }

    with pytest.raises(video_rename.RenameError) as exc_info:
        video_rename.rollback_video_rename(event, {})

    assert exc_info.value.code == "target_exists"
    assert video_path.read_bytes() == b"video"
    assert new_path.read_bytes() == b"other-video"
