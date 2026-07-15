"""
Scraper API 路由 - 單檔刮削

端點：
- POST /api/scrape-single  — 單一影片刮削（搜尋元數據、建資料夾、重命名、下載封面、產生 NFO）
- POST /api/batch-enrich   — 批次原地補完（SSE streaming）
- GET  /api/scraper/missing-samples — 檢查缺少本地劇照的影片（唯讀 + 可安全 reconcile DB）
- POST /api/scraper/batch-fetch-samples — 批次補齊劇照（SSE streaming）
"""

import asyncio
import json
import os
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional

from core.database import VideoRepository, get_db_path
from core.db_inflow import try_inflow_upsert
from core.enricher import enrich_single, fetch_samples_only, resolve_nfo_cover_paths
from core.organizer import organize_file
from core.path_utils import to_file_uri, uri_to_fs_path, coerce_to_file_uri
from core.sample_images import (
    check_multi_video_folder,
    has_valid_local_samples,
    resolve_batch_sample_targets,
    scan_missing_samples,
)
from core.scraper import search_jav, search_jav_single_source, strip_internal_nfo_keys
from core.source_config import validate_source_id
from core.cf_transport import get_cf_transport, CfChallengeRequired, CfTransportUnavailable
from core.scrapers.javlibrary import JAVLIBRARY_ORIGIN
from core.logger import get_logger
from core.config import get_gallery_source_paths, load_config
from core import thumbnail_cache
from web.routers.notifications import emit_notification as _emit_notif

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["scraper"])

# Batch sample-fetch re-entrancy guard (sync work in threadpool; use threading.Lock).
_batch_fetch_samples_lock = threading.Lock()
_batch_fetch_samples_busy = False

MAX_BATCH_FETCH_SAMPLES = 500


def _configured_gallery_dir_uris(config: Optional[dict] = None) -> List[str]:
    """Configured gallery directory file:/// URIs (whitelist). Fail-closed when empty."""
    if config is None:
        config = load_config()
    gallery_config = config.get("gallery", {}) or {}
    directories = get_gallery_source_paths(gallery_config)
    path_mappings = gallery_config.get("path_mappings", {}) or {}
    uris: List[str] = []
    for d in directories:
        try:
            uris.append(d if str(d).startswith("file:///") else to_file_uri(d, path_mappings))
        except ValueError:
            continue
    return uris


def _safe_client_error(code: str) -> str:
    """Map internal error codes to client-safe messages (no raw filesystem paths)."""
    mapping = {
        "no_configured_dirs": "未設定掃描資料夾，無法檢查劇照",
        "empty_selection": "未選擇任何影片",
        "not_in_db": "部分路徑不在資料庫中",
        "outside_library": "部分路徑不在設定的掃描資料夾內",
        "missing_number": "部分影片缺少番號",
        "busy": "劇照批次補齊進行中，請稍後再試",
    }
    if code.startswith("too_many:"):
        parts = code.split(":")
        try:
            limit = parts[1]
            return f"一次最多處理 {limit} 部"
        except Exception:
            return "選取數量超過上限"
    return mapping.get(code, "請求無效")


class ScrapeRequest(BaseModel):
    file_path: str
    number: Optional[str] = None
    # 前端可直接傳入 metadata，避免重新搜尋
    metadata: Optional[dict] = None


class ScrapeResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    original_path: Optional[str] = None
    new_folder: Optional[str] = None
    new_filename: Optional[str] = None
    cover_path: Optional[str] = None
    nfo_path: Optional[str] = None


