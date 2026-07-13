"""Local maintenance, backup, restore, and customized-build status APIs."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

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
