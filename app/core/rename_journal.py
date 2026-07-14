"""Persistent rename journal for preview → apply → rollback.

Stores JSONL entries under output/rename_history.jsonl.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.database import get_db_path
from core.logger import get_logger

logger = get_logger(__name__)


def _journal_path() -> Path:
    return get_db_path().parent / "rename_history.jsonl"


def append_event(event: dict[str, Any]) -> dict[str, Any]:
    """Append a rename event. Injects id + created_at if missing."""
    entry = dict(event)
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def list_events(limit: int = 50) -> list[dict[str, Any]]:
    path = _journal_path()
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


def _rewrite_event(event_id: str, mutator) -> bool:
    """Apply mutator(obj) in-place for matching id; rewrite journal file."""
    path = _journal_path()
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
                out.append(line)
                continue
            if obj.get("id") == event_id:
                mutator(obj)
                found = True
            out.append(json.dumps(obj, ensure_ascii=False))
        if found:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return found
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
