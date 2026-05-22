"""SQLite 连接与表结构初始化。"""

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_news (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    content         TEXT,
    source          TEXT,
    published_at    TEXT NOT NULL,
    published_ts    INTEGER NOT NULL,
    crawled_at      TEXT NOT NULL,
    extra_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_published ON raw_news(published_ts DESC);

CREATE TABLE IF NOT EXISTS curated_news (
    id              TEXT PRIMARY KEY,
    raw_id          TEXT NOT NULL,
    title           TEXT NOT NULL,
    content         TEXT,
    source          TEXT,
    published_at    TEXT NOT NULL,
    published_ts    INTEGER NOT NULL,
    cleaned_at      TEXT NOT NULL,
    clean_provider  TEXT,
    extra_json      TEXT,
    FOREIGN KEY (raw_id) REFERENCES raw_news(id)
);
CREATE INDEX IF NOT EXISTS idx_curated_published ON curated_news(published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_curated_raw_id ON curated_news(raw_id);

CREATE TABLE IF NOT EXISTS rejected_news (
    id              TEXT PRIMARY KEY,
    raw_id          TEXT,
    title           TEXT,
    rejected_at     TEXT NOT NULL,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_rejected_raw_id ON rejected_news(raw_id);

CREATE TABLE IF NOT EXISTS sync_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""


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
