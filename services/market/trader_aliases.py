"""游资席位别名匹配器（Phase M2）。

``dim_trader_alias`` 表替代旧的 ``config/trader_seat_aliases.json``，
提供「营业部全名 → 简称」映射，让 ``MarketSummary.dragon_tiger.famous_traders``
能直接显示「拉萨天团」「章盟主」等可读名字。

匹配策略（三级）::

    1. 精确匹配 exalter（主键命中，O(1)）
    2. 部分包含匹配（数据库 LIKE，O(n)）
    3. 仍未命中 → 返回 None，交给 AIEnricher 兜底（M2.5）

GUI 编辑能力（M3 阶段的 ``TraderAliasDialog`` 会用）::

    add() / update() / delete() / list_all() / list_famous()

线程安全：内部用 RLock + 本地缓存避免每次都扫表。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from services.market.market_db import MarketDB, get_market_db

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class TraderAlias:
    """``dim_trader_alias`` 一行。"""

    exalter: str           # 营业部全名（主键）
    alias: str             # 简称
    is_famous: bool = False
    notes: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "exalter": self.exalter,
            "alias": self.alias,
            "is_famous": bool(self.is_famous),
            "notes": self.notes,
            "updated_at": self.updated_at,
        }


@dataclass
class MatchResult:
    """匹配结果。

    Attributes:
        exalter: 原始 exalter（输入）
        alias: 匹配到的别名；None 表示未命中
        is_famous: 是否标记为知名游资
        source: ``exact`` / ``contains`` / ``none``
    """

    exalter: str
    alias: Optional[str]
    is_famous: bool
    source: str

    @property
    def matched(self) -> bool:
        return self.alias is not None


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class TraderAliasMatcher:
    """游资席位别名匹配器（封装 ``dim_trader_alias`` 表）。"""

    def __init__(self, db: Optional[MarketDB] = None) -> None:
        self.db = db or get_market_db()
        self._lock = threading.RLock()
        self._cache: Optional[List[TraderAlias]] = None

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    def reload(self) -> None:
        """清掉缓存，下次 match 会重新扫表。"""
        with self._lock:
            self._cache = None

    def _all(self) -> List[TraderAlias]:
        """带缓存的全表读取。"""
        with self._lock:
            if self._cache is None:
                self._cache = list(self._scan_db())
            return list(self._cache)

    def _scan_db(self) -> List[TraderAlias]:
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT exalter, alias, is_famous, notes, updated_at "
                "FROM dim_trader_alias "
                "ORDER BY is_famous DESC, length(exalter) DESC"
            ).fetchall()
        return [
            TraderAlias(
                exalter=str(r["exalter"]),
                alias=str(r["alias"]),
                is_famous=bool(r["is_famous"]),
                notes=(str(r["notes"]) if r["notes"] else None),
                updated_at=(
                    str(r["updated_at"]) if r["updated_at"] else None
                ),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def match(self, exalter: Optional[str]) -> MatchResult:
        """三级匹配：exact → contains → None。"""
        if not exalter:
            return MatchResult(
                exalter="", alias=None, is_famous=False, source="none"
            )
        exalter_str = str(exalter).strip()
        if not exalter_str:
            return MatchResult(
                exalter="", alias=None, is_famous=False, source="none"
            )

        items = self._all()

        # 1) 精确
        for t in items:
            if t.exalter == exalter_str:
                return MatchResult(
                    exalter=exalter_str,
                    alias=t.alias,
                    is_famous=t.is_famous,
                    source="exact",
                )

        # 2) 部分包含
        # 优先：表里 exalter 是 query 的子串（数据库里存的全名是简化后的关键词）
        # 次选：query 是表里 exalter 的子串（数据库里存的更完整）
        # 按 exalter 长度 DESC 已经在 _scan_db 排序好，长的优先匹配
        for t in items:
            if t.exalter and t.exalter in exalter_str:
                return MatchResult(
                    exalter=exalter_str,
                    alias=t.alias,
                    is_famous=t.is_famous,
                    source="contains",
                )
        for t in items:
            if t.exalter and exalter_str in t.exalter:
                return MatchResult(
                    exalter=exalter_str,
                    alias=t.alias,
                    is_famous=t.is_famous,
                    source="contains",
                )

        return MatchResult(
            exalter=exalter_str, alias=None, is_famous=False, source="none"
        )

    # ------------------------------------------------------------------
    # GUI / CLI 写操作
    # ------------------------------------------------------------------

    def add(
        self,
        exalter: str,
        alias: str,
        *,
        is_famous: bool = True,
        notes: Optional[str] = None,
    ) -> TraderAlias:
        """新增或覆盖一条别名。"""
        exalter_clean = (exalter or "").strip()
        alias_clean = (alias or "").strip()
        if not exalter_clean or not alias_clean:
            raise ValueError("exalter / alias 均不能为空")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO dim_trader_alias "
                "(exalter, alias, is_famous, notes, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(exalter) DO UPDATE SET "
                "  alias = excluded.alias, "
                "  is_famous = excluded.is_famous, "
                "  notes = excluded.notes, "
                "  updated_at = excluded.updated_at",
                (
                    exalter_clean,
                    alias_clean,
                    1 if is_famous else 0,
                    notes,
                    now,
                ),
            )
        self.reload()
        return TraderAlias(
            exalter=exalter_clean,
            alias=alias_clean,
            is_famous=is_famous,
            notes=notes,
            updated_at=now,
        )

    update = add  # 主键冲突已通过 ON CONFLICT 覆盖

    def delete(self, exalter: str) -> bool:
        """删除一条别名。"""
        clean = (exalter or "").strip()
        if not clean:
            return False
        with self.db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM dim_trader_alias WHERE exalter = ?",
                (clean,),
            )
        self.reload()
        return cursor.rowcount > 0

    def list_all(self) -> List[TraderAlias]:
        return self._all()

    def list_famous(self) -> List[TraderAlias]:
        return [t for t in self._all() if t.is_famous]

    def count(self) -> int:
        return len(self._all())


__all__ = [
    "TraderAlias",
    "MatchResult",
    "TraderAliasMatcher",
]
