"""Regression tests for NFO update required-field policy, NFO-as-truth,
local-DB-first, no-op fingerprints, and selected-path validation.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from _helpers import TempDb  # noqa: E402

from core.database import Video, VideoRepository, init_db  # noqa: E402
from core.nfo_updater import (  # noqa: E402
    MAX_NFO_UPDATE_PATHS,
    NFO_UPDATE_POLICY_VERSION,
    OPTIONAL_NFO_FIELDS,
    REQUIRED_NFO_FIELDS,
    check_cache_needs_update,
    clear_noop_fingerprint,
    compute_noop_fingerprint,
    get_noop_fingerprint,
    is_noop_suppressed,
    local_db_meta_signature,
    missing_fields,
    needs_update,
    needs_update_from_nfo_fields,
    read_nfo_fields,
    set_noop_fingerprint,
    update_nfo_file,
    update_videos_generator,
)
from web.routers.scanner import validate_nfo_update_paths  # noqa: E402


def _write_nfo(path: Path, body: str) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<movie>\n' + body + "\n</movie>\n",
        encoding="utf-8",
    )


def _complete_required_xml(**overrides) -> str:
    fields = {
        "title": "Sample Title",
        "premiered": "2024-01-01",
        "studio": "Sample Maker",
        "runtime": "120",
        "actor": "Actor A",
        "genre": "TagA",
    }
    fields.update(overrides)
    parts = [
        f"<title>{fields['title']}</title>",
        f"<premiered>{fields['premiered']}</premiered>",
        f"<studio>{fields['studio']}</studio>",
        f"<runtime>{fields['runtime']}</runtime>",
        f"<actor><name>{fields['actor']}</name></actor>",
        f"<genre>{fields['genre']}</genre>",
        f"<tag>{fields['genre']}</tag>",
    ]
    return "\n".join(parts)


class PolicyTests(unittest.TestCase):
    def test_required_and_optional_sets(self) -> None:
        self.assertEqual(
            set(REQUIRED_NFO_FIELDS),
            {"title", "date", "actor", "genre", "maker", "duration"},
        )
        self.assertEqual(set(OPTIONAL_NFO_FIELDS), {"director", "series", "label"})
        # Single shared lists — needs_update uses required_only
        info_dir_only = {
            "title": "T",
            "date": "2020-01-01",
            "actor": "A",
            "genre": "G",
            "maker": "M",
            "duration": 100,
            "director": "",
            "num": "ABC-001",
        }
        need, missing = needs_update(info_dir_only, has_nfo=True)
        self.assertFalse(need)
        self.assertEqual(missing, [])

    def test_director_only_missing_not_candidate_from_nfo(self) -> None:
        fields = {
            "title": "T",
            "date": "2020",
            "actor": "A",
            "genre": "G",
            "maker": "M",
            "duration": 10,
            "director": "",
            "series": "",
            "label": "",
        }
        need, missing = needs_update_from_nfo_fields(
            fields, has_nfo=True, has_number=True
        )
        self.assertFalse(need)
        self.assertEqual(missing, [])

    def test_series_label_only_missing_not_candidates(self) -> None:
        fields = {
            "title": "T",
            "date": "2020",
            "actor": "A",
            "genre": "G",
            "maker": "M",
            "duration": 10,
            "director": "D",
            "series": "",
            "label": "",
        }
        need, missing = needs_update_from_nfo_fields(
            fields, has_nfo=True, has_number=True
        )
        self.assertFalse(need)
        self.assertEqual(missing, [])
        self.assertEqual(missing_fields(fields, required_only=True), [])


class NfoAsTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="nfo-truth-")
        self.root = Path(self._tmpdir.name)
        self.video = self.root / "ABC-001.mp4"
        self.nfo = self.root / "ABC-001.nfo"
        self.video.write_bytes(b"fake")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _cache_entry(self, info_overrides=None, nfo_mtime=1.0) -> dict:
        info = {
            "title": "",
            "date": "",
            "actor": "",
            "genre": "",
            "maker": "",
            "num": "ABC-001",
            "director": "",
            "duration": None,
            "series": "",
            "label": "",
        }
        if info_overrides:
            info.update(info_overrides)
        return {"nfo_mtime": nfo_mtime, "info": info}

    def test_stale_db_missing_but_nfo_complete_not_candidate(self) -> None:
        _write_nfo(self.nfo, _complete_required_xml())
        # DB says everything missing
        cache = {
            str(self.video): self._cache_entry(
                {
                    "title": "",
                    "date": "",
                    "actor": "",
                    "genre": "",
                    "maker": "",
                    "duration": None,
                }
            )
        }
        # Use file URI path that get_nfo_path_from_video can resolve
        # uri_to_fs_path accepts plain FS paths too
        stats = check_cache_needs_update(cache)
        self.assertEqual(stats["need_update"], 0)
        self.assertEqual(stats["paths"], [])

    def test_stale_db_complete_preflight_skips_search(self) -> None:
        _write_nfo(self.nfo, _complete_required_xml())
        path = str(self.video)
        cache = {
            path: self._cache_entry(
                {
                    "title": "",
                    "date": "",
                    "actor": "",
                    "genre": "",
                    "maker": "",
                    "duration": None,
                }
            )
        }
        search = mock.Mock(return_value={"title": "Remote"})
        gen = update_videos_generator(cache, [path], search_fn=search)
        msgs = list(gen)
        search.assert_not_called()
        # should report skipped_complete
        types = [m.get("type") for m in msgs if isinstance(m, dict)]
        self.assertIn("progress", types)

    def test_nfo_missing_required_filled_from_db_no_search(self) -> None:
        # NFO missing title only
        body = _complete_required_xml()
        body = body.replace("<title>Sample Title</title>", "<title></title>")
        _write_nfo(self.nfo, body)
        path = str(self.video)
        cache = {
            path: self._cache_entry(
                {
                    "title": "From DB",
                    "date": "2024-01-01",
                    "actor": "Actor A",
                    "genre": "TagA",
                    "maker": "Sample Maker",
                    "duration": 120,
                }
            )
        }
        search = mock.Mock(return_value={"title": "Remote Should Not Win"})
        gen = update_videos_generator(cache, [path], search_fn=search)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            result = stop.value

        search.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result["updated"], 1)
        fields = read_nfo_fields(str(self.nfo))
        self.assertEqual(fields["title"], "From DB")

    def test_remote_only_for_still_missing_required(self) -> None:
        body = _complete_required_xml()
        body = body.replace("<title>Sample Title</title>", "")
        body = body.replace("<studio>Sample Maker</studio>", "")
        _write_nfo(self.nfo, body)
        path = str(self.video)
        cache = {
            path: self._cache_entry(
                {
                    "title": "Local Title",  # DB has title
                    "date": "2024-01-01",
                    "actor": "Actor A",
                    "genre": "TagA",
                    "maker": "",  # DB missing maker → need network
                    "duration": 120,
                }
            )
        }
        search = mock.Mock(
            return_value={
                "title": "Remote Title",
                "maker": "Remote Maker",
                "actors": ["X"],
                "tags": ["Y"],
            }
        )
        gen = update_videos_generator(cache, [path], search_fn=search)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as stop:
            result = stop.value

        search.assert_called_once_with("ABC-001")
        self.assertEqual(result["updated"], 1)
        fields = read_nfo_fields(str(self.nfo))
        # Local title preferred for missing field; remote only for maker
        self.assertEqual(fields["title"], "Local Title")
        self.assertEqual(fields["maker"], "Remote Maker")


class PreserveExistingTests(unittest.TestCase):
    def test_existing_fields_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            nfo = Path(td) / "X.nfo"
            _write_nfo(
                nfo,
                _complete_required_xml()
                + "\n<website>https://example.com</website>\n"
                + "<plot>Keep me</plot>\n",
            )
            # Try to "fill" with different values — should not overwrite
            updated, msg = update_nfo_file(
                str(nfo),
                {
                    "title": "New Title",
                    "date": "2099-01-01",
                    "actors": ["New Actor"],
                    "tags": ["NewTag"],
                    "maker": "New Maker",
                    "duration": 999,
                },
                {},
            )
            # May still add mpaa if missing
            text = nfo.read_text(encoding="utf-8")
            self.assertIn("Sample Title", text)
            self.assertIn("https://example.com", text)
            self.assertIn("Keep me", text)
            self.assertNotIn("New Title", text)
            self.assertNotIn("New Maker", text)


class NoopFingerprintTests(unittest.TestCase):
    def test_noop_suppresses_second_run_and_invalidates(self) -> None:
        with TempDb() as db_path:
            path = "file:///D:/Videos/JAV/ABC-002.mp4"
            number = "ABC-002"
            missing = ["title"]
            sig = local_db_meta_signature({"title": "", "date": "2020", "num": number})
            fp1 = compute_noop_fingerprint(
                path=path,
                number=number,
                nfo_mtime=100.0,
                missing_required=missing,
                local_db_sig=sig,
            )
            set_noop_fingerprint(path, fp1, db_path=db_path)
            self.assertTrue(
                is_noop_suppressed(path, fp1, force=False, db_path=db_path)
            )
            # force bypasses
            self.assertFalse(
                is_noop_suppressed(path, fp1, force=True, db_path=db_path)
            )
            # mtime change invalidates
            fp2 = compute_noop_fingerprint(
                path=path,
                number=number,
                nfo_mtime=200.0,
                missing_required=missing,
                local_db_sig=sig,
            )
            self.assertNotEqual(fp1, fp2)
            self.assertFalse(
                is_noop_suppressed(path, fp2, force=False, db_path=db_path)
            )
            # DB metadata change invalidates
            sig2 = local_db_meta_signature(
                {"title": "Now have title", "date": "2020", "num": number}
            )
            fp3 = compute_noop_fingerprint(
                path=path,
                number=number,
                nfo_mtime=100.0,
                missing_required=missing,
                local_db_sig=sig2,
            )
            self.assertNotEqual(fp1, fp3)
            # policy version change invalidates
            fp4 = compute_noop_fingerprint(
                path=path,
                number=number,
                nfo_mtime=100.0,
                missing_required=missing,
                local_db_sig=sig,
                policy_version=NFO_UPDATE_POLICY_VERSION + 1,
            )
            self.assertNotEqual(fp1, fp4)
            # missing fields change
            fp5 = compute_noop_fingerprint(
                path=path,
                number=number,
                nfo_mtime=100.0,
                missing_required=["title", "maker"],
                local_db_sig=sig,
            )
            self.assertNotEqual(fp1, fp5)
            clear_noop_fingerprint(path, db_path=db_path)
            self.assertIsNone(get_noop_fingerprint(path, db_path=db_path))

    def test_generator_records_and_suppresses_noop(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                video = root / "ZZZ-009.mp4"
                nfo = root / "ZZZ-009.nfo"
                video.write_bytes(b"x")
                # Missing title; DB also missing title; search returns nothing useful
                body = _complete_required_xml()
                body = body.replace("<title>Sample Title</title>", "")
                _write_nfo(nfo, body)
                path = str(video)
                cache = {
                    path: {
                        "nfo_mtime": 1.0,
                        "info": {
                            "title": "",
                            "date": "2024-01-01",
                            "actor": "Actor A",
                            "genre": "TagA",
                            "maker": "Sample Maker",
                            "num": "ZZZ-009",
                            "director": "",
                            "duration": 120,
                            "series": "",
                            "label": "",
                        },
                    }
                }
                # Remote cannot supply the missing required title
                search = mock.Mock(return_value=None)
                gen = update_videos_generator(
                    cache, [path], search_fn=search, db_path=db_path
                )
                result1 = None
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result1 = stop.value
                self.assertEqual(search.call_count, 1)
                self.assertEqual(result1["no_metadata"], 1)
                self.assertIsNotNone(get_noop_fingerprint(path, db_path=db_path))
                # second run — suppressed (no network)
                gen2 = update_videos_generator(
                    cache, [path], search_fn=search, db_path=db_path
                )
                result2 = None
                try:
                    while True:
                        next(gen2)
                except StopIteration as stop:
                    result2 = stop.value
                self.assertEqual(search.call_count, 1)  # no extra call
                self.assertEqual(result2["suppressed_noop"], 1)


class PathValidationTests(unittest.TestCase):
    def test_validate_dedupe_cap_reject(self) -> None:
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            lib = "file:///D:/Videos/JAV"
            good = f"{lib}/A.mp4"
            good2 = f"{lib}/B.mp4"
            outside = "file:///E:/Other/C.mp4"
            repo.upsert(
                Video(path=good, number="A-001", title="A", nfo_mtime=1.0)
            )
            repo.upsert(
                Video(path=good2, number="B-001", title="B", nfo_mtime=1.0)
            )
            repo.upsert(
                Video(path=outside, number="C-001", title="C", nfo_mtime=1.0)
            )
            dir_uris = [lib + "/"]

            # empty
            accepted, err = validate_nfo_update_paths(
                [], repo=repo, dir_uris=dir_uris
            )
            self.assertEqual(accepted, [])
            self.assertIsNotNone(err)

            # not in DB
            accepted, err = validate_nfo_update_paths(
                [f"{lib}/missing.mp4"], repo=repo, dir_uris=dir_uris
            )
            self.assertEqual(accepted, [])
            self.assertIn("不在資料庫", err)

            # outside library
            accepted, err = validate_nfo_update_paths(
                [outside], repo=repo, dir_uris=dir_uris
            )
            self.assertEqual(accepted, [])
            self.assertIn("不在設定的資料夾", err)

            # dedupe
            accepted, err = validate_nfo_update_paths(
                [good, good, good2], repo=repo, dir_uris=dir_uris
            )
            self.assertIsNone(err)
            self.assertEqual(accepted, [good, good2])

            # cap
            many = [f"{lib}/X{i}.mp4" for i in range(MAX_NFO_UPDATE_PATHS + 1)]
            for p in many:
                repo.upsert(Video(path=p, number="X", nfo_mtime=1.0))
            accepted, err = validate_nfo_update_paths(
                many, repo=repo, dir_uris=dir_uris
            )
            self.assertEqual(accepted, [])
            self.assertIn("上限", err)

    def test_empty_dir_uris_fail_closed(self) -> None:
        """Configured gallery dirs empty → reject selected paths (no fail-open)."""
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            good = "file:///D:/Videos/JAV/A.mp4"
            repo.upsert(Video(path=good, number="A-001", title="A", nfo_mtime=1.0))
            accepted, err = validate_nfo_update_paths(
                [good], repo=repo, dir_uris=[]
            )
            self.assertEqual(accepted, [])
            self.assertIsNotNone(err)
            self.assertIn("未設定", err)


class PartialLocalWriteTests(unittest.TestCase):
    """Local partial fill must write before network; preserve on remote failure."""

    def _nfo_missing_title_and_maker(self, root: Path) -> tuple[str, Path]:
        video = root / "LOC-001.mp4"
        nfo = root / "LOC-001.nfo"
        video.write_bytes(b"x")
        body = _complete_required_xml()
        body = body.replace("<title>Sample Title</title>", "")
        body = body.replace("<studio>Sample Maker</studio>", "")
        _write_nfo(nfo, body)
        return str(video), nfo

    def _cache(self, path: str, *, title: str, maker: str = "") -> dict:
        return {
            path: {
                "nfo_mtime": 1.0,
                "info": {
                    "title": title,
                    "date": "2024-01-01",
                    "actor": "Actor A",
                    "genre": "TagA",
                    "maker": maker,
                    "num": "LOC-001",
                    "director": "",
                    "duration": 120,
                    "series": "",
                    "label": "",
                },
            }
        }

    def test_local_title_remote_none_writes_and_suppresses(self) -> None:
        """NFO misses title+maker; DB has title; remote None → write title,
        updated=1, maker still missing; next run suppressed without re-search.
        """
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                path, nfo = self._nfo_missing_title_and_maker(Path(td))
                cache = self._cache(path, title="Local Title Only", maker="")
                search = mock.Mock(return_value=None)

                gen = update_videos_generator(
                    cache, [path], search_fn=search, db_path=db_path
                )
                result1 = None
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result1 = stop.value

                self.assertEqual(result1["updated"], 1)
                self.assertEqual(result1["failed"], 0)
                fields = read_nfo_fields(str(nfo))
                self.assertEqual(fields["title"], "Local Title Only")
                self.assertFalse(fields.get("maker"))
                self.assertEqual(search.call_count, 1)
                self.assertIsNotNone(get_noop_fingerprint(path, db_path=db_path))

                # Second run: same residual missing → suppressed, no extra search
                gen2 = update_videos_generator(
                    cache, [path], search_fn=search, db_path=db_path
                )
                result2 = None
                try:
                    while True:
                        next(gen2)
                except StopIteration as stop:
                    result2 = stop.value

                self.assertEqual(search.call_count, 1)
                self.assertEqual(result2["suppressed_noop"], 1)
                self.assertEqual(result2["updated"], 0)

    def test_remote_exception_after_local_write_keeps_update(self) -> None:
        """Remote raises after local write → keep local update; not failed."""
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                path, nfo = self._nfo_missing_title_and_maker(Path(td))
                cache = self._cache(path, title="Kept Title", maker="")
                search = mock.Mock(side_effect=RuntimeError("network down"))

                gen = update_videos_generator(
                    cache, [path], search_fn=search, db_path=db_path
                )
                result = None
                try:
                    while True:
                        next(gen)
                except StopIteration as stop:
                    result = stop.value

                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["failed"], 0)
                fields = read_nfo_fields(str(nfo))
                self.assertEqual(fields["title"], "Kept Title")
                self.assertFalse(fields.get("maker"))
                self.assertIsNotNone(get_noop_fingerprint(path, db_path=db_path))


class RouterNfoUpdateEndpointTests(unittest.TestCase):
    """POST/GET /api/gallery/update router-level coverage (no real network/DB files)."""

    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.routers import scanner as scanner_mod

        self.scanner_mod = scanner_mod
        self.app = FastAPI()
        self.app.include_router(scanner_mod.router)
        self.client = TestClient(self.app)

    def test_get_update_returns_streaming_response(self) -> None:
        """GET remains StreamingResponse / full-library-compatible."""
        with TempDb() as db_path:
            # Empty library → done event without work
            with mock.patch.object(
                self.scanner_mod, "get_db_path", return_value=db_path
            ):
                with mock.patch.object(
                    self.scanner_mod,
                    "update_videos_generator",
                    side_effect=AssertionError("GET empty lib must not run generator"),
                ):
                    resp = self.client.get("/api/gallery/update")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
            body = resp.text
            self.assertIn("data:", body)
            # Parse last SSE event
            events = [
                line[6:] for line in body.splitlines() if line.startswith("data: ")
            ]
            self.assertTrue(events)
            last = __import__("json").loads(events[-1])
            self.assertEqual(last.get("type"), "done")

    def test_post_empty_paths_400_no_stream(self) -> None:
        with TempDb() as db_path:
            entered = {"stream": False}

            def _boom(*_a, **_k):
                entered["stream"] = True
                if False:
                    yield ""

            with mock.patch.object(
                self.scanner_mod, "get_db_path", return_value=db_path
            ):
                with mock.patch.object(
                    self.scanner_mod, "_gallery_configured_dir_uris", return_value=["file:///D:/Videos/"]
                ):
                    with mock.patch.object(
                        self.scanner_mod, "_run_nfo_update_stream", side_effect=_boom
                    ):
                        resp = self.client.post(
                            "/api/gallery/update",
                            json={"paths": []},
                        )
            self.assertEqual(resp.status_code, 400)
            data = resp.json()
            self.assertFalse(data.get("success", True))
            self.assertFalse(entered["stream"])

    def test_post_outside_library_400_no_stream(self) -> None:
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            lib = "file:///D:/Videos/JAV"
            outside = "file:///E:/Other/X.mp4"
            repo.upsert(Video(path=outside, number="X-001", nfo_mtime=1.0))
            entered = {"stream": False}

            def _boom(*_a, **_k):
                entered["stream"] = True
                if False:
                    yield ""

            with mock.patch.object(
                self.scanner_mod, "get_db_path", return_value=db_path
            ):
                with mock.patch.object(
                    self.scanner_mod,
                    "_gallery_configured_dir_uris",
                    return_value=[lib + "/"],
                ):
                    with mock.patch.object(
                        self.scanner_mod, "_run_nfo_update_stream", side_effect=_boom
                    ):
                        resp = self.client.post(
                            "/api/gallery/update",
                            json={"paths": [outside]},
                        )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("不在設定的資料夾", resp.json().get("error", ""))
            self.assertFalse(entered["stream"])

    def test_post_not_in_db_400_no_stream(self) -> None:
        with TempDb() as db_path:
            entered = {"stream": False}

            def _boom(*_a, **_k):
                entered["stream"] = True
                if False:
                    yield ""

            with mock.patch.object(
                self.scanner_mod, "get_db_path", return_value=db_path
            ):
                with mock.patch.object(
                    self.scanner_mod,
                    "_gallery_configured_dir_uris",
                    return_value=["file:///D:/Videos/JAV/"],
                ):
                    with mock.patch.object(
                        self.scanner_mod, "_run_nfo_update_stream", side_effect=_boom
                    ):
                        resp = self.client.post(
                            "/api/gallery/update",
                            json={"paths": ["file:///D:/Videos/JAV/missing.mp4"]},
                        )
            self.assertEqual(resp.status_code, 400)
            self.assertIn("不在資料庫", resp.json().get("error", ""))
            self.assertFalse(entered["stream"])

    def test_post_selected_deduped_paths_only(self) -> None:
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            lib = "file:///D:/Videos/JAV"
            good = f"{lib}/A.mp4"
            good2 = f"{lib}/B.mp4"
            repo.upsert(Video(path=good, number="A-001", nfo_mtime=1.0))
            repo.upsert(Video(path=good2, number="B-001", nfo_mtime=1.0))

            captured = {}

            def _fake_stream(paths=None, *, force=False, selected_mode=False):
                captured["paths"] = list(paths or [])
                captured["selected_mode"] = selected_mode
                captured["force"] = force
                yield 'data: {"type":"done","updated":0,"failed":0,"selected":2}\n\n'

            with mock.patch.object(
                self.scanner_mod, "get_db_path", return_value=db_path
            ):
                with mock.patch.object(
                    self.scanner_mod,
                    "_gallery_configured_dir_uris",
                    return_value=[lib + "/"],
                ):
                    with mock.patch.object(
                        self.scanner_mod,
                        "_run_nfo_update_stream",
                        side_effect=_fake_stream,
                    ):
                        resp = self.client.post(
                            "/api/gallery/update",
                            json={"paths": [good, good, good2], "force": True},
                        )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
            self.assertEqual(captured["paths"], [good, good2])
            self.assertTrue(captured["selected_mode"])
            self.assertTrue(captured["force"])


class CandidateDirectorOnlyIntegration(unittest.TestCase):
    def test_director_only_missing_nfo_not_in_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "DIR-001.mp4"
            nfo = root / "DIR-001.nfo"
            video.write_bytes(b"x")
            _write_nfo(nfo, _complete_required_xml())  # no director
            path = str(video)
            cache = {
                path: {
                    "nfo_mtime": time.time(),
                    "info": {
                        "title": "Sample Title",
                        "date": "2024-01-01",
                        "actor": "Actor A",
                        "genre": "TagA",
                        "maker": "Sample Maker",
                        "num": "DIR-001",
                        "director": "",  # DB also missing director
                        "duration": 120,
                        "series": "",
                        "label": "",
                    },
                }
            }
            stats = check_cache_needs_update(cache)
            self.assertEqual(stats["need_update"], 0)


class NfoMtimeSyncTests(unittest.TestCase):
    def test_successful_write_syncs_nfo_mtime(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                video = root / "MT-001.mp4"
                nfo = root / "MT-001.nfo"
                video.write_bytes(b"x")
                body = _complete_required_xml()
                body = body.replace("<title>Sample Title</title>", "")
                _write_nfo(nfo, body)
                # store with file path as videos.path
                path = str(video)
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(
                        path=path,
                        number="MT-001",
                        title="From DB",
                        release_date="2024-01-01",
                        actresses=["Actor A"],
                        tags=["TagA"],
                        maker="Sample Maker",
                        duration=120,
                        nfo_mtime=1.0,
                    )
                )
                cache = {
                    path: {
                        "nfo_mtime": 1.0,
                        "info": {
                            "title": "From DB",
                            "date": "2024-01-01",
                            "actor": "Actor A",
                            "genre": "TagA",
                            "maker": "Sample Maker",
                            "num": "MT-001",
                            "director": "",
                            "duration": 120,
                            "series": "",
                            "label": "",
                        },
                    }
                }
                gen = update_videos_generator(
                    cache, [path], search_fn=mock.Mock(), db_path=db_path
                )
                list(gen)
                row = repo.get_by_path(path)
                self.assertIsNotNone(row)
                self.assertGreater(row.nfo_mtime, 1.0)
                self.assertAlmostEqual(
                    row.nfo_mtime, os.path.getmtime(nfo), places=2
                )


if __name__ == "__main__":
    unittest.main()
