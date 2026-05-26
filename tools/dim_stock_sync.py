"""dim_stock 维度表同步 CLI（Phase M6）。

一次性把 Tushare ``stock_basic`` 接口的全市场股票基础信息拉到
``data/market.db.dim_stock``，给后续所有按 ts_code 反查股名 / 行业 / 上市日期
的逻辑打底。

用法
----

第一次初始化（在市股 ~5400 只，1 次 API，~10 秒）::

    python tools/dim_stock_sync.py

同时拉退市股（让历史 ts_code 也能查到名字）::

    python tools/dim_stock_sync.py --include-delisted

预览（不真拉）::

    python tools/dim_stock_sync.py --dry-run

退出码
------

0  成功
1  拉取失败
2  参数错误

何时重跑
--------

* 新股密集上市后（每月一次足够）
* 发现某只新股 GUI 显示「—」（说明 dim_stock 没这只）
* 退市股需要保留时（带 ``--include-delisted``）
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


_log = logging.getLogger("dim_stock_sync")


def _count_existing() -> int:
    db = get_market_db()
    with db.connect(readonly=True) as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
        )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dim_stock_sync",
        description="把 Tushare stock_basic 全市场拉到 dim_stock 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--include-delisted", action="store_true",
        help="同时拉退市股（list_status=D），让历史 ts_code 都能反查名字",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="只打印当前行数和将要做的事，不真拉",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="DEBUG 级别日志",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    before = _count_existing()
    print("=" * 60)
    print("  dim_stock 同步")
    print(f"  当前行数: {before}")
    print(f"  模式: {'在市+退市' if args.include_delisted else '仅在市股'}")
    print("=" * 60)

    if args.dry_run:
        print("\n[dry-run] 将调用 fetch_dim_stock_full(...)，未执行")
        return 0

    fetcher = TushareMarketFetcher(
        client=TushareClient(),
        db=get_market_db(),
    )
    try:
        result = fetcher.fetch_dim_stock_full(
            include_delisted=args.include_delisted,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[错误] 同步失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    after = _count_existing()
    print()
    print("=" * 60)
    print(f"  在市股写入: {result['L']} 行")
    if args.include_delisted:
        print(f"  退市股写入: {result['D']} 行")
    print(f"  本次入库总数: {result['total']} 行")
    print(f"  dim_stock 行数: {before} → {after}（净增 {after - before}）")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
