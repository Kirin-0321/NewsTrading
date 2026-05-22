"""SQLite 存储层。"""

from services.storage.database import get_db_path, init_database
from services.storage.raw_store import RawStore
from services.storage.curated_store import CuratedStore
from services.storage.theme_store import ThemeStore

_raw_store = None
_curated_store = None
_theme_store = None


def get_raw_store() -> RawStore:
    global _raw_store
    if _raw_store is None:
        _raw_store = RawStore()
    return _raw_store


def get_curated_store() -> CuratedStore:
    global _curated_store
    if _curated_store is None:
        _curated_store = CuratedStore()
    return _curated_store


def get_theme_store() -> ThemeStore:
    global _theme_store
    if _theme_store is None:
        _theme_store = ThemeStore()
    return _theme_store


__all__ = [
    "get_db_path",
    "init_database",
    "get_raw_store",
    "get_curated_store",
    "get_theme_store",
    "RawStore",
    "CuratedStore",
    "ThemeStore",
]
