"""Actress alias review + merge tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402
from core.actress_alias_review import _normalize_name, review_actress_aliases  # noqa: E402
from core.database import AliasRepository, Video, VideoRepository  # noqa: E402


class ActressAliasReviewTests(unittest.TestCase):
    def test_normalize_name(self) -> None:
        self.assertEqual(_normalize_name("三 上 悠 亜"), _normalize_name("三上悠亜"))
        self.assertEqual(_normalize_name("Yua・Mikami"), _normalize_name("yua mikami"))

    def test_unrecognized_and_merge(self) -> None:
        with TempDb():
            alias = AliasRepository()
            alias.add("三上悠亜", ["Yua Mikami"], source="manual")
            alias.add("三上悠亚", [], source="manual")  # simplified Chinese split group

            repo = VideoRepository()
            repo.upsert(Video(
                path="file:///D:/t/a.mp4",
                number="TEST-1",
                title="t",
                actresses=["三上悠亜", "未知新人A"],
            ))
            repo.upsert(Video(
                path="file:///D:/t/b.mp4",
                number="TEST-2",
                title="t",
                actresses=["未知新人A"],
            ))

            review = review_actress_aliases(limit=50)
            self.assertGreaterEqual(review["summary"]["unrecognized"], 1)
            self.assertTrue(any(u["name"] == "未知新人A" for u in review["unrecognized"]))

            # merge simplified into traditional
            merged = alias.merge_groups("三上悠亜", "三上悠亚")
            self.assertEqual(merged.primary_name, "三上悠亜")
            self.assertIn("三上悠亚", merged.aliases)
            self.assertIsNone(alias.get_by_primary("三上悠亚"))

    def test_attach_alias(self) -> None:
        with TempDb():
            alias = AliasRepository()
            alias.add("橋本ありな", [], source="manual")
            ok, err = alias.add_alias("橋本ありな", "桥本有菜")
            self.assertTrue(ok, err)
            rec = alias.get_by_primary("橋本ありな")
            self.assertIn("桥本有菜", rec.aliases)


if __name__ == "__main__":
    unittest.main()
