"""Persistent background task API."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Literal, Optional

from core.task_manager import task_manager


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class ReadonlyTaskRequest(BaseModel):
    source_path: str
    output_path: str = ""
    external_manager: Optional[Literal["off", "jellyfin", "emby", "kodi"]] = None
    proxy_url: str = ""
    force: bool = False
    strm_path_mappings: Optional[Dict[str, str]] = None


@router.get("")
def list_tasks() -> dict:
    return {"success": True, "items": task_manager.list()}


@router.post("/readonly")
def create_readonly_task(request: ReadonlyTaskRequest) -> dict:
    if request.external_manager in {"jellyfin", "emby", "kodi"} and not request.output_path.strip():
        raise HTTPException(status_code=400, detail="output_path is required")
    return {"success": True, "task": task_manager.create_readonly(request.model_dump())}


@router.post("/{task_id}/{action}")
def control_task(task_id: str, action: Literal["pause", "resume", "cancel"]) -> dict:
    try:
        return {"success": True, "task": task_manager.control(task_id, action)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
