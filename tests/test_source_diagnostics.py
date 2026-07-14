"""Source diagnostics aggregation (offline, no live probe)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.source_diagnostics import diagnose_dependencies, diagnose_sources  # noqa: E402


class SourceDiagnosticsTests(unittest.TestCase):
    def test_dependencies_shape(self) -> None:
        deps = diagnose_dependencies({"search": {"proxy_url": ""}})
        for key in ("curl_cffi", "ffmpeg", "yt_dlp", "cf_transport", "proxy"):
            self.assertIn(key, deps)
            self.assertIn("ok", deps[key])
            self.assertIn("status", deps[key])

    def test_proxy_not_configured(self) -> None:
        deps = diagnose_dependencies({"search": {"proxy_url": ""}})
        self.assertFalse(deps["proxy"]["ok"])
        self.assertEqual(deps["proxy"]["status"], "not_configured")

    def test_proxy_configured(self) -> None:
        deps = diagnose_dependencies({"search": {"proxy_url": "http://127.0.0.1:7890"}})
        self.assertTrue(deps["proxy"]["ok"])

    def test_diagnose_sources_without_probe(self) -> None:
        fake_config = {
            "search": {"proxy_url": ""},
            "sources": [
                {
                    "id": "javdb", "type": "builtin", "display_name_key": "JavDB",
                    "enabled": True, "order": 0, "config": {}, "is_beta": False,
                    "manual_only": False, "requires_proxy": False, "is_censored": True,
                },
                {
                    "id": "dmm", "type": "builtin", "display_name_key": "DMM",
                    "enabled": True, "order": 1, "config": {}, "is_beta": False,
                    "manual_only": False, "requires_proxy": True, "is_censored": True,
                },
            ],
        }
        with mock.patch("core.source_diagnostics.load_config", return_value=fake_config):
            with mock.patch("core.source_diagnostics.metatube_state") as mt:
                mt.availability_map.return_value = {}
                mt.is_connected = False
                result = diagnose_sources(probe=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["summary"]["total"], 2)
        by_id = {s["id"]: s for s in result["sources"]}
        self.assertIn("javdb", by_id)
        self.assertIn("dmm", by_id)
        # DMM without proxy → needs_proxy and NOT effective auto-pool
        self.assertEqual(by_id["dmm"]["runtime_status"], "needs_proxy")
        self.assertFalse(by_id["dmm"].get("in_auto_pool_effective"))
        self.assertFalse(by_id["dmm"].get("in_auto_pool"))
        # May still be "configured" in enabled sources list
        self.assertTrue(
            by_id["dmm"].get("in_auto_pool_configured") or by_id["dmm"]["enabled"]
        )
        # JavDB soft-hint about proxy when not configured
        self.assertTrue(
            any("代理" in i for i in by_id["javdb"].get("issues", []))
            or by_id["javdb"]["runtime_status"] in ("ok", "missing_dependency")
        )


if __name__ == "__main__":
    unittest.main()
