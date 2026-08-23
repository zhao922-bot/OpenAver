"""Safe preview/apply/rollback APIs for metadata-based media renames."""

from __future__ import annotations

import threading
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core import rename_journal
from core.config import iter_gallery_sources, load_config
from core.database import VideoRepository, init_db
from core.path_utils import coerce_to_file_uri, is_path_under_dir
from core.video_rename import (
    RenameError,
    apply_video_rename,
    plan_video_rename,
    rollback_video_rename,
)


router = APIRouter(prefix="/api/renames", tags=["renames"])
_operation_lock = threading.Lock()
RENAME_BATCH_LIMIT = 50


class RenameBatchRequest(BaseModel):
    paths: list[Annotated[str, Field(max_length=4000)]] = Field(
        default_factory=list,
        max_length=RENAME_BATCH_LIMIT,
    )
    rename_folder: bool = True
    expected_new_paths: dict[str, str] = Field(default_factory=dict, max_length=RENAME_BATCH_LIMIT)


class RenameRollbackRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def _client_error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _require_loopback(request: Request) -> None:
    from web.app import _is_loopback_host  # noqa: PLC2701 -- shared loopback decision owner

    host = request.client.host if request.client else ""
    if not _is_loopback_host(host):
        raise _client_error(403, "local_only")


def _library_context() -> tuple[list[tuple[str, bool]], dict]:
    config = load_config()
    gallery = config.get("gallery", {})
    mappings = gallery.get("path_mappings", {})
    sources: list[tuple[str, bool]] = []
    for source in iter_gallery_sources(gallery):
        try:
            uri = coerce_to_file_uri(source.path, mappings).rstrip("/")
        except ValueError:
            continue
        sources.append((uri, not source.readonly))
    sources.sort(key=lambda item: len(item[0]), reverse=True)
    return sources, mappings


def _is_writable_library_uri(uri: str, sources: list[tuple[str, bool]]) -> bool:
    match = next((item for item in sources if is_path_under_dir(uri, item[0])), None)
    return bool(match and match[1])


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "nfo_before"}


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    public = {key: value for key, value in event.items() if key != "entries"}
    public["entries"] = [_public_plan(item) for item in event.get("entries") or []]
    return public


def _selected_videos(paths: list[str], repo: VideoRepository) -> list[Any]:
    if paths:
        return [repo.get_by_path(path) for path in paths]
    return repo.get_all()[:RENAME_BATCH_LIMIT]


@router.post("/preview")
def rename_preview(payload: RenameBatchRequest) -> dict:
    init_db()
    sources, mappings = _library_context()
    repo = VideoRepository()
    requested = payload.paths or [video.path for video in repo.get_all()]
    truncated = len(requested) > RENAME_BATCH_LIMIT
    requested = requested[:RENAME_BATCH_LIMIT]
    results: list[dict[str, Any]] = []
    for path in requested:
        if not _is_writable_library_uri(path, sources):
            results.append({"path": path, "success": False, "error": "not_writable"})
            continue
        video = repo.get_by_path(path)
        if video is None:
            results.append({"path": path, "success": False, "error": "not_found"})
            continue
        try:
            plan = plan_video_rename(video, mappings, rename_folder=payload.rename_folder)
            results.append({"path": path, "success": True, **_public_plan(plan)})
        except RenameError as exc:
            results.append({"path": path, "success": False, "error": exc.code})
    return {
        "success": True,
        "dry_run": True,
        "requested": len(requested),
        "truncated": truncated,
        "would_rename": sum(item.get("renamed", False) for item in results),
        "results": results,
    }


@router.post("/apply")
def rename_apply(payload: RenameBatchRequest, request: Request) -> dict:
    _require_loopback(request)
    if not payload.paths:
        raise _client_error(400, "paths_required")
    if not _operation_lock.acquire(blocking=False):
        raise _client_error(409, "operation_busy")
    try:
        init_db()
        sources, mappings = _library_context()
        repo = VideoRepository()
        results: list[dict[str, Any]] = []
        for path in payload.paths:
            if not _is_writable_library_uri(path, sources):
                results.append({"path": path, "success": False, "error": "not_writable"})
                continue
            video = repo.get_by_path(path)
            if video is None:
                results.append({"path": path, "success": False, "error": "not_found"})
                continue
            try:
                result = apply_video_rename(
                    video,
                    mappings,
                    rename_folder=payload.rename_folder,
                    expected_new_path=payload.expected_new_paths.get(path),
                )
                results.append({"path": path, "success": True, **_public_plan(result)})
            except RenameError as exc:
                results.append({"path": path, "success": False, "error": exc.code})
        failed = sum(not item["success"] for item in results)
        return {
            "success": failed == 0,
            "renamed": sum(item.get("renamed", False) for item in results),
            "failed": failed,
            "results": results,
        }
    finally:
        _operation_lock.release()


@router.get("/history")
def rename_history(limit: int = 30) -> dict:
    return {
        "success": True,
        "items": [_public_event(item) for item in rename_journal.list_events(limit)],
    }


@router.post("/rollback")
def rename_rollback(payload: RenameRollbackRequest, request: Request) -> dict:
    _require_loopback(request)
    if not _operation_lock.acquire(blocking=False):
        raise _client_error(409, "operation_busy")
    try:
        event = rename_journal.get_event(payload.event_id)
        if event is None:
            raise _client_error(404, "event_not_found")
        sources, mappings = _library_context()
        entries = event.get("entries") or []
        if len(entries) != 1:
            raise _client_error(409, "invalid_journal")
        plan = entries[0]
        if not all(
            _is_writable_library_uri(plan.get(key, ""), sources)
            for key in ("old_uri", "new_uri")
        ):
            raise _client_error(403, "not_writable")
        try:
            result = rollback_video_rename(event, mappings)
        except RenameError as exc:
            raise _client_error(409, exc.code) from None
        return {"success": True, "event_id": payload.event_id, **result}
    finally:
        _operation_lock.release()


@router.post("/recover-pending")
def recover_pending(request: Request) -> dict:
    _require_loopback(request)
    if not _operation_lock.acquire(blocking=False):
        raise _client_error(409, "operation_busy")
    try:
        sources, mappings = _library_context()
        results: list[dict[str, Any]] = []
        pending = [
            event for event in rename_journal.list_events(200)
            if event.get("status") in {"pending", "recovery_required"}
        ]
        for event in pending:
            entries = event.get("entries") or []
            if len(entries) != 1 or not all(
                _is_writable_library_uri(entries[0].get(key, ""), sources)
                for key in ("old_uri", "new_uri")
            ):
                results.append({"event_id": event.get("id"), "success": False, "error": "not_writable"})
                continue
            try:
                result = rollback_video_rename(event, mappings)
                results.append({"event_id": event.get("id"), "success": True, **result})
            except RenameError as exc:
                results.append({"event_id": event.get("id"), "success": False, "error": exc.code})
        return {
            "success": all(item["success"] for item in results),
            "recovered": sum(item["success"] for item in results),
            "failed": sum(not item["success"] for item in results),
            "results": results,
        }
    finally:
        _operation_lock.release()
