"""盘后数据历史回填 CLI（Phase M5）。

把过去 N 个交易日的盘后总结一次性拉满 ``market.db``，给后续 CLI 评估
回测 / Agent 优化打底用。

用法
----

最近 5 天 hybrid 模式（建议先小试）::

    python tools/market_fetch_backfill.py --days 5

最近 30 天 hybrid 模式（约 20~25 分钟）::

    python tools/market_fetch_backfill.py --days 30

指定区间（含 ``--start`` ``--end``，会自动剔除非开市日）::

    python tools/market_fetch_backfill.py --start 20260401 --end 20260525

仅模拟，不真跑::

    python tools/market_fetch_backfill.py --days 30 --dry-run

强制重拉所有日（无视 ``market_summaries`` 缓存）::

    python tools/market_fetch_backfill.py --days 30 --force-refresh

写 CSV 汇总报告::

    python tools/market_fetch_backfill.py --days 30 \
        --report data/backups/backfill_20260526.csv

退出码
------

0  全部成功，或 ``--continue-on-error`` 时部分成功
1  ``--no-continue`` 情况下首个失败立即返回
2  参数错误
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.market.service import (  # noqa: E402
    MarketSummaryResult,
    MarketSummaryService,
)
from services.market.trade_date import (  # noqa: E402
    TradeDateError,
    get_calendar_row,
)
from services.market.tushare_client import TushareClient  # noqa: E402


_log = logging.getLogger("market_fetch_backfill")

VALID_MODES = ("tushare-only", "hybrid", "ai-full")


# ---------------------------------------------------------------------------
# 交易日枚举
# ---------------------------------------------------------------------------


def _ensure_calendar_window(
    client: TushareClient,
    *,
    start: str,
    end: str,
) -> None:
    """一次性把 ``[start, end]`` 区间的日历拉进 ``dim_trade_calendar``。

    比一格一格调用 ``resolve_trade_date`` 高效得多——
    一次 API 即可覆盖近 90 天，避免链式枚举触发 N 次 ``trade_cal``。
    """
    from services.market.market_db import get_market_db

    db = get_market_db()
    rows = client.call(
        "trade_cal",
        params={
            "exchange": "SSE",
            "start_date": start,
            "end_date": end,
        },
        fields="cal_date,is_open,pretrade_date",
    )
    if not rows:
        return
    with db.connect() as conn:
        for r in rows:
            cal_date = str(r.get("cal_date") or "")
            if not cal_date:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO dim_trade_calendar "
                "(trade_date, is_open, pretrade_date, cal_date) "
                "VALUES (?, ?, ?, ?)",
                (
                    cal_date,
                    int(r.get("is_open") or 0),
                    str(r.get("pretrade_date") or "") or None,
                    cal_date,
                ),
            )


def list_recent_trade_dates(
    client: TushareClient,
    n: int,
    *,
    anchor: Optional[str] = None,
) -> List[str]:
    """从 ``anchor`` (默认今天) 起向前枚举 N 个开市日，DESC。

    先一次 trade_cal 拉一个足够大的窗口（按 N × 1.6 + 14 天换算）入缓存，
    再 SQL 反查 ``DESC LIMIT N``。
    """
    from datetime import datetime, timedelta

    from services.market.market_db import get_market_db

    end_str = (anchor or datetime.now().strftime("%Y%m%d")).strip()
    end_dt = datetime.strptime(end_str, "%Y%m%d")
    span_days = max(int(n * 1.6) + 14, 60)
    start_dt = end_dt - timedelta(days=span_days)
    start_str = start_dt.strftime("%Y%m%d")

    _ensure_calendar_window(client, start=start_str, end=end_str)

    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date FROM dim_trade_calendar "
            "WHERE trade_date BETWEEN ? AND ? AND is_open = 1 "
            "ORDER BY trade_date DESC LIMIT ?",
            (start_str, end_str, n),
        ).fetchall()
    return [str(r[0]) for r in rows]


def list_trade_dates_in_range(
    client: TushareClient,
    start: str,
    end: str,
) -> List[str]:
    """枚举 ``[start, end]`` 区间内所有开市日，ASC。"""
    _ensure_calendar_window(client, start=start, end=end)

    from services.market.market_db import get_market_db

    db = get_market_db()
    with db.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT trade_date FROM dim_trade_calendar "
            "WHERE trade_date BETWEEN ? AND ? AND is_open = 1 "
            "ORDER BY trade_date ASC",
            (start, end),
        ).fetchall()
    return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def run_backfill(
    trade_dates: List[str],
    *,
    mode: str,
    top_n: int,
    force_refresh: bool,
    continue_on_error: bool,
    report_path: Optional[Path] = None,
) -> int:
    """逐日跑 ``MarketSummaryService.build()``。返回退出码。"""
    svc = MarketSummaryService()
    total = len(trade_dates)
    results: List[Tuple[str, MarketSummaryResult, float]] = []
    succ = 0
    fail = 0
    skipped_cache = 0
    t_global = time.time()

    print(f"\n>>> 开始回填 {total} 个交易日 | 模式={mode} | "
          f"force_refresh={force_refresh}")

    for i, td in enumerate(trade_dates, 1):
        prefix = f"[{i:>3}/{total}] {td}"
        t0 = time.time()

        def _on_progress(msg: str, _p: str = prefix) -> None:
            _log.debug("%s · %s", _p, msg)

        try:
            result = svc.build(
                trade_date=td,
                mode=mode,
                top_sector_n=top_n,
                force_refresh=force_refresh,
                progress_callback=_on_progress,
            )
        except Exception as e:
            elapsed = time.time() - t0
            print(f"{prefix}  ❌ 异常: {type(e).__name__}: {e}  "
                  f"({elapsed:.1f}s)")
            fail += 1
            if not continue_on_error:
                _write_report(results, report_path)
                return 1
            continue

        elapsed = time.time() - t0
        results.append((td, result, elapsed))

        if not result.ok:
            print(f"{prefix}  ❌ {result.error or '未知错误'}  "
                  f"({elapsed:.1f}s)")
            fail += 1
            if not continue_on_error:
                _write_report(results, report_path)
                return 1
            continue

        flag = "🟢 cache" if result.from_cache else "🆕 fresh"
        if result.from_cache:
            skipped_cache += 1
        comp = (result.completeness or 0) * 100
        print(
            f"{prefix}  {flag}  完整度 {comp:5.1f}%  "
            f"API {result.api_call_count:>2}  "
            f"{elapsed:5.1f}s"
        )
        succ += 1

    elapsed_total = time.time() - t_global
    avg_comp = (
        sum((r.completeness or 0) for _, r, _ in results) / len(results)
        if results else 0
    )
    total_api = sum(r.api_call_count for _, r, _ in results)

    print("\n" + "=" * 60)
    print(f"  回填完成: 成功 {succ} / 失败 {fail} / 总数 {total}")
    print(f"  其中命中缓存 (零 API): {skipped_cache}")
    print(f"  平均完整度: {avg_comp * 100:.1f}%")
    print(f"  累计 API 调用: {total_api}")
    print(f"  总耗时: {elapsed_total:.1f}s "
          f"(平均 {elapsed_total / max(total, 1):.1f}s/天)")
    print("=" * 60)

    _write_report(results, report_path)

    return 0 if (fail == 0 or continue_on_error) else 1


def _write_report(
    results: List[Tuple[str, MarketSummaryResult, float]],
    report_path: Optional[Path],
) -> None:
    if not report_path:
        return
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow([
            "trade_date", "ok", "mode", "from_cache",
            "completeness", "elapsed_ms", "api_call_count",
            "wall_seconds", "error",
        ])
        for td, r, wall in results:
            writer.writerow([
                td,
                int(r.ok),
                r.mode,
                int(r.from_cache),
                f"{r.completeness or 0:.4f}",
                r.elapsed_ms,
                r.api_call_count,
                f"{wall:.2f}",
                (r.error or "").replace("\n", " ")[:200],
            ])
    print(f"\n[报告] CSV 已写入 {report_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="market_fetch_backfill",
        description=(
            "历史回填多个交易日的盘后总结到 data/market.db，"
            "给后续 CLI 评估回测打底。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument(
        "--days", type=int, default=None,
        help="最近 N 个交易日（默认 30；与 --start/--end 互斥）",
    )
    g.add_argument(
        "--start", type=str, default=None,
        help="区间起始 YYYYMMDD（含），需配合 --end",
    )
    p.add_argument(
        "--end", type=str, default=None,
        help="区间结束 YYYYMMDD（含），与 --start 配套",
    )
    p.add_argument(
        "--mode", choices=list(VALID_MODES), default="hybrid",
        help="构建模式，默认 hybrid",
    )
    p.add_argument(
        "--top-n", type=int, default=10,
        help="板块榜单 Top N（默认 10）",
    )
    p.add_argument(
        "--force-refresh", action="store_true",
        help="忽略缓存，所有交易日都重拉",
    )
    p.add_argument(
        "--continue-on-error", action="store_true", default=True,
        help="单日失败不中断（默认开启）",
    )
    p.add_argument(
        "--no-continue", dest="continue_on_error", action="store_false",
        help="单日失败立即终止整个回填",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要回填的日期，不真跑",
    )
    p.add_argument(
        "--report", type=str, default=None,
        help="写入 CSV 汇总报告的路径",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG 级别日志（含 service progress 回调）",
    )
    return p


def _resolve_dates(args: argparse.Namespace) -> List[str]:
    client = TushareClient()

    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("[错误] --start 与 --end 必须同时指定")
        if len(args.start) != 8 or len(args.end) != 8:
            raise SystemExit("[错误] 日期格式必须为 YYYYMMDD")
        if args.start > args.end:
            raise SystemExit("[错误] start 必须 ≤ end")
        return list_trade_dates_in_range(client, args.start, args.end)

    n = args.days if args.days is not None else 30
    if n <= 0:
        raise SystemExit("[错误] --days 必须 > 0")
    dates_desc = list_recent_trade_dates(client, n)
    # 反向，按时间正序回填（便于人看进度）
    return list(reversed(dates_desc))


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    try:
        dates = _resolve_dates(args)
    except TradeDateError as e:
        print(f"[错误] 解析交易日失败: {e}", file=sys.stderr)
        return 2

    if not dates:
        print("[错误] 计算出的交易日列表为空", file=sys.stderr)
        return 2

    print("=" * 60)
    print(f"  Phase M5 回填 — {len(dates)} 个交易日")
    print(f"  范围: {dates[0]} ~ {dates[-1]}")
    print(f"  模式: {args.mode} | force={args.force_refresh}")
    print("=" * 60)
    if args.dry_run:
        print("\n[dry-run] 将回填的交易日（ASC）:")
        for td in dates:
            row = get_calendar_row(td)
            tag = "✓" if (row and row.is_open) else "?"
            print(f"  {tag} {td}")
        print(f"\n共 {len(dates)} 天，未执行（--dry-run）。")
        return 0

    return run_backfill(
        dates,
        mode=args.mode,
        top_n=args.top_n,
        force_refresh=args.force_refresh,
        continue_on_error=args.continue_on_error,
        report_path=Path(args.report) if args.report else None,
    )


if __name__ == "__main__":
    sys.exit(main())
