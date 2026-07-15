"""
Local sample-image (剧照 / extrafanart) helpers.

Central, unit-testable checks for "has valid local stills" and candidate scan
for batch fill. Disk files are authoritative; remote http(s) URLs never count
as local presence.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from core.logger import get_logger
from core.path_utils import is_path_under_dir, to_file_uri, uri_to_fs_path

logger = get_logger(__name__)

# Common still image suffixes accepted as valid local samples.
SAMPLE_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def count_direct_videos_on_disk(
    folder_fs_path: str,
    *,
    video_extensions: Optional[Set[str]] = None,
    config: Optional[dict] = None,
) -> int:
    """Count non-recursive video files directly under ``folder_fs_path``.

    Uses ``scraper.video_extensions`` via :func:`core.video_extensions.get_video_extensions`
    (includes configured extensions such as ``.strm``).

    Raises ``OSError`` (or other I/O errors) on directory list/stat failure so
    callers can fail-closed. Does **not** catch and return 0/1.
    """
    if video_extensions is None:
        from core.video_extensions import get_video_extensions

        if config is None:
            # Prefer live scraper.video_extensions; fall back to defaults if
            # config cannot be loaded (deps/env). Config-load failure is not a
            # directory-list error — do not treat it as multi via this path.
            try:
                from core.config import load_config

                config = load_config()
            except Exception as e:
                logger.debug(
                    "count_direct_videos_on_disk: load_config failed (%s); using defaults",
                    e,
                )
                config = {}
        video_extensions = get_video_extensions(config or {})

    folder = Path(folder_fs_path)
    if not folder.is_dir():
        raise OSError(f"not a directory: {folder_fs_path}")

    count = 0
    # iterdir() / is_file() may raise; let it propagate (fail-closed upstream).
    for child in folder.iterdir():
        try:
            is_file = child.is_file()
        except OSError:
            raise
        if not is_file:
            continue
        if child.suffix.lower() in video_extensions:
            count += 1
    return count


def check_multi_video_folder(
    repo,
    folder_uri_prefix: str,
    *,
    config: Optional[dict] = None,
) -> Tuple[bool, int, Optional[str]]:
    """Decide whether a folder is multi-video (shared extrafanart unsafe).

    Checks **both**:
    - DB direct-child video count (``repo.count_videos_in_folder``)
    - On-disk direct video files using configured ``scraper.video_extensions``

    Either count ``> 1`` → multi. Any count/list exception is **fail-closed**
    (treated as multi; never assume single-video / count=1).

    Returns ``(is_multi, effective_count, error_code)``.
    ``effective_count`` is ``max(db, disk)`` when both succeed, else ``-1`` on
    failure. ``error_code`` is set only on fail-closed exception paths.
    """
    if not folder_uri_prefix or not str(folder_uri_prefix).endswith("/"):
        logger.warning(
            "check_multi_video_folder: invalid prefix %r — fail-closed multi",
            folder_uri_prefix,
        )
        return True, -1, "invalid_folder_prefix"

    try:
        db_count = int(repo.count_videos_in_folder(folder_uri_prefix))
    except Exception as e:
        logger.warning(
            "check_multi_video_folder: DB count failed (%s): %s: %s — fail-closed multi",
            folder_uri_prefix,
            type(e).__name__,
            e,
        )
        return True, -1, "db_count_failed"

    try:
        folder_uri = folder_uri_prefix.rstrip("/")
        folder_fs = uri_to_fs_path(folder_uri)
        disk_count = count_direct_videos_on_disk(folder_fs, config=config)
    except Exception as e:
        logger.warning(
            "check_multi_video_folder: disk list failed (%s): %s: %s — fail-closed multi",
            folder_uri_prefix,
            type(e).__name__,
            e,
        )
        return True, -1, "disk_list_failed"

    effective = max(db_count, disk_count)
    return effective > 1, effective, None


def looks_like_image_bytes(data: bytes) -> bool:
    """True if *data* starts with a known still-image signature (JPEG/PNG/WebP/GIF/BMP).

    Rejects HTML, random bytes, and other non-image payloads that could otherwise
    be treated as valid stills after a partial/corrupt write.
    """
    if not data or len(data) < 3:
        return False
    # JPEG
    if data[:2] == b"\xff\xd8":
        return True
    # PNG
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    # WebP: RIFF....WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    # GIF (not in SAMPLE_IMAGE_EXTENSIONS, but useful for download validation)
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    # BMP
    if data[:2] == b"BM":
        return True
    return False


def is_valid_local_image_file(fs_path: str | Path) -> bool:
    """True if path exists as a regular non-empty image with known suffix + magic."""
    try:
        p = Path(fs_path)
    except (TypeError, ValueError):
        return False
    if p.suffix.lower() not in SAMPLE_IMAGE_EXTENSIONS:
        return False
    try:
        if not p.is_file() or p.stat().st_size <= 0:
            return False
        with open(p, "rb") as f:
            head = f.read(16)
        return looks_like_image_bytes(head)
    except OSError:
        return False


def list_extrafanart_local_images(video_fs_path: str) -> List[str]:
    """Return sorted FS paths of valid images under ``<video_dir>/extrafanart/``."""
    try:
        parent = Path(video_fs_path).parent
    except (TypeError, ValueError):
        return []
    ef_dir = parent / "extrafanart"
    if not ef_dir.is_dir():
        return []
    found: List[str] = []
    try:
        for child in sorted(ef_dir.iterdir()):
            if is_valid_local_image_file(child):
                found.append(str(child))
    except OSError as e:
        logger.debug("extrafanart list failed (%s): %s", ef_dir, e)
        return []
    return found


def local_sample_fs_paths_from_db(sample_images: Optional[Sequence[str]]) -> List[str]:
    """Resolve DB ``sample_images`` entries that point to valid local image files.

    Remote http(s) URLs are ignored. ``file:///`` URIs and absolute FS paths
    are accepted when the target file is a valid non-empty local image.
    """
    out: List[str] = []
    for entry in sample_images or []:
        if not isinstance(entry, str) or not entry.strip():
            continue
        uri = entry.strip()
        if uri.startswith("http://") or uri.startswith("https://"):
            continue
        fs: Optional[str] = None
        if uri.startswith("file:///"):
            try:
                fs = uri_to_fs_path(uri)
            except Exception:
                continue
        elif os.path.isabs(uri):
            fs = uri
        else:
            continue
        if fs and is_valid_local_image_file(fs):
            out.append(fs)
    return out


def has_valid_local_samples(
    video_fs_path: str,
    sample_images_db: Optional[Sequence[str]] = None,
) -> bool:
    """True if extrafanart has valid images or DB points at valid local files."""
    if list_extrafanart_local_images(video_fs_path):
        return True
    if local_sample_fs_paths_from_db(sample_images_db):
        return True
    return False


def extrafanart_uris_from_disk(video_fs_path: str) -> List[str]:
    """file:/// URIs for every valid image currently under extrafanart/."""
    return [to_file_uri(p) for p in list_extrafanart_local_images(video_fs_path)]


