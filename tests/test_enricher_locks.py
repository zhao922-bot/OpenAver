"""Enricher respects field_locks when merging scraper data."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.enricher import _merge_meta  # noqa: E402


class EnricherLocksTests(unittest.TestCase):
    def test_merge_skips_locked_fields(self) -> None:
        base = {
            "title": "人工标题",
            "original_title": "日文",
            "actresses": [],
            "maker": "KeepMaker",
            "director": "",
            "series": "",
            "label": "",
            "tags": [],
            "release_date": "",
            "cover_url": "",
        }
        supplement = {
            "title": "刮削标题",
            "original_title": "新しい",
            "actresses": ["A"],
            "maker": "ScrapedMaker",
            "director": "D",
            "series": "S",
            "label": "L",
            "tags": ["t"],
            "release_date": "2024-01-01",
            "cover_url": "https://example.com/c.jpg",
        }
        merged, filled = _merge_meta(
            base, supplement, locks={"title": True, "maker": True, "cover": True}
        )
        self.assertEqual(merged["title"], "人工标题")
        self.assertEqual(merged["maker"], "KeepMaker")
        self.assertEqual(merged["cover_url"], "")  # cover locked
        self.assertNotIn("title", filled)
        self.assertNotIn("maker", filled)
        self.assertNotIn("cover", filled)
        self.assertIn("actresses", filled)
        self.assertEqual(merged["actresses"], ["A"])

    def test_merge_fills_unlocked(self) -> None:
        base = {
            "title": "",
            "actresses": [],
            "maker": "",
            "director": "",
            "series": "",
            "label": "",
            "tags": [],
            "release_date": "",
            "cover_url": "",
        }
        supplement = {
            "title": "T",
            "actresses": ["A"],
            "maker": "M",
            "director": "D",
            "series": "S",
            "label": "L",
            "tags": ["t"],
            "release_date": "2020-01-01",
            "cover_url": "https://x/c.jpg",
        }
        merged, filled = _merge_meta(base, supplement, locks={})
        self.assertEqual(merged["title"], "T")
        self.assertIn("title", filled)
        self.assertIn("cover", filled)
        self.assertEqual(merged["cover_url"], "https://x/c.jpg")


if __name__ == "__main__":
    unittest.main()
