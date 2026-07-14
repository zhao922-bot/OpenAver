"""
web/routers/diagnostics.py
==========================
1) POST /api/client-log — 前端 beacon（TASK-79-T3）
2) GET  /api/diagnostics/sources — 資料源 / 依賴狀態（運維中心）
3) GET  /api/diagnostics/dependencies — 僅依賴檢查（curl_cffi / ffmpeg / yt-dlp …）
"""
from typing import Literal, Optional

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from core.logger import get_logger
from core.source_diagnostics import diagnose_dependencies, diagnose_sources

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


# Rate-limit noisy resource failures (proxy-image / favicon storms).
_resource_log_bucket: dict[str, float] = {}
_RESOURCE_LOG_COOLDOWN = 30.0  # seconds per source key


@router.post("/client-log")
def client_log(payload: ClientLogPayload) -> Response:
    """前端 beacon sink：截斷防灌爆，CD10 level split，回 204 無 body。"""
    msg = (payload.message or '')[:4000]
    stack = (payload.stack or '')[:4000]
    source = (payload.source or '')[:1000]
    # Dedupe resource errors for proxy-image / broken covers — keep first, silence 30s
    if payload.phase == 'error' and payload.kind == 'resource':
        import time as _time
        key = source.split('?', 1)[0] if source else msg[:120]
        # Collapse query strings: /api/proxy-image is the high-volume offender
        if 'proxy-image' in key or 'proxy_image' in key:
            key = 'proxy-image'
        now = _time.monotonic()
        last = _resource_log_bucket.get(key, 0.0)
        if now - last < _RESOURCE_LOG_COOLDOWN:
            return Response(status_code=204)
        _resource_log_bucket[key] = now
        # Opportunistic prune
        if len(_resource_log_bucket) > 200:
            cutoff = now - _RESOURCE_LOG_COOLDOWN * 2
            for k, t in list(_resource_log_bucket.items()):
                if t < cutoff:
                    _resource_log_bucket.pop(k, None)

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
        # Resource errors: DEBUG after first (already rate-limited above)
        if payload.kind == 'resource':
            logger.debug(line)  # avoid flooding WARNING with WebView resource noise
        else:
            logger.warning(line + (f"\n{stack}" if stack else ''))  # CD10: error → WARNING
    else:
        logger.info(line)  # CD10: boot / post_alpine → INFO
    return Response(status_code=204)


@router.get("/diagnostics/dependencies")
def diagnostics_dependencies() -> dict:
    """依賴健康檢查：curl_cffi / ffmpeg / yt-dlp / CF / proxy。"""
    return {"success": True, "dependencies": diagnose_dependencies()}


@router.get("/diagnostics/sources")
def diagnostics_sources(
    probe: bool = Query(False, description="是否對 builtin 來源做連通性探測（較慢）"),
) -> dict:
    """資料源狀態：啟用、auto pool、依賴缺失、可選 live probe 與修復提示。"""
    return diagnose_sources(probe=probe)
