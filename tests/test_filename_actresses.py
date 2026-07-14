"""Filename actress dedupe: do not append CN primary when JP alias is in title."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from web.routers.showcase import (  # noqa: E402
    _build_video_basename,
    _display_actresses_for_filename,
    _name_already_in_text,
)


class FilenameActressTests(unittest.TestCase):
    def test_name_already_in_text(self) -> None:
        self.assertTrue(_name_already_in_text("三澄寧々", "标题 三澄寧々"))
        self.assertFalse(_name_already_in_text("三澄宁宁", "标题 三澄寧々"))

    def test_skip_when_alias_in_title(self) -> None:
        groups = [("三澄宁宁", {"三澄宁宁", "三澄寧々"})]
        video = SimpleNamespace(
            actresses=["三澄寧々"],
            original_title="溢れ出る色気 三澄寧々",
            title="",
        )
        names = _display_actresses_for_filename(video, groups)
        self.assertEqual(names, [])

    def test_append_when_not_in_title(self) -> None:
        groups = [("三澄宁宁", {"三澄宁宁", "三澄寧々"})]
        video = SimpleNamespace(
            actresses=["三澄寧々"],
            original_title="溢れ出る色気と艶",
            title="",
        )
        names = _display_actresses_for_filename(video, groups)
        self.assertEqual(names, ["三澄宁宁"])

    def test_basename_no_double_actress(self) -> None:
        video = SimpleNamespace(
            number="IPZZ-708",
            actresses=["三澄寧々"],
            original_title="溢れ出る色気と艶で雄を求める美女と快楽のままに貪り合う濃厚ベロキスと官能セックス 三澄寧々",
            title="",
            path="file:///D:/Videos/JAV/x/x.mp4",
        )
        groups = [("三澄宁宁", {"三澄宁宁", "三澄寧々"})]
        with mock.patch("web.routers.showcase._get_actress_alias_groups", return_value=groups):
            base = _build_video_basename(video)
        self.assertIn("IPZZ-708 - ", base)
        self.assertIn("三澄寧々", base)
        # Must not append simplified primary again
        self.assertNotIn("三澄宁宁", base)
        self.assertEqual(base.count("三澄"), 1)


if __name__ == "__main__":
    unittest.main()
