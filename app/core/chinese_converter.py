"""Deterministic Chinese script conversion used for imported metadata."""

from __future__ import annotations

from functools import lru_cache

from vendor.opencc import OpenCC


@lru_cache(maxsize=1)
def _taiwan_to_simplified_converter() -> OpenCC:
    # tw2sp applies both character and Taiwan phrase conversion.
    return OpenCC("tw2sp")


def traditional_to_simplified(value: str) -> str:
    """Convert Taiwan Traditional Chinese text to Simplified Chinese."""
    text = value or ""
    if not text:
        return ""
    return _taiwan_to_simplified_converter().convert(text)
