"""Incremental scan diff unit tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.scan_diff import (  # noqa: E402
    build_db_index_rows,
    build_db_uri_by_key,
    diff_files,
    file_changed,
    path_key,
)


class ScanDiffTests(unittest.TestCase):
    def test_path_key_casefold(self) -> None:
        self.assertEqual(
            path_key("file:///D:/Videos/A.mp4"),
            path_key("file:///d:/videos/a.mp4"),
        )

    def test_file_changed_mtime(self) -> None:
        self.assertTrue(file_changed((1.0, 0, 100), 2.0, 0, 100))
        self.assertFalse(file_changed((1.0, 0, 100), 1.0, 0, 100))

    def test_file_changed_size(self) -> None:
        self.assertTrue(file_changed((1.0, 0, 100), 1.0, 0, 200))
        # legacy size=0 does not force change
        self.assertFalse(file_changed((1.0, 0, 0), 1.0, 0, 999))

    def test_file_changed_nfo(self) -> None:
        self.assertTrue(file_changed((1.0, 5.0, 100), 1.0, 6.0, 100))

    def test_diff_new_changed_unchanged(self) -> None:
        rows = [
            ("file:///D:/Videos/JAV/A.mp4", 10.0, 1.0, 1000),
            ("file:///D:/Videos/JAV/B.mp4", 20.0, 2.0, 2000),
        ]
        db_index = build_db_index_rows(rows)
        db_uri = build_db_uri_by_key(rows)

        files = [
            # unchanged (case different path → same key)
            {"path": r"D:\Videos\JAV\A.mp4", "mtime": 10.0, "nfo_mtime": 1.0, "size": 1000},
            # changed size
            {"path": r"D:\Videos\JAV\B.mp4", "mtime": 20.0, "nfo_mtime": 2.0, "size": 9999},
            # new
            {"path": r"D:\Videos\JAV\C.mp4", "mtime": 30.0, "nfo_mtime": 0, "size": 3000},
        ]
        diff = diff_files(files, db_index, db_uri, path_mappings={}, force_full=False)
        self.assertEqual(diff.unchanged, 1)
        self.assertEqual(diff.new_count, 1)
        self.assertEqual(diff.changed_count, 1)
        self.assertEqual(len(diff.needs_scan), 2)

    def test_force_full_scans_all(self) -> None:
        rows = [("file:///D:/Videos/JAV/A.mp4", 10.0, 0, 100)]
        db_index = build_db_index_rows(rows)
        db_uri = build_db_uri_by_key(rows)
        files = [{"path": r"D:\Videos\JAV\A.mp4", "mtime": 10.0, "nfo_mtime": 0, "size": 100}]
        diff = diff_files(files, db_index, db_uri, force_full=True)
        self.assertEqual(len(diff.needs_scan), 1)
        self.assertEqual(diff.unchanged, 0)

    def test_deleted_candidates(self) -> None:
        rows = [
            ("file:///D:/Videos/JAV/A.mp4", 1, 0, 10),
            ("file:///D:/Videos/JAV/Gone.mp4", 1, 0, 10),
        ]
        db_index = build_db_index_rows(rows)
        db_uri = build_db_uri_by_key(rows)
        files = [{"path": r"D:\Videos\JAV\A.mp4", "mtime": 1, "nfo_mtime": 0, "size": 10}]
        diff = diff_files(files, db_index, db_uri)
        self.assertTrue(any("Gone" in u for u in diff.deleted_candidates))


if __name__ == "__main__":
    unittest.main()
