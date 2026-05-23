"""SQLite 连接与表结构初始化。"""

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

-- 题材预测主表：每份分析报告 AI 抽取得到的题材独立一行（快照式）
-- v2 (2026-05-22) 删 catalyst/resonance_count/raw_excerpt，合并语义到 reason
CREATE TABLE IF NOT EXISTS theme_predictions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id           TEXT NOT NULL,
    report_date         TEXT NOT NULL,
    report_time         TEXT,
    report_path         TEXT NOT NULL,
    theme_name          TEXT NOT NULL,
    theme_category      TEXT,
    strength_score      INTEGER NOT NULL,
    strength_level      TEXT NOT NULL,
    priority_rank       INTEGER,  -- 单份报告内排序序号；非全局唯一，跨 report_id 可重复
    duration            TEXT,
    expectation_gap     TEXT,
    sentiment           TEXT NOT NULL DEFAULT '利好',
    is_cold             INTEGER NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL,
    risk_note           TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theme_report_date  ON theme_predictions(report_date DESC);
CREATE INDEX IF NOT EXISTS idx_theme_name         ON theme_predictions(theme_name);
CREATE INDEX IF NOT EXISTS idx_theme_strength     ON theme_predictions(report_date, strength_score DESC);
CREATE INDEX IF NOT EXISTS idx_theme_category     ON theme_predictions(theme_category);
CREATE INDEX IF NOT EXISTS idx_theme_report_id    ON theme_predictions(report_id);

-- 题材-标的 关联表（v2: 删 elasticity）
CREATE TABLE IF NOT EXISTS theme_stocks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    stock_name      TEXT NOT NULL,
    stock_code      TEXT,
    role            TEXT,
    reason          TEXT,
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_theme ON theme_stocks(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_name  ON theme_stocks(stock_name);
CREATE INDEX IF NOT EXISTS idx_theme_stocks_code  ON theme_stocks(stock_code);

-- 题材-新闻 关联表（v2: 删 news_title，news_id 升级为关键字段，由脚本反查报告底部填入）
CREATE TABLE IF NOT EXISTS theme_news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id        INTEGER NOT NULL,
    news_ref        TEXT NOT NULL,
    news_id         TEXT,
    relation_type   TEXT,
    FOREIGN KEY (theme_id) REFERENCES theme_predictions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_theme_news_theme   ON theme_news(theme_id);
CREATE INDEX IF NOT EXISTS idx_theme_news_news_id ON theme_news(news_id);
"""

# v2 schema 指纹：每张题材表应该存在的列。如果检测到旧列就 DROP 重建。
_THEME_V2_COLUMNS = {
    "theme_predictions": {
        "id", "report_id", "report_date", "report_time", "report_path",
        "theme_name", "theme_category", "strength_score", "strength_level",
        "priority_rank", "duration", "expectation_gap", "sentiment",
        "is_cold", "reason", "risk_note", "created_at",
    },
    "theme_stocks": {
        "id", "theme_id", "stock_name", "stock_code", "role", "reason",
    },
    "theme_news": {
        "id", "theme_id", "news_ref", "news_id", "relation_type",
    },
}


def _ensure_clean_status_columns(conn: sqlite3.Connection) -> None:
    """老库补 clean_status / clean_reason 字段，并直接抛弃旧的 curated/rejected 表。

    旧分类不做迁移：原始新闻全部回到 pending，需要重新跑清洗。
    """
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


def _migrate_theme_tables_v2(conn: sqlite3.Connection) -> None:
    """检测题材三张表的 schema，若与 v2 不一致则 DROP 重建。

    题材数据是衍生的（从分析报告 .md 抽取），重新跑就能再生，
    所以直接清空比写复杂的 ALTER TABLE 迁移脚本更干净。
    """
    needs_rebuild = False
    for table, want_cols in _THEME_V2_COLUMNS.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            continue  # 表不存在，CREATE 时会建
        if not rows:
            continue
        actual_cols = {r[1] for r in rows}
        if actual_cols != want_cols:
            needs_rebuild = True
            _log.warning(
                "题材表 %s schema 与 v2 不一致：actual=%s expect=%s，将清空重建",
                table, sorted(actual_cols), sorted(want_cols),
            )
            break

    if needs_rebuild:
        for table in ("theme_news", "theme_stocks", "theme_predictions"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()


def get_project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def get_db_path() -> str:
    return os.path.join(get_project_root(), "data", "news.db")


def init_database(db_path=None) -> str:
    """
    创建 data 目录并初始化表结构。

    Returns:
        数据库文件路径
    """
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sqlite3.connect(path) as conn:
        _migrate_theme_tables_v2(conn)
        _ensure_clean_status_columns(conn)  # 旧库补列 + DROP 旧 curated/rejected 表
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
    return path


@contextmanager
def get_connection(db_path=None) -> Iterator[sqlite3.Connection]:
    """获取数据库连接（Row 工厂）。"""
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
