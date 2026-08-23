"""Preview, apply, recover, and roll back metadata-based media renames."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET

from core.actress_names import (
    build_actress_display_map,
    display_actress_name,
    load_actress_alias_groups,
)
from core.atomic_write import atomic_write
from core.config import load_config
from core.database import VideoRepository, get_db_path
from core.db_inflow import try_inflow_upsert
from core.logger import get_logger
from core.nfo_utils import sanitize_nfo_bytes
from core.organizer import sanitize_filename
from core.path_utils import to_file_uri, uri_to_local_fs_path
from core import rename_journal, thumbnail_cache
from core.title_translation import repath_translation_history
from core.video_extensions import get_video_extensions


logger = get_logger(__name__)
MAX_BASENAME_LENGTH = 220


class RenameError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _clean_part(value: str, limit: int) -> str:
    cleaned = sanitize_filename(value or "").strip(" .")
    return cleaned[:limit].rstrip(" .")


def _strip_number_prefix(title: str, number: str) -> str:
    return re.sub(
        rf"^\s*{re.escape(number)}\s*(?:[-_ ]+\s*)?",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()


def _preserved_suffix(stem: str) -> str:
    configured = load_config().get("scraper", {}).get("suffix_keywords", [])
    candidates = list(configured) + ["-c", "-chs", "-cht", "-sub"]
    lowered = stem.casefold()
    for suffix in sorted({str(item) for item in candidates if item}, key=len, reverse=True):
        if lowered.endswith(suffix.casefold()):
            return stem[-len(suffix):]
    return ""


def build_video_basename(video, current_stem: str = "") -> str:
    number = _clean_part(video.number or current_stem, 64)
    source_title = (video.original_title or video.title or "").strip()
    title = _clean_part(_strip_number_prefix(source_title, number), 135)
    groups = load_actress_alias_groups()
    display_map = build_actress_display_map(groups)
    actors: list[str] = []
    source_compact = re.sub(r"\s+", "", source_title)
    for raw_name in video.actresses or []:
        display_name = display_actress_name(raw_name, display_map)
        known_group = next((names for primary, names in groups if primary == display_name), [raw_name])
        if any(re.sub(r"\s+", "", name) in source_compact for name in known_group if name):
            continue
        if display_name and display_name not in actors:
            actors.append(_clean_part(display_name, 40))

    pieces = [number]
    if title and title.casefold() != number.casefold():
        pieces.append(title)
    pieces.extend(actor for actor in actors if actor)
    suffix = _preserved_suffix(current_stem)
    result = " - ".join(pieces[:2]) if len(pieces) >= 2 else pieces[0]
    if len(pieces) > 2:
        result = f"{result} {' '.join(pieces[2:])}"
    budget = max(1, MAX_BASENAME_LENGTH - len(suffix))
    return _clean_part(result, budget) + suffix


def _fit_basename(basename: str, old_path: Path) -> str:
    """Keep both the renamed directory and file below conservative path limits."""
    fixed_length = len(str(old_path.parent.parent)) + len(old_path.suffix) + 3
    max_for_path = (240 - fixed_length) // 2
    limit = min(MAX_BASENAME_LENGTH, max_for_path)
    if limit < 24:
        raise RenameError("path_too_long")
    fitted = _clean_part(basename, limit)
    if not fitted:
        raise RenameError("metadata_incomplete")
    return fitted


def _linked_files(video_path: Path) -> list[Path]:
    prefix = video_path.stem
    result = []
    for item in video_path.parent.iterdir():
        if not item.is_file():
            continue
        if item.name == video_path.name:
            result.append(item)
            continue
        if item.name.startswith(prefix) and item.name[len(prefix):len(prefix) + 1] in {".", "-", "_"}:
            result.append(item)
    return sorted(result, key=lambda item: item.name.casefold())


def plan_video_rename(video, path_mappings: dict, *, rename_folder: bool = True) -> dict[str, Any]:
    old_path = Path(uri_to_local_fs_path(video.path, path_mappings)).resolve()
    if not old_path.is_file():
        raise RenameError("file_not_found")
    extensions = get_video_extensions(load_config())
    videos = [
        item for item in old_path.parent.iterdir()
        if item.is_file() and item.suffix.casefold() in extensions
    ]
    if len(videos) != 1:
        raise RenameError("multiple_videos")

    new_base = _fit_basename(build_video_basename(video, old_path.stem), old_path)
    if not new_base:
        raise RenameError("metadata_incomplete")
    old_dir = old_path.parent
    new_dir = old_dir.with_name(new_base) if rename_folder else old_dir
    linked = _linked_files(old_path)
    moves = []
    for source in linked:
        tail = source.name[len(old_path.stem):]
        destination = new_dir / f"{new_base}{tail}"
        moves.append({"from": str(source), "to": str(destination)})
    new_path = new_dir / f"{new_base}{old_path.suffix}"
    renamed = str(old_path) != str(new_path)
    return {
        "renamed": renamed,
        "reason": "rename_required" if renamed else "already_named",
        "old_uri": video.path,
        "new_uri": to_file_uri(str(new_path), path_mappings),
        "old_path": str(old_path),
        "new_path": str(new_path),
        "old_dir": str(old_dir),
        "new_dir": str(new_dir),
        "new_base": new_base,
        "folder_renamed": old_dir != new_dir,
        "file_moves": moves,
        "number": video.number or "",
    }


def _validate_plan_targets(plan: dict[str, Any]) -> None:
    old_dir = Path(plan["old_dir"])
    new_dir = Path(plan["new_dir"])
    if new_dir != old_dir and new_dir.exists():
        raise RenameError("target_exists")
    sources = {str(Path(move["from"]).resolve()).casefold() for move in plan["file_moves"]}
    for move in plan["file_moves"]:
        source_key = str(Path(move["from"]).resolve()).casefold()
        target_in_old_dir = old_dir / Path(move["to"]).name
        key = str(target_in_old_dir.resolve()).casefold()
        if target_in_old_dir.exists() and key != source_key:
            raise RenameError("target_exists")
        if key in sources and key != source_key:
            raise RenameError("target_exists")


def _nfo_snapshot(plan: dict[str, Any]) -> str | None:
    nfo_path = Path(plan["old_path"]).with_suffix(".nfo")
    try:
        return nfo_path.read_text(encoding="utf-8") if nfo_path.is_file() else None
    except (OSError, UnicodeError):
        raise RenameError("nfo_read_failed") from None


def _rewrite_nfo_images(video_path: Path, moves: list[dict[str, str]]) -> None:
    nfo_path = video_path.with_suffix(".nfo")
    if not nfo_path.is_file():
        return
    try:
        root = ET.fromstring(sanitize_nfo_bytes(nfo_path.read_bytes()))
        renamed_files = {
            Path(move["from"]).name: Path(move["to"]).name
            for move in moves
            if Path(move["from"]).name != Path(move["to"]).name
        }
        changed = False
        for node in root.iter():
            current = (node.text or "").strip()
            replacement = renamed_files.get(current)
            if replacement:
                node.text = replacement
                changed = True
        if not changed:
            return
        ET.indent(root, space="  ")
        with atomic_write(nfo_path, mode="w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0" encoding="utf-8"?>\n')
            handle.write(ET.tostring(root, encoding="unicode"))
    except (OSError, ET.ParseError):
        raise RenameError("nfo_write_failed") from None


def _invalidate_thumbnails(plan: dict[str, Any]) -> None:
    for uri in (plan.get("old_uri", ""), plan.get("new_uri", "")):
        if not uri:
            continue
        try:
            thumbnail_cache.invalidate(uri)
        except Exception:
            logger.exception("Thumbnail invalidation failed for %s", uri)


def _restore_library_path(plan: dict[str, Any]) -> bool:
    old_path = Path(plan["old_path"])
    new_path = Path(plan["new_path"])
    repo = VideoRepository()
    old_row = repo.get_by_path(plan["old_uri"])
    new_row = repo.get_by_path(plan["new_uri"])
    if old_row is not None and new_row is None:
        repath_translation_history(repo.db_path, plan["new_uri"], plan["old_uri"])
        return True
    if not old_path.is_file():
        return False
    result = try_inflow_upsert(str(old_path), str(new_path))
    restored = result == "synced" and repo.get_by_path(plan["old_uri"]) is not None
    if restored:
        repath_translation_history(repo.db_path, plan["new_uri"], plan["old_uri"])
    return restored


def _reverse_filesystem(plan: dict[str, Any], *, restore_nfo: bool = True) -> None:
    old_dir = Path(plan["old_dir"])
    new_dir = Path(plan["new_dir"])
    if new_dir != old_dir and new_dir.exists() and not old_dir.exists():
        new_dir.rename(old_dir)
    for move in reversed(plan["file_moves"]):
        source = Path(move["from"])
        current = old_dir / Path(move["to"]).name
        if current.exists() and not source.exists():
            current.rename(source)
    if restore_nfo and plan.get("nfo_before") is not None:
        nfo_path = Path(plan["old_path"]).with_suffix(".nfo")
        with atomic_write(nfo_path, mode="w", encoding="utf-8") as handle:
            handle.write(plan["nfo_before"])


def apply_video_rename(
    video,
    path_mappings: dict,
    *,
    rename_folder: bool = True,
    expected_new_path: str | None = None,
) -> dict[str, Any]:
    plan = plan_video_rename(video, path_mappings, rename_folder=rename_folder)
    if expected_new_path is not None and plan["new_path"] != expected_new_path:
        raise RenameError("preview_stale")
    if not plan["renamed"]:
        return plan
    _validate_plan_targets(plan)
    plan["nfo_before"] = _nfo_snapshot(plan)
    event = rename_journal.append_event({
        "kind": "video_rename",
        "status": "pending",
        "entries": [plan],
    })
    event_id = event["id"]
    old_dir = Path(plan["old_dir"])
    new_dir = Path(plan["new_dir"])
    filesystem_changed = False
    library_synced = False
    try:
        for move in plan["file_moves"]:
            source = Path(move["from"])
            target = old_dir / Path(move["to"]).name
            if source != target:
                source.rename(target)
                filesystem_changed = True
        if new_dir != old_dir:
            old_dir.rename(new_dir)
            filesystem_changed = True
        new_path = Path(plan["new_path"])
        _rewrite_nfo_images(new_path, plan["file_moves"])
        if try_inflow_upsert(str(new_path), plan["old_path"]) != "synced":
            raise RenameError("library_sync_failed")
        library_synced = True
        repath_translation_history(
            get_db_path(),
            plan["old_uri"],
            plan["new_uri"],
        )
        _invalidate_thumbnails(plan)
        if not rename_journal.update_event(event_id, status="completed"):
            logger.error("Rename completed but journal event %s could not be finalized", event_id)
            return {
                **plan,
                "journal_id": event_id,
                "status": "completed",
                "journal_status": "pending",
                "warning": "journal_finalize_failed",
            }
        return {**plan, "journal_id": event_id, "status": "completed"}
    except Exception as exc:
        logger.exception("Media rename failed; restoring filesystem")
        restored = not filesystem_changed
        try:
            _reverse_filesystem(plan)
            restored = Path(plan["old_path"]).is_file()
        except Exception:
            logger.exception("Automatic media rename rollback failed")
            restored = False
        if restored and library_synced:
            restored = _restore_library_path(plan)
        elif restored:
            repo = VideoRepository()
            if repo.get_by_path(plan["new_uri"]) is not None:
                restored = _restore_library_path(plan)
        _invalidate_thumbnails(plan)
        code = exc.code if isinstance(exc, RenameError) else "rename_failed"
        rename_journal.update_event(
            event_id,
            status="failed" if restored else "recovery_required",
            error_code=code,
            automatically_restored=restored,
        )
        if not restored:
            raise RenameError("recovery_required") from None
        raise RenameError(code) from None


def rollback_video_rename(event: dict[str, Any], path_mappings: dict) -> dict[str, Any]:
    if event.get("status") == "rolled_back":
        raise RenameError("already_rolled_back")
    entries = event.get("entries") or []
    if len(entries) != 1:
        raise RenameError("invalid_journal")
    plan = entries[0]
    old_path = Path(plan["old_path"])
    new_path = Path(plan["new_path"])
    if old_path.exists() and new_path.exists():
        raise RenameError("target_exists")
    if old_path.exists() and not new_path.exists():
        if not _restore_library_path(plan):
            raise RenameError("library_sync_failed")
        finalized = rename_journal.mark_rolled_back(event["id"])
        result = {"restored": True, "already_restored": True, "path": str(old_path)}
        if not finalized:
            result.update(journal_status="pending", warning="journal_finalize_failed")
        return result
    try:
        _reverse_filesystem(plan)
        if not old_path.is_file():
            raise RenameError("rollback_verify_failed")
        if not _restore_library_path(plan):
            raise RenameError("library_sync_failed")
        _invalidate_thumbnails(plan)
        finalized = rename_journal.mark_rolled_back(event["id"])
        result = {"restored": True, "already_restored": False, "path": str(old_path)}
        if not finalized:
            result.update(journal_status="pending", warning="journal_finalize_failed")
        return result
    except RenameError:
        raise
    except Exception:
        logger.exception("Media rename rollback failed")
        raise RenameError("rollback_failed") from None
