"""SQLite 存储层。

单表结构：`raw_news` 用 `clean_status` 字段区分 pending/curated/rejected，
不再保留 curated_news / rejected_news 子表。
"""

from services.storage.database import get_db_path, init_database
from services.storage.news_utils import (
    CLEAN_CURATED,
    CLEAN_PENDING,
    CLEAN_REJECTED,
)
from services.storage.raw_store import RawStore
from services.storage.theme_store import ThemeStore

_raw_store = None
_theme_store = None


def get_raw_store() -> RawStore:
    global _raw_store
    if _raw_store is None:
        _raw_store = RawStore()
    return _raw_store


def get_theme_store() -> ThemeStore:
    global _theme_store
    if _theme_store is None:
        _theme_store = ThemeStore()
    return _theme_store


__all__ = [
    "get_db_path",
    "init_database",
    "get_raw_store",
    "get_theme_store",
    "RawStore",
    "ThemeStore",
    "CLEAN_PENDING",
    "CLEAN_CURATED",
    "CLEAN_REJECTED",
]