def reconcile_sample_images_from_disk(repo, video) -> bool:
    """If disk extrafanart has valid images and DB differs, sync sample_images only.

    Disk is source of truth for extrafanart. Does not touch any other DB field
    and never hits the network. Returns True when the DB row was updated.
    """
    try:
        fs_path = uri_to_fs_path(video.path)
    except Exception:
        return False
    disk_uris = extrafanart_uris_from_disk(fs_path)
    if not disk_uris:
        return False

    current = list(video.sample_images or [])
    # Already matches disk (order-insensitive, same multiset).
    if len(current) == len(disk_uris) and set(current) == set(disk_uris):
        return False

    try:
        repo.update_sample_images(video.path, disk_uris)
        return True
    except Exception as e:
        logger.warning(
            "reconcile sample_images failed for %s: %s: %s",
            video.path,
            type(e).__name__,
            e,
        )
        return False


def _video_fs_path(video) -> Optional[str]:
    try:
        fs = uri_to_fs_path(video.path)
    except Exception:
        return None
    if not fs or not os.path.isfile(fs):
        return None
    return fs


def _folder_uri_prefix(video_fs_path: str) -> str:
    return to_file_uri(os.path.dirname(video_fs_path)) + "/"


def scan_missing_samples(
    repo,
    dir_uris: Sequence[str],
    *,
    config: Optional[dict] = None,
) -> Dict[str, Any]:
    """Scan DB for videos that lack valid local stills inside configured dirs.

    Fail-closed: empty ``dir_uris`` returns success=False and zero items
    (never falls back to the whole library).

    Multi-video folders are counted as ``skipped_multi`` and never become
    fetch candidates (shared extrafanart must not be polluted). Detection uses
    :func:`check_multi_video_folder` (DB **and** on-disk extensions, fail-closed).

    When extrafanart already has valid images but DB is stale/empty, reconciles
    ``sample_images`` only (no network).
    """
    if not dir_uris:
        return {
            "success": False,
            "error": "no_configured_dirs",
            "count": 0,
            "items": [],
            "skipped_multi": 0,
            "reconciled": 0,
        }

    items: List[Dict[str, str]] = []
    skipped_multi = 0
    reconciled = 0
    # Cache multi determination per folder (shared with batch preflight rules).
    folder_is_multi: Dict[str, bool] = {}

    for video in repo.get_all():
        path = getattr(video, "path", None) or ""
        if not path:
            continue
        if not any(is_path_under_dir(path, uri) for uri in dir_uris):
            continue
        number = (getattr(video, "number", None) or "").strip()
        if not number:
            continue

        fs_path = _video_fs_path(video)
        if not fs_path:
            continue

        folder_prefix = _folder_uri_prefix(fs_path)
        if folder_prefix not in folder_is_multi:
            is_multi, _count, _err = check_multi_video_folder(
                repo, folder_prefix, config=config
            )
            folder_is_multi[folder_prefix] = is_multi
        if folder_is_multi[folder_prefix]:
            if not has_valid_local_samples(fs_path, video.sample_images):
                skipped_multi += 1
            continue

        # Disk has stills → reconcile DB if needed; not a missing candidate.
        disk_images = list_extrafanart_local_images(fs_path)
        if disk_images:
            if reconcile_sample_images_from_disk(repo, video):
                reconciled += 1
            continue

        if has_valid_local_samples(fs_path, video.sample_images):
            continue

        items.append({"path": path, "number": number})

    return {
        "success": True,
        "count": len(items),
        "items": items,
        "skipped_multi": skipped_multi,
        "reconciled": reconciled,
    }


