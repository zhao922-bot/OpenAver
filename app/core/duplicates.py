"""Duplicate video detection: number groups, size groups, optional content fingerprint."""
from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from core.database import Video, VideoRepository, init_db
from core.logger import get_logger
from core.path_utils import uri_to_fs_path

logger = get_logger(__name__)

# Read first + last chunk for a cheap content fingerprint (not cryptographic integrity).
_FINGERPRINT_CHUNK = 1024 * 1024  # 1 MiB


def content_fingerprint(fs_path: str, size: int | None = None) -> Optional[str]:
    """Return sha1 of size + head + tail of file, or None if unreadable."""
    try:
        path = Path(fs_path)
        if not path.is_file():
            return None
        file_size = size if size is not None else path.stat().st_size
        h = hashlib.sha1()
        h.update(str(file_size).encode("ascii"))
        with path.open("rb") as f:
            head = f.read(_FINGERPRINT_CHUNK)
            h.update(head)
            if file_size > _FINGERPRINT_CHUNK * 2:
                f.seek(max(0, file_size - _FINGERPRINT_CHUNK))
                h.update(f.read(_FINGERPRINT_CHUNK))
            elif file_size > _FINGERPRINT_CHUNK:
                # middle remainder
                h.update(f.read())
        return h.hexdigest()[:16]
    except OSError as exc:
        logger.debug("fingerprint failed for %s: %s", fs_path, exc)
        return None


def _video_row(video: Video) -> dict[str, Any]:
    fs = ""
    exists = False
    size = int(video.size_bytes or 0)
    try:
        fs = uri_to_fs_path(video.path)
        if fs and os.path.isfile(fs):
            exists = True
            if size <= 0:
                size = os.path.getsize(fs)
    except Exception:
        pass
    return {
        "path": video.path,
        "number": (video.number or "").upper(),
        "title": video.title or video.original_title or "",
        "size_bytes": size,
        "fs_path": fs,
        "exists": exists,
        "mtime": video.mtime or 0,
    }


def find_duplicates(
    *,
    compute_hash: bool = False,
    min_group_size: int = 2,
    limit_groups: int = 100,
) -> dict[str, Any]:
    """Scan library for duplicate candidates.

    Groups:
      - by_number: same 番号 (case-insensitive), ≥2 videos
      - by_size: same size_bytes (>0), ≥2 videos, not already singleton
      - by_hash: same content fingerprint (optional, only among size groups or all pairs of same size)
    """
    init_db()
    videos = VideoRepository().get_all()
    rows = [_video_row(v) for v in videos]

    by_number: dict[str, list] = defaultdict(list)
    by_size: dict[int, list] = defaultdict(list)
    for row in rows:
        if row["number"]:
            by_number[row["number"]].append(row)
        if row["size_bytes"] > 0:
            by_size[row["size_bytes"]].append(row)

    number_groups = []
    for num, items in by_number.items():
        if len(items) >= min_group_size:
            number_groups.append({
                "key": num,
                "kind": "number",
                "count": len(items),
                "items": items,
            })
    number_groups.sort(key=lambda g: (-g["count"], g["key"]))

    size_groups = []
    for size, items in by_size.items():
        if len(items) >= min_group_size:
            size_groups.append({
                "key": str(size),
                "kind": "size",
                "count": len(items),
                "size_bytes": size,
                "items": items,
            })
    size_groups.sort(key=lambda g: (-g["count"], -g.get("size_bytes", 0)))

    hash_groups = []
    if compute_hash:
        # Only hash files that already share a size (cheap filter) or share a number
        candidates: list[dict] = []
        seen_paths = set()
        for g in size_groups:
            for item in g["items"]:
                if item["path"] not in seen_paths and item["exists"]:
                    candidates.append(item)
                    seen_paths.add(item["path"])
        for g in number_groups:
            for item in g["items"]:
                if item["path"] not in seen_paths and item["exists"]:
                    candidates.append(item)
                    seen_paths.add(item["path"])

        by_fp: dict[str, list] = defaultdict(list)
        for item in candidates:
            fp = content_fingerprint(item["fs_path"], item["size_bytes"])
            if not fp:
                continue
            item = dict(item, fingerprint=fp)
            by_fp[fp].append(item)
        for fp, items in by_fp.items():
            if len(items) >= min_group_size:
                hash_groups.append({
                    "key": fp,
                    "kind": "hash",
                    "count": len(items),
                    "fingerprint": fp,
                    "size_bytes": items[0].get("size_bytes", 0),
                    "items": items,
                })
        hash_groups.sort(key=lambda g: (-g["count"], g["key"]))

    return {
        "success": True,
        "summary": {
            "total_videos": len(rows),
            "number_duplicate_groups": len(number_groups),
            "size_duplicate_groups": len(size_groups),
            "hash_duplicate_groups": len(hash_groups),
            "number_duplicate_videos": sum(g["count"] for g in number_groups),
            "compute_hash": compute_hash,
        },
        "by_number": number_groups[:limit_groups],
        "by_size": size_groups[:limit_groups],
        "by_hash": hash_groups[:limit_groups],
    }
