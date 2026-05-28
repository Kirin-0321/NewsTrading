"""查某交易日全部板块行情 → stdout（GUI 全部板块 Tab 的 CLI 影子）。

为什么必须有 CLI: 同 query_all_stocks.py，参考
:doc:`.cursor/rules/cli-first-development.mdc`。

用法::

    python tools/query_all_sectors.py --date 20260526
    python tools/query_all_sectors.py --date 20260526 --json
    python tools/query_all_sectors.py --date 20260526 --top 20
    python tools/query_all_sectors.py --date 20260526 --bottom 10
    python tools/query_all_sectors.py --date 20260526 --search 半导体

    # 2026-05-28 多源改造后：按 idx_type 单一来源筛选
    python tools/query_all_sectors.py --date 20260527 --idx-type 同花顺概念
    python tools/query_all_sectors.py --date 20260527 --idx-type 行业板块

    # 查当日各 idx_type 命中行数（GUI ComboBox 动态文案数据源）
    python tools/query_all_sectors.py --date 20260527 --counts

退出码::

    0 成功
    1 业务失败（trade_date 无该日数据 / 数据库异常）
    2 参数错误
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from gui.utils.market_db_helper import (  # noqa: E402
    query_all_sectors,
    query_sector_idx_type_counts,
)


_IDX_TYPE_CHOICES = (
    "概念板块", "行业板块", "地域板块", "同花顺行业", "同花顺概念",
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="查某交易日全部板块行情",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--date", required=True,
        help="目标交易日 YYYYMMDD",
    )
    p.add_argument(
        "--search", default=None,
        help="按板块名模糊匹配",
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="只输出涨幅前 N 名",
    )
    p.add_argument(
        "--bottom", type=int, default=None,
        help="只输出跌幅前 N 名（与 --top 互斥；同时给则取 --bottom）",
    )
    p.add_argument(
        "--idx-type", default=None, choices=_IDX_TYPE_CHOICES,
        help="按 dim_sector.idx_type 单源筛选（不传 = 全部 5 源合并）",
    )
    p.add_argument(
        "--counts", action="store_true",
        help="只输出当日各 idx_type 命中行数（不返回明细）",
    )
    p.add_argument(
        "--json", dest="as_json", action="store_true",
        help="输出 JSON 数组到 stdout",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    td = args.date.strip()
    if not (len(td) == 8 and td.isdigit()):
        print(f"[ERROR] --date 须为 YYYYMMDD，得到 {args.date!r}", file=sys.stderr)
        return 2

    if args.counts:
        counts = query_sector_idx_type_counts(td)
        if not counts:
            print(f"[WARN] {td} 无数据", file=sys.stderr)
            return 1
        if args.as_json:
            json.dump(counts, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            print(f"trade_date={td}  idx_type 分布:")
            total = 0
            for it, c in sorted(counts.items(), key=lambda x: -x[1]):
                print(f"  {it:10s} {c}")
                total += c
            print(f"  {'合计':10s} {total}")
        return 0

    rows = query_all_sectors(td, idx_type=args.idx_type)
    if not rows:
        print(
            f"[WARN] {td}"
            f"{(' idx_type=' + args.idx_type) if args.idx_type else ''}"
            f" 无 fact_sector_daily 数据",
            file=sys.stderr,
        )
        return 1

    if args.search:
        kw = args.search.lower()
        rows = [r for r in rows if kw in r["name"].lower()]

    if args.bottom is not None and args.bottom > 0:
        rows = rows[-args.bottom:][::-1]
    elif args.top is not None and args.top > 0:
        rows = rows[: args.top]

    if args.as_json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"trade_date={td}  rows={len(rows)}")
        print(
            f"{'#':>3} {'板块':<14} {'涨幅':>8} {'5日':>8} "
            f"{'涨停':>4} {'主力(亿)':>10} {'龙头股 Top3'}"
        )
        for r in rows:
            pct = (
                f"{r['pct_chg']:+.2f}%" if r["pct_chg"] is not None else "—"
            )
            pct5 = (
                f"{r['pct_chg_5d']:+.2f}%"
                if r["pct_chg_5d"] is not None else "—"
            )
            lu = (
                str(r["limit_up_count"])
                if r["limit_up_count"] is not None else "—"
            )
            mn = (
                f"{r['main_net_yi']:,.2f}"
                if r["main_net_yi"] is not None else "—"
            )
            leaders = " · ".join(
                f"{ld['name']}({ld.get('status') or ''})"
                for ld in (r.get("leaders") or [])[:3]
            ) or "—"
            print(
                f"{r['rank']:>3} {r['name']:<14} {pct:>8} {pct5:>8} "
                f"{lu:>4} {mn:>10} {leaders}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
