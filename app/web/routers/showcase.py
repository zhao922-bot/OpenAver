"""
Showcase API 路由 - 影片展示資料端點

端點：
- GET /api/showcase/videos        — 取得所有影片資料（供 Showcase 頁面客戶端渲染）
- GET /api/showcase/video?path=   — 取得單筆影片資料（供 T3 enrich 後刷新卡片）
"""

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.database import AliasRepository, VideoRepository, get_db_path, init_db
from core.enricher import enrich_single as enrich_local_video
from core.path_utils import to_file_uri, is_path_under_dir, uri_to_fs_path
from core.logger import get_logger
from core.config import iter_gallery_sources, load_config
from core.scrapers.utils import has_japanese
from core import thumbnail_cache
from web.routers.translate import (
    _protect_actress_names,
    _restore_actress_names,
    get_translate_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/showcase", tags=["showcase"])

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".flv", ".webm"
}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class TranslateVideoRequest(BaseModel):
    path: str
    force: bool = False


class RollbackTranslationRequest(BaseModel):
    path: str


class ConfirmTranslationRequest(BaseModel):
    path: str
    confirmed: bool = True


class UpdateTitleRequest(BaseModel):
    path: str
    title: str = Field(min_length=1, max_length=500)
    original_title: Optional[str] = None
    lock: bool = True  # manual edit defaults to locked


class FieldLockRequest(BaseModel):
    path: str
    field: str
    locked: bool = True


class RenameVideoRequest(BaseModel):
    path: str
    rename_folder: bool = True
    dry_run: bool = False


class AutoRenameVideoRequest(BaseModel):
    path: str
    rename_folder: bool = True
    dry_run: bool = False
    source: Optional[str] = None
    javbus_lang: Optional[str] = None


class RenameVideosRequest(BaseModel):
    paths: list[str]
    rename_folder: bool = True
    dry_run: bool = False


def _serialize_video(
    v,
    path_mappings: dict,
    enabled: bool = False,
    actress_display_map: dict = None,
    has_translation_history: bool = False,
) -> dict:
    """將 Video ORM 物件序列化為前端 JSON dict（列表端點與單筆端點共用）。

    feature/71 T4：thumbnail_cache_enabled 開關決定 cover_url 走 thumb / image 分支。
    - enabled  → cover_url 指向 T3 /api/gallery/thumb?path=<quote(v.path)>（thumb key = video path）
    - disabled → 維持現狀 /api/gallery/image?path=<quote(uri_to_fs_path(v.cover_path))>（字節不變）
    cover_full_url 恆原圖（不受 flag 影響），供 T6 燈箱 blur-up 上層淡入用。
    """
    cover_url = ""
    cover_full_url = ""
    if v.cover_path:
        original_url = f"/api/gallery/image?path={quote(uri_to_fs_path(v.cover_path), safe='')}"
        cover_full_url = original_url
        if enabled:
            cover_url = f"/api/gallery/thumb?path={quote(v.path, safe='')}"
        else:
            cover_url = original_url

    sample_urls = []
    for img_uri in (v.sample_images or []):
        local_path = uri_to_fs_path(img_uri)
        sample_urls.append(f"/api/gallery/image?path={quote(local_path, safe='')}")

    display_map = actress_display_map or {}
    actresses = [
        display_map.get(name) or display_map.get((name or "").lower()) or name
        for name in (v.actresses or [])
    ]

    return {
        "path": v.path,                                          # file:/// URI（開啟影片用）
        "title": v.title,
        "original_title": v.original_title,
        "actresses": ','.join(actresses),  # 逗號分隔字串
        "number": v.number or '',
        "maker": v.maker,
        "release_date": v.release_date,
        "tags": ','.join(v.tags) if v.tags else '',              # 逗號分隔字串
        "size": v.size_bytes,
        "cover_url": cover_url,                                  # enabled→thumb / disabled→image
        "cover_full_url": cover_full_url,                        # 恆原圖 /api/gallery/image?path=...（T6 燈箱）
        "mtime": int(v.mtime) if v.mtime else 0,                 # Unix timestamp 整數
        "director": v.director or '',
        "duration": v.duration,                                  # Optional[int]，None 時前端 x-show 隱藏
        "series": v.series or '',
        "label": v.label or '',
        "sample_images": sample_urls,
        "user_tags": v.user_tags or [],              # list[str]，空時回空 list
        "has_cover": bool(v.cover_path),             # DB 初判（不做 IO）
        "has_nfo": (v.nfo_mtime or 0) > 0,          # 對齊 41a nfo_mtime 寫入契約，防禦 NULL
        "has_translation_history": bool(has_translation_history),
        "field_sources": getattr(v, "field_sources", None) or {},
        "field_locks": getattr(v, "field_locks", None) or {},
        "translation_meta": getattr(v, "translation_meta", None) or {},
    }


def _get_configured_dirs(config: dict) -> tuple[set, dict]:
    """從 config 取出 configured_dir_uris 與 path_mappings（列表與單筆端點共用）"""
    gallery_config = config.get('gallery', {})
    path_mappings = gallery_config.get('path_mappings', {})

    configured_dir_uris: set = set()
    for source in iter_gallery_sources(gallery_config):
        try:
            configured_dir_uris.add(to_file_uri(source.path, path_mappings))
        except ValueError:
            continue

    return configured_dir_uris, path_mappings


def _get_actress_display_map() -> dict:
    """Map every actress alias to its display primary name."""
    display_map = {}
    try:
        for record in AliasRepository().get_all():
            names = [record.primary_name] + (record.aliases or [])
            for name in names:
                if not name:
                    continue
                display_map[name] = record.primary_name
                display_map[name.lower()] = record.primary_name
    except Exception as exc:
        logger.warning("load actress display aliases failed: %s", exc)
    return display_map


def _get_actress_alias_groups() -> list[tuple[str, set[str]]]:
    groups = []
    try:
        for record in AliasRepository().get_all():
            names = {record.primary_name, *(record.aliases or [])}
            names = {name for name in names if name}
            if names:
                groups.append((record.primary_name, names))
    except Exception as exc:
        logger.warning("load actress alias groups failed: %s", exc)
    return groups


