"""Rename journal append / list / rollback mark tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402
from core import rename_journal  # noqa: E402


class RenameJournalTests(unittest.TestCase):
    def test_append_list_mark(self) -> None:
        with TempDb():
            ev = rename_journal.append_event({
                "kind": "batch_rename",
                "entries": [{"old_path": "a", "new_path": "b"}],
                "renamed": 1,
                "failed": 0,
                "skipped": 0,
            })
            self.assertIn("id", ev)
            self.assertIn("created_at", ev)

            items = rename_journal.list_events(limit=10)
            self.assertGreaterEqual(len(items), 1)
            self.assertEqual(items[0]["id"], ev["id"])

            found = rename_journal.get_event(ev["id"])
            self.assertIsNotNone(found)

            ok = rename_journal.mark_rolled_back(ev["id"])
            self.assertTrue(ok)
            again = rename_journal.get_event(ev["id"])
            assert again is not None
            self.assertTrue(again.get("rolled_back"))

    def test_missing_event(self) -> None:
        with TempDb():
            self.assertIsNone(rename_journal.get_event("does-not-exist"))
            self.assertFalse(rename_journal.mark_rolled_back("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
