"""题材预测库（SQLite）。

存三张表:
    theme_predictions  题材主表（每份报告快照）
    theme_stocks       题材-标的 关联（含 normalized_code）
    theme_news         题材-新闻 关联

v4 关键变化（2026-05-27）:
    - 删 sentiment 字段（由 strength_score 正负号承载）
    - strength_score 改 -100 ~ +100（带符号）
    - strength_level 改 9 档枚举（重大利空 ... 重大利多）
    - 新增 prompt_id / prompt_version（从 ai_reports 反查冗余）
    - 新增 sector_ts_code / sector_match_conf（matcher 富化结果）
    - normalized_code 走 matcher（在 theme_extractor 阶段已写入 stock["normalized_code"]）
    - P1 修复：report_path 入库前统一 _to_relative_posix
    - P2 修复：save_themes 入口 DELETE 同 report_id 旧数据，幂等

设计要点:
    - 同一 report_id 内允许多题材；跨日同题材独立成行（快照式）
    - priority_rank 仅在该 report_id 内有效，全库可重复
    - save_themes() 全程事务：主表 + 标的 + 新闻 三表一起入库失败回滚
    - 强度 score / level 在 _normalize_theme() 自动互相校正（score 为准）
"""

from datetime import datetime
from typing import Dict, List, Optional

from core.theme_extractor import _VALID_LEVELS, _score_to_level
from services.storage.ai_inference_db import get_ai_inference_db

_VALID_DURATION = {"短期", "中期", "长期"}
_VALID_GAP = {"高", "中高", "中", "中低", "低"}


def _ensure_yyyymmdd(s: str, field: str = "report_date") -> None:
    """协议校验：``report_date`` 必须是 8 位 YYYYMMDD（schema 强制）。

    2026-05-27 18:30 加入：历史上存在 ``YYYY-MM-DD`` 入库违反协议的债
    （已修），此处入口防御让以后再违反立刻炸而不是隐性下游报错。
    """
    if not (isinstance(s, str) and len(s) == 8 and s.isdigit()):
        raise ValueError(
            f"{field} 应为 YYYYMMDD 8 位数字，得到 {s!r}"
        )


def _normalize_theme(theme: Dict) -> Dict:
    """字段补全 + 等级/分数自洽校正（v4 适配带符号 score）。

    规则:
        - strength_score 缺失 -> 0（中性）
        - 越界 -> clamp 到 [-100, +100]
        - strength_level 始终由 score 派生（不接受外部冲突值）
    """
    out = dict(theme)

    score = out.get("strength_score")
    if isinstance(score, str):
        try:
            score = int(score)
        except (TypeError, ValueError):
            score = None
    if score is None:
        score = 0
    elif isinstance(score, int):
        score = max(-100, min(100, score))
    else:
        score = 0

    out["strength_score"] = score
    out["strength_level"] = _score_to_level(score)

    if out.get("duration") not in _VALID_DURATION and out.get("duration"):
        out["duration"] = None
    if out.get("expectation_gap") not in _VALID_GAP and out.get("expectation_gap"):
        out["expectation_gap"] = None

    out["is_cold"] = 1 if out.get("is_cold") else 0

    # 移除 v1 残留字段（防御性）
    out.pop("sentiment", None)

    return out


def _lookup_prompt_meta(report_path: str) -> Dict[str, Optional[str]]:
    """从 ai_reports 反查 prompt_id / prompt_version（P1 路径规范化双保险）。

    Returns:
        {"prompt_id": str | None, "prompt_version": str | None}
        反查失败一律 None，不抛异常。
    """
    if not report_path:
        return {"prompt_id": None, "prompt_version": None}
    try:
        from services.storage.ai_reports_store import (
            _to_relative_posix,
            get_ai_reports_store,
        )
        rel = _to_relative_posix(report_path)
        rec = get_ai_reports_store().get_by_path(rel)
        if rec is not None:
            return {
                "prompt_id": getattr(rec, "prompt_id", None),
                "prompt_version": getattr(rec, "prompt_version", None),
            }
    except Exception:
        pass
    return {"prompt_id": None, "prompt_version": None}