def _sanitize_filename_part(value: str, max_length: int = 180) -> str:
    value = (value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = "未命名"
    if value.upper() in WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .")
    return value


def _strip_number_prefix(title: str, number: str) -> str:
    title = (title or "").strip()
    number = (number or "").strip()
    if not title or not number:
        return title
    pattern = r"^\s*" + re.escape(number) + r"\s*[-_ ]+\s*"
    return re.sub(pattern, "", title, flags=re.IGNORECASE).strip()


def _normalize_name_for_compare(value: str) -> str:
    return "".join(ch.casefold() for ch in (value or "").strip() if ch.isalnum())


def _video_needs_metadata_enrich(video) -> bool:
    number_norm = _normalize_name_for_compare(video.number or "")
    title = (video.original_title or video.title or "").strip()
    title_norm = _normalize_name_for_compare(title)
    stem_norm = _normalize_name_for_compare(Path(uri_to_fs_path(video.path)).stem)
    if not title_norm:
        return True
    if number_norm and title_norm == number_norm:
        return True
    if stem_norm and title_norm == stem_norm:
        return True
    if not video.actresses:
        return True
    return False


def _name_already_in_text(name: str, text: str) -> bool:
    """True if name (or a close spacing variant) already appears in text."""
    if not name or not text:
        return False
    if name in text:
        return True
    # Tolerate full-width / half-width spaces differences
    compact_name = re.sub(r"\s+", "", name)
    compact_text = re.sub(r"\s+", "", text)
    return bool(compact_name) and compact_name in compact_text


def _display_actresses_for_filename(video, alias_groups: list[tuple[str, set[str]]]) -> list[str]:
    """Return actress names to *append* after title in basename.

    Skip if any name in the same alias group is already present in the title
    (e.g. title ends with 三澄寧々 while primary is 三澄宁宁 — do not append both).
    """
    actresses = []
    raw_actresses = video.actresses or []
    source_title = (video.original_title or video.title or "").strip()
    group_by_name = {}
    for primary, names in alias_groups:
        for name in names:
            group_by_name[name] = (primary, names)
            group_by_name[name.lower()] = (primary, names)

    for raw_name in raw_actresses:
        if not raw_name:
            continue
        group = group_by_name.get(raw_name) or group_by_name.get(raw_name.lower())
        if group:
            primary, names = group
            # Any form of this person already in title → do not append
            if any(_name_already_in_text(n, source_title) for n in names if n):
                continue
            display_name = primary
        else:
            display_name = raw_name
            if _name_already_in_text(display_name, source_title):
                continue
        if display_name and display_name not in actresses:
            actresses.append(display_name)
    return actresses


def _build_video_basename(video) -> str:
    number = _sanitize_filename_part(video.number or Path(uri_to_fs_path(video.path)).stem, 60)
    source_title = (video.original_title or video.title or "").strip()
    title = _strip_number_prefix(source_title, number)
    title = _sanitize_filename_part(title, 140)
    alias_groups = _get_actress_alias_groups()
    actress_names = _display_actresses_for_filename(video, alias_groups)
    # Also drop actresses already present in the sanitized title segment
    actress_names = [
        n for n in actress_names
        if not _name_already_in_text(n, title)
        and not any(
            _name_already_in_text(alias, title)
            for primary, names in alias_groups
            if n == primary or n in names
            for alias in names
        )
    ]
    parts = [f"{number} - {title}"]
    if actress_names:
        parts.append(" ".join(_sanitize_filename_part(name, 40) for name in actress_names))
    return _sanitize_filename_part(" ".join(parts), 220)


def _path_mtime_as_db_value(path: Path) -> float:
    if not path.exists():
        return 0.0
    return path.stat().st_mtime


def _replace_path_prefix(uri: str, old_dir: Path, new_dir: Path) -> str:
    if not uri:
        return uri
    try:
        fs_path = Path(uri_to_fs_path(uri))
        if fs_path == old_dir or old_dir in fs_path.parents:
            return to_file_uri(str(new_dir / fs_path.relative_to(old_dir)))
    except Exception:
        return uri
    return uri


def _update_nfo_after_rename(nfo_path: Path, video, new_cover_name: Optional[str]) -> bool:
    if not nfo_path.exists():
        return False
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()

        def ensure_child(name: str):
            child = root.find(name)
            if child is None:
                child = ET.SubElement(root, name)
            return child

        title = video.title or video.original_title or ""
        original_title = video.original_title or video.title or ""
        ensure_child("title").text = title
        ensure_child("originaltitle").text = original_title
        if new_cover_name:
            ensure_child("poster").text = new_cover_name
            ensure_child("thumb").text = new_cover_name
            ensure_child("fanart").text = new_cover_name

        try:
            ET.indent(tree, space="  ")
        except AttributeError:
            pass
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception as exc:
        logger.warning("update nfo after rename failed for %s: %s", nfo_path, exc)
        return False


def _planned_sidecar_moves(video_path: Path, new_base: str) -> list[tuple[Path, Path]]:
    old_stem = video_path.stem
    wanted_stems = {
        old_stem,
        f"{old_stem}-poster",
        f"{old_stem}-fanart",
    }
    moves: list[tuple[Path, Path]] = []
    for item in video_path.parent.iterdir():
        if not item.is_file():
            continue
        if item == video_path or item.stem in wanted_stems:
            suffix = item.suffix
            if item.stem == f"{old_stem}-poster":
                new_name = f"{new_base}-poster{suffix}"
            elif item.stem == f"{old_stem}-fanart":
                new_name = f"{new_base}-fanart{suffix}"
            else:
                new_name = f"{new_base}{suffix}"
            target = item.with_name(new_name)
            if item != target:
                moves.append((item, target))
    return moves


def _rename_video_assets(
    video,
    path_mappings: dict,
    rename_folder: bool = True,
    dry_run: bool = False,
    journal: bool = True,
) -> dict:
    # Respect filename lock — skip auto rename
    locks = getattr(video, "field_locks", None) or {}
    if locks.get("filename"):
        old_video_path = Path(uri_to_fs_path(video.path))
        return {
            "renamed": False,
            "reason": "filename_locked",
            "new_base": old_video_path.stem,
            "old_path": str(old_video_path),
            "new_path": str(old_video_path),
            "folder_renamed": False,
            "file_moves": [],
        }

    old_video_path = Path(uri_to_fs_path(video.path))
    if not old_video_path.exists():
        raise FileNotFoundError(str(old_video_path))

    new_base = _build_video_basename(video)
    old_dir = old_video_path.parent
    parent_dir = old_dir.parent
    video_files = [
        p for p in old_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    should_rename_folder = bool(rename_folder and len(video_files) == 1)
    new_dir = parent_dir / new_base if should_rename_folder else old_dir

    moves = _planned_sidecar_moves(old_video_path, new_base)
    if not moves and new_dir == old_dir:
        return {
            "renamed": False,
            "reason": "already_named",
            "new_base": new_base,
            "old_path": str(old_video_path),
            "new_path": str(old_video_path),
            "folder_renamed": False,
            "file_moves": [],
        }

    move_sources = {src.resolve() for src, _ in moves}
    for src, dst in moves:
        if dst.exists() and dst.resolve() not in move_sources:
            raise FileExistsError(str(dst))
    if new_dir != old_dir and new_dir.exists():
        raise FileExistsError(str(new_dir))

    final_video_path = new_dir / f"{new_base}{old_video_path.suffix}"
    planned_moves = [{"from": str(src), "to": str(dst)} for src, dst in moves]
    # Compute new URI + NFO snapshot before any mutation (and before dry_run return)
    # so batch planning and single pending journals share a complete recovery plan.
    new_video_uri = to_file_uri(str(final_video_path), path_mappings)
    nfo_before_text = None
    old_nfo_path = old_video_path.with_suffix(".nfo")
    if old_nfo_path.is_file():
        try:
            nfo_before_text = old_nfo_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("could not snapshot NFO before rename: %s", exc)

    if dry_run:
        return {
            "renamed": bool(moves or new_dir != old_dir),
            "dry_run": True,
            "new_base": new_base,
            "old_path": str(old_video_path),
            "new_path": str(final_video_path),
            "old_uri": video.path,
            "new_uri": new_video_uri,
            "folder_renamed": new_dir != old_dir,
            "file_moves": planned_moves,
            "nfo_before_text": nfo_before_text,
            "number": video.number,
        }

    # Resolve cover target BEFORE any filesystem mutation
    cover_target_name = None
    if video.cover_path:
        try:
            cover_fs = Path(uri_to_fs_path(video.cover_path))
            for src, dst in moves:
                try:
                    if cover_fs.resolve() == src.resolve() or cover_fs.name == src.name:
                        cover_target_name = dst.name
                        break
                except OSError:
                    if cover_fs.name == src.name:
                        cover_target_name = dst.name
                        break
        except Exception:
            pass

    journal_entry = {
        "old_uri": video.path,
        "new_uri": new_video_uri,
        "old_path": str(old_video_path),
        "new_path": str(final_video_path),
        "folder_renamed": new_dir != old_dir,
        "file_moves": planned_moves,
        "number": video.number,
        "nfo_before": nfo_before_text,
        "status": "pending",
    }
    pending_journal_id = None
    if journal:
        try:
            from core import rename_journal
            pending_ev = rename_journal.append_event({
                "kind": "single_rename",
                "status": "pending",
                "entries": [journal_entry],
                "renamed": 0,
                "failed": 0,
                "skipped": 0,
            })
            pending_journal_id = pending_ev.get("id")
            if not pending_journal_id:
                raise RuntimeError("rename journal returned empty id")
        except Exception as exc:
            # Hard-fail: without a journal id the rename cannot be rolled back safely
            logger.error("rename pre-journal failed — aborting rename: %s", exc)
            raise RuntimeError(
                f"rename journal write failed; aborting to keep rollback safety: {exc}"
            ) from exc

    completed_file_moves: list[tuple[Path, Path]] = []
    folder_was_renamed = False
    nfo_updated = False
    try:
        for src, dst in moves:
            src.rename(dst)
            completed_file_moves.append((src, dst))
        if new_dir != old_dir:
            old_dir.rename(new_dir)
            folder_was_renamed = True

        cover_uri = video.cover_path
        if cover_uri:
            if cover_target_name:
                cover_uri = to_file_uri(str(new_dir / cover_target_name), path_mappings)
            elif new_dir != old_dir:
                cover_uri = _replace_path_prefix(cover_uri, old_dir, new_dir)

        sample_images = list(video.sample_images or [])
        if new_dir != old_dir:
            sample_images = [
                _replace_path_prefix(uri, old_dir, new_dir)
                for uri in sample_images
            ]

        repo = VideoRepository()
        nfo_path = final_video_path.with_suffix(".nfo")
        nfo_updated = _update_nfo_after_rename(
            nfo_path,
            video,
            f"{new_base}.jpg" if (final_video_path.with_suffix(".jpg")).exists() else None,
        )
        if not repo.update_media_paths(
            video.path,
            new_video_uri,
            cover_path=cover_uri,
            sample_images=sample_images,
            mtime=_path_mtime_as_db_value(final_video_path),
            nfo_mtime=_path_mtime_as_db_value(nfo_path),
        ):
            raise RuntimeError("video not found after rename — rolling back files")
        # Thumbnail cache is best-effort; never roll back FS/DB for cache failures
        try:
            thumbnail_cache.invalidate(video.path)
            thumbnail_cache.invalidate(new_video_uri)
        except Exception as inv_exc:
            logger.warning("thumbnail cache invalidate after rename failed: %s", inv_exc)

    except Exception:
        # Best-effort reverse of filesystem changes so FS and DB stay consistent
        try:
            if folder_was_renamed and new_dir.exists() and not old_dir.exists():
                new_dir.rename(old_dir)
            for src, dst in reversed(completed_file_moves):
                actual = old_dir / dst.name if (old_dir / dst.name).exists() else (
                    new_dir / dst.name if (new_dir / dst.name).exists() else dst
                )
                target = src if src.parent.exists() else (old_dir / src.name)
                if actual.exists() and not target.exists():
                    actual.rename(target)
        except Exception as rev_exc:
            logger.error("rename FS rollback failed: %s", rev_exc)
        if pending_journal_id:
            try:
                from core import rename_journal
                rename_journal.mark_failed(pending_journal_id, "rename aborted; FS rolled back")
            except Exception:
                pass
        raise

    result = {
        "renamed": True,
        "new_base": new_base,
        "old_path": str(old_video_path),
        "new_path": str(final_video_path),
        "old_uri": video.path,
        "new_uri": new_video_uri,
        "folder_renamed": new_dir != old_dir,
        "file_moves": planned_moves,
        "nfo_updated": nfo_updated,
        "nfo_before": nfo_before_text is not None,
        "nfo_before_text": nfo_before_text,  # batch journal / content rollback
        "journal_id": pending_journal_id,
        "journal_status": "completed",
    }
    if journal or pending_journal_id:
        try:
            from core import rename_journal
            entry_done = {
                "old_uri": video.path,
                "new_uri": new_video_uri,
                "old_path": str(old_video_path),
                "new_path": str(final_video_path),
                "folder_renamed": new_dir != old_dir,
                "file_moves": planned_moves,
                "number": video.number,
                "nfo_before": nfo_before_text,
                "status": "completed",
            }
            if pending_journal_id:
                finalized = rename_journal.finalize_event(
                    pending_journal_id,
                    {
                        "kind": "single_rename",
                        "status": "completed",
                        "entries": [entry_done],
                        "renamed": 1,
                        "failed": 0,
                        "skipped": 0,
                    },
                )
                if not finalized:
                    # Rename applied, but journal still pending and fully rollback-capable.
                    logger.error(
                        "rename journal finalize_event returned False for %s — "
                        "leaving pending recovery plan",
                        pending_journal_id,
                    )
                    result["journal_status"] = "pending"
                    result["journal_warning"] = "journal_finalize_failed"
            elif journal:
                rename_journal.append_event({
                    "kind": "single_rename",
                    "status": "completed",
                    "entries": [entry_done],
                    "renamed": 1,
                    "failed": 0,
                    "skipped": 0,
                })
        except Exception as exc:
            logger.warning("rename journal finalize failed: %s", exc)
            result["journal_status"] = "pending"
            result["journal_warning"] = f"journal_finalize_error: {exc}"[:200]
    return result


def _choose_translate_source(video) -> str:
    """Pick the Japanese title to translate, preserving existing translated titles."""
    original_title = (video.original_title or "").strip()
    title = (video.title or "").strip()
    if original_title and has_japanese(original_title):
        return original_title
    return title


def _sync_nfo_title(video_uri: str, title: str, original_title: str) -> bool:
    """Best-effort sync of translated title to sidecar NFO."""
    try:
        video_path = Path(uri_to_fs_path(video_uri))
        nfo_path = video_path.with_suffix(".nfo")
        if not nfo_path.exists():
            return False

        tree = ET.parse(nfo_path)
        root = tree.getroot()

        def ensure_child(name: str):
            child = root.find(name)
            if child is None:
                child = ET.SubElement(root, name)
            return child

        ensure_child("title").text = title or ""
        ensure_child("originaltitle").text = original_title or ""

        try:
            ET.indent(tree, space="  ")
        except AttributeError:
            pass
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception as exc:
        logger.warning("sync nfo title failed for %s: %s", video_uri, exc)
        return False


def _record_title_translation_history(
    db_path: Path,
    video,
    new_title: str,
    new_original_title: str,
    source: str = "showcase_translate",
    *,
    model: str = "",
    provider: str = "",
    prompt_version: str = "",
    source_hash: str = "",
    confirmed: bool = False,
) -> None:
    """Save the previous title values before a translation overwrites them."""
    old_title = video.title or ""
    old_original = video.original_title or ""
    new_title = new_title or ""
    new_original_title = new_original_title or ""
    if old_title == new_title and old_original == new_original_title:
        return

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                INSERT INTO title_translation_history (
                    path, number, old_title, old_original_title,
                    new_title, new_original_title, source,
                    model, provider, prompt_version, source_hash, confirmed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video.path,
                    video.number or "",
                    old_title,
                    old_original,
                    new_title,
                    new_original_title,
                    source,
                    model or "",
                    provider or "",
                    prompt_version or "",
                    source_hash or "",
                    1 if confirmed else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        # Fallback without new columns (pre-migration race)
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    INSERT INTO title_translation_history (
                        path, number, old_title, old_original_title,
                        new_title, new_original_title, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video.path, video.number or "", old_title, old_original,
                        new_title, new_original_title, source,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc2:
            logger.warning("record title translation history failed for %s: %s / %s", video.path, exc, exc2)


def _title_translation_history_keys(db_path: Path) -> tuple[set[str], set[str]]:
    """Return paths and numbers that have rollback history."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT DISTINCT path, number FROM title_translation_history"
            ).fetchall()
            paths = {row[0] for row in rows if row[0]}
            numbers = {row[1] for row in rows if row[1]}
            return paths, numbers
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("load title translation history keys failed: %s", exc)
        return set(), set()


def _has_title_translation_history(db_path: Path, video) -> bool:
    paths, numbers = _title_translation_history_keys(db_path)
    return bool(video.path in paths or ((video.number or "") in numbers))


def _latest_title_translation_history(db_path: Path, video) -> Optional[dict]:
    """Return the newest rollback candidate for a video path, falling back to number."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM title_translation_history
                WHERE path = ? OR (? != '' AND number = ?)
                ORDER BY
                    CASE WHEN new_title = ? THEN 0 ELSE 1 END,
                    id DESC
                LIMIT 1
                """,
                (video.path, video.number or "", video.number or "", video.title or ""),
            ).fetchall()
            if not rows:
                return None
            return dict(rows[0])
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("load title translation history failed for %s: %s", video.path, exc)
        return None


def _delete_title_translation_history(db_path: Path, history_id: int) -> None:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM title_translation_history WHERE id = ?", (history_id,))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("delete title translation history failed for %s: %s", history_id, exc)


@router.get("/videos")
def get_videos():
    """取得所有影片資料（用於 Showcase 頁面客戶端渲染）"""
    try:
        db_path = get_db_path()

        # 空庫情境：資料庫檔案不存在
        if not db_path.exists():
            return JSONResponse({
                "success": True,
                "videos": [],
                "total": 0
            })

        init_db(db_path)  # 確保 schema 存在（防止半毀損 DB）
        repo = VideoRepository(db_path)

        # 只取「當前設定資料夾」底下的記錄（DB 保留全部當 cache）
        config = load_config()
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        all_videos = [v for v in repo.get_all()
                      if any(is_path_under_dir(v.path, uri) for uri in configured_dir_uris)]

        thumb_enabled = config.get('thumbnail_cache_enabled', False)
        actress_display_map = _get_actress_display_map()
        history_paths, history_numbers = _title_translation_history_keys(db_path)
        videos_json = [
            _serialize_video(
                v,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                v.path in history_paths or ((v.number or "") in history_numbers),
            )
            for v in all_videos
        ]

        return JSONResponse({
            "success": True,
            "videos": videos_json,
            "total": len(videos_json)
        })

    except Exception as e:
        logger.error("取得影片資料失敗: %s", e)
        return JSONResponse({
            "success": False,
            "error": "取得影片資料失敗",
            "videos": [],
            "total": 0
        }, status_code=500)


@router.get("/health-check")
def health_check(include_details: bool = Query(False, description="include issue details")):
    """Read-only local library health check for metadata and file consistency."""
    try:
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({
                "success": True,
                "summary": {
                    "total": 0,
                    "issue_count": 0,
                    "missing_title": 0,
                    "missing_actresses": 0,
                    "missing_video_files": 0,
                    "missing_cover_files": 0,
                    "missing_nfo_files": 0,
                    "rename_issues": 0,
                    "metadata_needs_enrich": 0,
                },
                "issues": [],
            })

        init_db(db_path)
        repo = VideoRepository(db_path)
        config = load_config()
        configured_dir_uris, _path_mappings = _get_configured_dirs(config)
        alias_groups = _get_actress_alias_groups()
        all_videos = [
            v for v in repo.get_all()
            if not configured_dir_uris or any(is_path_under_dir(v.path, uri) for uri in configured_dir_uris)
        ]

        counters = {
            "missing_title": 0,
            "missing_actresses": 0,
            "missing_video_files": 0,
            "missing_cover_files": 0,
            "missing_nfo_files": 0,
            "rename_issues": 0,
            "metadata_needs_enrich": 0,
        }
        issues = []

        def add_issue(video, kind: str, message: str, extra: dict | None = None) -> None:
            counters[kind] += 1
            if include_details:
                item = {
                    "number": video.number or "",
                    "kind": kind,
                    "message": message,
                    "path": video.path,
                }
                if extra:
                    item.update(extra)
                issues.append(item)

        for video in all_videos:
            title = (video.original_title or video.title or "").strip()
            if not title:
                add_issue(video, "missing_title", "missing title")
            if not (video.actresses or []):
                add_issue(video, "missing_actresses", "missing actresses")

            video_path = Path(uri_to_fs_path(video.path))
            if not video_path.exists():
                add_issue(video, "missing_video_files", "video file missing")

            if video.cover_path and not Path(uri_to_fs_path(video.cover_path)).exists():
                add_issue(video, "missing_cover_files", "cover file missing")

            nfo_path = video_path.with_suffix(".nfo")
            if not nfo_path.exists():
                add_issue(video, "missing_nfo_files", "nfo file missing")

            try:
                expected = _build_video_basename(video)
                display_actresses = _display_actresses_for_filename(video, alias_groups)
                missing_display = [name for name in display_actresses if name not in video_path.stem]
                if video_path.stem != expected or missing_display:
                    add_issue(video, "rename_issues", "filename does not match current rule", {
                        "current": video_path.stem,
                        "expected": expected,
                        "missing_display_names": missing_display,
                    })
            except Exception as exc:
                add_issue(video, "rename_issues", f"rename rule check failed: {exc}")

            if _video_needs_metadata_enrich(video):
                add_issue(video, "metadata_needs_enrich", "metadata incomplete or placeholder-like")

        issue_count = sum(counters.values())
        return JSONResponse({
            "success": True,
            "summary": {
                "total": len(all_videos),
                "issue_count": issue_count,
                **counters,
            },
            "issues": issues if include_details else [],
        })

    except Exception as exc:
        logger.exception("showcase health check failed: %s", exc)
        return JSONResponse({
            "success": False,
            "error": "health check failed",
        }, status_code=500)


@router.get("/video")
def get_video(path: str = Query(..., description="file:/// URI")):
    """取得單筆影片資料（用於 T3 refreshVideoData enrich 後刷新卡片）"""
    try:
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        init_db(db_path)
        repo = VideoRepository(db_path)

        config = load_config()
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        if not any(is_path_under_dir(path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        v = repo.get_by_path(path)
        if v is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        thumb_enabled = config.get('thumbnail_cache_enabled', False)
        actress_display_map = _get_actress_display_map()
        return JSONResponse({
            "success": True,
            "video": _serialize_video(
                v,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                _has_title_translation_history(db_path, v),
            ),
        })

    except Exception as e:
        logger.error("取得單筆影片失敗: %s", e)
        return JSONResponse({"success": False, "error": "取得影片資料失敗"}, status_code=500)


@router.post("/rename-video")
def rename_video(request: RenameVideoRequest):
    """Rename one local video, its same-stem sidecars, and optionally its one-video folder."""
    try:
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        init_db(db_path)
        repo = VideoRepository(db_path)
        config = load_config()
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        result = _rename_video_assets(
            video,
            path_mappings,
            rename_folder=request.rename_folder,
            dry_run=request.dry_run,
        )
        updated_video = None
        if not request.dry_run:
            updated_video = repo.get_by_path(result.get("new_uri") or request.path)

        thumb_enabled = config.get("thumbnail_cache_enabled", False)
        actress_display_map = _get_actress_display_map()
        return JSONResponse({
            "success": True,
            **result,
            "video": _serialize_video(
                updated_video,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                _has_title_translation_history(db_path, updated_video),
            )
            if updated_video else None,
        })
    except FileExistsError as exc:
        return JSONResponse({"success": False, "error": "target_exists", "path": str(exc)}, status_code=409)
    except FileNotFoundError as exc:
        return JSONResponse({"success": False, "error": "file_not_found", "path": str(exc)}, status_code=404)
    except Exception as exc:
        logger.exception("showcase rename failed: %s", exc)
        return JSONResponse({"success": False, "error": "rename failed"}, status_code=500)


@router.post("/auto-rename-video")
def auto_rename_video(request: AutoRenameVideoRequest):
    """Enrich code-only local videos first, then rename by number/title/actresses."""
    try:
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        init_db(db_path)
        repo = VideoRepository(db_path)
        config = load_config()
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        enriched = False
        enrich_error = None
        if _video_needs_metadata_enrich(video):
            if request.dry_run:
                result = {
                    "renamed": True,
                    "dry_run": True,
                    "needs_enrich": True,
                    "reason": "metadata_incomplete",
                    "old_path": uri_to_fs_path(video.path),
                    "new_path": "",
                    "folder_renamed": False,
                    "file_moves": [],
                }
                thumb_enabled = config.get("thumbnail_cache_enabled", False)
                actress_display_map = _get_actress_display_map()
                return JSONResponse({
                    "success": True,
                    **result,
                    "video": _serialize_video(
                        video,
                        path_mappings,
                        thumb_enabled,
                        actress_display_map,
                        _has_title_translation_history(db_path, video),
                    ),
                })

            search_cfg = config.get("search", {})
            enrich_result = enrich_local_video(
                file_path=video.path,
                number=video.number or "",
                mode="refresh_full",
                write_nfo=True,
                write_cover=True,
                write_extrafanart=False,
                overwrite_existing=True,
                external_manager=config.get("scraper", {}).get("external_manager", "off"),
                proxy_url=search_cfg.get("proxy_url", ""),
                source=request.source,
                javbus_lang=request.javbus_lang,
            )
            if not enrich_result.success:
                enrich_error = enrich_result.error or "metadata enrich failed"
                return JSONResponse({
                    "success": False,
                    "error": enrich_error,
                    "stage": "enrich",
                }, status_code=502)
            enriched = True
            video = repo.get_by_path(request.path) or video
            if _video_needs_metadata_enrich(video):
                return JSONResponse({
                    "success": False,
                    "error": "metadata title still incomplete",
                    "stage": "enrich",
                }, status_code=502)

        result = _rename_video_assets(
            video,
            path_mappings,
            rename_folder=request.rename_folder,
            dry_run=request.dry_run,
        )
        updated_video = None
        if not request.dry_run:
            updated_video = repo.get_by_path(result.get("new_uri") or request.path)

        thumb_enabled = config.get("thumbnail_cache_enabled", False)
        actress_display_map = _get_actress_display_map()
        return JSONResponse({
            "success": True,
            "enriched": enriched,
            "enrich_error": enrich_error,
            **result,
            "video": _serialize_video(
                updated_video,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                _has_title_translation_history(db_path, updated_video),
            )
            if updated_video else _serialize_video(
                video,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                _has_title_translation_history(db_path, video),
            ),
        })
    except FileExistsError as exc:
        return JSONResponse({"success": False, "error": "target_exists", "path": str(exc)}, status_code=409)
    except FileNotFoundError as exc:
        return JSONResponse({"success": False, "error": "file_not_found", "path": str(exc)}, status_code=404)
    except Exception as exc:
        logger.exception("showcase auto rename failed: %s", exc)
        return JSONResponse({"success": False, "error": "auto rename failed"}, status_code=500)


@router.post("/rename-videos")
def rename_videos(request: RenameVideosRequest):
    """Batch rename local videos by number, original title, and display actress names."""
    db_path = get_db_path()
    if not db_path.exists():
        return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

    init_db(db_path)
    repo = VideoRepository(db_path)
    config = load_config()
    configured_dir_uris, path_mappings = _get_configured_dirs(config)
    thumb_enabled = config.get("thumbnail_cache_enabled", False)
    actress_display_map = _get_actress_display_map()

    results = []
    renamed = 0
    skipped = 0
    failed = 0
    seen = set()
    for path in request.paths:
        if not path or path in seen:
            continue
        seen.add(path)
        try:
            if not any(is_path_under_dir(path, uri) for uri in configured_dir_uris):
                raise FileNotFoundError(path)
            video = repo.get_by_path(path)
            if video is None:
                raise FileNotFoundError(path)
            result = _rename_video_assets(
                video,
                path_mappings,
                rename_folder=request.rename_folder,
                dry_run=request.dry_run,
            )
            if result.get("renamed"):
                renamed += 1
            else:
                skipped += 1
            updated_video = None
            if not request.dry_run:
                updated_video = repo.get_by_path(result.get("new_uri") or path)
            results.append({
                "success": True,
                **result,
                "video": _serialize_video(
                    updated_video,
                    path_mappings,
                    thumb_enabled,
                    actress_display_map,
                    _has_title_translation_history(db_path, updated_video),
                )
                if updated_video else None,
            })
        except Exception as exc:
            failed += 1
            logger.warning("batch rename failed for %s: %s", path, exc)
            results.append({
                "success": False,
                "path": path,
                "error": "target_exists" if isinstance(exc, FileExistsError)
                else "file_not_found" if isinstance(exc, FileNotFoundError)
                else "rename failed",
                "detail": str(exc),
            })

    return JSONResponse({
        "success": failed == 0,
        "renamed": renamed,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }, status_code=200 if failed == 0 else 207)


@router.post("/translate-video")
async def translate_video(request: TranslateVideoRequest):
    """Translate one local showcase video title and persist it."""
    try:
        config = await asyncio.to_thread(load_config)
        translate_config = config.get("translate", {})
        if not translate_config.get("enabled", False):
            return JSONResponse({"success": False, "error": "translate disabled"}, status_code=400)

        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        init_db(db_path)
        repo = VideoRepository(db_path)
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        from core.field_meta import (
            TRANSLATE_PROMPT_VERSION,
            build_translation_meta,
            should_skip_retranslate,
            source_text_hash,
        )

        source_title = _choose_translate_source(video)
        current_title = (video.title or "").strip()
        current_original = (video.original_title or "").strip()
        already_translated = (
            current_original
            and has_japanese(current_original)
            and current_title
            and not has_japanese(current_title)
            and current_title != current_original
        )

        # Confirmed translation with same source text + prompt version → skip
        skip_confirmed, skip_reason = should_skip_retranslate(
            getattr(video, "translation_meta", None),
            source_text=source_title,
            force=request.force,
        )
        if skip_confirmed:
            thumb_enabled = config.get("thumbnail_cache_enabled", False)
            actress_display_map = _get_actress_display_map()
            return JSONResponse({
                "success": True,
                "skipped": True,
                "reason": skip_reason or "translation_confirmed",
                "video": _serialize_video(
                    video, path_mappings, thumb_enabled, actress_display_map,
                    _has_title_translation_history(db_path, video),
                ),
            })

        # Locked title without force → skip auto translate
        locks = getattr(video, "field_locks", None) or {}
        if locks.get("title") and not request.force:
            thumb_enabled = config.get("thumbnail_cache_enabled", False)
            actress_display_map = _get_actress_display_map()
            return JSONResponse({
                "success": True,
                "skipped": True,
                "reason": "title_locked",
                "video": _serialize_video(
                    video, path_mappings, thumb_enabled, actress_display_map,
                    _has_title_translation_history(db_path, video),
                ),
            })

        if already_translated and not request.force:
            thumb_enabled = config.get("thumbnail_cache_enabled", False)
            actress_display_map = _get_actress_display_map()
            return JSONResponse({
                "success": True,
                "skipped": True,
                "reason": "already_translated",
                "video": _serialize_video(
                    video,
                    path_mappings,
                    thumb_enabled,
                    actress_display_map,
                    _has_title_translation_history(db_path, video),
                ),
            })

        if not source_title or not has_japanese(source_title):
            thumb_enabled = config.get("thumbnail_cache_enabled", False)
            actress_display_map = _get_actress_display_map()
            return JSONResponse({
                "success": True,
                "skipped": True,
                "reason": "no_japanese",
                "video": _serialize_video(
                    video,
                    path_mappings,
                    thumb_enabled,
                    actress_display_map,
                    _has_title_translation_history(db_path, video),
                ),
            })

        translate_service = await asyncio.to_thread(get_translate_service)
        provider = (translate_config.get("provider") or "ollama")
        model = ""
        try:
            model = getattr(translate_service, "model", "") or ""
            if not model:
                model = (translate_config.get(provider) or {}).get("model", "")
        except Exception:
            model = ""

        context = {
            "actors": video.actresses or [],
            "number": video.number or "",
        }
        protected_title, actress_replacements = _protect_actress_names(
            source_title,
            video.actresses or [],
        )
        translated_title = (
            _restore_actress_names(
                await translate_service.translate_single(protected_title, context) or "",
                actress_replacements,
            )
        ).strip()
        if not translated_title:
            return JSONResponse({"success": False, "error": "empty translation"}, status_code=502)

        original_title = current_original if current_original else source_title
        tmeta = build_translation_meta(
            provider=provider,
            model=model,
            prompt_version=TRANSLATE_PROMPT_VERSION,
            source_text=source_title,
            confirmed=False,
            previous=getattr(video, "translation_meta", None),
        )
        _record_title_translation_history(
            db_path, video, translated_title, original_title,
            source=f"translate:{provider}",
            model=model,
            provider=provider,
            prompt_version=TRANSLATE_PROMPT_VERSION,
            source_hash=source_text_hash(source_title),
        )
        if not repo.update_title(
            video.path, translated_title, original_title,
            lock=False,
            source=f"translate:{provider}",
            translation_meta=tmeta,
        ):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        nfo_updated = _sync_nfo_title(video.path, translated_title, original_title)
        updated_video = repo.get_by_path(video.path)
        thumb_enabled = config.get("thumbnail_cache_enabled", False)
        actress_display_map = _get_actress_display_map()

        return JSONResponse({
            "success": True,
            "translated_title": translated_title,
            "original_title": original_title,
            "nfo_updated": nfo_updated,
            "translation_meta": tmeta,
            "video": _serialize_video(updated_video, path_mappings, thumb_enabled, actress_display_map, True),
        })

    except ValueError as e:
        logger.exception("showcase translate config error: %s", e)
        return JSONResponse({"success": False, "error": "translate config error"}, status_code=400)
    except Exception as e:
        logger.exception("showcase translate failed: %s", e)
        return JSONResponse({"success": False, "error": "translate failed"}, status_code=500)


@router.post("/confirm-translation")
def confirm_translation(request: ConfirmTranslationRequest):
    """Mark current Chinese title as human-confirmed (locks against auto re-translate)."""
    try:
        from core.field_meta import build_translation_meta, parse_json_map, set_lock

        config = load_config()
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        init_db(db_path)
        repo = VideoRepository(db_path)
        configured_dir_uris, path_mappings = _get_configured_dirs(config)
        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        prev = parse_json_map(video.translation_meta)
        source_text = _choose_translate_source(video)
        tmeta = build_translation_meta(
            provider=prev.get("provider", ""),
            model=prev.get("model", ""),
            prompt_version=prev.get("prompt_version", ""),
            source_text=source_text,
            confirmed=bool(request.confirmed),
            previous=prev,
        )
        locks = parse_json_map(video.field_locks)
        if request.confirmed:
            locks = set_lock(locks, "title", True)
        else:
            # Unconfirm does not force-unlock; user may keep manual lock
            pass
        repo.update_field_meta(video.path, locks=locks, translation_meta=tmeta)
        updated = repo.get_by_path(video.path)
        thumb_enabled = config.get("thumbnail_cache_enabled", False)
        return JSONResponse({
            "success": True,
            "confirmed": bool(request.confirmed),
            "translation_meta": tmeta,
            "field_locks": locks,
            "video": _serialize_video(
                updated, path_mappings, thumb_enabled, _get_actress_display_map(),
                _has_title_translation_history(db_path, updated),
            ),
        })
    except Exception as e:
        logger.exception("confirm translation failed: %s", e)
        return JSONResponse({"success": False, "error": "confirm failed"}, status_code=500)


@router.post("/update-title")
def update_title_manual(request: UpdateTitleRequest):
    """Manual title edit; defaults to locking the title against auto overwrite."""
    try:
        config = load_config()
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        init_db(db_path)
        repo = VideoRepository(db_path)
        configured_dir_uris, path_mappings = _get_configured_dirs(config)
        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        ok = repo.update_title(
            video.path,
            request.title.strip(),
            request.original_title if request.original_title is not None else video.original_title,
            lock=request.lock,
            source="manual",
        )
        if not ok:
            return JSONResponse({"success": False, "error": "update failed"}, status_code=500)
        nfo_updated = _sync_nfo_title(
            video.path,
            request.title.strip(),
            request.original_title if request.original_title is not None else (video.original_title or ""),
        )
        updated = repo.get_by_path(video.path)
        return JSONResponse({
            "success": True,
            "nfo_updated": nfo_updated,
            "locked": request.lock,
            "video": _serialize_video(
                updated, path_mappings, config.get("thumbnail_cache_enabled", False),
                _get_actress_display_map(),
                _has_title_translation_history(db_path, updated),
            ),
        })
    except Exception as e:
        logger.exception("update title failed: %s", e)
        return JSONResponse({"success": False, "error": "update title failed"}, status_code=500)


@router.post("/field-lock")
def set_field_lock(request: FieldLockRequest):
    """Lock/unlock a metadata field against auto enrich/translate/rename."""
    try:
        from core.field_meta import TRACKED_FIELDS
        if request.field not in TRACKED_FIELDS:
            return JSONResponse(
                {"success": False, "error": f"unknown field; allowed: {', '.join(TRACKED_FIELDS)}"},
                status_code=400,
            )
        config = load_config()
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        init_db(db_path)
        repo = VideoRepository(db_path)
        configured_dir_uris, path_mappings = _get_configured_dirs(config)
        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        if not repo.set_field_lock(request.path, request.field, request.locked):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)
        updated = repo.get_by_path(request.path)
        return JSONResponse({
            "success": True,
            "field": request.field,
            "locked": request.locked,
            "field_locks": updated.field_locks if updated else {},
            "video": _serialize_video(
                updated, path_mappings, config.get("thumbnail_cache_enabled", False),
                _get_actress_display_map(),
                _has_title_translation_history(db_path, updated),
            ) if updated else None,
        })
    except Exception as e:
        logger.exception("field lock failed: %s", e)
        return JSONResponse({"success": False, "error": "field lock failed"}, status_code=500)


@router.post("/rollback-translation")
def rollback_translation(request: RollbackTranslationRequest):
    """Restore the latest saved title/original_title pair for one local video."""
    try:
        config = load_config()
        db_path = get_db_path()
        if not db_path.exists():
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        init_db(db_path)
        repo = VideoRepository(db_path)
        configured_dir_uris, path_mappings = _get_configured_dirs(config)

        if not any(is_path_under_dir(request.path, uri) for uri in configured_dir_uris):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        video = repo.get_by_path(request.path)
        if video is None:
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        history = _latest_title_translation_history(db_path, video)
        if not history:
            thumb_enabled = config.get("thumbnail_cache_enabled", False)
            actress_display_map = _get_actress_display_map()
            return JSONResponse({
                "success": True,
                "restored": False,
                "reason": "no_history",
                "video": _serialize_video(video, path_mappings, thumb_enabled, actress_display_map, False),
            })

        old_title = history.get("old_title") or ""
        old_original = history.get("old_original_title") or ""
        if not repo.update_title(video.path, old_title, old_original):
            return JSONResponse({"success": False, "error": "video not found"}, status_code=404)

        nfo_updated = _sync_nfo_title(video.path, old_title, old_original)
        _delete_title_translation_history(db_path, int(history["id"]))

        updated_video = repo.get_by_path(video.path)
        thumb_enabled = config.get("thumbnail_cache_enabled", False)
        actress_display_map = _get_actress_display_map()
        return JSONResponse({
            "success": True,
            "restored": True,
            "title": old_title,
            "original_title": old_original,
            "nfo_updated": nfo_updated,
            "video": _serialize_video(
                updated_video,
                path_mappings,
                thumb_enabled,
                actress_display_map,
                _has_title_translation_history(db_path, updated_video),
            ),
        })
    except Exception as e:
        logger.exception("showcase rollback translation failed: %s", e)
        return JSONResponse({"success": False, "error": "rollback failed"}, status_code=500)


@router.delete("/video")
def delete_video(path: str = Query(..., description="file:/// URI")):
    """從收藏移除單筆影片（71-T7，CD-10 / §1.6）。

    只刪 DB row（repo.delete_by_paths，DB-only）+ 砍衍生縮圖 WebP
    （thumbnail_cache.invalidate）。**絕不 unlink 影片檔或原始封面檔。**

    刻意「無 scope guard」：issue #57 要刪的正是已移出 gallery 設定資料夾的
    stale DB row，那些 path 依定義不在任何 configured dir 下，scope guard 會
    擋掉正當用例。未知 path → delete_by_paths rowcount=0，安全 no-op。

    `def`（非 async）→ Starlette threadpool，body 內 DB / unlink 在 worker thread。
    不進 capabilities（D9）。
    """
    db_path = get_db_path()
    if not db_path.exists():
        return JSONResponse({"deleted": 0})

    init_db(db_path)
    repo = VideoRepository(db_path)

    n = repo.delete_by_paths([path])
    thumbnail_cache.invalidate(path)

    return JSONResponse({"deleted": n})
