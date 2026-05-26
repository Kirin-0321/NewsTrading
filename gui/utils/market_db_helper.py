"""GUI 端直查 market.db 的只读辅助函数（M6 GUI 优化第 1 批）。

为什么放 gui/utils 而不是 services/market？
    这些查询的产物只在 GUI 渲染中使用，不进入 MarketSummary canonical JSON，
    避免污染后端结构 / 不破坏 prompt cache。所有函数均 readonly。

调用关系::

    market_summary_page.py
        ├─ A3 _populate_sectors → query_sector_leaders()
        ├─ A4 _populate_dragon_tiger → query_other_traders()
        └─ A4 _populate_dragon_tiger → query_stock_names() (顺手补股票名)
"""

from __future__ import annotations

import logging
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


__all__ = [
    "query_other_traders",
    "query_sector_leaders",
    "query_stock_names",
]
