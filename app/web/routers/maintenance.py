"""Local maintenance, backup, restore, diagnostic pack, and customized-build status APIs."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.diagnostic_pack import (
    create_diagnostic_pack,
    diagnostic_pack_file,
    list_diagnostic_packs,
)
from core.maintenance import (
    BACKUP_ROOT,
    create_backup,
    list_backups,
    restore_backup,
)
from core.version import VERSION_INFO


router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Maintenance is local-only")


class BackupRequest(BaseModel):
    reason: str = "manual"


class RestoreRequest(BaseModel):
    name: str


@router.get("/status")
def maintenance_status() -> dict:
    return {"success": True, "build": VERSION_INFO, "backups": list_backups()[:5]}


@router.get("/backups")
def backups() -> dict:
    return {"success": True, "items": list_backups()}


@router.post("/backups")
def make_backup(payload: BackupRequest, request: Request) -> dict:
    _local_only(request)
    return {"success": True, "backup": create_backup(payload.reason)}


@router.get("/backups/{name}/download")
def download_backup(name: str, request: Request):
    _local_only(request)
    root = BACKUP_ROOT.resolve()
    archive = (root / name / "data-export.zip").resolve()
    if archive.parent.parent != root or not archive.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(archive, filename=f"OpenAver-{name}.zip")


@router.post("/restore")
def restore(payload: RestoreRequest, request: Request) -> dict:
    _local_only(request)
    return {"success": True, **restore_backup(payload.name), "restart_required": True}


@router.get("/diagnostics")
def diagnostics_list() -> dict:
    return {"success": True, "items": list_diagnostic_packs()}


@router.post("/diagnostics")
def diagnostics_create(request: Request) -> dict:
    """Create a redacted diagnostic zip (logs tail + version + source status)."""
    _local_only(request)
    pack = create_diagnostic_pack()
    return {"success": True, "pack": pack}


@router.get("/diagnostics/{name}/download")
def diagnostics_download(name: str, request: Request):
    _local_only(request)
    try:
        path = diagnostic_pack_file(name)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pack name")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Diagnostic pack not found")
    return FileResponse(path, filename=f"OpenAver-{name}.zip")
