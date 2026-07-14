"""DB-level field locks, sources, and translation_meta persistence."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402
from core.database import Video, VideoRepository  # noqa: E402
from core.field_meta import is_locked, build_translation_meta  # noqa: E402


class DatabaseLocksTests(unittest.TestCase):
    def _seed(self, repo: VideoRepository) -> Video:
        v = Video(
            path="file:///D:/Videos/JAV/TEST-001/TEST-001.mp4",
            number="TEST-001",
            title="旧标题",
            original_title="古いタイトル",
            actresses=["女優A"],
            maker="Maker",
            cover_path="file:///D:/Videos/JAV/TEST-001/TEST-001.jpg",
        )
        repo.upsert(v)
        return v

    def test_set_field_lock_roundtrip(self) -> None:
        with TempDb():
            repo = VideoRepository()
            v = self._seed(repo)
            self.assertTrue(repo.set_field_lock(v.path, "title", True))
            loaded = repo.get_by_path(v.path)
            assert loaded is not None
            self.assertTrue(is_locked(loaded.field_locks, "title"))

            self.assertTrue(repo.set_field_lock(v.path, "title", False))
            loaded2 = repo.get_by_path(v.path)
            assert loaded2 is not None
            self.assertFalse(is_locked(loaded2.field_locks, "title"))

    def test_update_title_manual_locks_and_source(self) -> None:
        with TempDb():
            repo = VideoRepository()
            v = self._seed(repo)
            ok = repo.update_title(
                v.path,
                "人工中文标题",
                v.original_title,
                lock=True,
                source="manual",
            )
            self.assertTrue(ok)
            loaded = repo.get_by_path(v.path)
            assert loaded is not None
            self.assertEqual(loaded.title, "人工中文标题")
            self.assertTrue(is_locked(loaded.field_locks, "title"))
            self.assertEqual(loaded.field_sources.get("title"), "manual")

    def test_translation_meta_persist(self) -> None:
        with TempDb():
            repo = VideoRepository()
            v = self._seed(repo)
            meta = build_translation_meta(
                provider="openai",
                model="deepseek",
                source_text=v.original_title,
                confirmed=True,
            )
            ok = repo.update_title(
                v.path,
                "翻译后的中文",
                v.original_title,
                lock=True,
                source="translate:openai",
                translation_meta=meta,
            )
            self.assertTrue(ok)
            loaded = repo.get_by_path(v.path)
            assert loaded is not None
            self.assertTrue(loaded.translation_meta.get("confirmed"))
            self.assertEqual(loaded.translation_meta.get("provider"), "openai")
            self.assertEqual(loaded.field_sources.get("title"), "translate:openai")

    def test_upsert_preserves_empty_meta_maps(self) -> None:
        """Upsert with empty field_sources should not wipe existing locks when CASE applies.

        Note: our upsert CASE keeps existing when excluded is '{}'.
        """
        with TempDb():
            repo = VideoRepository()
            v = self._seed(repo)
            repo.set_field_lock(v.path, "actresses", True)
            # re-upsert media-ish video without field_locks set (defaults {})
            v2 = Video(
                path=v.path,
                number=v.number,
                title="新刮削标题",
                original_title=v.original_title,
                actresses=["B"],
                maker=v.maker,
                cover_path=v.cover_path,
            )
            repo.upsert(v2)
            loaded = repo.get_by_path(v.path)
            assert loaded is not None
            # Empty '{}' maps should preserve previous via SQL CASE
            self.assertTrue(is_locked(loaded.field_locks, "actresses"))


if __name__ == "__main__":
    unittest.main()
