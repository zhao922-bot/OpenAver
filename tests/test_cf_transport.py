from __future__ import annotations

import sys
from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"
WINDOWS_DIR = APP_DIR / "windows"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(WINDOWS_DIR))

from cf_transport_impl import PyWebViewCfTransport, _same_document_url  # noqa: E402


class FakeWindow:
    def __init__(self) -> None:
        self.loads: list[str] = []
        self.show_count = 0
        self.hide_count = 0

    def show(self) -> None:
        self.show_count += 1

    def hide(self) -> None:
        self.hide_count += 1

    def load_url(self, url: str) -> None:
        self.loads.append(url)

def make_transport() -> PyWebViewCfTransport:
    transport = object.__new__(PyWebViewCfTransport)
    transport._win = FakeWindow()
    transport._dead = False
    transport._cf_url = None
    transport._cf_urls = {"javlibrary": "https://www.javlibrary.com/ja/"}
    transport._active_cache_key = "javlibrary"
    transport._last_navigation_target = ""
    transport._event_states = lambda: "[test]"
    transport._bridge_ready = lambda: False
    return transport


class CfTransportTests(unittest.TestCase):
    def test_same_document_url_ignores_trailing_slash_and_fragment(self) -> None:
        self.assertTrue(_same_document_url(
            "https://www.javlibrary.com/ja/#results",
            "https://www.javlibrary.com/ja/",
        ))
        self.assertFalse(_same_document_url(
            "https://www.javlibrary.com/ja/",
            "https://www.javlibrary.com/en/",
        ))

    def test_duplicate_javlibrary_navigation_is_debounced(self) -> None:
        transport = make_transport()

        transport.begin_solve("https://www.javlibrary.com/ja/", "javlibrary")
        transport.begin_solve("https://www.javlibrary.com/ja/", "javlibrary")

        self.assertEqual(transport._win.show_count, 2)
        self.assertEqual(transport._win.hide_count, 0)
        self.assertEqual(
            transport._win.loads,
            ["https://www.javlibrary.com/ja/"],
        )

    def test_javlibrary_navigation_remains_visible(self) -> None:
        transport = make_transport()

        transport.begin_solve("https://www.javlibrary.com/ja/", "javlibrary")

        self.assertEqual(transport._win.show_count, 1)
        self.assertEqual(transport._win.hide_count, 0)


if __name__ == "__main__":
    unittest.main()
