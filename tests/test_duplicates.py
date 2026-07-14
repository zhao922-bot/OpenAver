"""Duplicate detection unit tests."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402
from core.database import Video, VideoRepository  # noqa: E402
from core.duplicates import content_fingerprint, find_duplicates  # noqa: E402


class DuplicatesTests(unittest.TestCase):
    def test_fingerprint_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(b"hello-world" * 1000)
            fp1 = content_fingerprint(str(p))
            fp2 = content_fingerprint(str(p))
            self.assertIsNotNone(fp1)
            self.assertEqual(fp1, fp2)

    def test_fingerprint_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.bin"
            b = Path(tmp) / "b.bin"
            a.write_bytes(b"AAA" * 500)
            b.write_bytes(b"BBB" * 500)
            self.assertNotEqual(content_fingerprint(str(a)), content_fingerprint(str(b)))

    def test_find_by_number(self) -> None:
        with TempDb(), tempfile.TemporaryDirectory() as tmp:
            repo = VideoRepository()
            # two same number different paths
            for i, name in enumerate(("one.mp4", "two.mp4")):
                fs = Path(tmp) / name
                fs.write_bytes(b"x" * (100 + i))
                uri = "file:///" + str(fs).replace("\\", "/")
                if os.name == "nt" and not uri.startswith("file:///"):
                    pass
                from core.path_utils import to_file_uri
                uri = to_file_uri(str(fs))
                repo.upsert(Video(
                    path=uri,
                    number="SONE-001",
                    title=f"t{i}",
                    size_bytes=fs.stat().st_size,
                ))
            result = find_duplicates(compute_hash=False)
            self.assertGreaterEqual(result["summary"]["number_duplicate_groups"], 1)
            self.assertTrue(any(g["key"] == "SONE-001" for g in result["by_number"]))


if __name__ == "__main__":
    unittest.main()
