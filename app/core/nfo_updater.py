"""
NFO Updater - 批次更新 NFO 檔案中缺失的欄位
整合到 Gallery 頁面使用

Required-field policy (single source of truth):
  REQUIRED: title, date, actor, genre, maker, duration
  OPTIONAL: director, series, label  (fill opportunistically; never enqueue alone)
"""

from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Generator, List, Optional, Sequence, Tuple

from core.logger import get_logger
from core.nfo_utils import sanitize_nfo_bytes
from core.path_utils import uri_to_fs_path
from core.scraper import search_jav

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared NFO completeness policy (candidate detection + update preflight)
# ---------------------------------------------------------------------------

# Bump when fingerprint inputs or update algorithm change so old no-op
# fingerprints cannot suppress corrected behavior.
NFO_UPDATE_POLICY_VERSION = 2

REQUIRED_NFO_FIELDS: Tuple[str, ...] = (
    "title",
    "date",
    "actor",
    "genre",
    "maker",
    "duration",
)

OPTIONAL_NFO_FIELDS: Tuple[str, ...] = (
    "director",
    "series",
    "label",
)

ALL_FILLABLE_NFO_FIELDS: Tuple[str, ...] = REQUIRED_NFO_FIELDS + OPTIONAL_NFO_FIELDS

# Bounded retry window for no-op fingerprints (seconds)
NOOP_FINGERPRINT_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

# POST selected-path cap
MAX_NFO_UPDATE_PATHS = 500


def is_field_present(value) -> bool:
    """Whether a logical NFO field has a usable value."""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(str(v).strip() for v in value)
    if isinstance(value, (int, float)):
        # duration=0 is valid
        return True
    return bool(str(value).strip())


def missing_fields(
    fields: Dict,
    *,
    required_only: bool = True,
) -> List[str]:
    """Return missing field names from a field map (shared policy)."""
    keys = REQUIRED_NFO_FIELDS if required_only else ALL_FILLABLE_NFO_FIELDS
    return [k for k in keys if not is_field_present(fields.get(k))]


# ---------------------------------------------------------------------------
# NFO parse helpers (source of truth for repair feature)
# ---------------------------------------------------------------------------

def parse_nfo(nfo_path: str) -> Tuple[Optional[ET.ElementTree], Optional[ET.Element]]:
    """解析 NFO 檔案"""
    try:
        raw = Path(nfo_path).read_bytes()
        raw = sanitize_nfo_bytes(raw)
        root = ET.fromstring(raw)
        tree = ET.ElementTree(root)
        return tree, root
    except Exception as e:
        logger.warning(f"NFO 解析失敗: {nfo_path} - {e}")
        return None, None


def extract_nfo_fields_from_root(root: ET.Element) -> Dict:
    """Extract logical field values from an NFO root element.

    Handles title, premiered/release/year, actors, genre/tag, studio/maker,
    runtime/duration, director, series (set/name), label consistently with
    gallery_scanner.parse_nfo.
    """
    def _text(*tags: str) -> str:
        for tag in tags:
            elem = root.find(tag)
            if elem is not None and elem.text and elem.text.strip():
                return elem.text.strip()
        return ""

    title = _text("title")
    date = _text("premiered", "release", "year")
    maker = _text("studio", "maker")
    director = _text("director")
    label = _text("label")

    actors: List[str] = []
    for actor_elem in root.findall(".//actor/name"):
        if actor_elem.text and actor_elem.text.strip():
            actors.append(actor_elem.text.strip())
    actor = ",".join(actors)

    genres: List[str] = []
    seen = set()
    for tag_name in ("genre", "tag"):
        for elem in root.findall(tag_name):
            if elem.text and elem.text.strip():
                g = elem.text.strip()
                if g not in seen:
                    seen.add(g)
                    genres.append(g)
    genre = ",".join(genres)

    duration = None
    runtime_text = _text("runtime")
    if runtime_text:
        try:
            duration = int(runtime_text)
        except ValueError:
            duration = None

    series = ""
    set_name = root.find("set/name")
    if set_name is not None and set_name.text and set_name.text.strip():
        series = set_name.text.strip()

    return {
        "title": title,
        "date": date,
        "actor": actor,
        "genre": genre,
        "maker": maker,
        "duration": duration,
        "director": director,
        "series": series,
        "label": label,
    }


