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


def _media_request_headers(referer: str = "", extra: dict | None = None) -> dict:
    """Headers shared by preflight probe and yt-dlp download (same UA / Referer)."""
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


def validate_direct_media_url(
    url: str,
    allow_private: bool = False,
    *,
    referer: str = "",
) -> dict:
    """Reject HTML pages and accept only HLS manifests or direct video responses.

    Uses the same User-Agent / Referer as the downloader so signed links that
    require browser-like headers fail early with a clear Chinese reason.
    """
    _validate_network_target(url, allow_private=allow_private)
    headers = _media_request_headers(referer=referer, extra={"Range": "bytes=0-65535"})
    try:
        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            with client.stream("GET", url, headers=headers) as response:
                status = response.status_code
                if status in (401, 403):
                    raise DownloadValidationError(
                        "链接已过期或被拒绝访问（HTTP 403）。请重新获取带签名的直链，"
                        "并确认是否需要来源页 Referer。"
                    )
                if status == 404:
                    raise DownloadValidationError("媒体地址不存在（HTTP 404），请检查链接是否完整")
                if status == 410:
                    raise DownloadValidationError("媒体链接已失效（HTTP 410），请重新获取直链")
                if status >= 400:
                    raise DownloadValidationError(f"源站返回 HTTP {status}，无法下载")
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
    except DownloadValidationError:
        raise
    except httpx.TimeoutException as exc:
        raise DownloadValidationError("连接媒体源超时，请检查网络或稍后重试") from exc
    except httpx.HTTPError as exc:
        msg = str(exc).lower()
        if "403" in msg or "forbidden" in msg:
            raise DownloadValidationError(
                "链接已过期或被拒绝访问（HTTP 403）。请重新获取有效直链后重试"
            ) from exc
        if "name or service not known" in msg or "getaddrinfo" in msg or "nodename" in msg:
            raise DownloadValidationError("无法解析媒体主机名，请检查链接或网络") from exc
        raise DownloadValidationError(f"媒体地址请求失败：{_redact_urls(str(exc))}") from exc

    text_prefix = prefix.lstrip()[:512].upper()
    if content_type in {"text/html", "application/xhtml+xml"} or b"<HTML" in text_prefix:
        raise DownloadValidationError("这是网页地址而不是媒体直链，请粘贴 m3u8 或视频文件 URL")
    is_hls = b"#EXTM3U" in text_prefix or content_type in {
        "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl",
    }
    is_video = content_type.startswith("video/") or content_type in {
        "application/octet-stream", "binary/octet-stream",
    }
    if not is_hls and not is_video:
        raise DownloadValidationError(f"不支持的媒体类型：{content_type or '未知'}")
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


# error_code → Chinese message for UI (also mirrored in locales)
ERROR_MESSAGES_ZH = {
    "http_403": "链接已过期或被拒绝访问（HTTP 403）。请重新获取有效直链；若站点需要来源页，请填写 Referer。",
    "http_404": "媒体地址不存在（HTTP 404），请检查链接是否完整。",
    "link_expired": "链接已过期（签名/有效期失效）。请回到来源页重新复制直链后重试。",
    "need_referer": "源站要求 Referer。请填写来源页地址，或使用带来源页的授权下载。",
    "engine_missing": "下载引擎未安装（yt-dlp）。请确认 OpenAver/tools/yt-dlp.exe 存在。",
    "ffmpeg_missing": "未找到 FFmpeg。m3u8 封装需要 FFmpeg，请安装并加入 PATH。",
    "target_exists": "目标文件夹已有其他文件，请更换番号或清理目标目录。",
    "disk_full": "磁盘空间不足，请清理磁盘后重试。",
    "timeout": "连接或下载超时，请检查网络后重试。",
    "network_error": "网络错误，无法连接媒体源。",
    "not_media": "这不是可用的媒体直链（网页或未知类型）。",
    "download_failed": "下载失败。请查看详情或更换直链后重试。",
}


def _error_code(message: str) -> str:
    lowered = (message or "").lower()
    raw = message or ""
    if "链接已过期" in raw or "签名" in raw or "expired" in lowered or "token" in lowered and "403" in lowered:
        return "link_expired"
    if "403" in lowered or "forbidden" in lowered or "被拒绝访问" in raw:
        if "referer" in lowered or "来源页" in raw:
            return "need_referer"
        return "http_403"
    if "404" in lowered or "不存在" in raw:
        return "http_404"
    if "ffmpeg" in lowered and ("not installed" in lowered or "not found" in lowered or "未找到" in raw or "path" in lowered):
        return "ffmpeg_missing"
    if "multi-thread download engine is not installed" in lowered or "yt-dlp" in lowered and "not" in lowered:
        return "engine_missing"
    if "target folder already contains files" in lowered or "已有其他文件" in raw:
        return "target_exists"
    if "no space" in lowered or "disk full" in lowered or "errno 28" in lowered or "磁盘空间" in raw:
        return "disk_full"
    if "timeout" in lowered or "timed out" in lowered or "超时" in raw:
        return "timeout"
    if "resolve" in lowered or "getaddrinfo" in lowered or "network" in lowered or "连接" in raw:
        return "network_error"
    if "网页" in raw or "not media" in lowered or "unsupported media" in lowered or "不是媒体" in raw:
        return "not_media"
    return "download_failed"


