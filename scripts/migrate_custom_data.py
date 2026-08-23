"""Merge an older customized OpenAver profile into this integration tree."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.atomic_write import atomic_write
from core.database import init_db


UNIQUE_TABLES = {
    "videos": "path",
    "actress_aliases": "primary_name",
    "tag_aliases": "primary_name",
    "actresses": "name",
}
HISTORY_COLUMNS = (
    "path",
    "number",
    "old_title",
    "old_original_title",
    "new_title",
    "new_original_title",
    "source",
    "created_at",
)


def _app_root(root: Path) -> Path:
    root = root.resolve()
    nested = root / "app"
    if (nested / "output" / "openaver.db").is_file():
        return nested
    return root


def _deep_merge(base: Any, overlay: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(overlay, dict):
        return overlay
    result = dict(base)
    for key, value in overlay.items():
        result[key] = _deep_merge(result[key], value) if key in result else value
    return result


def merge_config(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Keep official-only defaults while applying the user's older settings."""

    merged = _deep_merge(target, {key: value for key, value in source.items() if key != "sources"})
    target_sources = {
        item.get("id"): item
        for item in target.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    source_sources = [
        item for item in source.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    ]
    sources = [
        _deep_merge(target_sources.get(item["id"], {}), item)
        for item in source_sources
    ]
    source_ids = {item["id"] for item in source_sources}
    sources.extend(
        item for item in target.get("sources", [])
        if isinstance(item, dict) and item.get("id") not in source_ids
    )
    if sources:
        merged["sources"] = sources
    return merged


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _merge_unique_table(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    key: str,
) -> int:
    source_columns = _table_columns(source, table)
    target_columns = set(_table_columns(target, table))
    columns = [column for column in source_columns if column in target_columns and column != "id"]
    if key not in columns:
        return 0
    rows = source.execute(
        f"SELECT {', '.join(columns)} FROM {table}"
    ).fetchall()
    update_columns = [column for column in columns if column not in {key, "created_at"}]
    placeholders = ", ".join("?" for _ in columns)
    update_sql = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
    conflict_sql = f"DO UPDATE SET {update_sql}" if update_sql else "DO NOTHING"
    target.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({key}) {conflict_sql}",
        rows,
    )
    return len(rows)


def _merge_translation_history(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> int:
    source_columns = set(_table_columns(source, "title_translation_history"))
    target_columns = set(_table_columns(target, "title_translation_history"))
    columns = [column for column in HISTORY_COLUMNS if column in source_columns and column in target_columns]
    if "path" not in columns:
        return 0
    inserted = 0
    placeholders = ", ".join("?" for _ in columns)
    where = " AND ".join(f"{column} IS ?" for column in columns)
    for row in source.execute(f"SELECT {', '.join(columns)} FROM title_translation_history"):
        if target.execute(
            f"SELECT 1 FROM title_translation_history WHERE {where} LIMIT 1",
            tuple(row),
        ).fetchone():
            continue
        target.execute(
            f"INSERT INTO title_translation_history ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(row),
        )
        inserted += 1
    return inserted


def migrate_database(source_path: Path, target_path: Path) -> dict[str, int]:
    init_db(target_path)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    counts: dict[str, int] = {}
    try:
        source_tables = {
            row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        target.execute("BEGIN IMMEDIATE")
        for table, key in UNIQUE_TABLES.items():
            counts[table] = (
                _merge_unique_table(source, target, table, key)
                if table in source_tables
                else 0
            )
        counts["title_translation_history"] = (
            _merge_translation_history(source, target)
            if "title_translation_history" in source_tables
            else 0
        )
        target.commit()
        return counts
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


def merge_rename_history(source_path: Path, target_path: Path) -> int:
    if not source_path.is_file():
        return 0
    existing_ids: set[str] = set()
    existing_lines: list[str] = []
    if target_path.is_file():
        existing_lines = target_path.read_text(encoding="utf-8").splitlines()
        for line in existing_lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("id"):
                existing_ids.add(str(value["id"]))
    additions: list[str] = []
    for line in source_path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_id = str(value.get("id", "")) if isinstance(value, dict) else ""
        if not event_id or event_id in existing_ids:
            continue
        existing_ids.add(event_id)
        additions.append(json.dumps(value, ensure_ascii=False))
    if additions:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(target_path, mode="w", encoding="utf-8") as handle:
            handle.write("\n".join([*existing_lines, *additions]) + "\n")
    return len(additions)


def migrate_profile(source_root: Path, target_root: Path) -> dict[str, Any]:
    source = _app_root(source_root)
    target = target_root.resolve()
    source_db = source / "output" / "openaver.db"
    target_db = target / "output" / "openaver.db"
    if not source_db.is_file():
        raise FileNotFoundError(f"Source database not found: {source_db}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = target / "backups" / f"pre-custom-migration-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    target_config = target / "web" / "config.json"
    for path in (target_db, target_config, target / "output" / "rename_history.jsonl"):
        if path.is_file():
            shutil.copy2(path, backup_dir / path.name)

    target_db.parent.mkdir(parents=True, exist_ok=True)
    counts = migrate_database(source_db, target_db)

    source_config = source / "web" / "config.json"
    if source_config.is_file():
        base_config_path = target_config if target_config.is_file() else target / "web" / "config.default.json"
        base = json.loads(base_config_path.read_text(encoding="utf-8"))
        incoming = json.loads(source_config.read_text(encoding="utf-8"))
        merged = merge_config(base, incoming)
        with atomic_write(target_config, mode="w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    history_count = merge_rename_history(
        source / "output" / "rename_history.jsonl",
        target / "output" / "rename_history.jsonl",
    )
    return {
        "backup_dir": str(backup_dir),
        "database": counts,
        "rename_history": history_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()
    print(  # noqa: T201 -- CLI result is intentionally written to stdout
        json.dumps(migrate_profile(args.source_root, args.target_root), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()