def read_nfo_fields(nfo_path: str) -> Optional[Dict]:
    """Parse NFO file and return field map, or None if missing/unparseable."""
    if not nfo_path or not os.path.exists(nfo_path):
        return None
    _tree, root = parse_nfo(nfo_path)
    if root is None:
        return None
    return extract_nfo_fields_from_root(root)


def get_nfo_path_from_video(video_path: str) -> Optional[str]:
    """從影片路徑取得對應的 NFO 檔案路徑

    Args:
        video_path: 影片檔案路徑（可能是 file:/// URL 或任意格式路徑）

    Returns:
        NFO 檔案的路徑，不存在則返回 None
    """
    video_path = uri_to_fs_path(video_path)

    video_p = Path(video_path)
    nfo_path = video_p.with_suffix(".nfo")

    if nfo_path.exists():
        return str(nfo_path)

    return None


def nfo_mtime_for_path(nfo_path: str) -> float:
    try:
        return float(os.path.getmtime(nfo_path))
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Local DB metadata helpers
# ---------------------------------------------------------------------------

def info_to_metadata(info: dict) -> dict:
    """Convert cache/DB info dict to search_jav-like metadata keys."""
    actors: List[str] = []
    raw_actor = info.get("actor") or ""
    if isinstance(raw_actor, list):
        actors = [str(a).strip() for a in raw_actor if str(a).strip()]
    elif raw_actor:
        actors = [a.strip() for a in str(raw_actor).split(",") if a.strip()]

    tags: List[str] = []
    raw_genre = info.get("genre") or ""
    if isinstance(raw_genre, list):
        tags = [str(t).strip() for t in raw_genre if str(t).strip()]
    elif raw_genre:
        tags = [g.strip() for g in str(raw_genre).split(",") if g.strip()]

    return {
        "title": (info.get("title") or "").strip() if info.get("title") else "",
        "date": (info.get("date") or "").strip() if info.get("date") else "",
        "actors": actors,
        "tags": tags,
        "maker": (info.get("maker") or "").strip() if info.get("maker") else "",
        "director": (info.get("director") or "").strip() if info.get("director") else "",
        "duration": info.get("duration"),
        "series": (info.get("series") or "").strip() if info.get("series") else "",
        "label": (info.get("label") or "").strip() if info.get("label") else "",
    }


def _meta_value_for_field(metadata: dict, field: str):
    if field == "actor":
        return metadata.get("actors") or []
    if field == "genre":
        return metadata.get("tags") or []
    if field == "date":
        return metadata.get("date") or ""
    return metadata.get(field)


def merge_metadata_for_missing(
    base: dict,
    remote: Optional[dict],
    missing: Sequence[str],
) -> dict:
    """Fill only still-missing fields from remote into a copy of base."""
    out = dict(base)
    if not remote:
        return out
    for field in missing:
        if is_field_present(_meta_value_for_field(out, field)):
            continue
        if field == "actor":
            actors = remote.get("actors") or []
            if actors:
                out["actors"] = list(actors)
        elif field == "genre":
            tags = remote.get("tags") or []
            if tags:
                out["tags"] = list(tags)
        elif field == "date":
            if remote.get("date"):
                out["date"] = remote["date"]
        elif field == "duration":
            if remote.get("duration") is not None:
                out["duration"] = remote["duration"]
        else:
            val = remote.get(field)
            if is_field_present(val):
                out[field] = val
    # Opportunistic optional fill when remote already fetched
    for field in OPTIONAL_NFO_FIELDS:
        if is_field_present(_meta_value_for_field(out, field)):
            continue
        if field == "director" and remote.get("director"):
            out["director"] = remote["director"]
        elif field == "series" and remote.get("series"):
            out["series"] = remote["series"]
        elif field == "label" and remote.get("label"):
            out["label"] = remote["label"]
    # Preserve internal carriers if present
    for k in ("_summary", "_rating"):
        if k in remote and k not in out:
            out[k] = remote[k]
    return out


