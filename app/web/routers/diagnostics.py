"""
web/routers/diagnostics.py — TASK-79-T3
=======================================
本地前端診斷 sink（client-log beacon）。

端點：
  POST /api/client-log — 前端 head inline beacon 送 boot / post_alpine /
                         error 生命週期事件；寫入 OpenAver.frontend channel
                         （→ debug.log）。回 204、無 body。

性質：純本地 append-only log sink。零外送、無 DB、無背景任務。
刻意「不揭露」於 capabilities._TOOLS（CD13）——它是診斷基建，非 AI 工具。
"""
from typing import Literal, Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel

from core.logger import get_logger

logger = get_logger('frontend')  # → OpenAver.frontend，進 debug.log
router = APIRouter(prefix="/api", tags=["diagnostics"])


class ClientLogPayload(BaseModel):
    phase: Optional[Literal['boot', 'post_alpine', 'error']] = None
    message: str
    kind: Optional[str] = None
    source: Optional[str] = None
    lineno: Optional[int] = None
    colno: Optional[int] = None
    stack: Optional[str] = None
    user_agent: Optional[str] = None
    importmap_supported: Optional[bool] = None
    alpine_version: Optional[str] = None
    pywebview_api: Optional[bool] = None
    path: Optional[str] = None


@router.post("/client-log")
def client_log(payload: ClientLogPayload) -> Response:
    """前端 beacon sink：截斷防灌爆，CD10 level split，回 204 無 body。"""
    msg = (payload.message or '')[:4000]
    stack = (payload.stack or '')[:4000]
    source = (payload.source or '')[:1000]
    location = ""
    if source:
        location += f" source={source}"
    if payload.lineno is not None:
        location += f" line={payload.lineno}"
    if payload.colno is not None:
        location += f" col={payload.colno}"
    line = (
        f"[client] phase={payload.phase} kind={payload.kind} "
        f"path={payload.path}{location} ua={payload.user_agent} :: {msg}"
    )
    if payload.phase == 'error':
        logger.warning(line + (f"\n{stack}" if stack else ''))  # CD10: error → WARNING
    else:
        logger.info(line)  # CD10: boot / post_alpine → INFO
    return Response(status_code=204)
