"""Rename journal + real rename failure-path tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402
from core import rename_journal  # noqa: E402
from core.database import Video, VideoRepository  # noqa: E402
from core.path_utils import to_file_uri  # noqa: E402


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

    def test_update_pending_progress_incremental(self) -> None:
        """Mid-batch crash recovery: entries accumulate before finalize."""
        with TempDb():
            pending = rename_journal.append_event({
                "kind": "batch_rename",
                "status": "pending",
                "entries": [],
                "renamed": 0,
                "failed": 0,
                "skipped": 0,
            })
            eid = pending["id"]

            rename_journal.update_pending_progress(eid, {
                "entries": [{"old_path": "a", "new_path": "a2", "status": "completed"}],
                "renamed": 1,
                "failed": 0,
                "skipped": 0,
                "processed": 1,
            })
            mid = rename_journal.get_event(eid)
            assert mid is not None
            self.assertEqual(mid["status"], "pending")
            self.assertEqual(len(mid["entries"]), 1)
            self.assertEqual(mid["renamed"], 1)
            self.assertIn("updated_at", mid)

            rename_journal.update_pending_progress(eid, {
                "entries": [
                    {"old_path": "a", "new_path": "a2", "status": "completed"},
                    {"old_path": "b", "new_path": "b2", "status": "completed"},
                ],
                "renamed": 2,
                "failed": 0,
                "skipped": 0,
                "processed": 2,
            })
            mid2 = rename_journal.get_event(eid)
            assert mid2 is not None
            self.assertEqual(len(mid2["entries"]), 2)
            self.assertEqual(mid2["renamed"], 2)

            rename_journal.finalize_event(eid, {
                "kind": "batch_rename",
                "status": "completed",
                "entries": mid2["entries"],
                "renamed": 2,
                "failed": 0,
                "skipped": 0,
            })
            done = rename_journal.get_event(eid)
            assert done is not None
            self.assertEqual(done["status"], "completed")
            self.assertEqual(len(done["entries"]), 2)


class RenameAssetsFailurePathTests(unittest.TestCase):
    """Real filesystem rename + DB + journal failure paths."""

    def _make_video_tree(self, root: Path, number: str = "TEST-100"):
        folder = root / number
        folder.mkdir(parents=True)
        samples_dir = folder / "samples"
        samples_dir.mkdir()
        video = folder / f"{number}.mp4"
        cover = folder / f"{number}.jpg"
        nfo = folder / f"{number}.nfo"
        sample1 = samples_dir / "1.jpg"
        sample2 = samples_dir / "2.jpg"
        video.write_bytes(b"fake-mp4")
        cover.write_bytes(b"fake-jpg")
        nfo.write_text(
            f'<?xml version="1.0"?>\n<movie><title>{number}</title></movie>',
            encoding="utf-8",
        )
        sample1.write_bytes(b"s1")
        sample2.write_bytes(b"s2")
        return folder, video, cover, nfo, sample1, sample2

    def _seed_db(self, video_path: Path, cover: Path, samples: list[Path], number: str) -> Video:
        v = Video(
            path=to_file_uri(str(video_path)),
            number=number,
            title="テストタイトル",
            original_title="テストタイトル",
            actresses=["女優A"],
            cover_path=to_file_uri(str(cover)),
            sample_images=[to_file_uri(str(s)) for s in samples],
        )
        VideoRepository().upsert(v)
        return VideoRepository().get_by_path(v.path)  # type: ignore[return-value]

    def test_pre_journal_failure_aborts_without_rename(self) -> None:
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root)
                db_video = self._seed_db(video, cover, [s1, s2], "TEST-100")
                old_stem = video.stem

                from web.routers.showcase import _rename_video_assets

                with mock.patch(
                    "core.rename_journal.append_event",
                    side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        _rename_video_assets(
                            db_video,
                            {},
                            rename_folder=True,
                            dry_run=False,
                            journal=True,
                        )
                self.assertIn("journal", str(ctx.exception).lower())
                # File must still be at original location
                self.assertTrue(video.exists(), "video should not be renamed when journal fails")
                self.assertEqual(video.stem, old_stem)
                still = VideoRepository().get_by_path(db_video.path)
                assert still is not None
                self.assertEqual(still.path, db_video.path)

    def test_thumbnail_invalidate_failure_keeps_db_and_files(self) -> None:
        """DB update success + thumb fail must NOT roll files back or leave DB/FS mismatch."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "TEST-200")
                db_video = self._seed_db(video, cover, [s1, s2], "TEST-200")
                old_uri = db_video.path

                from web.routers.showcase import _rename_video_assets
                import web.routers.showcase as showcase_mod

                with mock.patch.object(
                    showcase_mod.thumbnail_cache,
                    "invalidate",
                    side_effect=RuntimeError("thumb boom"),
                ):
                    result = _rename_video_assets(
                        db_video,
                        {},
                        rename_folder=True,
                        dry_run=False,
                        journal=True,
                    )

                self.assertTrue(result.get("renamed"))
                new_uri = result["new_uri"]
                self.assertNotEqual(new_uri, old_uri)
                # Old path gone, new path exists
                self.assertFalse(Path(result["old_path"]).exists())
                self.assertTrue(Path(result["new_path"]).exists())
                # DB points at new path
                self.assertIsNone(VideoRepository().get_by_path(old_uri))
                loaded = VideoRepository().get_by_path(new_uri)
                assert loaded is not None
                self.assertEqual(loaded.path, new_uri)
                # sample_images should have been rewritten to new folder
                self.assertTrue(loaded.sample_images)
                for uri in loaded.sample_images:
                    self.assertIn("TEST-200", uri)  # basename still in path via new folder name
                    # folder renamed to new_base which contains number
                    self.assertNotIn("/TEST-200/", uri.replace("\\", "/"))

    def test_rename_updates_sample_images_and_rollback_restores(self) -> None:
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "TEST-300")
                db_video = self._seed_db(video, cover, [s1, s2], "TEST-300")
                old_uri = db_video.path
                old_samples = list(db_video.sample_images)

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback
                from web.routers.operations import RenameRollbackRequest

                result = _rename_video_assets(
                    db_video,
                    {},
                    rename_folder=True,
                    dry_run=False,
                    journal=True,
                )
                self.assertTrue(result.get("renamed"))
                new_uri = result["new_uri"]
                loaded = VideoRepository().get_by_path(new_uri)
                assert loaded is not None
                self.assertNotEqual(loaded.sample_images, old_samples)
                # New sample URIs should resolve to existing files
                for uri in loaded.sample_images:
                    from core.path_utils import uri_to_fs_path
                    self.assertTrue(
                        Path(uri_to_fs_path(uri)).exists(),
                        f"sample missing after rename: {uri}",
                    )

                # Build a batch-style journal entry (as batch apply would) and roll back
                entry = {
                    "old_uri": result["old_uri"],
                    "new_uri": result["new_uri"],
                    "old_path": result["old_path"],
                    "new_path": result["new_path"],
                    "folder_renamed": result["folder_renamed"],
                    "file_moves": result["file_moves"],
                    "number": "TEST-300",
                    "nfo_before": result.get("nfo_before_text"),
                    "status": "completed",
                }
                # Prefer the journal id from single rename if present
                jid = result.get("journal_id")
                if jid:
                    # finalize already done inside rename; mark not rolled_back
                    ev = rename_journal.get_event(jid)
                    assert ev is not None
                else:
                    ev = rename_journal.append_event({
                        "kind": "batch_rename",
                        "status": "completed",
                        "entries": [entry],
                        "renamed": 1,
                        "failed": 0,
                        "skipped": 0,
                    })
                    jid = ev["id"]

                # Ensure journal has the entry with folder_renamed for sample restore
                if jid:
                    rename_journal.finalize_event(jid, {
                        "kind": "batch_rename",
                        "status": "completed",
                        "entries": [entry],
                        "renamed": 1,
                        "failed": 0,
                        "skipped": 0,
                    })
                    # clear rolled_back if any
                    def _clear_rb(obj):
                        obj.pop("rolled_back", None)
                        obj.pop("rolled_back_at", None)
                        obj["status"] = "completed"
                    rename_journal._rewrite_event(jid, _clear_rb)

                rb = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb.get("success"), rb)
                self.assertEqual(rb.get("restored"), 1)

                restored = VideoRepository().get_by_path(old_uri)
                assert restored is not None
                self.assertEqual(restored.path, old_uri)
                # sample_images must point back under old folder
                self.assertEqual(len(restored.sample_images), 2)
                for uri, old in zip(restored.sample_images, old_samples):
                    self.assertEqual(uri, old)
                    from core.path_utils import uri_to_fs_path
                    self.assertTrue(Path(uri_to_fs_path(uri)).exists())
                # Files back at original paths
                self.assertTrue(video.exists())
                self.assertTrue(s1.exists())
                self.assertTrue(s2.exists())

    def test_db_update_failure_rolls_back_files(self) -> None:
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "TEST-400")
                db_video = self._seed_db(video, cover, [s1, s2], "TEST-400")

                from web.routers.showcase import _rename_video_assets

                with mock.patch.object(
                    VideoRepository,
                    "update_media_paths",
                    return_value=False,
                ):
                    with self.assertRaises(RuntimeError):
                        _rename_video_assets(
                            db_video,
                            {},
                            rename_folder=True,
                            dry_run=False,
                            journal=True,
                        )
                # Files restored
                self.assertTrue(video.exists())
                self.assertTrue(folder.exists())
                still = VideoRepository().get_by_path(db_video.path)
                assert still is not None
                self.assertEqual(still.path, db_video.path)


if __name__ == "__main__":
    unittest.main()
