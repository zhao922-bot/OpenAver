"""core.database.alias — AliasRecord 與 AliasRepository（女優別名，spec-87 子模組）。"""
import json
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from datetime import datetime

from ._alias_base import _AliasRepositoryBase


@dataclass
class AliasRecord:
    """新版女優別名資料模型（平坦 group schema）"""
    primary_name: str = ""
    aliases: List[str] = field(default_factory=list)  # JSON array
    source: str = "manual"  # 'manual' | 'auto'
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """轉為字典（JSON 欄位序列化）"""
        data = asdict(self)
        data["aliases"] = json.dumps(self.aliases, ensure_ascii=False)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_row(cls, row: tuple, columns: List[str]) -> "AliasRecord":
        """從資料庫 row 建立"""
        data = dict(zip(columns, row, strict=True))
        if "aliases" in data and data["aliases"]:
            try:
                data["aliases"] = json.loads(data["aliases"])
            except json.JSONDecodeError:
                data["aliases"] = []
        else:
            data["aliases"] = []
        if "created_at" in data and data["created_at"]:
            if isinstance(data["created_at"], str):
                data["created_at"] = datetime.fromisoformat(data["created_at"])
        if "updated_at" in data and data["updated_at"]:
            if isinstance(data["updated_at"], str):
                data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        return cls(**data)


class AliasRepository(_AliasRepositoryBase[AliasRecord]):
    """新版女優別名資料存取層（平坦 group schema）"""

    _table = "actress_aliases"
    _sql_alias = "aa"
    _record_cls = AliasRecord

    def promote_singleton_primary(
        self,
        old_primary: str,
        new_primary: str,
        source: str = "curated_common_zh",
    ) -> bool:
        """Promote or merge an empty one-name group atomically.

        A group with aliases is user-maintained and must not be rewritten.  The
        old primary becomes an alias so existing NFO names keep resolving.  A
        pre-existing target is supported to repair partially completed seeds.
        """
        old_primary = old_primary.strip()
        new_primary = new_primary.strip()
        if not old_primary or not new_primary or old_primary == new_primary:
            return False

        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN EXCLUSIVE")
            cursor.execute(
                "SELECT aliases FROM actress_aliases WHERE primary_name = ?",
                (old_primary,),
            )
            row = cursor.fetchone()
            if row is None:
                conn.rollback()
                return False
            try:
                existing_aliases = json.loads(row[0] or "[]")
            except json.JSONDecodeError:
                conn.rollback()
                return False
            if existing_aliases:
                conn.rollback()
                return False

            cursor.execute(
                "SELECT aliases FROM actress_aliases WHERE primary_name = ?",
                (new_primary,),
            )
            target_row = cursor.fetchone()
            if target_row is not None:
                try:
                    target_aliases = json.loads(target_row[0] or "[]")
                except json.JSONDecodeError:
                    conn.rollback()
                    return False
                cursor.execute(
                    "DELETE FROM actress_aliases WHERE primary_name = ?",
                    (old_primary,),
                )
                if old_primary not in target_aliases:
                    target_aliases.append(old_primary)
                cursor.execute(
                    """UPDATE actress_aliases
                       SET aliases = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE primary_name = ?""",
                    (json.dumps(target_aliases, ensure_ascii=False), new_primary),
                )
                conn.commit()
                return True

            available, _ = self._check_global_uniqueness_cursor(
                cursor, new_primary, exclude_primary=old_primary
            )
            if not available:
                conn.rollback()
                return False

            cursor.execute(
                """UPDATE actress_aliases
                   SET primary_name = ?, aliases = ?, source = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE primary_name = ?""",
                (
                    new_primary,
                    json.dumps([old_primary], ensure_ascii=False),
                    source,
                    old_primary,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sync_from_favorite(
        self, name: str, aliases: List[str], source: str = "auto"
    ) -> dict:
        """
        從 favorite 同步 alias group（resolve-first，CD-6）。

        Returns:
            {"primary_name": str, "skipped_aliases": list[str]}
        """
        # resolve name → 找到所屬 group (若有)
        resolved = self.resolve(name)
        target_record: Optional[AliasRecord] = None

        if len(resolved) > 1 or (len(resolved) == 1 and name not in resolved):
            # name 解析到某個 group
            primary_in_resolved = next(
                (n for n in resolved if self.get_by_primary(n) is not None), None
            )
            if primary_in_resolved:
                target_record = self.get_by_primary(primary_in_resolved)
        else:
            target_record = self.get_by_primary(name)

        target_primary = target_record.primary_name if target_record else name

        # §46 guard: 無既有記錄 + 輸入 aliases 為空 → 不建空記錄
        if target_record is None and not aliases:
            return {"primary_name": target_primary, "skipped_aliases": []}

        conn = self._get_connection()
        cursor = conn.cursor()
        skipped: List[str] = []
        try:
            cursor.execute("BEGIN EXCLUSIVE")

            # 逐一檢查 incoming aliases
            merged_aliases: List[str] = list(target_record.aliases) if target_record else []
            for alias in aliases:
                if alias == target_primary or alias in merged_aliases:
                    continue
                ok, _ = self._check_global_uniqueness_cursor(
                    cursor, alias, exclude_primary=target_primary
                )
                if not ok:
                    skipped.append(alias)
                else:
                    merged_aliases.append(alias)

            if target_record is None and not merged_aliases:
                conn.rollback()
                return {"primary_name": target_primary, "skipped_aliases": skipped}

            aliases_json = json.dumps(merged_aliases, ensure_ascii=False)
            if target_record is None:
                cursor.execute(
                    """INSERT INTO actress_aliases (primary_name, aliases, source)
                       VALUES (?, ?, ?)""",
                    (target_primary, aliases_json, source),
                )
            else:
                cursor.execute(
                    """UPDATE actress_aliases
                       SET aliases = ?, source = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE primary_name = ?""",
                    (aliases_json, source, target_primary),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {"primary_name": target_primary, "skipped_aliases": skipped}
