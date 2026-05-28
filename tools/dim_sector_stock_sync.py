"""板块 ↔ 成员股关联 全量同步 CLI（Phase B / v2 领涨股）。

为什么必须有 CLI: 按 ``.cursor/rules/cli-first-development.mdc``，
所有 services 新方法必须有 CLI 影子，让接口可独立测试 + 未来 web 化。

关联设计：
    doc/design/05-28-1625-板块领涨股与GUI展开施工方案.md

数据来源：Tushare ``dc_member`` 接口（东方财富板块成份股）

用法
----
最简版（按今天交易日同步全部 dc 板块）::

    python tools/dim_sector_stock_sync.py --trade-date 20260527

只同步概念板块（约 486 个，最快）::

    python tools/dim_sector_stock_sync.py --trade-date 20260527 \\
        --idx-type 概念板块

dry-run 看清单不真跑 API::

    python tools/dim_sector_stock_sync.py --trade-date 20260527 --dry-run

退出码::

    0 成功（含全部子板块 OK 或部分失败但 --continue-on-error）
    1 业务失败（trade_date 无数据 / DB 异常）
    2 参数错误
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.market.market_db import get_market_db  # noqa: E402
from services.market.tushare_client import TushareClient  # noqa: E402
from services.market.tushare_fetcher import (  # noqa: E402
    TushareMarketFetcher,
)


_IDX_TYPE_CHOICES = ("概念板块", "行业板块", "地域板块")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "全量同步板块成员股关联（dc_member 接口逐板块拉，约 5-8 分钟）"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--trade-date", required=True,
        help="目标交易日 YYYYMMDD（dc_member 按当日成员快照返回）",
    )
    p.add_argument(
        "--idx-type", default=None, choices=_IDX_TYPE_CHOICES,
        help=(
            "限定 dim_sector.idx_type；不传 = 三类全部"
            "（约 1013 板块，5-8 分钟）"
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="不调 API、不写库，只输出板块清单",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    td = args.trade_date.strip()
    if not (len(td) == 8 and td.isdigit()):
        print(
            f"[ERROR] --trade-date 须为 YYYYMMDD，得到 {args.trade_date!r}",
            file=sys.stderr,
        )
        return 2

    db = get_market_db()
    db.ensure_schema()

    if args.dry_run:
        with db.connect(readonly=True) as conn:
            sql = "SELECT COUNT(*) FROM dim_sector WHERE src='dc'"
            params: list = []
            if args.idx_type:
                sql += " AND idx_type = ?"
                params.append(args.idx_type)
            n = conn.execute(sql, params).fetchone()[0]
        scope = args.idx_type or "三类全部"
        print(
            f"[dry-run] 将同步 {n} 个 dc {scope} 板块的成员（约 "
            f"{n * 0.4:.0f}-{n * 0.6:.0f}s）"
        )
        return 0

    client = TushareClient()
    fetcher = TushareMarketFetcher(db=db, client=client)

    t0 = time.time()
    last_logged = [t0]

    def _progress(done: int, total: int, name: str) -> None:
        now = time.time()
        if done == total or now - last_logged[0] > 5.0 or done % 50 == 0:
            elapsed = now - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(
                f"  [{done:>4}/{total}] {name[:20]:<20} "
                f"耗时 {elapsed:>5.0f}s / ETA {eta:>4.0f}s "
                f"({rate:.1f} 板块/s)",
                flush=True,
            )
            last_logged[0] = now

    print(f"=== 全量同步 dim_sector_stock（trade_date={td}）===")
    if args.idx_type:
        print(f"  范围：dc 源 + idx_type='{args.idx_type}'")
    else:
        print("  范围：dc 源全部三类（概念 + 行业 + 地域）")

    out = fetcher.fetch_dim_sector_stock_full(
        td, idx_type=args.idx_type, progress=_progress,
    )

    elapsed = time.time() - t0
    print(
        f"\n=== 完成（{elapsed:.1f}s）===\n"
        f"  板块总数        : {out['sectors_total']}\n"
        f"  成功            : {out['sectors_ok']}\n"
        f"  失败            : {out['sectors_failed']}\n"
        f"  写入成员关联    : {out['rows_written']}\n"
        f"  API 调用次数    : {out['api_calls']}"
    )

    if out["sectors_failed"] > 0:
        print(
            f"\n[WARN] {out['sectors_failed']} 个板块 dc_member 失败"
            "，可重跑覆盖（重跑会先 DELETE 旧行）",
            file=sys.stderr,
        )

    if out["rows_written"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
