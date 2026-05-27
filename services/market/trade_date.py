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
# Phase 1 新增：N 日偏移 + 区间查询（打分模块用）
# ---------------------------------------------------------------------------


#: next_trade_date 向后扩窗的最大单次步长（含小长假兜底）。
#: 超过 30 天 + 2*n 仍找不到 → 抛 TradeDateError。
_MAX_FORWARD_DAYS = 30


def next_trade_date(
    date_str: str,
    n: int = 1,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
) -> str:
    """从 ``date_str`` 起，向后数 ``n`` 个交易日。

    Args:
        date_str: ``YYYYMMDD`` 起始日（自身可以是非交易日）。
        n: 偏移步数。
            - ``n == 0``：若 ``date_str`` 开市则返回 ``date_str``；否则向**前**
              找最近开市日（语义与 :func:`resolve_trade_date` 一致）。
            - ``n >= 1``：返回 ``date_str`` 之**后**的第 ``n`` 个开市日
              （不计 ``date_str`` 自身是否开市；常用于 D+5 打分）。
            - ``n < 0``：未实现，抛 :class:`ValueError`。
        db: 可选 ``MarketDB``；省略取全局单例。
        client: 可选 ``TushareClient``；当本地 ``dim_trade_calendar`` 缓存
            不够覆盖前进窗口时，用它一次性补窗。``None`` 且缓存不足时
            抛 :class:`TradeDateError`（不会悄悄发起 API 调用）。

    Returns:
        目标交易日 ``YYYYMMDD``。

    Raises:
        ValueError: ``n < 0`` 或 ``date_str`` 格式错误。
        TradeDateError: 前进 ``_MAX_FORWARD_DAYS + 2*n`` 天仍找不到第 n 个
            开市日，或缓存不足且未提供 client。
    """
    if n < 0:
        raise ValueError(f"next_trade_date 一期不支持负 n（得到 {n}）")
    _ensure_yyyymmdd(date_str)

    db = db or get_market_db()
    db.ensure_schema()

    if n == 0:
        # n=0 行为：复用 resolve_trade_date（自动向前找）
        if client is None:
            row, _ = _find_trade_date_in_db(
                db, date_str, lookback_days=DEFAULT_LOOKBACK_DAYS
            )
            if row is None:
                raise TradeDateError(
                    f"n=0 且 client 未提供，缓存不足以解析 {date_str} 附近的开市日"
                )
            return row.trade_date
        td, _ = resolve_trade_date(client, date_str, db=db)
        return td

    # n >= 1：向后取第 n 个开市日
    forward_days = _MAX_FORWARD_DAYS + 2 * n
    end_window = (
        datetime.strptime(date_str, "%Y%m%d") + timedelta(days=forward_days)
    ).strftime("%Y%m%d")

    # 1. 先看本地缓存能否覆盖 [date_str+1, end_window]
    after = _trade_dates_after_in_db(db, date_str, end_window)
    if len(after) >= n:
        return after[n - 1]

    # 2. 缓存不够 → 拉日历向后扩窗
    if client is None:
        raise TradeDateError(
            f"本地缓存不足（仅 {len(after)} 个开市日 < n={n}），"
            f"且 client 未提供，无法补全 [{date_str}, {end_window}] 日历"
        )
    _refresh_calendar_forward(client, db, date_str, forward_days=forward_days)

    after = _trade_dates_after_in_db(db, date_str, end_window)
    if len(after) >= n:
        return after[n - 1]
    raise TradeDateError(
        f"向后 {forward_days} 天仍找不到第 {n} 个开市日（起点 {date_str}）"
    )