@router.post("/scrape-single")
def scrape_single(request: ScrapeRequest) -> dict:
    """
    單檔刮削 API

    流程:
    1. 搜尋元數據
    2. 建立資料夾
    3. 重命名影片
    4. 下載封面
    5. 生成 NFO
    """
    file_path = request.file_path
    number = request.number

    # 如果沒有提供番號，嘗試從檔名提取
    if not number:
        from core.scraper import extract_number
        number = extract_number(file_path)

    if not number:
        return {
            "success": False,
            "error": "無法識別番號，請手動輸入"
        }

    # 優先使用前端傳來的 metadata
    if request.metadata:
        metadata = request.metadata
        metadata['number'] = number
    else:
        # 沒有 metadata 才重新搜尋
        metadata = search_jav(number)
        if not metadata:
            return {
                "success": False,
                "error": f"找不到 {number} 的資料"
            }
        metadata['number'] = number

    logger.debug(f"[scraper] cover URL: {metadata.get('cover', 'NO COVER')}")

    # 載入設定
    config = load_config()
    scraper_config = config.get('scraper', {})

    # 執行整理（scraper_config 已包含 suffix_keywords，organize_file 自行偵測）
    result = organize_file(file_path, metadata, scraper_config)

    # 覆蓋保護：目標路徑已存在時回傳 duplicate 狀態（不覆蓋）
    if result.get('duplicate'):
        return {
            "success": False,
            "duplicate": True,
            "duplicate_target": result.get('duplicate_target', ''),
        }

    # scrape 成功後：若 metadata 含 user_tags，寫入 DB（與現有值取聯集）
    if result.get('success') and metadata.get('user_tags'):
        try:
            user_tags = metadata['user_tags']
            new_filename = result.get('new_filename', '')
            if new_filename:
                path_uri = to_file_uri(new_filename)
                repo = VideoRepository()
                existing = repo.get_by_path(path_uri)
                existing_user_tags = existing.user_tags if existing else []
                merged = existing_user_tags + [t for t in user_tags if t not in existing_user_tags]
                repo.update_user_tags(path_uri, merged)
        except Exception:
            logger.warning("scrape_single: DB upsert user_tags 失敗，result 仍回傳", exc_info=True)

    # in-flow upsert：整理成功後條件式寫入 DB（只在 Scanner 追蹤目錄內才執行）
    db_sync_status = "not_linked"
    if result.get("success"):
        target_file = result.get("new_filename")
        if target_file:
            # 72d-P2C：cd2/part2 外部模式下 organizer F2 skip NFO，scan_file 無 NFO 可讀
            # → 傳 scraped_metadata 讓 db_inflow overlay scraped fields，cd2 row 與 cd1 一致。
            # 非 multipart（skipped_nfo_multipart 不存在或 False）一律傳 None（byte-identical）。
            _multipart_meta = metadata if result.get("skipped_nfo_multipart") else None
            db_sync_status = try_inflow_upsert(
                target_file,
                old_file_path=file_path,
                scraped_metadata=_multipart_meta,
            )
        else:
            logger.warning("scrape_single: organize_file 回傳缺 new_filename，skip in-flow upsert")

    return {**result, "db_sync_status": db_sync_status}


class EnrichRequest(BaseModel):
    file_path: str
    number: str
    mode: Literal["refresh_full", "fill_missing", "db_to_sidecar"] = "fill_missing"
    write_nfo: bool = True
    write_cover: bool = True
    write_extrafanart: bool = False
    overwrite_existing: bool = False
    source: Optional[str] = None
    javbus_lang: Optional[str] = None


class BatchEnrichItem(BaseModel):
    file_path: str
    number: str
    source: Optional[str] = None       # per-item override（優先於 batch default）
    javbus_lang: Optional[str] = None  # per-item override


class BatchEnrichRequest(BaseModel):
    items: List[BatchEnrichItem]       # max 20，超過返回 422
    mode: Literal["refresh_full", "fill_missing", "db_to_sidecar"] = "refresh_full"
    source: Optional[str] = None       # batch default（item 未指定時用此值）
    javbus_lang: Optional[str] = None  # batch default
    write_nfo: bool = True
    write_cover: bool = True
    write_extrafanart: bool = False
    overwrite_existing: bool = False


