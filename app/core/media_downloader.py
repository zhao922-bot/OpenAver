"""Authorized direct-media downloader with persistent progress and library import."""

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
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx

from core.config import load_config, mutate_config
from core.database import Video, VideoRepository, get_db_path
from core.gallery_scanner import VideoScanner
from core.logger import get_logger
from core.organizer import download_image, generate_nfo, sanitize_filename


logger = get_logger(__name__)
DOWNLOADS_PATH = get_db_path().parent / "download_tasks.json"
INSTALL_ROOT = Path(__file__).resolve().parents[2]
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
TERMINAL_STATES = {"completed", "cancelled", "failed"}
ACTIVE_STATES = {"queued", "probing", "running", "paused", "cancelling"}


class DownloadValidationError(ValueError):
    pass


def _safe_url_for_display(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_urls(value: str) -> str:
    def replace(match: re.Match) -> str:
        return _safe_url_for_display(match.group(0).rstrip(".,;)]"))
    return re.sub(r"https?://[^\s]+", replace, value or "")


def _validate_network_target(url: str, allow_private: bool = False) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DownloadValidationError("Only direct HTTP or HTTPS media URLs are accepted")
    if parsed.username or parsed.password:
        raise DownloadValidationError("Credentials in media URLs are not accepted")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise DownloadValidationError("The media host could not be resolved") from exc
    if allow_private:
        return
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise DownloadValidationError("Private, loopback, and reserved network targets are blocked")


def validate_direct_media_url(url: str, allow_private: bool = False) -> dict:
    """Reject HTML pages and accept only HLS manifests or direct video responses."""
    _validate_network_target(url, allow_private=allow_private)
    headers = {"Range": "bytes=0-65535", "User-Agent": BROWSER_USER_AGENT}
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                _validate_network_target(str(response.url), allow_private=allow_private)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= 65536:
                        break
                prefix = b"".join(chunks)[:65536]
    except httpx.HTTPError as exc:
        raise DownloadValidationError(f"Media URL request failed: {exc}") from exc

    text_prefix = prefix.lstrip()[:512].upper()
    if content_type in {"text/html", "application/xhtml+xml"} or b"<HTML" in text_prefix:
        raise DownloadValidationError("Web pages are not media URLs; paste a direct m3u8 or video URL")
    is_hls = b"#EXTM3U" in text_prefix or content_type in {
        "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
    }
    is_video = content_type.startswith("video/") or content_type in {
        "application/octet-stream", "binary/octet-stream",
    }
    if not is_hls and not is_video:
        raise DownloadValidationError(f"Unsupported media response type: {content_type or 'unknown'}")
    return {"kind": "hls" if is_hls else "video", "content_type": content_type}


def _ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("FFmpeg is not installed or is not available on PATH")
    return path


def _yt_dlp_path() -> str:
    candidates = [INSTALL_ROOT / "tools" / "yt-dlp.exe", INSTALL_ROOT / "tools" / "yt-dlp"]
    path = next((str(item) for item in candidates if item.is_file()), None) or shutil.which("yt-dlp")
    if not path:
        raise RuntimeError("The multi-thread download engine is not installed")
    return path


def _parse_duration(url: str) -> float | None:
    command = [
        _ffmpeg_path(), "-hide_banner", "-nostdin", "-user_agent", BROWSER_USER_AGENT, "-protocol_whitelist",
        "http,https,tcp,tls,crypto", "-i", url, "-t", "0", "-f", "null", "NUL" if os.name == "nt" else "/dev/null",
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=35, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def _suspend_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x0800, False, process.pid)
        if not handle:
            raise OSError("Unable to open download process for pause")
        try:
            if ctypes.windll.ntdll.NtSuspendProcess(handle) != 0:
                raise OSError("Unable to pause download")
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
            raise OSError("Unable to open download process for resume")
        try:
            if ctypes.windll.ntdll.NtResumeProcess(handle) != 0:
                raise OSError("Unable to resume download")
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
    if "403 forbidden" in lowered or "http error 403" in lowered:
        return "http_403"
    if "multi-thread download engine is not installed" in lowered:
        return "engine_missing"
    if "target folder already contains files" in lowered:
        return "target_exists"
    return "download_failed"


class MediaDownloadManager:
    def __init__(self, state_path: Path = DOWNLOADS_PATH, *, allow_private_urls: bool = False) -> None:
        self.state_path = Path(state_path)
        self.allow_private_urls = allow_private_urls
        self._lock = threading.RLock()
        configured = load_config().get("download", {})
        self._settings = {
            "max_concurrent_downloads": int(configured.get("max_concurrent_downloads", 4)),
            "fragment_threads": int(configured.get("fragment_threads", 16)),
            "retry_count": int(configured.get("retry_count", 5)),
            "request_timeout_seconds": int(configured.get("request_timeout_seconds", 30)),
        }
        self._limiter = ResizableLimiter(self._settings["max_concurrent_downloads"])
        self._tasks = self._load()
        self._threads: dict[str, threading.Thread] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        for task in self._tasks.values():
            if task.get("status") in {"probing", "running", "paused", "cancelling"}:
                task["status"] = "paused"
                task["message"] = "Interrupted by application restart; resume to restart the transfer"
        self._save()

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(self._tasks, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.state_path)

    def _public(self, task: dict) -> dict:
        result = dict(task)
        payload = dict(result.get("payload", {}))
        payload["media_url"] = _safe_url_for_display(payload.get("media_url", ""))
        result["payload"] = payload
        if result.get("status") == "failed" and not result.get("error_code"):
            result["error_code"] = _error_code(result.get("message", ""))
        return result

    def settings(self) -> dict:
        try:
            engine_path = _yt_dlp_path()
        except RuntimeError:
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
            return [self._public(item) for item in sorted(self._tasks.values(), key=lambda x: x["created_at"], reverse=True)]

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
                raise ValueError("Only finished downloads can be removed")
            self._tasks.pop(task_id)
            self._save()

    def create(self, payload: dict) -> dict:
        number = payload["number"].strip().upper()
        payload = dict(payload, number=number)
        with self._lock:
            for item in self._tasks.values():
                if item.get("status") in ACTIVE_STATES and item.get("payload", {}).get("number") == number:
                    raise DownloadValidationError(f"An active download already exists for {number}")
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat(timespec="seconds")
        task = {
            "id": task_id, "type": "media_download", "status": "queued",
            "created_at": now, "updated_at": now, "progress": 0.0,
            "elapsed_seconds": 0.0, "duration_seconds": None, "bytes_written": 0,
            "speed": "", "message": "Queued", "payload": payload, "result": None,
        }
        with self._lock:
            self._tasks[task_id] = task
            self._save()
        self._launch(task_id)
        return self._public(task)

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
                task["status"] = "paused"
                task["message"] = "Paused"
            elif action == "resume" and status == "paused":
                thread = self._threads.get(task_id)
                if process and process.poll() is None:
                    _resume_process(process)
                    task["status"] = "running"
                    task["message"] = "Downloading"
                elif not thread or not thread.is_alive():
                    task["status"] = "queued"
                    task["message"] = "Queued to restart"
                    launch = True
                else:
                    task["status"] = "running"
                    task["message"] = "Downloading"
            elif action == "cancel" and status not in TERMINAL_STATES:
                task["status"] = "cancelling"
                task["message"] = "Cancelling"
                if process and process.poll() is None:
                    try:
                        _resume_process(process)
                    except OSError:
                        pass
                    process.terminate()
            elif action == "retry" and status == "failed":
                number = task.get("payload", {}).get("number")
                duplicate = any(
                    item is not task
                    and item.get("status") in ACTIVE_STATES
                    and item.get("payload", {}).get("number") == number
                    for item in self._tasks.values()
                )
                if duplicate:
                    raise ValueError(f"An active download already exists for {number}")
                task.update({
                    "status": "queued", "progress": 0.0, "elapsed_seconds": 0.0,
                    "duration_seconds": None, "bytes_written": 0, "speed": "",
                    "message": "Queued to retry", "error_code": None, "result": None,
                })
                launch = True
            else:
                raise ValueError(f"Cannot {action} a download in state {status}")
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()
            public = self._public(task)
        if launch:
            self._launch(task_id)
        return public

    def _launch(self, task_id: str) -> None:
        with self._lock:
            thread = self._threads.get(task_id)
            if thread and thread.is_alive():
                waiter = threading.Thread(
                    target=self._launch_after,
                    args=(task_id, thread),
                    daemon=True,
                    name=f"media-download-retry-{task_id}",
                )
                waiter.start()
                return
            thread = threading.Thread(target=self._run, args=(task_id,), daemon=True, name=f"media-download-{task_id}")
            self._threads[task_id] = thread
            thread.start()

    def _launch_after(self, task_id: str, prior: threading.Thread) -> None:
        prior.join()
        with self._lock:
            if self._tasks.get(task_id, {}).get("status") != "queued":
                return
        self._launch(task_id)

    def _update(self, task_id: str, **changes) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.update(changes)
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()

    def _wait_until_runnable(self, task_id: str) -> bool:
        while True:
            with self._lock:
                status = self._tasks[task_id]["status"]
            if status == "cancelling":
                return False
            if status != "paused":
                return True
            time.sleep(0.2)

    def _write_assets_and_import(self, payload: dict, output_path: Path, duration: float | None) -> None:
        stem = output_path.stem
        title = payload.get("chinese_title") or payload.get("title") or ""
        generate_nfo(
            number=payload["number"], title=title, original_title=payload.get("title", ""),
            actors=payload.get("actors", []), tags=payload.get("tags", []), date=payload.get("date", ""),
            maker=payload.get("maker", ""), url=payload.get("source_page_url", ""),
            output_path=str(output_path.with_suffix(".nfo")), duration=round(duration / 60) if duration else None,
            director=payload.get("director", ""), series=payload.get("series", ""), label=payload.get("label", ""),
            external_manager=load_config().get("scraper", {}).get("external_manager", "off"),
        )
        cover = payload.get("cover", "")
        if cover:
            try:
                _validate_network_target(cover, allow_private=self.allow_private_urls)
                download_image(cover, str(output_path.with_name(stem + ".jpg")))
            except DownloadValidationError:
                logger.warning("Skipped unsafe cover URL for %s", payload["number"])
        info = VideoScanner().scan_file(str(output_path), None)
        VideoRepository().upsert(Video.from_video_info(info))

    def _run(self, task_id: str) -> None:
        process: subprocess.Popen | None = None
        try:
            with self._limiter.slot():
                if not self._wait_until_runnable(task_id):
                    self._update(task_id, status="cancelled", message="Cancelled")
                    return
                with self._lock:
                    payload = dict(self._tasks[task_id]["payload"])
                    settings = dict(self._settings)
                self._update(task_id, status="probing", message="Validating direct media URL")
                media_info = validate_direct_media_url(payload["media_url"], allow_private=self.allow_private_urls)
                duration = _parse_duration(payload["media_url"])
                self._update(task_id, duration_seconds=duration, message="Preparing download")
                if not self._wait_until_runnable(task_id):
                    self._update(task_id, status="cancelled", message="Cancelled")
                    return

                destination = Path(payload["destination"]).resolve()
                display_title = payload.get("chinese_title") or payload.get("title") or ""
                base_name = sanitize_filename(f"{payload['number']} {display_title}".strip())[:120].rstrip(" .")
                folder = destination / base_name
                output_path = folder / f"{base_name}.mp4"
                work_prefix = f".{base_name}.download."
                if folder.exists():
                    unexpected = [item for item in folder.iterdir() if not item.name.startswith(work_prefix)]
                    if unexpected:
                        raise DownloadValidationError(f"Target folder already contains files: {folder.name}")
                else:
                    folder.mkdir(parents=True, exist_ok=False)

                command = [
                    _yt_dlp_path(), "--ignore-config", "--no-playlist", "--no-simulate", "--newline", "--progress",
                    "--color", "no_color", "--encoding", "utf-8", "--progress-delta", "0.5",
                    "--progress-template",
                    "download:OPENAVER_PROGRESS=%(progress._percent_str)s|%(progress.downloaded_bytes)s|%(progress.total_bytes_estimate)s|%(progress._speed_str)s|%(progress.eta)s",
                    "--print", "before_dl:OPENAVER_DURATION=%(duration)s",
                    "--print", "after_move:OPENAVER_FILEPATH=%(filepath)s",
                    "--user-agent", BROWSER_USER_AGENT,
                    "--downloader", "m3u8:native",
                    "--concurrent-fragments", str(settings["fragment_threads"]),
                    "--retries", str(settings["retry_count"]),
                    "--fragment-retries", str(settings["retry_count"]),
                    "--socket-timeout", str(settings["request_timeout_seconds"]),
                    "--abort-on-unavailable-fragments",
                    "--ffmpeg-location", _ffmpeg_path(), "--remux-video", "mp4",
                    "--output", str(folder / f"{work_prefix}%(ext)s"),
                    payload["media_url"],
                ]
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags,
                )
                with self._lock:
                    self._processes[task_id] = process
                self._update(
                    task_id, status="running", message="Downloading", media_kind=media_info["kind"],
                    engine="yt-dlp", fragment_threads=settings["fragment_threads"],
                )

                last_save = 0.0
                output_lines: deque[str] = deque(maxlen=24)
                downloaded_path: Path | None = None
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if line.startswith("OPENAVER_DURATION="):
                        value = line.split("=", 1)[1]
                        try:
                            duration = float(value)
                            self._update(task_id, duration_seconds=duration)
                        except (TypeError, ValueError):
                            pass
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
                    elapsed = duration * percent / 100 if duration else 0.0
                    speed = values[3].strip() if len(values) > 3 and values[3] != "NA" else ""
                    now = time.monotonic()
                    if now - last_save >= 0.45:
                        self._update(
                            task_id, progress=round(percent, 1), elapsed_seconds=round(elapsed, 1),
                            bytes_written=bytes_written, speed=speed,
                        )
                        last_save = now

                return_code = process.wait()
                with self._lock:
                    status = self._tasks[task_id]["status"]
                if status == "cancelling":
                    self._update(task_id, status="cancelled", message="Cancelled")
                    return
                if return_code != 0:
                    raise RuntimeError("\n".join(output_lines)[-1200:] or "Download engine failed")

                if not downloaded_path or not downloaded_path.is_file():
                    candidates = [
                        item for item in folder.glob(f"{work_prefix}*")
                        if item.is_file() and ".part" not in item.name and not item.name.endswith(".ytdl")
                    ]
                    downloaded_path = max(candidates, key=lambda item: item.stat().st_size) if candidates else None
                if not downloaded_path or not downloaded_path.is_file():
                    raise RuntimeError("Download completed but the output file was not found")
                os.replace(downloaded_path, output_path)
                import_error = ""
                try:
                    self._write_assets_and_import(payload, output_path, duration)
                except Exception as exc:
                    logger.exception("Downloaded %s but library import failed", payload["number"])
                    import_error = _redact_urls(str(exc))
                self._update(
                    task_id, status="completed", progress=100.0,
                    elapsed_seconds=round(duration, 1) if duration else 0.0,
                    bytes_written=output_path.stat().st_size, speed="",
                    message="Completed" if not import_error else "Downloaded; library import needs attention",
                    result={
                        "output_path": str(output_path), "folder": str(folder),
                        "import_error": import_error,
                    },
                )
        except Exception as exc:
            logger.exception("Authorized media download task %s failed", task_id)
            message = _redact_urls(str(exc))
            self._update(task_id, status="failed", message=message, error_code=_error_code(message))
        finally:
            with self._lock:
                self._processes.pop(task_id, None)


media_download_manager = MediaDownloadManager()
