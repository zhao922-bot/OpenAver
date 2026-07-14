"""Incremental scan diff: decide which files need full scan_file().

Compare filesystem snapshot (path, mtime, size, nfo_mtime) against DB index.
Windows path keys are casefolded so D:\\Videos and d:\\videos match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from core.path_utils import CURRENT_ENV, normalize_for_compare, to_file_uri


def path_key(uri_or_path: str) -> str:
    """Stable compare key for a file URI or FS path."""
    return normalize_for_compare(uri_or_path or "")


@dataclass
class ScanDiffResult:
    needs_scan: list[dict] = field(default_factory=list)  # file_info dicts
    unchanged: int = 0
    new_count: int = 0
    changed_count: int = 0
    current_uris: set[str] = field(default_factory=set)
    # URIs present in DB under scanned roots but missing on disk (caller filters by dir)
    deleted_candidates: list[str] = field(default_factory=list)


def build_db_index_rows(
    rows: Iterable[tuple],
) -> dict[str, tuple[float, float, int]]:
    """Build {path_key: (mtime, nfo_mtime, size_bytes)} from DB rows.

    Each row: (path, mtime, nfo_mtime, size_bytes?)
    """
    index: dict[str, tuple[float, float, int]] = {}
    for row in rows:
        path = row[0] or ""
        mtime = float(row[1] or 0)
        nfo_mtime = float(row[2] or 0)
        size = int(row[3] or 0) if len(row) > 3 else 0
        index[path_key(path)] = (mtime, nfo_mtime, size)
        # Also keep original-path map via side channel? Callers need original URI for delete.
    return index


def build_db_uri_by_key(rows: Iterable[tuple]) -> dict[str, str]:
    """{path_key: original_db_path_uri} for deletion reporting."""
    out: dict[str, str] = {}
    for row in rows:
        path = row[0] or ""
        out[path_key(path)] = path
    return out


def file_changed(
    db_entry: Optional[tuple],
    file_mtime: float,
    file_nfo_mtime: float,
    file_size: int,
    *,
    use_size: bool = True,
) -> bool:
    """True if DB entry missing or mtime/nfo/size differ."""
    if db_entry is None:
        return True
    db_mtime, db_nfo, db_size = db_entry[0], db_entry[1], db_entry[2] if len(db_entry) > 2 else 0
    if float(db_mtime or 0) != float(file_mtime or 0):
        return True
    if float(db_nfo or 0) != float(file_nfo_mtime or 0):
        return True
    if use_size:
        # Legacy rows with size=0: skip size check to avoid mass rescan once
        if int(db_size or 0) > 0 and int(db_size or 0) != int(file_size or 0):
            return True
    return False


def diff_files(
    file_infos: list[dict],
    db_index: Mapping[str, tuple],
    db_uri_by_key: Mapping[str, str],
    *,
    path_mappings: dict | None = None,
    force_full: bool = False,
    use_size: bool = True,
) -> ScanDiffResult:
    """Compare fast_scan results to DB index.

    file_info keys: path, mtime, size, nfo_mtime
    db_index keys must be path_key(uri); values (mtime, nfo_mtime, size).
    """
    result = ScanDiffResult()
    seen_keys: set[str] = set()

    for file_info in file_infos:
        fs_path = file_info["path"]
        file_uri = to_file_uri(fs_path, path_mappings or {})
        result.current_uris.add(file_uri)
        key = path_key(file_uri)
        seen_keys.add(key)

        if force_full:
            result.needs_scan.append(file_info)
            if key in db_index:
                result.changed_count += 1
            else:
                result.new_count += 1
            continue

        db_entry = db_index.get(key)
        if db_entry is None:
            result.needs_scan.append(file_info)
            result.new_count += 1
        elif file_changed(
            db_entry,
            file_info.get("mtime", 0),
            file_info.get("nfo_mtime", 0),
            file_info.get("size", 0),
            use_size=use_size,
        ):
            result.needs_scan.append(file_info)
            result.changed_count += 1
        else:
            result.unchanged += 1

    # Deleted = in DB index but not seen (caller should further filter by directory)
    for key, uri in db_uri_by_key.items():
        if key not in seen_keys:
            result.deleted_candidates.append(uri)

    return result
