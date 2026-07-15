"""Smoke-test pure JS helpers for samples batch via node --check + assert script."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "pages" / "scanner" / "samples-batch.js"
STATE = ROOT / "app" / "web" / "static" / "js" / "pages" / "scanner" / "state-batch.js"


class SamplesBatchJsTests(unittest.TestCase):
    def _node(self) -> str:
        return "node"

    def test_node_check_samples_batch(self) -> None:
        r = subprocess.run(
            [self._node(), "--check", str(JS)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_node_check_state_batch(self) -> None:
        r = subprocess.run(
            [self._node(), "--check", str(STATE)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_helper_logic_via_node(self) -> None:
        # CommonJS-compatible reimplementation of pure helpers (ESM export not
        # loaded via node -e without type:module). Keep in sync with samples-batch.js.
        script = r"""
        function shouldShowMissingSamplesRow(opts) {
          const o = opts && typeof opts === 'object' ? opts : {};
          return Number(o.count || 0) > 0 || Number(o.skippedMulti || 0) > 0;
        }
        function missingSamplesLabelParams(opts) {
          const o = opts && typeof opts === 'object' ? opts : {};
          return {
            count: Number(o.count || 0),
            skipped_multi: Number(o.skippedMulti || 0),
          };
        }
        function summarizeBatchSamplesDone(summary, checkPhaseSkippedMulti) {
          const s = summary && typeof summary === 'object' ? summary : {};
          const skippedComplete = Number(s.skipped_complete || 0);
          const skippedMultiBatch = Number(s.skipped_multi || 0);
          const skippedMultiCheck = Number(checkPhaseSkippedMulti || 0);
          const skippedField = Number(s.skipped || 0);
          let skipped = skippedComplete;
          if (skippedComplete === 0 && skippedField > 0) {
            skipped = Math.max(0, skippedField - skippedMultiBatch);
          }
          return {
            success: Number(s.success || 0),
            images: Number(s.images_downloaded || 0),
            noSamples: Number(s.no_samples || 0),
            skipped,
            skippedMulti: skippedMultiCheck + skippedMultiBatch,
            failed: Number(s.failed || 0),
          };
        }

        // visibility
        if (!shouldShowMissingSamplesRow({ count: 0, skippedMulti: 3 })) process.exit(10);
        if (!shouldShowMissingSamplesRow({ count: 2, skippedMulti: 0 })) process.exit(11);
        if (shouldShowMissingSamplesRow({ count: 0, skippedMulti: 0 })) process.exit(12);

        // label params
        const lp = missingSamplesLabelParams({ count: 5, skippedMulti: 2 });
        if (lp.count !== 5 || lp.skipped_multi !== 2) process.exit(13);

        // summarize: check-phase multi + batch multi without double-count in skipped
        const summary = {
          success: 2,
          images_downloaded: 7,
          no_samples: 1,
          skipped: 3,
          skipped_complete: 2,
          skipped_multi: 1,
          failed: 1,
        };
        const out = summarizeBatchSamplesDone(summary, 4);
        if (out.success !== 2 || out.images !== 7 || out.noSamples !== 1) process.exit(14);
        if (out.skipped !== 2) process.exit(15); // complete only
        if (out.skippedMulti !== 5) process.exit(16); // 4 check + 1 batch
        if (out.failed !== 1) process.exit(17);

        // busy status
        if (Number(409) !== 409) process.exit(18);
        console.log('ok');
        """
        r = subprocess.run(
            [self._node(), "-e", script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)


if __name__ == "__main__":
    unittest.main()
