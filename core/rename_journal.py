"""Durable append-only journal for recoverable media renames."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from core.atomic_write import atomic_write
from core.database import get_db_path
from core.logger import get_logger


logger = get_logger(__name__)
_journal_lock = threading.RLock()


def journal_path() -> Path:
    return get_db_path().parent / "rename_history.jsonl"


def append_event(event: dict[str, Any]) -> dict[str, Any]:
    entry = dict(event)
    entry.setdefault("id", uuid.uuid4().hex[:12])
    entry.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
    path = journal_path()
    with _journal_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return entry


def list_events(limit: int = 50) -> list[dict[str, Any]]:
    path = journal_path()
    with _journal_lock:
        if not path.is_file():
            return []
        try:
            rows: list[dict[str, Any]] = []
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                if len(rows) >= max(1, min(int(limit), 200)):
                    break
            return rows
        except OSError:
            logger.exception("Could not read rename journal")
            return []


def get_event(event_id: str) -> dict[str, Any] | None:
    return next((item for item in list_events(200) if item.get("id") == event_id), None)


def _rewrite_event(event_id: str, mutator: Callable[[dict[str, Any]], None]) -> bool:
    path = journal_path()
    with _journal_lock:
        if not path.is_file():
            return False
        try:
            output: list[str] = []
            found = False
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    output.append(line)
                    continue
                if isinstance(value, dict) and value.get("id") == event_id:
                    mutator(value)
                    found = True
                output.append(json.dumps(value, ensure_ascii=False))
            if not found:
                return False
            with atomic_write(path, mode="w", encoding="utf-8") as handle:
                handle.write("\n".join(output) + "\n")
            return True
        except OSError:
            logger.exception("Could not update rename journal event %s", event_id)
            return False


def update_event(event_id: str, **changes: Any) -> bool:
    def mutate(event: dict[str, Any]) -> None:
        event.update(changes)
        event["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    return _rewrite_event(event_id, mutate)


def mark_rolled_back(event_id: str) -> bool:
    return update_event(
        event_id,
        status="rolled_back",
        rolled_back=True,
        rolled_back_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def mark_failed(event_id: str, code: str) -> bool:
    return update_event(event_id, status="failed", error_code=code)
