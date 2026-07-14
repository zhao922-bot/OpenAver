"""Unit tests for field provenance, locks, and translation version helpers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.field_meta import (  # noqa: E402
    TRANSLATE_PROMPT_VERSION,
    apply_field_locks,
    build_translation_meta,
    is_locked,
    merge_sources,
    set_lock,
    set_source,
    should_skip_retranslate,
    source_text_hash,
)


class FieldMetaTests(unittest.TestCase):
    def test_lock_toggle(self) -> None:
        locks = set_lock({}, "title", True)
        self.assertTrue(is_locked(locks, "title"))
        locks = set_lock(locks, "title", False)
        self.assertFalse(is_locked(locks, "title"))

    def test_merge_sources(self) -> None:
        sources = merge_sources({"title": "dmm"}, {"actresses": "javbus", "title": "javdb"})
        self.assertEqual(sources["title"], "javdb")
        self.assertEqual(sources["actresses"], "javbus")

    def test_set_source(self) -> None:
        sources = set_source({}, "cover", "dmm")
        self.assertEqual(sources["cover"], "dmm")

    def test_apply_field_locks_preserves_values(self) -> None:
        existing = {"title": "manual-cn", "maker": "Prestige", "cover_path": "file:///a.jpg"}
        incoming = {"title": "scraped", "maker": "X", "cover_path": "file:///b.jpg"}
        locks = {"title": True, "cover": True}
        preserved = apply_field_locks(
            existing, incoming, locks, field_map={"cover": "cover_path"}
        )
        self.assertIn("title", preserved)
        self.assertIn("cover", preserved)
        self.assertEqual(incoming["title"], "manual-cn")
        self.assertEqual(incoming["cover_path"], "file:///a.jpg")
        self.assertEqual(incoming["maker"], "X")  # not locked

    def test_source_text_hash_stable(self) -> None:
        self.assertEqual(source_text_hash("  テスト  "), source_text_hash("テスト"))
        self.assertNotEqual(source_text_hash("a"), source_text_hash("b"))

    def test_build_translation_meta(self) -> None:
        meta = build_translation_meta(
            provider="openai",
            model="deepseek-v4-flash",
            source_text="溢れ出る色気",
            confirmed=False,
        )
        self.assertEqual(meta["provider"], "openai")
        self.assertEqual(meta["model"], "deepseek-v4-flash")
        self.assertEqual(meta["prompt_version"], TRANSLATE_PROMPT_VERSION)
        self.assertFalse(meta["confirmed"])
        self.assertTrue(meta["source_text_hash"])
        self.assertTrue(meta["translated_at"].endswith("Z"))

    def test_skip_retranslate_when_confirmed_and_same_source(self) -> None:
        meta = build_translation_meta(
            provider="openai", model="x", source_text="日文标题", confirmed=True
        )
        skip, reason = should_skip_retranslate(meta, source_text="日文标题")
        self.assertTrue(skip)
        self.assertEqual(reason, "translation_confirmed")

    def test_no_skip_when_force(self) -> None:
        meta = build_translation_meta(
            provider="openai", model="x", source_text="日文标题", confirmed=True
        )
        skip, _ = should_skip_retranslate(meta, source_text="日文标题", force=True)
        self.assertFalse(skip)

    def test_no_skip_when_source_changed(self) -> None:
        meta = build_translation_meta(
            provider="openai", model="x", source_text="旧标题", confirmed=True
        )
        skip, _ = should_skip_retranslate(meta, source_text="新标题")
        self.assertFalse(skip)

    def test_no_skip_when_prompt_version_changed(self) -> None:
        meta = build_translation_meta(
            provider="openai", model="x", source_text="タイトル", confirmed=True
        )
        meta["prompt_version"] = "v0-old"
        skip, _ = should_skip_retranslate(meta, source_text="タイトル")
        self.assertFalse(skip)

    def test_no_skip_when_unconfirmed(self) -> None:
        meta = build_translation_meta(
            provider="openai", model="x", source_text="タイトル", confirmed=False
        )
        skip, _ = should_skip_retranslate(meta, source_text="タイトル")
        self.assertFalse(skip)


if __name__ == "__main__":
    unittest.main()
