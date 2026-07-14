"""Field provenance, manual locks, and translation version helpers.

Stored on videos as JSON columns:
  - field_sources: {"title":"dmm","actresses":"javbus","cover":"dmm","maker":"javdb"}
  - field_locks:   {"title":true,"actresses":true,"cover":true,"maker":true,"filename":true}
  - translation_meta: {
        "provider","model","prompt_version","translated_at",
        "confirmed","source_text_hash","locked_by_confirm"
    }
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Optional

# Logical fields we track for provenance / locks
TRACKED_FIELDS = (
    "title",           # display title (often Chinese after translate)
    "original_title",  # Japanese / source title
    "actresses",
    "maker",
    "cover",
    "tags",
    "filename",        # rename protection (logical, not a DB column of content)
)

# Prompt template version — bump when LANGUAGE_PROMPTS wording changes materially
TRANSLATE_PROMPT_VERSION = "v1"


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            data = json.loads(value)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def parse_json_map(value: Any) -> dict:
    return _as_dict(value)


def dumps_json_map(value: Mapping | None) -> str:
    return json.dumps(dict(value or {}), ensure_ascii=False)


def is_locked(locks: Any, field: str) -> bool:
    data = _as_dict(locks)
    return bool(data.get(field))


def set_lock(locks: Any, field: str, locked: bool = True) -> dict:
    data = _as_dict(locks)
    if locked:
        data[field] = True
    else:
        data.pop(field, None)
    return data


def set_source(sources: Any, field: str, source: str) -> dict:
    data = _as_dict(sources)
    if source:
        data[field] = source
    return data


def merge_sources(existing: Any, updates: Mapping[str, str]) -> dict:
    data = _as_dict(existing)
    for field, source in updates.items():
        if source:
            data[field] = source
    return data


def source_text_hash(text: str) -> str:
    raw = (text or "").strip().encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def build_translation_meta(
    *,
    provider: str,
    model: str,
    prompt_version: str = TRANSLATE_PROMPT_VERSION,
    source_text: str = "",
    confirmed: bool = False,
    previous: Any = None,
) -> dict:
    prev = _as_dict(previous)
    return {
        "provider": provider or prev.get("provider") or "",
        "model": model or prev.get("model") or "",
        "prompt_version": prompt_version or TRANSLATE_PROMPT_VERSION,
        "translated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "confirmed": bool(confirmed),
        "source_text_hash": source_text_hash(source_text) if source_text else prev.get("source_text_hash", ""),
        "locked_by_confirm": bool(confirmed),
    }


def should_skip_retranslate(
    translation_meta: Any,
    *,
    source_text: str,
    current_prompt_version: str = TRANSLATE_PROMPT_VERSION,
    force: bool = False,
) -> tuple[bool, str]:
    """Return (skip, reason). Skip confirmed translations when source/prompt unchanged."""
    if force:
        return False, ""
    meta = _as_dict(translation_meta)
    if not meta:
        return False, ""
    if not meta.get("confirmed"):
        return False, ""
    if meta.get("prompt_version") != current_prompt_version:
        return False, ""  # prompt changed → allow retranslate
    if source_text and meta.get("source_text_hash") and meta.get("source_text_hash") != source_text_hash(source_text):
        return False, ""  # source title changed
    return True, "translation_confirmed"


def apply_field_locks(
    existing_values: Mapping[str, Any],
    incoming_values: MutableMapping[str, Any],
    locks: Any,
    *,
    field_map: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Copy locked fields from existing into incoming. Returns list of preserved fields.

    field_map maps lock-key → value-key when names differ (e.g. cover → cover_path).
    """
    locks_d = _as_dict(locks)
    preserved: list[str] = []
    fmap = dict(field_map or {})
    for lock_field, locked in locks_d.items():
        if not locked:
            continue
        value_key = fmap.get(lock_field, lock_field)
        if value_key in existing_values:
            incoming_values[value_key] = existing_values[value_key]
            preserved.append(lock_field)
    return preserved


def provenance_from_scraper(source_used: str, filled_fields: Iterable[str]) -> dict[str, str]:
    """Build field_sources updates from enricher filled list + cover."""
    src = (source_used or "scraper").strip() or "scraper"
    out: dict[str, str] = {}
    for f in filled_fields:
        if f in TRACKED_FIELDS or f in {"title", "original_title", "actresses", "maker", "tags", "release_date"}:
            # Map release_date etc. only if tracked; still record common fields
            key = f if f in TRACKED_FIELDS else f
            if key in TRACKED_FIELDS or key in {"title", "original_title", "actresses", "maker", "tags"}:
                out[key if key in TRACKED_FIELDS else key] = src
    return out
