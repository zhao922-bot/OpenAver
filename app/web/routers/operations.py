"""Library issue dashboard, repair actions, rename preview/apply/rollback, actress-name provenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import load_config
from core.database import AliasRepository, VideoRepository, get_db_path, init_db
from core.enricher import enrich_single
from core.logger import get_logger
from core.path_utils import uri_to_fs_path
from core import rename_journal, thumbnail_cache

logger = get_logger(__name__)

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


@router.get("/actress-alias-review")
def actress_alias_review(limit: int = 80) -> dict:
    """Review queues: unrecognized names, multi-Chinese groups, merge candidates."""
    from core.actress_alias_review import review_actress_aliases
    return review_actress_aliases(limit=max(1, min(limit, 300)))


class AliasMergeRequest(BaseModel):
    keep: str
    absorb: str


@router.post("/actress-alias-merge")
def actress_alias_merge(payload: AliasMergeRequest) -> dict:
    """Merge absorb alias group into keep (batch-friendly single pair)."""
    init_db()
    try:
        record = AliasRepository().merge_groups(payload.keep.strip(), payload.absorb.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "success": True,
        "group": {
            "primary_name": record.primary_name,
            "aliases": record.aliases,
            "source": record.source,
        },
    }


class AliasAttachRequest(BaseModel):
    name: str
    attach_to: str


@router.post("/actress-alias-attach")
def actress_alias_attach(payload: AliasAttachRequest) -> dict:
    """Attach an unrecognized library name as alias of an existing group."""
    init_db()
    name = payload.name.strip()
    attach_to = payload.attach_to.strip()
    if not name or not attach_to:
        raise HTTPException(status_code=400, detail="name and attach_to required")
    repo = AliasRepository()
    group = repo.get_by_primary(attach_to) or repo.find_by_alias(attach_to)
    if not group:
        raise HTTPException(status_code=404, detail="target group not found")
    ok, err = repo.add_alias(group.primary_name, name)
    if not ok:
        raise HTTPException(status_code=400, detail=err or "attach failed")
    updated = repo.get_by_primary(group.primary_name)
    return {
        "success": True,
        "group": {
            "primary_name": updated.primary_name if updated else group.primary_name,
            "aliases": updated.aliases if updated else group.aliases,
        },
    }


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


# ── Rename preview / apply / history / rollback ─────────────────────────────


class RenameBatchRequest(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=50)
    rename_folder: bool = True
    dry_run: bool = True


@router.get("/duplicates")
def list_duplicates(
    compute_hash: bool = False,
    limit: int = 50,
) -> dict:
    """Detect duplicate videos by number, size, and optional content fingerprint."""
    from core.duplicates import find_duplicates

    result = find_duplicates(
        compute_hash=compute_hash,
        min_group_size=2,
        limit_groups=max(1, min(limit, 200)),
    )
    return result


@router.get("/field-locks")
def list_field_locks(limit: int = 100) -> dict:
    """List videos that have any manual field locks or confirmed translations."""
    init_db()
    items = []
    for video in VideoRepository().get_all():
        locks = video.field_locks or {}
        sources = video.field_sources or {}
        tmeta = video.translation_meta or {}
        if not locks and not tmeta.get("confirmed"):
            continue
        items.append({
            "path": video.path,
            "number": video.number or "",
            "title": video.title or video.original_title or "",
            "field_locks": locks,
            "field_sources": sources,
            "translation_meta": tmeta,
        })
        if len(items) >= max(1, min(limit, 500)):
            break
    return {"success": True, "total": len(items), "items": items}


class OpsFieldLockRequest(BaseModel):
    path: str
    field: str
    locked: bool = True


@router.post("/field-lock")
def ops_set_field_lock(payload: OpsFieldLockRequest) -> dict:
    from core.field_meta import TRACKED_FIELDS
    if payload.field not in TRACKED_FIELDS:
        raise HTTPException(status_code=400, detail=f"unknown field: {payload.field}")
    init_db()
    repo = VideoRepository()
    if not repo.set_field_lock(payload.path, payload.field, payload.locked):
        raise HTTPException(status_code=404, detail="video not found")
    video = repo.get_by_path(payload.path)
    return {
        "success": True,
        "path": payload.path,
        "field": payload.field,
        "locked": payload.locked,
        "field_locks": video.field_locks if video else {},
    }


@router.get("/rename-issues")
def rename_issues(limit: int = 100) -> dict:
    """List videos whose basename does not match showcase naming rule."""
    # Lazy import avoids circular import with showcase router helpers
    from web.routers.showcase import (
        _build_video_basename,
        _display_actresses_for_filename,
        _get_actress_alias_groups,
        _get_configured_dirs,
    )
    from core.path_utils import is_path_under_dir

    init_db()
    config = load_config()
    configured_dir_uris, _pm = _get_configured_dirs(config)
    alias_groups = _get_actress_alias_groups()
    items = []
    for video in VideoRepository().get_all():
        if configured_dir_uris and not any(is_path_under_dir(video.path, uri) for uri in configured_dir_uris):
            continue
        try:
            video_path = Path(uri_to_fs_path(video.path))
            expected = _build_video_basename(video)
            display_actresses = _display_actresses_for_filename(video, alias_groups)
            missing_display = [n for n in display_actresses if n not in video_path.stem]
            if video_path.stem != expected or missing_display:
                items.append({
                    "path": video.path,
                    "number": video.number or "",
                    "current": video_path.stem,
                    "expected": expected,
                    "missing_display_names": missing_display,
                    "title": video.original_title or video.title or "",
                })
        except Exception as exc:
            items.append({
                "path": video.path,
                "number": video.number or "",
                "current": "",
                "expected": "",
                "error": str(exc)[:200],
                "title": video.original_title or video.title or "",
            })
        if len(items) >= max(1, min(limit, 500)):
            break
    return {"success": True, "total": len(items), "items": items}


RENAME_BATCH_LIMIT = 50


@router.post("/rename-preview")
def rename_preview(payload: RenameBatchRequest) -> dict:
    """Dry-run rename plan for selected paths (or all rename issues if paths empty)."""
    from web.routers.showcase import _rename_video_assets, _get_configured_dirs
    from core.path_utils import is_path_under_dir

    init_db()
    config = load_config()
    configured_dir_uris, path_mappings = _get_configured_dirs(config)
    repo = VideoRepository()
    paths = list(payload.paths)
    if not paths:
        issues = rename_issues(limit=500)
        paths = [i["path"] for i in issues.get("items", [])]

    requested = len(paths)
    truncated = requested > RENAME_BATCH_LIMIT
    paths = paths[:RENAME_BATCH_LIMIT]

    results = []
    for path in paths:
        try:
            if configured_dir_uris and not any(is_path_under_dir(path, uri) for uri in configured_dir_uris):
                results.append({"path": path, "success": False, "error": "not_in_library"})
                continue
            video = repo.get_by_path(path)
            if not video:
                results.append({"path": path, "success": False, "error": "not_found"})
                continue
            plan = _rename_video_assets(
                video, path_mappings,
                rename_folder=payload.rename_folder,
                dry_run=True,
            )
            results.append({"path": path, "success": True, **plan})
        except Exception as exc:
            results.append({"path": path, "success": False, "error": str(exc)[:200]})
    return {
        "success": True,
        "dry_run": True,
        "count": len(results),
        "requested": requested,
        "processed": len(results),
        "truncated": truncated,
        "limit": RENAME_BATCH_LIMIT,
        "would_rename": sum(1 for r in results if r.get("renamed")),
        "results": results,
    }


@router.post("/rename-apply")
def rename_apply(payload: RenameBatchRequest) -> dict:
    """Apply rename for selected paths and record journal for rollback."""
    from web.routers.showcase import _rename_video_assets, _get_configured_dirs
    from core.path_utils import is_path_under_dir

    if payload.dry_run:
        return rename_preview(payload)

    init_db()
    config = load_config()
    configured_dir_uris, path_mappings = _get_configured_dirs(config)
    repo = VideoRepository()
    paths = list(payload.paths)
    if not paths:
        raise HTTPException(status_code=400, detail="paths required for apply")

    requested = len(paths)
    truncated = requested > RENAME_BATCH_LIMIT
    paths = paths[:RENAME_BATCH_LIMIT]

    # Pre-write pending journal plan so crash mid-batch is recoverable
    pending_event = rename_journal.append_event({
        "kind": "batch_rename",
        "status": "pending",
        "entries": [],
        "requested": requested,
        "truncated": truncated,
        "limit": RENAME_BATCH_LIMIT,
        "renamed": 0,
        "failed": 0,
        "skipped": 0,
    })
    pending_id = pending_event.get("id")

    batch_entries = []
    results = []
    renamed = 0
    failed = 0
    skipped = 0

    def _persist_batch_progress() -> None:
        """Write completed entries after each item so mid-batch crash is recoverable."""
        if not pending_id:
            return
        try:
            rename_journal.update_pending_progress(pending_id, {
                "status": "pending",
                "entries": list(batch_entries),
                "renamed": renamed,
                "failed": failed,
                "skipped": skipped,
                "processed": len(results),
            })
        except Exception as prog_exc:
            logger.warning("batch rename progress journal write failed: %s", prog_exc)

    for path in paths:
        try:
            if configured_dir_uris and not any(is_path_under_dir(path, uri) for uri in configured_dir_uris):
                results.append({"path": path, "success": False, "error": "not_in_library"})
                failed += 1
                _persist_batch_progress()
                continue
            video = repo.get_by_path(path)
            if not video:
                results.append({"path": path, "success": False, "error": "not_found"})
                failed += 1
                _persist_batch_progress()
                continue
            result = _rename_video_assets(
                video, path_mappings,
                rename_folder=payload.rename_folder,
                dry_run=False,
                journal=False,  # batch journal owns history
            )
            if result.get("renamed"):
                renamed += 1
                batch_entries.append({
                    "old_uri": result.get("old_uri") or path,
                    "new_uri": result.get("new_uri"),
                    "old_path": result.get("old_path"),
                    "new_path": result.get("new_path"),
                    "folder_renamed": result.get("folder_renamed"),
                    "file_moves": result.get("file_moves") or [],
                    "number": video.number,
                    "nfo_before": result.get("nfo_before_text"),
                    "status": "completed",
                })
            else:
                skipped += 1
            results.append({"path": path, "success": True, **{k: v for k, v in result.items() if k != "nfo_before_text"}})
        except Exception as exc:
            failed += 1
            logger.warning("rename-apply failed for %s: %s", path, exc)
            results.append({"path": path, "success": False, "error": str(exc)[:200]})
        # Persist after every item (success or fail) so crash mid-batch keeps recovery info
        _persist_batch_progress()

    if pending_id:
        rename_journal.finalize_event(pending_id, {
            "kind": "batch_rename",
            "status": "completed" if failed == 0 else "partial",
            "entries": batch_entries,
            "requested": requested,
            "processed": len(results),
            "truncated": truncated,
            "limit": RENAME_BATCH_LIMIT,
            "renamed": renamed,
            "failed": failed,
            "skipped": skipped,
        })

    return {
        "success": failed == 0,
        "renamed": renamed,
        "failed": failed,
        "skipped": skipped,
        "requested": requested,
        "processed": len(results),
        "truncated": truncated,
        "limit": RENAME_BATCH_LIMIT,
        "journal_id": pending_id,
        "results": results,
    }


@router.get("/rename-history")
def rename_history(limit: int = 30) -> dict:
    return {"success": True, "items": rename_journal.list_events(limit=limit)}


class RenameRollbackRequest(BaseModel):
    event_id: str


@router.post("/rename-rollback")
def rename_rollback(payload: RenameRollbackRequest) -> dict:
    """Reverse a journaled batch rename (files + DB paths + sample_images)."""
    from core.database import VideoRepository
    from core.path_utils import to_file_uri
    from web.routers.showcase import _get_configured_dirs, _replace_path_prefix

    event = rename_journal.get_event(payload.event_id)
    if not event:
        raise HTTPException(status_code=404, detail="journal event not found")
    if event.get("rolled_back"):
        raise HTTPException(status_code=409, detail="already rolled back")

    entries = event.get("entries") or []
    if not entries:
        raise HTTPException(status_code=400, detail="empty journal event")

    init_db()
    config = load_config()
    _dirs, path_mappings = _get_configured_dirs(config)
    repo = VideoRepository()
    restored = 0
    errors = []

    # Reverse order for safety
    for entry in reversed(entries):
        try:
            new_path = Path(entry["new_path"])
            old_path = Path(entry["old_path"])
            moves = entry.get("file_moves") or []
            # Reverse file moves: to → from, under possibly renamed folder
            # After rename: files live under new_dir with new names.
            # We need to rename files back then rename folder if needed.
            new_dir = new_path.parent
            old_dir = old_path.parent
            folder_was_renamed = bool(entry.get("folder_renamed"))

            # Reverse sidecar/file renames (new names → old names) while still in new_dir
            for move in reversed(moves):
                src = Path(move["to"])
                # If folder was renamed, move["to"] was planned under old_dir; actual file is under new_dir
                if folder_was_renamed:
                    candidate = new_dir / Path(move["to"]).name
                    if candidate.exists():
                        src = candidate
                dst_name = Path(move["from"]).name
                dst = src.with_name(dst_name)
                if src.exists() and src != dst:
                    if dst.exists():
                        raise FileExistsError(str(dst))
                    src.rename(dst)

            if folder_was_renamed and new_dir.exists() and new_dir != old_dir:
                if old_dir.exists():
                    raise FileExistsError(str(old_dir))
                new_dir.rename(old_dir)

            # Restore NFO content snapshot if we captured it before rename
            nfo_before = entry.get("nfo_before")
            if nfo_before is not None:
                try:
                    nfo_restore_path = Path(entry["old_path"]).with_suffix(".nfo")
                    # After folder reverse, old_path parent should exist
                    if not nfo_restore_path.parent.exists():
                        nfo_restore_path = old_dir / nfo_restore_path.name
                    nfo_restore_path.write_text(nfo_before, encoding="utf-8")
                except OSError as nfo_exc:
                    logger.warning("NFO content restore failed: %s", nfo_exc)

            # DB path restore (video + cover + sample_images)
            old_uri = entry.get("old_uri")
            new_uri = entry.get("new_uri")
            if new_uri and old_uri:
                video = repo.get_by_path(new_uri)
                if video:
                    restored_cover = video.cover_path
                    if restored_cover and folder_was_renamed:
                        # Reverse folder prefix first (handles samples/ subdirs too)
                        restored_cover = _replace_path_prefix(
                            restored_cover, new_dir, old_dir
                        )
                    if restored_cover:
                        try:
                            cfs = Path(uri_to_fs_path(restored_cover))
                            if not cfs.exists() and old_dir.exists():
                                cand = old_dir / cfs.name
                                if cand.exists():
                                    restored_cover = to_file_uri(str(cand), path_mappings)
                        except Exception:
                            pass

                    samples = list(video.sample_images or [])
                    if samples and folder_was_renamed:
                        samples = [
                            _replace_path_prefix(uri, new_dir, old_dir)
                            for uri in samples
                        ]
                    elif samples:
                        # File-only rename: map cover-like basename changes if needed
                        # Sample files are usually under a fixed samples/ folder and
                        # keep their names; leave as-is unless folder moved.
                        pass

                    repo.update_media_paths(
                        new_uri,
                        old_uri,
                        cover_path=restored_cover,
                        sample_images=samples,
                    )
                    try:
                        thumbnail_cache.invalidate(new_uri)
                        thumbnail_cache.invalidate(old_uri)
                    except Exception:
                        pass
            restored += 1
        except Exception as exc:
            logger.warning("rename rollback entry failed: %s", exc)
            errors.append(str(exc)[:200])

    if restored and not errors:
        rename_journal.mark_rolled_back(payload.event_id)

    return {
        "success": len(errors) == 0 and restored > 0,
        "restored": restored,
        "errors": errors,
        "event_id": payload.event_id,
    }
