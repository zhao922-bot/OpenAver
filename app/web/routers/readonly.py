from dataclasses import asdict
from pathlib import Path
from typing import Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import DirectoryConfig, iter_gallery_sources, load_config
from core.database import VideoRepository
from core.gallery_scanner import fast_scan_directory
from core.path_utils import uri_to_fs_path
from core.readonly_producer import produce_source
from core.scraper import extract_number
from core.video_extensions import get_video_extensions


router = APIRouter(prefix="/api/readonly", tags=["readonly"])


class ReadonlyProduceRequest(BaseModel):
    source_path: str
    output_path: str = ""
    external_manager: Optional[Literal["off", "jellyfin", "emby", "kodi"]] = None
    proxy_url: str = ""
    force: bool = False
    strm_path_mappings: Optional[Dict[str, str]] = None


@router.get("/sources")
def list_readonly_sources() -> dict:
    config = load_config()
    sources = [
        s.model_dump()
        for s in iter_gallery_sources(config.get("gallery", {}))
        if s.readonly
    ]
    return {"success": True, "sources": sources}


@router.post("/produce")
def produce_readonly_source(req: ReadonlyProduceRequest) -> dict:
    config = load_config()
    scraper = dict(config.get("scraper", {}))

    if req.external_manager is not None:
        scraper["external_manager"] = req.external_manager
    if req.strm_path_mappings is not None:
        scraper["strm_path_mappings"] = req.strm_path_mappings
    config["scraper"] = scraper

    external_manager = scraper.get("external_manager", "off")
    if external_manager in {"jellyfin", "emby", "kodi"} and not req.output_path.strip():
        raise HTTPException(
            status_code=400,
            detail="jellyfin/emby/kodi 模式需要 output_path，用于生成媒体库目录和 .strm",
        )

    source = DirectoryConfig(
        path=req.source_path,
        readonly=True,
        output_path=req.output_path,
    )
    repo = VideoRepository()

    try:
        reachable = Path(uri_to_fs_path(req.source_path)).exists()
    except Exception:
        reachable = False

    result = produce_source(
        source,
        config,
        repo,
        proxy_url=req.proxy_url or config.get("search", {}).get("proxy_url", ""),
        force=req.force,
        reachable=reachable,
        strm_mappings_getter=(
            (lambda: req.strm_path_mappings)
            if req.strm_path_mappings is not None
            else None
        ),
    )

    return {
        "success": not bool(result.aborted_reason),
        "result": asdict(result),
    }


@router.post("/dry-run")
def dry_run_readonly_source(req: ReadonlyProduceRequest) -> dict:
    config = load_config()
    source_fs = uri_to_fs_path(req.source_path)
    source_dir = Path(source_fs)
    if not source_dir.exists():
        return {
            "success": False,
            "aborted_reason": "unreachable",
            "files": [],
            "total": 0,
        }

    gallery = config.get("gallery", {})
    min_size_bytes = int(gallery.get("min_size_mb", 0)) * 1024 * 1024
    files = fast_scan_directory(
        str(source_dir),
        get_video_extensions(config),
        min_size_bytes,
    )
    preview = [
        {
            "path": item["path"],
            "number": extract_number(Path(item["path"]).name),
            "size": item.get("size", 0),
            "mtime": item.get("mtime", 0),
        }
        for item in files
    ]
    return {"success": True, "files": preview, "total": len(preview)}
