"""Find local-library videos whose still-image files are actually missing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.config import get_gallery_source_paths, load_config
from core.database import VideoRepository, init_db
from core.path_utils import (
    coerce_to_file_uri,
    is_path_under_dir,
    uri_to_local_fs_path,
)


router = APIRouter(prefix="/api/sample-batches", tags=["sample-batches"])
SAMPLE_SCAN_LIMIT = 500


class MissingSamplesRequest(BaseModel):
    paths: list[Annotated[str, Field(max_length=4000)]] = Field(
        default_factory=list,
        max_length=SAMPLE_SCAN_LIMIT,
    )


def _configured_library(config: dict) -> tuple[set[str], dict]:
    gallery = config.get("gallery", {})
    mappings = gallery.get("path_mappings", {})
    roots: set[str] = set()
    for value in get_gallery_source_paths(gallery):
        try:
            roots.add(coerce_to_file_uri(value, mappings))
        except ValueError:
            continue
    return roots, mappings


def _has_existing_samples(video, path_mappings: dict) -> bool:
    for uri in video.sample_images or []:
        try:
            fs_path = uri_to_local_fs_path(uri, path_mappings)
            if fs_path and Path(fs_path).is_file():
                return True
        except (OSError, ValueError):
            continue
    return False


@router.post("/missing")
def missing_samples(payload: MissingSamplesRequest) -> dict:
    init_db()
    config = load_config()
    roots, mappings = _configured_library(config)
    repo = VideoRepository()
    if payload.paths:
        videos = [repo.get_by_path(path) for path in payload.paths]
    else:
        videos = repo.get_all()[:SAMPLE_SCAN_LIMIT]

    missing = []
    ineligible = 0
    for video in videos:
        if video is None or not any(is_path_under_dir(video.path, root) for root in roots):
            ineligible += 1
            continue
        if _has_existing_samples(video, mappings):
            continue
        if not (video.number or "").strip():
            ineligible += 1
            continue
        missing.append({
            "path": video.path,
            "number": video.number,
            "title": video.title or video.original_title or "",
            "stale_entries": len(video.sample_images or []),
        })
    return {
        "success": True,
        "items": missing,
        "missing": len(missing),
        "ineligible": ineligible,
        "scanned": len(videos),
    }