def resolve_batch_sample_targets(
    *,
    repo,
    dir_uris: Sequence[str],
    items: Optional[Sequence[Any]] = None,
    paths: Optional[Sequence[str]] = None,
    max_items: int = 500,
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Validate client selection for batch-fetch-samples.

    Accepts ``items`` (dicts with path/file_path) and/or ``paths``.
    Dedupes by path, caps count, requires configured dirs, rejects outside
    whitelist / not-in-DB. Number always comes from DB (client number ignored).

    Returns (accepted, error_message). On error accepted is empty.
    """
    if not dir_uris:
        return [], "no_configured_dirs"

    raw_paths: List[str] = []
    if items:
        for it in items:
            if isinstance(it, dict):
                p = it.get("path") or it.get("file_path") or ""
            else:
                p = getattr(it, "path", None) or getattr(it, "file_path", None) or ""
            if isinstance(p, str) and p.strip():
                raw_paths.append(p.strip())
    if paths:
        for p in paths:
            if isinstance(p, str) and p.strip():
                raw_paths.append(p.strip())

    if not raw_paths:
        return [], "empty_selection"

    seen = set()
    deduped: List[str] = []
    for p in raw_paths:
        if p in seen:
            continue
        seen.add(p)
        deduped.append(p)

    if len(deduped) > max_items:
        return [], f"too_many:{max_items}:{len(deduped)}"

    accepted: List[Dict[str, str]] = []
    for p in deduped:
        video = repo.get_by_path(p)
        if video is None:
            # Try coerce: some clients send FS path instead of file URI
            try:
                from core.path_utils import coerce_to_file_uri

                alt = coerce_to_file_uri(p)
                if alt != p:
                    video = repo.get_by_path(alt)
                    if video is not None:
                        p = alt
            except Exception:
                pass
        if video is None:
            return [], "not_in_db"
        if not any(is_path_under_dir(video.path, uri) for uri in dir_uris):
            return [], "outside_library"
        number = (video.number or "").strip()
        if not number:
            return [], "missing_number"
        accepted.append({"path": video.path, "number": number})

    if not accepted:
        return [], "empty_selection"
    return accepted, None
