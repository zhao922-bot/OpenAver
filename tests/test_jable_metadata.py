from __future__ import annotations

import sys
from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from core.cf_transport import CfChallengeRequired  # noqa: E402
from core.chinese_converter import traditional_to_simplified  # noqa: E402
from core.jable_metadata import parse_jable_titles  # noqa: E402


TRADITIONAL_TITLE = (
    "\u6563\u767c\u6fc3\u6fc3\u9b45\u529b\u7684\u7f8e\u5973\uff0c"
    "\u6e34\u6c42\u64c1\u62b1\uff01\u5169\u4eba\u76e1\u60c5\u6253\u70ae "
    "\u4e09\u6f84\u5be7\u5be7"
)


class JableMetadataTests(unittest.TestCase):
    def test_converts_taiwan_traditional_to_simplified(self) -> None:
        converted = traditional_to_simplified(TRADITIONAL_TITLE)

        self.assertIn("\u6563\u53d1\u6d53\u6d53", converted)
        self.assertIn("\u4e24\u4eba", converted)
        self.assertIn("\u5c3d\u60c5", converted)
        self.assertTrue(converted.endswith("\u4e09\u6f84\u5b81\u5b81"))

    def test_parses_spaced_number_and_returns_simplified_title(self) -> None:
        html = (
            "<html><head><title>IPZZ 708 AV</title></head><body>"
            f"<div><h4>IPZZ 708 {TRADITIONAL_TITLE}</h4>"
            '<a href="//jable.tv/videos/ipzz-708/">watch</a></div>'
            "</body></html>"
        )

        result = parse_jable_titles(
            html,
            "IPZZ-708",
            "https://jable.tv/search/IPZZ-708/",
        )

        self.assertFalse(result["title_zh"].startswith("IPZZ"))
        self.assertIn("\u6563\u53d1", result["title_zh"])
        self.assertEqual(result["candidates"][0]["language"], "zh")
        self.assertEqual(
            result["candidates"][0]["url"],
            "https://jable.tv/videos/ipzz-708/",
        )

    def test_does_not_return_unrelated_fallback_heading(self) -> None:
        html = (
            "<html><head><title>Search results - Jable.TV</title></head>"
            "<body><h1>Search results</h1></body></html>"
        )

        result = parse_jable_titles(
            html,
            "IPZZ-708",
            "https://jable.tv/search/IPZZ-708/",
        )

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["title_zh"], "")

    def test_cloudflare_page_is_not_treated_as_metadata(self) -> None:
        html = "<html><head><title>Just a moment...</title></head></html>"

        with self.assertRaises(CfChallengeRequired):
            parse_jable_titles(html, "IPZZ-708", "https://jable.tv/")


if __name__ == "__main__":
    unittest.main()
