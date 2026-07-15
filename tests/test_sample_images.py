"""Unit tests for local sample-image detection, reconcile, and batch resolve."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _helpers import APP_DIR, TempDb  # noqa: F401 — sets path via import side effects

import sys

sys.path.insert(0, str(APP_DIR))

from core.database import Video, VideoRepository  # noqa: E402
from core.path_utils import to_file_uri  # noqa: E402
from core.sample_images import (  # noqa: E402
    check_multi_video_folder,
    count_direct_videos_on_disk,
    has_valid_local_samples,
    is_valid_local_image_file,
    list_extrafanart_local_images,
    reconcile_sample_images_from_disk,
    resolve_batch_sample_targets,
    scan_missing_samples,
)


def _touch_video(dir_path: Path, name: str = "movie.mp4") -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    vp = dir_path / name
    vp.write_bytes(b"fake-video")
    return vp


def _minimal_image_bytes(suffix: str, size: int = 16) -> bytes:
    """Minimal valid magic header + padding so is_valid_local_image_file accepts it."""
    suf = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
    if suf in (".jpg", ".jpeg"):
        head = b"\xff\xd8\xff\xe0"
    elif suf == ".png":
        head = b"\x89PNG\r\n\x1a\n"
    elif suf == ".webp":
        # RIFF + size + WEBP
        head = b"RIFF" + (12).to_bytes(4, "little") + b"WEBP"
    else:
        head = b"x"
    if size <= len(head):
        return head[:size] if size > 0 else b""
    return head + b"\x00" * (size - len(head))


def _write_img(path: Path, size: int = 16, *, valid: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if size <= 0:
        path.write_bytes(b"")
    elif valid:
        path.write_bytes(_minimal_image_bytes(path.suffix, size))
    else:
        path.write_bytes(b"x" * size)
    return path


class ValidLocalImageTests(unittest.TestCase):
    def test_empty_dir_no_samples(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vp = _touch_video(Path(td) / "IPZZ-001")
            self.assertEqual(list_extrafanart_local_images(str(vp)), [])
            self.assertFalse(has_valid_local_samples(str(vp), []))

    def test_zero_byte_image_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vp = _touch_video(Path(td) / "IPZZ-001")
            zero = Path(td) / "IPZZ-001" / "extrafanart" / "fanart1.jpg"
            _write_img(zero, size=0)
            self.assertFalse(is_valid_local_image_file(zero))
            self.assertEqual(list_extrafanart_local_images(str(vp)), [])
            self.assertFalse(has_valid_local_samples(str(vp), []))

    def test_unsupported_suffix_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vp = _touch_video(Path(td) / "IPZZ-001")
            gif = Path(td) / "IPZZ-001" / "extrafanart" / "fanart1.gif"
            _write_img(gif)
            self.assertFalse(is_valid_local_image_file(gif))
            self.assertEqual(list_extrafanart_local_images(str(vp)), [])

    def test_valid_jpg_png_webp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "IPZZ-001"
            vp = _touch_video(root)
            ef = root / "extrafanart"
            for name in ("fanart1.jpg", "a.png", "b.webp", "c.jpeg"):
                _write_img(ef / name)
            found = list_extrafanart_local_images(str(vp))
            self.assertEqual(len(found), 4)
            self.assertTrue(has_valid_local_samples(str(vp), []))

    def test_random_nonempty_jpg_not_valid(self) -> None:
        """Non-image bytes with .jpg suffix must not count as valid stills."""
        with tempfile.TemporaryDirectory() as td:
            junk = Path(td) / "fake.jpg"
            junk.write_bytes(b"not-an-image-payload!!!!!")
            self.assertFalse(is_valid_local_image_file(junk))

    def test_minimal_magic_samples_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for name in ("a.jpg", "b.png", "c.webp"):
                p = Path(td) / name
                _write_img(p, size=24, valid=True)
                self.assertTrue(is_valid_local_image_file(p), name)

    def test_remote_url_not_local(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            vp = _touch_video(Path(td) / "IPZZ-001")
            remote = ["https://example.com/a.jpg", "http://cdn/x.png"]
            self.assertFalse(has_valid_local_samples(str(vp), remote))

    def test_db_file_uri_valid_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "IPZZ-001"
            vp = _touch_video(root)
            # Image not under extrafanart but referenced by DB
            side = root / "still.jpg"
            _write_img(side)
            uri = to_file_uri(str(side))
            self.assertTrue(has_valid_local_samples(str(vp), [uri]))


class ReconcileAndScanTests(unittest.TestCase):
    def test_reconcile_stale_db_from_disk(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "IPZZ-065"
                vp = _touch_video(root)
                img = _write_img(root / "extrafanart" / "fanart1.jpg")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(
                        path=path_uri,
                        number="IPZZ-065",
                        sample_images=["https://remote/old.jpg"],
                    )
                )
                video = repo.get_by_path(path_uri)
                self.assertTrue(reconcile_sample_images_from_disk(repo, video))
                reloaded = repo.get_by_path(path_uri)
                self.assertEqual(len(reloaded.sample_images), 1)
                self.assertTrue(reloaded.sample_images[0].startswith("file:///"))
                self.assertIn("fanart1.jpg", reloaded.sample_images[0].replace("\\", "/"))
                # Other fields untouched — path/number still same
                self.assertEqual(reloaded.number, "IPZZ-065")
                # second call is no-op
                video2 = repo.get_by_path(path_uri)
                self.assertFalse(reconcile_sample_images_from_disk(repo, video2))
                # img path used
                self.assertTrue(img.is_file())

    def test_scan_extrafanart_has_img_db_empty_reconciles_not_missing(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "IPZZ-227"
                vp = _touch_video(root)
                _write_img(root / "extrafanart" / "fanart1.png")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="IPZZ-227", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                result = scan_missing_samples(repo, [dir_uri])
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["reconciled"], 1)
                reloaded = repo.get_by_path(path_uri)
                self.assertTrue(reloaded.sample_images)

    def test_scan_missing_candidate(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "IPZZ-117"
                vp = _touch_video(root)
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="IPZZ-117", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                result = scan_missing_samples(repo, [dir_uri])
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["items"][0]["number"], "IPZZ-117")
                self.assertEqual(result["skipped_multi"], 0)

    def test_scan_multi_video_folder_skipped(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                folder = Path(td) / "MIX"
                v1 = _touch_video(folder, "A.mp4")
                v2 = _touch_video(folder, "B.mp4")
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=to_file_uri(str(v1)), number="A-001", sample_images=[]))
                repo.upsert(Video(path=to_file_uri(str(v2)), number="B-001", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                result = scan_missing_samples(repo, [dir_uri])
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["skipped_multi"], 2)

    def test_scan_db_one_disk_second_unscanned_skipped_multi(self) -> None:
        """DB has one row; disk has a second video not yet scanned → multi."""
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                folder = Path(td) / "MIX"
                v1 = _touch_video(folder, "A.mp4")
                # Unscanned second video on disk only
                _touch_video(folder, "B.mp4")
                # Also covers configured extensions like .strm
                (folder / "C.strm").write_text("http://example/stream", encoding="utf-8")
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(path=to_file_uri(str(v1)), number="A-001", sample_images=[])
                )
                dir_uri = to_file_uri(td) + "/"
                cfg = {"scraper": {"video_extensions": [".mp4", ".strm", ".mkv"]}}
                result = scan_missing_samples(repo, [dir_uri], config=cfg)
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["skipped_multi"], 1)

                folder_prefix = to_file_uri(str(folder)) + "/"
                is_multi, effective, err = check_multi_video_folder(
                    repo, folder_prefix, config=cfg
                )
                self.assertTrue(is_multi)
                self.assertIsNone(err)
                self.assertGreaterEqual(effective, 2)
                self.assertEqual(
                    count_direct_videos_on_disk(str(folder), config=cfg), 3
                )

    def test_scan_count_exception_fail_closed_skipped_multi(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                folder = Path(td) / "X"
                v1 = _touch_video(folder, "A.mp4")
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(path=to_file_uri(str(v1)), number="X-1", sample_images=[])
                )
                dir_uri = to_file_uri(td) + "/"
                with mock.patch.object(
                    VideoRepository,
                    "count_videos_in_folder",
                    side_effect=RuntimeError("db boom"),
                ):
                    result = scan_missing_samples(repo, [dir_uri])
                self.assertTrue(result["success"])
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["skipped_multi"], 1)

    def test_scan_empty_configured_dirs_fail_closed(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "X")
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=to_file_uri(str(vp)), number="X-1", sample_images=[]))
                result = scan_missing_samples(repo, [])
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], "no_configured_dirs")
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["items"], [])

    def test_scan_outside_dir_rejected(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td_in:
                with tempfile.TemporaryDirectory() as td_out:
                    vp = _touch_video(Path(td_out) / "OUT")
                    repo = VideoRepository(db_path)
                    repo.upsert(
                        Video(path=to_file_uri(str(vp)), number="OUT-1", sample_images=[])
                    )
                    dir_uri = to_file_uri(td_in) + "/"
                    result = scan_missing_samples(repo, [dir_uri])
                    self.assertTrue(result["success"])
                    self.assertEqual(result["count"], 0)


class ResolveBatchTargetsTests(unittest.TestCase):
    def test_empty_selection(self) -> None:
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            accepted, err = resolve_batch_sample_targets(
                repo=repo, dir_uris=["file:///D:/Videos/"], items=[], paths=[]
            )
            self.assertEqual(accepted, [])
            self.assertEqual(err, "empty_selection")

    def test_dedupe_and_ignore_client_number(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "IPZZ-119")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="IPZZ-119", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                accepted, err = resolve_batch_sample_targets(
                    repo=repo,
                    dir_uris=[dir_uri],
                    items=[
                        {"path": path_uri, "number": "FAKE-999"},
                        {"file_path": path_uri, "number": "OTHER"},
                    ],
                    paths=[path_uri],
                )
                self.assertIsNone(err)
                self.assertEqual(len(accepted), 1)
                self.assertEqual(accepted[0]["number"], "IPZZ-119")

    def test_outside_library(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td_in:
                with tempfile.TemporaryDirectory() as td_out:
                    vp = _touch_video(Path(td_out) / "Z")
                    path_uri = to_file_uri(str(vp))
                    repo = VideoRepository(db_path)
                    repo.upsert(Video(path=path_uri, number="Z-1", sample_images=[]))
                    accepted, err = resolve_batch_sample_targets(
                        repo=repo,
                        dir_uris=[to_file_uri(td_in) + "/"],
                        paths=[path_uri],
                    )
                    self.assertEqual(accepted, [])
                    self.assertEqual(err, "outside_library")

    def test_no_configured_dirs(self) -> None:
        with TempDb() as db_path:
            repo = VideoRepository(db_path)
            accepted, err = resolve_batch_sample_targets(
                repo=repo, dir_uris=[], paths=["file:///x"]
            )
            self.assertEqual(err, "no_configured_dirs")
            self.assertEqual(accepted, [])


class FetchSamplesOnlySemanticsTests(unittest.TestCase):
    def test_no_sample_urls_not_success(self) -> None:
        from core.enricher import fetch_samples_only

        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "NO-SAMPLES")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(path=path_uri, number="NO-SAMPLES", title="Keep", sample_images=[])
                )
                with mock.patch("core.enricher.search_jav", return_value={
                    "source": "javbus",
                    "sample_images": [],
                    "title": "ShouldNotWrite",
                }):
                    result = fetch_samples_only(path_uri, "NO-SAMPLES")
                self.assertFalse(result.success)
                self.assertEqual(result.error, "no_samples")
                self.assertEqual(result.extrafanart_written, 0)
                self.assertFalse(result.nfo_written)
                self.assertFalse(result.cover_written)
                reloaded = repo.get_by_path(path_uri)
                self.assertEqual(reloaded.title, "Keep")
                self.assertEqual(reloaded.sample_images, [])
                self.assertFalse((Path(td) / "NO-SAMPLES" / "extrafanart").exists() or
                                 any((Path(td) / "NO-SAMPLES").glob("*.nfo")))

    def test_all_downloads_fail_not_success(self) -> None:
        from core.enricher import fetch_samples_only

        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "FAIL-DL")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="FAIL-DL", title="T", sample_images=[]))
                with mock.patch(
                    "core.enricher.search_jav",
                    return_value={
                        "source": "javdb",
                        "sample_images": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                    },
                ):
                    with mock.patch("core.enricher.download_image", return_value=False):
                        result = fetch_samples_only(path_uri, "FAIL-DL")
                self.assertFalse(result.success)
                self.assertEqual(result.error, "download_failed")
                self.assertEqual(result.extrafanart_written, 0)
                reloaded = repo.get_by_path(path_uri)
                self.assertEqual(reloaded.title, "T")
                self.assertEqual(reloaded.sample_images, [])

    def test_partial_success_writes_only_existing_uris(self) -> None:
        from core.enricher import fetch_samples_only

        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "PARTIAL"
                vp = _touch_video(root)
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="PARTIAL", sample_images=[]))

                def _dl(url, dest):
                    # Only first URL succeeds
                    if url.endswith("a.jpg"):
                        Path(dest).parent.mkdir(parents=True, exist_ok=True)
                        Path(dest).write_bytes(_minimal_image_bytes(".jpg", 40))
                        return True
                    return False

                with mock.patch(
                    "core.enricher.search_jav",
                    return_value={
                        "source": "javbus",
                        "sample_images": [
                            "https://example.com/a.jpg",
                            "https://example.com/b.jpg",
                        ],
                    },
                ):
                    with mock.patch("core.enricher.download_image", side_effect=_dl):
                        result = fetch_samples_only(
                            path_uri, "PARTIAL", db_path=db_path
                        )
                self.assertTrue(result.success)
                self.assertEqual(result.extrafanart_written, 1)
                reloaded = repo.get_by_path(path_uri)
                self.assertEqual(len(reloaded.sample_images), 1)
                fs = reloaded.sample_images[0]
                self.assertTrue(fs.startswith("file:///"))
                # No NFO/cover side effects
                self.assertFalse(list(root.glob("*.nfo")))
                self.assertFalse(result.nfo_written)
                self.assertFalse(result.cover_written)

    def test_success_with_preexisting_disk_images_reports_zero_new(self) -> None:
        """If files already on disk (or concurrent write) but newly_written=0,
        success=True with extrafanart_written=0; DB still syncs actual files."""
        from core.enricher import fetch_samples_only

        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "EXIST"
                vp = _touch_video(root)
                path_uri = to_file_uri(str(vp))
                # Pre-seed disk still so _write_extrafanart skips overwrite
                _write_img(root / "extrafanart" / "fanart1.jpg", size=40)
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="EXIST", sample_images=[]))

                with mock.patch(
                    "core.enricher.search_jav",
                    return_value={
                        "source": "javbus",
                        "sample_images": ["https://example.com/a.jpg"],
                    },
                ):
                    with mock.patch(
                        "core.enricher.download_image", return_value=False
                    ) as dl:
                        result = fetch_samples_only(
                            path_uri, "EXIST", db_path=db_path
                        )
                self.assertTrue(result.success)
                # Must report newly written count, NOT final_uris total.
                self.assertEqual(result.extrafanart_written, 0)
                reloaded = repo.get_by_path(path_uri)
                self.assertEqual(len(reloaded.sample_images), 1)
                self.assertTrue(reloaded.sample_images[0].startswith("file:///"))
                # download may or may not be attempted for fanart1 (skipped existing)
                _ = dl

    def test_writer_does_not_overwrite_existing(self) -> None:
        from core.enricher import _write_extrafanart

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "KEEP"
            vp = _touch_video(root)
            existing = root / "extrafanart" / "fanart1.jpg"
            _write_img(existing, size=32)
            original = existing.read_bytes()

            with mock.patch("core.enricher.download_image") as dl:
                dl.return_value = True
                written = _write_extrafanart(
                    str(vp),
                    ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                    write_extrafanart=True,
                )
            # fanart1 skipped; only fanart2 may be attempted
            self.assertEqual(existing.read_bytes(), original)
            # download called only for non-existing slots
            self.assertTrue(dl.called)
            # newly written may include fanart2 only
            for uri in written:
                self.assertNotIn("fanart1.jpg", uri.replace("\\", "/"))


class RouterMissingSamplesAndBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.routers import scraper as scraper_mod

        self.scraper_mod = scraper_mod
        # Reset busy flag between tests
        scraper_mod._batch_fetch_samples_busy = False
        self.app = FastAPI()
        self.app.include_router(scraper_mod.router)
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.scraper_mod._batch_fetch_samples_busy = False

    def test_get_missing_samples_empty_dirs_fail_closed(self) -> None:
        with TempDb() as db_path:
            with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                with mock.patch.object(
                    self.scraper_mod, "_configured_gallery_dir_uris", return_value=[]
                ):
                    resp = self.client.get("/api/scraper/missing-samples")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["success"])
            self.assertEqual(data["count"], 0)

    def test_get_missing_samples_lists_candidate(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "IPZZ-065")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="IPZZ-065", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        resp = self.client.get("/api/scraper/missing-samples")
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertEqual(data["count"], 1)
                self.assertEqual(data["items"][0]["number"], "IPZZ-065")

    def test_post_empty_body_400(self) -> None:
        with TempDb() as db_path:
            with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                with mock.patch.object(
                    self.scraper_mod,
                    "_configured_gallery_dir_uris",
                    return_value=["file:///D:/Videos/"],
                ):
                    resp = self.client.post(
                        "/api/scraper/batch-fetch-samples",
                        json={},
                    )
            self.assertEqual(resp.status_code, 400)
            self.assertFalse(resp.json().get("success", True))

    def test_post_outside_whitelist_400(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td_in:
                with tempfile.TemporaryDirectory() as td_out:
                    vp = _touch_video(Path(td_out) / "OUT")
                    path_uri = to_file_uri(str(vp))
                    repo = VideoRepository(db_path)
                    repo.upsert(Video(path=path_uri, number="OUT-1", sample_images=[]))
                    with mock.patch.object(
                        self.scraper_mod, "get_db_path", return_value=db_path
                    ):
                        with mock.patch.object(
                            self.scraper_mod,
                            "_configured_gallery_dir_uris",
                            return_value=[to_file_uri(td_in) + "/"],
                        ):
                            resp = self.client.post(
                                "/api/scraper/batch-fetch-samples",
                                json={"paths": [path_uri]},
                            )
            self.assertEqual(resp.status_code, 400)

    def test_post_client_forged_number_uses_db(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "REAL-001")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="REAL-001", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                seen_numbers = []

                def _fake_process(path, number, proxy_url, db_path=None, **_kw):
                    seen_numbers.append(number)
                    return {
                        "status": "no_samples",
                        "number": number,
                        "path": path,
                        "images_written": 0,
                        "error": "no_samples",
                    }

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch.object(
                            self.scraper_mod,
                            "_process_one_batch_sample",
                            side_effect=_fake_process,
                        ):
                            resp = self.client.post(
                                "/api/scraper/batch-fetch-samples",
                                json={
                                    "items": [
                                        {"path": path_uri, "number": "FORGED-999"}
                                    ]
                                },
                            )
                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/event-stream", resp.headers.get("content-type", ""))
                self.assertEqual(seen_numbers, ["REAL-001"])
                # busy released
                self.assertFalse(self.scraper_mod._batch_fetch_samples_busy)

    def test_post_recheck_skips_complete(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "HAS"
                vp = _touch_video(root)
                _write_img(root / "extrafanart" / "fanart1.jpg")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="HAS-1", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                fetch_called = {"n": 0}

                def _boom(*_a, **_k):
                    fetch_called["n"] += 1
                    raise AssertionError("must not call fetch when already complete")

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "core.enricher.fetch_samples_only", side_effect=_boom
                        ):
                            resp = self.client.post(
                                "/api/scraper/batch-fetch-samples",
                                json={"paths": [path_uri]},
                            )
                self.assertEqual(resp.status_code, 200)
                events = [
                    line[6:]
                    for line in resp.text.splitlines()
                    if line.startswith("data: ")
                ]
                import json as _json

                parsed = [_json.loads(e) for e in events]
                item_ev = next(e for e in parsed if e.get("type") == "item")
                self.assertEqual(item_ev["status"], "skipped_complete")
                done = next(e for e in parsed if e.get("type") == "done")
                self.assertEqual(done["summary"]["skipped_complete"], 1)
                self.assertEqual(fetch_called["n"], 0)
                self.assertFalse(self.scraper_mod._batch_fetch_samples_busy)

    def test_post_summary_success_no_samples_failed(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                paths = []
                repo = VideoRepository(db_path)
                for name in ("OK", "NONE", "BAD"):
                    vp = _touch_video(Path(td) / name)
                    uri = to_file_uri(str(vp))
                    repo.upsert(Video(path=uri, number=name, sample_images=[]))
                    paths.append(uri)
                dir_uri = to_file_uri(td) + "/"

                def _proc(path, number, proxy_url, db_path=None, **_kw):
                    if number == "OK":
                        return {
                            "status": "success",
                            "number": number,
                            "path": path,
                            "images_written": 3,
                            "error": None,
                        }
                    if number == "NONE":
                        return {
                            "status": "no_samples",
                            "number": number,
                            "path": path,
                            "images_written": 0,
                            "error": "no_samples",
                        }
                    return {
                        "status": "failed",
                        "number": number,
                        "path": path,
                        "images_written": 0,
                        "error": "download_failed",
                    }

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch.object(
                            self.scraper_mod,
                            "_process_one_batch_sample",
                            side_effect=_proc,
                        ):
                            resp = self.client.post(
                                "/api/scraper/batch-fetch-samples",
                                json={"paths": paths},
                            )
                import json as _json

                events = [
                    _json.loads(line[6:])
                    for line in resp.text.splitlines()
                    if line.startswith("data: ")
                ]
                done = next(e for e in events if e.get("type") == "done")
                s = done["summary"]
                self.assertEqual(s["success"], 1)
                self.assertEqual(s["images_downloaded"], 3)
                self.assertEqual(s["no_samples"], 1)
                self.assertEqual(s["failed"], 1)
                self.assertFalse(self.scraper_mod._batch_fetch_samples_busy)

    def test_process_row_deleted_after_validate_no_fetch(self) -> None:
        """After resolve, if DB row is gone, processor must not call fetch."""
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "GONE")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="GONE-1", sample_images=[]))
                # Simulate post-validate deletion
                repo.delete_by_paths([path_uri])
                self.assertIsNone(repo.get_by_path(path_uri))

                fetch_called = {"n": 0}

                def _boom(*_a, **_k):
                    fetch_called["n"] += 1
                    raise AssertionError("must not fetch when row gone")

                with mock.patch(
                    "web.routers.scraper.fetch_samples_only", side_effect=_boom
                ):
                    result = self.scraper_mod._process_one_batch_sample(
                        path_uri, "GONE-1", "", db_path
                    )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "not_in_db")
                self.assertEqual(fetch_called["n"], 0)

    def test_process_count_exception_fail_closed_no_fetch(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "ERR")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="ERR-1", sample_images=[]))

                fetch_called = {"n": 0}

                def _boom(*_a, **_k):
                    fetch_called["n"] += 1
                    raise AssertionError("must not fetch on multi-check failure")

                with mock.patch(
                    "core.sample_images.check_multi_video_folder",
                    return_value=(True, -1, "db_count_failed"),
                ):
                    with mock.patch(
                        "web.routers.scraper.fetch_samples_only", side_effect=_boom
                    ):
                        # Also patch the name imported into scraper if needed
                        with mock.patch.object(
                            self.scraper_mod,
                            "check_multi_video_folder",
                            return_value=(True, -1, "db_count_failed"),
                        ):
                            result = self.scraper_mod._process_one_batch_sample(
                                path_uri, "ERR-1", "", db_path
                            )
                self.assertEqual(result["status"], "skipped_multi")
                self.assertEqual(fetch_called["n"], 0)

    def test_router_real_processor_temp_db_updates_sample_images(self) -> None:
        """End-to-end processor path on temp DB; mock network only, not processor."""
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "INT"
                vp = _touch_video(root)
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(
                    Video(
                        path=path_uri,
                        number="INT-001",
                        title="Keep",
                        sample_images=[],
                    )
                )
                dir_uri = to_file_uri(td) + "/"

                def _dl(_url, dest):
                    Path(dest).parent.mkdir(parents=True, exist_ok=True)
                    Path(dest).write_bytes(_minimal_image_bytes(".jpg", 40))
                    return True

                # Spy: VideoRepository must only open the temp db_path.
                opened_paths: list = []
                real_vr = VideoRepository

                class _TrackingRepo(real_vr):  # type: ignore[valid-type,misc]
                    def __init__(self, db_path_arg=None):
                        opened_paths.append(db_path_arg)
                        super().__init__(db_path_arg)

                with mock.patch.object(
                    self.scraper_mod, "get_db_path", return_value=db_path
                ):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "web.routers.scraper.VideoRepository", _TrackingRepo
                        ):
                            with mock.patch(
                                "core.enricher.VideoRepository", _TrackingRepo
                            ):
                                with mock.patch(
                                    "core.enricher.search_jav",
                                    return_value={
                                        "source": "javbus",
                                        "sample_images": [
                                            "https://example.com/a.jpg",
                                            "https://example.com/b.jpg",
                                        ],
                                    },
                                ):
                                    with mock.patch(
                                        "core.enricher.download_image",
                                        side_effect=_dl,
                                    ):
                                        # Do NOT mock _process_one_batch_sample.
                                        resp = self.client.post(
                                            "/api/scraper/batch-fetch-samples",
                                            json={"paths": [path_uri]},
                                        )
                self.assertEqual(resp.status_code, 200)
                import json as _json

                events = [
                    _json.loads(line[6:])
                    for line in resp.text.splitlines()
                    if line.startswith("data: ")
                ]
                item_ev = next(e for e in events if e.get("type") == "item")
                self.assertEqual(item_ev["status"], "success")
                self.assertEqual(item_ev["images_written"], 2)
                done = next(e for e in events if e.get("type") == "done")
                self.assertEqual(done["summary"]["success"], 1)
                self.assertEqual(done["summary"]["images_downloaded"], 2)

                # Same temp DB sample_images updated; title untouched.
                reloaded = VideoRepository(db_path).get_by_path(path_uri)
                self.assertIsNotNone(reloaded)
                self.assertEqual(reloaded.title, "Keep")
                self.assertEqual(len(reloaded.sample_images), 2)
                for u in reloaded.sample_images:
                    self.assertTrue(u.startswith("file:///"))

                # Production default path never used (all opens carry temp db_path).
                self.assertTrue(opened_paths)
                for p in opened_paths:
                    self.assertIsNotNone(p)
                    self.assertEqual(Path(p).resolve(), Path(db_path).resolve())
                self.assertFalse(self.scraper_mod._batch_fetch_samples_busy)

    def test_busy_returns_409_and_exception_releases(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "B")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="B-1", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"

                # Simulate busy
                self.scraper_mod._batch_fetch_samples_busy = True
                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        resp = self.client.post(
                            "/api/scraper/batch-fetch-samples",
                            json={"paths": [path_uri]},
                        )
                self.assertEqual(resp.status_code, 409)
                self.scraper_mod._batch_fetch_samples_busy = False

                # Exception path still releases busy via finally
                def _raise(*_a, **_k):
                    raise RuntimeError("boom")

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch.object(
                            self.scraper_mod,
                            "_process_one_batch_sample",
                            side_effect=_raise,
                        ):
                            resp2 = self.client.post(
                                "/api/scraper/batch-fetch-samples",
                                json={"paths": [path_uri]},
                            )
                self.assertEqual(resp2.status_code, 200)
                # item counted as failed, busy cleared
                self.assertFalse(self.scraper_mod._batch_fetch_samples_busy)


class MultiVideoHelperUnitTests(unittest.TestCase):
    def test_disk_count_uses_config_extensions_including_strm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            (folder / "a.mp4").write_bytes(b"v")
            (folder / "b.strm").write_text("x", encoding="utf-8")
            (folder / "c.txt").write_text("n", encoding="utf-8")
            cfg = {"scraper": {"video_extensions": [".mp4", ".strm"]}}
            self.assertEqual(count_direct_videos_on_disk(str(folder), config=cfg), 2)

    def test_check_fail_closed_on_db_exception(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                folder = Path(td) / "F"
                folder.mkdir()
                (folder / "a.mp4").write_bytes(b"v")
                repo = VideoRepository(db_path)
                prefix = to_file_uri(str(folder)) + "/"
                with mock.patch.object(
                    repo, "count_videos_in_folder", side_effect=OSError("db")
                ):
                    is_multi, effective, err = check_multi_video_folder(repo, prefix)
                self.assertTrue(is_multi)
                self.assertEqual(effective, -1)
                self.assertEqual(err, "db_count_failed")


class SingleFetchSamplesRouteTests(unittest.TestCase):
    """POST /api/scraper/fetch-samples must share ops10 batch safety rules."""

    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from web.routers import scraper as scraper_mod

        self.scraper_mod = scraper_mod
        self.app = FastAPI()
        self.app.include_router(scraper_mod.router)
        self.client = TestClient(self.app)

    def test_forged_number_not_passed_to_scraper(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "REAL-001")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="REAL-001", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                seen = []

                def _fake_fetch(file_path, number, proxy_url="", db_path=None):
                    seen.append(number)
                    from core.enricher import EnrichResult

                    return EnrichResult(
                        success=True,
                        nfo_written=False,
                        cover_written=False,
                        extrafanart_written=1,
                        fields_filled=[],
                        source_used="javbus",
                        error=None,
                    )

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "web.routers.scraper.fetch_samples_only",
                            side_effect=_fake_fetch,
                        ):
                            resp = self.client.post(
                                "/api/scraper/fetch-samples",
                                json={
                                    "file_path": path_uri,
                                    "number": "FORGED-999",
                                },
                            )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertEqual(data["extrafanart_written"], 1)
                self.assertEqual(seen, ["REAL-001"])

    def test_outside_library_rejects_without_network(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td_in:
                with tempfile.TemporaryDirectory() as td_out:
                    vp = _touch_video(Path(td_out) / "OUT")
                    path_uri = to_file_uri(str(vp))
                    repo = VideoRepository(db_path)
                    repo.upsert(Video(path=path_uri, number="OUT-1", sample_images=[]))
                    called = {"n": 0}

                    def _boom(*_a, **_k):
                        called["n"] += 1
                        raise AssertionError("must not network")

                    with mock.patch.object(
                        self.scraper_mod, "get_db_path", return_value=db_path
                    ):
                        with mock.patch.object(
                            self.scraper_mod,
                            "_configured_gallery_dir_uris",
                            return_value=[to_file_uri(td_in) + "/"],
                        ):
                            with mock.patch(
                                "web.routers.scraper.fetch_samples_only",
                                side_effect=_boom,
                            ):
                                resp = self.client.post(
                                    "/api/scraper/fetch-samples",
                                    json={"file_path": path_uri, "number": "OUT-1"},
                                )
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    self.assertFalse(data["success"])
                    self.assertIn("extrafanart_written", data)
                    self.assertEqual(data["extrafanart_written"], 0)
                    self.assertEqual(called["n"], 0)

    def test_not_in_db_rejects_without_network(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "GHOST")
                path_uri = to_file_uri(str(vp))
                dir_uri = to_file_uri(td) + "/"
                called = {"n": 0}

                def _boom(*_a, **_k):
                    called["n"] += 1
                    raise AssertionError("must not network")

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "web.routers.scraper.fetch_samples_only",
                            side_effect=_boom,
                        ):
                            resp = self.client.post(
                                "/api/scraper/fetch-samples",
                                json={"file_path": path_uri, "number": "GHOST-1"},
                            )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertFalse(data["success"])
                self.assertEqual(data["extrafanart_written"], 0)
                self.assertEqual(called["n"], 0)

    def test_existing_stills_skip_network(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "DONE"
                vp = _touch_video(root)
                _write_img(root / "extrafanart" / "fanart1.jpg")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="DONE-1", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"
                called = {"n": 0}

                def _boom(*_a, **_k):
                    called["n"] += 1
                    raise AssertionError("must not network")

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "web.routers.scraper.fetch_samples_only",
                            side_effect=_boom,
                        ):
                            resp = self.client.post(
                                "/api/scraper/fetch-samples",
                                json={"file_path": path_uri, "number": "DONE-1"},
                            )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertEqual(data["extrafanart_written"], 0)
                self.assertEqual(called["n"], 0)

    def test_malformed_path_returns_json_not_500(self) -> None:
        with TempDb() as db_path:
            with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                with mock.patch.object(
                    self.scraper_mod,
                    "_configured_gallery_dir_uris",
                    return_value=["file:///D:/Videos/"],
                ):
                    resp = self.client.post(
                        "/api/scraper/fetch-samples",
                        json={"file_path": "not a uri\x00bad", "number": "X"},
                    )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertFalse(data["success"])
            self.assertIn("error", data)
            self.assertIn("extrafanart_written", data)

    def test_success_happy_path(self) -> None:
        with TempDb() as db_path:
            with tempfile.TemporaryDirectory() as td:
                vp = _touch_video(Path(td) / "OK-1")
                path_uri = to_file_uri(str(vp))
                repo = VideoRepository(db_path)
                repo.upsert(Video(path=path_uri, number="OK-1", sample_images=[]))
                dir_uri = to_file_uri(td) + "/"

                def _fake_fetch(file_path, number, proxy_url="", db_path=None):
                    from core.enricher import EnrichResult

                    return EnrichResult(
                        success=True,
                        nfo_written=False,
                        cover_written=False,
                        extrafanart_written=2,
                        fields_filled=[],
                        source_used="javbus",
                        error=None,
                    )

                with mock.patch.object(self.scraper_mod, "get_db_path", return_value=db_path):
                    with mock.patch.object(
                        self.scraper_mod,
                        "_configured_gallery_dir_uris",
                        return_value=[dir_uri],
                    ):
                        with mock.patch(
                            "web.routers.scraper.fetch_samples_only",
                            side_effect=_fake_fetch,
                        ):
                            resp = self.client.post(
                                "/api/scraper/fetch-samples",
                                json={"file_path": path_uri, "number": "OK-1"},
                            )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["success"])
                self.assertIsNone(data.get("error"))
                self.assertEqual(data["extrafanart_written"], 2)


class AtomicDownloadImageTests(unittest.TestCase):
    def test_write_failure_leaves_no_final_or_temp(self) -> None:
        from core.organizer import download_image

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cover.jpg"
            # Pretend HTTP returns a valid large JPEG payload
            jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 1200
            fake_resp = mock.Mock()
            fake_resp.status_code = 200
            fake_resp.content = jpeg
            fake_resp.headers = {"Content-Type": "image/jpeg"}

            with mock.patch("core.organizer.requests.get", return_value=fake_resp):
                with mock.patch(
                    "core.organizer.os.replace", side_effect=OSError("disk full")
                ):
                    ok = download_image("https://example.com/a.jpg", str(dest))
            self.assertFalse(ok)
            self.assertFalse(dest.exists())
            leftovers = list(Path(td).glob("*.tmp")) + list(Path(td).glob(".*"))
            self.assertEqual(leftovers, [])

    def test_does_not_overwrite_existing_on_failed_write(self) -> None:
        from core.organizer import download_image

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cover.jpg"
            original = b"\xff\xd8\xff\xe0" + b"OLD" * 100
            dest.write_bytes(original)
            jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 1200
            fake_resp = mock.Mock()
            fake_resp.status_code = 200
            fake_resp.content = jpeg
            fake_resp.headers = {"Content-Type": "image/jpeg"}

            with mock.patch("core.organizer.requests.get", return_value=fake_resp):
                with mock.patch(
                    "core.organizer.os.replace", side_effect=OSError("nope")
                ):
                    ok = download_image("https://example.com/a.jpg", str(dest))
            self.assertFalse(ok)
            self.assertEqual(dest.read_bytes(), original)

    def test_rejects_non_image_payload(self) -> None:
        from core.organizer import download_image

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cover.jpg"
            fake_resp = mock.Mock()
            fake_resp.status_code = 200
            fake_resp.content = b"<html>not image</html>" + b"x" * 1200
            fake_resp.headers = {"Content-Type": "image/jpeg"}  # spoofed

            with mock.patch("core.organizer.requests.get", return_value=fake_resp):
                ok = download_image("https://example.com/a.jpg", str(dest))
            self.assertFalse(ok)
            self.assertFalse(dest.exists())

    def test_success_atomic_write(self) -> None:
        from core.organizer import download_image
        from core.sample_images import is_valid_local_image_file

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "cover.jpg"
            jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 1200
            fake_resp = mock.Mock()
            fake_resp.status_code = 200
            fake_resp.content = jpeg
            fake_resp.headers = {"Content-Type": "image/jpeg"}

            with mock.patch("core.organizer.requests.get", return_value=fake_resp):
                ok = download_image("https://example.com/a.jpg", str(dest))
            self.assertTrue(ok)
            self.assertTrue(is_valid_local_image_file(dest))
            self.assertEqual(list(Path(td).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
