"""Persistent controllable background tasks for long-running library work."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import uuid

from core.config import DirectoryConfig, load_config
from core.database import VideoRepository, get_db_path
from core.logger import get_logger
from core.path_utils import uri_to_fs_path
from core.readonly_producer import produce_source


logger = get_logger(__name__)
TASKS_PATH = get_db_path().parent / "background_tasks.json"


class TaskManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._tasks = self._load()
        for task in self._tasks.values():
            if task.get("status") in {"running", "cancelling"}:
                task["status"] = "paused"
                task["message"] = "Paused after application restart"
        self._save()

    def _load(self) -> dict:
        if not TASKS_PATH.exists():
            return {}
        try:
            return json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = TASKS_PATH.with_suffix(".tmp")
        temp.write_text(json.dumps(self._tasks, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, TASKS_PATH)

    def list(self) -> list[dict]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda x: x["created_at"], reverse=True)

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def create_readonly(self, payload: dict) -> dict:
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        task = {
            "id": task_id,
            "type": "readonly_produce",
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "processed": 0,
            "created": 0,
            "skipped": 0,
            "failed": 0,
            "payload": payload,
            "message": "Queued",
            "result": None,
        }
        with self._lock:
            self._tasks[task_id] = task
            self._save()
        self._launch(task_id)
        return dict(task)

    def control(self, task_id: str, action: str) -> dict:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if action == "pause" and task["status"] in {"running", "queued"}:
                task["status"] = "pausing"
                task["message"] = "Pausing at the next video"
            elif action == "cancel" and task["status"] not in {"completed", "cancelled", "failed"}:
                task["status"] = "cancelling"
                task["message"] = "Cancelling at the next video"
            elif action == "resume" and task["status"] == "paused":
                task["status"] = "queued"
                task["message"] = "Queued to resume"
            else:
                raise ValueError(f"Cannot {action} task in state {task['status']}")
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()
        if action == "resume":
            self._launch(task_id)
        return dict(task)

    def _launch(self, task_id: str) -> None:
        with self._lock:
            running = self._threads.get(task_id)
            if running and running.is_alive():
                return
            thread = threading.Thread(target=self._run_readonly, args=(task_id,), daemon=True, name=f"openaver-task-{task_id}")
            self._threads[task_id] = thread
            thread.start()

    def _should_abort(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks[task_id]["status"] in {"pausing", "cancelling"}

    def _progress(self, task_id: str, outcome) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task["processed"] += 1
            if outcome.status in {"created", "skipped", "failed"}:
                task[outcome.status] += 1
            task["message"] = f"{outcome.number}: {outcome.status}"
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    def _run_readonly(self, task_id: str) -> None:
        try:
            with self._lock:
                task = self._tasks[task_id]
                task["status"] = "running"
                task["message"] = "Running"
                task["updated_at"] = datetime.now().isoformat(timespec="seconds")
                payload = dict(task["payload"])
                self._save()
            config = load_config()
            scraper = dict(config.get("scraper", {}))
            if payload.get("external_manager") is not None:
                scraper["external_manager"] = payload["external_manager"]
            if payload.get("strm_path_mappings") is not None:
                scraper["strm_path_mappings"] = payload["strm_path_mappings"]
            config["scraper"] = scraper
            source = DirectoryConfig(
                path=payload["source_path"], readonly=True, output_path=payload.get("output_path", "")
            )
            result = produce_source(
                source,
                config,
                VideoRepository(),
                proxy_url=payload.get("proxy_url") or config.get("search", {}).get("proxy_url", ""),
                force=bool(payload.get("force", False)),
                reachable=Path(uri_to_fs_path(source.path)).exists(),
                on_progress=lambda outcome: self._progress(task_id, outcome),
                should_abort=lambda: self._should_abort(task_id),
                strm_mappings_getter=(lambda: load_config().get("scraper", {}).get("strm_path_mappings", {})),
            )
            with self._lock:
                task = self._tasks[task_id]
                if task["status"] == "pausing":
                    task["status"] = "paused"
                    task["message"] = "Paused"
                elif task["status"] == "cancelling":
                    task["status"] = "cancelled"
                    task["message"] = "Cancelled"
                elif result.aborted_reason:
                    task["status"] = "failed"
                    task["message"] = result.aborted_reason
                else:
                    task["status"] = "completed"
                    task["message"] = "Completed"
                task["result"] = asdict(result)
                task["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()
        except Exception as exc:
            logger.exception("Background task %s failed", task_id)
            with self._lock:
                task = self._tasks[task_id]
                task["status"] = "failed"
                task["message"] = str(exc)
                task["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()


task_manager = TaskManager()
