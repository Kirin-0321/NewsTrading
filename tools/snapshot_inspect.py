"""手动校验某个回测视角下的时间窗 + 新闻 + 大盘日期边界。

业务定位
--------
**回测时间边界 inspector**（2026-05-27 时间窗重构后新版本）。
对应 ``doc/design/05-27-1515-回测时间边界说明书.md`` 的 §4 例子。

主人用本 CLI 的场景：
* 决定批量回测前，先看哪些 trade_date 的 news 是否覆盖够
* GUI 手动回测页的「时间边界预览面板」共享同一个核心
  函数（见 :func:`inspect_snapshot`）
* CI / 调试时核对某天为什么 ``snapshot_news_count`` 这么少

输出布局
--------
对每个 (trade_date, news_start_dt, news_end_dt) 输出 4 段：

1. **输入** —— trade_date / news_status
2. **时间窗** —— news_start / news_end / news_end 上限 / 当前是否触上限
3. **快照实况** —— 实际命中 news 数 / news ts 范围 / market_date / 完整性
4. **打分日期（D+1~D+5）** —— 调 ``next_trade_date`` 推算
5. **新闻样本（可选 ``--show-news N``）** —— 头 N 条 title + 时间

用法
----
::

    # 默认窗（主人推荐）：trade_date 14:00 → next_open 09:00
    python tools/snapshot_inspect.py --date 20260522

    # 手动指定时间窗（用 YYYYMMDDHHMM 格式）
    python tools/snapshot_inspect.py --date 20260522 \\
        --news-start 202605221400 --news-end 202605221600

    # 全部新闻（含 rejected）而不是仅 curated
    python tools/snapshot_inspect.py --date 20260522 --all-news

    # 区间扫描
    python tools/snapshot_inspect.py --date-range 20260522..20260526

    # 展示头 5 条新闻
    python tools/snapshot_inspect.py --date 20260522 --show-news 5
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.market.trade_date import (  # noqa: E402
    TradeDateError,
    next_trade_date,
    trade_dates_between,
)
from services.market.tushare_client import TushareClient  # noqa: E402
from services.scoring.snapshot import (  # noqa: E402
    SnapshotError,
    build_snapshot,
)


# ---------------------------------------------------------------------------
# 时间字符串解析
# ---------------------------------------------------------------------------


def _parse_yyyymmddhhmm(s: str) -> datetime:
    """``202605221400`` → datetime(2026, 5, 22, 14, 0)。"""
    s = s.strip()
    if len(s) != 12 or not s.isdigit():
        raise SystemExit(
            f"时间格式应为 12 位 YYYYMMDDHHMM，得到 {s!r}"
        )
    return datetime.strptime(s, "%Y%m%d%H%M")


# ---------------------------------------------------------------------------
# 核心：单视角检查
# ---------------------------------------------------------------------------


def inspect_snapshot(
    trade_date: str,
    *,
    news_start_dt: Optional[datetime] = None,
    news_end_dt: Optional[datetime] = None,
    news_status: str = "curated",
    days_back: int = 5,
    client: Optional[TushareClient] = None,
) -> dict:
    """对单个回测视角返回时间窗 + 快照报告。

    Returns:
        dict 字段：
            * input: trade_date / news_status / 是否手动指定窗
            * window: news_start_dt / news_end_dt / max_news_end_dt /
                      news_window_human / hit_max
            * derived: news_count / news_ts_actual / market_summary_date /
                       market_summary_md_len / snapshot_complete /
                       missing_reason
            * d_plus: 列表[{day: 1, score_date: 'YYYYMMDD'}, ...]
            * error: str / None
    """
    client = client or TushareClient()
    report: dict = {
        "input": {
            "trade_date": trade_date,
            "news_status": news_status or "all",
            "user_overrode_start": news_start_dt is not None,
            "user_overrode_end": news_end_dt is not None,
        },
        "window": None,
        "derived": None,
        "d_plus": [],
        "error": None,
    }

    # 1. snapshot
    try:
        snap = build_snapshot(
            trade_date,
            news_start_dt=news_start_dt,
            news_end_dt=news_end_dt,
            news_status=news_status,
            client=client,
        )
    except SnapshotError as exc:
        report["error"] = f"SnapshotError: {exc}"
        return report

    report["window"] = {
        "news_start_dt": snap.news_start_dt.isoformat(timespec="seconds"),
        "news_end_dt": snap.news_end_dt.isoformat(timespec="seconds"),
        "max_news_end_dt": (
            snap.max_news_end_dt.isoformat(timespec="seconds")
            if snap.max_news_end_dt else None
        ),
        "news_window_human": (
            f"[{snap.news_start_dt.isoformat(timespec='seconds')}, "
            f"{snap.news_end_dt.isoformat(timespec='seconds')})"
        ),
        "hit_max": (
            snap.max_news_end_dt is not None
            and snap.news_end_dt == snap.max_news_end_dt
        ),
    }

    actual_first = (
        datetime.fromtimestamp(snap.actual_first_ts).isoformat(
            timespec="seconds"
        )
        if snap.actual_first_ts else None
    )
    actual_last = (
        datetime.fromtimestamp(snap.actual_last_ts).isoformat(
            timespec="seconds"
        )
        if snap.actual_last_ts else None
    )

    report["derived"] = {
        "news_count": snap.news_count,
        "news_ts_actual": {
            "earliest": actual_first,
            "latest": actual_last,
        },
        "market_summary_date": snap.market_summary_date,
        "market_summary_md_len": (
            len(snap.market_summary_md)
            if snap.market_summary_md else 0
        ),
        "snapshot_complete": snap.is_complete,
        "missing_reason": snap.market_summary_missing_reason,
        "_snap_obj": snap,
    }

    # 2. D+1 ~ D+N 推算
    for n in range(1, days_back + 1):
        try:
            sd = next_trade_date(trade_date, n, client=client)
            report["d_plus"].append({"day": n, "score_date": sd})
        except TradeDateError as exc:
            report["d_plus"].append({
                "day": n, "score_date": None, "error": str(exc),
            })
            break

    return report


# ---------------------------------------------------------------------------
# CLI 输出
# ---------------------------------------------------------------------------


def _print_report(report: dict, *, show_news: int = 0) -> None:
    ipt = report["input"]
    print("─" * 70)
    head = (
        f"【视角】trade_date={ipt['trade_date']} "
        f"news_status={ipt['news_status']}"
    )
    if ipt["user_overrode_start"] or ipt["user_overrode_end"]:
        head += "  (手动指定窗)"
    else:
        head += "  (默认窗 = 主人语义)"
    print(head)

    if report["error"]:
        print(f"  [ERROR] {report['error']}")
        return

    w = report["window"]
    d = report["derived"]

    hit_flag = "  ← 已触上限" if w["hit_max"] else ""
    print(
        f"  news_window = {w['news_window_human']}{hit_flag}\n"
        f"  上限 news_end = {w['max_news_end_dt']} "
        f"(= next_trade_date 09:00)"
    )

    actual_str = "-"
    if d["news_ts_actual"]["earliest"]:
        actual_str = (
            f"{d['news_ts_actual']['earliest']} ~ "
            f"{d['news_ts_actual']['latest']}"
        )
    print(
        f"  实际 news 数 = {d['news_count']}  "
        f"(实际时间范围: {actual_str})"
    )
    print(
        f"  market_summary_date = {d['market_summary_date']} "
        f"(md 长度 {d['market_summary_md_len']} 字符)"
    )
    print(f"  snapshot_complete = {d['snapshot_complete']}")
    if d["missing_reason"]:
        print(f"  missing_reason = {d['missing_reason']}")

    print("  D+N 打分日期:")
    for item in report["d_plus"]:
        if item["score_date"]:
            print(f"    D+{item['day']} = {item['score_date']}")
        else:
            print(f"    D+{item['day']} = [ERROR] {item.get('error')}")

    if show_news > 0 and d["news_count"] > 0:
        snap = d["_snap_obj"]
        print(f"\n  新闻样本（头 {min(show_news, snap.news_count)} 条）:")
        for n in snap.news[: show_news]:
            title = (n.get("title") or "")[:60]
            src = n.get("source") or ""
            t = n.get("published_at") or ""
            print(f"    [{t}] {src:8s} | {title}")


def _strip_internal(report: dict) -> dict:
    """从 report 剥离不能 JSON 序列化的内部对象（_snap_obj）。"""
    clean = dict(report)
    if clean.get("derived"):
        d = dict(clean["derived"])
        d.pop("_snap_obj", None)
        clean["derived"] = d
    return clean


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="snapshot_inspect",
        description=(
            "查验某 (trade_date + 新闻窗) 视角的全部时间边界。"
            "对应 doc/design/05-27-1515-回测时间边界说明书.md §4 例子"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--date", help="单日 YYYYMMDD")
    grp.add_argument(
        "--date-range",
        help="区间 YYYYMMDD..YYYYMMDD（自动跳过非交易日）",
    )
    p.add_argument(
        "--news-start", default=None,
        help="新闻窗左边界 YYYYMMDDHHMM；不传 = trade_date 14:00",
    )
    p.add_argument(
        "--news-end", default=None,
        help=(
            "新闻窗右边界 YYYYMMDDHHMM；不传 = next_trade_date 09:00"
            "（也是硬上限）"
        ),
    )
    p.add_argument(
        "--all-news", action="store_true",
        help="不过滤 clean_status（含 rejected/pending）；默认仅 curated",
    )
    p.add_argument(
        "--days-back", type=int, default=5,
        help="推算 D+N 的最大 N（默认 5）",
    )
    p.add_argument(
        "--show-news", type=int, default=0,
        help="若 > 0 则额外打印前 N 条新闻样本（标题 + 时间）",
    )
    p.add_argument(
        "--json", action="store_true",
        help="输出 JSON（机器友好，CI 用）",
    )
    return p.parse_args(argv)


def _resolve_dates(args, client: TushareClient) -> List[str]:
    if args.date:
        return [args.date]
    start, _sep, end = args.date_range.partition("..")
    if not (start and end):
        raise SystemExit(
            f"--date-range 格式应为 YYYYMMDD..YYYYMMDD: {args.date_range}"
        )
    try:
        return trade_dates_between(start, end, client=client)
    except TradeDateError as exc:
        raise SystemExit(f"解析交易日失败: {exc}")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    client = TushareClient()
    dates = _resolve_dates(args, client)
    if not dates:
        print("[warn] 解析后无可用交易日")
        return 0

    news_start_dt = (
        _parse_yyyymmddhhmm(args.news_start) if args.news_start else None
    )
    news_end_dt = (
        _parse_yyyymmddhhmm(args.news_end) if args.news_end else None
    )
    news_status = "" if args.all_news else "curated"

    reports: List[dict] = []
    any_error = False
    for d in dates:
        rep = inspect_snapshot(
            d,
            news_start_dt=news_start_dt,
            news_end_dt=news_end_dt,
            news_status=news_status,
            days_back=args.days_back,
            client=client,
        )
        if rep.get("error"):
            any_error = True
        reports.append(rep)

    if args.json:
        print(json.dumps(
            [_strip_internal(r) for r in reports],
            ensure_ascii=False, indent=2,
        ))
    else:
        print(
            f"\n[plan] {len(dates)} 个视角 "
            f"(news_status={news_status or 'all'})"
        )
        for r in reports:
            _print_report(r, show_news=args.show_news)
        print("─" * 70)
        ok_n = sum(
            1 for r in reports
            if not r.get("error") and r["derived"]["snapshot_complete"]
        )
        partial_n = sum(
            1 for r in reports
            if not r.get("error") and not r["derived"]["snapshot_complete"]
        )
        err_n = sum(1 for r in reports if r.get("error"))
        print(
            f"[summary] complete={ok_n} / partial={partial_n} / "
            f"error={err_n} 总 {len(reports)} 视角"
        )

    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
