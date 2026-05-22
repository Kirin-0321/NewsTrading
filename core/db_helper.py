"""
本地新闻存储辅助（SQLite 原始库封装）

说明：历史命名沿用 db_helper；底层已迁移至 services.storage.raw_store。
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from services.storage import get_raw_store
from services.storage.news_utils import parse_news_time as _parse_news_time
from core.semantic_dedup import semantic_deduplicate


def get_latest_news_from_db() -> Optional[Dict]:
    """获取原始库中最新的一条新闻。"""
    news = get_raw_store().get_latest_news()
    if news:
        print(f"[数据库] 最新新闻时间: {news.get('datetime') or news.get('time', '')}")
        print(f"[数据库] 最新新闻标题: {news.get('title', '')[:50]}...")
    return news


def load_all_db_news() -> List[Dict]:
    """加载原始库全部新闻。"""
    all_news = get_raw_store().get_all_news()
    print(f"[数据库] 总共加载 {len(all_news)} 条新闻")
    return all_news


def deduplicate_with_db(new_news_list: List[Dict]) -> Tuple[List[Dict], Dict]:
    """
    将新爬取的新闻与原始库去重。

    返回:
        (去重后的新增列表, 统计信息)
    """
    store = get_raw_store()
    print("[去重] 开始去重处理...")
    print(f"[去重] 新爬取新闻: {len(new_news_list)} 条")

    internal_deduped = semantic_deduplicate(new_news_list)
    internal_removed = len(new_news_list) - len(internal_deduped)
    print(f"[去重] 内部去重: 去除 {internal_removed} 条")

    existing_ids = store.get_all_ids()
    new_only = [
        n for n in internal_deduped
        if str(n.get("id", "")) not in existing_ids
    ]
    db_removed = len(internal_deduped) - len(new_only)
    print(f"[去重] 与库去重: 去除 {db_removed} 条")
    print(f"[去重] 最终保留: {len(new_only)} 条新增新闻")

    stats = {
        "crawled": len(new_news_list),
        "internal_duplicates": internal_removed,
        "db_duplicates": db_removed,
        "final_saved": len(new_only),
    }
    return new_only, stats


def parse_news_time(news: Dict) -> Optional[datetime]:
    """解析新闻时间。"""
    return _parse_news_time(news)
