from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from web.routers import downloads


def _request(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_download_request_does_not_accept_chinese_title():
    assert "chinese_title" not in downloads.DownloadRequest.model_fields


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_loopback_mutations_are_allowed(host):
    downloads._require_loopback(_request(host))


def test_remote_mutations_are_rejected():
    with pytest.raises(HTTPException) as exc_info:
        downloads._require_loopback(_request("203.0.113.10"))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "local_only"}


def test_remote_queue_reads_are_rejected():
    with pytest.raises(HTTPException) as exc_info:
        downloads.list_downloads(_request("203.0.113.10"))
    assert exc_info.value.status_code == 403


def test_destination_must_be_an_exact_configured_root(tmp_path):
    root = tmp_path / "library"
    child = root / "child"
    child.mkdir(parents=True)

    assert downloads._selected_destination(str(root), [str(root)]) == str(root)
    with pytest.raises(HTTPException) as exc_info:
        downloads._selected_destination(str(child), [str(root)])
    assert exc_info.value.detail == {"code": "invalid_destination"}


def test_destinations_exclude_readonly_sources(tmp_path, monkeypatch):
    writable = tmp_path / "writable"
    readonly = tmp_path / "readonly"
    writable.mkdir()
    readonly.mkdir()
    sources = [
        SimpleNamespace(path=str(writable), readonly=False),
        SimpleNamespace(path=str(readonly), readonly=True),
    ]
    monkeypatch.setattr(downloads, "load_config", lambda: {"gallery": {}})
    monkeypatch.setattr(downloads, "iter_gallery_sources", lambda _gallery: sources)
    monkeypatch.setattr(downloads, "uri_to_local_fs_path", lambda value, _mappings: value)

    assert downloads._destinations() == [str(Path(writable).resolve())]
