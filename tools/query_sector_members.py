"""查板块成员股当日行情 CLI（v2 领涨股 + GUI 展开）。

按 ``.cursor/rules/cli-first-development.mdc`` 要求，给 GUI 懒加载用到的
``query_sector_members`` 函数配套 CLI 入口，便于独立测试 + 未来 web 化。

数据流::

    dim_sector_stock (009 迁移)
        × fact_stock_daily（pct_chg / close）
        × fact_limit_stock（U/Z/D 状态）
            → 按当日 pct_chg DESC 排序

用法
----

查白酒板块 20260527 全部成员（领涨 Top3 在前）::

    python tools/query_sector_members.py BK0896.DC --trade-date 20260527

只看 Top 5 领涨::

    python tools/query_sector_members.py BK0896.DC \\
        --trade-date 20260527 --limit 5

JSON 输出（未来 API 响应体雏形）::

    python tools/query_sector_members.py BK0896.DC \\
        --trade-date 20260527 --json

ths 板块自动 dc fallback::

    python tools/query_sector_members.py 885525.TI --trade-date 20260527

退出码::

    0 成功（含空结果）
    1 业务失败（板块不在 dim_sector_stock + 无 fallback）
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
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from gui.utils.market_db_helper import query_sector_members  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "查板块成员股当日行情（领涨股 Top N + GUI 展开同口径）"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "sector_ts_code",
        help=(
            "板块代码（如 BK0896.DC / 885525.TI）；ths 板块自动 dc fallback"
        ),
    )
    p.add_argument(
        "--trade-date", required=True,
        help="交易日 YYYYMMDD",
    )
    p.add_argument(
        "--limit", type=int, default=200,
        help="最多返回成员数",
    )
    p.add_argument(
        "--json", action="store_true",
        help="以 JSON 数组形式输出（未来 API 响应体雏形）",
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

    rows = query_sector_members(
        args.sector_ts_code.strip(), td, limit=args.limit,
    )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0 if rows else 1

    print(
        f"=== {args.sector_ts_code} @ {td} "
        f"({len(rows)} 个成员，按 pct DESC) ==="
    )
    if not rows:
        print("(无成员关联或当日无行情)")
        return 1

    print(f"  {'股票':<10} {'代码':<13} {'涨幅%':>8} {'收盘':>8}  状态")
    print("  " + "-" * 50)
    for r in rows:
        name = (r["name"] or "")[:10]
        ts = r["ts_code"]
        pct = r["pct_chg"]
        close = r["close"]
        status = r["limit_status"] or "—"
        pct_s = f"{pct:+.2f}" if pct is not None else "—"
        close_s = f"{close:.2f}" if close is not None else "—"
        print(
            f"  {name:<10} {ts:<13} {pct_s:>8} {close_s:>8}  {status}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
