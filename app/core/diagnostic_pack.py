"""One-click diagnostic pack export (redacted config + logs + source status)."""
from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.config import CONFIG_PATH, load_config
from core.database import get_db_path
from core.logger import get_logger
from core.version import VERSION_INFO

logger = get_logger(__name__)

INSTALL_ROOT = Path(__file__).resolve().parents[2]
DIAG_ROOT = INSTALL_ROOT / "backups" / "diagnostics"

_SECRET_KEY_RE = re.compile(
    r"(api_key|token|password|secret|lan_token|authorization)",
    re.IGNORECASE,
)


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if _SECRET_KEY_RE.search(str(k)):
                if v:
                    out[k] = "***REDACTED***"
                else:
                    out[k] = v
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    if isinstance(obj, str):
        return _redact_log_text(obj)
    return obj


def _redact_url(url: str) -> str:
    """Strip query/fragment and sensitive path segments from a URL."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        p = urlsplit(url.rstrip(".,;)]'\""))
        path = p.path or "/"
        sensitive = ("token", "sig", "sign", "auth", "key", "exp", "expires", "m3u8")
        lower = path.lower()
        if any(s in lower for s in sensitive) or lower.endswith(".m3u8"):
            # Keep only host + media type hint
            hint = "m3u8" if ".m3u8" in lower else "media"
            path = f"/[REDACTED_{hint}]"
        # Always drop query + fragment (signed links live there)
        return urlunsplit((p.scheme, p.netloc, path, "", ""))
    except Exception:
        return "[REDACTED_URL]"


def _redact_log_text(text: str) -> str:
    """Redact secrets and signed media URLs from free-form log text."""
    if not text:
        return text
    # Authorization: Bearer <token>  or  Authorization: <token>
    # Must run before generic key=value so the whole header value is covered.
    text = re.sub(
        r"(?i)\bAuthorization\s*[:=]\s*(?:Bearer\s+)?\S+",
        "Authorization: ***REDACTED***",
        text,
    )
    # Standalone Bearer tokens (HTTP header value without Authorization key)
    text = re.sub(
        r"(?i)\bBearer\s+\S+",
        "Bearer ***REDACTED***",
        text,
    )
    # key=value secrets (api_key=..., token: ..., password=...)
    text = re.sub(
        r"(api[_-]?key|token|password|secret|lan_token)\s*[:=]\s*\S+",
        r"\1=***REDACTED***",
        text,
        flags=re.IGNORECASE,
    )
    # Full URLs (m3u8 / signed CDN)
    text = re.sub(
        r"https?://[^\s\"'<>\\]+",
        lambda m: _redact_url(m.group(0)),
        text,
    )
    return text


def _config_summary(config: dict) -> dict:
    gallery = config.get("gallery") or {}
    dirs = gallery.get("directories") or []
    dir_summaries = []
    for d in dirs:
        if isinstance(d, str):
            dir_summaries.append({"path": d, "readonly": False})
        elif isinstance(d, dict):
            dir_summaries.append({
                "path": d.get("path", ""),
                "readonly": bool(d.get("readonly")),
                "has_output": bool(d.get("output_path")),
            })
    sources = []
    for s in config.get("sources") or []:
        if isinstance(s, dict):
            sources.append({
                "id": s.get("id"),
                "enabled": s.get("enabled"),
                "order": s.get("order"),
                "manual_only": s.get("manual_only"),
                "requires_proxy": s.get("requires_proxy"),
                "type": s.get("type"),
            })
    translate = config.get("translate") or {}
    return {
        "locale": (config.get("general") or {}).get("locale"),
        "theme": (config.get("general") or {}).get("theme"),
        "proxy_configured": bool((config.get("search") or {}).get("proxy_url")),
        "translate_enabled": bool(translate.get("enabled")),
        "translate_provider": translate.get("provider"),
        "gallery_dir_count": len(dir_summaries),
        "gallery_dirs": dir_summaries,
        "sources": sources,
        "download": config.get("download") or {},
        "thumbnail_cache_enabled": config.get("thumbnail_cache_enabled"),
        "metatube_enabled": bool((config.get("metatube") or {}).get("enabled")),
    }


def create_diagnostic_pack(*, log_tail_lines: int = 800) -> dict:
    """Write a zip under backups/diagnostics/ and return metadata + path."""
    DIAG_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    pack_name = f"diag-{stamp}"
    zip_path = DIAG_ROOT / f"{pack_name}.zip"

    config = {}
    try:
        config = load_config()
    except Exception as exc:
        logger.warning("diagnostic pack: load_config failed: %s", exc)

    deps = {}
    sources = {}
    try:
        from core.source_diagnostics import diagnose_dependencies, diagnose_sources
        deps = diagnose_dependencies(config)
        sources = diagnose_sources(probe=False)
    except Exception as exc:
        logger.warning("diagnostic pack: diagnose failed: %s", exc)
        deps = {"error": str(exc)}

    db_stats = {}
    try:
        from core.database import VideoRepository, init_db
        init_db()
        repo = VideoRepository()
        videos = repo.get_all()
        locked = sum(1 for v in videos if v.field_locks)
        confirmed = sum(1 for v in videos if (v.translation_meta or {}).get("confirmed"))
        db_stats = {
            "video_count": len(videos),
            "locked_videos": locked,
            "confirmed_translations": confirmed,
            "db_path": str(get_db_path()),
        }
    except Exception as exc:
        db_stats = {"error": str(exc)}

    download_settings = {}
    try:
        from core.media_downloader import media_download_manager
        download_settings = media_download_manager.settings()
        # strip engine path if sensitive? keep as-is for support
    except Exception as exc:
        download_settings = {"error": str(exc)}

    log_excerpt = ""
    log_path = INSTALL_ROOT / "logs" / "debug.log"
    if log_path.is_file():
        try:
            # Read tail efficiently for large logs
            with log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read_size = min(size, 512 * 1024)
                f.seek(-read_size, 2)
                data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            log_excerpt = "\n".join(lines[-log_tail_lines:])
            log_excerpt = _redact_log_text(log_excerpt)
        except OSError as exc:
            log_excerpt = f"(failed to read log: {exc})"

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "pack_name": pack_name,
        "version": VERSION_INFO,
        "note": (
            "Secrets and signed media URLs redacted. "
            "Still review before sharing outside trusted support channels."
        ),
    }

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("version.json", json.dumps(VERSION_INFO, ensure_ascii=False, indent=2))
        zf.writestr(
            "config_summary.json",
            json.dumps(_config_summary(config), ensure_ascii=False, indent=2),
        )
        # Full config redacted (no raw secrets)
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                zf.writestr(
                    "config_redacted.json",
                    json.dumps(_redact(raw), ensure_ascii=False, indent=2),
                )
            except Exception as exc:
                zf.writestr("config_redacted.json", json.dumps({"error": str(exc)}))
        zf.writestr("dependencies.json", json.dumps(deps, ensure_ascii=False, indent=2))
        zf.writestr("sources.json", json.dumps(sources, ensure_ascii=False, indent=2))
        zf.writestr("library_stats.json", json.dumps(db_stats, ensure_ascii=False, indent=2))
        zf.writestr(
            "download_settings.json",
            json.dumps(download_settings, ensure_ascii=False, indent=2),
        )
        if log_excerpt:
            zf.writestr("debug_tail.log", log_excerpt)

    return {
        "name": pack_name,
        "path": str(zip_path),
        "size": zip_path.stat().st_size,
        "created_at": manifest["created_at"],
        "download": f"/api/maintenance/diagnostics/{pack_name}/download",
    }


def list_diagnostic_packs(limit: int = 20) -> list[dict]:
    if not DIAG_ROOT.is_dir():
        return []
    items = []
    for p in sorted(DIAG_ROOT.glob("diag-*.zip"), reverse=True):
        items.append({
            "name": p.stem,
            "path": str(p),
            "size": p.stat().st_size,
            "created_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "download": f"/api/maintenance/diagnostics/{p.stem}/download",
        })
        if len(items) >= limit:
            break
    return items


def diagnostic_pack_file(name: str) -> Path:
    # only allow diag-YYYY... pattern
    if not re.fullmatch(r"diag-\d{8}-\d{6}", name or ""):
        raise ValueError("Invalid diagnostic pack name")
    path = (DIAG_ROOT / f"{name}.zip").resolve()
    if path.parent != DIAG_ROOT.resolve() or not path.is_file():
        raise FileNotFoundError(name)
    return path
