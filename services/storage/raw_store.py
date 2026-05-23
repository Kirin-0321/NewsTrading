"""原始新闻库（SQLite，含清洗状态）。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from services.storage.database import get_connection, init_database
from services.storage.news_utils import (
    CLEAN_CURATED,
    CLEAN_PENDING,
    CLEAN_REJECTED,
    news_to_row,
    row_to_news,
    parse_news_time,
)


@dataclass
class UpsertResult:
    inserted: int = 0
    skipped: int = 0


class RawStore:
    """新闻读写；清洗状态统一在 raw_news.clean_status / clean_reason。"""

    def __init__(self):
        init_database()

    def get_latest_news(self) -> Optional[Dict]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM raw_news ORDER BY published_ts DESC LIMIT 1"
            ).fetchone()
        return row_to_news(row) if row else None

    def count(self, status: Optional[str] = None) -> int:
        with get_connection() as conn:
            if status:
                return conn.execute(
                    "SELECT COUNT(*) FROM raw_news WHERE clean_status=?",
                    (status,),
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM raw_news").fetchone()[0]

    def count_curated(self) -> int:
        return self.count(CLEAN_CURATED)

    def count_rejected(self) -> int:
        return self.count(CLEAN_REJECTED)

    def get_time_bounds(self, status: Optional[str] = None):
        with get_connection() as conn:
            if status:
                row = conn.execute(
                    """
                    SELECT MIN(published_ts), MAX(published_ts) FROM raw_news
                    WHERE clean_status=?
                    """,
                    (status,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT MIN(published_ts), MAX(published_ts) FROM raw_news"
                ).fetchone()
        if not row or row[0] is None or row[1] is None:
            return None, None
        return datetime.fromtimestamp(row[0]), datetime.fromtimestamp(row[1])

    def get_all_news(self, status: Optional[str] = None) -> List[Dict]:
        with get_connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_news WHERE clean_status=?
                    ORDER BY published_ts DESC
                    """,
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM raw_news ORDER BY published_ts DESC"
                ).fetchall()
        return [row_to_news(r) for r in rows]

    def get_all_ids(self) -> Set[str]:
        with get_connection() as conn:
            rows = conn.execute("SELECT id FROM raw_news").fetchall()
        return {r["id"] for r in rows}

    def get_news_since(self, since: datetime, status: Optional[str] = None) -> List[Dict]:
        ts = int(since.timestamp())
        with get_connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_news
                    WHERE published_ts >= ? AND clean_status=?
                    ORDER BY published_ts DESC
                    """,
                    (ts, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_news WHERE published_ts >= ?
                    ORDER BY published_ts DESC
                    """,
                    (ts,),
                ).fetchall()
        return [row_to_news(r) for r in rows]

    def get_news_in_range(
        self,
        start: datetime,
        end: datetime,
        status: Optional[str] = None,
    ) -> List[Dict]:
        with get_connection() as conn:
            params = [int(start.timestamp()), int(end.timestamp())]
            status_sql = ""
            if status:
                status_sql = " AND clean_status=?"
                params.append(status)
            rows = conn.execute(
                f"""
                SELECT * FROM raw_news
                WHERE published_ts >= ? AND published_ts <= ?{status_sql}
                ORDER BY published_ts DESC
                """,
                params,
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def upsert_news(self, items: List[Dict]) -> UpsertResult:
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
                    (id, title, content, source, published_at, published_ts,
                     crawled_at, clean_status, clean_reason, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["title"],
                        row["content"],
                        row["source"],
                        row["published_at"],
                        row["published_ts"],
                        row["crawled_at"],
                        CLEAN_PENDING,
                        None,
                        row["extra_json"],
                    ),
                )
                if cur.rowcount > 0:
                    result.inserted += 1
                else:
                    result.skipped += 1
        return result

    def get_uncleaned_news(self, limit: int = 500) -> List[Dict]:
        """取 clean_status=pending 的新闻。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM raw_news
                WHERE clean_status=?
                ORDER BY published_ts ASC
                LIMIT ?
                """,
                (CLEAN_PENDING, limit),
            ).fetchall()
        return [row_to_news(r) for r in rows]

    def count_uncleaned(self) -> int:
        return self.count(CLEAN_PENDING)

    def apply_clean_results(
        self,
        kept: List[Dict],
        removed: List[Dict],
        provider: str = "deepseek",
    ) -> Tuple[int, int]:
        """
        批量写入清洗结果。

        Returns:
            (精选更新数, 剔除更新数)
        """
        kept_n = 0
        removed_n = 0
        with get_connection() as conn:
            for news in kept:
                nid = str(news.get("raw_id") or news.get("id") or "")
                if not nid:
                    continue
                reason = news.get("keep_reason") or "符合保留标准"
                cur = conn.execute(
                    """
                    UPDATE raw_news SET clean_status=?, clean_reason=?
                    WHERE id=? AND clean_status=?
                    """,
                    (CLEAN_CURATED, reason, nid, CLEAN_PENDING),
                )
                if cur.rowcount == 0:
                    cur = conn.execute(
                        """
                        UPDATE raw_news SET clean_status=?, clean_reason=?
                        WHERE id=?
                        """,
                        (CLEAN_CURATED, reason, nid),
                    )
                kept_n += cur.rowcount

            for news in removed:
                nid = str(news.get("raw_id") or news.get("id") or "")
                if not nid:
                    continue
                reason = news.get("removal_reason") or "不符合保留标准"
                cur = conn.execute(
                    """
                    UPDATE raw_news SET clean_status=?, clean_reason=?
                    WHERE id=? AND clean_status=?
                    """,
                    (CLEAN_REJECTED, reason, nid, CLEAN_PENDING),
                )
                if cur.rowcount == 0:
                    cur = conn.execute(
                        """
                        UPDATE raw_news SET clean_status=?, clean_reason=?
                        WHERE id=?
                        """,
                        (CLEAN_REJECTED, reason, nid),
                    )
                removed_n += cur.rowcount
        return kept_n, removed_n

    def get_daily_stats(self, status: Optional[str] = None) -> List[Dict]:
        with get_connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT substr(published_at, 1, 10) AS day,
                           COUNT(*) AS cnt,
                           MIN(published_at) AS time_start,
                           MAX(published_at) AS time_end
                    FROM raw_news
                    WHERE clean_status=?
                    GROUP BY day
                    ORDER BY day DESC
                    """,
                    (status,),
                ).fetchall()
            else:
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

    def get_news_for_date(
        self, date_str: str, status: Optional[str] = None
    ) -> List[Dict]:
        with get_connection() as conn:
            if status:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_news
                    WHERE published_at LIKE ? AND clean_status=?
                    ORDER BY published_ts DESC
                    """,
                    (f"{date_str}%", status),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM raw_news
                    WHERE published_at LIKE ?
                    ORDER BY published_ts DESC
                    """,
                    (f"{date_str}%",),
                ).fetchall()
        return [row_to_news(r) for r in rows]

    def reset_clean_status_by_date(self, date_str: str, from_status: str) -> int:
        """将某日某状态的新闻重置为 pending（用于数据管理页删除精选/剔除）。"""
        with get_connection() as conn:
            cur = conn.execute(
                """
                UPDATE raw_news SET clean_status=?, clean_reason=NULL
                WHERE published_at LIKE ? AND clean_status=?
                """,
                (CLEAN_PENDING, f"{date_str}%", from_status),
            )
            return cur.rowcount

    def delete_by_date(self, date_str: str) -> int:
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM raw_news WHERE published_at LIKE ?",
                (f"{date_str}%",),
            )
            return cur.rowcount

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

    @staticmethod
    def parse_time(news: Dict) -> Optional[datetime]:
        return parse_news_time(news)
