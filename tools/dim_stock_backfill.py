"""维度表 dim_stock 一次性填充工具。

调 Tushare ``stock_basic`` 接口拉全市场股票字典（ts_code → name /
market / industry / list_date），一次入库 ``data/market.db.dim_stock``。

用法
----

只补在市股（默认，约 5000 只，1 次 API）::

    python tools/dim_stock_backfill.py

含退市 + 暂停上市（约 6500 只，3 次 API）::

    python tools/dim_stock_backfill.py --status ALL

仅模拟不真跑::

    python tools/dim_stock_backfill.py --dry-run

退出码
------

* 0 成功
* 1 失败（已写入的行不会回滚）
* 2 参数错误

后续维护
--------

* 推荐**每月手动重跑一次**覆盖新上市股 / 改名 / 退市
* 也可加进 ``schedule_page`` 的定时任务月度档（待主人决定）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.market.market_db import get_market_db  # noqa: E402
from services.market.tushare_client import TushareClient  # noqa: E402
from services.market.tushare_fetcher import TushareMarketFetcher  # noqa: E402

_log = logging.getLogger("dim_stock_backfill")

VALID_STATUS = ("L", "D", "P", "ALL")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dim_stock_backfill",
        description=(
            "一次性把 Tushare stock_basic 全量拉进 data/market.db.dim_stock，"
            "给龙虎榜个股名 / 涨停股名 / 板块龙头股名等场景做 ts_code→name 反查。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--status", choices=list(VALID_STATUS), default="L",
        help="L=在市股（默认）/ D=退市 / P=暂停上市 / ALL=全部",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只打印将要做的事，不真跑",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG 级别日志",
    )
    return p


def _resolve_statuses(arg: str) -> List[str]:
    if arg == "ALL":
        return ["L", "D", "P"]
    return [arg]


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    statuses = _resolve_statuses(args.status)

    print("=" * 60)
    print("  dim_stock 全量回填")
    print(f"  list_status: {statuses}")
    print("=" * 60)

    if args.dry_run:
        print(f"\n[dry-run] 将对 {len(statuses)} 个 status 各调一次 "
              f"stock_basic（最多 {len(statuses)} 次 API）。")
        return 0

    client = TushareClient()
    db = get_market_db()
    fetcher = TushareMarketFetcher(client, db)

    total_written = 0
    for st in statuses:
        try:
            n = fetcher.fetch_dim_stock_full(list_status=st)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ status={st}: {type(exc).__name__}: {exc}")
            return 1
        print(f"  ✅ status={st}: {n:>5} 行入库")
        total_written += n

    with db.connect(readonly=True) as conn:
        total_in_table = conn.execute(
            "SELECT COUNT(*) FROM dim_stock"
        ).fetchone()[0]
        sample = conn.execute(
            "SELECT ts_code, name, market, industry FROM dim_stock "
            "ORDER BY ts_code LIMIT 5"
        ).fetchall()

    print()
    print("=" * 60)
    print(f"  本次写入: {total_written} 行")
    print(f"  表中现有: {total_in_table} 行")
    print("=" * 60)
    print("\n样本（前 5 条 ts_code 升序）：")
    for r in sample:
        print(f"  {r['ts_code']:<12} {r['name']:<10} "
              f"{(r['market'] or '-'):<8} {r['industry'] or '-'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
