"""Authorized direct-media downloads with persistent task state.

The downloader deliberately accepts media URLs only. It does not discover,
scrape, or bypass access controls on source websites. Creation is further
restricted by the API router to configured writable library directories.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid

import httpx

from core.atomic_write import atomic_replace_file, atomic_write
from core.config import load_config, mutate_config, normalize_external_manager
from core.database import get_db_path
from core.db_inflow import try_inflow_upsert
from core.logger import get_logger
from core.organizer import download_image, generate_nfo, sanitize_filename


logger = get_logger(__name__)
INSTALL_ROOT = Path(__file__).resolve().parents[1]
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
TERMINAL_STATES = frozenset({"completed", "cancelled", "failed"})
ACTIVE_STATES = frozenset({"queued", "probing", "running", "paused", "cancelling"})
MAX_REDIRECTS = 5


class DownloadValidationError(ValueError):
    """Expected validation failure represented by a stable client-safe code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _safe_url_for_display(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


def _redact_urls(value: str) -> str:
    def replace(match: re.Match) -> str:
        raw = match.group(0).rstrip(".,;)]")
        return _safe_url_for_display(raw)

    return re.sub(r"https?://[^\s]+", replace, value or "")


def _parse_direct_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise DownloadValidationError("invalid_url") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DownloadValidationError("invalid_url")
    if parsed.username or parsed.password or port == 0:
        raise DownloadValidationError("invalid_url")


def _validate_network_target(url: str, *, allow_private: bool = False) -> None:
    _parse_direct_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    host_lower = host.lower()
    try:
        ipaddress.ip_address(host_lower)
    except ValueError:
        if "." not in host_lower or host_lower.endswith((".localhost", ".local")):
            raise DownloadValidationError("blocked_target") from None
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except OSError:
        raise DownloadValidationError("resolve_failed") from None
    if allow_private:
        return
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise DownloadValidationError("resolve_failed") from None
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            raise DownloadValidationError("blocked_target")


def _media_request_headers(referer: str = "", extra: dict | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    }
    if referer:
        headers["Referer"] = referer
    if extra:
        headers.update(extra)
    return headers


def _validated_referer(value: str) -> str:
    """Return a header-safe HTTP referer, or an empty string for bad metadata."""

    referer = (value or "").strip()
    if not referer or "\r" in referer or "\n" in referer:
        return ""
    try:
        _parse_direct_url(referer)
    except DownloadValidationError:
        return ""
    return referer


def _status_error(status: int) -> DownloadValidationError:
    if status in {401, 403}:
        return DownloadValidationError("http_403")
    if status == 404:
        return DownloadValidationError("http_404")
    if status == 410:
        return DownloadValidationError("link_expired")
    return DownloadValidationError("http_error")


def validate_direct_media_url(
    url: str,
    *,
    allow_private: bool = False,
    referer: str = "",
) -> dict[str, str]:
    """Probe a direct URL and reject HTML, unsafe redirects, and unknown data."""

    current = url
    headers = _media_request_headers(referer, {"Range": "bytes=0-65535"})
    try:
        with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            for redirect_count in range(MAX_REDIRECTS + 1):
                _validate_network_target(current, allow_private=allow_private)
                with client.stream("GET", current, headers=headers) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        location = response.headers.get("location", "")
                        if not location or redirect_count >= MAX_REDIRECTS:
                            raise DownloadValidationError("unsafe_redirect")
                        current = urljoin(current, location)
                        continue
                    if status >= 400:
                        raise _status_error(status)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= 65536:
                            break
                    prefix = b"".join(chunks)[:65536]
                    break
            else:  # pragma: no cover - loop is bounded and exits above
                raise DownloadValidationError("unsafe_redirect")
    except DownloadValidationError:
        raise
    except httpx.TimeoutException:
        raise DownloadValidationError("timeout") from None
    except httpx.HTTPError:
        raise DownloadValidationError("network_error") from None

    text_prefix = prefix.lstrip()[:512].upper()
    if content_type in {"text/html", "application/xhtml+xml"} or b"<HTML" in text_prefix:
        raise DownloadValidationError("not_media")
    is_hls = b"#EXTM3U" in text_prefix or content_type in {
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
    }
    is_video = content_type.startswith("video/") or content_type in {
        "application/octet-stream",
        "binary/octet-stream",
    }
    if not is_hls and not is_video:
        raise DownloadValidationError("unsupported_media")
    return {"kind": "hls" if is_hls else "video", "content_type": content_type}


def _tool_candidates(name: str) -> list[Path]:
    suffix = ".exe" if os.name == "nt" else ""
    return [
        INSTALL_ROOT / "tools" / f"{name}{suffix}",
        Path.home() / "OpenAver" / "tools" / f"{name}{suffix}",
    ]


def _tool_path(name: str, error_code: str) -> str:
    bundled = next((str(path) for path in _tool_candidates(name) if path.is_file()), "")
    resolved = bundled or shutil.which(name) or ""
    if not resolved:
        raise DownloadValidationError(error_code)
    return resolved


def _ffmpeg_path() -> str:
    return _tool_path("ffmpeg", "ffmpeg_missing")


def _yt_dlp_path() -> str:
    return _tool_path("yt-dlp", "engine_missing")


def _suspend_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, process.pid)
        if not handle:
            raise OSError("pause_failed")
        try:
            if ctypes.windll.ntdll.NtSuspendProcess(handle) != 0:
                raise OSError("pause_failed")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        import signal

        os.kill(process.pid, signal.SIGSTOP)


