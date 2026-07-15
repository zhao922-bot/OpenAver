"""Rename journal + real rename failure-path tests."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
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

    def test_rewrite_preserves_malformed_lines(self) -> None:
        with TempDb() as db_path:
            journal = db_path.parent / "rename_history.jsonl"
            good = {
                "id": "abc123def456",
                "kind": "batch_rename",
                "status": "completed",
                "renamed": 1,
            }
            journal.write_text(
                "not-json-at-all\n"
                + json.dumps(good, ensure_ascii=False)
                + "\n{broken\n",
                encoding="utf-8",
            )
            ok = rename_journal.mark_rolled_back("abc123def456")
            self.assertTrue(ok)
            raw = journal.read_text(encoding="utf-8")
            self.assertIn("not-json-at-all", raw)
            self.assertIn("{broken", raw)
            self.assertIn('"rolled_back":true', raw.replace(" ", ""))

    def test_atomic_rewrite_failure_preserves_original(self) -> None:
        """Failed os.replace must keep original journal and leave no temp files."""
        with TempDb() as db_path:
            journal = db_path.parent / "rename_history.jsonl"
            original = (
                json.dumps({
                    "id": "evt-keep-me",
                    "kind": "batch_rename",
                    "status": "completed",
                    "secret_marker": "ORIGINAL_CONTENTS",
                    "renamed": 1,
                }, ensure_ascii=False)
                + "\n"
            )
            journal.write_text(original, encoding="utf-8")

            with mock.patch("core.rename_journal.os.replace", side_effect=OSError("disk full")):
                ok = rename_journal.mark_rolled_back("evt-keep-me")
            self.assertFalse(ok)

            # Original contents untouched
            self.assertEqual(journal.read_text(encoding="utf-8"), original)
            self.assertIn("ORIGINAL_CONTENTS", journal.read_text(encoding="utf-8"))
            self.assertNotIn("rolled_back", journal.read_text(encoding="utf-8"))

            # No leftover temp files from the failed rewrite
            leftovers = list(db_path.parent.glob(".rename_journal_*.tmp"))
            self.assertEqual(leftovers, [], f"leftover temp files: {leftovers}")

    def test_nested_lock_does_not_deadlock(self) -> None:
        """RLock allows same-thread nested acquisition."""
        with TempDb():
            ev = rename_journal.append_event({
                "kind": "batch_rename",
                "status": "pending",
                "entries": [],
                "renamed": 0,
                "failed": 0,
                "skipped": 0,
            })
            # Hold the lock and call public APIs that also acquire it
            with rename_journal._journal_lock:
                rename_journal.list_events(limit=5)
                rename_journal.get_event(ev["id"])
                rename_journal.update_pending_progress(ev["id"], {
                    "renamed": 1,
                    "processed": 1,
                })
            again = rename_journal.get_event(ev["id"])
            assert again is not None
            self.assertEqual(again["renamed"], 1)

    def test_concurrent_append_no_lost_lines(self) -> None:
        with TempDb():
            errors: list[BaseException] = []
            barrier = threading.Barrier(8)

            def worker(i: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    rename_journal.append_event({
                        "kind": "batch_rename",
                        "worker": i,
                        "status": "completed",
                    })
                except BaseException as exc:  # noqa: BLE001 — collect for main thread
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [])
            items = rename_journal.list_events(limit=50)
            self.assertEqual(len(items), 8)


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

    def test_pending_journal_contains_new_uri_before_mutation(self) -> None:
        """Single rename pending entry must store new_uri (not None) pre-mutation."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "TEST-450")
                db_video = self._seed_db(video, cover, [s1, s2], "TEST-450")
                captured: list[dict] = []

                from web.routers.showcase import _rename_video_assets

                real_append = rename_journal.append_event

                def _capture_append(event):
                    captured.append(json.loads(json.dumps(event)))
                    return real_append(event)

                with mock.patch.object(rename_journal, "append_event", side_effect=_capture_append):
                    result = _rename_video_assets(
                        db_video, {}, rename_folder=True, dry_run=False, journal=True,
                    )
                self.assertTrue(result.get("renamed"))
                self.assertTrue(captured)
                pending_entry = captured[0]["entries"][0]
                self.assertIsNotNone(pending_entry.get("new_uri"))
                self.assertEqual(pending_entry["new_uri"], result["new_uri"])
                self.assertEqual(pending_entry["old_uri"], result["old_uri"])
                self.assertEqual(pending_entry["old_path"], result["old_path"])
                self.assertEqual(pending_entry["new_path"], result["new_path"])
                self.assertIn("file_moves", pending_entry)
                self.assertIn("folder_renamed", pending_entry)
                self.assertEqual(pending_entry.get("number"), "TEST-450")
                self.assertIsNotNone(pending_entry.get("nfo_before"))


