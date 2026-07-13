"""
core.metatube — Metatube HTTP federation package.

Intentionally empty: do NOT re-export MetatubeHttpClient here.
Reason: re-exporting creates an independent binding in __init__ that breaks
monkeypatch / unittest.mock patch targets in tests (patch silently misses).
Import directly from core.metatube.client / core.metatube.errors.
"""
