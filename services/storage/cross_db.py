"""跨库 ATTACH 上下文工具（Phase -1 三库重构）。

用例：打分模块需要同时查 ai_inference.db（主，题材/标的）+ market.db
（个股/板块行情）+ news.db（题材关联新闻）三个库。SQLite 通过
``ATTACH DATABASE`` 把外部 db 文件挂到当前连接，之后用 ``alias.table``
就可以跨库 JOIN。

本模块提供唯一入口 :func:`attached_dbs`，确保：

1. **正确的别名约定**: ``ai`` / ``market`` / ``news``（与所有打分 SQL 一致）
2. **退出自动 DETACH**: 异常路径也会 DETACH，避免连接复用时残留 ATTACH 污染
3. **PRAGMA 一致性**: WAL / foreign_keys / busy_timeout 与各库 connect() 一致
4. **只读优化**: 允许主库以只读方式打开（打分模块大量场景仅查询）

约定（推荐）：
    - 主库 = ai_inference.db (alias 'ai')
    - 跨库 JOIN 时 alias 用 ``m.`` ``n.`` 引用外部表
    - 例：``SELECT ... FROM theme_predictions tp JOIN m.fact_stock_daily fsd ON ...``

使用示例::

    from services.storage.cross_db import attached_dbs

    with attached_dbs(primary='ai', attach=('market', 'news')) as conn:
        rows = conn.execute('''
            SELECT tp.theme_name, fsd.pct_chg, n.title
            FROM theme_predictions tp
            JOIN theme_stocks ts ON ts.theme_id = tp.id
            JOIN m.fact_stock_daily fsd
              ON fsd.ts_code = ts.normalized_code AND fsd.trade_date = ?
            LEFT JOIN theme_news tn ON tn.theme_id = tp.id
            LEFT JOIN n.raw_news n ON n.id = tn.news_id
            WHERE tp.report_date = ?
        ''', (score_date, report_date)).fetchall()
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator, Sequence

_log = logging.getLogger(__name__)

# 别名 → 数据库路径解析器（懒加载，避免循环 import）
_DB_ALIASES = ("ai", "market", "news")


def _get_db_path(alias: str) -> str:
    """根据别名返回数据库的绝对路径字符串。"""
    if alias == "ai":
        from services.storage.ai_inference_db import get_ai_inference_db
        return get_ai_inference_db().path.as_posix()
    if alias == "market":
        from services.market.market_db import get_market_db
        return get_market_db().path.as_posix()
    if alias == "news":
        from services.storage.database import get_db_path
        return str(get_db_path()).replace("\\", "/")
    raise ValueError(
        f"未知的库别名 {alias!r}，合法值: {_DB_ALIASES}"
    )


def _ensure_primary_schema(alias: str) -> None:
    """主库打开前确保 schema 已 ready（避免首次跑时表不存在）。"""
    if alias == "ai":
        from services.storage.ai_inference_db import get_ai_inference_db
        get_ai_inference_db().ensure_schema()
    elif alias == "market":
        from services.market.market_db import get_market_db
        get_market_db().ensure_schema()
    elif alias == "news":
        from services.storage.database import init_database
        init_database()


@contextmanager
def attached_dbs(
    primary: str = "ai",
    attach: Sequence[str] = ("market",),
    *,
    readonly: bool = False,
) -> Iterator[sqlite3.Connection]:
    """打开主库连接 + ATTACH 指定从库，退出时自动 DETACH + close。

    Args:
        primary: 主库别名，``ai`` / ``market`` / ``news``。
        attach: 要 ATTACH 的从库别名列表，可重复任意子集。
                **不可包含 ``primary``**（自己 ATTACH 自己会报错）。
        readonly: 仅作用于主库；ATTACH 上来的从库总以读写挂载，
                  靠应用层不写来约束（绝大多数场景从库只读）。

    Yields:
        sqlite3.Connection，row_factory 已设为 ``sqlite3.Row``，
        PRAGMA 已配置（WAL / FK / busy_timeout=30s）。

    Raises:
        ValueError: 别名非法 / primary in attach。
        sqlite3.OperationalError: ATTACH 失败（一般是文件不存在）。
    """
    if primary not in _DB_ALIASES:
        raise ValueError(
            f"primary 必须是 {_DB_ALIASES} 之一，得到 {primary!r}"
        )
    bad = [a for a in attach if a not in _DB_ALIASES]
    if bad:
        raise ValueError(
            f"attach 包含非法别名 {bad}，合法 {_DB_ALIASES}"
        )
    if primary in attach:
        raise ValueError(
            f"primary={primary!r} 不能同时出现在 attach 中（自己挂载自己）"
        )

    _ensure_primary_schema(primary)
    primary_path = _get_db_path(primary)

    if readonly:
        uri = f"file:{primary_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        conn = sqlite3.connect(primary_path, timeout=30)
    conn.row_factory = sqlite3.Row

    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            if not readonly:
                raise

        attached: list[str] = []
        for alias in attach:
            path = _get_db_path(alias)
            try:
                conn.execute(f"ATTACH DATABASE '{path}' AS {alias}")
                attached.append(alias)
            except sqlite3.OperationalError as exc:
                raise sqlite3.OperationalError(
                    f"ATTACH {alias} ({path}) 失败: {exc}"
                ) from exc

        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception:
            if not readonly:
                conn.rollback()
            raise
        finally:
            # 异常路径也要 DETACH，避免连接复用时污染
            for alias in reversed(attached):
                try:
                    conn.execute(f"DETACH DATABASE {alias}")
                except sqlite3.OperationalError as exc:
                    _log.warning(
                        "DETACH %s 失败 (忽略): %s", alias, exc
                    )
    finally:
        conn.close()


__all__ = ["attached_dbs"]
