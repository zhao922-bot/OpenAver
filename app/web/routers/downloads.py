"""Authorized direct-media downloads and public Jable title metadata."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl

from core.cf_transport import CfChallengeRequired, CfTransportUnavailable, get_cf_transport
from core.config import load_config
from core.jable_metadata import JABLE_ORIGIN, lookup_jable_titles
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


@router.get("/jable-titles")
def jable_titles(request: Request, number: str = Query(min_length=2, max_length=64)) -> dict:
    _local_only(request)
    normalized = number.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]+", normalized):
        raise HTTPException(status_code=400, detail="Invalid catalog number")
    try:
        return {"success": True, "data": lookup_jable_titles(normalized)}
    except CfChallengeRequired:
        transport = get_cf_transport()
        if transport is None:
            raise HTTPException(status_code=503, detail={"reason": "cf_unavailable"})
        try:
            transport.begin_solve(JABLE_ORIGIN, "jable")
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"reason": "cf_unavailable"}) from exc
        raise HTTPException(status_code=409, detail={"reason": "cf_challenge"})
    except CfTransportUnavailable as exc:
        raise HTTPException(status_code=503, detail={"reason": "cf_unavailable"}) from exc


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
    if action not in {"pause", "resume", "cancel"}:
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
