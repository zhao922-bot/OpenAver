"""JavDB scraper proxy wiring (no network)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.scrapers.javdb import JavDBScraper, CURL_CFFI_AVAILABLE  # noqa: E402
from core.scrapers.models import ScraperConfig  # noqa: E402


class JavDBProxyTests(unittest.TestCase):
    def test_config_accepts_proxy(self) -> None:
        s = JavDBScraper(ScraperConfig(proxy_url="http://127.0.0.1:7890"))
        self.assertEqual(s.config.proxy_url, "http://127.0.0.1:7890")

    @unittest.skipUnless(CURL_CFFI_AVAILABLE, "curl_cffi not available")
    def test_get_html_passes_proxies(self) -> None:
        s = JavDBScraper(ScraperConfig(proxy_url="http://127.0.0.1:7890"))
        with mock.patch("core.scrapers.javdb.curl_requests") as req:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = "<html><div class='movie-list'></div></html>"
            req.get.return_value = resp
            html = s._get_html("https://javdb.com/search?q=SONE-205")
            self.assertIsNotNone(html)
            kwargs = req.get.call_args.kwargs
            self.assertIn("proxies", kwargs)
            self.assertEqual(kwargs["proxies"]["https"], "http://127.0.0.1:7890")

    @unittest.skipUnless(CURL_CFFI_AVAILABLE, "curl_cffi not available")
    def test_get_html_no_proxy_when_empty(self) -> None:
        s = JavDBScraper(ScraperConfig(proxy_url=""))
        with mock.patch("core.scrapers.javdb.curl_requests") as req:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = "<html></html>"
            req.get.return_value = resp
            s._get_html("https://javdb.com/")
            kwargs = req.get.call_args.kwargs
            self.assertNotIn("proxies", kwargs)

    @unittest.skipUnless(CURL_CFFI_AVAILABLE, "curl_cffi not available")
    def test_geo_block_returns_none(self) -> None:
        s = JavDBScraper()
        with mock.patch("core.scrapers.javdb.curl_requests") as req:
            resp = mock.Mock()
            resp.status_code = 200
            resp.text = "Due to copyright restrictions, access to this site is prohibited in the country"
            req.get.return_value = resp
            self.assertIsNone(s._get_html("https://javdb.com/"))


if __name__ == "__main__":
    unittest.main()
