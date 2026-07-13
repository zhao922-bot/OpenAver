"""Library issue dashboard, repair actions, and actress-name provenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import load_config
from core.database import AliasRepository, VideoRepository, get_db_path, init_db
from core.enricher import enrich_single
from core.path_utils import uri_to_fs_path


router = APIRouter(prefix="/api/operations", tags=["operations"])


def _metadata_audit(video) -> dict:
    checks = {
        "title": bool((video.original_title or video.title or "").strip()),
        "actresses": bool(video.actresses),
        "maker": bool((video.maker or "").strip()),
        "release_date": bool((video.release_date or "").strip()),
        "tags": bool(video.tags),
        "cover": bool(video.cover_path and Path(uri_to_fs_path(video.cover_path)).exists()),
        "nfo": Path(uri_to_fs_path(video.path)).with_suffix(".nfo").exists(),
        "video": Path(uri_to_fs_path(video.path)).exists(),
    }
    present = sum(checks.values())
    return {
        "path": video.path,
        "number": video.number or "",
        "title": video.original_title or video.title or "",
        "actresses": video.actresses or [],
        "score": round(present / len(checks), 3),
        "confidence": "high" if present >= 7 else ("medium" if present >= 5 else "low"),
        "missing": [name for name, ok in checks.items() if not ok],
    }


@router.get("/dashboard")
def dashboard(limit: int = 100) -> dict:
    init_db()
    videos = VideoRepository().get_all()
    audited = [_metadata_audit(video) for video in videos]
    unresolved = [item for item in audited if item["missing"]]
    unresolved.sort(key=lambda item: (item["score"], item["number"]))
    return {
        "success": True,
        "summary": {
            "total": len(audited),
            "unresolved": len(unresolved),
            "high_confidence": sum(item["confidence"] == "high" for item in audited),
            "medium_confidence": sum(item["confidence"] == "medium" for item in audited),
            "low_confidence": sum(item["confidence"] == "low" for item in audited),
        },
        "items": unresolved[:max(1, min(limit, 500))],
    }


class RepairRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=20)
    source: str = "auto"


@router.post("/repair")
def repair(payload: RepairRequest) -> dict:
    config = load_config()
    proxy_url = config.get("search", {}).get("proxy_url", "")
    external = config.get("scraper", {}).get("external_manager", "off")
    repo = VideoRepository()
    results = []
    for path in payload.paths:
        video = repo.get_by_path(path)
        if not video:
            results.append({"path": path, "success": False, "error": "not_found"})
            continue
        result = enrich_single(
            file_path=path,
            number=video.number or "",
            mode="fill_missing",
            write_nfo=True,
            write_cover=True,
            write_extrafanart=False,
            overwrite_existing=False,
            external_manager=external,
            proxy_url=proxy_url,
            source=None if payload.source == "auto" else payload.source,
        )
        results.append({"path": path, "success": result.success, "error": result.error})
    return {
        "success": all(item["success"] for item in results),
        "updated": sum(item["success"] for item in results),
        "failed": sum(not item["success"] for item in results),
        "results": results,
    }


class EvidenceRequest(BaseModel):
    source_url: str = ""
    confidence: float = Field(0.8, ge=0, le=1)
    verified: bool = False
    notes: str = ""


@router.get("/actress-names")
def actress_name_audit() -> dict:
    init_db()
    groups = AliasRepository().get_all()
    usage = {}
    for video in VideoRepository().get_all():
        for name in video.actresses or []:
            usage[name] = usage.get(name, 0) + 1
    with sqlite3.connect(str(get_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        evidence = {
            row["primary_name"]: dict(row)
            for row in conn.execute("SELECT * FROM actress_alias_evidence")
        }
    items = []
    for group in groups:
        item = {
            "primary_name": group.primary_name,
            "aliases": group.aliases,
            "source": group.source,
            "usage_count": sum(usage.get(name, 0) for name in [group.primary_name, *group.aliases]),
            "evidence": evidence.get(group.primary_name),
        }
        items.append(item)
    items.sort(key=lambda item: (bool(item["evidence"] and item["evidence"]["verified"]), -item["usage_count"]))
    return {"success": True, "items": items, "unverified": sum(not (i["evidence"] and i["evidence"]["verified"]) for i in items)}


@router.put("/actress-names/{primary_name}/evidence")
def save_actress_evidence(primary_name: str, payload: EvidenceRequest) -> dict:
    init_db()
    if not AliasRepository().get_by_primary(primary_name):
        raise HTTPException(status_code=404, detail="Alias group not found")
    with sqlite3.connect(str(get_db_path())) as conn:
        conn.execute(
            """INSERT INTO actress_alias_evidence
               (primary_name, source_url, confidence, verified, notes, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(primary_name) DO UPDATE SET
                 source_url=excluded.source_url,
                 confidence=excluded.confidence,
                 verified=excluded.verified,
                 notes=excluded.notes,
                 updated_at=CURRENT_TIMESTAMP""",
            (primary_name, payload.source_url, payload.confidence, int(payload.verified), payload.notes),
        )
    return {"success": True}
