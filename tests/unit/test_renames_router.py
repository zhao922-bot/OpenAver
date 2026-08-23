from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web.routers import renames


def _request(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_loopback_mutations_are_allowed(host):
    renames._require_loopback(_request(host))


def test_remote_mutations_are_rejected():
    with pytest.raises(HTTPException) as exc_info:
        renames._require_loopback(_request("203.0.113.20"))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "local_only"}


def test_most_specific_source_controls_writability():
    sources = [
        ("file:///D:/Videos/JAV/private", False),
        ("file:///D:/Videos/JAV", True),
    ]

    assert renames._is_writable_library_uri("file:///D:/Videos/JAV/ABC/file.mp4", sources)
    assert not renames._is_writable_library_uri(
        "file:///D:/Videos/JAV/private/ABC/file.mp4",
        sources,
    )


def test_public_history_removes_nfo_snapshot():
    event = {
        "id": "abc",
        "status": "completed",
        "entries": [{"old_path": "old", "new_path": "new", "nfo_before": "secret"}],
    }

    public = renames._public_event(event)

    assert public["entries"] == [{"old_path": "old", "new_path": "new"}]


def test_preview_reports_fixed_error_codes(monkeypatch):
    video = SimpleNamespace(path="file:///D:/Videos/JAV/ABC/file.mp4")
    repo = SimpleNamespace(get_all=lambda: [video], get_by_path=lambda _path: video)
    monkeypatch.setattr(renames, "init_db", lambda: None)
    monkeypatch.setattr(renames, "VideoRepository", lambda: repo)
    monkeypatch.setattr(
        renames,
        "_library_context",
        lambda: ([("file:///D:/Videos/JAV", True)], {}),
    )
    monkeypatch.setattr(
        renames,
        "plan_video_rename",
        lambda *_args, **_kwargs: {"renamed": True, "nfo_before": "hidden"},
    )

    result = renames.rename_preview(renames.RenameBatchRequest())

    assert result["would_rename"] == 1
    assert "nfo_before" not in result["results"][0]
