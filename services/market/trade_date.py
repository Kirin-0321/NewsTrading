"""交易日解析（Phase M1b）。

向上给出统一入口 :func:`resolve_trade_date`：

* 输入 ``YYYYMMDD`` → 校验是开市日 + 返回 ``(trade_date, prev_trade_date)``。
* 输入 ``None`` → 取"今天起向前找最近的开市日"。

实现策略
--------
1. 先查 ``dim_trade_calendar`` 表（命中零 API 消耗）；
2. 未命中或区间不足时，调一次 ``trade_cal`` 拉一段日历入库，再查；
3. 返回的 ``prev_trade_date`` 来自 ``trade_cal`` 自带的 ``pretrade_date`` 字段，
   不用我们手算。

只面向 SSE（上交所）日历——A 股两市开市/休市完全一致，单交易所足够。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from services.market.market_db import MarketDB, get_market_db
from services.market.tushare_client import TushareClient

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 缺省回看天数：从 ``date_str`` 起向前看 14 天，足以覆盖最长的 5 天小长假
DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_EXCHANGE = "SSE"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class TradeDateError(RuntimeError):
    """交易日解析失败。"""


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarRow:
    """``dim_trade_calendar`` 一行的简化视图。"""

    trade_date: str
    is_open: int
    pretrade_date: Optional[str]
    cal_date: Optional[str]


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


def resolve_trade_date(
    client: TushareClient,
    date_str: Optional[str] = None,
    *,
    db: Optional[MarketDB] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Tuple[str, str]:
    """解析交易日。

    Args:
        client: Tushare 客户端，未命中本地缓存时用它去拉。
        date_str: ``YYYYMMDD``；为 None 时取今天向前最近的开市日。
        db: ``MarketDB`` 实例，省略则取全局单例。
        lookback_days: 当 ``date_str`` 不开市时回看几天。

    Returns:
        ``(trade_date, prev_trade_date)``，二者均为 ``YYYYMMDD`` 字符串。

    Raises:
        TradeDateError: 14 天回看仍找不到开市日 / Tushare 报错等。
    """
    db = db or get_market_db()
    db.ensure_schema()

    target = (date_str or datetime.now().strftime("%Y%m%d")).strip()
    if not (len(target) == 8 and target.isdigit()):
        raise TradeDateError(f"日期格式应为 YYYYMMDD，实际: {target!r}")

    # 1) 先看本地是否能命中——日历命中 + 找到开市日
    row, prev = _find_trade_date_in_db(db, target, lookback_days=lookback_days)
    if row is not None and prev is not None:
        return row.trade_date, prev

    # 2) 本地未命中 → 调 trade_cal 一次性补足近 lookback 天，写入缓存
    _refresh_calendar_window(client, db, target, lookback_days=lookback_days)

    row, prev = _find_trade_date_in_db(db, target, lookback_days=lookback_days)
    if row is None or prev is None:
        raise TradeDateError(
            f"无法解析 {target} 附近的开市日（回看 {lookback_days} 天）。"
            f"请检查 Tushare trade_cal 是否可用、日期是否过远。"
        )
    return row.trade_date, prev


def get_calendar_row(
    trade_date: str,
    *,
    db: Optional[MarketDB] = None,
) -> Optional[CalendarRow]:
    """便捷方法：直接查某一天的日历记录（不补 API）。"""
    db = db or get_market_db()
    with db.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT trade_date, is_open, pretrade_date, cal_date "
            "FROM dim_trade_calendar WHERE trade_date = ?",
            (trade_date,),
        ).fetchone()
    return _row_to_dataclass(row) if row else None


# ---------------------------------------------------------------------------
# DB / API 实现
# ---------------------------------------------------------------------------


def _find_trade_date_in_db(
    db: MarketDB,
    target: str,
    *,
    lookback_days: int,
) -> Tuple[Optional[CalendarRow], Optional[str]]:
    """从 dim_trade_calendar 取 [target-lookback, target] 范围内的所有行，
    返回 ``(最近的开市日行, prev_trade_date)``；不足或全休市返回 (None, None)。

    我们要求范围内 **同时存在** 一个 is_open=1 的行以及对应的 pretrade_date 字段非空。
    """
    start = (
        datetime.strptime(target, "%Y%m%d") - timedelta(days=lookback_days)
    ).strftime("%Y%m%d")
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date, is_open, pretrade_date, cal_date "
            "FROM dim_trade_calendar "
            "WHERE trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date DESC",
            (start, target),
        ).fetchall()

    if not rows:
        return None, None

    # 必须把整个窗口都覆盖到了才能信任本地缓存
    expected = _expected_days(start, target)
    have = {r["trade_date"] for r in rows}
    if not expected.issubset(have):
        return None, None

    for r in rows:
        if int(r["is_open"]) == 1 and r["pretrade_date"]:
            return _row_to_dataclass(r), str(r["pretrade_date"])
    return None, None


def _refresh_calendar_window(
    client: TushareClient,
    db: MarketDB,
    target: str,
    *,
    lookback_days: int,
) -> int:
    """调用 ``trade_cal`` 拉 ``[target-lookback, target]`` 窗口入库。

    Returns:
        写入/更新的行数。
    """
    start = (
        datetime.strptime(target, "%Y%m%d") - timedelta(days=lookback_days)
    ).strftime("%Y%m%d")
    rows = client.call(
        "trade_cal",
        params={
            "exchange": DEFAULT_EXCHANGE,
            "start_date": start,
            "end_date": target,
        },
        fields="cal_date,is_open,pretrade_date",
    )
    if not rows:
        _log.warning(
            "trade_cal 返回空 (start=%s end=%s)，无法补全日历缓存",
            start,
            target,
        )
        return 0

    _persist_calendar_rows(db, rows)
    _log.info(
        "trade_cal 拉取 %d 条 → dim_trade_calendar (%s ~ %s)",
        len(rows),
        start,
        target,
    )
    return len(rows)


def _persist_calendar_rows(
    db: MarketDB, raw_rows: Sequence[dict]
) -> None:
    """把 ``trade_cal`` 返回的字典数组写入 ``dim_trade_calendar``。

    幂等：主键冲突时 ``INSERT OR REPLACE`` 覆盖。
    """
    if not raw_rows:
        return
    payload: List[tuple] = []
    for r in raw_rows:
        cal_date = (r.get("cal_date") or "").strip()
        if not cal_date:
            continue
        payload.append(
            (
                cal_date,
                int(r.get("is_open") or 0),
                (r.get("pretrade_date") or None),
                cal_date,
            )
        )
    if not payload:
        return
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO dim_trade_calendar "
            "(trade_date, is_open, pretrade_date, cal_date) "
            "VALUES (?, ?, ?, ?)",
            payload,
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _expected_days(start: str, end: str) -> set[str]:
    """返回 ``[start, end]`` 闭区间的所有自然日 ``YYYYMMDD``。"""
    s = datetime.strptime(start, "%Y%m%d")
    e = datetime.strptime(end, "%Y%m%d")
    out: set[str] = set()
    cur = s
    while cur <= e:
        out.add(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def _row_to_dataclass(row) -> CalendarRow:
    """sqlite3.Row → CalendarRow。"""
    return CalendarRow(
        trade_date=str(row["trade_date"]),
        is_open=int(row["is_open"]),
        pretrade_date=(
            str(row["pretrade_date"]) if row["pretrade_date"] else None
        ),
        cal_date=str(row["cal_date"]) if row["cal_date"] else None,
    )


# ---------------------------------------------------------------------------
# CLI 自检
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            # type: ignore[attr-defined]
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s | %(message)s",
    )

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    cli = TushareClient()
    td, ptd = resolve_trade_date(cli, arg)
    print(f"[trade_date] input={arg!r} -> trade_date={td}, prev={ptd}")
    print(f"[trade_date] tushare API 调用 = {cli.call_count}")

    td2, ptd2 = resolve_trade_date(cli, arg)
    print(f"[trade_date] 缓存命中 -> {td2}/{ptd2}, 累计调用 = {cli.call_count}")