def trade_dates_between(
    start: str,
    end: str,
    *,
    db: Optional[MarketDB] = None,
    client: Optional[TushareClient] = None,
) -> List[str]:
    """返回 ``[start, end]`` 闭区间内所有交易日（升序）。

    Args:
        start / end: ``YYYYMMDD`` 边界（包含）。
        db: 可选 ``MarketDB``。
        client: 可选 ``TushareClient``；缓存不足时用它补窗，None 时抛错。

    Returns:
        升序排列的开市日列表；区间内全休市则返回空列表。

    Raises:
        ValueError: 边界格式错误，或 ``start > end``。
        TradeDateError: 缓存未覆盖且未提供 client。
    """
    _ensure_yyyymmdd(start)
    _ensure_yyyymmdd(end)
    if start > end:
        raise ValueError(f"start({start}) > end({end})")

    db = db or get_market_db()
    db.ensure_schema()

    expected = _expected_days(start, end)

    if _calendar_covers(db, expected):
        return _open_dates_in_range_from_db(db, start, end)

    if client is None:
        raise TradeDateError(
            f"本地缓存未覆盖区间 [{start}, {end}]，且 client 未提供"
        )

    # 一次性拉整段日历入库（trade_cal 接口本身就接受任意区间）
    rows = client.call(
        "trade_cal",
        params={
            "exchange": DEFAULT_EXCHANGE,
            "start_date": start,
            "end_date": end,
        },
        fields="cal_date,is_open,pretrade_date",
    )
    if rows:
        _persist_calendar_rows(db, rows)

    if not _calendar_covers(db, expected):
        raise TradeDateError(
            f"trade_cal 拉取后仍未覆盖区间 [{start}, {end}]，"
            f"请检查 Tushare 返回是否完整"
        )
    return _open_dates_in_range_from_db(db, start, end)


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


def _refresh_calendar_forward(
    client: TushareClient,
    db: MarketDB,
    target: str,
    *,
    forward_days: int,
) -> int:
    """调 ``trade_cal`` 拉 ``[target, target+forward_days]`` 窗口入库。

    专给 :func:`next_trade_date` 用。

    Returns:
        写入/更新的行数。
    """
    end = (
        datetime.strptime(target, "%Y%m%d") + timedelta(days=forward_days)
    ).strftime("%Y%m%d")
    rows = client.call(
        "trade_cal",
        params={
            "exchange": DEFAULT_EXCHANGE,
            "start_date": target,
            "end_date": end,
        },
        fields="cal_date,is_open,pretrade_date",
    )
    if not rows:
        _log.warning(
            "trade_cal 向后拉取空 (start=%s end=%s)",
            target, end,
        )
        return 0
    _persist_calendar_rows(db, rows)
    _log.info(
        "trade_cal 前向拉取 %d 条 → dim_trade_calendar (%s ~ %s)",
        len(rows), target, end,
    )
    return len(rows)


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


def _ensure_yyyymmdd(s: str) -> None:
    """校验形如 ``YYYYMMDD`` 的日期字符串，无效抛 ``ValueError``。"""
    if not isinstance(s, str) or len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式应为 YYYYMMDD，得到 {s!r}")
    # 进一步校验是合法日期（非 99999999 等）
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"日期不合法 {s!r}: {exc}") from exc


def _trade_dates_after_in_db(
    db: MarketDB,
    after: str,
    end_window: str,
) -> List[str]:
    """从 dim_trade_calendar 取 ``(after, end_window]`` 内所有开市日。

    缺数据不报错，调用方负责自行判断长度是否够 n。
    """
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date FROM dim_trade_calendar "
            "WHERE trade_date > ? AND trade_date <= ? AND is_open = 1 "
            "ORDER BY trade_date ASC",
            (after, end_window),
        ).fetchall()
    return [str(r["trade_date"]) for r in rows]


def _open_dates_in_range_from_db(
    db: MarketDB,
    start: str,
    end: str,
) -> List[str]:
    """从 dim_trade_calendar 取 ``[start, end]`` 内所有开市日（升序）。"""
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date FROM dim_trade_calendar "
            "WHERE trade_date BETWEEN ? AND ? AND is_open = 1 "
            "ORDER BY trade_date ASC",
            (start, end),
        ).fetchall()
    return [str(r["trade_date"]) for r in rows]


def _calendar_covers(db: MarketDB, expected_dates: set[str]) -> bool:
    """检查 ``dim_trade_calendar`` 是否包含 ``expected_dates`` 全集。"""
    if not expected_dates:
        return True
    with db.connect(readonly=True) as conn:
        placeholders = ",".join("?" for _ in expected_dates)
        rows = conn.execute(
            f"SELECT trade_date FROM dim_trade_calendar "
            f"WHERE trade_date IN ({placeholders})",
            list(expected_dates),
        ).fetchall()
    have = {str(r["trade_date"]) for r in rows}
    return expected_dates.issubset(have)


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
