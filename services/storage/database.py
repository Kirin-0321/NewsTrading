"""news.db 连接与表结构初始化（Phase -1 三库重构后瘦身版）。

v5（2026-05-27 Phase -1）：news.db 只保留爬虫产物：
    - raw_news        原始新闻表（爬虫不可重建数据）
    - sync_meta       同步元信息表

AI 衍生数据全部迁移到 ``data/ai_inference.db``：
    - ai_reports / theme_predictions / theme_stocks / theme_news
    - theme_prediction_scores / theme_stock_scores（本期新增）

详见 doc/design/05-27-1209-三库表结构详细设计.md
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

_log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_news (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    content         TEXT,
    source          TEXT,
    published_at    TEXT NOT NULL,
    published_ts    INTEGER NOT NULL,
    crawled_at      TEXT NOT NULL,
    clean_status    TEXT NOT NULL DEFAULT 'pending',
    clean_reason    TEXT,
    extra_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_published ON raw_news(published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_raw_clean_status ON raw_news(clean_status);

CREATE TABLE IF NOT EXISTS sync_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""

# Phase -1 三库重构：旧的 AI 相关 4 张表已迁到 ai_inference.db，
# init_database 入口处 DROP 一次即可（幂等，无害）。
_LEGACY_AI_TABLES = [
    "theme_news",          # 子表先删（避免 FK 失败）
    "theme_stocks",
    "theme_predictions",
    "ai_reports",
]


def _ensure_clean_status_columns(conn: sqlite3.Connection) -> None:
    """老库补 clean_status / clean_reason 字段，并直接抛弃旧的 curated/rejected 表。

    旧分类不做迁移：原始新闻全部回到 pending，需要重新跑清洗。

    若 ``raw_news`` 表尚不存在（首次初始化的新库），跳过补字段逻辑——
    后续 ``executescript(_SCHEMA)`` 会按最新 schema 直接建表。
    """
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='raw_news'"
    ).fetchone():
        for legacy in ("curated_news", "rejected_news"):
            conn.execute(f"DROP TABLE IF EXISTS {legacy}")
        conn.commit()
        return

    cols = {r[1] for r in conn.execute("PRAGMA table_info(raw_news)").fetchall()}
    changed = False
    if "clean_status" not in cols:
        conn.execute(
            "ALTER TABLE raw_news ADD COLUMN clean_status TEXT NOT NULL DEFAULT 'pending'"
        )
        changed = True
    if "clean_reason" not in cols:
        conn.execute("ALTER TABLE raw_news ADD COLUMN clean_reason TEXT")
        changed = True

    for legacy in ("curated_news", "rejected_news"):
        conn.execute(f"DROP TABLE IF EXISTS {legacy}")

    if changed:
        _log.info("raw_news 已添加 clean_status / clean_reason，旧分类表已删除")
    conn.commit()


def _drop_legacy_ai_tables(conn: sqlite3.Connection) -> None:
    """Phase -1 一次性清理：把 AI 相关 4 张老表从 news.db 移除。

    迁库后 news.db 不再持有 AI 衍生数据，旧表（如果存在）也已被主人决策"直接清空"。
    DROP IF EXISTS 幂等，重复跑无害。

    主人决策（v3）：
        - 不迁移任何旧数据到 ai_inference.db
        - 19 份历史 md 文件保留在磁盘但不批量重抽
    """
    dropped: list[str] = []
    for table in _LEGACY_AI_TABLES:
        # 仅在表存在时记录日志，DROP IF EXISTS 始终安全
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        if exists:
            dropped.append(table)
    if dropped:
        # 清理 AUTOINCREMENT 计数残留（不存在则 OperationalError，忽略）
        try:
            conn.execute(
                "DELETE FROM sqlite_sequence WHERE name IN "
                "('theme_predictions','theme_stocks','theme_news','ai_reports')"
            )
        except sqlite3.OperationalError:
            pass
        _log.warning(
            "Phase -1: 已从 news.db DROP 旧 AI 表 %s（数据已按主人决策清空，"
            "未来写入将走 data/ai_inference.db）",
            dropped,
        )
        conn.commit()


def get_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_db_path() -> str:
    return os.path.join(get_project_root(), "data", "news.db")


def init_database(db_path=None) -> str:
    """创建 data 目录并初始化 news.db 表结构（仅 raw_news + sync_meta）。

    Phase -1 后此函数不再管 AI 表，那部分由
    ``services.storage.ai_inference_db.AIInferenceDB.ensure_schema()`` 负责。

    Returns:
        数据库文件路径
    """
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as conn:
        _drop_legacy_ai_tables(conn)        # Phase -1: 清旧 AI 表
        _ensure_clean_status_columns(conn)  # 老库补 raw_news 字段 + DROP 旧 curated/rejected
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    return path


@contextmanager
def get_connection(db_path=None) -> Iterator[sqlite3.Connection]:
    """获取 news.db 连接（Row 工厂）。

    Phase -1 后**只**用于 raw_news / sync_meta；
    AI 相关读写请走 ``services.storage.ai_inference_db.get_ai_inference_db().connect()``。
    """
    path = db_path or get_db_path()
    if not os.path.exists(path):
        init_database(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
