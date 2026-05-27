"""GUI 端直查 market.db 的只读辅助函数（M6 GUI 优化第 1 批）。

为什么放 gui/utils 而不是 services/market？
    这些查询的产物只在 GUI 渲染中使用，不进入 MarketSummary canonical JSON，
    避免污染后端结构 / 不破坏 prompt cache。所有函数均 readonly。

调用关系::

    market_summary_page.py
        ├─ A3 _populate_sectors → query_sector_leaders()
        ├─ A4 _populate_dragon_tiger → query_other_traders()
        └─ A4 _populate_dragon_tiger → query_stock_names() (顺手补股票名)
    ai_analysis_page.py
        └─ showEvent → last_settled_trade_date() + get_cached_summary_md()
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from services.market.market_db import get_market_db

_log = logging.getLogger(__name__)


def query_other_traders(
    trade_date: str,
    *,
    min_net_buy_yi: float = 0.0,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """查未命中 dim_trader_alias.is_famous=1 的普通龙虎榜机构席位。

    输入:
        trade_date       YYYYMMDD
        min_net_buy_yi   按 |net_buy_yi| 过滤的下限（亿元），默认 0 = 不过滤
        limit            最多返回条数
    输出:
        list of dict —— 字段 ts_code, exalter, side, net_buy_yi,
                         buy_amount_yi, sell_amount_yi
        按 |net_buy_yi| DESC 排序。
    """
    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT t.ts_code, t.exalter, t.side, "
                "       t.net_buy_yi, t.buy_amount_yi, t.sell_amount_yi "
                "FROM fact_top_inst t "
                "LEFT JOIN dim_trader_alias a "
                "  ON a.exalter = t.exalter AND a.is_famous = 1 "
                "WHERE t.trade_date = ? "
                "  AND a.exalter IS NULL "
                "  AND ABS(COALESCE(t.net_buy_yi, 0)) >= ? "
                "ORDER BY ABS(COALESCE(t.net_buy_yi, 0)) DESC "
                "LIMIT ?",
                (trade_date, float(min_net_buy_yi), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        _log.warning("query_other_traders 失败 td=%s: %s", trade_date, exc)
        return []


def query_sector_leaders(
    sector_name: Optional[str],
    trade_date: str,
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """按板块名（模糊匹配 fact_limit_stock.theme）取该板块的涨停股做「龙头股代理」。

    口径说明:
        因为 fact_limit_stock 只记录涨停/炸板/跌停股，本函数返回的"龙头"实际是
        「该板块的涨停股按连板高度 / 封单金额排序的前 N 只」。对于全板块都没有
        涨停的弱势板块，本函数会返回空列表（这正是预期）。

    输入:
        sector_name 板块名（来自 sectors_top[].name），空串/None → 空结果
        trade_date  YYYYMMDD
        limit       最多返回数量（默认 3）
    输出:
        list of dict —— 字段 ts_code, name, cons_nums, limit_up_time,
                         pct_chg, status
        按 (cons_nums DESC, fd_amount_yi DESC) 排序。
    """
    if not sector_name:
        return []
    db = get_market_db()
    pattern = f"%{sector_name}%"
    try:
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT l.ts_code, "
                "       COALESCE(d.name, "
                "         json_extract(l.raw_json, '$.name')) AS name, "
                "       l.cons_nums, l.limit_up_time, "
                "       l.pct_chg, l.status "
                "FROM fact_limit_stock l "
                "LEFT JOIN dim_stock d ON d.ts_code = l.ts_code "
                "WHERE l.trade_date = ? "
                "  AND l.limit_type = 'U' "
                "  AND l.theme LIKE ? "
                "ORDER BY COALESCE(l.cons_nums, 1) DESC, "
                "         COALESCE(l.fd_amount_yi, 0) DESC "
                "LIMIT ?",
                (trade_date, pattern, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "query_sector_leaders 失败 sector=%s td=%s: %s",
            sector_name, trade_date, exc,
        )
        return []


def query_sectors_extended(
    trade_date: str,
    *,
    top_n: int = 20,
    bottom_n: int = 10,
) -> Dict[str, List[Dict[str, Any]]]:
    """直查 fact_sector_daily 取当日涨幅 Top N + 跌幅 Top N。

    用途:
        后端 _read_sectors_top 默认只取 Top 10 进 summary，本函数为 GUI 提供
        扩展榜单数据，供「Top 20 / Bottom 10」面板填充。

    口径说明:
        因为后端 _enrich_sector_5d_pct 只对当日 Top 10 算 pct_chg_5d，
        本函数返回的 Top 11~N 及全部 Bottom 板块的 pct_chg_5d 通常为 None；
        catalysts/catalysts_source 字段也保持空（未跑 cls/ai 注入）。
        所以"扩展段"板块的 GUI 显示中：5 日列、high_risk 行底色、催化色块
        都会自动走 placeholder 路径，这是预期行为。

    输入:
        trade_date  YYYYMMDD
        top_n       涨幅榜返回前 N 名（默认 20）
        bottom_n    跌幅榜返回前 N 名（默认 10）
    输出:
        {"top": [...], "bottom": [...]}
        每条 dict 字段对齐 sectors_top[]：
        rank / ts_code / name / pct_chg / pct_chg_5d / limit_up_count /
        main_net_yi / main_elg_yi / main_lg_yi / leaders / catalysts /
        catalysts_source / high_risk
    """
    if not trade_date:
        return {"top": [], "bottom": []}

    base_sql = (
        "SELECT s.ts_code, s.pct_chg, s.main_net_yi, s.main_elg_yi, "
        "       s.main_lg_yi, s.pct_chg_5d, "
        "       s.rank_today, d.name "
        "FROM fact_sector_daily s "
        "LEFT JOIN dim_sector d ON d.ts_code = s.ts_code "
        "WHERE s.trade_date = ? "
    )

    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            top_rows = conn.execute(
                base_sql
                + "ORDER BY s.pct_chg DESC NULLS LAST LIMIT ?",
                (trade_date, int(top_n)),
            ).fetchall()
            bottom_rows = conn.execute(
                base_sql
                + "ORDER BY s.pct_chg ASC NULLS LAST LIMIT ?",
                (trade_date, int(bottom_n)),
            ).fetchall()

            def _normalize(rows) -> List[Dict[str, Any]]:
                out = []
                for i, r in enumerate(rows, 1):
                    name = str(r["name"] or "")
                    lu, leaders = _derive_sector_leaders(conn, trade_date, name)
                    out.append({
                        "rank": i,
                        "ts_code": str(r["ts_code"] or ""),
                        "name": name,
                        "pct_chg": r["pct_chg"],
                        "pct_chg_5d": r["pct_chg_5d"],
                        "limit_up_count": lu,
                        "main_net_yi": r["main_net_yi"],
                        "main_elg_yi": r["main_elg_yi"],
                        "main_lg_yi": r["main_lg_yi"],
                        "leaders": leaders,
                        "catalysts": [],
                        "catalysts_source": "none",
                        "high_risk": None,
                    })
                return out

            return {
                "top": _normalize(top_rows),
                "bottom": _normalize(bottom_rows),
            }
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "query_sectors_extended 失败 td=%s: %s", trade_date, exc,
        )
        return {"top": [], "bottom": []}


def _derive_sector_leaders(
    conn: Any,
    trade_date: str,
    sector_name: str,
    *,
    limit: int = 3,
) -> tuple:
    """从 fact_limit_stock 按 theme LIKE 派生 (涨停数, leaders 列表)。

    与 services.market.service._derive_sector_leaders 同口径，供 GUI
    扩展段（Top 11~20 / Bottom 10）补全 limit_up_count 与 leaders 字段。
    返回 leader dict 字段：name / pct_chg / status / cons_nums。
    """
    if not sector_name:
        return (None, [])
    try:
        pattern = f"%{sector_name}%"
        cnt = conn.execute(
            "SELECT COUNT(*) FROM fact_limit_stock "
            "WHERE trade_date = ? AND limit_type = 'U' AND theme LIKE ?",
            (trade_date, pattern),
        ).fetchone()
        limit_up_count = int(cnt[0]) if cnt and cnt[0] else None

        rows = conn.execute(
            "SELECT COALESCE(d.name, "
            "         json_extract(l.raw_json, '$.name')) AS name, "
            "       l.pct_chg, l.status, l.cons_nums "
            "FROM fact_limit_stock l "
            "LEFT JOIN dim_stock d ON d.ts_code = l.ts_code "
            "WHERE l.trade_date = ? AND l.limit_type = 'U' "
            "  AND l.theme LIKE ? "
            "ORDER BY COALESCE(l.cons_nums, 1) DESC, "
            "         COALESCE(l.fd_amount_yi, 0) DESC "
            "LIMIT ?",
            (trade_date, pattern, int(limit)),
        ).fetchall()
        leaders = [
            {
                "name": str(r["name"] or ""),
                "pct_chg": r["pct_chg"],
                "status": r["status"],
                "cons_nums": r["cons_nums"],
            }
            for r in rows
        ]
        return (limit_up_count, leaders)
    except Exception:  # noqa: BLE001
        return (None, [])


def query_all_stocks(trade_date: str) -> List[Dict[str, Any]]:
    """直查 fact_stock_daily 取某交易日全 A 股行情（GUI 全部个股 Tab 用）。

    数据合并::

        fact_stock_daily   ← 主表（ts_code/pct_chg/close/amount）
          LEFT JOIN dim_stock        on ts_code → name / industry / market
          LEFT JOIN fact_limit_stock on (ts_code,trade_date) → limit_type 涨停标

    输入:
        trade_date  YYYYMMDD（必传，空串/None → 返回 []）

    输出:
        list of dict —— 每条字段::

            ts_code     str   600172.SH
            name        str   股票名（dim_stock 缺失时为 ""）
            industry    str   申万行业（缺失为 ""）
            market      str   主板/创业板/科创板/北证（缺失为 ""）
            pct_chg     Optional[float]   涨跌幅 %
            close       Optional[float]   收盘价 元
            amount_yi   Optional[float]   成交额 亿元（amount/100000，amount 单位千元）
            limit_type  Optional[str]     U=涨停 / Z=炸板 / D=跌停 / None=普通

        默认按 pct_chg DESC NULLS LAST 排序（GUI 默认倒序展示）。
    """
    if not trade_date:
        return []

    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT s.ts_code, "
                "       COALESCE(d.name, '') AS name, "
                "       COALESCE(d.industry, '') AS industry, "
                "       COALESCE(d.market, '') AS market, "
                "       s.pct_chg, s.close, s.amount, "
                "       l.limit_type "
                "FROM fact_stock_daily s "
                "LEFT JOIN dim_stock d ON d.ts_code = s.ts_code "
                "LEFT JOIN fact_limit_stock l "
                "  ON l.ts_code = s.ts_code "
                "  AND l.trade_date = s.trade_date "
                "WHERE s.trade_date = ? "
                "ORDER BY s.pct_chg DESC NULLS LAST",
                (trade_date,),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        _log.warning("query_all_stocks 失败 td=%s: %s", trade_date, exc)
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        amount = r["amount"]
        amount_yi = (
            float(amount) / 100000.0 if amount is not None else None
        )
        out.append({
            "ts_code": str(r["ts_code"] or ""),
            "name": str(r["name"] or ""),
            "industry": str(r["industry"] or ""),
            "market": str(r["market"] or ""),
            "pct_chg": r["pct_chg"],
            "close": r["close"],
            "amount_yi": amount_yi,
            "limit_type": (str(r["limit_type"]) if r["limit_type"] else None),
        })
    return out


def query_all_sectors(trade_date: str) -> List[Dict[str, Any]]:
    """直查 fact_sector_daily 取某交易日全部板块行情（GUI 全部板块 Tab 用）。

    与 :func:`query_sectors_extended` 同口径，但取**全部**约 480 个板块，
    一次 SQL 完成；leaders/limit_up_count 仍走 :func:`_derive_sector_leaders`
    LIKE 查询，~480 次 LIKE 在 SSD 上约 1~2s（已与主人对齐接受）。

    输入:
        trade_date  YYYYMMDD（必传，空串/None → []）

    输出:
        list of dict —— 字段对齐 sectors_top[]::

            rank ts_code name pct_chg pct_chg_5d limit_up_count
            main_net_yi main_elg_yi main_lg_yi leaders
            catalysts(=[]) catalysts_source(="none") high_risk(=None)

        按 pct_chg DESC NULLS LAST 排序，rank 从 1 计。
    """
    if not trade_date:
        return []

    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT s.ts_code, s.pct_chg, s.main_net_yi, "
                "       s.main_elg_yi, s.main_lg_yi, s.pct_chg_5d, "
                "       s.rank_today, d.name "
                "FROM fact_sector_daily s "
                "LEFT JOIN dim_sector d ON d.ts_code = s.ts_code "
                "WHERE s.trade_date = ? "
                "ORDER BY s.pct_chg DESC NULLS LAST",
                (trade_date,),
            ).fetchall()

            out: List[Dict[str, Any]] = []
            for i, r in enumerate(rows, 1):
                name = str(r["name"] or "")
                lu, leaders = _derive_sector_leaders(conn, trade_date, name)
                out.append({
                    "rank": i,
                    "ts_code": str(r["ts_code"] or ""),
                    "name": name,
                    "pct_chg": r["pct_chg"],
                    "pct_chg_5d": r["pct_chg_5d"],
                    "limit_up_count": lu,
                    "main_net_yi": r["main_net_yi"],
                    "main_elg_yi": r["main_elg_yi"],
                    "main_lg_yi": r["main_lg_yi"],
                    "leaders": leaders,
                    "catalysts": [],
                    "catalysts_source": "none",
                    "high_risk": None,
                })
            return out
    except Exception as exc:  # noqa: BLE001
        _log.warning("query_all_sectors 失败 td=%s: %s", trade_date, exc)
        return []


def query_stock_names(ts_codes: List[str]) -> Dict[str, str]:
    """批量查 ts_code → name 映射。

    用于补全后端 dragon_tiger.stocks[].name（service.py 当前写死 None）。
    输入空列表时直接返回空字典。
    """
    if not ts_codes:
        return {}
    db = get_market_db()
    try:
        qmarks = ",".join("?" * len(ts_codes))
        with db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT ts_code, name FROM dim_stock "
                f"WHERE ts_code IN ({qmarks})",
                list(ts_codes),
            ).fetchall()
        return {str(r["ts_code"]): str(r["name"] or "") for r in rows}
    except Exception as exc:  # noqa: BLE001
        _log.warning("query_stock_names 失败: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 自动填入 AI 分析页面盘后总结（M6 / 2026-05-27）
# ---------------------------------------------------------------------------


def last_settled_trade_date(
    now: Optional[datetime] = None,
) -> Optional[str]:
    """算「最近一个已收盘的交易日」（GUI 自动填入用）。

    边界规则
    --------
    * ``now.hour >= 16``（下午 4 点及之后）→ 候选 = 今天
    * 否则 → 候选 = 昨天
    * 把候选作为上界，向前找 ``dim_trade_calendar.is_open=1`` 的最近一行
      （周末 / 春节假期等会自动跳过到上一个开市日）

    举例
    ----
    * 2026-05-27 02:00（周三凌晨，hour<16）→ 候选=20260526（周二）→ 周二开市 → 返回 20260526
    * 2026-05-27 16:30（周三下午 4 点后）→ 候选=20260527 → 周三开市 → 返回 20260527
    * 2026-05-30 11:00（周六）→ 候选=20260529（周五，hour<16）→ 周五开市 → 返回 20260529
    * 2026-05-31 22:00（周日）→ 候选=20260531（周日，hour>=16）→ 非开市 → 自动回退到 20260529

    Args:
        now: 测试时可注入；省略则取 ``datetime.now()``。

    Returns:
        ``YYYYMMDD`` 字符串；``dim_trade_calendar`` 表为空或回看仍未命中时返回 ``None``。
    """
    now = now or datetime.now()
    if now.hour >= 16:
        candidate = now
    else:
        candidate = now - timedelta(days=1)
    target = candidate.strftime("%Y%m%d")

    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT trade_date FROM dim_trade_calendar "
                "WHERE trade_date <= ? AND is_open = 1 "
                "ORDER BY trade_date DESC LIMIT 1",
                (target,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        _log.warning("last_settled_trade_date 查询失败: %s", exc)
        return None
    return str(row[0]) if row else None


def get_cached_summary_md(trade_date: str) -> Optional[str]:
    """从 ``market_summaries`` 表读取某日已渲染的 compact Markdown。

    Args:
        trade_date: ``YYYYMMDD``

    Returns:
        ``summary_md`` 字符串；该日无缓存（market_fetch 还没跑过）时返回 ``None``。
    """
    if not trade_date:
        return None
    db = get_market_db()
    try:
        with db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT summary_md FROM market_summaries "
                "WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "get_cached_summary_md(%s) 失败: %s", trade_date, exc
        )
        return None
    return str(row[0]) if row and row[0] else None


__all__ = [
    "query_other_traders",
    "query_sector_leaders",
    "query_sectors_extended",
    "query_all_stocks",
    "query_all_sectors",
    "query_stock_names",
    "last_settled_trade_date",
    "get_cached_summary_md",
]
