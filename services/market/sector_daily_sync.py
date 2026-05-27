"""板块日行情同步（Phase 1 Step 1.3）。

接口对称 :mod:`services.market.stock_daily_sync`，但底层 **复用**
:class:`services.market.tushare_fetcher.TushareMarketFetcher` 已有的
``_fetch_sector_moneyflow`` 步骤——盘后自动化模块跑了几个月、积累了
14660 行的同款逻辑，没必要重写一份。

接口
----
* :func:`sync_sector_daily` 单交易日同步
* :func:`sync_sector_daily_range` 区间批量

为什么不直接调 :meth:`TushareMarketFetcher.fetch`？
    后者会一口气把 index/sector/limit/hsgt/top_list 等十几个表全拉，
    本模块只需要 sector 一张表，单独切出来给打分模块 / 调度器用。
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from services.market.market_db import MarketDB, get_market_db
from services.market.stock_daily_sync import (  # 复用类型
    BatchSyncResult,
    DailySyncResult,
)
from services.market.trade_date import (
    TradeDateError,
    trade_dates_between,
)
from services.market.tushare_client import TushareClient, TushareError
from services.market.tushare_fetcher import FetchResult, TushareMarketFetcher

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 单交易日
# ---------------------------------------------------------------------------


def sync_sector_daily(
    trade_date: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
    force: bool = False,
) -> DailySyncResult:
    """同步指定交易日所有概念板块的日行情 → ``fact_sector_daily``。

    Args:
        trade_date: ``YYYYMMDD`` 目标交易日。
        db / client: 可选注入；不给则用全局单例。
        force: 默认 False；True 时即使本日已有数据也会重写（依靠
            ``INSERT OR REPLACE`` 自动覆盖；不删 dim_sector）。

    Returns:
        :class:`DailySyncResult`。失败时 ``ok=False`` + ``error``。
    """
    start = time.perf_counter()
    db = db or get_market_db()
    db.ensure_schema()

    if not _is_yyyymmdd(trade_date):
        return DailySyncResult(
            ok=False, trade_date=trade_date,
            error=f"trade_date 格式应为 YYYYMMDD，得到 {trade_date!r}",
        )

    # 1. 幂等检查
    if not force:
        existing = _count_rows(db, trade_date)
        if existing > 0:
            return DailySyncResult(
                ok=True, trade_date=trade_date,
                rows_written=existing, skipped=True,
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )

    # 2. 复用 fetcher 单步
    client = client or TushareClient()
    fetcher = TushareMarketFetcher(client, db)
    result = FetchResult(trade_date=trade_date, prev_trade_date="")
    api0 = client.call_count
    try:
        fetcher._fetch_sector_moneyflow(result)  # noqa: SLF001
    except TushareError as exc:
        return DailySyncResult(
            ok=False, trade_date=trade_date,
            api_calls=client.call_count - api0,
            error=f"moneyflow_ind_dc({trade_date}) 调用失败: {exc}",
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    api_calls = client.call_count - api0

    written = int(result.ingested.get("fact_sector_daily", 0))

    return DailySyncResult(
        ok=True, trade_date=trade_date,
        rows_written=written, api_calls=api_calls,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )


# ---------------------------------------------------------------------------
# 区间批量
# ---------------------------------------------------------------------------


def sync_sector_daily_range(
    start: str,
    end: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
    force: bool = False,
    on_progress=None,
) -> BatchSyncResult:
    """同步 ``[start, end]`` 闭区间内所有交易日的板块日行情。"""
    t0 = time.perf_counter()
    db = db or get_market_db()
    client = client or TushareClient()

    try:
        td_list = trade_dates_between(start, end, db=db, client=client)
    except TradeDateError as exc:
        return BatchSyncResult(
            ok=False, days_total=0, days_synced=0, days_skipped=0,
            days_failed=0, rows_written=0, api_calls=0,
            elapsed_ms=int((time.perf_counter() - t0) * 1000),
            failures=[DailySyncResult(
                ok=False, trade_date=f"{start}~{end}",
                error=f"日历解析失败: {exc}",
            )],
        )

    days_synced = days_skipped = days_failed = 0
    rows_written = api_calls = 0
    failures: List[DailySyncResult] = []

    total = len(td_list)
    for idx, td in enumerate(td_list, start=1):
        res = sync_sector_daily(td, db=db, client=client, force=force)
        rows_written += res.rows_written
        api_calls += res.api_calls
        if not res.ok:
            days_failed += 1
            failures.append(res)
        elif res.skipped:
            days_skipped += 1
        else:
            days_synced += 1

        if on_progress:
            try:
                on_progress(idx, total, res)
            except Exception:  # noqa: BLE001
                pass

    return BatchSyncResult(
        ok=(days_failed == 0),
        days_total=total,
        days_synced=days_synced,
        days_skipped=days_skipped,
        days_failed=days_failed,
        rows_written=rows_written,
        api_calls=api_calls,
        elapsed_ms=int((time.perf_counter() - t0) * 1000),
        failures=failures,
    )


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------


def _count_rows(db: MarketDB, trade_date: str) -> int:
    """返回 fact_sector_daily 中该交易日已有的行数。"""
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM fact_sector_daily "
            "WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
    return int(row["c"]) if row else 0


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()
