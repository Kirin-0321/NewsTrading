"""SQLite 存储层（Phase -1 三库架构）。

三库职责：
    - **news.db**：raw_news / sync_meta（爬虫产物，不可重建）
    - **market.db**：dim_* / fact_*（接口产物，可重建）
    - **ai_inference.db**：ai_reports / theme_* / *_scores（AI 衍生，可重算）

详见 doc/design/05-27-1209-三库表结构详细设计.md
"""

from services.storage.ai_inference_db import (
    AIInferenceDB,
    get_ai_inference_db,
)
from services.storage.ai_reports_store import (
    AIReportRecord,
    AIReportsStore,
    get_ai_reports_store,
    record_report,
)
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


def init_all_databases() -> None:
    """Phase -1: 启动入口一次性初始化三库 schema。

    供 main.py / CLI 工具的"卫兵式调用"使用，单独跑某个 store 时
    各自的 ensure_schema() 也会兜底，所以这里只是把三库都热起来。
    """
    init_database()  # news.db
    from services.market.market_db import get_market_db
    get_market_db().ensure_schema()
    get_ai_inference_db().ensure_schema()


__all__ = [
    "get_db_path",
    "init_database",
    "init_all_databases",
    "get_raw_store",
    "get_theme_store",
    "get_ai_reports_store",
    "record_report",
    "AIReportRecord",
    "AIReportsStore",
    "AIInferenceDB",
    "get_ai_inference_db",
    "RawStore",
    "ThemeStore",
    "CLEAN_PENDING",
    "CLEAN_CURATED",
    "CLEAN_REJECTED",
]
