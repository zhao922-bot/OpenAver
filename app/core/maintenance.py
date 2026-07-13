"""Consistent local backups for the customized OpenAver installation."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import zipfile

from core.config import CONFIG_PATH
from core.database import get_db_path
from core.version import VERSION_INFO


INSTALL_ROOT = Path(__file__).resolve().parents[2]
BACKUP_ROOT = INSTALL_ROOT / "backups"


def _safe_backup_dir(name: str) -> Path:
    candidate = (BACKUP_ROOT / name).resolve()
    if candidate.parent != BACKUP_ROOT.resolve() or not candidate.is_dir():
        raise ValueError("Invalid backup name")
    return candidate


def create_backup(reason: str = "manual") -> dict:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = BACKUP_ROOT / f"snapshot-{stamp}"
    backup_dir.mkdir()

    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, backup_dir / "config.json")

    db_path = Path(get_db_path())
    if db_path.exists():
        source = sqlite3.connect(str(db_path))
        target = sqlite3.connect(str(backup_dir / "openaver.db"))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason[:120],
        "version": VERSION_INFO,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    archive = backup_dir / "data-export.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename in ("config.json", "openaver.db", "manifest.json"):
            path = backup_dir / filename
            if path.exists():
                zf.write(path, filename)
    return backup_info(backup_dir)


def backup_info(path: Path) -> dict:
    manifest_path = path / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    files = [p for p in path.iterdir() if p.is_file()]
    return {
        "name": path.name,
        "created_at": manifest.get("created_at"),
        "reason": manifest.get("reason", "legacy"),
        "size": sum(p.stat().st_size for p in files),
        "has_config": (path / "config.json").exists(),
        "has_database": (path / "openaver.db").exists(),
        "download": f"/api/maintenance/backups/{path.name}/download"
        if (path / "data-export.zip").exists() else None,
    }


def list_backups() -> list[dict]:
    if not BACKUP_ROOT.exists():
        return []
    items = [backup_info(p) for p in BACKUP_ROOT.iterdir() if p.is_dir()]
    return sorted(items, key=lambda item: item["name"], reverse=True)


def restore_backup(name: str) -> dict:
    source = _safe_backup_dir(name)
    safety = create_backup(f"before-restore:{name}")

    config_source = source / "config.json"
    if config_source.exists():
        shutil.copy2(config_source, CONFIG_PATH)

    db_source = source / "openaver.db"
    if db_source.exists():
        with sqlite3.connect(str(db_source)) as check:
            result = check.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError("Backup database failed integrity check")
        destination = Path(get_db_path())
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_conn = sqlite3.connect(str(db_source))
        dest_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
            source_conn.close()

    return {"restored": name, "safety_backup": safety["name"]}
