"""API for user-authorized direct media downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl

from core.config import iter_gallery_sources, load_config
from core.media_downloader import DownloadValidationError, get_media_download_manager
from core.path_utils import uri_to_local_fs_path


router = APIRouter(prefix="/api/downloads", tags=["downloads"])


class DownloadRequest(BaseModel):
    media_url: HttpUrl
    destination: str = Field(default="", max_length=2000)
    number: str = Field(min_length=2, max_length=64)
    title: str = Field(default="", max_length=500)
    actors: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=20)
    tags: list[Annotated[str, Field(max_length=100)]] = Field(default_factory=list, max_length=50)
    maker: str = Field(default="", max_length=150)
    date: str = Field(default="", max_length=20)
    director: str = Field(default="", max_length=150)
    series: str = Field(default="", max_length=200)
    label: str = Field(default="", max_length=150)
    cover: str = Field(default="", max_length=2000)
    source_page_url: str = Field(default="", max_length=2000)
    rights_confirmed: bool = False


class DownloadSettingsRequest(BaseModel):
    max_concurrent_downloads: int = Field(ge=1, le=8)
    fragment_threads: int = Field(ge=1, le=64)


class RetryWithUrlRequest(BaseModel):
    media_url: str = Field(default="", max_length=4000)


def _client_error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _require_loopback(request: Request) -> None:
    from web.app import _is_loopback_host  # noqa: PLC2701 -- shared loopback decision owner

    host = request.client.host if request.client else ""
    if not _is_loopback_host(host):
        raise _client_error(403, "local_only")


def _destinations() -> list[str]:
    config = load_config()
    gallery = config.get("gallery", {})
    mappings = gallery.get("path_mappings", {})
    result: list[str] = []
    seen: set[str] = set()
    for source in iter_gallery_sources(gallery):
        if source.readonly:
            continue
        try:
            local = Path(uri_to_local_fs_path(source.path, mappings)).resolve()
        except (OSError, ValueError):
            continue
        key = str(local).casefold()
        if local.is_dir() and key not in seen:
            seen.add(key)
            result.append(str(local))
    return result


def _selected_destination(value: str, allowed: list[str]) -> str:
    if not allowed:
        raise _client_error(409, "no_destination")
    if not value:
        return allowed[0]
    try:
        selected = str(Path(value).resolve())
    except (OSError, ValueError):
        raise _client_error(400, "invalid_destination") from None
    match = next((item for item in allowed if item.casefold() == selected.casefold()), "")
    if not match:
        raise _client_error(400, "invalid_destination")
    return match


@router.get("")
def list_downloads(request: Request) -> dict:
    _require_loopback(request)
    return {"success": True, "items": get_media_download_manager().list()}


@router.get("/destinations")
def download_destinations(request: Request) -> dict:
    _require_loopback(request)
    return {"success": True, "items": _destinations()}


@router.get("/settings")
def download_settings(request: Request) -> dict:
    _require_loopback(request)
    return {"success": True, "settings": get_media_download_manager().settings()}


@router.put("/settings")
def update_download_settings(payload: DownloadSettingsRequest, request: Request) -> dict:
    _require_loopback(request)
    settings = get_media_download_manager().configure(
        max_concurrent_downloads=payload.max_concurrent_downloads,
        fragment_threads=payload.fragment_threads,
    )
    return {"success": True, "settings": settings}


@router.post("")
def create_download(payload: DownloadRequest, request: Request) -> dict:
    _require_loopback(request)
    if not payload.rights_confirmed:
        raise _client_error(400, "rights_required")
    destinations = _destinations()
    data = payload.model_dump(mode="json")
    data["media_url"] = str(payload.media_url)
    data["destination"] = _selected_destination(payload.destination, destinations)
    data.pop("rights_confirmed", None)
    try:
        task = get_media_download_manager().create(data)
    except DownloadValidationError as exc:
        raise _client_error(409, exc.code) from None
    return {"success": True, "task": task}


@router.post("/{task_id}/control/{action}")
def control_download(task_id: str, action: str, request: Request) -> dict:
    _require_loopback(request)
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise _client_error(404, "unknown_action")
    try:
        manager = get_media_download_manager()
        existing = manager.get(task_id)
        if existing is None:
            raise KeyError(task_id)
        if action in {"resume", "retry"}:
            _selected_destination(existing.get("payload", {}).get("destination", ""), _destinations())
        task = manager.control(task_id, action)
    except KeyError:
        raise _client_error(404, "task_not_found") from None
    except (DownloadValidationError, OSError) as exc:
        code = exc.code if isinstance(exc, DownloadValidationError) else "control_failed"
        raise _client_error(409, code) from None
    return {"success": True, "task": task}


@router.post("/{task_id}/retry-with-url")
def retry_download_with_url(task_id: str, payload: RetryWithUrlRequest, request: Request) -> dict:
    _require_loopback(request)
    try:
        manager = get_media_download_manager()
        existing = manager.get(task_id)
        if existing is None:
            raise KeyError(task_id)
        _selected_destination(existing.get("payload", {}).get("destination", ""), _destinations())
        if payload.media_url.strip():
            manager.update_media_url(task_id, payload.media_url)
        task = manager.control(task_id, "retry")
    except KeyError:
        raise _client_error(404, "task_not_found") from None
    except DownloadValidationError as exc:
        raise _client_error(409, exc.code) from None
    return {"success": True, "task": task}


@router.delete("/history")
def clear_download_history(request: Request, only_failed: bool = False) -> dict:
    _require_loopback(request)
    removed = get_media_download_manager().clear_history(only_failed=only_failed)
    return {"success": True, "removed": removed}


@router.delete("/{task_id}")
def remove_download(task_id: str, request: Request) -> dict:
    _require_loopback(request)
    try:
        get_media_download_manager().remove(task_id)
    except KeyError:
        raise _client_error(404, "task_not_found") from None
    except DownloadValidationError as exc:
        raise _client_error(409, exc.code) from None
    return {"success": True}
