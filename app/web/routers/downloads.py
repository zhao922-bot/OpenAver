"""Authorized direct-media downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl

from core.config import load_config
from core.media_downloader import DownloadValidationError, media_download_manager


router = APIRouter(prefix="/api/downloads", tags=["downloads"])


class DownloadRequest(BaseModel):
    media_url: HttpUrl
    destination: str = ""
    number: str = Field(min_length=2, max_length=64)
    title: str = Field(default="", max_length=500)
    chinese_title: str = Field(default="", max_length=500)
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


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Download creation is local-only")


def _destinations() -> list[str]:
    result = []
    for item in load_config().get("gallery", {}).get("directories", []):
        if isinstance(item, str):
            path, readonly = item, False
        else:
            path, readonly = item.get("path", ""), bool(item.get("readonly"))
        if path and not readonly and Path(path).is_dir():
            result.append(str(Path(path).resolve()))
    return result


@router.get("")
def list_downloads() -> dict:
    return {"success": True, "items": media_download_manager.list()}


@router.get("/destinations")
def download_destinations() -> dict:
    return {"success": True, "items": _destinations()}


@router.get("/settings")
def download_settings() -> dict:
    return {"success": True, "settings": media_download_manager.settings()}


@router.put("/settings")
def update_download_settings(payload: DownloadSettingsRequest, request: Request) -> dict:
    _local_only(request)
    settings = media_download_manager.configure(
        max_concurrent_downloads=payload.max_concurrent_downloads,
        fragment_threads=payload.fragment_threads,
    )
    return {"success": True, "settings": settings}


@router.post("")
def create_download(payload: DownloadRequest, request: Request) -> dict:
    _local_only(request)
    if not payload.rights_confirmed:
        raise HTTPException(status_code=400, detail="Usage rights must be confirmed")
    destinations = _destinations()
    if not destinations:
        raise HTTPException(status_code=409, detail="No writable scan folder is configured")
    destination = str(Path(payload.destination).resolve()) if payload.destination else destinations[0]
    if destination not in destinations:
        raise HTTPException(status_code=400, detail="Destination must be a configured writable scan folder")
    data = payload.model_dump(mode="json")
    data["media_url"] = str(payload.media_url)
    data["destination"] = destination
    data.pop("rights_confirmed", None)
    try:
        return {"success": True, "task": media_download_manager.create(data)}
    except DownloadValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/{action}")
def control_download(task_id: str, action: str, request: Request) -> dict:
    _local_only(request)
    if action not in {"pause", "resume", "cancel", "retry"}:
        raise HTTPException(status_code=404, detail="Unknown action")
    try:
        return {"success": True, "task": media_download_manager.control(task_id, action)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Download not found")
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{task_id}")
def remove_download(task_id: str, request: Request) -> dict:
    _local_only(request)
    try:
        media_download_manager.remove(task_id)
        return {"success": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Download not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
