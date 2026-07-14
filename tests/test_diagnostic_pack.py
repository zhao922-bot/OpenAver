"""Diagnostic pack creation tests."""
from __future__ import annotations

import json
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.diagnostic_pack import (  # noqa: E402
    _redact,
    _redact_log_text,
    _redact_url,
    create_diagnostic_pack,
    list_diagnostic_packs,
)


class DiagnosticPackTests(unittest.TestCase):
    def test_redact_secrets(self) -> None:
        data = {
            "translate": {"openai": {"api_key": "sk-secret", "model": "x"}},
            "security": {"lan_token": "abc123"},
            "safe": "ok",
        }
        red = _redact(data)
        self.assertEqual(red["translate"]["openai"]["api_key"], "***REDACTED***")
        self.assertEqual(red["security"]["lan_token"], "***REDACTED***")
        self.assertEqual(red["safe"], "ok")
        self.assertEqual(red["translate"]["openai"]["model"], "x")

    def test_redact_m3u8_and_query(self) -> None:
        raw = (
            "download https://cdn.example.com/path/token/abc/video.m3u8?sig=xyz&exp=1 "
            "api_key=sk-leak"
        )
        cleaned = _redact_log_text(raw)
        self.assertNotIn("sig=xyz", cleaned)
        self.assertNotIn("sk-leak", cleaned)
        self.assertIn("cdn.example.com", cleaned)
        self.assertIn("REDACTED", cleaned)
        u = _redact_url("https://media.example/x.m3u8?token=abc")
        self.assertNotIn("token=abc", u)
        self.assertIn("media.example", u)

    def test_create_pack_zip(self) -> None:
        with mock.patch("core.diagnostic_pack.diagnose_sources", create=True):
            pack = create_diagnostic_pack(log_tail_lines=50)
        self.assertTrue(Path(pack["path"]).is_file())
        self.assertGreater(pack["size"], 0)
        with zipfile.ZipFile(pack["path"], "r") as zf:
            names = set(zf.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("version.json", names)
            self.assertIn("config_summary.json", names)
            self.assertIn("dependencies.json", names)
            manifest = json.loads(zf.read("manifest.json"))
            self.assertIn("created_at", manifest)

        items = list_diagnostic_packs(limit=5)
        self.assertTrue(any(i["name"] == pack["name"] for i in items))


if __name__ == "__main__":
    unittest.main()
