"""Actress display-name resolution and translation protection."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Iterable

from core.database import AliasRepository
from core.logger import get_logger


logger = get_logger(__name__)
CURATED_NAMES_PATH = Path(__file__).resolve().parents[1] / "data" / "actress_names_zh_CN.json"


def seed_curated_actress_names(db_path: Path | None = None) -> dict[str, int]:
    """Merge bundled common Chinese names without replacing user primaries."""

    try:
        records = json.loads(CURATED_NAMES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not load curated actress-name data")
        return {"updated": 0, "skipped": 0}

    repository = AliasRepository(db_path)
    updated = 0
    skipped = 0
    for record in records:
        primary = str(record.get("primary_name", "")).strip()
        aliases = list(dict.fromkeys(
            str(alias).strip()
            for alias in record.get("aliases", [])
            if str(alias).strip() and str(alias).strip() != primary
        ))
        if not primary or not aliases:
            skipped += 1
            continue
        existing = repository.get_by_primary(primary)
        missing = aliases if existing is None else [
            alias for alias in aliases if alias not in existing.aliases
        ]
        if not missing:
            continue
        source = existing.source if existing is not None else "curated_common_zh"
        result = repository.sync_from_favorite(primary, missing, source=source)
        updated += 1
        skipped += len(result.get("skipped_aliases", []))
    return {"updated": updated, "skipped": skipped}


def load_actress_alias_groups() -> list[tuple[str, list[str]]]:
    """Return common display names with every known spelling in the group."""

    groups: list[tuple[str, list[str]]] = []
    try:
        for record in AliasRepository().get_all():
            names = list(
                dict.fromkeys(
                    name.strip()
                    for name in [record.primary_name, *(record.aliases or [])]
                    if name and name.strip()
                )
            )
            if names:
                groups.append((record.primary_name.strip(), names))
    except Exception:
        logger.exception("Could not load actress aliases")
    return groups


def build_actress_display_map(
    groups: Iterable[tuple[str, list[str]]] | None = None,
) -> dict[str, str]:
    """Map every exact and case-folded alias to its configured primary name."""

    result: dict[str, str] = {}
    for primary, names in groups if groups is not None else load_actress_alias_groups():
        for name in names:
            result[name] = primary
            result[name.casefold()] = primary
    return result


def display_actress_name(name: str, display_map: dict[str, str]) -> str:
    value = (name or "").strip()
    return display_map.get(value) or display_map.get(value.casefold()) or value


def normalize_actress_names(text: str, display_map: dict[str, str]) -> str:
    """Replace known aliases in existing titles with their configured primary names."""

    if not text or not display_map:
        return text or ""

    names = sorted(
        {name for name, primary in display_map.items() if name and primary},
        key=len,
        reverse=True,
    )
    pattern = re.compile("|".join(re.escape(name) for name in names))
    return pattern.sub(lambda match: display_map[match.group(0)], text)


def protect_actress_names(
    text: str,
    actors: Iterable[str] | None = None,
    *,
    groups: Iterable[tuple[str, list[str]]] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Replace known actress names with stable tokens before model translation."""

    if not text:
        return text, []

    alias_groups = list(groups) if groups is not None else load_actress_alias_groups()
    actor_set = {
        actor.strip()
        for actor in (actors or [])
        if actor and actor.strip()
    }
    candidates: list[tuple[str, str]] = []
    for display_name, names in alias_groups:
        group_matches_context = bool(actor_set.intersection(names) or display_name in actor_set)
        for name in names:
            if name in text or group_matches_context:
                candidates.append((name, display_name))

    display_map = build_actress_display_map(alias_groups)
    for actor in actor_set:
        if actor in text:
            candidates.append((actor, display_actress_name(actor, display_map)))

    protected = text
    replacements: list[tuple[str, str]] = []
    used_names: set[str] = set()
    for source_name, display_name in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
        if source_name in used_names or source_name not in protected:
            continue
        token = f"ZXACT{len(replacements) + 1:03d}ZX"
        protected = protected.replace(source_name, token)
        replacements.append((token, display_name))
        used_names.add(source_name)
    return protected, replacements


def restore_actress_names(text: str, replacements: list[tuple[str, str]]) -> str:
    """Restore model-mutated tokens and never silently lose a known name."""

    if not text or not replacements:
        return text

    restored = text
    restored_names: list[str] = []
    for token, display_name in replacements:
        restored_names.append(display_name)
        token_number = int(token.removeprefix("ZXACT").removesuffix("ZX"))
        pattern = re.compile(
            rf"Z[\s_-]*X[\s_-]*ACT[\s_-]*0*{token_number}[\s_-]*Z[\s_-]*X",
            re.IGNORECASE,
        )
        restored = pattern.sub(lambda _match, value=display_name: value, restored)

    for display_name in dict.fromkeys(restored_names):
        if display_name and display_name not in restored:
            restored = f"{restored} {display_name}".strip()

    return re.sub(
        r"Z[\s_-]*X[\s_-]*ACT[^\s]{0,12}",
        "",
        restored,
        flags=re.IGNORECASE,
    ).strip()
