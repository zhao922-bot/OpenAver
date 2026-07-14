"""Image headers + proxy disk cache tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.image_headers import headers_for_image_url, referer_for_image_url  # noqa: E402
from core import image_proxy_cache  # noqa: E402


class ImageHeadersTests(unittest.TestCase):
    def test_dmm_referer(self) -> None:
        url = "https://pics.dmm.co.jp/digital/video/ssis00221/ssis00221pl.jpg"
        self.assertIn("dmm.co.jp", referer_for_image_url(url))
        h = headers_for_image_url(url)
        self.assertIn("Mozilla", h["User-Agent"])
        self.assertTrue(h["Referer"].startswith("https://www.dmm.co.jp"))

    def test_javbus_referer(self) -> None:
        url = "https://www.javbus.com/pics/cover/abc.jpg"
        self.assertIn("javbus", referer_for_image_url(url))

    def test_javdb_cdn_referer(self) -> None:
        url = "https://c0.jdbstatic.com/covers/xx/xx.jpg"
        self.assertIn("javdb", referer_for_image_url(url))

    def test_extra_referer_override(self) -> None:
        h = headers_for_image_url("https://example.com/a.jpg", extra_referer="https://custom/")
        self.assertEqual(h["Referer"], "https://custom/")


class ImageProxyCacheTests(unittest.TestCase):
    def test_put_get_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proxy-img"
            with mock.patch.object(image_proxy_cache, "_cache_root", return_value=root):
                url = "https://pics.dmm.co.jp/digital/video/x/xpl.jpg"
                body = b"\xff\xd8\xff" + b"x" * 200
                image_proxy_cache.put_cached(url, body, "image/jpeg")
                hit = image_proxy_cache.get_cached(url)
                self.assertIsNotNone(hit)
                assert hit is not None
                self.assertEqual(hit[0], body)
                self.assertEqual(hit[1], "image/jpeg")

    def test_miss_unknown_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proxy-img"
            with mock.patch.object(image_proxy_cache, "_cache_root", return_value=root):
                self.assertIsNone(image_proxy_cache.get_cached("https://no.such/image.jpg"))

    def test_rejects_tiny_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proxy-img"
            with mock.patch.object(image_proxy_cache, "_cache_root", return_value=root):
                url = "https://example.com/tiny.jpg"
                image_proxy_cache.put_cached(url, b"x", "image/jpeg")
                self.assertIsNone(image_proxy_cache.get_cached(url))


if __name__ == "__main__":
    unittest.main()
