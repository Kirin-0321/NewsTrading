"""精选新闻库与被剔除记录（SQLite）。"""

from datetime import datetime
from typing import Dict, List, Optional

from services.storage.database import get_connection, init_database
from services.storage.news_utils import news_to_row, row_to_news, news_item_id


class CuratedStore:
    """精选库读写。"""

    def __init__(self):
        init_database()

    def count(self) -> int:
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM curated_news").fetchone()[0]

    def get_time_bounds(self):
        """返回 (最早发布时间, 最晚发布时间)，无数据时为 (None, None)。"""
        with get_connection() as conn:
            row = conn.execute(
                "SELECT MIN(published_ts), MAX(published_ts) FROM curated_news"
            ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None, None
        return datetime.fromtimestamp(row[0]), datetime.fromtimestamp(row[1])

    def get_news_in_range(
        self, start: datetime, end: datetime
    ) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM curated_news
                WHERE published_ts >= ? AND published_ts <= ?
                ORDER BY published_ts DESC
                """,
                (int(start.timestamp()), int(end.timestamp())),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def upsert_cleaned(
        self,
        items: List[Dict],
        provider: str = "deepseek",
    ) -> int:
        """写入 AI 清洗保留项。返回新增条数。"""
        if not items:
            return 0

        cleaned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        inserted = 0
        with get_connection() as conn:
            for news in items:
                row = news_to_row(news)
                cid = news_item_id(news)
                raw_id = str(news.get("raw_id") or news.get("id") or cid)
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO curated_news
                    (id, raw_id, title, content, source, published_at, published_ts,
                     cleaned_at, clean_provider, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cid,
                        raw_id,
                        row["title"],
                        row["content"],
                        row["source"],
                        row["published_at"],
                        row["published_ts"],
                        cleaned_at,
                        provider,
                        row["extra_json"],
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
        return inserted

    def save_rejected(
        self,
        items: List[Dict],
        reason: str = "ai_remove",
    ) -> int:
        """记录被 AI 剔除的新闻。"""
        if not items:
            return 0

        rejected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        count = 0
        with get_connection() as conn:
            for news in items:
                rid = f"rej_{news_item_id(news)}"
                raw_id = str(news.get("raw_id") or news.get("id") or "")
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO rejected_news
                    (id, raw_id, title, rejected_at, reason)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (rid, raw_id, news.get("title") or "", rejected_at, reason),
                )
                if cur.rowcount > 0:
                    count += 1
        return count

    def get_meta(self, key: str) -> Optional[str]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO sync_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def get_clean_watermark(self) -> Optional[datetime]:
        val = self.get_meta("clean_watermark_ts")
        if not val:
            return None
        try:
            return datetime.fromtimestamp(int(val))
        except (TypeError, ValueError, OSError):
            return None

    def set_clean_watermark(self, ts: datetime) -> None:
        self.set_meta("clean_watermark_ts", str(int(ts.timestamp())))

    def get_daily_stats(self) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(published_at, 1, 10) AS day,
                       COUNT(*) AS cnt,
                       MIN(published_at) AS time_start,
                       MAX(published_at) AS time_end
                FROM curated_news
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
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM curated_news
                WHERE published_at LIKE ?
                ORDER BY published_ts DESC
                """,
                (f"{date_str}%",),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def delete_by_date(self, date_str: str) -> int:
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM curated_news WHERE published_at LIKE ?",
                (f"{date_str}%",),
            )
            return cur.rowcount

    def count_rejected(self) -> int:
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM rejected_news").fetchone()[0]

    def get_rejected_daily_stats(self) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT substr(rejected_at, 1, 10) AS day,
                       COUNT(*) AS cnt
                FROM rejected_news
                GROUP BY day
                ORDER BY day DESC
                """
            ).fetchall()
        return [{"date": r["day"], "count": r["cnt"]} for r in rows]

    def get_rejected_for_date(self, date_str: str) -> List[Dict]:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT raw_id, title, rejected_at, reason
                FROM rejected_news
                WHERE rejected_at LIKE ?
                ORDER BY rejected_at DESC
                """,
                (f"{date_str}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_rejected_by_date(self, date_str: str) -> int:
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM rejected_news WHERE rejected_at LIKE ?",
                (f"{date_str}%",),
            )
            return cur.rowcount
