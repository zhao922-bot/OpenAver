"""Persistent rename journal for preview → apply → rollback.

Stores JSONL entries under output/rename_history.jsonl.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.database import get_db_path
from core.logger import get_logger

logger = get_logger(__name__)

# Guards append / read / rewrite against in-process concurrent access.
# RLock so nested same-thread calls (e.g. rewrite helpers) never deadlock.
_journal_lock = threading.RLock()


def _journal_path() -> Path:
    return get_db_path().parent / "rename_history.jsonl"


def append_event(event: dict[str, Any]) -> dict[str, Any]:
    """Append a rename event. Injects id + created_at if missing."""
    entry = dict(event)
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    path = _journal_path()
    with _journal_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_events(limit: int = 50) -> list[dict[str, Any]]:
    path = _journal_path()
    with _journal_lock:
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(rows) >= max(1, min(limit, 200)):
                    break
        except OSError as exc:
            logger.warning("rename journal read failed: %s", exc)
        return rows


def get_event(event_id: str) -> Optional[dict[str, Any]]:
    for ev in list_events(limit=200):
        if ev.get("id") == event_id:
            return ev
    return None


def _atomic_write_journal(path: Path, content: str) -> bool:
    """Write journal content atomically via temp file + fsync + os.replace.

    On any failure: preserve the original journal, remove the temp file, return False.
    """
    tmp_path: Optional[str] = None
    fd: Optional[int] = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".rename_journal_",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            fd = None  # ownership transferred to file object
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # File closed; atomically replace the journal
        os.replace(tmp_path, path)
        tmp_path = None  # ownership transferred to destination
        return True
    except Exception as exc:
        logger.warning("rename journal atomic write failed: %s", exc)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


def _rewrite_event(event_id: str, mutator) -> bool:
    """Apply mutator(obj) in-place for matching id; rewrite journal file atomically."""
    path = _journal_path()
    with _journal_lock:
        if not path.is_file():
            return False
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            out = []
            found = False
            for line in lines:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Preserve malformed lines as-is
                    out.append(line)
                    continue
                if obj.get("id") == event_id:
                    mutator(obj)
                    found = True
                out.append(json.dumps(obj, ensure_ascii=False))
            if not found:
                return False
            content = "\n".join(out) + "\n"
            return _atomic_write_journal(path, content)
        except OSError as exc:
            logger.warning("rename journal rewrite failed: %s", exc)
            return False


def mark_rolled_back(event_id: str) -> bool:
    """Rewrite journal marking event as rolled_back (best-effort full rewrite)."""

    def _mut(obj: dict) -> None:
        obj["rolled_back"] = True
        obj["rolled_back_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        obj["status"] = "rolled_back"

    return _rewrite_event(event_id, _mut)


def mark_failed(event_id: str, reason: str = "") -> bool:
    def _mut(obj: dict) -> None:
        obj["status"] = "failed"
        obj["failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if reason:
            obj["fail_reason"] = reason[:500]

    return _rewrite_event(event_id, _mut)


def update_pending_progress(event_id: str, progress: dict[str, Any]) -> bool:
    """Merge progress into a pending journal event after each batch item.

    Used so a mid-batch crash still leaves completed entries recoverable
    (entries / renamed / failed / skipped counters).
    """

    def _mut(obj: dict) -> None:
        for key in ("entries", "renamed", "failed", "skipped", "processed", "status"):
            if key in progress:
                obj[key] = progress[key]
        obj["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    return _rewrite_event(event_id, _mut)


def finalize_event(event_id: str, payload: dict[str, Any]) -> bool:
    """Replace pending event body with completed payload (preserve id/created_at)."""

    def _mut(obj: dict) -> None:
        eid = obj.get("id")
        created = obj.get("created_at")
        obj.clear()
        obj.update(payload)
        obj["id"] = eid
        if created:
            obj["created_at"] = created
        obj["status"] = payload.get("status") or "completed"
        obj["finalized_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    return _rewrite_event(event_id, _mut)