class RenameRecoveryPhase2Tests(unittest.TestCase):
    """Phase-2: journal completeness, finalize/progress failures, rollback verify."""

    def setUp(self) -> None:
        # Batch apply filters by configured library dirs; tests use temp paths.
        self._cfg_patch = mock.patch(
            "web.routers.showcase._get_configured_dirs",
            return_value=(set(), {}),
        )
        self._cfg_patch.start()

    def tearDown(self) -> None:
        self._cfg_patch.stop()

    def _make_video_tree(self, root: Path, number: str = "PH2-100"):
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

    def test_single_finalize_false_keeps_pending_and_rollback_works(self) -> None:
        """1. finalize_event False: pending has new_uri; rollback restores FS/DB/cover/samples."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-1")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-1")
                old_uri = db_video.path
                old_samples = list(db_video.sample_images)
                old_cover = db_video.cover_path

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback, RenameRollbackRequest

                with mock.patch.object(rename_journal, "finalize_event", return_value=False):
                    result = _rename_video_assets(
                        db_video, {}, rename_folder=True, dry_run=False, journal=True,
                    )

                self.assertTrue(result.get("renamed"))
                jid = result["journal_id"]
                self.assertIsNotNone(jid)
                self.assertEqual(result.get("journal_status"), "pending")
                self.assertEqual(result.get("journal_warning"), "journal_finalize_failed")

                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertEqual(ev.get("status"), "pending")
                self.assertFalse(ev.get("rolled_back"))
                entry = ev["entries"][0]
                self.assertEqual(entry.get("new_uri"), result["new_uri"])
                self.assertIsNotNone(entry.get("new_uri"))
                self.assertTrue(entry.get("file_moves"))
                self.assertIsNotNone(entry.get("nfo_before"))

                # Files/DB are at new location; only after rollback status is rolled_back
                self.assertTrue(Path(result["new_path"]).exists())
                self.assertIsNone(VideoRepository().get_by_path(old_uri))

                rb = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb.get("success"), rb)
                self.assertEqual(rb.get("restored"), 1)

                after = rename_journal.get_event(jid)
                assert after is not None
                self.assertTrue(after.get("rolled_back"))
                self.assertEqual(after.get("status"), "rolled_back")

                restored = VideoRepository().get_by_path(old_uri)
                assert restored is not None
                self.assertEqual(restored.path, old_uri)
                self.assertEqual(restored.cover_path, old_cover)
                self.assertEqual(list(restored.sample_images), old_samples)
                self.assertTrue(video.exists())
                self.assertTrue(s1.exists())
                self.assertTrue(s2.exists())
                self.assertFalse(Path(result["new_path"]).exists())

    def test_batch_pre_progress_false_does_not_mutate(self) -> None:
        """2. Pre-mutation progress False: item not mutated; batch not clean success."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-2")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-2")
                old_uri = db_video.path

                from web.routers.operations import rename_apply, RenameBatchRequest
                from web.routers import showcase as showcase_mod

                real_rename = showcase_mod._rename_video_assets
                mutate_calls = {"n": 0}

                def _wrap(vid, path_mappings, rename_folder=True, dry_run=False, journal=True):
                    if not dry_run:
                        mutate_calls["n"] += 1
                    return real_rename(
                        vid, path_mappings,
                        rename_folder=rename_folder,
                        dry_run=dry_run,
                        journal=journal,
                    )

                def _progress_fail(event_id, progress):
                    # Fail when a recovery plan is being persisted (pre-mutation)
                    if progress.get("entries"):
                        return False
                    return True

                with mock.patch.object(showcase_mod, "_rename_video_assets", side_effect=_wrap):
                    with mock.patch.object(
                        rename_journal, "update_pending_progress", side_effect=_progress_fail
                    ):
                        resp = rename_apply(RenameBatchRequest(
                            paths=[old_uri],
                            rename_folder=True,
                            dry_run=False,
                        ))

                self.assertFalse(resp.get("success"), resp)
                self.assertEqual(resp.get("error"), "pre_mutation_journal_failed")
                self.assertEqual(mutate_calls["n"], 0)
                self.assertTrue(video.exists())
                self.assertIsNotNone(VideoRepository().get_by_path(old_uri))
                self.assertEqual(resp.get("renamed"), 0)

    def test_batch_post_progress_fail_after_mutation_keeps_plan(self) -> None:
        """3. Post-mutation progress failure: complete plan remains; rollback restores FS+DB."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-3")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-3")
                old_uri = db_video.path
                old_samples = list(db_video.sample_images)

                from web.routers.operations import (
                    rename_apply, RenameBatchRequest, rename_rollback, RenameRollbackRequest,
                )

                real_update = rename_journal.update_pending_progress

                def _post_fail(event_id, progress):
                    entries = progress.get("entries") or []
                    if entries and any(e.get("status") == "completed" for e in entries):
                        # Pre-mutation plan already durable; refuse post-mutation progress write.
                        return False
                    return real_update(event_id, progress)

                with mock.patch.object(
                    rename_journal, "update_pending_progress", side_effect=_post_fail
                ):
                    resp = rename_apply(RenameBatchRequest(
                        paths=[old_uri],
                        rename_folder=True,
                        dry_run=False,
                    ))

                self.assertFalse(resp.get("success"), resp)
                self.assertEqual(resp.get("error"), "post_mutation_progress_failed")
                self.assertEqual(resp.get("renamed"), 1)
                jid = resp["journal_id"]
                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertEqual(len(ev.get("entries") or []), 1)
                entry = ev["entries"][0]
                for key in (
                    "old_uri", "new_uri", "old_path", "new_path",
                    "file_moves", "folder_renamed", "number",
                ):
                    self.assertIn(key, entry)
                self.assertIsNotNone(entry.get("new_uri"))
                self.assertIsNotNone(entry.get("nfo_before"))
                # Mutation applied
                self.assertIsNone(VideoRepository().get_by_path(old_uri))
                self.assertIsNotNone(VideoRepository().get_by_path(entry["new_uri"]))

                rb = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb.get("success"), rb)
                restored = VideoRepository().get_by_path(old_uri)
                assert restored is not None
                self.assertEqual(list(restored.sample_images), old_samples)
                self.assertTrue(video.exists())

    def test_batch_finalize_false_not_clean_success(self) -> None:
        """4. Batch finalize False: not clean success; mutations remain recoverable."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-4")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-4")
                old_uri = db_video.path

                from web.routers.operations import (
                    rename_apply, RenameBatchRequest, rename_rollback, RenameRollbackRequest,
                )

                with mock.patch.object(rename_journal, "finalize_event", return_value=False):
                    resp = rename_apply(RenameBatchRequest(
                        paths=[old_uri],
                        rename_folder=True,
                        dry_run=False,
                    ))

                self.assertFalse(resp.get("success"), resp)
                self.assertEqual(resp.get("error"), "journal_finalize_failed")
                self.assertEqual(resp.get("journal_status"), "pending")
                self.assertEqual(resp.get("renamed"), 1)
                jid = resp["journal_id"]
                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertEqual(ev.get("status"), "pending")
                self.assertEqual(len(ev["entries"]), 1)
                self.assertIsNotNone(ev["entries"][0].get("new_uri"))

                rb = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb.get("success"), rb)
                self.assertIsNotNone(VideoRepository().get_by_path(old_uri))
                self.assertTrue(video.exists())

    def test_rollback_update_media_paths_false_not_marked(self) -> None:
        """5. update_media_paths False (and not already restored): fail, not rolled_back."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-5")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-5")
                old_uri = db_video.path

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback, RenameRollbackRequest

                result = _rename_video_assets(
                    db_video, {}, rename_folder=True, dry_run=False, journal=True,
                )
                jid = result["journal_id"]
                new_uri = result["new_uri"]

                with mock.patch.object(
                    VideoRepository, "update_media_paths", return_value=False
                ):
                    rb = rename_rollback(RenameRollbackRequest(event_id=jid))

                self.assertFalse(rb.get("success"), rb)
                self.assertTrue(any("update_media_paths" in e for e in rb.get("errors") or []))
                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertFalse(ev.get("rolled_back"))
                self.assertNotEqual(ev.get("status"), "rolled_back")
                # DB still at new path (mock prevented restore)
                self.assertIsNotNone(VideoRepository().get_by_path(new_uri))

    def test_nfo_restore_write_failure_not_marked(self) -> None:
        """6. NFO restore write failure: response fails; not marked rolled_back."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-6")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-6")

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback, RenameRollbackRequest
                import web.routers.operations as ops_mod

                result = _rename_video_assets(
                    db_video, {}, rename_folder=True, dry_run=False, journal=True,
                )
                jid = result["journal_id"]

                with mock.patch.object(
                    ops_mod,
                    "_restore_nfo_snapshot",
                    side_effect=OSError("nfo write denied"),
                ):
                    rb = rename_rollback(RenameRollbackRequest(event_id=jid))

                self.assertFalse(rb.get("success"), rb)
                self.assertTrue(
                    any("nfo_restore" in e for e in rb.get("errors") or []),
                    rb,
                )
                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertFalse(ev.get("rolled_back"))

    def test_mark_rolled_back_false_response_fails_retryable(self) -> None:
        """7. mark_rolled_back False: success false; retry still possible."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, "PH2-7")
                db_video = self._seed_db(video, cover, [s1, s2], "PH2-7")
                old_uri = db_video.path

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback, RenameRollbackRequest

                result = _rename_video_assets(
                    db_video, {}, rename_folder=True, dry_run=False, journal=True,
                )
                jid = result["journal_id"]

                with mock.patch.object(rename_journal, "mark_rolled_back", return_value=False):
                    rb = rename_rollback(RenameRollbackRequest(event_id=jid))

                self.assertFalse(rb.get("success"), rb)
                self.assertIn("mark_rolled_back_failed", rb.get("errors") or [])
                # FS/DB actually restored (idempotent retry can finish marking)
                self.assertTrue(video.exists())
                self.assertIsNotNone(VideoRepository().get_by_path(old_uri))
                ev = rename_journal.get_event(jid)
                assert ev is not None
                self.assertFalse(ev.get("rolled_back"))

                # Retry without mock succeeds
                rb2 = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb2.get("success"), rb2)
                ev2 = rename_journal.get_event(jid)
                assert ev2 is not None
                self.assertTrue(ev2.get("rolled_back"))

    def test_retry_after_partial_rollback_idempotent(self) -> None:
        """8. Partial rollback then retry succeeds idempotently."""
        with TempDb():
            with tempfile.TemporaryDirectory(prefix="oa-rename-") as td:
                root = Path(td)
                # Two items in one journal event
                trees = []
                for num in ("PH2-8A", "PH2-8B"):
                    folder, video, cover, nfo, s1, s2 = self._make_video_tree(root, num)
                    db_video = self._seed_db(video, cover, [s1, s2], num)
                    trees.append((folder, video, cover, nfo, s1, s2, db_video))

                from web.routers.showcase import _rename_video_assets
                from web.routers.operations import rename_rollback, RenameRollbackRequest

                entries = []
                for folder, video, cover, nfo, s1, s2, db_video in trees:
                    result = _rename_video_assets(
                        db_video, {}, rename_folder=True, dry_run=False, journal=False,
                    )
                    self.assertTrue(result.get("renamed"))
                    entries.append({
                        "old_uri": result["old_uri"],
                        "new_uri": result["new_uri"],
                        "old_path": result["old_path"],
                        "new_path": result["new_path"],
                        "folder_renamed": result["folder_renamed"],
                        "file_moves": result["file_moves"],
                        "number": db_video.number,
                        "nfo_before": result.get("nfo_before_text"),
                        "status": "completed",
                    })

                ev = rename_journal.append_event({
                    "kind": "batch_rename",
                    "status": "completed",
                    "entries": entries,
                    "renamed": 2,
                    "failed": 0,
                    "skipped": 0,
                })
                jid = ev["id"]

                # First attempt: fail NFO on the *second* entry processed (reverse order => first tree)
                import web.routers.operations as ops_mod
                real_restore_nfo = ops_mod._restore_nfo_snapshot
                nfo_calls = {"n": 0}

                def _nfo_fail_once(entry, old_dir):
                    nfo_calls["n"] += 1
                    if nfo_calls["n"] == 1:
                        raise OSError("simulated nfo fail on first reverse entry")
                    return real_restore_nfo(entry, old_dir)

                with mock.patch.object(ops_mod, "_restore_nfo_snapshot", side_effect=_nfo_fail_once):
                    rb1 = rename_rollback(RenameRollbackRequest(event_id=jid))

                self.assertFalse(rb1.get("success"), rb1)
                self.assertGreaterEqual(rb1.get("restored", 0), 0)
                # Not marked rolled_back
                mid = rename_journal.get_event(jid)
                assert mid is not None
                self.assertFalse(mid.get("rolled_back"))

                # Retry completes (already-restored entry is verified; remaining restored)
                rb2 = rename_rollback(RenameRollbackRequest(event_id=jid))
                self.assertTrue(rb2.get("success"), rb2)
                self.assertEqual(rb2.get("restored"), 2)
                done = rename_journal.get_event(jid)
                assert done is not None
                self.assertTrue(done.get("rolled_back"))

                for folder, video, cover, nfo, s1, s2, db_video in trees:
                    self.assertTrue(video.exists(), f"missing {video}")
                    self.assertIsNotNone(VideoRepository().get_by_path(db_video.path))


if __name__ == "__main__":
    unittest.main()