class ThemeStore:
    """题材预测库读写（v5: 已迁库到 ai_inference.db）。"""

    def __init__(self):
        get_ai_inference_db().ensure_schema()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def save_themes(
        self,
        report_meta: Dict,
        themes: List[Dict],
        news_id_map: Optional[Dict[str, str]] = None,
    ) -> Dict:
        """批量入库一份报告抽取出的题材（v5: 支持 is_backtest 透传）。

        Args:
            report_meta: {
                'report_id': str（文件名 string），
                'report_date': 'YYYYMMDD'（schema 协议；入口 _ensure_yyyymmdd 强校验），
                'report_time': 'HH:MM',
                'report_path': str（建议已是相对 posix；本函数会再规范化一次），
                'is_backtest': bool（可选，默认 False；True=虚拟回测产物）
            }
            themes: 每个元素至少含 theme_name / strength_score / reason；
                    可选 stocks / news / sector_ts_code / sector_match_conf
            news_id_map: 报告底部反查得到的 {'新闻107': 'curated_id', ...}

        Returns:
            {'themes': N, 'stocks': M, 'news': K, 'prompt_id': '...' | None}
        """
        if not themes:
            return {"themes": 0, "stocks": 0, "news": 0, "prompt_id": None}

        report_id = report_meta.get("report_id") or ""
        report_date = report_meta.get("report_date") or ""
        report_time = report_meta.get("report_time")
        raw_path = report_meta.get("report_path") or ""
        is_backtest = 1 if report_meta.get("is_backtest") else 0
        if not report_id or not report_date or not raw_path:
            raise ValueError(
                "report_meta 必须包含 report_id / report_date / report_path"
            )
        _ensure_yyyymmdd(report_date)

        # P1: 双保险路径规范化（即使上游传了绝对路径也能修正）
        try:
            from services.storage.ai_reports_store import _to_relative_posix
            report_path = _to_relative_posix(raw_path)
        except Exception:
            report_path = raw_path.replace("\\", "/")

        # 反查 prompt_id（找不到则 None，不影响入库）
        prompt_meta = _lookup_prompt_meta(report_path)
        prompt_id = prompt_meta["prompt_id"]
        prompt_version = prompt_meta["prompt_version"]

        news_id_map = news_id_map or {}
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        theme_count = stock_count = news_count = 0

        with get_ai_inference_db().connect() as conn:
            # P2: 同 report_id 重复抽取防护
            # 先清旧数据（CASCADE 自动清 stocks / news 子表）
            conn.execute(
                "DELETE FROM theme_predictions WHERE report_id = ?",
                (report_id,),
            )

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
                     is_cold, reason, risk_note,
                     prompt_id, prompt_version,
                     sector_ts_code, sector_match_conf,
                     is_backtest, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        t["is_cold"],
                        reason,
                        t.get("risk_note"),
                        prompt_id,
                        prompt_version,
                        t.get("sector_ts_code"),
                        t.get("sector_match_conf"),
                        is_backtest,
                        created_at,
                    ),
                )
                theme_id = cur.lastrowid
                theme_count += 1

                for stock in t.get("stocks") or []:
                    name = (
                        stock.get("name") or stock.get("stock_name") or ""
                    ).strip()
                    if not name:
                        continue
                    conn.execute(
                        """
                        INSERT INTO theme_stocks
                        (theme_id, stock_name, stock_code, normalized_code,
                         role, reason)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            theme_id,
                            name,
                            stock.get("code") or stock.get("stock_code"),
                            stock.get("normalized_code"),
                            stock.get("role"),
                            stock.get("reason"),
                        ),
                    )
                    stock_count += 1

                for news in t.get("news") or []:
                    if isinstance(news, str):
                        news = {"ref": news}
                    ref = (news.get("ref") or news.get("news_ref") or "").strip()
                    if not ref:
                        continue
                    db_id = (
                        news.get("id") or news.get("news_id")
                        or news_id_map.get(ref)
                    )
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

        return {
            "themes": theme_count,
            "stocks": stock_count,
            "news": news_count,
            "prompt_id": prompt_id,
        }

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def count(self) -> int:
        with get_ai_inference_db().connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM theme_predictions"
            ).fetchone()[0]

    def get_by_report(self, report_id: str) -> List[Dict]:
        """按报告 ID 取所有题材（含关联标的 / 新闻）。"""
        with get_ai_inference_db().connect() as conn:
            rows = conn.execute(
                "SELECT * FROM theme_predictions WHERE report_id = ? "
                "ORDER BY priority_rank IS NULL, priority_rank, "
                "strength_score DESC",
                (report_id,),
            ).fetchall()
            return [self._expand(conn, r) for r in rows]

    def get_by_date(
        self,
        report_date: str,
        prompt_id: Optional[str] = None,
    ) -> List[Dict]:
        """按日期取所有题材；可选按模板过滤（P6 新增 prompt_id 参数）。"""
        with get_ai_inference_db().connect() as conn:
            where = "WHERE report_date = ?"
            params: List = [report_date]
            if prompt_id:
                where += " AND prompt_id = ?"
                params.append(prompt_id)
            rows = conn.execute(
                f"""
                SELECT * FROM theme_predictions
                {where}
                ORDER BY report_time DESC, strength_score DESC
                """,
                params,
            ).fetchall()
            return [self._expand(conn, r) for r in rows]

    def get_theme_history(
        self,
        theme_name: str,
        limit: int = 30,
    ) -> List[Dict]:
        """按题材名跨日查询历史热度演化。"""
        with get_ai_inference_db().connect() as conn:
            rows = conn.execute(
                """
                SELECT id, report_date, report_time, strength_score,
                       strength_level, priority_rank, is_cold, reason,
                       prompt_id, prompt_version, sector_ts_code
                FROM theme_predictions
                WHERE theme_name = ?
                ORDER BY report_date DESC, report_time DESC
                LIMIT ?
                """,
                (theme_name, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stock_themes(
        self,
        stock_name: str,
        limit: int = 50,
    ) -> List[Dict]:
        """反查：某只票踩过哪些题材。"""
        with get_ai_inference_db().connect() as conn:
            rows = conn.execute(
                """
                SELECT tp.report_date, tp.report_time, tp.theme_name,
                       tp.strength_score, tp.strength_level,
                       tp.prompt_id, ts.normalized_code,
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

    def list_distinct_prompts(self) -> List[str]:
        """返回所有出现过的 prompt_id（去重）；模板筛选下拉用。"""
        with get_ai_inference_db().connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT prompt_id FROM theme_predictions "
                "WHERE prompt_id IS NOT NULL "
                "ORDER BY prompt_id"
            ).fetchall()
            return [r[0] for r in rows]

    def delete_by_report(self, report_id: str) -> int:
        """删除指定报告的全部题材（CASCADE 自动清掉子表）。"""
        with get_ai_inference_db().connect() as conn:
            cur = conn.execute(
                "DELETE FROM theme_predictions WHERE report_id = ?",
                (report_id,),
            )
            return cur.rowcount

    @staticmethod
    def _expand(conn, theme_row) -> Dict:
        """填充关联标的与新闻（v4: stocks 多 normalized_code）。"""
        theme = dict(theme_row)
        tid = theme["id"]
        stocks = conn.execute(
            """
            SELECT stock_name, stock_code, normalized_code, role, reason
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
