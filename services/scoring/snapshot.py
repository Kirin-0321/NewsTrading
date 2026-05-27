"""历史快照重建（Phase 5 Step 5.1 + 2026-05-27 时间窗语义重构）。

业务定位
--------
Phase 6 虚拟回测的"时光机"——给定 ``trade_date`` 和一段
``[news_start_dt, news_end_dt)`` 新闻窗口，重建当时能看到的：

1. **新闻列表**：``raw_news`` 中
   ``published_ts ∈ [news_start_dt, news_end_dt)``
   且 ``clean_status`` 满足过滤的全部行
2. **盘后总结**：``market_summaries`` 中**严格早于** news_end_dt 的最新一份
   * news_end_dt < trade_date 16:00 → 当天盘后还没出 → 用前一交易日
   * news_end_dt ≥ trade_date 16:00 → 当天 16:00 盘后已结算 → 用当天

时间窗主人语义（**2026-05-27 重构**）
-----------------------------------
* **左边界（news_start_dt）默认 = trade_date 14:00**（午后开始）
* **右边界（news_end_dt）默认 = next_trade_date(trade_date) 09:00**
  （下一交易日开盘前，跨整个夜间/周末）
* **右边界硬上限 = next_trade_date(trade_date) 09:00**（铁律）
* 主人可手动收窄/外推，但右边界不允许超过上限

举例（trade_date=周五 5/22）
----------------------------
* 默认窗 = ``[5/22 14:00, 5/25 09:00)``（覆盖整个周末）
* market_summary_date = ``5/22``（当天盘后）
* 主人若把右拖到 5/22 20:00 → 窗变 ``[5/22 14:00, 5/22 20:00)``，可
* 主人若把右拖到 5/25 10:00 → **超上限，抛 SnapshotError**

防穿越铁律（**写代码时一定要保证**）
----------------------------------
* news_start_dt < news_end_dt（左严格小于右）
* news_end_dt ≤ next_trade_date(trade_date).09:00（不允许覆盖下下次开盘后）
* 所有 news.published_ts < news_end_dt（数据兜底 assert）
* market_summary_date 必须 ≤ news_end_dt 对应日（推导逻辑保证）

接口
----
:func:`build_snapshot` 入口；
:func:`compute_default_news_window` 算主人默认窗（GUI/CLI 用）；
返回 :class:`HistoricalSnapshot` 数据类。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from services.market.market_db import MarketDB, get_market_db
from services.market.trade_date import (
    TradeDateError,
    get_calendar_row,
    next_trade_date,
    resolve_trade_date,
)
from services.market.tushare_client import TushareClient
from services.storage.database import get_connection

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量（主人默认值）
# ---------------------------------------------------------------------------

#: 默认左边界小时（trade_date 14:00）— 午后短线视角起点
DEFAULT_NEWS_START_HOUR = 14

#: 右边界上限小时（next_trade_date 09:00）— 下一交易日开盘前
DEFAULT_NEWS_END_HOUR = 9

#: 盘后数据可用时间阈值——午后 16:00 之后视为当天盘后已结算
MARKET_SETTLE_HOUR = 16


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class SnapshotError(RuntimeError):
    """快照重建不可恢复错误。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class HistoricalSnapshot:
    """某一历史时间窗能看到的全部信息。"""

    trade_date: str                  # 用户输入的锚定交易日 YYYYMMDD
    news_start_dt: datetime          # 新闻窗左边界（含）
    news_end_dt: datetime            # 新闻窗右边界（不含）
    news_start_ts: int               # 左边界 UNIX 秒
    news_end_ts: int                 # 右边界 UNIX 秒
    news_status: str = "curated"     # 新闻过滤："curated" / "" 全部

    news: List[Dict[str, Any]] = field(default_factory=list)
    news_count: int = 0
    actual_first_ts: Optional[int] = None    # 实际最早 published_ts
    actual_last_ts: Optional[int] = None     # 实际最晚 published_ts

    market_summary_date: Optional[str] = None
    market_summary_md: Optional[str] = None
    market_summary_missing_reason: Optional[str] = None

    #: 右边界硬上限（防穿越用，仅记录便于 inspector / GUI 展示）
    max_news_end_dt: Optional[datetime] = None

    #: 调试/审计用：本快照是否完整（news + market 都齐）
    @property
    def is_complete(self) -> bool:
        return self.news_count > 0 and self.market_summary_md is not None

    # ---- 向后兼容旧属性别名（让既有调用方平滑切换）-----------------------

    @property
    def cutoff_dt(self) -> datetime:
        """旧字段别名 = news_end_dt。"""
        return self.news_end_dt

    @property
    def cutoff_ts(self) -> int:
        """旧字段别名 = news_end_ts。"""
        return self.news_end_ts


