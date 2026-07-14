"""Download error classification + friendly Chinese messages."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.media_downloader import (  # noqa: E402
    ERROR_MESSAGES_ZH,
    _error_code,
    _friendly_error,
    _media_request_headers,
)


class DownloadErrorTests(unittest.TestCase):
    def test_http_403(self) -> None:
        code = _error_code("ERROR: Unable to download webpage: HTTP Error 403: Forbidden")
        self.assertEqual(code, "http_403")

    def test_link_expired_chinese(self) -> None:
        code = _error_code("链接已过期或被拒绝访问（HTTP 403）")
        self.assertIn(code, {"http_403", "link_expired"})

    def test_ffmpeg_missing(self) -> None:
        self.assertEqual(_error_code("FFmpeg is not installed or is not available on PATH"), "ffmpeg_missing")

    def test_engine_missing(self) -> None:
        self.assertEqual(
            _error_code("The multi-thread download engine is not installed"),
            "engine_missing",
        )

    def test_disk_full(self) -> None:
        self.assertEqual(_error_code("OSError: [Errno 28] No space left on device"), "disk_full")

    def test_timeout(self) -> None:
        self.assertEqual(_error_code("Read timed out"), "timeout")

    def test_target_exists(self) -> None:
        self.assertEqual(
            _error_code("Target folder already contains files: SONE-205"),
            "target_exists",
        )

    def test_friendly_prefers_chinese_validation(self) -> None:
        msg = "这是网页地址而不是媒体直链，请粘贴 m3u8 或视频文件 URL"
        code, friendly = _friendly_error(msg)
        self.assertEqual(code, "not_media")
        self.assertIn("网页", friendly)

    def test_friendly_maps_ffmpeg_dump(self) -> None:
        raw = "ffmpeg version ... Error opening input files: Server returned 403 Forbidden"
        code, friendly = _friendly_error(raw)
        self.assertEqual(code, "http_403")
        self.assertEqual(friendly, ERROR_MESSAGES_ZH["http_403"])

    def test_media_headers_include_ua_and_optional_referer(self) -> None:
        h = _media_request_headers(referer="https://example.com/page")
        self.assertIn("Mozilla", h["User-Agent"])
        self.assertEqual(h["Referer"], "https://example.com/page")
        h2 = _media_request_headers()
        self.assertNotIn("Referer", h2)


if __name__ == "__main__":
    unittest.main()
