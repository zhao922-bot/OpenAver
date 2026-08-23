"""Unit tests for core/atomic_write.py::atomic_write (TASK-113d-T1).

New file per TASK-113d-T1 DoD's 機械自證: this commit's diff must not touch
any existing tests/ file. Everything new for T1 — the primitive's own
six-cell matrix and the `thumbnail_cache.generate()` call-site regression
lock that plugs the "no os.replace failure coverage" gap — lives here.
"""
import errno
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.atomic_write import atomic_replace_file, atomic_write


class TestAtomicReplaceFile:
    def test_replaces_destination_with_completed_source(self, tmp_path):
        source = tmp_path / "download.part"
        destination = tmp_path / "video.mp4"
        source.write_bytes(b"complete-video")
        destination.write_bytes(b"old-video")

        atomic_replace_file(source, destination)

        assert not source.exists()
        assert destination.read_bytes() == b"complete-video"

    def test_replace_failure_preserves_source_and_destination(self, tmp_path):
        source = tmp_path / "download.part"
        destination = tmp_path / "video.mp4"
        source.write_bytes(b"complete-video")
        destination.write_bytes(b"old-video")

        with patch("core.atomic_write.os.replace", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError, match="denied"):
                atomic_replace_file(source, destination)

        assert source.read_bytes() == b"complete-video"
        assert destination.read_bytes() == b"old-video"


