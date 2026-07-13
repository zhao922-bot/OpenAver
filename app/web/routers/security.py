"""Local management endpoints for LAN access authentication."""

import secrets

from fastapi import APIRouter, HTTPException, Request

from core.config import load_config, mutate_config


router = APIRouter(prefix="/api/security", tags=["security"])


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Security settings are local-only")


@router.get("/lan-token")
def get_lan_token(request: Request) -> dict:
    _local_only(request)
    cfg = load_config().get("security", {})
    return {
        "success": True,
        "token": cfg.get("lan_token", ""),
        "require_lan_auth": cfg.get("require_lan_auth", True),
        "usage": "Open the LAN URL with ?token=... once, or send Authorization: Bearer ...",
    }


@router.post("/lan-token/rotate")
def rotate_lan_token(request: Request) -> dict:
    _local_only(request)
    token = secrets.token_urlsafe(24)
    mutate_config(lambda cfg: cfg.setdefault("security", {}).update({
        "lan_token": token,
        "require_lan_auth": True,
    }))
    return {"success": True, "token": token, "sessions_invalidated": True}
