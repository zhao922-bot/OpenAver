import json
import sqlite3

from core.database import init_db
from scripts.migrate_custom_data import merge_config, merge_rename_history, migrate_database


def test_merge_config_keeps_official_sources_and_new_nested_defaults():
    target = {
        "gallery": {"directories": [], "cover_badges": {"enabled": False}},
        "sources": [
            {"id": "dmm", "enabled": True, "config": {"official": True}},
            {"id": "new-source", "enabled": False},
        ],
    }
    source = {
        "gallery": {"directories": ["D:/Videos/JAV"]},
        "sources": [{"id": "dmm", "enabled": False}],
        "download": {"fragment_threads": 12},
    }

    merged = merge_config(target, source)

    assert merged["gallery"] == {
        "directories": ["D:/Videos/JAV"],
        "cover_badges": {"enabled": False},
    }
    assert merged["sources"] == [
        {"id": "dmm", "enabled": False, "config": {"official": True}},
        {"id": "new-source", "enabled": False},
    ]
    assert merged["download"]["fragment_threads"] == 12


def test_database_migration_is_idempotent_and_uses_common_columns(tmp_path):
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    init_db(source_path)
    init_db(target_path)
    source = sqlite3.connect(source_path)
    source.execute(
        "INSERT INTO videos (path, number, title, original_title) VALUES (?, ?, ?, ?)",
        ("file:///D:/Videos/JAV/ABC-123.mp4", "ABC-123", "中文标题", "日本語"),
    )
    source.execute(
        "INSERT INTO actress_aliases (primary_name, aliases, source) VALUES (?, ?, ?)",
        ("梓光莉", json.dumps(["梓ヒカリ"], ensure_ascii=False), "manual"),
    )
    source.execute(
        """INSERT INTO title_translation_history
           (path, number, old_title, new_title, source)
           VALUES (?, ?, ?, ?, ?)""",
        ("file:///D:/Videos/JAV/ABC-123.mp4", "ABC-123", "日本語", "中文标题", "ai"),
    )
    source.commit()
    source.close()

    first = migrate_database(source_path, target_path)
    second = migrate_database(source_path, target_path)

    target = sqlite3.connect(target_path)
    assert target.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    assert target.execute("SELECT COUNT(*) FROM actress_aliases").fetchone()[0] == 1
    assert target.execute("SELECT COUNT(*) FROM title_translation_history").fetchone()[0] == 1
    target.close()
    assert first["videos"] == 1
    assert second["videos"] == 1


def test_rename_history_merge_skips_duplicate_and_invalid_events(tmp_path):
    source = tmp_path / "source.jsonl"
    target = tmp_path / "target.jsonl"
    source.write_text(
        '\n'.join([
            json.dumps({"id": "one", "status": "completed"}),
            "not-json",
            json.dumps({"id": "two", "status": "completed"}),
        ]),
        encoding="utf-8",
    )
    target.write_text(json.dumps({"id": "one", "status": "rolled_back"}) + "\n", encoding="utf-8")

    assert merge_rename_history(source, target) == 1
    assert merge_rename_history(source, target) == 0
    assert [json.loads(line)["id"] for line in target.read_text(encoding="utf-8").splitlines()] == [
        "one",
        "two",
    ]
