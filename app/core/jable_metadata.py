"""Read public title metadata from Jable without inspecting media resources."""

from __future__ import annotations

import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from core.cf_transport import CfChallengeRequired, CfTransportUnavailable, get_cf_transport


JABLE_ORIGIN = "https://jable.tv/"


def _clean_title(value: str, number: str) -> str:
    text = " ".join((value or "").split())
    text = re.sub(rf"(?i)^\s*{re.escape(number)}\s*[-_:/|\u2013\u2014]*\s*", "", text)
    return text.strip()


def _language(value: str) -> str:
    if re.search(r"[\u3040-\u30ff]", value):
        return "ja"
    if re.search(r"[\u3400-\u9fff]", value):
        return "zh"
    return "other"


def parse_jable_titles(html: str, number: str, search_url: str) -> dict:
    """Extract title candidates only; media tags and scripts are deliberately ignored."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    lowered = page_title.lower()
    if "just a moment" in lowered or soup.select_one("#challenge-form, .cf-challenge"):
        raise CfChallengeRequired("Jable Cloudflare challenge is still active")

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    number_upper = number.upper()

    for anchor in soup.select('a[href*="/videos/"]'):
        href = anchor.get("href", "")
        card = anchor.find_parent(["article", "div", "li"])
        title_node = card.select_one(".title, h4, h5, h6") if card else None
        raw = (title_node or anchor).get_text(" ", strip=True)
        if number_upper not in raw.upper():
            title_attr = anchor.get("title", "")
            if number_upper not in title_attr.upper():
                continue
            raw = title_attr
        title = _clean_title(raw, number)
        if not title:
            continue
        if href.startswith("/"):
            href = JABLE_ORIGIN.rstrip("/") + href
        key = (title.casefold(), href)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"title": title, "language": _language(title), "url": href})

    if not candidates:
        raw = ""
        for selector in ("h4", "h1", 'meta[property="og:title"]'):
            node = soup.select_one(selector)
            if node:
                raw = node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True)
                if number_upper in raw.upper():
                    break
        title = _clean_title(raw, number)
        if title:
            candidates.append({"title": title, "language": _language(title), "url": search_url})

    chinese = next((item["title"] for item in candidates if item["language"] == "zh"), "")
    japanese = next((item["title"] for item in candidates if item["language"] == "ja"), "")
    return {
        "number": number,
        "search_url": search_url,
        "title_zh": chinese,
        "title_ja": japanese,
        "candidates": candidates[:12],
    }


def lookup_jable_titles(number: str) -> dict:
    """Fetch a public search page through the app's user-visible CF transport."""
    transport = get_cf_transport()
    if transport is None:
        raise CfTransportUnavailable("Jable metadata lookup requires the desktop application")
    search_url = f"{JABLE_ORIGIN}search/{quote(number)}/"
    html = transport.fetch(search_url, cache_key="jable")
    return parse_jable_titles(html, number, search_url)