class RescrapePreviewRequest(BaseModel):
    number: str
    source: str = "auto"


@router.post("/rescrape/preview")
def rescrape_preview_endpoint(request: RescrapePreviewRequest) -> dict:
    """重刮預覽（CD-62-3）：只搜不寫，復用 B1 搜尋路徑。

    - source=auto → search_jav（走 merger）。
    - 具體來源 → search_jav_single_source（明確選源繞 merger，CD-62-6）。
    回傳成功 dict + success:True；not-found（None）→ 200 {success:False}。
    不下載 cover（cover 是遠端 URL，原樣回前端，無 SSRF 面）。
    """
    config = load_config()
    search_cfg = config.get("search", {})
    proxy_url = search_cfg.get("proxy_url", "")

    try:
        if request.source == "auto":
            result = search_jav(
                request.number,
                source="auto",
                proxy_url=proxy_url,
            )
        else:
            result = search_jav_single_source(
                request.number, request.source, proxy_url
            )

        if result is None:
            return {"success": False}
        return {"success": True, **strip_internal_nfo_keys(result)}
    except CfChallengeRequired:
        t = get_cf_transport()
        if t:
            try:
                t.begin_solve(JAVLIBRARY_ORIGIN, 'javlibrary')  # 非阻塞
            except Exception:
                logger.exception("rescrape_preview: begin_solve 失敗，回 cf_unavailable")
                return {"success": False, "cf_unavailable": True}
        return {"success": False, "cf_needed": True}
    except CfTransportUnavailable:
        return {"success": False, "cf_unavailable": True}
    except Exception:
        logger.exception("rescrape_preview_endpoint 失敗")
        return {"success": False, "error": "預覽搜尋失敗，請查閱日誌"}


@router.post("/enrich-single")
def enrich_single_endpoint(request: EnrichRequest) -> dict:
    config = load_config()
    search_cfg = config.get("search", {})
    proxy_url = search_cfg.get("proxy_url", "")

    # CD-62-4 分裂陷阱智慧防呆：refresh_full + overwrite=false 時，若這組設定不會寫出任何
    # sidecar（NFO/cover）卻仍 _db_upsert，就是純分裂。一個 sidecar「會寫」需 write 旗標開 + 檔案缺
    # （此分支 overwrite 已為 false，既有檔不覆寫）。兩者皆不會寫 → 擋；任一會寫則放行（quick-enrich
    # 缺封面零回歸）。涵蓋 write_nfo/write_cover 皆 false 的純 DB-only 路徑（Codex P1）。
    # write_extrafanart 刻意排除：_write_extrafanart 無 overwrite gate 且只在 scraper 回
    # sample_images 才寫；若 scraper 無 samples → 零磁碟寫出但 _db_upsert 照跑 = 分裂，
    # 故不得計入「保證會寫 sidecar」；補劇照請用 /api/scraper/fetch-samples（Codex PR#47 round-2 P2）。
    # 在 try 之前 raise，避免被下方 except Exception 吞成籠統 200。
    if request.mode == "refresh_full" and not request.overwrite_existing:
        nfo_path, cover_path = resolve_nfo_cover_paths(request.file_path)
        will_write_nfo = request.write_nfo and not os.path.exists(nfo_path)
        will_write_cover = request.write_cover and not os.path.exists(cover_path)
        # 72d-P2A：外部圖寫出機會也是合法的寫出路徑（72b-T6 加入 external_manager 後守衛未同步）
        external_manager = config.get("scraper", {}).get("external_manager", "off")
        if external_manager != "off":
            stem = os.path.splitext(uri_to_fs_path(request.file_path))[0]
            poster_path = stem + "-poster.jpg"
            fanart_path = stem + "-fanart.jpg"
            # 底圖存在 + 至少一張外部圖缺 → _write_external_images 有寫出機會
            cover_exists_on_disk = os.path.exists(cover_path)
            will_write_external = cover_exists_on_disk and (
                not os.path.exists(poster_path) or not os.path.exists(fanart_path)
            )
        else:
            will_write_external = False
        if not will_write_nfo and not will_write_cover and not will_write_external:
            raise HTTPException(
                status_code=400,
                detail="refresh_full + overwrite_existing=false 在此設定下不會寫出任何 NFO/封面，只會更新 DB 造成與磁碟分裂；請開 overwrite_existing、確保 NFO/封面有實際寫入，或補劇照請改用 /api/scraper/fetch-samples",
            )

    try:
        result = enrich_single(
            file_path=request.file_path,
            number=request.number,
            mode=request.mode,
            write_nfo=request.write_nfo,
            write_cover=request.write_cover,
            write_extrafanart=request.write_extrafanart,
            overwrite_existing=request.overwrite_existing,
            external_manager=config.get("scraper", {}).get("external_manager", "off"),
            proxy_url=proxy_url,
            source=request.source,
            javbus_lang=request.javbus_lang,
        )
        # feature/71 T8: 換封面成功 → 失效舊縮圖（下次 lazy/prewarm 重生，CD-9 / spec 2.A.7）。
        # request.file_path 已是 DB 的 file:/// URI（前端送 currentLightboxVideo.path /
        # missing-check items / rescrape，皆 DB v.path）。縮圖 canonical key = v.path 原字串
        # hash（generate/serve/prewarm 同源），故 invalidate 必須用同一 URI 原值——用冪等
        # coerce_to_file_uri（已是 URI 就原樣回），不可再套 to_file_uri 造成 file:///file:///
        # double-encode 砍錯 hash（PR #60 Codex P2）。
        if result.success:
            thumbnail_cache.invalidate(coerce_to_file_uri(request.file_path))
        from dataclasses import asdict
        return asdict(result)
    except Exception:
        logger.exception("enrich_single_endpoint 失敗")
        return {"success": False, "error": "enrich 處理失敗，請查閱日誌"}


