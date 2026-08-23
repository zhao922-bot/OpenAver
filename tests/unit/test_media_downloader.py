from contextlib import nullcontext
from pathlib import Path

import pytest

import core.media_downloader as downloader


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> downloader.MediaDownloadManager:
    monkeypatch.setattr(downloader, "load_config", lambda: {"download": {}})
    return downloader.MediaDownloadManager(tmp_path / "tasks.json")


def test_safe_url_for_display_removes_credentials_query_and_fragment():
    value = downloader._safe_url_for_display(
        "https://user:password@cdn.example:8443/video.m3u8?token=secret#chapter"
    )
    assert value == "https://cdn.example:8443/video.m3u8"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://source.example/title/ABC-123", "https://source.example/title/ABC-123"),
        ("file:///tmp/source", ""),
        ("https://source.example/title\r\nX-Test: injected", ""),
        ("https://user:secret@source.example/title", ""),
    ],
)
def test_referer_must_be_a_header_safe_http_url(value, expected):
    assert downloader._validated_referer(value) == expected


def test_private_network_target_is_blocked(monkeypatch):
    monkeypatch.setattr(
        downloader.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )

    with pytest.raises(downloader.DownloadValidationError) as exc_info:
        downloader._validate_network_target("https://example.test/video.m3u8")

    assert exc_info.value.code == "blocked_target"


def test_probe_revalidates_every_redirect(monkeypatch):
    validated = []

    def fake_validate(url, **_kwargs):
        validated.append(url)

    class FakeResponse:
        def __init__(self, status, headers, body=b""):
            self.status_code = status
            self.headers = headers
            self._body = body

        def iter_bytes(self):
            yield self._body

    class FakeClient:
        def __init__(self, **_kwargs):
            self.responses = iter(
                [
                    FakeResponse(302, {"location": "https://cdn.example/master.m3u8"}),
                    FakeResponse(
                        200,
                        {"content-type": "application/vnd.apple.mpegurl"},
                        b"#EXTM3U\n#EXT-X-VERSION:3\n",
                    ),
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return nullcontext(next(self.responses))

    monkeypatch.setattr(downloader, "_validate_network_target", fake_validate)
    monkeypatch.setattr(downloader.httpx, "Client", FakeClient)

    result = downloader.validate_direct_media_url("https://origin.example/video")

    assert result["kind"] == "hls"
    assert validated == [
        "https://origin.example/video",
        "https://cdn.example/master.m3u8",
    ]


def test_create_has_no_chinese_title_and_redacts_public_url(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "_launch", lambda _task_id: None)

    task = manager.create(
        {
            "number": "abc-123",
            "title": "Japanese title",
            "media_url": "https://cdn.example/video.m3u8?token=secret",
            "source_page_url": "https://source.example/title?session=secret",
            "cover": "https://img.example/cover.jpg?token=secret",
            "destination": str(tmp_path),
        }
    )

    assert task["payload"]["number"] == "ABC-123"
    assert task["payload"]["title"] == "Japanese title"
    assert "chinese_title" not in task["payload"]
    assert task["payload"]["media_url"] == "https://cdn.example/video.m3u8"
    assert task["payload"]["source_page_url"] == "https://source.example/title"
    assert task["payload"]["cover"] == "https://img.example/cover.jpg"


def test_invalid_download_settings_fall_back_to_bounded_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(
        downloader,
        "load_config",
        lambda: {
            "download": {
                "max_concurrent_downloads": "bad",
                "fragment_threads": 999,
                "retry_count": -5,
                "request_timeout_seconds": 9999,
            }
        },
    )

    manager = downloader.MediaDownloadManager(tmp_path / "tasks.json")

    assert manager._settings == {
        "max_concurrent_downloads": 4,
        "fragment_threads": 64,
        "retry_count": 0,
        "request_timeout_seconds": 300,
    }


def test_library_import_uses_authoritative_inflow_path(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    output_path = tmp_path / "ABC-123 Japanese title.mp4"
    output_path.write_bytes(b"video")
    nfo_calls = []
    inflow_calls = []
    monkeypatch.setattr(downloader, "download_image", lambda *_args, **_kwargs: True)

    def fake_generate_nfo(**kwargs):
        nfo_calls.append(kwargs)
        return True

    def fake_inflow(path):
        inflow_calls.append(path)
        return "synced"

    monkeypatch.setattr(downloader, "generate_nfo", fake_generate_nfo)
    monkeypatch.setattr(downloader, "try_inflow_upsert", fake_inflow)

    warnings = manager._write_assets_and_import(
        {"number": "ABC-123", "title": "Japanese title"},
        output_path,
        600,
    )

    assert warnings == []
    assert nfo_calls[0]["title"] == "Japanese title"
    assert nfo_calls[0]["original_title"] == "Japanese title"
    assert inflow_calls == [str(output_path)]
