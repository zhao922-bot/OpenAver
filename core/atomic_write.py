"""atomic_write.py — 共用、無狀態的原子寫檔 primitive（CD-113d-1/2）。

收斂 core.config / core.thumbnail_cache / web.routers.actress 三處各自手寫的
「同目錄 mkstemp → 拿到開著的 fh → 關 fd → os.replace → 例外時清 temp」骨架
（core.actress_photo 的第四處在 T2 一併接進來——它原本用固定可預測的 temp 檔名，
換成本 primitive 的 mkstemp 才順帶修掉同一位女優並發下載互撞暫存檔，CD-113d-4）。
只做這一件事：鎖策略、成功後清舊 sibling 檔、失敗語意（拋例外 vs 回 False）、
dest 在哪，全部留給呼叫端決定（spec §2.5 / D-8）。
"""
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator, Optional, Union
import os          # 🔴 必須是屬性存取形式：禁 `from os import replace`
import tempfile    # 🔴 同上：禁 `from tempfile import mkstemp`


@contextmanager
def atomic_write(
    dest: Union[Path, str],
    *,
    mode: str = "wb",
    encoding: Optional[str] = None,
    suffix: str = ".tmp",
) -> Iterator[IO]:
    """Yield an open file handle whose content lands at `dest` atomically.

    Mechanics (the part every caller must share): create the temp file in
    `dest.parent` via `tempfile.mkstemp` — same directory means same volume,
    without which `os.replace` fails with `EXDEV` — hand the caller the open
    handle, close the fd, then `os.replace(tmp, dest)`. The fd is closed
    *before* the replace because Windows refuses to replace a file that is
    still open (`BE-ENV-01`).

    Two failure paths, both handled identically: the caller's block raises
    while writing, or `os.replace` itself fails (on Windows it is not
    guaranteed to succeed — antivirus and the thumbnail cache hold handles).
    Either way the temp file is removed, `dest` is left **byte-for-byte
    untouched**, and the original exception propagates unchanged.

    `dest.parent` must already exist — creating it is the caller's business,
    because whether a missing directory is an error or a routine first write
    differs per caller. `suffix` reaches `mkstemp` verbatim; pass the real
    extension when something downstream sniffs the temp name.

    Deliberately NOT here (spec-113 §2.5 — each stays with the caller):
    locking, deleting old sibling files after a successful write, turning
    failures into a `False` return, and deciding where `dest` is.
    """
    dest = Path(dest)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, suffix=suffix)
    tmp = Path(tmp)
    try:
        # 🔴 os.fdopen 自己包一層 try（Codex PR review P2）：mkstemp 回傳的 fd
        # 在這一行之前完全由這個函式擁有；若呼叫端傳了無效的 mode/encoding 組合
        # （例如 mode="wb" 又給 encoding="utf-8"），fdopen 會在「接管 fd」之前
        # 就拋 ValueError——此時外層的 `with` 從未成立，fd 不會被 with 關閉。
        # 若讓這個例外直接落進下面的 except，只會 unlink(tmp)，fd 仍開著：
        # 重複觸發會耗盡 fd（Windows 上開著的 handle 還會擋 unlink，temp 檔留底）。
        # 明確在這裡關閉 fd、原樣重拋，讓 fd 的生命週期不論哪條路徑都有人收尾。
        try:
            f = os.fdopen(fd, mode, encoding=encoding)
        except BaseException:
            os.close(fd)
            raise
        with f:
            yield f
        # fd 已由上面的 with 關閉 → 安全 replace（Windows file-lock 前提）
        os.replace(tmp, dest)
    except BaseException:
        # 路徑 1：區塊內使用者程式碼拋例外；路徑 2：os.replace 自己拋例外
        # （BE-ENV-01：Windows 防毒/縮圖快取會擋，不是必成功）。
        # 兩條路徑都落在這個 except，行為一致：清 temp、不動 dest、原例外往上傳。
        #
        # 🔴 必須是 BaseException 而非 Exception（Stage 2 pre-merge review P2）：
        # KeyboardInterrupt / SystemExit 不是 Exception 的子類。這不是形式主義——
        # T2 為了改用本 primitive，把 actress_photo.download_actress_photo 原本的
        # `finally: tmp.unlink()`（finally 對 BaseException 照樣執行）整段刪掉；
        # 只收 Exception 就等於在 Ctrl-C 這條路上比修改前更差，而且 mkstemp 的隨機
        # 檔名不被該函式的 `{safe_name}.*` 清舊檔 glob 命中 → 沒有任何程式碼會再碰它。
        # 裸 `raise` 原樣重拋，呼叫端的例外／回 False 語意完全不變。
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_replace_file(source: Union[Path, str], dest: Union[Path, str]) -> None:
    """Atomically move an already-complete file onto its final same-volume path."""
    os.replace(Path(source), Path(dest))