class TestAtomicWritePrimitive:
    """Six-cell matrix (TASK-113d-T1 測試矩陣)."""

    def test_normal_write_leaves_no_temp_leftover(self, tmp_path):
        dest = tmp_path / "out.txt"

        with atomic_write(dest, mode="w") as f:
            f.write("hello")

        assert dest.read_text() == "hello"
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    def test_exception_inside_block_propagates_and_leaves_dest_untouched(self, tmp_path):
        dest = tmp_path / "out.txt"

        with pytest.raises(RuntimeError, match="boom"):
            with atomic_write(dest, mode="w") as f:
                f.write("partial")
                raise RuntimeError("boom")

        assert not dest.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_exception_inside_block_does_not_clobber_existing_dest(self, tmp_path):
        dest = tmp_path / "out.txt"
        dest.write_text("original")

        with pytest.raises(RuntimeError, match="boom"):
            with atomic_write(dest, mode="w") as f:
                f.write("partial")
                raise RuntimeError("boom")

        assert dest.read_text() == "original"
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_base_exception_inside_block_cleans_temp(self, tmp_path, exc_type):
        """Ctrl-C / sys.exit() mid-write must not leave a temp file behind.

        `KeyboardInterrupt` and `SystemExit` derive from `BaseException`, not
        `Exception`, so a cleanup branch that catches only `Exception` lets the
        `mkstemp` file survive. This is not hypothetical bookkeeping: T2 deleted
        `core.actress_photo.download_actress_photo`'s `finally: tmp.unlink()`
        (which *did* run for `BaseException`) in favour of this primitive, and
        the surviving temp's random `mkstemp` name does **not** match that
        function's `{safe_name}.*` sibling-cleanup glob — so nothing would ever
        remove it. Found by Stage 2 pre-merge review (codex 5.6-terra, P2).
        """
        dest = tmp_path / "out.txt"
        dest.write_text("original")

        with pytest.raises(exc_type):
            with atomic_write(dest, mode="w") as f:
                f.write("partial")
                raise exc_type()

        assert dest.read_text() == "original"
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    @pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
    def test_base_exception_from_os_replace_cleans_temp(self, tmp_path, exc_type):
        """Same contract on the second failure path (`os.replace` itself)."""
        dest = tmp_path / "out.txt"

        with patch("core.atomic_write.os.replace", side_effect=exc_type()):
            with pytest.raises(exc_type):
                with atomic_write(dest, mode="w") as f:
                    f.write("data")

        assert not dest.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_os_replace_failure_propagates_and_cleans_temp(self, tmp_path):
        dest = tmp_path / "out.txt"

        with patch("core.atomic_write.os.replace", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                with atomic_write(dest, mode="w") as f:
                    f.write("data")

        assert not dest.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_os_replace_failure_does_not_clobber_existing_dest(self, tmp_path):
        dest = tmp_path / "out.txt"
        dest.write_text("original")

        with patch("core.atomic_write.os.replace", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                with atomic_write(dest, mode="w") as f:
                    f.write("data")

        assert dest.read_text() == "original"
        leftovers = [p for p in tmp_path.iterdir() if p != dest]
        assert leftovers == []

    def test_text_mode_with_encoding_roundtrips_non_ascii(self, tmp_path):
        dest = tmp_path / "out.json"
        content = '{"名前": "テスト", "數字": 123}'

        with atomic_write(dest, mode="w", encoding="utf-8") as f:
            f.write(content)

        assert dest.read_text(encoding="utf-8") == content

    def test_temp_file_created_in_same_dir_as_dest(self, tmp_path):
        dest = tmp_path / "sub" / "out.txt"
        dest.parent.mkdir()
        captured = {}
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, tmp = real_mkstemp(*args, **kwargs)
            captured["tmp"] = tmp
            return fd, tmp

        with patch("core.atomic_write.tempfile.mkstemp", side_effect=spy_mkstemp):
            with atomic_write(dest, mode="w") as f:
                f.write("x")

        assert Path(captured["tmp"]).parent == dest.parent

    def test_fd_closed_before_os_replace_is_called(self, tmp_path):
        dest = tmp_path / "out.txt"
        call_order = []
        real_fdopen = os.fdopen
        real_replace = os.replace

        def spy_fdopen(fd, *args, **kwargs):
            fh = real_fdopen(fd, *args, **kwargs)
            real_close = fh.close

            def spy_close():
                call_order.append("fdopen_close")
                real_close()

            fh.close = spy_close
            return fh

        def spy_replace(src, dst, *args, **kwargs):
            call_order.append("os_replace")
            return real_replace(src, dst, *args, **kwargs)

        with patch("core.atomic_write.os.fdopen", side_effect=spy_fdopen), \
             patch("core.atomic_write.os.replace", side_effect=spy_replace):
            with atomic_write(dest, mode="w") as f:
                f.write("x")

        assert call_order == ["fdopen_close", "os_replace"]

    def test_fdopen_failure_closes_fd_and_leaves_no_temp_leftover(self, tmp_path):
        """Codex PR review P2：mkstemp 回傳的 fd 在 `os.fdopen()` 接管它之前完全
        由 `atomic_write` 擁有——若呼叫端傳了無效的 mode/encoding 組合
        （`mode="wb"` 又給 `encoding="utf-8"`，真實會被 `ValueError` 拒絕），
        `os.fdopen` 在接管前就拋例外，外層 `with` 從未成立，fd 不會被 with 關閉。
        斷言必須是「真的關了」——`os.fstat(fd)` 拋 `OSError`/`EBADF`——不是只斷言
        「有拋例外」，否則測不出 fd 洩漏（實測見本卡回報：修正前 fd 在例外後仍可
        `os.fstat` 成功）。"""
        dest = tmp_path / "out.txt"
        captured = {}

        def bad_fdopen(fd, *args, **kwargs):
            captured["fd"] = fd
            raise ValueError("binary mode doesn't take an encoding argument")

        with patch("core.atomic_write.os.fdopen", side_effect=bad_fdopen):
            with pytest.raises(ValueError):
                with atomic_write(dest, mode="wb", encoding="utf-8"):
                    pass  # pragma: no cover — fdopen 失敗於 with 成立前，永遠不會進來

        assert "fd" in captured, "os.fdopen 從未被呼叫，測試沒有測到目標路徑"
        with pytest.raises(OSError) as excinfo:
            os.fstat(captured["fd"])
        assert excinfo.value.errno == errno.EBADF

        assert not dest.exists()
        leftovers = list(tmp_path.iterdir())
        assert leftovers == []

    def test_suffix_param_used_for_temp_file(self, tmp_path):
        dest = tmp_path / "out.jpg"
        captured = {}
        real_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            fd, tmp = real_mkstemp(*args, **kwargs)
            captured["tmp"] = tmp
            return fd, tmp

        with patch("core.atomic_write.tempfile.mkstemp", side_effect=spy_mkstemp):
            with atomic_write(dest, suffix=".jpg") as f:
                f.write(b"x")

        assert captured["tmp"].endswith(".jpg")


class TestThumbnailCacheGenerateAtomicWriteFailure:
    """補網（測試矩陣 #7）：thumbnail_cache.generate() 對 os.replace 失敗的回歸鎖。

    plan/task card 已核對現有 tests/unit/test_thumbnail_cache.py 19 支測試
    零命中 os.replace/PermissionError/OSError 故障注入——這是唯一一支補上
    這個缺口的測試，刻意放在本檔（機械自證：不得補進既有測試檔）。
    """

    def test_generate_returns_false_on_os_replace_failure_and_cleans_temp(self, tmp_path, monkeypatch):
        import core.thumbnail_cache as tc
        from PIL import Image

        thumb_dir = tmp_path / "thumb"
        monkeypatch.setattr(tc, "_thumb_dir", lambda: thumb_dir)

        cover = tmp_path / "cover.jpg"
        Image.new("RGB", (100, 100), (10, 20, 30)).save(str(cover), "JPEG")

        dst = thumb_dir / "ab" / "abc.webp"

        with patch("core.atomic_write.os.replace", side_effect=OSError("disk full")):
            result = tc.generate(str(cover), dst)

        assert result is False
        assert not dst.exists()
        # dst.parent 由 generate() 內的 mkdir 建立（無條件斷言：用 if 包住會在
        # 目錄不存在時靜默跳過整條檢查，正是本 branch 在抓的假綠形狀）
        assert dst.parent.exists()
        assert list(dst.parent.iterdir()) == []
