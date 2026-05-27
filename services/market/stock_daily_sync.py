"""个股日行情同步（Phase 1 Step 1.2）。

调用 Tushare ``daily`` 接口拉全 A 股某日 OHLCV → ``fact_stock_daily``。

设计要点
--------
* **单 API 全市场**：``daily(trade_date=YYYYMMDD)`` 一次返回当日 ~5400 只
  个股的 OHLCV，单次调用 ~3 秒、消耗 1 次 Tushare 调用积分。
* **幂等**：默认检测 ``fact_stock_daily`` 已有该交易日则跳过（``force=True``
  覆盖重写）。批量场景配合 ``--resume`` 可断点续传。
* **批量入库**：``INSERT OR REPLACE`` + ``executemany``，5400 行 ~50ms。
* **失败不抛主流程**：返回 :class:`DailySyncResult` 带 ``ok`` 标记和
  ``error`` 字段，由调用方决定是否重试/告警。

接口
----
* :func:`sync_stock_daily` 单交易日同步
* :func:`sync_stock_daily_range` 区间批量
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from services.market.market_db import MarketDB, get_market_db
from services.market.trade_date import (
    TradeDateError,
    trade_dates_between,
)
from services.market.tushare_client import TushareClient, TushareError

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class DailySyncResult:
    """单交易日同步结果（GUI / 日志 / 调度器都用它）。"""

    ok: bool
    trade_date: str
    rows_written: int = 0
    api_calls: int = 0
    elapsed_ms: int = 0
    skipped: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# 单交易日同步
# ---------------------------------------------------------------------------


def sync_stock_daily(
    trade_date: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
    force: bool = False,
) -> DailySyncResult:
    """同步指定交易日全 A 股 ``daily`` 行情 → ``fact_stock_daily``。

    Args:
        trade_date: ``YYYYMMDD`` 目标交易日（必须是开市日；非开市日 daily
            会返回空，本函数当作"成功 0 行"处理）。
        db: 可选 ``MarketDB``，默认走全局单例。
        client: 可选 ``TushareClient``，默认懒构造。
        force: 默认 False；True 时即使本日已有数据也会重新拉取并覆盖。

    Returns:
        :class:`DailySyncResult`，**不抛业务异常**。失败时 ``ok=False`` +
        ``error`` 字段，方便上游统一处理。

    防穿越约束：trade_date 必须严格 ``YYYYMMDD``。
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
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return DailySyncResult(
                ok=True, trade_date=trade_date,
                rows_written=existing, skipped=True,
                elapsed_ms=elapsed_ms,
            )

    # 2. 拉取
    client = client or TushareClient()
    try:
        rows = client.call("daily", params={"trade_date": trade_date})
    except TushareError as exc:
        return DailySyncResult(
            ok=False, trade_date=trade_date,
            api_calls=1, error=f"daily({trade_date}) 调用失败: {exc}",
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )

    if not rows:
        # 非交易日 / 接口异常 → ok=True rows_written=0（上层依据 0 行可判断）
        return DailySyncResult(
            ok=True, trade_date=trade_date,
            rows_written=0, api_calls=1,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )

    # 3. 入库
    written = _upsert_rows(db, trade_date, rows, force=force)

    return DailySyncResult(
        ok=True, trade_date=trade_date,
        rows_written=written, api_calls=1,
        elapsed_ms=int((time.perf_counter() - start) * 1000),
    )


# ---------------------------------------------------------------------------
# 区间批量
# ---------------------------------------------------------------------------


@dataclass
class BatchSyncResult:
    """区间批量同步聚合结果。"""

    ok: bool
    days_total: int
    days_synced: int
    days_skipped: int
    days_failed: int
    rows_written: int
    api_calls: int
    elapsed_ms: int
    failures: List[DailySyncResult] = field(default_factory=list)


def sync_stock_daily_range(
    start: str,
    end: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
    force: bool = False,
    on_progress=None,
) -> BatchSyncResult:
    """同步 ``[start, end]`` 闭区间内所有交易日的个股日行情。

    自动跳过非交易日（用 :func:`trade_dates_between` 解析日历）。

    Args:
        start / end: ``YYYYMMDD``。
        on_progress: 可选回调 ``(idx, total, result: DailySyncResult)``，
            每完成一天调一次（CLI 用于打印进度条）。
    """
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
        res = sync_stock_daily(td, db=db, client=client, force=force)
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


_INSERT_SQL = (
    "INSERT OR REPLACE INTO fact_stock_daily "
    "(ts_code, trade_date, open, high, low, close, pre_close, "
    " pct_chg, vol, amount) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _count_rows(db: MarketDB, trade_date: str) -> int:
    """返回 fact_stock_daily 中该交易日已有的行数。"""
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM fact_stock_daily "
            "WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
    return int(row["c"]) if row else 0


def _upsert_rows(
    db: MarketDB,
    trade_date: str,
    raw_rows: list,
    *,
    force: bool,
) -> int:
    """把 daily 接口返回的字典列表批量 upsert，返回写入条数。

    force=True 时先 DELETE 当日全部数据保证干净覆盖。
    """
    payload = []
    for r in raw_rows:
        ts_code = (r.get("ts_code") or "").strip()
        td = (r.get("trade_date") or "").strip()
        if not ts_code or td != trade_date:
            continue
        payload.append((
            ts_code,
            td,
            _to_float(r.get("open")),
            _to_float(r.get("high")),
            _to_float(r.get("low")),
            _to_float(r.get("close")),
            _to_float(r.get("pre_close")),
            _to_float(r.get("pct_chg")),
            _to_float(r.get("vol")),
            _to_float(r.get("amount")),
        ))

    if not payload:
        return 0

    with db.connect() as conn:
        if force:
            conn.execute(
                "DELETE FROM fact_stock_daily WHERE trade_date = ?",
                (trade_date,),
            )
        conn.executemany(_INSERT_SQL, payload)
    _log.info(
        "fact_stock_daily ingest: %s 行 (trade_date=%s, force=%s)",
        len(payload), trade_date, force,
    )
    return len(payload)


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()