# ---------------------------------------------------------------------------
# 默认窗 helper（主人语义）
# ---------------------------------------------------------------------------


def compute_default_news_window(
    trade_date: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
) -> Tuple[datetime, datetime]:
    """算主人定义的默认新闻窗。

    Args:
        trade_date: ``YYYYMMDD`` 锚定日（可以是非交易日）。
        db / client: 解析 next_trade_date 用。

    Returns:
        ``(start_dt, end_dt)``，其中：
            * start_dt = trade_date 14:00:00
            * end_dt   = next_trade_date(trade_date) 09:00:00

    Raises:
        TradeDateError: 缓存不足且未提供 client。
        SnapshotError: trade_date 格式错误。
    """
    if not _is_yyyymmdd(trade_date):
        raise SnapshotError(
            f"trade_date 必须是 YYYYMMDD，得到 {trade_date!r}"
        )
    start_dt = datetime.strptime(trade_date, "%Y%m%d").replace(
        hour=DEFAULT_NEWS_START_HOUR, minute=0, second=0, microsecond=0,
    )
    db = db or get_market_db()
    next_td = next_trade_date(trade_date, 1, db=db, client=client)
    end_dt = datetime.strptime(next_td, "%Y%m%d").replace(
        hour=DEFAULT_NEWS_END_HOUR, minute=0, second=0, microsecond=0,
    )
    return start_dt, end_dt


