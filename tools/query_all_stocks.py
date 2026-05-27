"""查某交易日全 A 股行情 → stdout（GUI 全部个股 Tab 的 CLI 影子）。

为什么必须有 CLI（参考 .cursor/rules/cli-first-development.mdc）::

    GUI 全部个股 Tab 触发 query_all_stocks(trade_date)
    ↓ 同接口必须有"无 UI 调用契约"
    本脚本 = 该接口的 CLI 入口，参数即未来 API 请求体，
    --json 输出即未来 API 响应体。

用法::

    python tools/query_all_stocks.py --date 20260526
    python tools/query_all_stocks.py --date 20260526 --json
    python tools/query_all_stocks.py --date 20260526 --filter U  # 只看涨停
    python tools/query_all_stocks.py --date 20260526 --top 20    # 只看涨幅 Top20
    python tools/query_all_stocks.py --date 20260526 --search 茅台  # 模糊匹配代码/名称

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

from gui.utils.market_db_helper import query_all_stocks  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="查某交易日全 A 股行情",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--date", required=True,
        help="目标交易日 YYYYMMDD",
    )
    p.add_argument(
        "--filter", choices=["U", "Z", "D"], default=None,
        help="按涨停标过滤：U=涨停 / Z=炸板 / D=跌停",
    )
    p.add_argument(
        "--search", default=None,
        help="按代码或名称模糊匹配（大小写不敏感）",
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="只输出前 N 行（已按涨跌幅倒序）",
    )
    p.add_argument(
        "--json", dest="as_json", action="store_true",
        help="输出 JSON 数组到 stdout（字段对齐 service 返回 dict）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    td = args.date.strip()
    if not (len(td) == 8 and td.isdigit()):
        print(f"[ERROR] --date 须为 YYYYMMDD，得到 {args.date!r}", file=sys.stderr)
        return 2

    rows = query_all_stocks(td)
    if not rows:
        print(f"[WARN] {td} 无 fact_stock_daily 数据", file=sys.stderr)
        return 1

    if args.filter:
        rows = [r for r in rows if r.get("limit_type") == args.filter]

    if args.search:
        kw = args.search.lower()
        rows = [
            r for r in rows
            if kw in r["ts_code"].lower() or kw in r["name"].lower()
        ]

    if args.top is not None and args.top > 0:
        rows = rows[: args.top]

    if args.as_json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"trade_date={td}  rows={len(rows)}")
        print(
            f"{'代码':<11} {'名称':<10} {'行业':<10} "
            f"{'涨幅':>8} {'收盘':>8} {'成交额(亿)':>12} {'标':>3}"
        )
        for r in rows:
            pct = (
                f"{r['pct_chg']:+.2f}%" if r["pct_chg"] is not None else "—"
            )
            close = (
                f"{r['close']:,.2f}" if r["close"] is not None else "—"
            )
            amt = (
                f"{r['amount_yi']:,.2f}"
                if r["amount_yi"] is not None else "—"
            )
            mark = r["limit_type"] or ""
            print(
                f"{r['ts_code']:<11} {r['name']:<10} {r['industry']:<10} "
                f"{pct:>8} {close:>8} {amt:>12} {mark:>3}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