class FetchSamplesRequest(BaseModel):
    file_path: str
    # Client number is ignored; DB number is the sole authority (ops10 parity).
    number: Optional[str] = None


@router.post("/scraper/fetch-samples")
def fetch_samples_endpoint(req: FetchSamplesRequest) -> dict:
    """Single-item sample fetch with the same safety rules as batch (ops10).

    - DB number only (client ``number`` ignored)
    - Path must be under configured gallery dirs and exist in DB (fail-closed)
    - Skip network when valid local stills already exist
    - Precheck / validation errors return stable JSON (not HTTP 500)

    Response stays frontend-compatible: ``success``, ``error``,
    ``extrafanart_written``; ``error == "multi_video_folder"`` still recognized.
    """
    def _fail(error: str, **extra) -> dict:
        out = {"success": False, "error": error, "extrafanart_written": 0}
        out.update(extra)
        return out

    try:
        config = load_config()
        proxy_url = (config.get("search") or {}).get("proxy_url", "")
        db_path = get_db_path()
        dir_uris = _configured_gallery_dir_uris(config)
        if not dir_uris:
            return _fail(_safe_client_error("no_configured_dirs"))

        repo = VideoRepository(db_path)
        # Same whitelist + DB authority as batch-fetch-samples.
        accepted, err = resolve_batch_sample_targets(
            repo=repo,
            dir_uris=dir_uris,
            paths=[req.file_path],
            max_items=1,
        )
        if err:
            return _fail(_safe_client_error(err))

        path = accepted[0]["path"]
        number = accepted[0]["number"]  # DB number only
        result = _process_one_batch_sample(
            path, number, proxy_url, db_path, config=config
        )

        status = result.get("status")
        if status == "success":
            return {
                "success": True,
                "error": None,
                "extrafanart_written": int(result.get("images_written") or 0),
                "source_used": result.get("source_used") or "",
            }
        if status == "skipped_complete":
            # Already has valid local stills — no network was used.
            return {
                "success": True,
                "error": None,
                "extrafanart_written": 0,
            }
        if status == "skipped_multi":
            return {
                "success": False,
                "error": "multi_video_folder",
                "count": 0,
                "extrafanart_written": 0,
            }

        raw_err = result.get("error") or "failed"
        # Keep stable codes the frontend/tests may match; otherwise client-safe text.
        if raw_err in ("no_samples", "download_failed", "file_missing", "not_in_db", "missing_number"):
            err_map = {
                "no_samples": "no_samples",
                "download_failed": "download_failed",
                "file_missing": "檔案不存在",
                "not_in_db": _safe_client_error("not_in_db"),
                "missing_number": _safe_client_error("missing_number"),
            }
            return _fail(err_map.get(raw_err, raw_err))
        return _fail("fetch_samples 處理失敗，請查閱日誌")
    except Exception:
        logger.exception("fetch_samples_endpoint 失敗")
        return {
            "success": False,
            "error": "fetch_samples 處理失敗，請查閱日誌",
            "extrafanart_written": 0,
        }