def compute_max_news_end_dt(
    trade_date: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
) -> datetime:
    """新闻窗右边界硬上限 = next_trade_date(trade_date) 09:00:00。

    与 :func:`compute_default_news_window` 的 end_dt 数值相同，单独抽出
    作为「上限约束」语义点。
    """
    _, end = compute_default_news_window(
        trade_date, db=db, client=client,
    )
    return end


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def build_snapshot(
    trade_date: str,
    *,
    news_start_dt: Optional[datetime] = None,
    news_end_dt: Optional[datetime] = None,
    news_status: str = "curated",
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
) -> HistoricalSnapshot:
    """重建 ``trade_date`` + ``[news_start_dt, news_end_dt)`` 时点的快照。

    Args:
        trade_date: ``YYYYMMDD`` 锚定日（**可以是非交易日**，节假日也允许）。
        news_start_dt: 新闻窗左边界（**含**），``None`` = trade_date 14:00。
        news_end_dt: 新闻窗右边界（**不含**），``None`` = next_trade_date
            09:00。**最大不能超过 next_trade_date 09:00**。
        news_status: ``raw_news.clean_status`` 过滤。
            * ``"curated"``（默认）= 仅精选
            * ``""`` 或 ``None`` = 全部状态（含 rejected / pending）
        db / client: 可选注入；解析 next_trade_date 与 prev_trade_date 用。

    Returns:
        :class:`HistoricalSnapshot`。

    Raises:
        SnapshotError:
            * trade_date 格式错
            * news_start_dt >= news_end_dt
            * news_end_dt 超过 next_trade_date 09:00 硬上限
            * market summary 解析失败（穿越触发）
    """
    if not _is_yyyymmdd(trade_date):
        raise SnapshotError(
            f"trade_date 必须是 YYYYMMDD，得到 {trade_date!r}"
        )

    db = db or get_market_db()
    db.ensure_schema()

    # 1. 算默认窗 + 上限
    default_start, max_end = compute_default_news_window(
        trade_date, db=db, client=client,
    )
    if news_start_dt is None:
        news_start_dt = default_start
    if news_end_dt is None:
        news_end_dt = max_end

    # 2. 校验
    if news_start_dt >= news_end_dt:
        raise SnapshotError(
            f"news_start_dt({news_start_dt.isoformat()}) >= "
            f"news_end_dt({news_end_dt.isoformat()})，左必须严格小于右"
        )
    if news_end_dt > max_end:
        raise SnapshotError(
            f"news_end_dt({news_end_dt.isoformat()}) 超过硬上限"
            f"({max_end.isoformat()} = next_trade_date 09:00)，"
            f"不允许覆盖下下次开盘以后的新闻"
        )

    start_ts = int(news_start_dt.timestamp())
    end_ts = int(news_end_dt.timestamp())
    snap = HistoricalSnapshot(
        trade_date=trade_date,
        news_start_dt=news_start_dt,
        news_end_dt=news_end_dt,
        news_start_ts=start_ts,
        news_end_ts=end_ts,
        news_status=news_status or "",
        max_news_end_dt=max_end,
    )

    # 3. 新闻
    snap.news = _load_news_in_range(
        start_ts=start_ts,
        end_ts=end_ts,
        status=(news_status or None),
    )
    snap.news_count = len(snap.news)
    if snap.news:
        ts_list = [int(n["published_ts"]) for n in snap.news]
        snap.actual_first_ts = min(ts_list)
        snap.actual_last_ts = max(ts_list)
        # 防穿越铁律（数据兜底）
        for n in snap.news:
            if int(n["published_ts"]) >= end_ts:
                raise SnapshotError(
                    f"穿越检测失败：news_id={n.get('id')} "
                    f"published_ts={n['published_ts']} >= news_end_ts="
                    f"{end_ts}（news_end_dt="
                    f"{news_end_dt.isoformat()}）"
                )

    # 4. 盘后总结
    market_date, missing = _resolve_market_summary_date(
        trade_date=trade_date,
        news_end_dt=news_end_dt,
        db=db,
        client=client,
    )
    snap.market_summary_date = market_date
    snap.market_summary_missing_reason = missing
    if market_date:
        md = _load_market_summary_md(db, market_date)
        if md is None:
            snap.market_summary_missing_reason = (
                f"market_summaries 没有 {market_date} 的 summary_md "
                f"(可能盘后自动化模块尚未跑该日)"
            )
        else:
            snap.market_summary_md = md
            # 二次防穿越：market_date 16:00 不能晚于 news_end_dt
            md_eod_ts = int(
                datetime.strptime(
                    market_date, "%Y%m%d"
                ).replace(hour=MARKET_SETTLE_HOUR).timestamp()
            )
            if md_eod_ts > end_ts:
                raise SnapshotError(
                    f"穿越检测失败：market_summary_date={market_date} "
                    f"对应 16:00 时间戳 {md_eod_ts} > news_end_ts="
                    f"{end_ts}"
                )

    _log.info(
        "snapshot[%s news_win=[%s,%s) status=%s] news=%d "
        "market_date=%s (complete=%s)",
        trade_date,
        news_start_dt.strftime("%Y%m%d-%H%M"),
        news_end_dt.strftime("%Y%m%d-%H%M"),
        snap.news_status or "all",
        snap.news_count,
        snap.market_summary_date, snap.is_complete,
    )
    return snap


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _load_news_in_range(
    *,
    start_ts: int,
    end_ts: int,
    status: Optional[str],
) -> List[Dict[str, Any]]:
    """从 news.db.raw_news 查 ``[start_ts, end_ts)`` 内的新闻（升序）。"""
    sql = (
        "SELECT id, title, content, source, published_at, "
        "       published_ts, crawled_at, clean_status, "
        "       clean_reason, extra_json "
        "FROM raw_news "
        "WHERE published_ts >= ? AND published_ts < ? "
    )
    params: List = [start_ts, end_ts]
    if status:
        sql += "AND clean_status = ? "
        params.append(status)
    sql += "ORDER BY published_ts ASC"

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _resolve_market_summary_date(
    *,
    trade_date: str,
    news_end_dt: datetime,
    db: MarketDB,
    client: Optional[TushareClient],
) -> tuple[Optional[str], Optional[str]]:
    """决定本快照该用哪一天的 ``market_summaries`` 行。

    规则：
        * news_end_dt < trade_date 16:00 → 当天盘后还没出 → 用前一交易日
        * news_end_dt ≥ trade_date 16:00 且 trade_date 开市 → 用当天
        * trade_date 非交易日 → 用 prev_trade_date（即使 news_end_dt 很晚）

    Returns:
        ``(market_date, missing_reason)``。
        market_date 为 None 时 missing_reason 给出原因。
    """
    cal = get_calendar_row(trade_date, db=db)
    is_open_today = cal is not None and int(cal.is_open) == 1
    pretrade_today = cal.pretrade_date if cal else None

    trade_date_settle = datetime.strptime(
        trade_date, "%Y%m%d"
    ).replace(hour=MARKET_SETTLE_HOUR)

    # trade_date 是开市日，且 news_end_dt 跨过 16:00 → 用当天
    if is_open_today and news_end_dt >= trade_date_settle:
        return trade_date, None

    # 否则用前一交易日（当天盘后未出 / 非交易日）
    if pretrade_today:
        return str(pretrade_today), None

    # 缓存里没有日历，尝试在线解析
    if client is None:
        return None, (
            f"trade_date={trade_date} 不在 dim_trade_calendar，"
            f"且 client=None 无法补窗"
        )
    try:
        _, prev = resolve_trade_date(client, trade_date, db=db)
        return prev, None
    except TradeDateError as exc:
        return None, f"resolve_trade_date 失败: {exc}"


def _load_market_summary_md(
    db: MarketDB,
    trade_date: str,
) -> Optional[str]:
    """查 market_summaries.summary_md。"""
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT summary_md FROM market_summaries "
            "WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
    if row is None:
        return None
    md = row["summary_md"]
    if not md or not str(md).strip():
        return None
    return str(md)


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()
