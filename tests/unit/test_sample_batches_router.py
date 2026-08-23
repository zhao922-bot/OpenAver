from types import SimpleNamespace

from core.path_utils import to_file_uri
from web.routers import sample_batches


def test_existing_sample_file_is_not_missing(tmp_path):
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"image")
    video = SimpleNamespace(sample_images=[to_file_uri(str(image))])

    assert sample_batches._has_existing_samples(video, {}) is True


def test_stale_sample_database_entry_is_missing(tmp_path):
    missing = tmp_path / "deleted.jpg"
    video = SimpleNamespace(sample_images=[to_file_uri(str(missing))])

    assert sample_batches._has_existing_samples(video, {}) is False


def test_missing_scan_filters_scope_and_number(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    eligible = SimpleNamespace(
        path=to_file_uri(str(root / "ABC-123.mp4")),
        number="ABC-123",
        title="Title",
        original_title="",
        sample_images=[],
    )
    no_number = SimpleNamespace(
        path=to_file_uri(str(root / "unknown.mp4")),
        number="",
        title="Unknown",
        original_title="",
        sample_images=[],
    )
    outside = SimpleNamespace(
        path="file:///Z:/outside/DEF-456.mp4",
        number="DEF-456",
        title="Outside",
        original_title="",
        sample_images=[],
    )
    videos = {video.path: video for video in (eligible, no_number, outside)}
    repo = SimpleNamespace(
        get_by_path=lambda path: videos.get(path),
        get_all=lambda: list(videos.values()),
    )
    monkeypatch.setattr(sample_batches, "init_db", lambda: None)
    monkeypatch.setattr(sample_batches, "VideoRepository", lambda: repo)
    monkeypatch.setattr(
        sample_batches,
        "load_config",
        lambda: {"gallery": {"directories": [str(root)], "path_mappings": {}}},
    )

    result = sample_batches.missing_samples(sample_batches.MissingSamplesRequest())

    assert result["missing"] == 1
    assert result["items"][0]["number"] == "ABC-123"
    assert result["ineligible"] == 2


def test_missing_scan_reports_stale_entry_count(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    stale_uri = to_file_uri(str(root / "deleted-1.jpg"))
    video = SimpleNamespace(
        path=to_file_uri(str(root / "ABC-123.mp4")),
        number="ABC-123",
        title="Title",
        original_title="",
        sample_images=[stale_uri],
    )
    repo = SimpleNamespace(get_by_path=lambda _path: video, get_all=lambda: [video])
    monkeypatch.setattr(sample_batches, "init_db", lambda: None)
    monkeypatch.setattr(sample_batches, "VideoRepository", lambda: repo)
    monkeypatch.setattr(
        sample_batches,
        "load_config",
        lambda: {"gallery": {"directories": [str(root)], "path_mappings": {}}},
    )

    result = sample_batches.missing_samples(
        sample_batches.MissingSamplesRequest(paths=[video.path])
    )

    assert result["items"][0]["stale_entries"] == 1
