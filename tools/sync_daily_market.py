"""每日行情同步 CLI（Phase 1 Step 1.4）。

用法
----
::

    # 回填最近 30 个交易日（默认）
    python tools/sync_daily_market.py --days 30

    # 单日
    python tools/sync_daily_market.py --date 20260527

    # 区间
    python tools/sync_daily_market.py --start 20260501 --end 20260527

    # 只同步个股 / 只同步板块
    python tools/sync_daily_market.py --days 5 --only stock
    python tools/sync_daily_market.py --days 5 --only sector

    # 强制重写（不跳过已存在数据）
    python tools/sync_daily_market.py --date 20260527 --force

    # dry-run（不发 API，只打印计划）
    python tools/sync_daily_market.py --days 30 --dry-run

输出
----
* 每个交易日一行实时进度
* 结尾打印总耗时 / API 调用次数 / 失败统计
* 失败日落 ``data/sync_failures.log``（追加模式）

退出码
------
* 0 全部 ok
* 1 至少一日失败
* 2 参数错误
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.market.market_db import get_market_db  # noqa: E402
from services.market.sector_daily_sync import (  # noqa: E402
    sync_sector_daily_range,
)
from services.market.stock_daily_sync import (  # noqa: E402
    BatchSyncResult,
    sync_stock_daily_range,
)
from services.market.trade_date import (  # noqa: E402
    TradeDateError,
    trade_dates_between,
)
from services.market.tushare_client import TushareClient  # noqa: E402

_log = logging.getLogger("sync_daily_market")

_FAILURE_LOG = _ROOT / "data" / "sync_failures.log"


# ---------------------------------------------------------------------------
# 参数 + 区间解析
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sync_daily_market",
        description="同步个股 / 板块日行情到 market.db",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--date", help="单日 YYYYMMDD")
    grp.add_argument(
        "--days", type=int,
        help="回填最近 N 个交易日（含今日，需先解算日历）",
    )
    p.add_argument("--start", help="区间起始 YYYYMMDD（与 --end 配对）")
    p.add_argument("--end", help="区间结束 YYYYMMDD（与 --start 配对）")
    p.add_argument(
        "--only",
        choices=("stock", "sector", "both"),
        default="both",
        help="同步范围（默认 both）",
    )
    p.add_argument(
        "--force", action="store_true",
        help="忽略幂等检查，覆盖重拉",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只解析交易日并打印计划，不发任何 API",
    )
    return p.parse_args(argv)


def _resolve_range(
    args: argparse.Namespace,
    client: TushareClient,
) -> Tuple[str, str]:
    """根据 CLI 参数解析 (start, end) 区间。"""
    if args.date:
        if not _is_yyyymmdd(args.date):
            raise SystemExit(f"--date 必须是 YYYYMMDD: {args.date}")
        return args.date, args.date

    if args.start or args.end:
        if not (args.start and args.end):
            raise SystemExit("--start 与 --end 必须同时指定")
        if not (_is_yyyymmdd(args.start) and _is_yyyymmdd(args.end)):
            raise SystemExit("--start/--end 必须是 YYYYMMDD")
        if args.start > args.end:
            raise SystemExit("--start 不能晚于 --end")
        return args.start, args.end

    # 默认行为：days=30
    days = args.days if args.days else 30
    if days <= 0:
        raise SystemExit("--days 必须 > 0")
    today = datetime.now().strftime("%Y%m%d")
    earliest = (
        datetime.now() - timedelta(days=days * 2 + 5)  # 留足周末/节假日 buffer
    ).strftime("%Y%m%d")
    try:
        td_list = trade_dates_between(earliest, today, client=client)
    except TradeDateError as exc:
        raise SystemExit(f"日历解析失败: {exc}")
    if not td_list:
        raise SystemExit(f"区间 [{earliest},{today}] 无开市日")
    selected = td_list[-days:]
    return selected[0], selected[-1]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _print_progress_factory(prefix: str):
    """生成 on_progress 回调：每完成一天打印一行。"""

    def _cb(idx: int, total: int, res) -> None:
        status = "OK" if res.ok else "FAIL"
        flag = " (skipped)" if res.skipped else ""
        msg = (
            f"  [{prefix}] [{status}] {idx}/{total} {res.trade_date} "
            f"rows={res.rows_written} api={res.api_calls} "
            f"elapsed={res.elapsed_ms}ms{flag}"
        )
        if not res.ok and res.error:
            msg += f" | error: {res.error}"
        print(msg, flush=True)

    return _cb


def _write_failures(label: str, batch: BatchSyncResult) -> None:
    """把失败明细落 data/sync_failures.log（追加）。"""
    if not batch.failures:
        return
    _FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    with _FAILURE_LOG.open("a", encoding="utf-8") as f:
        for fail in batch.failures:
            f.write(
                f"{ts}\t{label}\t{fail.trade_date}\t"
                f"{fail.error or 'unknown'}\n"
            )


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    client = TushareClient()
    db = get_market_db()
    db.ensure_schema()

    start, end = _resolve_range(args, client)
    try:
        td_list = trade_dates_between(start, end, client=client)
    except TradeDateError as exc:
        print(f"[FAIL] 日历解析失败: {exc}", file=sys.stderr)
        return 2

    sample = _format_sample(td_list)
    print(
        f"[plan] 范围 [{start}, {end}]: {len(td_list)} 个交易日 "
        f"({sample})"
    )
    print(
        f"[plan] only={args.only} force={args.force} "
        f"dry_run={args.dry_run}"
    )

    if args.dry_run:
        print("[dry-run] 不发 API，结束。")
        return 0

    t0 = time.perf_counter()
    api0 = client.call_count
    total_failed = 0

    if args.only in ("stock", "both"):
        print(f"\n=== 个股日行情同步 ({len(td_list)} 天) ===")
        stock_batch = sync_stock_daily_range(
            start, end,
            db=db, client=client,
            force=args.force,
            on_progress=_print_progress_factory("stock"),
        )
        print(
            f"[stock] done: synced={stock_batch.days_synced} "
            f"skipped={stock_batch.days_skipped} "
            f"failed={stock_batch.days_failed} "
            f"rows={stock_batch.rows_written} "
            f"api={stock_batch.api_calls} "
            f"elapsed={stock_batch.elapsed_ms}ms"
        )
        _write_failures("stock", stock_batch)
        total_failed += stock_batch.days_failed

    if args.only in ("sector", "both"):
        print(f"\n=== 板块日行情同步 ({len(td_list)} 天) ===")
        sector_batch = sync_sector_daily_range(
            start, end,
            db=db, client=client,
            force=args.force,
            on_progress=_print_progress_factory("sector"),
        )
        print(
            f"[sector] done: synced={sector_batch.days_synced} "
            f"skipped={sector_batch.days_skipped} "
            f"failed={sector_batch.days_failed} "
            f"rows={sector_batch.rows_written} "
            f"api={sector_batch.api_calls} "
            f"elapsed={sector_batch.elapsed_ms}ms"
        )
        _write_failures("sector", sector_batch)
        total_failed += sector_batch.days_failed

    total_elapsed = (time.perf_counter() - t0)
    print(
        f"\n[summary] 总耗时 {total_elapsed:.1f}s | "
        f"Tushare API 调用 {client.call_count - api0} 次 | "
        f"失败 {total_failed} 日"
    )

    if total_failed > 0:
        print(f"[summary] 失败详情写入 {_FAILURE_LOG}")
        return 1
    return 0


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()


def _format_sample(td_list: List[str]) -> str:
    """打印计划时的交易日预览（多于 6 天时只取首尾各 3 天）。"""
    if len(td_list) <= 6:
        return " / ".join(td_list)
    return (
        "+".join(td_list[:3]) + " ... " + "+".join(td_list[-3:])
    )


if __name__ == "__main__":
    sys.exit(main())
