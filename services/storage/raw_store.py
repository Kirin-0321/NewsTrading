"""原始新闻库（SQLite）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set

from services.storage.database import get_connection, init_database
from services.storage.news_utils import news_to_row, row_to_news, parse_news_time


@dataclass
class UpsertResult:
    inserted: int = 0
    skipped: int = 0


class RawStore:
    """原始库读写。"""

    def __init__(self):
        init_database()

    def get_latest_news(self) -> Optional[Dict]:
        """按发布时间取最新一条。"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM raw_news ORDER BY published_ts DESC LIMIT 1"
            ).fetchone()
        return row_to_news(row) if row else None

    def count(self) -> int:
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]

    def get_time_bounds(self):
        """返回 (最早发布时间, 最晚发布时间)，无数据时为 (None, None)。"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT MIN(published_ts), MAX(published_ts) FROM raw_news"
            ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None, None
        return datetime.fromtimestamp(row[0]), datetime.fromtimestamp(row[1])

    def get_all_news(self) -> List[Dict]:
        """按发布时间倒序返回全部新闻。"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM raw_news ORDER BY published_ts DESC"
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def get_all_ids(self) -> Set[str]:
        with get_connection() as conn:
            rows = conn.execute("SELECT id FROM raw_news").fetchall()
        return {r["id"] for r in rows}

    def get_news_since(self, since: datetime) -> List[Dict]:
        ts = int(since.timestamp())
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM raw_news WHERE published_ts >= ? ORDER BY published_ts DESC",
                (ts,),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def get_news_in_range(
        self, start: datetime, end: datetime
    ) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM raw_news
                WHERE published_ts >= ? AND published_ts <= ?
                ORDER BY published_ts DESC
                """,
                (int(start.timestamp()), int(end.timestamp())),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def upsert_news(self, items: List[Dict]) -> UpsertResult:
        """插入新新闻（按 id 去重，已存在则跳过）。"""
        result = UpsertResult()
        if not items:
            return result

        crawled_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            for news in items:
                row = news_to_row(news, crawled_at=crawled_at)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO raw_news
                    (id, title, content, source, published_at, published_ts, crawled_at, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["title"],
                        row["content"],
                        row["source"],
                        row["published_at"],
                        row["published_ts"],
                        row["crawled_at"],
                        row["extra_json"],
                    ),
                )
                if cur.rowcount > 0:
                    result.inserted += 1
                else:
                    result.skipped += 1
        return result

    def get_uncleaned_news(self, limit: int = 500) -> List[Dict]:
        """取尚未进入精选库或剔除库的新闻。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.* FROM raw_news r
                LEFT JOIN curated_news c ON c.raw_id = r.id
                LEFT JOIN rejected_news j ON j.raw_id = r.id
                WHERE c.id IS NULL AND j.raw_id IS NULL
                ORDER BY r.published_ts ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def count_uncleaned(self) -> int:
        with get_connection() as conn:
            return conn.execute(
                """
                SELECT COUNT(*) FROM raw_news r
                LEFT JOIN curated_news c ON c.raw_id = r.id
                LEFT JOIN rejected_news j ON j.raw_id = r.id
                WHERE c.id IS NULL AND j.raw_id IS NULL
                """
            ).fetchone()[0]

    def get_daily_stats(self) -> List[Dict]:
        """按日期聚合统计。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(published_at, 1, 10) AS day,
                       COUNT(*) AS cnt,
                       MIN(published_at) AS time_start,
                       MAX(published_at) AS time_end
                FROM raw_news
                GROUP BY day
                ORDER BY day DESC
                """
            ).fetchall()
        return [
            {
                "date": r["day"],
                "count": r["cnt"],
                "time_start": r["time_start"],
                "time_end": r["time_end"],
            }
            for r in rows
        ]

    def get_news_for_date(self, date_str: str) -> List[Dict]:
        """获取指定日期的新闻（YYYY-MM-DD）。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM raw_news
                WHERE published_at LIKE ?
                ORDER BY published_ts DESC
                """,
                (f"{date_str}%",),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def delete_by_date(self, date_str: str) -> int:
        """删除指定日期的原始新闻。"""
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM raw_news WHERE published_at LIKE ?",
                (f"{date_str}%",),
            )
            return cur.rowcount

    @staticmethod
    def parse_time(news: Dict) -> Optional[datetime]:
        return parse_news_time(news)