class BatchFetchSamplesItem(BaseModel):
    path: Optional[str] = None
    file_path: Optional[str] = None
    number: Optional[str] = None  # ignored; DB number is authoritative


class BatchFetchSamplesRequest(BaseModel):
    items: Optional[List[BatchFetchSamplesItem]] = None
    paths: Optional[List[str]] = None


@router.get("/scraper/missing-samples")
def missing_samples_endpoint() -> dict:
    """Read-only check: videos under configured dirs that lack valid local stills.

    May reconcile stale sample_images from disk extrafanart (no network).
    Fail-closed when no gallery directories are configured.
    """
    try:
        db_path = get_db_path()
        if not db_path.exists():
            return {
                "success": True,
                "count": 0,
                "items": [],
                "skipped_multi": 0,
                "reconciled": 0,
            }
        dir_uris = _configured_gallery_dir_uris()
        if not dir_uris:
            return {
                "success": False,
                "error": _safe_client_error("no_configured_dirs"),
                "count": 0,
                "items": [],
                "skipped_multi": 0,
                "reconciled": 0,
            }
        repo = VideoRepository(db_path)
        result = scan_missing_samples(repo, dir_uris, config=load_config())
        if not result.get("success"):
            return {
                "success": False,
                "error": _safe_client_error(result.get("error") or "no_configured_dirs"),
                "count": 0,
                "items": [],
                "skipped_multi": 0,
                "reconciled": 0,
            }
        return result
    except Exception:
        logger.exception("missing_samples_endpoint failed")
        return {
            "success": False,
            "error": "檢查缺少劇照失敗",
            "count": 0,
            "items": [],
            "skipped_multi": 0,
            "reconciled": 0,
        }


