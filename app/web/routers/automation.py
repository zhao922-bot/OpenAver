"""Reviewed new-file automation queue."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.automation_watcher import (
    apply_item,
    dismiss_item,
    list_items,
    preview_item,
    rollback_item,
    scan_for_new_files,
)
from core.config import load_config, mutate_config


router = APIRouter(prefix="/api/automation", tags=["automation"])


class WatchConfig(BaseModel):
    enabled: bool
    interval_seconds: int = 60
    settle_seconds: int = 30


@router.get("/status")
def status() -> dict:
    cfg = load_config().get("automation", {})
    items = list_items()
    return {
        "success": True,
        "config": cfg,
        "pending": sum(item.get("status") == "pending" for item in items),
        "failed": sum(item.get("status") == "failed" for item in items),
    }


@router.put("/watch")
def configure_watch(payload: WatchConfig) -> dict:
    interval = max(15, min(payload.interval_seconds, 3600))
    settle = max(5, min(payload.settle_seconds, 3600))
    mutate_config(lambda cfg: cfg.setdefault("automation", {}).update({
        "watch_enabled": payload.enabled,
        "scan_interval_seconds": interval,
        "settle_seconds": settle,
    }))
    return {"success": True, "enabled": payload.enabled, "interval_seconds": interval, "settle_seconds": settle}


@router.post("/scan-now")
def scan_now() -> dict:
    return {"success": True, **scan_for_new_files(force_stable=True)}


@router.get("/items")
def items() -> dict:
    return {"success": True, "items": list_items()}


@router.post("/items/{item_id}/preview")
def preview(item_id: str, refresh: bool = False) -> dict:
    try:
        return {"success": True, "item": preview_item(item_id, refresh)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Queue item not found")


@router.post("/items/{item_id}/apply")
def apply(item_id: str) -> dict:
    try:
        item = apply_item(item_id)
        return {"success": item.get("status") == "completed", "item": item}
    except KeyError:
        raise HTTPException(status_code=404, detail="Queue item not found")


@router.post("/items/{item_id}/dismiss")
def dismiss(item_id: str) -> dict:
    try:
        return {"success": True, "item": dismiss_item(item_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Queue item not found")


@router.post("/items/{item_id}/rollback")
def rollback(item_id: str) -> dict:
    try:
        return {"success": True, "item": rollback_item(item_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Queue item not found")
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
