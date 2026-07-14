"""Actress alias review: unrecognized names, multi-Chinese groups, merge candidates."""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from core.database import AliasRepository, VideoRepository, init_db

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_HIRA_KATA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_cjk(s: str) -> bool:
    return bool(_CJK_RE.search(s or ""))


def _has_japanese_kana(s: str) -> bool:
    return bool(_HIRA_KATA_RE.search(s or ""))


def _normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip().lower()
    s = re.sub(r"[\s·・.．'’\-—_]+", "", s)
    return s


def _known_name_set(groups) -> set[str]:
    known: set[str] = set()
    for g in groups:
        known.add(g.primary_name)
        for a in g.aliases or []:
            known.add(a)
    return known


def review_actress_aliases(*, limit: int = 100) -> dict[str, Any]:
    """Build review queues for the ops actress-alias center."""
    init_db()
    groups = AliasRepository().get_all()
    known = _known_name_set(groups)
    known_norm = {_normalize_name(n): n for n in known}

    # usage counts from library
    usage: dict[str, int] = defaultdict(int)
    for video in VideoRepository().get_all():
        for name in video.actresses or []:
            name = (name or "").strip()
            if name:
                usage[name] += 1

    # 1) Unrecognized: appear in library but not in any alias group
    unrecognized = []
    for name, count in usage.items():
        if name not in known:
            unrecognized.append({
                "name": name,
                "usage_count": count,
                "has_cjk": _has_cjk(name),
                "has_kana": _has_japanese_kana(name),
                "normalized": _normalize_name(name),
            })
    unrecognized.sort(key=lambda x: (-x["usage_count"], x["name"]))

    # 2) Multi-Chinese: groups with ≥2 CJK names (primary+aliases)
    multi_chinese = []
    for g in groups:
        names = [g.primary_name, *(g.aliases or [])]
        cjk_names = [n for n in names if _has_cjk(n)]
        if len(cjk_names) >= 2:
            multi_chinese.append({
                "primary_name": g.primary_name,
                "aliases": g.aliases or [],
                "cjk_names": cjk_names,
                "usage_count": sum(usage.get(n, 0) for n in names),
                "source": g.source,
            })
    multi_chinese.sort(key=lambda x: (-x["usage_count"], x["primary_name"]))

    # group usage for ranking
    group_usage: dict[str, int] = {}
    primary_of_name: dict[str, str] = {}
    for g in groups:
        names = [g.primary_name, *(g.aliases or [])]
        group_usage[g.primary_name] = sum(usage.get(n, 0) for n in names)
        for n in names:
            primary_of_name[n] = g.primary_name
            primary_of_name[_normalize_name(n)] = g.primary_name

    # 3a normalized collisions between groups → merge candidates
    merge_candidates = []
    seen_pairs: set[tuple[str, str]] = set()
    norm_to_primaries: dict[str, list[str]] = defaultdict(list)
    for g in groups:
        for n in [g.primary_name, *(g.aliases or [])]:
            nn = _normalize_name(n)
            if nn and g.primary_name not in norm_to_primaries[nn]:
                norm_to_primaries[nn].append(g.primary_name)
    for nn, primaries in norm_to_primaries.items():
        uniq = sorted(set(primaries))
        if len(uniq) < 2:
            continue
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                # keep = higher usage
                if group_usage.get(b, 0) > group_usage.get(a, 0):
                    a, b = b, a
                pair = (a, b)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                merge_candidates.append({
                    "kind": "normalized_match",
                    "keep": a,
                    "absorb": b,
                    "reason": f"规范化后相同：{nn}",
                    "usage_keep": group_usage.get(a, 0),
                    "usage_absorb": group_usage.get(b, 0),
                })

    # 3b unrecognized names that normalize-match an existing group
    suggested_attach = []
    for item in unrecognized[: max(limit * 2, 50)]:
        nn = item["normalized"]
        if not nn:
            continue
        target_primary = primary_of_name.get(nn)
        if target_primary:
            suggested_attach.append({
                "name": item["name"],
                "usage_count": item["usage_count"],
                "attach_to": target_primary,
                "reason": "与已有别名规范化匹配",
            })

    # 3c mixed script groups
    mixed_script = []
    for g in groups:
        names = [g.primary_name, *(g.aliases or [])]
        has_kana = any(_has_japanese_kana(n) for n in names)
        has_cjk = any(_has_cjk(n) for n in names)
        has_latin = any(_LATIN_RE.search(n or "") and not _has_cjk(n) for n in names)
        if has_kana and has_cjk:
            mixed_script.append({
                "primary_name": g.primary_name,
                "aliases": g.aliases or [],
                "usage_count": group_usage.get(g.primary_name, 0),
                "reason": "同时含假名与汉字名，请确认中文是否正确",
            })
        elif has_latin and has_cjk and len(names) >= 3:
            mixed_script.append({
                "primary_name": g.primary_name,
                "aliases": g.aliases or [],
                "usage_count": group_usage.get(g.primary_name, 0),
                "reason": "罗马字与中文混用较多，请确认",
            })
    mixed_script.sort(key=lambda x: (-x["usage_count"], x["primary_name"]))

    return {
        "success": True,
        "summary": {
            "total_groups": len(groups),
            "total_unique_actresses_in_library": len(usage),
            "unrecognized": len(unrecognized),
            "multi_chinese_groups": len(multi_chinese),
            "merge_candidates": len(merge_candidates),
            "suggested_attach": len(suggested_attach),
            "mixed_script_groups": len(mixed_script),
        },
        "unrecognized": unrecognized[:limit],
        "multi_chinese": multi_chinese[:limit],
        "merge_candidates": merge_candidates[:limit],
        "suggested_attach": suggested_attach[:limit],
        "mixed_script": mixed_script[:limit],
    }