def _process_one_batch_sample(
    path: str,
    number: str,
    proxy_url: str,
    db_path,
    *,
    config: Optional[dict] = None,
) -> Dict[str, Any]:
    """Process a single video for batch sample fetch (runs in executor).

    Always opens the same ``db_path`` validated at the endpoint (never the
    ambient default DB). Re-reads the row at execution time; if the record is
    gone or number/path is invalid, fail-closed (no network). Re-checks multi
    via :func:`check_multi_video_folder` (DB + disk, fail-closed). Never
    overwrites existing stills. Only writes extrafanart/ + sample_images.
    """
    try:
        fs_path = uri_to_fs_path(path)
    except Exception:
        fs_path = path

    if not os.path.isfile(fs_path):
        return {
            "status": "failed",
            "number": number,
            "path": path,
            "images_written": 0,
            "error": "file_missing",
        }

    if db_path is None:
        return {
            "status": "failed",
            "number": number,
            "path": path,
            "images_written": 0,
            "error": "missing_db_path",
        }

    repo = VideoRepository(db_path)
    folder_uri_prefix = to_file_uri(os.path.dirname(fs_path)) + "/"
    is_multi, _count, multi_err = check_multi_video_folder(
        repo, folder_uri_prefix, config=config
    )
    if is_multi:
        return {
            "status": "skipped_multi",
            "number": number,
            "path": path,
            "images_written": 0,
            "error": multi_err or "multi_video_folder",
        }

    # Re-read from the same DB at execution time (TOCTOU / row may be gone).
    video = repo.get_by_path(path)
    if video is None:
        return {
            "status": "failed",
            "number": number,
            "path": path,
            "images_written": 0,
            "error": "not_in_db",
        }

    db_number = (video.number or "").strip()
    if not db_number:
        return {
            "status": "failed",
            "number": number,
            "path": path,
            "images_written": 0,
            "error": "missing_number",
        }

    sample_db = video.sample_images or []
    if has_valid_local_samples(fs_path, sample_db):
        return {
            "status": "skipped_complete",
            "number": db_number,
            "path": path,
            "images_written": 0,
            "error": None,
        }

    # Authoritative number from DB only — never fall back to accepted/client number.
    result = fetch_samples_only(
        file_path=path,
        number=db_number,
        proxy_url=proxy_url,
        db_path=db_path,
    )
    if result.success:
        return {
            "status": "success",
            "number": db_number,
            "path": path,
            "images_written": int(result.extrafanart_written or 0),
            "error": None,
            "source_used": result.source_used or "",
        }
    err = result.error or "failed"
    if err == "no_samples":
        status = "no_samples"
    elif err == "download_failed":
        status = "failed"
    else:
        status = "failed"
    return {
        "status": status,
        "number": db_number,
        "path": path,
        "images_written": 0,
        "error": err,
        "source_used": result.source_used or "",
    }


