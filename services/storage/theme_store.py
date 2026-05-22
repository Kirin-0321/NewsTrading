"""题材预测库（SQLite）。

存三张表:
    theme_predictions  题材主表（每份报告快照）
    theme_stocks       题材-标的 关联
    theme_news         题材-新闻 关联

设计要点:
    - 同一份报告(report_id)允许重复插入相同题材，按 report_date 做时序快照
    - save_themes() 是事务性的：主表 + 标的 + 新闻 三表一起入库失败回滚
    - 强度 score / level 在 _normalize_theme() 自动互相校正
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

from services.storage.database import get_connection, init_database


_LEVEL_RANGE: List[Tuple[str, int, int]] = [
    ("极强", 80, 100),
    ("强", 60, 79),
    ("中", 40, 59),
    ("弱", 0, 39),
]
_VALID_LEVELS = {row[0] for row in _LEVEL_RANGE}
_VALID_SENTIMENT = {"利好", "利空", "中性"}
_VALID_DURATION = {"短期", "中期", "长期"}
_VALID_GAP = {"高", "中高", "中", "中低", "低"}


def _score_to_level(score: int) -> str:
    """0-100 分按区间映射等级。"""
    for level, lo, hi in _LEVEL_RANGE:
        if lo <= score <= hi:
            return level
    return "中"


def _normalize_theme(theme: Dict) -> Dict:
    """字段补全与等级/分数自洽校正。

    规则:
        - strength_score 缺失或越界 -> 按 level 取区间中位数
        - 两者都缺失 -> score=50, level=中
        - 冲突时 strength_score 为准，level 由分数覆写
    """
    out = dict(theme)

    score = out.get("strength_score")
    level = out.get("strength_level")

    if isinstance(score, str):
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = None
    if isinstance(score, int):
        score = max(0, min(100, score))

    if score is None and level in _VALID_LEVELS:
        for lv, lo, hi in _LEVEL_RANGE:
            if lv == level:
                score = (lo + hi) // 2
                break
    if score is None:
        score = 50

    out["strength_score"] = score
    out["strength_level"] = _score_to_level(score)

    sentiment = out.get("sentiment")
    if sentiment not in _VALID_SENTIMENT:
        out["sentiment"] = "利好"

    if out.get("duration") not in _VALID_DURATION and out.get("duration"):
        out["duration"] = None
    if out.get("expectation_gap") not in _VALID_GAP and out.get("expectation_gap"):
        out["expectation_gap"] = None

    out["is_cold"] = 1 if out.get("is_cold") else 0

    return out


class ThemeStore:
    """题材预测库读写。"""

    def __init__(self):
        init_database()

    def save_themes(
        self,
        report_meta: Dict,
        themes: List[Dict],
        news_id_map: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """批量入库一份报告抽取出的题材。

        Args:
            report_meta: {
                'report_id': str,
                'report_date': 'YYYY-MM-DD',
                'report_time': 'HH:MM',
                'report_path': str
            }
            themes: 每个元素至少含 theme_name / strength_score 或 strength_level / reason；
                    可选 stocks: List[{name,code?,role?,reason?}]
                         news:   List[{ref,relation_type?}]
            news_id_map: 报告底部反查得到的 {'新闻107': 'curated_id', ...}，
                         用于补全 theme_news.news_id。若 AI 已直接给出 news.id 则优先用。

        Returns:
            {'themes': N, 'stocks': M, 'news': K}
        """
        if not themes:
            return {"themes": 0, "stocks": 0, "news": 0}

        report_id = report_meta.get("report_id") or ""
        report_date = report_meta.get("report_date") or ""
        report_time = report_meta.get("report_time")
        report_path = report_meta.get("report_path") or ""
        if not report_id or not report_date or not report_path:
            raise ValueError("report_meta 必须包含 report_id / report_date / report_path")

        news_id_map = news_id_map or {}
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        theme_count = stock_count = news_count = 0

        with get_connection() as conn:
            for raw in themes:
                t = _normalize_theme(raw)
                theme_name = (t.get("theme_name") or "").strip()
                reason = (t.get("reason") or "").strip()
                if not theme_name or not reason:
                    continue

                cur = conn.execute(
                    """
                    INSERT INTO theme_predictions
                    (report_id, report_date, report_time, report_path,
                     theme_name, theme_category,
                     strength_score, strength_level,
                     priority_rank, duration, expectation_gap,
                     sentiment, is_cold,
                     reason, risk_note,
                     created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        report_date,
                        report_time,
                        report_path,
                        theme_name,
                        t.get("theme_category"),
                        t["strength_score"],
                        t["strength_level"],
                        t.get("priority_rank"),
                        t.get("duration"),
                        t.get("expectation_gap"),
                        t["sentiment"],
                        t["is_cold"],
                        reason,
                        t.get("risk_note"),
                        created_at,
                    ),
                )
                theme_id = cur.lastrowid
                theme_count += 1

                for stock in t.get("stocks") or []:
                    name = (stock.get("name") or stock.get("stock_name") or "").strip()
                    if not name:
                        continue
                    conn.execute(
                        """
                        INSERT INTO theme_stocks
                        (theme_id, stock_name, stock_code, role, reason)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            theme_id,
                            name,
                            stock.get("code") or stock.get("stock_code"),
                            stock.get("role"),
                            stock.get("reason"),
                        ),
                    )
                    stock_count += 1

                for news in t.get("news") or []:
                    # 兼容外部直接传字符串数组的情况（Agent / 测试）
                    if isinstance(news, str):
                        news = {"ref": news}
                    ref = (news.get("ref") or news.get("news_ref") or "").strip()
                    if not ref:
                        continue
                    # 优先级：AI 直接给 > 报告反查映射
                    db_id = (news.get("id") or news.get("news_id")
                             or news_id_map.get(ref))
                    conn.execute(
                        """
                        INSERT INTO theme_news
                        (theme_id, news_ref, news_id, relation_type)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            theme_id,
                            ref,
                            db_id,
                            news.get("relation_type"),
                        ),
                    )
                    news_count += 1

        return {"themes": theme_count, "stocks": stock_count, "news": news_count}

    def count(self) -> int:
        with get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM theme_predictions").fetchone()[0]

    def get_by_report(self, report_id: str) -> List[Dict]:
        """按报告 ID 取出所有题材（含关联标的 / 新闻）。"""
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM theme_predictions WHERE report_id = ? ORDER BY priority_rank IS NULL, priority_rank, strength_score DESC",
                (report_id,),
            ).fetchall()
            return [self._expand(conn, r) for r in rows]

    def get_by_date(self, report_date: str) -> List[Dict]:
        """按日期取所有题材（同日多次分析会全部返回）。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM theme_predictions
                WHERE report_date = ?
                ORDER BY report_time DESC, strength_score DESC
                """,
                (report_date,),
            ).fetchall()
            return [self._expand(conn, r) for r in rows]

    def get_theme_history(self, theme_name: str, limit: int = 30) -> List[Dict]:
        """按题材名跨日查询历史热度演化。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, report_date, report_time, strength_score, strength_level,
                       priority_rank, sentiment, is_cold, reason
                FROM theme_predictions
                WHERE theme_name = ?
                ORDER BY report_date DESC, report_time DESC
                LIMIT ?
                """,
                (theme_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stock_themes(self, stock_name: str, limit: int = 50) -> List[Dict]:
        """反查：某只票踩过哪些题材。"""
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT tp.report_date, tp.report_time, tp.theme_name,
                       tp.strength_score, tp.strength_level,
                       ts.role, ts.reason AS stock_reason
                FROM theme_stocks ts
                JOIN theme_predictions tp ON tp.id = ts.theme_id
                WHERE ts.stock_name = ?
                ORDER BY tp.report_date DESC, tp.report_time DESC
                LIMIT ?
                """,
                (stock_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_by_report(self, report_id: str) -> int:
        """删除指定报告的全部题材（CASCADE 自动清掉子表）。"""
        with get_connection() as conn:
            cur = conn.execute(
                "DELETE FROM theme_predictions WHERE report_id = ?", (report_id,)
            )
            return cur.rowcount

    @staticmethod
    def _expand(conn, theme_row) -> Dict:
        """填充关联标的与新闻（v2: stocks 删 elasticity，news 删 news_title）。"""
        theme = dict(theme_row)
        tid = theme["id"]
        stocks = conn.execute(
            """
            SELECT stock_name, stock_code, role, reason
            FROM theme_stocks WHERE theme_id = ?
            """,
            (tid,),
        ).fetchall()
        news = conn.execute(
            """
            SELECT news_ref, news_id, relation_type
            FROM theme_news WHERE theme_id = ?
            """,
            (tid,),
        ).fetchall()
        theme["stocks"] = [dict(s) for s in stocks]
        theme["news"] = [dict(n) for n in news]
        return theme
