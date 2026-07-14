"""Shared HTTP headers for remote image fetches (proxy + cover download).

Source-specific Referer / Accept / UA reduce 403 anti-hotlink failures from
JavBus, DMM, JavDB CDN, and actress photo hosts.
"""
from __future__ import annotations

from urllib.parse import urlparse

FULL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_ACCEPT = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def referer_for_image_url(url: str) -> str:
    """Return best-effort Referer for a remote image URL."""
    h = _host(url)
    if not h:
        return ""
    if "javbus.com" in h:
        return "https://www.javbus.com/"
    if "dmm.co.jp" in h:
        return "https://www.dmm.co.jp/"
    if "jav321.com" in h:
        return "https://www.jav321.com/"
    if "jdbstatic.com" in h or h.endswith("javdb.com") or h == "javdb.com":
        return "https://javdb.com/"
    if "heyzo.com" in h:
        return "https://www.heyzo.com/"
    if "1pondo.tv" in h:
        return "https://www.1pondo.tv/"
    if "caribbeancom.com" in h:
        return "https://www.caribbeancom.com/"
    if "10musume.com" in h:
        return "https://www.10musume.com/"
    if "fc2.com" in h:
        return "https://adult.contents.fc2.com/"
    if "graphis" in h:
        return "https://www.graphis.ne.jp/"
    if "minnano-av.com" in h:
        return "https://www.minnano-av.com/"
    if "avsox" in h or h == "file.netcdn.space":
        return "https://avsox.click/"
    if "wikimedia.org" in h:
        return "https://commons.wikimedia.org/"
    if "jsdelivr.net" in h:
        return "https://cdn.jsdelivr.net/"
    return ""


def headers_for_image_url(url: str, extra_referer: str = "") -> dict[str, str]:
    """Build request headers for fetching a remote image."""
    referer = (extra_referer or "").strip() or referer_for_image_url(url)
    headers = {
        "User-Agent": FULL_UA,
        "Accept": _ACCEPT,
        "Accept-Language": "ja-JP,ja;q=0.9,zh-TW;q=0.8,zh;q=0.7,en;q=0.6",
    }
    if referer:
        headers["Referer"] = referer
    return headers
