from pathlib import Path

from core.logger import get_default_log_dir


def test_default_log_dir_preserves_legacy_location(monkeypatch):
    monkeypatch.delenv("OPENAVER_LOG_DIR", raising=False)

    assert get_default_log_dir() == Path.home() / "OpenAver" / "logs"


def test_default_log_dir_accepts_isolated_override(monkeypatch, tmp_path):
    target = tmp_path / "isolated-logs"
    monkeypatch.setenv("OPENAVER_LOG_DIR", str(target))

    assert get_default_log_dir() == target
