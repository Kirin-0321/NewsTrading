"""
Agent MCP 入口占位（Phase 6）

后续在此注册 tools，映射到 services 层：
  - crawl_news      -> services.crawl_sync_service.crawl_sync
  - clean_news      -> services.clean_sync_service.clean_sync
  - analyze_news    -> services.analysis_service.analyze_news
  - get_db_stats    -> raw/curated count
"""

from services.storage import get_raw_store, get_curated_store
from services.crawl_sync_service import crawl_sync
from services.clean_sync_service import clean_sync
from services.analysis_service import analyze_news


def get_db_stats() -> dict:
    """Agent 只读：数据库统计。"""
    return {
        "raw_count": get_raw_store().count(),
        "curated_count": get_curated_store().count(),
    }


__all__ = [
    "crawl_sync",
    "clean_sync",
    "analyze_news",
    "get_db_stats",
]
