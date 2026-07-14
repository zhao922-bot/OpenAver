"""Shared test helpers for OpenAver unit tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class TempDb:
    """Context manager that redirects core.database.get_db_path to a temp file."""

    def __init__(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self.db_path: Path | None = None
        self._orig = None

    def __enter__(self) -> Path:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="openaver-test-")
        root = Path(self._tmpdir.name)
        self.db_path = root / "openaver.db"
        import core.database as database

        self._orig = database.get_db_path
        database.get_db_path = lambda: self.db_path  # type: ignore[assignment]
        # Also patch modules that may have imported get_db_path by name
        try:
            import core.image_proxy_cache as ipc
            self._ipc_orig = getattr(ipc, "get_db_path", None)
            ipc.get_db_path = lambda: self.db_path  # type: ignore[assignment]
        except Exception:
            self._ipc_orig = None
        try:
            import core.rename_journal as rj
            self._rj_orig = getattr(rj, "get_db_path", None)
            rj.get_db_path = lambda: self.db_path  # type: ignore[assignment]
        except Exception:
            self._rj_orig = None

        database.init_db(self.db_path)
        return self.db_path

    def __exit__(self, *exc) -> None:
        import core.database as database

        if self._orig is not None:
            database.get_db_path = self._orig  # type: ignore[assignment]
        if getattr(self, "_ipc_orig", None) is not None:
            import core.image_proxy_cache as ipc
            ipc.get_db_path = self._ipc_orig  # type: ignore[assignment]
        if getattr(self, "_rj_orig", None) is not None:
            import core.rename_journal as rj
            rj.get_db_path = self._rj_orig  # type: ignore[assignment]
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