def _resume_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, process.pid)
        if not handle:
            raise OSError("resume_failed")
        try:
            if ctypes.windll.ntdll.NtResumeProcess(handle) != 0:
                raise OSError("resume_failed")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        import signal

        os.kill(process.pid, signal.SIGCONT)


class ResizableLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._active = 0
        self._condition = threading.Condition()

    @contextmanager
    def slot(self):
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait(timeout=0.5)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def resize(self, limit: int) -> None:
        with self._condition:
            self._limit = max(1, int(limit))
            self._condition.notify_all()

    @property
    def active(self) -> int:
        with self._condition:
            return self._active


def _error_code(message: str) -> str:
    lowered = (message or "").lower()
    if "403" in lowered or "forbidden" in lowered:
        return "http_403"
    if "404" in lowered:
        return "http_404"
    if "expired" in lowered or ("token" in lowered and "403" in lowered):
        return "link_expired"
    if "no space" in lowered or "disk full" in lowered or "errno 28" in lowered:
        return "disk_full"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "resolve" in lowered or "getaddrinfo" in lowered or "network" in lowered:
        return "network_error"
    return "download_failed"


class MediaDownloadManager:
    """Thread-backed persistent download queue."""

    def __init__(self, state_path: Path | None = None, *, allow_private_urls: bool = False) -> None:
        self.state_path = Path(state_path or (get_db_path().parent / "download_tasks.json"))
        self.allow_private_urls = allow_private_urls
        self._lock = threading.RLock()
        configured = load_config().get("download", {})
        def bounded_int(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(configured.get(key, default))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        self._settings = {
            "max_concurrent_downloads": bounded_int("max_concurrent_downloads", 4, 1, 8),
            "fragment_threads": bounded_int("fragment_threads", 16, 1, 64),
            "retry_count": bounded_int("retry_count", 5, 0, 20),
            "request_timeout_seconds": bounded_int("request_timeout_seconds", 30, 5, 300),
        }
        self._limiter = ResizableLimiter(self._settings["max_concurrent_downloads"])
        self._tasks = self._load()
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._last_persist: dict[str, float] = {}
        changed = False
        for task in self._tasks.values():
            if task.get("status") in {"probing", "running", "paused", "cancelling"}:
                task["status"] = "paused"
                task["message"] = "interrupted"
                changed = True
        if changed:
            self._save_locked()

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Download state could not be loaded", exc_info=True)
            return {}

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(self.state_path, mode="w", encoding="utf-8") as handle:
            json.dump(self._tasks, handle, ensure_ascii=False, indent=2)
        try:
            self.state_path.chmod(0o600)
        except OSError:
            logger.debug("Could not tighten download state permissions", exc_info=True)

    def _public(self, task: dict) -> dict:
        result = dict(task)
        payload = dict(result.get("payload", {}))
        for key in ("media_url", "source_page_url", "cover"):
            payload[key] = _safe_url_for_display(payload.get(key, ""))
        result["payload"] = payload
        result.setdefault("eta_seconds", None)
        return result

    def settings(self) -> dict:
        try:
            engine_path = _yt_dlp_path()
        except DownloadValidationError:
            engine_path = ""
        with self._lock:
            return {
                **self._settings,
                "active_downloads": self._limiter.active,
                "engine": "yt-dlp",
                "engine_available": bool(engine_path),
            }

    def configure(self, *, max_concurrent_downloads: int, fragment_threads: int) -> dict:
        max_concurrent_downloads = max(1, min(8, int(max_concurrent_downloads)))
        fragment_threads = max(1, min(64, int(fragment_threads)))
        with self._lock:
            self._settings["max_concurrent_downloads"] = max_concurrent_downloads
            self._settings["fragment_threads"] = fragment_threads
            self._limiter.resize(max_concurrent_downloads)

        def update(config: dict) -> None:
            section = config.setdefault("download", {})
            section["max_concurrent_downloads"] = max_concurrent_downloads
            section["fragment_threads"] = fragment_threads

        mutate_config(update)
        return self.settings()

    def list(self) -> list[dict]:
        with self._lock:
            ordered = sorted(self._tasks.values(), key=lambda item: item["created_at"], reverse=True)
            return [self._public(item) for item in ordered]

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._public(task) if task else None

    def remove(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.get("status") not in TERMINAL_STATES:
                raise DownloadValidationError("task_active")
            self._tasks.pop(task_id)
            self._save_locked()

    def clear_history(self, *, only_failed: bool = False) -> int:
        with self._lock:
            remove_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.get("status") in TERMINAL_STATES
                and (not only_failed or task.get("status") == "failed")
            ]
            for task_id in remove_ids:
                self._tasks.pop(task_id, None)
            if remove_ids:
                self._save_locked()
            return len(remove_ids)

    def create(self, payload: dict) -> dict:
        _parse_direct_url(payload.get("media_url", ""))
        number = payload["number"].strip().upper()
        payload = dict(payload, number=number, title=payload.get("title", "").strip())
        with self._lock:
            if any(
                item.get("status") in ACTIVE_STATES
                and item.get("payload", {}).get("number") == number
                for item in self._tasks.values()
            ):
                raise DownloadValidationError("duplicate_active")
            task_id = uuid.uuid4().hex[:12]
            now = datetime.now().isoformat(timespec="seconds")
            task = {
                "id": task_id,
                "type": "media_download",
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "progress": 0.0,
                "elapsed_seconds": 0.0,
                "duration_seconds": None,
                "bytes_written": 0,
                "speed": "",
                "eta_seconds": None,
                "message": "queued",
                "payload": payload,
                "result": None,
            }
            self._tasks[task_id] = task
            self._save_locked()
            public = self._public(task)
        self._launch(task_id)
        return public

    def control(self, task_id: str, action: str) -> dict:
        launch = False
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            status = task["status"]
            process = self._processes.get(task_id)
            if action == "pause" and status in {"queued", "probing", "running"}:
                if process and process.poll() is None:
                    _suspend_process(process)
                task.update(status="paused", message="paused")
            elif action == "resume" and status == "paused":
                thread = self._threads.get(task_id)
                if process and process.poll() is None:
                    _resume_process(process)
                    task.update(status="running", message="running")
                elif not thread or not thread.is_alive():
                    task.update(status="queued", message="queued")
                    launch = True
                else:
                    task.update(status="running", message="running")
            elif action == "cancel" and status not in TERMINAL_STATES:
                task.update(status="cancelling", message="cancelling")
                if process and process.poll() is None:
                    try:
                        _resume_process(process)
                    except OSError:
                        logger.debug("Resume before cancellation failed", exc_info=True)
                    process.terminate()
            elif action == "retry" and status == "failed":
                number = task.get("payload", {}).get("number")
                if any(
                    item is not task
                    and item.get("status") in ACTIVE_STATES
                    and item.get("payload", {}).get("number") == number
                    for item in self._tasks.values()
                ):
                    raise DownloadValidationError("duplicate_active")
                task.update(
                    status="queued",
                    progress=0.0,
                    elapsed_seconds=0.0,
                    duration_seconds=None,
                    bytes_written=0,
                    speed="",
                    eta_seconds=None,
                    message="queued",
                    error_code=None,
                    detail_message=None,
                    result=None,
                )
                launch = True
            else:
                raise DownloadValidationError("invalid_task_state")
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_locked()
            public = self._public(task)
        if launch:
            self._launch(task_id)
        return public

    def update_media_url(self, task_id: str, media_url: str) -> dict:
        media_url = (media_url or "").strip()
        _parse_direct_url(media_url)
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.get("status") != "failed":
                raise DownloadValidationError("invalid_task_state")
            task["payload"] = dict(task.get("payload") or {}, media_url=media_url)
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_locked()
            return self._public(task)

    def _launch(self, task_id: str) -> None:
        with self._lock:
            prior = self._threads.get(task_id)
            if prior and prior.is_alive():
                waiter = threading.Thread(
                    target=self._launch_after,
                    args=(task_id, prior),
                    daemon=True,
                    name=f"media-download-retry-{task_id}",
                )
                waiter.start()
                return
            thread = threading.Thread(
                target=self._run,
                args=(task_id,),
                daemon=True,
                name=f"media-download-{task_id}",
            )
            self._threads[task_id] = thread
            thread.start()

    def _launch_after(self, task_id: str, prior: threading.Thread) -> None:
        prior.join()
        with self._lock:
            if self._tasks.get(task_id, {}).get("status") != "queued":
                return
        self._launch(task_id)

    def _update(self, task_id: str, *, persist: bool = True, **changes) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.update(changes)
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if persist:
                self._save_locked()

    def _wait_until_runnable(self, task_id: str) -> bool:
        while True:
            with self._lock:
                status = self._tasks[task_id]["status"]
            if status == "cancelling":
                return False
            if status != "paused":
                return True
            time.sleep(0.2)

    def _write_assets_and_import(self, payload: dict, output_path: Path, duration: float | None) -> list[str]:
        warnings: list[str] = []
        config = load_config()
        external_manager = normalize_external_manager(
            config.get("scraper", {}).get("external_manager", "off")
        )
        stem = output_path.stem
        cover_ok = False
        cover = payload.get("cover", "")
        if cover:
            try:
                _validate_network_target(cover, allow_private=self.allow_private_urls)
                cover_suffix = "-poster.jpg" if external_manager != "off" else ".jpg"
                cover_ok = download_image(cover, str(output_path.with_name(stem + cover_suffix)))
                if not cover_ok:
                    warnings.append("cover_failed")
            except DownloadValidationError:
                warnings.append("cover_blocked")

        title = payload.get("title", "")
        nfo_ok = generate_nfo(
            number=payload["number"],
            title=title,
            original_title=title,
            actors=payload.get("actors", []),
            tags=payload.get("tags", []),
            date=payload.get("date", ""),
            maker=payload.get("maker", ""),
            url=payload.get("source_page_url", ""),
            output_path=str(output_path.with_suffix(".nfo")),
            duration=round(duration / 60) if duration else None,
            director=payload.get("director", ""),
            series=payload.get("series", ""),
            label=payload.get("label", ""),
            has_poster=cover_ok and external_manager != "off",
            external_manager=external_manager,
        )
        if not nfo_ok:
            warnings.append("nfo_failed")

        if try_inflow_upsert(str(output_path)) != "synced":
            warnings.append("library_import_failed")
        return warnings

    def _build_command(self, payload: dict, folder: Path, work_prefix: str, settings: dict) -> list[str]:
        command = [
            _yt_dlp_path(),
            "--ignore-config",
            "--no-playlist",
            "--no-simulate",
            "--newline",
            "--progress",
            "--color",
            "no_color",
            "--encoding",
            "utf-8",
            "--progress-delta",
            "0.5",
            "--progress-template",
            "download:OPENAVER_PROGRESS=%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes_estimate)s|%(progress._speed_str)s|%(progress.eta)s",
            "--print",
            "before_dl:OPENAVER_DURATION=%(duration)s",
            "--print",
            "after_move:OPENAVER_FILEPATH=%(filepath)s",
            "--user-agent",
            BROWSER_USER_AGENT,
            "--downloader",
            "m3u8:native",
            "--concurrent-fragments",
            str(settings["fragment_threads"]),
            "--retries",
            str(settings["retry_count"]),
            "--fragment-retries",
            str(settings["retry_count"]),
            "--socket-timeout",
            str(settings["request_timeout_seconds"]),
            "--abort-on-unavailable-fragments",
            "--ffmpeg-location",
            _ffmpeg_path(),
            "--remux-video",
            "mp4",
            "--output",
            str(folder / f"{work_prefix}%(ext)s"),
        ]
        referer = _validated_referer(payload.get("source_page_url", ""))
        if referer:
            command.extend(["--referer", referer, "--add-header", f"Referer:{referer}"])
        command.append(payload["media_url"])
        return command

    def _run(self, task_id: str) -> None:  # noqa: C901 - subprocess lifecycle is intentionally linear
        process: subprocess.Popen | None = None
        try:
            if not self._wait_until_runnable(task_id):
                self._update(task_id, status="cancelled", message="cancelled")
                return
            with self._limiter.slot():
                if not self._wait_until_runnable(task_id):
                    self._update(task_id, status="cancelled", message="cancelled")
                    return
                with self._lock:
                    payload = dict(self._tasks[task_id]["payload"])
                    settings = dict(self._settings)
                self._update(task_id, status="probing", message="probing")
                destination = Path(payload["destination"]).resolve()
                try:
                    usage = shutil.disk_usage(destination)
                except OSError:
                    usage = None
                if usage and usage.free < 500 * 1024 * 1024:
                    raise DownloadValidationError("disk_full")

                referer = _validated_referer(payload.get("source_page_url", ""))
                media_info = validate_direct_media_url(
                    payload["media_url"],
                    allow_private=self.allow_private_urls,
                    referer=referer,
                )
                if not self._wait_until_runnable(task_id):
                    self._update(task_id, status="cancelled", message="cancelled")
                    return

                base_name = sanitize_filename(
                    f"{payload['number']} {payload.get('title', '')}".strip()
                )[:120].rstrip(" .")
                folder = destination / base_name
                output_path = folder / f"{base_name}.mp4"
                work_prefix = f".{base_name}.download."
                if folder.exists():
                    unexpected = [item for item in folder.iterdir() if not item.name.startswith(work_prefix)]
                    if unexpected:
                        raise DownloadValidationError("target_exists")
                else:
                    folder.mkdir(parents=True, exist_ok=False)

                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen(
                    self._build_command(payload, folder, work_prefix, settings),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=flags,
                )
                with self._lock:
                    self._processes[task_id] = process
                self._update(
                    task_id,
                    status="running",
                    message="running",
                    media_kind=media_info["kind"],
                    engine="yt-dlp",
                    fragment_threads=settings["fragment_threads"],
                )

                started_at = time.monotonic()
                last_disk_save = started_at
                output_lines: deque[str] = deque(maxlen=24)
                downloaded_path: Path | None = None
                duration: float | None = None
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if line.startswith("OPENAVER_DURATION="):
                        try:
                            duration = float(line.split("=", 1)[1])
                        except (TypeError, ValueError):
                            duration = None
                        self._update(task_id, duration_seconds=duration)
                        continue
                    if line.startswith("OPENAVER_FILEPATH="):
                        downloaded_path = Path(line.split("=", 1)[1])
                        continue
                    if not line.startswith("OPENAVER_PROGRESS="):
                        continue
                    values = line.split("=", 1)[1].split("|", 4)
                    percent_match = re.search(r"\d+(?:\.\d+)?", values[0] if values else "")
                    percent = min(99.9, float(percent_match.group(0))) if percent_match else 0.0
                    try:
                        bytes_written = int(values[1])
                    except (IndexError, TypeError, ValueError):
                        bytes_written = 0
                    wall_elapsed = time.monotonic() - started_at
                    speed = values[3].strip() if len(values) > 3 and values[3] != "NA" else ""
                    eta_seconds = None
                    if len(values) > 4 and values[4] not in {"", "NA", "None"}:
                        try:
                            eta_seconds = float(values[4])
                        except (TypeError, ValueError):
                            eta_seconds = None
                    if eta_seconds is None and percent > 1:
                        eta_seconds = wall_elapsed * (100 - percent) / percent
                    now = time.monotonic()
                    persist = now - last_disk_save >= 5.0
                    self._update(
                        task_id,
                        persist=persist,
                        progress=round(percent, 1),
                        elapsed_seconds=round(duration * percent / 100 if duration else wall_elapsed, 1),
                        bytes_written=bytes_written,
                        speed=speed,
                        eta_seconds=round(eta_seconds, 1) if eta_seconds is not None else None,
                    )
                    if persist:
                        last_disk_save = now

                return_code = process.wait()
                with self._lock:
                    status = self._tasks[task_id]["status"]
                if status == "cancelling":
                    self._update(task_id, status="cancelled", message="cancelled")
                    return
                if return_code != 0:
                    raw = _redact_urls("\n".join(output_lines)[-1200:])
                    error = DownloadValidationError(_error_code(raw))
                    error.detail = raw[-400:]
                    raise error

                if not downloaded_path or not downloaded_path.is_file():
                    candidates = [
                        item
                        for item in folder.glob(f"{work_prefix}*")
                        if item.is_file() and ".part" not in item.name and not item.name.endswith(".ytdl")
                    ]
                    downloaded_path = max(candidates, key=lambda item: item.stat().st_size) if candidates else None
                if not downloaded_path or not downloaded_path.is_file():
                    raise DownloadValidationError("output_missing")
                atomic_replace_file(downloaded_path, output_path)

                warnings: list[str]
                try:
                    warnings = self._write_assets_and_import(payload, output_path, duration)
                except Exception:
                    logger.exception("Downloaded %s but library import failed", payload["number"])
                    warnings = ["library_import_failed"]
                self._update(
                    task_id,
                    status="completed",
                    progress=100.0,
                    elapsed_seconds=round(duration, 1) if duration else 0.0,
                    bytes_written=output_path.stat().st_size,
                    speed="",
                    eta_seconds=0,
                    message="completed_with_warnings" if warnings else "completed",
                    result={
                        "output_path": str(output_path),
                        "folder": str(folder),
                        "warnings": warnings,
                    },
                )
        except DownloadValidationError as exc:
            logger.warning("Authorized media download %s failed: %s", task_id, exc.code)
            self._update(
                task_id,
                status="failed",
                message="failed",
                error_code=exc.code,
                detail_message=getattr(exc, "detail", None),
            )
        except Exception:
            logger.exception("Authorized media download %s failed unexpectedly", task_id)
            self._update(task_id, status="failed", message="failed", error_code="download_failed")
        finally:
            with self._lock:
                self._processes.pop(task_id, None)


_manager: MediaDownloadManager | None = None
_manager_lock = threading.Lock()


def get_media_download_manager() -> MediaDownloadManager:
    """Create the process-wide manager only when a download endpoint is used."""

    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = MediaDownloadManager()
    return _manager
