"""Disk cache for /api/proxy-image remote fetches.

Layout mirrors thumbnail_cache: output/proxy-img/<sha1[:2]>/<sha1>
Optional sibling .meta JSON holds content-type.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urldefrag

from core.database import get_db_path
from core.logger import get_logger

logger = get_logger(__name__)

# Soft TTL (seconds). Stale entries are re-fetched but not immediately deleted.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
# Prune when total cache exceeds this (bytes). Best-effort.
MAX_CACHE_BYTES = 512 * 1024 * 1024


def _cache_root() -> Path:
    return get_db_path().parent / "proxy-img"


def cache_paths_for(url: str) -> Tuple[Path, Path]:
    """Return (body_path, meta_path) for a remote image URL."""
    clean, _frag = urldefrag(url or "")
    h = hashlib.sha1(clean.encode("utf-8")).hexdigest()
    base = _cache_root() / h[:2] / h
    return base, Path(str(base) + ".meta")


def get_cached(url: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[Tuple[bytes, str]]:
    """Return (content, content_type) if a fresh cache entry exists."""
    body_path, meta_path = cache_paths_for(url)
    if not body_path.is_file():
        return None
    try:
        age = time.time() - body_path.stat().st_mtime
        if age > ttl_seconds:
            return None
        content_type = "image/jpeg"
        if meta_path.is_file():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            content_type = meta.get("content_type") or content_type
        return body_path.read_bytes(), content_type
    except Exception as exc:
        logger.debug("proxy cache read failed: %s", exc)
        return None


def put_cached(url: str, content: bytes, content_type: str) -> None:
    """Atomically write body + meta for url."""
    if not content or len(content) < 32:
        return
    body_path, meta_path = cache_paths_for(url)
    body_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=body_path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.replace(tmp, body_path)
        tmp = None
        meta_path.write_text(
            json.dumps(
                {
                    "content_type": content_type or "image/jpeg",
                    "url": urldefrag(url)[0],
                    "size": len(content),
                    "saved_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("proxy cache write failed: %s", exc)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def prune_cache(max_bytes: int = MAX_CACHE_BYTES) -> int:
    """Delete oldest cache bodies until under max_bytes. Returns deleted count."""
    root = _cache_root()
    if not root.is_dir():
        return 0
    files: list[tuple[float, Path, int]] = []
    total = 0
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix == ".meta":
            continue
        try:
            st = p.stat()
            files.append((st.st_mtime, p, st.st_size))
            total += st.st_size
        except OSError:
            continue
    if total <= max_bytes:
        return 0
    files.sort(key=lambda x: x[0])  # oldest first
    deleted = 0
    for _mtime, p, size in files:
        if total <= max_bytes:
            break
        try:
            p.unlink(missing_ok=True)
            Path(str(p) + ".meta").unlink(missing_ok=True)
            total -= size
            deleted += 1
        except OSError:
            continue
    return deleted
