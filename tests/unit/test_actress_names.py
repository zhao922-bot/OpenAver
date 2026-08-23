import core.actress_names as actress_names
from core.database import init_db


GROUPS = [("梓光莉", ["梓光莉", "梓ヒカリ", "梓光"])]


def test_translation_protection_uses_common_primary_name():
    protected, replacements = actress_names.protect_actress_names(
        "梓ヒカリと過ごす休日",
        ["梓ヒカリ"],
        groups=GROUPS,
    )

    assert protected == "ZXACT001ZXと過ごす休日"
    assert replacements == [("ZXACT001ZX", "梓光莉")]
    assert actress_names.restore_actress_names("ZX ACT 001 ZX的假日", replacements) == "梓光莉的假日"


def test_dropped_translation_token_keeps_the_known_name():
    restored = actress_names.restore_actress_names(
        "共同度过的假日",
        [("ZXACT001ZX", "梓光莉")],
    )
    assert restored == "共同度过的假日 梓光莉"


def test_longest_alias_is_protected_first():
    protected, replacements = actress_names.protect_actress_names(
        "梓光莉与梓光",
        groups=GROUPS,
    )
    assert protected == "ZXACT001ZX与ZXACT002ZX"
    assert replacements == [
        ("ZXACT001ZX", "梓光莉"),
        ("ZXACT002ZX", "梓光莉"),
    ]


def test_display_map_resolves_exact_and_casefolded_aliases():
    display_map = actress_names.build_actress_display_map([("Common", ["Common", "ALIAS"])])
    assert actress_names.display_actress_name("alias", display_map) == "Common"


def test_existing_title_aliases_are_normalized_to_primary_names():
    display_map = actress_names.build_actress_display_map(GROUPS)

    assert actress_names.normalize_actress_names("标题 梓光", display_map) == "标题 梓光莉"
    assert actress_names.normalize_actress_names("梓光莉与梓光", display_map) == "梓光莉与梓光莉"


def test_curated_names_seed_is_idempotent_and_keeps_existing_source(tmp_path):
    db_path = tmp_path / "names.db"
    init_db(db_path)

    first = actress_names.seed_curated_actress_names(db_path)
    second = actress_names.seed_curated_actress_names(db_path)
    repository = actress_names.AliasRepository(db_path)
    record = repository.get_by_primary("梓光莉")

    assert first["updated"] > 0
    assert second["updated"] == 0
    assert record is not None
    assert {"梓ヒカリ", "梓光"}.issubset(record.aliases)
    assert record.source == "curated_common_zh"
