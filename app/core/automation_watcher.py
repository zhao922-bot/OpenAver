"""Polling watcher that queues new code-named videos for reviewed organization."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time

from core.config import iter_gallery_sources, load_config
from core.database import VideoRepository, get_db_path, init_db
from core.logger import get_logger
from core.path_utils import to_file_uri, uri_to_fs_path
from core.scraper import extract_number, search_jav, strip_internal_nfo_keys
from core.video_extensions import ZERO_SIZE_EXTENSIONS, get_video_extensions


logger = get_logger(__name__)
QUEUE_PATH = get_db_path().parent / "automation_queue.json"
_queue_lock = threading.Lock()


def _load_queue() -> dict:
    with _queue_lock:
        if not QUEUE_PATH.exists():
            return {"items": []}
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"items": []}


def _save_queue(data: dict) -> None:
    with _queue_lock:
        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = QUEUE_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, QUEUE_PATH)


def _item_id(path: str) -> str:
    return hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()[:16]


def list_items() -> list[dict]:
    return sorted(_load_queue()["items"], key=lambda x: x.get("detected_at", ""), reverse=True)


def scan_for_new_files(force_stable: bool = False) -> dict:
    config = load_config()
    extensions = get_video_extensions(config) - ZERO_SIZE_EXTENSIONS
    settle = max(5, int(config.get("automation", {}).get("settle_seconds", 30)))
    now = time.time()
    init_db(get_db_path())
    known = {os.path.normcase(uri_to_fs_path(v.path)) for v in VideoRepository().get_all()}
    data = _load_queue()
    by_path = {os.path.normcase(item["path"]): item for item in data["items"]}
    discovered = 0

    for source in iter_gallery_sources(config.get("gallery", {})):
        if source.readonly:
            continue
        root = Path(uri_to_fs_path(source.path))
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in extensions:
                continue
            normalized = os.path.normcase(str(path))
            if normalized in known or normalized in by_path:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if not force_stable and now - stat.st_mtime < settle:
                continue
            number = extract_number(path.name)
            if not number:
                continue
            item = {
                "id": _item_id(str(path)),
                "path": str(path),
                "number": number,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "status": "pending",
                "detected_at": datetime.now().isoformat(timespec="seconds"),
                "preview": None,
                "result": None,
            }
            data["items"].append(item)
            by_path[normalized] = item
            discovered += 1

    _save_queue(data)
    return {"discovered": discovered, "pending": sum(i.get("status") == "pending" for i in data["items"])}


def _find(item_id: str) -> tuple[dict, dict]:
    data = _load_queue()
    item = next((entry for entry in data["items"] if entry.get("id") == item_id), None)
    if item is None:
        raise KeyError(item_id)
    return data, item


def preview_item(item_id: str, refresh: bool = False) -> dict:
    data, item = _find(item_id)
    if item.get("preview") and not refresh:
        return item
    metadata = search_jav(item["number"])
    if not metadata:
        item["preview"] = {"found": False, "error": "metadata_not_found"}
    else:
        clean = strip_internal_nfo_keys(metadata)
        actresses = clean.get("actresses") or clean.get("actor") or []
        if isinstance(actresses, str):
            actresses = [part.strip() for part in actresses.split(",") if part.strip()]
        item["preview"] = {
            "found": True,
            "number": item["number"],
            "title": clean.get("title") or "",
            "actresses": actresses,
            "maker": clean.get("maker") or "",
            "cover": clean.get("cover") or clean.get("cover_url") or "",
            "metadata": clean,
        }
    _save_queue(data)
    return item


def apply_item(item_id: str) -> dict:
    data, item = _find(item_id)
    if not item.get("preview", {}).get("found"):
        item = preview_item(item_id)
        data, item = _find(item_id)
    from web.routers.scraper import ScrapeRequest, scrape_single

    result = scrape_single(ScrapeRequest(
        file_path=item["path"],
        number=item["number"],
        metadata=item["preview"]["metadata"],
    ))
    item["result"] = result
    item["status"] = "completed" if result.get("success") else "failed"
    item["completed_at"] = datetime.now().isoformat(timespec="seconds")
    _save_queue(data)
    return item


def dismiss_item(item_id: str) -> dict:
    data, item = _find(item_id)
    item["status"] = "dismissed"
    _save_queue(data)
    return item


def rollback_item(item_id: str) -> dict:
    data, item = _find(item_id)
    result = item.get("result") or {}
    new_file = Path(result.get("new_filename") or "")
    original = Path(item["path"])
    if item.get("status") != "completed" or not new_file.is_file():
        raise ValueError("No completed organization to roll back")
    if original.exists():
        raise FileExistsError(str(original))

    original.parent.mkdir(parents=True, exist_ok=True)
    old_stem = new_file.stem
    for child in list(new_file.parent.iterdir()):
        if not child.is_file():
            continue
        destination = original.parent / child.name
        if child == new_file:
            destination = original
        elif child.stem == old_stem:
            destination = original.parent / f"{original.stem}{child.suffix}"
        if destination.exists():
            raise FileExistsError(str(destination))
        shutil.move(str(child), str(destination))

    try:
        new_file.parent.rmdir()
    except OSError:
        pass
    repo = VideoRepository()
    repo.update_media_paths(to_file_uri(str(new_file)), to_file_uri(str(original)), mtime=original.stat().st_mtime)
    item["status"] = "rolled_back"
    item["rolled_back_at"] = datetime.now().isoformat(timespec="seconds")
    _save_queue(data)
    return item


class AutomationWatcher:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="openaver-folder-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = load_config().get("automation", {})
                interval = max(15, int(cfg.get("scan_interval_seconds", 60)))
                if cfg.get("watch_enabled", False):
                    scan_for_new_files()
            except Exception:
                logger.exception("Automation watcher scan failed")
                interval = 60
            self._stop.wait(interval)


automation_watcher = AutomationWatcher()
