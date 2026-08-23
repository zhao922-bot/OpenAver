"""Crash-consistent title translation history and NFO synchronization."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any
import xml.etree.ElementTree as ET

from core.atomic_write import atomic_write
from core.database.connection import get_connection
from core.logger import get_logger
from core.nfo_utils import sanitize_nfo_bytes
from core.path_utils import uri_to_local_fs_path


logger = get_logger(__name__)


def translation_history_keys(db_path: Path) -> tuple[set[str], set[str]]:
    """Return paths and numbers with at least one usable rollback snapshot."""

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT path, number FROM title_translation_history"
        ).fetchall()
        unambiguous_numbers = {
            row[0]
            for row in conn.execute(
                """
                SELECT number
                FROM title_translation_history
                WHERE number != ''
                GROUP BY number
                HAVING COUNT(DISTINCT path) = 1
                """
            ).fetchall()
        }
        return (
            {row[0] for row in rows if row[0]},
            unambiguous_numbers,
        )
    finally:
        conn.close()


def repath_translation_history(db_path: Path, old_path: str, new_path: str) -> int:
    """Keep rollback snapshots attached to a video after its library URI changes."""

    if not old_path or not new_path or old_path == new_path:
        return 0
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "UPDATE title_translation_history SET path = ? WHERE path = ?",
            (new_path, old_path),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def apply_translation(
    db_path: Path,
    *,
    path: str,
    number: str,
    expected_title: str,
    expected_original_title: str,
    title: str,
    original_title: str,
    source: str,
) -> str:
    """Save rollback history and update the title in one transaction."""

    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT title, original_title FROM videos WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return "not_found"
        current_title = row[0] or ""
        current_original = row[1] or ""
        if current_title != expected_title or current_original != expected_original_title:
            conn.rollback()
            return "conflict"
        if current_title == title and current_original == original_title:
            conn.rollback()
            return "unchanged"

        conn.execute(
            """
            INSERT INTO title_translation_history (
                path, number, old_title, old_original_title,
                new_title, new_original_title, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                number,
                current_title,
                current_original,
                title,
                original_title,
                source,
            ),
        )
        conn.execute(
            """
            UPDATE videos
            SET title = ?, original_title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE path = ?
            """,
            (title, original_title, path),
        )
        conn.commit()
        return "updated"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_translation(db_path: Path, *, path: str, number: str) -> dict[str, Any] | None:
    """Restore and consume the latest matching snapshot atomically."""

    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM title_translation_history
            WHERE path = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (path,),
        ).fetchone()
        if row is None and number:
            fallback_rows = conn.execute(
                """
                SELECT * FROM title_translation_history
                WHERE number = ?
                ORDER BY id DESC
                """,
                (number,),
            ).fetchall()
            fallback_paths = {item["path"] for item in fallback_rows}
            if len(fallback_paths) == 1:
                row = fallback_rows[0]
        if row is None:
            conn.rollback()
            return None
        history = dict(row)
        cursor = conn.execute(
            """
            UPDATE videos
            SET title = ?, original_title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE path = ?
            """,
            (history.get("old_title") or "", history.get("old_original_title") or "", path),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return None
        conn.execute(
            "DELETE FROM title_translation_history WHERE id = ?",
            (history["id"],),
        )
        conn.commit()
        return history
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_nfo_title(video_uri: str, title: str, original_title: str, path_mappings: dict) -> float | None:
    """Atomically update a local NFO and return its new mtime when present."""

    video_path = Path(uri_to_local_fs_path(video_uri, path_mappings))
    nfo_path = video_path.with_suffix(".nfo")
    if not nfo_path.is_file():
        return None
    try:
        root = ET.fromstring(sanitize_nfo_bytes(nfo_path.read_bytes()))

        def set_value(name: str, value: str) -> None:
            node = root.find(name)
            if node is None:
                node = ET.SubElement(root, name)
            node.text = value

        set_value("title", title or "")
        set_value("originaltitle", original_title or "")
        ET.indent(root, space="  ")
        content = ET.tostring(root, encoding="unicode", xml_declaration=False)
        with atomic_write(nfo_path, mode="w", encoding="utf-8") as handle:
            handle.write('<?xml version="1.0" encoding="utf-8"?>\n')
            handle.write(content)
        return nfo_path.stat().st_mtime
    except (OSError, ET.ParseError):
        logger.exception("Could not synchronize translated title to NFO: %s", nfo_path)
        return None