def _friendly_error(message: str) -> tuple[str, str]:
    """Return (error_code, chinese_message). Prefer classified message over raw FFmpeg dump."""
    code = _error_code(message)
    # If already a short Chinese validation message, keep it
    if message and not any(tok in message.lower() for tok in ("traceback", "ffmpeg", "error opening", "http://", "https://")):
        if any("\u4e00" <= ch <= "\u9fff" for ch in message):
            return code, message[:300]
    return code, ERROR_MESSAGES_ZH.get(code, ERROR_MESSAGES_ZH["download_failed"])


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
        if result.get("status") == "failed":
            if not result.get("error_code"):
                code, friendly = _friendly_error(result.get("message", ""))
                result["error_code"] = code
                result["error_message"] = friendly
            elif not result.get("error_message"):
                result["error_message"] = ERROR_MESSAGES_ZH.get(
                    result["error_code"], result.get("message", "")
                )
        # ETA is set by _run from yt-dlp or wall_elapsed/progress estimate.
        # duration_seconds is video length (seconds of media), NOT total download
        # time — never recompute ETA from it (would turn e.g. 120s into 3600s).
        if "eta_seconds" not in result:
            result["eta_seconds"] = None
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

    def clear_history(self, *, only_failed: bool = False) -> int:
        """Remove terminal tasks. Returns count removed."""
        with self._lock:
            remove_ids = []
            for tid, task in self._tasks.items():
                status = task.get("status")
                if status not in TERMINAL_STATES:
                    continue
                if only_failed and status != "failed":
                    continue
                remove_ids.append(tid)
            for tid in remove_ids:
                self._tasks.pop(tid, None)
            if remove_ids:
                self._save()
            return len(remove_ids)

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
                    raise ValueError(f"番号 {number} 已有进行中的下载任务")
                # Optional: caller may have updated payload.media_url before retry
                task.update({
                    "status": "queued", "progress": 0.0, "elapsed_seconds": 0.0,
                    "duration_seconds": None, "bytes_written": 0, "speed": "",
                    "eta_seconds": None,
                    "message": "排队重试", "error_code": None, "error_message": None, "result": None,
                })
                launch = True
            else:
                raise ValueError(f"当前状态 {status} 无法执行 {action}")
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

    def _write_assets_and_import(
        self, payload: dict, output_path: Path, duration: float | None
    ) -> dict:
        """Write NFO/cover sidecars then scan+import into the library DB.

        Video download has already succeeded. Sidecar failures are recorded as
        warnings / import_error but must not skip DB import or reclassify the
        task as a download failure.

        Returns:
            ``{"warnings": list[str], "import_error": str}`` — client-safe
            messages only (no signed cover URLs).
        """
        warnings: list[str] = []
        stem = output_path.stem
        title = payload.get("chinese_title") or payload.get("title") or ""
        nfo_ok = generate_nfo(
            number=payload["number"], title=title, original_title=payload.get("title", ""),
            actors=payload.get("actors", []), tags=payload.get("tags", []), date=payload.get("date", ""),
            maker=payload.get("maker", ""), url=payload.get("source_page_url", ""),
            output_path=str(output_path.with_suffix(".nfo")), duration=round(duration / 60) if duration else None,
            director=payload.get("director", ""), series=payload.get("series", ""), label=payload.get("label", ""),
            external_manager=load_config().get("scraper", {}).get("external_manager", "off"),
        )
        if not nfo_ok:
            warnings.append("NFO 写入失败")
            logger.warning("NFO write failed for %s", payload["number"])

        cover = payload.get("cover", "")
        if cover:
            try:
                _validate_network_target(cover, allow_private=self.allow_private_urls)
                cover_ok = download_image(cover, str(output_path.with_name(stem + ".jpg")))
                if not cover_ok:
                    warnings.append("封面下载失败")
                    logger.warning("Cover download failed for %s", payload["number"])
            except DownloadValidationError:
                warnings.append("封面 URL 不安全，已跳过")
                logger.warning("Skipped unsafe cover URL for %s", payload["number"])

        # Always attempt library import even when sidecars failed.
        info = VideoScanner().scan_file(str(output_path), None)
        VideoRepository().upsert(Video.from_video_info(info))

        import_error = "；".join(warnings) if warnings else ""
        return {"warnings": warnings, "import_error": import_error}

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
                self._update(task_id, status="probing", message="正在校验媒体直链…")
                referer = (payload.get("source_page_url") or "").strip()
                # Disk space soft-check (≥ 500 MB free on destination drive)
                try:
                    dest_root = Path(payload["destination"]).resolve()
                    usage = shutil.disk_usage(str(dest_root if dest_root.exists() else dest_root.anchor or dest_root))
                    if usage.free < 500 * 1024 * 1024:
                        raise DownloadValidationError("磁盘空间不足（剩余不足 500MB），请清理后重试")
                except DownloadValidationError:
                    raise
                except Exception:
                    pass
                media_info = validate_direct_media_url(
                    payload["media_url"],
                    allow_private=self.allow_private_urls,
                    referer=referer,
                )
                duration = _parse_duration(payload["media_url"])
                self._update(task_id, duration_seconds=duration, message="准备下载…")
                if not self._wait_until_runnable(task_id):
                    self._update(task_id, status="cancelled", message="已取消")
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
                        raise DownloadValidationError(f"目标文件夹已有其他文件：{folder.name}")
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
                ]
                if referer:
                    command.extend(["--referer", referer, "--add-header", f"Referer:{referer}"])
                command.append(payload["media_url"])
                flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding="utf-8", errors="replace", creationflags=flags,
                )
                with self._lock:
                    self._processes[task_id] = process
                self._update(
                    task_id, status="running", message="下载中…", media_kind=media_info["kind"],
                    engine="yt-dlp", fragment_threads=settings["fragment_threads"],
                )

                last_save = 0.0
                started_at = time.monotonic()
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
                    wall_elapsed = time.monotonic() - started_at
                    media_elapsed = duration * percent / 100 if duration else wall_elapsed
                    speed = values[3].strip() if len(values) > 3 and values[3] != "NA" else ""
                    # ETA from yt-dlp or estimate from progress
                    eta_seconds = None
                    if len(values) > 4 and values[4] not in ("", "NA", "None"):
                        try:
                            eta_seconds = float(values[4])
                        except (TypeError, ValueError):
                            eta_seconds = None
                    if eta_seconds is None and percent > 1 and wall_elapsed > 0:
                        eta_seconds = wall_elapsed * (100 - percent) / percent
                    now = time.monotonic()
                    if now - last_save >= 0.45:
                        self._update(
                            task_id, progress=round(percent, 1),
                            elapsed_seconds=round(media_elapsed if duration else wall_elapsed, 1),
                            bytes_written=bytes_written, speed=speed,
                            eta_seconds=round(eta_seconds, 1) if eta_seconds is not None else None,
                        )
                        last_save = now

                return_code = process.wait()
                with self._lock:
                    status = self._tasks[task_id]["status"]
                if status == "cancelling":
                    self._update(task_id, status="cancelled", message="已取消")
                    return
                if return_code != 0:
                    raw = "\n".join(output_lines)[-1200:] or "Download engine failed"
                    code, friendly = _friendly_error(_redact_urls(raw))
                    # Prefer classified Chinese; keep redacted tail for hover detail
                    raise RuntimeError(friendly + (f"\n---\n{_redact_urls(raw)[-400:]}" if raw else ""))

                if not downloaded_path or not downloaded_path.is_file():
                    candidates = [
                        item for item in folder.glob(f"{work_prefix}*")
                        if item.is_file() and ".part" not in item.name and not item.name.endswith(".ytdl")
                    ]
                    downloaded_path = max(candidates, key=lambda item: item.stat().st_size) if candidates else None
                if not downloaded_path or not downloaded_path.is_file():
                    raise RuntimeError("下载完成但未找到输出文件")
                os.replace(downloaded_path, output_path)
                import_error = ""
                try:
                    asset_result = self._write_assets_and_import(payload, output_path, duration)
                    import_error = _redact_urls(asset_result.get("import_error") or "")
                except Exception as exc:
                    logger.exception("Downloaded %s but library import failed", payload["number"])
                    import_error = _redact_urls(str(exc))
                self._update(
                    task_id, status="completed", progress=100.0,
                    elapsed_seconds=round(duration, 1) if duration else 0.0,
                    bytes_written=output_path.stat().st_size, speed="", eta_seconds=0,
                    message="已完成" if not import_error else "已下载；入库需检查",
                    result={
                        "output_path": str(output_path), "folder": str(folder),
                        "import_error": import_error,
                    },
                )
        except Exception as exc:
            logger.exception("Authorized media download task %s failed", task_id)
            message = _redact_urls(str(exc))
            code, friendly = _friendly_error(message)
            self._update(
                task_id, status="failed",
                message=friendly,
                error_code=code,
                error_message=friendly,
                detail_message=message if message != friendly else None,
            )
        finally:
            with self._lock:
                self._processes.pop(task_id, None)

    def update_media_url(self, task_id: str, media_url: str) -> dict:
        """Update media URL on a failed task before retry (new signed link)."""
        media_url = (media_url or "").strip()
        if not media_url:
            raise ValueError("媒体直链不能为空")
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(task_id)
            if task.get("status") != "failed":
                raise ValueError("仅失败任务可更换直链")
            payload = dict(task.get("payload") or {})
            payload["media_url"] = media_url
            task["payload"] = payload
            task["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save()
            return self._public(task)


media_download_manager = MediaDownloadManager()
