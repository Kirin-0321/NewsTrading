"""板块日行情时间序列查询 CLI（sector_daily_query 服务的薄壳）。

用法::

    # 全量取该板块所有交易日（升序）
    python tools/sector_daily_query.py BK0871.DC

    # 限定区间（含两端）
    python tools/sector_daily_query.py BK0871.DC \\
        --start 20260101 --end 20260527

    # 降序输出（最新日期在前，适合直接看最近行情）
    python tools/sector_daily_query.py BK0871.DC --order desc

    # 仅返回 JSON（脚本可消费，字段对齐 SectorDailyHistory.to_dict）
    python tools/sector_daily_query.py BK0871.DC --json

    # 批量反查板块中文名（题材页板块名列用，stdout 总是 JSON）
    python tools/sector_daily_query.py --names BK0871.DC,BK0477.DC

退出码::

    0  success
    1  板块查询无数据（rows 为空）/ --names 模式下无任何命中
    2  参数错误
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

from services.market.sector_daily_query import (  # noqa: E402
    get_sector_daily_history,
    get_sector_names,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sector_daily_query",
        description="按板块 ts_code 查 fact_sector_daily 时间序列。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "ts_code",
        help="板块代码，例 BK0477.DC（与 theme_predictions.sector_ts_code 对齐）；"
             "在 --names 模式下作为逗号分隔的代码列表",
    )
    parser.add_argument(
        "--names", action="store_true",
        help="批量反查板块中文名模式：ts_code 视为逗号分隔列表，"
             "stdout 总输出 JSON {ts_code: name}（其它参数被忽略）",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="起始交易日 YYYYMMDD（含），默认不限",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="终止交易日 YYYYMMDD（含），默认不限",
    )
    parser.add_argument(
        "--order", choices=["asc", "desc"], default="asc",
        help="按交易日排序方向（默认 asc）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="以 JSON 输出（字段对齐 SectorDailyHistory.to_dict）",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="只展示前 N 行（0 = 全部）",
    )
    return parser


def _validate_yyyymmdd(label: str, val) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and len(val) == 8 and val.isdigit():
        return True
    print(f"[错误] --{label} 应为 YYYYMMDD，实际: {val!r}", file=sys.stderr)
    return False


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    if args.names:
        codes = [c.strip() for c in args.ts_code.split(",") if c.strip()]
        if not codes:
            print("[错误] --names 模式下 ts_code 不能为空", file=sys.stderr)
            return 2
        name_map = get_sector_names(codes)
        print(json.dumps(name_map, ensure_ascii=False, indent=2))
        return 0 if name_map else 1

    if not _validate_yyyymmdd("start", args.start):
        return 2
    if not _validate_yyyymmdd("end", args.end):
        return 2

    history = get_sector_daily_history(
        args.ts_code,
        start_date=args.start,
        end_date=args.end,
        order=args.order,
    )

    if args.json:
        payload = history.to_dict()
        if args.limit > 0:
            payload["rows"] = payload["rows"][: args.limit]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if history.count > 0 else 1

    # 人类可读输出
    print("=" * 72)
    print(
        f"  板块日行情序列  ts_code={history.ts_code}  "
        f"name={history.name or '—'}  "
        f"idx_type={history.idx_type or '—'}  "
        f"in_dim={history.in_dim}  count={history.count}"
    )
    print("=" * 72)

    if history.count == 0:
        if not history.in_dim:
            print(
                "  （板块代码不存在于 dim_sector 字典——"
                "多半是 ts_code 写错了，或 dim_sector 未同步最新板块字典）"
            )
        else:
            print(
                f"  （板块「{history.name}」({history.idx_type}）"
                f"在 dim_sector 有名字但 fact_sector_daily 无数据。\n"
                "   原因：Tushare moneyflow_ind_dc 接口只覆盖"
                "概念板块的资金流，行业板块类不返。\n"
                "   修复路径：补抓 moneyflow_ind_ths / moneyflow_ind_sw "
                "—— 主线施工任务，本 CLI 改不动。）"
            )
        return 1

    rows = history.rows[: args.limit] if args.limit > 0 else history.rows

    header = (
        f"  {'交易日':<10}  {'涨跌%':>8}  {'主力净(亿)':>10}  "
        f"{'排名':>5}  {'5日%':>8}  {'涨停':>5}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for r in rows:
        print(
            f"  {r.trade_date:<10}  "
            f"{_fmt_pct(r.pct_chg):>8}  "
            f"{_fmt_num(r.main_net_yi):>10}  "
            f"{_fmt_int(r.rank_today):>5}  "
            f"{_fmt_pct(r.pct_chg_5d):>8}  "
            f"{_fmt_int(r.limit_up_count):>5}"
        )

    if args.limit > 0 and history.count > args.limit:
        print(f"  ...（共 {history.count} 行，已截断到前 {args.limit}）")

    return 0


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v) -> str:
    if v is None:
        return "—"
    try:
        return str(int(v))
    except (TypeError, ValueError):
        return "—"


if __name__ == "__main__":
    sys.exit(main())