def local_db_meta_signature(info: dict) -> str:
    """Stable signature of relevant local DB metadata for fingerprinting."""
    meta = info_to_metadata(info or {})
    parts = [
        meta.get("title") or "",
        meta.get("date") or "",
        ",".join(meta.get("actors") or []),
        ",".join(meta.get("tags") or []),
        meta.get("maker") or "",
        str(meta.get("duration") if meta.get("duration") is not None else ""),
        meta.get("director") or "",
        meta.get("series") or "",
        meta.get("label") or "",
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def compute_noop_fingerprint(
    *,
    path: str,
    number: str,
    nfo_mtime: float,
    missing_required: Sequence[str],
    local_db_sig: str,
    policy_version: int = NFO_UPDATE_POLICY_VERSION,
) -> str:
    """Lightweight no-op fingerprint covering path/number, NFO mtime, missing set, DB meta, policy."""
    payload = "|".join([
        path or "",
        (number or "").upper(),
        f"{float(nfo_mtime):.6f}",
        ",".join(sorted(missing_required)),
        local_db_sig or "",
        f"v{int(policy_version)}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# No-op fingerprint store (SQLite)
# ---------------------------------------------------------------------------

def _noop_conn(db_path=None):
    from core.database import get_connection, get_db_path
    if db_path is None:
        db_path = get_db_path()
    return get_connection(db_path)


def ensure_noop_table(db_path=None) -> None:
    conn = _noop_conn(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nfo_update_noop (
                path TEXT PRIMARY KEY NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_noop_fingerprint(path: str, db_path=None) -> Optional[str]:
    ensure_noop_table(db_path)
    conn = _noop_conn(db_path)
    try:
        row = conn.execute(
            "SELECT fingerprint, expires_at FROM nfo_update_noop WHERE path = ?",
            (path,),
        ).fetchone()
        if not row:
            return None
        fingerprint, expires_at = row
        if expires_at and float(expires_at) < time.time():
            conn.execute("DELETE FROM nfo_update_noop WHERE path = ?", (path,))
            conn.commit()
            return None
        return fingerprint
    finally:
        conn.close()


def set_noop_fingerprint(
    path: str,
    fingerprint: str,
    *,
    ttl_seconds: int = NOOP_FINGERPRINT_TTL_SECONDS,
    db_path=None,
) -> None:
    ensure_noop_table(db_path)
    now = time.time()
    conn = _noop_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO nfo_update_noop (path, fingerprint, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                fingerprint = excluded.fingerprint,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (path, fingerprint, now, now + float(ttl_seconds)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_noop_fingerprint(path: str, db_path=None) -> None:
    ensure_noop_table(db_path)
    conn = _noop_conn(db_path)
    try:
        conn.execute("DELETE FROM nfo_update_noop WHERE path = ?", (path,))
        conn.commit()
    finally:
        conn.close()


def is_noop_suppressed(
    path: str,
    fingerprint: str,
    *,
    force: bool = False,
    db_path=None,
) -> bool:
    if force:
        return False
    stored = get_noop_fingerprint(path, db_path=db_path)
    return bool(stored and stored == fingerprint)


# ---------------------------------------------------------------------------
# Candidate detection (NFO file is source of truth)
# ---------------------------------------------------------------------------

def needs_update_from_nfo_fields(
    nfo_fields: Optional[Dict],
    *,
    has_nfo: bool,
    has_number: bool,
) -> Tuple[bool, List[str]]:
    """Whether automatic NFO update should enqueue this video.

    - Missing/unparseable NFO: never enqueue (no destructive rewrite).
    - Only REQUIRED missing fields enqueue; optional-only never does.
    """
    if not has_nfo or not has_number:
        return False, []
    if nfo_fields is None:
        return False, []
    missing = missing_fields(nfo_fields, required_only=True)
    return len(missing) > 0, missing


def needs_update(info: dict, has_nfo: bool = True) -> Tuple[bool, List[str]]:
    """Backward-compatible wrapper using DB/cache fields.

    Prefer check_cache_needs_update / needs_update_from_nfo_fields for
    automatic repair (those parse the actual NFO).
    """
    if not has_nfo or not info.get("num"):
        return False, []
    missing = missing_fields(info, required_only=True)
    return len(missing) > 0, missing


def check_cache_needs_update(
    cache: Dict[str, dict],
    *,
    force: bool = False,
    db_path=None,
) -> Dict:
    """檢查 cache 中需要更新的影片（以實際 NFO XML 為準）

    只檢查有 NFO 檔案的影片（nfo_mtime > 0 或 sidecar 存在）
    Optional-only gaps (director/series/label) do not enqueue.
    Unchanged no-op fingerprints are suppressed unless force=True.
    """
    stats = {
        "need_update": 0,
        "no_title": 0,
        "no_date": 0,
        "no_actor": 0,
        "no_genre": 0,
        "no_maker": 0,
        "no_duration": 0,
        "has_nfo_count": 0,
        "suppressed_noop": 0,
        "paths": [],
    }

    for path, data in cache.items():
        if path.startswith("_"):
            continue

        nfo_mtime = data.get("nfo_mtime", 0) or 0
        has_nfo_flag = nfo_mtime > 0
        info = data.get("info", {}) or {}
        number = info.get("num") or ""

        nfo_path = get_nfo_path_from_video(path)
        if not nfo_path:
            continue

        stats["has_nfo_count"] += 1
        nfo_fields = read_nfo_fields(nfo_path)
        need, missing = needs_update_from_nfo_fields(
            nfo_fields,
            has_nfo=True,
            has_number=bool(number),
        )
        if not need:
            continue

        actual_mtime = nfo_mtime_for_path(nfo_path)
        fp = compute_noop_fingerprint(
            path=path,
            number=number,
            nfo_mtime=actual_mtime,
            missing_required=missing,
            local_db_sig=local_db_meta_signature(info),
        )
        if is_noop_suppressed(path, fp, force=force, db_path=db_path):
            stats["suppressed_noop"] += 1
            continue

        stats["need_update"] += 1
        stats["paths"].append(path)
        for field in missing:
            key = f"no_{field}"
            if key in stats:
                stats[key] += 1

    return stats


# ---------------------------------------------------------------------------
# NFO write helpers
# ---------------------------------------------------------------------------

def update_nfo_user_tags(nfo_path: str, user_tags: List[str]) -> bool:
    """
    Surgical update — 只改 <user_tag> 元素，保留 NFO 中所有其他內容。

    這避免了用 generate_nfo() 全量重寫 NFO 時清掉 <website>、
    <tag>中文字幕</tag>、sidecar 欄位等既有資料的問題。

    Args:
        nfo_path: NFO 檔案路徑（native FS path）
        user_tags: 完整的 user_tags 列表（取代現有所有 <user_tag> 元素）

    Returns:
        True 代表寫入成功，False 代表 NFO 不存在或解析失敗。
        stub 場景（NFO 不存在）：回傳 False，不建立空殼 NFO。
    """
    if not os.path.exists(nfo_path):
        return False
    try:
        raw = Path(nfo_path).read_bytes()
        raw = sanitize_nfo_bytes(raw)
        root = ET.fromstring(raw)
        for old in list(root.findall("user_tag")):
            root.remove(old)
        for tag in user_tags:
            elem = ET.SubElement(root, "user_tag")
            elem.text = tag
        tree = ET.ElementTree(root)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception as e:
        logger.warning("[update_nfo_user_tags] NFO 更新失敗: %s — %s", nfo_path, e)
        return False


def get_element_text(root: ET.Element, tag: str) -> str:
    """取得元素文字"""
    elem = root.find(tag)
    if elem is not None and elem.text:
        return elem.text.strip()
    return ""


def set_element_text(root: ET.Element, tag: str, text: str, after_tag: str = None):
    """設定或新增元素文字"""
    elem = root.find(tag)
    if elem is not None:
        elem.text = text
    else:
        new_elem = ET.Element(tag)
        new_elem.text = text
        if after_tag:
            for i, child in enumerate(root):
                if child.tag == after_tag:
                    root.insert(i + 1, new_elem)
                    return
        root.append(new_elem)


def add_actor(root: ET.Element, actor_name: str):
    """新增演員元素"""
    for actor_elem in root.findall(".//actor/name"):
        if actor_elem.text and actor_elem.text.strip() == actor_name:
            return False

    actor = ET.SubElement(root, "actor")
    name = ET.SubElement(actor, "name")
    name.text = actor_name
    return True


def add_tags_and_genres(root: ET.Element, new_tags: List[str]) -> int:
    """新增 tag 和 genre 元素"""
    existing_tags = set()
    existing_genres = set()

    for elem in root.findall("tag"):
        if elem.text:
            existing_tags.add(elem.text.strip())
    for elem in root.findall("genre"):
        if elem.text:
            existing_genres.add(elem.text.strip())

    added_count = 0
    for tag_text in new_tags:
        tag_text = tag_text.strip()
        if not tag_text:
            continue
        if tag_text not in existing_tags:
            tag_elem = ET.SubElement(root, "tag")
            tag_elem.text = tag_text
            existing_tags.add(tag_text)
            added_count += 1
        if tag_text not in existing_genres:
            genre_elem = ET.SubElement(root, "genre")
            genre_elem.text = tag_text
            existing_genres.add(tag_text)

    return added_count


def indent_xml(elem: ET.Element, level: int = 0):
    """格式化 XML 縮排"""
    i = "\n" + level * "  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for child in elem:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i


def update_nfo_file(nfo_path: str, metadata: dict, info: dict = None) -> Tuple[bool, str]:
    """更新單個 NFO 檔案

    只補全空欄位，不覆蓋已有資料。Missingness is determined from the actual
    NFO XML (source of truth), not from possibly stale DB `info`.

    Args:
        nfo_path: NFO 檔案路徑
        metadata: 來源 metadata（DB 與/或 search_jav）
        info: 保留參數（向後相容）；不再作為 missing 判斷依據

    Returns:
        Tuple[bool, str]: (是否有修改, 訊息)
    """
    del info  # unused; NFO XML is the source of truth
    tree, root = parse_nfo(nfo_path)
    if root is None:
        return False, "無法解析 NFO"

    nfo_fields = extract_nfo_fields_from_root(root)
    modified = False
    changes = []

    # 補標題
    if not is_field_present(nfo_fields.get("title")) and metadata.get("title"):
        existing_title = get_element_text(root, "title")
        if existing_title and not get_element_text(root, "originaltitle"):
            set_element_text(root, "originaltitle", existing_title, after_tag="title")
        set_element_text(root, "title", metadata["title"])
        modified = True
        changes.append("title")

    # 補日期
    if not is_field_present(nfo_fields.get("date")) and metadata.get("date"):
        set_element_text(root, "premiered", metadata["date"])
        modified = True
        changes.append("date")

    # 補演員
    if not is_field_present(nfo_fields.get("actor")) and metadata.get("actors"):
        for actor_name in metadata["actors"]:
            if actor_name:
                add_actor(root, actor_name)
        modified = True
        changes.append("actors")

    # 補標籤
    if not is_field_present(nfo_fields.get("genre")) and metadata.get("tags"):
        added = add_tags_and_genres(root, metadata["tags"])
        if added > 0:
            modified = True
            changes.append(f"tags({added})")

    # 補片商
    if not is_field_present(nfo_fields.get("maker")) and metadata.get("maker"):
        set_element_text(root, "studio", metadata["maker"])
        modified = True
        changes.append("maker")

    # 補導演（optional）
    if not is_field_present(nfo_fields.get("director")) and metadata.get("director"):
        set_element_text(root, "director", metadata["director"])
        modified = True
        changes.append("director")

    # 補時長（duration=0 也要寫入）
    if not is_field_present(nfo_fields.get("duration")) and metadata.get("duration") is not None:
        set_element_text(root, "runtime", str(metadata["duration"]))
        modified = True
        changes.append("runtime")

    # 補系列（<set><name> 巢狀結構）
    if not is_field_present(nfo_fields.get("series")) and metadata.get("series"):
        set_elem = root.find("set")
        if set_elem is None:
            set_elem = ET.SubElement(root, "set")
        name_elem = set_elem.find("name")
        if name_elem is None:
            name_elem = ET.SubElement(set_elem, "name")
        name_elem.text = metadata["series"]
        modified = True
        changes.append("series")

    # 補廠牌標籤
    if not is_field_present(nfo_fields.get("label")) and metadata.get("label"):
        set_element_text(root, "label", metadata["label"])
        modified = True
        changes.append("label")

    # 補 plot（fill-if-missing）
    _summary = metadata.get("_summary", "")
    if not get_element_text(root, "plot") and _summary:
        set_element_text(root, "plot", _summary, after_tag="premiered")
        modified = True
        changes.append("plot")

    # 補 rating
    _rating = metadata.get("_rating")
    if not get_element_text(root, "rating") and _rating is not None and _rating > 0:
        set_element_text(root, "rating", f"{_rating * 2:.1f}", after_tag="plot")
        modified = True
        changes.append("rating")

    # 補 mpaa（fill-if-missing）
    if not get_element_text(root, "mpaa"):
        set_element_text(root, "mpaa", "JP-18+", after_tag="rating")
        modified = True
        changes.append("mpaa")

    if modified:
        indent_xml(root)
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return True, ",".join(changes)

    return False, "無需更新"


def _sync_video_nfo_mtime(video_path: str, nfo_path: str, db_path=None) -> None:
    """After a successful NFO write, sync the row's nfo_mtime to the file mtime."""
    try:
        from core.database import VideoRepository, get_db_path
        mtime = nfo_mtime_for_path(nfo_path)
        repo = VideoRepository(db_path or get_db_path())
        repo.set_nfo_mtime(video_path, mtime)
    except Exception:
        logger.exception("同步 nfo_mtime 失敗: %s", video_path)


# ---------------------------------------------------------------------------
# Batch update generator (SSE)
# ---------------------------------------------------------------------------

def _fingerprint_for_current_state(
    path: str,
    num: str,
    nfo_path: str,
    missing_required: Sequence[str],
    info: dict,
) -> str:
    """Compute no-op fingerprint from live NFO mtime + missing set + DB meta."""
    return compute_noop_fingerprint(
        path=path,
        number=num,
        nfo_mtime=nfo_mtime_for_path(nfo_path),
        missing_required=list(missing_required),
        local_db_sig=local_db_meta_signature(info),
    )


def _local_can_fill_any(
    local_meta: dict,
    missing_required: Sequence[str],
    missing_optional: Sequence[str],
) -> bool:
    for f in missing_required:
        if is_field_present(_meta_value_for_field(local_meta, f)):
            return True
    for f in missing_optional:
        if is_field_present(_meta_value_for_field(local_meta, f)):
            return True
    return False


def update_videos_generator(
    cache: Dict[str, dict],
    paths: List[str],
    *,
    force: bool = False,
    db_path=None,
    search_fn=None,
) -> Generator[dict, None, dict]:
    """更新影片的生成器（用於 SSE 串流）

    Flow per path:
      1. Parse actual NFO; if complete → skipped_complete (no network)
      2. Suppress unchanged no-op fingerprint
      3. Write all locally available missing fields immediately (no network wait)
      4. Reparse NFO + recompute missing; sync nfo_mtime after local write
      5. Call search_jav only for fields still missing after the local write
      6. Merge remote only for still-missing fields; write fill-if-missing
      7. Count at most one ``updated`` per path (local and/or remote write)
      8. On residual missing after local/remote: no-op fingerprint for *post* state
      9. Network failure after a successful local write keeps the local update
         and does not mark the video failed

    Args:
        cache: path → {nfo_mtime, info}
        paths: selected video paths
        force: ignore no-op fingerprint suppression
        db_path: optional SQLite path for fingerprint store
        search_fn: injectable search (default search_jav); for tests

    Yields:
        進度訊息 dict

    Returns:
        統計結果 dict
    """
    if search_fn is None:
        search_fn = search_jav

    t0 = time.time()
    stats = {
        "selected": len(paths),
        "updated": 0,
        "skipped_complete": 0,
        "suppressed_noop": 0,
        "no_metadata": 0,
        "failed": 0,
        # legacy keys kept for older callers
        "total": len(paths),
        "success": 0,
        "skipped": 0,
        "no_nfo": 0,
        "elapsed": 0.0,
    }

    for i, path in enumerate(paths, 1):
        data = cache.get(path, {})
        info = data.get("info", {}) or {}
        num = info.get("num", "") or ""

        yield {
            "type": "progress",
            "current": i,
            "total": len(paths),
            "num": num,
            "status": f"處理 {num}" if num else f"處理 {i}/{len(paths)}",
        }

        nfo_path = get_nfo_path_from_video(path)
        if not nfo_path:
            yield {
                "type": "log",
                "level": "warn",
                "message": f"[{i}] {num}: NFO 不存在",
            }
            stats["no_nfo"] += 1
            stats["failed"] += 1
            continue

        nfo_fields = read_nfo_fields(nfo_path)
        if nfo_fields is None:
            yield {
                "type": "log",
                "level": "warn",
                "message": f"[{i}] {num}: NFO 無法解析，跳過（不重寫）",
            }
            stats["failed"] += 1
            continue

        missing_required = missing_fields(nfo_fields, required_only=True)
        missing_optional = [
            f for f in OPTIONAL_NFO_FIELDS if not is_field_present(nfo_fields.get(f))
        ]

        # Cheap local preflight: already complete → skip without network
        if not missing_required:
            yield {
                "type": "log",
                "level": "info",
                "message": f"[{i}] {num}: 必要欄位已完整，跳過",
            }
            stats["skipped_complete"] += 1
            stats["skipped"] += 1
            clear_noop_fingerprint(path, db_path=db_path)
            continue

        fingerprint = _fingerprint_for_current_state(
            path, num, nfo_path, missing_required, info
        )
        if is_noop_suppressed(path, fingerprint, force=force, db_path=db_path):
            yield {
                "type": "log",
                "level": "info",
                "message": f"[{i}] {num}: 先前已嘗試且無結果（指紋未變），跳過",
            }
            stats["suppressed_noop"] += 1
            stats["skipped"] += 1
            continue

        local_meta = info_to_metadata(info)
        video_updated = False

        # ---- Phase A: apply all locally available fields first (no network) ----
        if _local_can_fill_any(local_meta, missing_required, missing_optional):
            try:
                local_changed, local_msg = update_nfo_file(nfo_path, local_meta, info)
            except Exception:
                logger.exception("本地寫入 NFO 失敗: %s", num)
                yield {
                    "type": "log",
                    "level": "error",
                    "message": f"[{i}] {num}: 更新 NFO 發生錯誤",
                }
                stats["failed"] += 1
                continue

            if local_changed:
                video_updated = True
                _sync_video_nfo_mtime(path, nfo_path, db_path=db_path)
                # Reparse after local write; recompute missing before network
                nfo_fields = read_nfo_fields(nfo_path)
                if nfo_fields is None:
                    yield {
                        "type": "log",
                        "level": "error",
                        "message": f"[{i}] {num}: 本地寫入後 NFO 無法解析",
                    }
                    stats["failed"] += 1
                    continue
                missing_required = missing_fields(nfo_fields, required_only=True)
                missing_optional = [
                    f
                    for f in OPTIONAL_NFO_FIELDS
                    if not is_field_present(nfo_fields.get(f))
                ]
                yield {
                    "type": "log",
                    "level": "info",
                    "message": f"[{i}] {num}: 本地已寫入 ({local_msg}) [local]",
                }

        # ---- Phase B: network only for fields still missing after local write ----
        if missing_required:
            # Post-local remaining required set (local values already on disk)
            still_missing = list(missing_required)

            if not num:
                yield {
                    "type": "log",
                    "level": "warn",
                    "message": f"[{i}] 無番號且本地資料不足，跳過",
                }
                if video_updated:
                    stats["updated"] += 1
                    stats["success"] += 1
                    # Residual missing after local-only path → fingerprint post-local
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    stats["no_metadata"] += 1
                    stats["skipped"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                continue

            yield {
                "type": "log",
                "level": "info",
                "message": f"[{i}] {num}: 本地後仍缺 {','.join(still_missing)}，搜尋中...",
            }

            remote = None
            try:
                remote = search_fn(num)
            except Exception:
                logger.exception("搜尋 JAV 資料失敗: %s", num)
                yield {
                    "type": "log",
                    "level": "error",
                    "message": f"[{i}] {num}: 搜尋發生錯誤",
                }
                # Local write already applied → keep it; do not fail the video
                if video_updated:
                    stats["updated"] += 1
                    stats["success"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    stats["failed"] += 1
                continue

            if not remote:
                yield {
                    "type": "log",
                    "level": "warn",
                    "message": f"[{i}] {num}: 找不到資料",
                }
                if video_updated:
                    stats["updated"] += 1
                    stats["success"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    stats["no_metadata"] += 1
                    stats["skipped"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                continue

            fill_meta = merge_metadata_for_missing(
                dict(local_meta), remote, still_missing
            )
            can_fill_required = any(
                is_field_present(_meta_value_for_field(fill_meta, f))
                for f in still_missing
            )
            can_fill_optional = any(
                is_field_present(_meta_value_for_field(fill_meta, f))
                for f in missing_optional
            )

            if not can_fill_required and not can_fill_optional:
                yield {
                    "type": "log",
                    "level": "warn",
                    "message": f"[{i}] {num}: 遠端無可用資料補全剩餘欄位",
                }
                if video_updated:
                    stats["updated"] += 1
                    stats["success"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    stats["no_metadata"] += 1
                    stats["skipped"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                continue

            try:
                remote_changed, remote_msg = update_nfo_file(nfo_path, fill_meta, info)
            except Exception:
                logger.exception("更新 NFO 失敗: %s", num)
                yield {
                    "type": "log",
                    "level": "error",
                    "message": f"[{i}] {num}: 更新 NFO 發生錯誤",
                }
                if video_updated:
                    # Local write already on disk — keep update, do not fail whole video
                    stats["updated"] += 1
                    stats["success"] += 1
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    stats["failed"] += 1
                continue

            if remote_changed:
                video_updated = True
                _sync_video_nfo_mtime(path, nfo_path, db_path=db_path)
                yield {
                    "type": "log",
                    "level": "info",
                    "message": f"[{i}] {num}: 已更新 ({remote_msg})",
                }
                # Reparse to see residual missing for fingerprint decision
                nfo_fields = read_nfo_fields(nfo_path)
                if nfo_fields is not None:
                    missing_required = missing_fields(nfo_fields, required_only=True)
                else:
                    missing_required = []

            if video_updated:
                stats["updated"] += 1
                stats["success"] += 1
                if missing_required:
                    # Partial fill still residual — suppress next identical run
                    set_noop_fingerprint(
                        path,
                        _fingerprint_for_current_state(
                            path, num, nfo_path, missing_required, info
                        ),
                        db_path=db_path,
                    )
                else:
                    clear_noop_fingerprint(path, db_path=db_path)
            else:
                yield {
                    "type": "log",
                    "level": "info",
                    "message": f"[{i}] {num}: {remote_msg}",
                }
                stats["skipped"] += 1
                stats["no_metadata"] += 1
                set_noop_fingerprint(
                    path,
                    _fingerprint_for_current_state(
                        path, num, nfo_path, missing_required, info
                    ),
                    db_path=db_path,
                )
            continue

        # ---- No remaining required fields after local write ----
        if video_updated:
            stats["updated"] += 1
            stats["success"] += 1
            clear_noop_fingerprint(path, db_path=db_path)
        else:
            # Neither local nor network could help (local had nothing useful)
            yield {
                "type": "log",
                "level": "warn",
                "message": f"[{i}] {num}: 無可用資料補全",
            }
            stats["no_metadata"] += 1
            stats["skipped"] += 1
            set_noop_fingerprint(
                path,
                _fingerprint_for_current_state(
                    path, num, nfo_path, missing_required, info
                ),
                db_path=db_path,
            )

    stats["elapsed"] = round(time.time() - t0, 2)
    return stats
