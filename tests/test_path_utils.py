"""Windows path normalization / case-insensitive comparison tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core import path_utils  # noqa: E402


class PathUtilsTests(unittest.TestCase):
    def test_paths_equal_drive_case(self) -> None:
        self.assertTrue(path_utils.paths_equal(r"D:\Videos\JAV", r"d:\videos\jav"))
        self.assertTrue(path_utils.paths_equal("file:///D:/Videos/JAV", "file:///d:/videos/jav"))

    def test_paths_equal_unicode(self) -> None:
        a = r"D:\Videos\中文\テスト"
        b = r"d:\videos\中文\テスト"
        self.assertTrue(path_utils.paths_equal(a, b))

    def test_is_path_under_dir_casefold(self) -> None:
        self.assertTrue(
            path_utils.is_path_under_dir(
                "file:///D:/Videos/JAV/a.mp4",
                "file:///d:/videos/jav",
            )
        )
        self.assertFalse(
            path_utils.is_path_under_dir(
                "file:///D:/Videos/JAV2/a.mp4",
                "file:///d:/videos/jav",
            )
        )

    def test_path_startswith_boundary(self) -> None:
        self.assertTrue(path_utils.path_startswith(r"D:\Videos\JAV\x", r"d:\videos\jav"))
        self.assertFalse(path_utils.path_startswith(r"D:\Videos\JAV2\x", r"d:\videos\jav"))

    def test_to_file_uri_uppercases_drive(self) -> None:
        uri = path_utils.to_file_uri(r"d:\videos\test.mp4")
        self.assertTrue(uri.startswith("file:///D:/") or uri.startswith("file:///d:/"))
        # On windows env, drive letter is uppercased by implementation
        if path_utils.CURRENT_ENV == "windows":
            self.assertTrue(uri.startswith("file:///D:/"))

    def test_normalize_for_compare_unc(self) -> None:
        a = path_utils.normalize_for_compare(r"\\Server\Share\Path")
        b = path_utils.normalize_for_compare(r"\\server\share\path")
        self.assertEqual(a, b)

    def test_paths_equal_identity(self) -> None:
        self.assertTrue(path_utils.paths_equal("same", "same"))
        self.assertFalse(path_utils.paths_equal("", "x"))
        self.assertFalse(path_utils.paths_equal("x", ""))


if __name__ == "__main__":
    unittest.main()
