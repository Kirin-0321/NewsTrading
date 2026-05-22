"""
① 数据获取服务：爬取 → 增量截断 → 去重 → 写入原始库 (SQLite)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from core.semantic_dedup import semantic_deduplicate, default_deduplicator
from services.storage import get_raw_store
from services.storage.news_utils import normalize_news, parse_news_time


@dataclass
class CrawlSyncResult:
    ok: bool = False
    crawled: int = 0
    inserted: int = 0
    skipped: int = 0
    internal_duplicates: int = 0
    error: Optional[str] = None
    stats: Dict = field(default_factory=dict)


class CrawlSyncService:
    """爬取并同步到原始库。"""

    def __init__(self):
        self.raw_store = get_raw_store()
        self._crawler = None

    def request_stop(self) -> None:
        if self._crawler is not None:
            self._crawler.request_stop()

    def run(
        self,
        scroll_times: int = 36,
        wait_seconds: int = 6,
        headless: bool = True,
        max_no_change: int = 3,
        incremental: bool = True,
        progress_callback: Optional[Callable] = None,
    ) -> CrawlSyncResult:
        """
        执行爬取同步。

        Args:
            scroll_times: 滚动次数
            wait_seconds: 每次滚动等待秒数
            headless: 无头模式
            max_no_change: 连续无新内容停止次数
            incremental: 是否增量（遇已有新闻停止并截断）
            progress_callback: (current, total, news_count, message)

        Returns:
            CrawlSyncResult
        """
        result = CrawlSyncResult()
        log = progress_callback or (lambda *a, **k: None)

        try:
            from core.news_crawler_scroll import NewsCrawler

            latest_news = None
            if incremental:
                latest_news = self.raw_store.get_latest_news()
                if latest_news:
                    t = latest_news.get("datetime") or latest_news.get("time", "")
                    log(0, 0, 0, f"[增量] 库内最新: {t} | {latest_news.get('title', '')[:40]}")

            crawler = NewsCrawler()
            self._crawler = crawler

            def _progress(current, total, news_count, message):
                if progress_callback:
                    progress_callback(current, total, news_count, message)

            crawler.set_progress_callback(_progress)

            if incremental and latest_news:
                latest_time = parse_news_time(latest_news)
                crawler.set_auto_stop(True, latest_time, latest_news.get("title", ""))

            log(0, 0, 0, f"开始爬取（滚动{scroll_times}次）...")
            crawl_result = crawler.run(
                scroll_times=scroll_times,
                wait_seconds=wait_seconds,
                headless=headless,
                max_no_change=max_no_change,
            )

            all_news = crawl_result.get("news_list", [])
            result.crawled = len(all_news)
            log(0, 0, len(all_news), f"爬取完成，共 {len(all_news)} 条")

            if incremental and latest_news and all_news:
                all_news = self._truncate_incremental(all_news, latest_news, log)

            before_dedup = len(all_news)

            from datetime import timedelta

            reference_pool = None
            if all_news:
                times = [parse_news_time(n) for n in all_news]
                times = [t for t in times if t]
                if times:
                    since = min(times) - timedelta(
                        minutes=default_deduplicator.time_window_minutes
                    )
                    reference_pool = self.raw_store.get_news_since(since)

            all_news = semantic_deduplicate(all_news, reference_pool=reference_pool)
            result.internal_duplicates = before_dedup - len(all_news)

            existing_ids = self.raw_store.get_all_ids()
            new_items = [
                normalize_news(n)
                for n in all_news
                if normalize_news(n)["id"] not in existing_ids
            ]

            upsert = self.raw_store.upsert_news(new_items)
            result.inserted = upsert.inserted
            result.skipped = upsert.skipped + (len(all_news) - len(new_items))

            result.ok = True
            result.stats = {
                "crawled": result.crawled,
                "internal_duplicates": result.internal_duplicates,
                "inserted": result.inserted,
                "skipped": result.skipped,
            }
            log(0, 0, result.inserted, f"入库完成: 新增 {result.inserted} 条")

        except Exception as e:
            result.error = str(e)
            result.ok = False

        finally:
            self._crawler = None

        return result

    def _truncate_incremental(
        self,
        all_news: List[Dict],
        latest_news: Dict,
        log: Callable,
    ) -> List[Dict]:
        latest_time = parse_news_time(latest_news)
        latest_title = latest_news.get("title", "")
        truncated = []

        for news in all_news:
            if latest_title and news.get("title") == latest_title:
                log(0, 0, 0, "[增量] 遇到已有新闻（标题匹配），截断")
                break
            current_time = parse_news_time(news)
            if current_time and latest_time and current_time <= latest_time:
                log(0, 0, 0, "[增量] 到达时间边界，截断")
                break
            truncated.append(news)

        if not truncated and all_news:
            return all_news
        return truncated


def crawl_sync(**kwargs) -> CrawlSyncResult:
    """模块级快捷入口。"""
    return CrawlSyncService().run(**kwargs)
