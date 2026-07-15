"""MediaDownloadManager: ETA preservation + sidecar import warnings."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.media_downloader import MediaDownloadManager  # noqa: E402


class PublicEtaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = MediaDownloadManager.__new__(MediaDownloadManager)

    def test_preserves_real_eta_not_video_duration(self) -> None:
        """duration_seconds is media length; must not overwrite real eta_seconds."""
        task = {
            "status": "running",
            "duration_seconds": 7200,
            "progress": 50,
            "eta_seconds": 120,
            "payload": {"media_url": "https://cdn.example/v.m3u8?sig=abc"},
        }
        public = self.mgr._public(task)
        self.assertEqual(public["eta_seconds"], 120)
        # Must not be duration * (1 - progress/100) = 3600
        self.assertNotEqual(public["eta_seconds"], 3600)

    def test_completed_eta_zero_preserved(self) -> None:
        task = {
            "status": "completed",
            "duration_seconds": 7200,
            "progress": 100,
            "eta_seconds": 0,
            "payload": {"media_url": "https://cdn.example/v.mp4"},
        }
        public = self.mgr._public(task)
        self.assertEqual(public["eta_seconds"], 0)

    def test_missing_eta_defaults_none(self) -> None:
        task = {
            "status": "queued",
            "duration_seconds": None,
            "progress": 0,
            "payload": {"media_url": "https://cdn.example/v.mp4"},
        }
        public = self.mgr._public(task)
        self.assertIsNone(public.get("eta_seconds"))

    def test_no_duration_progress_does_not_invent_eta(self) -> None:
        task = {
            "status": "running",
            "duration_seconds": 3600,
            "progress": 25,
            # no eta_seconds key — engine has not reported yet
            "payload": {"media_url": "https://cdn.example/v.mp4"},
        }
        public = self.mgr._public(task)
        # Must not invent duration-based ETA (2700)
        self.assertIsNone(public.get("eta_seconds"))


class WriteAssetsAndImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = MediaDownloadManager.__new__(MediaDownloadManager)
        self.mgr.allow_private_urls = False

    def _run_with_mocks(
        self,
        *,
        nfo_ok: bool,
        cover_ok: bool,
        cover_url: str = "https://cdn.example/cover.jpg?sig=secret",
    ) -> dict:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "SONE-205.mp4"
            out.write_bytes(b"video")
            payload = {
                "number": "SONE-205",
                "title": "t",
                "chinese_title": "中",
                "actors": [],
                "tags": [],
                "date": "2024-01-01",
                "maker": "M",
                "source_page_url": "https://example.com/p",
                "cover": cover_url,
                "director": "",
                "series": "",
                "label": "",
            }
            scan_info = mock.Mock()
            with mock.patch(
                "core.media_downloader.generate_nfo", return_value=nfo_ok
            ) as gen_nfo:
                with mock.patch(
                    "core.media_downloader.download_image", return_value=cover_ok
                ) as dl:
                    with mock.patch(
                        "core.media_downloader._validate_network_target"
                    ):
                        with mock.patch(
                            "core.media_downloader.load_config",
                            return_value={"scraper": {"external_manager": "off"}},
                        ):
                            with mock.patch(
                                "core.media_downloader.VideoScanner"
                            ) as Scanner:
                                Scanner.return_value.scan_file.return_value = scan_info
                                with mock.patch(
                                    "core.media_downloader.VideoRepository"
                                ) as Repo:
                                    with mock.patch(
                                        "core.media_downloader.Video"
                                    ) as VideoCls:
                                        VideoCls.from_video_info.return_value = mock.Mock()
                                        result = self.mgr._write_assets_and_import(
                                            payload, out, 120.0
                                        )
                                        # Scan/import must still run on sidecar failure
                                        Scanner.return_value.scan_file.assert_called_once()
                                        Repo.return_value.upsert.assert_called_once()
                                        gen_nfo.assert_called_once()
                                        if cover_url:
                                            dl.assert_called_once()
                                        return result

    def test_nfo_false_sets_import_error_still_scans(self) -> None:
        result = self._run_with_mocks(nfo_ok=False, cover_ok=True)
        self.assertIn("NFO", result["import_error"])
        self.assertTrue(result["warnings"])
        # No signed URL leakage
        self.assertNotIn("sig=secret", result["import_error"])

    def test_cover_false_sets_import_error_still_scans(self) -> None:
        result = self._run_with_mocks(nfo_ok=True, cover_ok=False)
        self.assertIn("封面", result["import_error"])
        self.assertNotIn("sig=secret", result["import_error"])
        self.assertNotIn("https://", result["import_error"])

    def test_both_ok_empty_import_error(self) -> None:
        result = self._run_with_mocks(nfo_ok=True, cover_ok=True)
        self.assertEqual(result["import_error"], "")
        self.assertEqual(result["warnings"], [])

    def test_completed_status_message_pattern(self) -> None:
        """Sidecar failure → completed with non-empty import_error message."""
        result = self._run_with_mocks(nfo_ok=False, cover_ok=False)
        self.assertTrue(result["import_error"])
        # Task layer maps this to "已下载；入库需检查"
        msg = "已完成" if not result["import_error"] else "已下载；入库需检查"
        self.assertEqual(msg, "已下载；入库需检查")


if __name__ == "__main__":
    unittest.main()