@router.post("/scraper/batch-fetch-samples")
async def batch_fetch_samples_endpoint(request: Request, body: BatchFetchSamplesRequest):
    """批次補齊劇照 — SSE streaming。

    - 明確接收 items 或 paths；去重；上限 MAX_BATCH_FETCH_SAMPLES
    - 路徑必須在 configured gallery dirs；number 以 DB 為準
    - 每項處理前重新檢查磁碟；已有劇照 → skipped_complete（不聯網）
    - 同目錄多片 → skipped_multi（不聯網）
    - 全域防重入 409；finally 釋放 busy；斷線儘量停止後續
    """
    global _batch_fetch_samples_busy

    db_path = get_db_path()
    if not db_path.exists():
        return JSONResponse(
            {"success": False, "error": "資料庫不存在，請先掃描"},
            status_code=400,
        )

    config = load_config()
    dir_uris = _configured_gallery_dir_uris(config)
    repo = VideoRepository(db_path)

    raw_items = body.items or []
    item_dicts: List[Dict[str, Any]] = []
    for it in raw_items:
        if isinstance(it, BatchFetchSamplesItem):
            item_dicts.append(it.model_dump() if hasattr(it, "model_dump") else it.dict())
        elif isinstance(it, dict):
            item_dicts.append(it)

    accepted, err = resolve_batch_sample_targets(
        repo=repo,
        dir_uris=dir_uris,
        items=item_dicts,
        paths=body.paths,
        max_items=MAX_BATCH_FETCH_SAMPLES,
    )
    if err:
        return JSONResponse(
            {"success": False, "error": _safe_client_error(err)},
            status_code=400,
        )

    acquired = _batch_fetch_samples_lock.acquire(blocking=False)
    if not acquired:
        return JSONResponse(
            {"success": False, "error": _safe_client_error("busy")},
            status_code=409,
        )
    if _batch_fetch_samples_busy:
        _batch_fetch_samples_lock.release()
        return JSONResponse(
            {"success": False, "error": _safe_client_error("busy")},
            status_code=409,
        )
    _batch_fetch_samples_busy = True
    _batch_fetch_samples_lock.release()

    search_cfg = config.get("search", {}) or {}
    proxy_url = search_cfg.get("proxy_url", "")
    total = len(accepted)
    # Capture the validated db path for the processor (do not re-open default DB).
    process_db_path = db_path

    async def event_generator():
        global _batch_fetch_samples_busy
        summary = {
            "total": total,
            "success": 0,
            "images_downloaded": 0,
            "no_samples": 0,
            "skipped": 0,
            "skipped_complete": 0,
            "skipped_multi": 0,
            "failed": 0,
        }
        try:
            for idx, item in enumerate(accepted, start=1):
                if await request.is_disconnected():
                    logger.info("batch-fetch-samples: client disconnected at %s/%s", idx, total)
                    break

                number = item["number"]
                path = item["path"]
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "progress",
                            "current": idx,
                            "total": total,
                            "number": number,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

                try:
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None,
                        lambda p=path, n=number: _process_one_batch_sample(
                            p, n, proxy_url, process_db_path, config=config
                        ),
                    )
                except Exception:
                    logger.exception("batch-fetch-samples item failed")
                    result = {
                        "status": "failed",
                        "number": number,
                        "path": path,
                        "images_written": 0,
                        "error": "processing_error",
                    }

                status = result.get("status") or "failed"
                images_written = int(result.get("images_written") or 0)
                if status == "success":
                    summary["success"] += 1
                    summary["images_downloaded"] += images_written
                elif status == "no_samples":
                    summary["no_samples"] += 1
                elif status == "skipped_complete":
                    summary["skipped_complete"] += 1
                    summary["skipped"] += 1
                elif status == "skipped_multi":
                    summary["skipped_multi"] += 1
                    summary["skipped"] += 1
                else:
                    summary["failed"] += 1

                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "item",
                            "current": idx,
                            "total": total,
                            "number": number,
                            "path": path,
                            "status": status,
                            "images_written": images_written,
                            "error": result.get("error"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

            yield (
                "data: "
                + json.dumps({"type": "done", "summary": summary}, ensure_ascii=False)
                + "\n\n"
            )
        except Exception:
            logger.exception("batch-fetch-samples stream failed")
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "message": "批次補齊劇照中斷，請查閱日誌"},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        finally:
            # Always release busy flag so a crashed run cannot stick forever.
            with _batch_fetch_samples_lock:
                _batch_fetch_samples_busy = False

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/batch-enrich")
async def batch_enrich_endpoint(request: BatchEnrichRequest):
    """批次 enrich — SSE streaming，最多 20 筆，按 file_path 去重"""
    if len(request.items) > 20:
        raise HTTPException(status_code=422, detail="items 上限為 20 筆")

    config = await asyncio.to_thread(load_config)
    search_cfg = config.get("search", {})
    proxy_url = search_cfg.get("proxy_url", "")

    # 去重（按 file_path）
    seen_paths: set = set()
    deduped_items = []
    for item in request.items:
        if item.file_path not in seen_paths:
            seen_paths.add(item.file_path)
            deduped_items.append(item)

    total = len(deduped_items)

    async def event_generator():
        success_count = 0
        failed_count = 0
        # 53b-T3: 補完開始通知
        _emit_notif(
            "info", "notif.batch_enrich_started",
            message=f"共 {total} 部",
            task_type="batch_enrich",
        )
        # scraper cache：只對 refresh_full 生效（100% 需要 scraper data）
        # fill_missing 由 enrich_single 內部判斷是否需要打外站，不 pre-fetch
        # value 為 dict（成功）或 {}（search_jav 回 None，負向 cache）
        scraper_cache: dict = {}

        try:
            for idx, item in enumerate(deduped_items, start=1):
                effective_source = item.source or request.source or "auto"
                # 未知 / 非法 source guard：不靜默轉成無效 cache_key，退回 'auto'（最小驚訝）。
                if effective_source != "auto" and not validate_source_id(effective_source):
                    logger.warning(
                        "batch_enrich: 未知 source %r（number=%s），退回 'auto'",
                        effective_source, item.number,
                    )
                    effective_source = "auto"
                effective_lang = item.javbus_lang or request.javbus_lang

                # progress 事件
                yield f"data: {json.dumps({'type': 'progress', 'current': idx, 'total': total, 'number': item.number})}\n\n"

                try:
                    loop = asyncio.get_running_loop()

                    # scraper cache（只對 refresh_full pre-fetch）
                    cached_data = None
                    if request.mode == "refresh_full":
                        cache_key = (item.number.upper(), effective_source, effective_lang)
                        if cache_key not in scraper_cache:
                            fetched = await loop.run_in_executor(
                                None,
                                lambda n=item.number, es=effective_source, el=effective_lang: search_jav(
                                    n,
                                    source=es,
                                    proxy_url=proxy_url,
                                    javbus_lang=el,
                                ),
                            )
                            # 負向 cache：search_jav 回 None → 存 {}（空 dict falsy）
                            # enrich_single 收到 {} 時 `is None` 為 False（不再搜），
                            # `not scraper_data` 為 True（回錯誤）
                            scraper_cache[cache_key] = fetched if fetched else {}
                        cached_data = scraper_cache[cache_key]

                    result = await loop.run_in_executor(
                        None,
                        lambda i=item, sd=cached_data, es=effective_source, el=effective_lang: enrich_single(
                            file_path=i.file_path,
                            number=i.number,
                            mode=request.mode,
                            write_nfo=request.write_nfo,
                            write_cover=request.write_cover,
                            write_extrafanart=request.write_extrafanart,
                            overwrite_existing=request.overwrite_existing,
                            external_manager=config.get("scraper", {}).get("external_manager", "off"),
                            proxy_url=proxy_url,
                            source=es if es != "auto" else None,
                            javbus_lang=el,
                            scraper_data=sd,
                        ),
                    )
                    from dataclasses import asdict
                    result_dict = asdict(result)
                    if result.success:
                        success_count += 1
                        # feature/71 T8: 換封面成功 → 失效舊縮圖（廉價同步 unlink，不需 offload）。
                        # item.file_path 已是 DB file:/// URI → 冪等 coerce，不可 double-encode
                        # （同 enrich-single，PR #60 Codex P2）。
                        thumbnail_cache.invalidate(coerce_to_file_uri(item.file_path))
                    else:
                        failed_count += 1
                    yield f"data: {json.dumps({'type': 'result-item', 'number': item.number, 'file_path': item.file_path, **result_dict})}\n\n"
                except Exception:
                    logger.exception("batch_enrich item %s 失敗", item.number)
                    failed_count += 1
                    yield f"data: {json.dumps({'type': 'result-item', 'number': item.number, 'file_path': item.file_path, 'success': False, 'error': 'enrich 處理失敗，請查閱日誌'})}\n\n"

            yield f"data: {json.dumps({'type': 'done', 'summary': {'total': total, 'success': success_count, 'failed': failed_count}})}\n\n"
            # 53b-T3: 補完完成通知
            if failed_count > 0:
                _emit_notif(
                    "warn", "notif.batch_enrich_done_with_errors",
                    message=f"補完 {success_count} 部，{failed_count} 部失敗",
                    task_type="batch_enrich",
                )
            else:
                _emit_notif(
                    "success", "notif.batch_enrich_done",
                    message=f"補完 {success_count} 部",
                    task_type="batch_enrich",
                )
        except Exception:
            logger.exception("[notif] batch_enrich 失敗")
            _emit_notif(
                "error", "notif.batch_enrich_failed",
                message="批次補完中斷，請查閱日誌",
                task_type="batch_enrich",
            )
            raise

    return StreamingResponse(event_generator(), media_type="text/event-stream")
