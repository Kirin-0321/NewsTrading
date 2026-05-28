"""聚类后视图 CLI（按 dim_sector_group 取下中位涨幅）。

业务定位
--------
2026-05-28 ai_source=D + cluster_method=llm_only + median_def=lower_median
决策点的查询入口。给 GUI「聚类后视图」+ AI Top N 板块榜去冗余用。

用法
----
::

    python tools/query_grouped_sectors.py 20260527             # 平面表（每组一行）
    python tools/query_grouped_sectors.py 20260527 --tree      # 嵌套：每组 + 成员明细
    python tools/query_grouped_sectors.py 20260527 --json      # JSON 输出
    python tools/query_grouped_sectors.py 20260527 --top 30    # 只看涨幅前 30 组
    python tools/query_grouped_sectors.py 20260527 --count     # 只输出当日组数

退出码
------
* 0  成功
* 1  无数据 / 失败
* 2  参数错
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
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _fmt_yi(v):
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}亿"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="聚类后板块视图（dim_sector_group 取下中位涨幅）",
    )
    p.add_argument(
        "trade_date", type=str, help="交易日 YYYYMMDD（必传）",
    )
    p.add_argument(
        "--tree", action="store_true",
        help="树形输出：每组 + 成员明细（看 is_median 标记）",
    )
    p.add_argument(
        "--top", type=int, default=None,
        help="只输出涨幅前 N 组",
    )
    p.add_argument(
        "--count", action="store_true",
        help="只输出当日组数",
    )
    p.add_argument(
        "--show-extra", action="store_true",
        help="平面 / 树形输出多显示 4 列：换手% / 总市值(亿) / 涨/跌 / "
             "超大单(亿)（2026-05-28 新增；JSON 模式始终全字段）",
    )
    p.add_argument(
        "--json", dest="as_json", action="store_true",
        help="JSON 结构化输出（便于 jq / 后端集成）",
    )
    args = p.parse_args(argv)

    td = args.trade_date.strip()
    if not (td.isdigit() and len(td) == 8):
        print(f"[ERROR] trade_date 应为 YYYYMMDD，收到 {td!r}", file=sys.stderr)
        return 2

    from services.market.sector_grouping import (
        count_grouped_sectors_for_date,
        query_grouped_sectors_for_date,
        query_grouped_sectors_with_members,
    )

    if args.count:
        n = count_grouped_sectors_for_date(td)
        if args.as_json:
            json.dump({"trade_date": td, "groups": n}, sys.stdout)
            sys.stdout.write("\n")
        else:
            print(f"trade_date={td}: {n} 个聚类组")
        return 0 if n > 0 else 1

    if args.tree:
        rows = query_grouped_sectors_with_members(td)
        if args.top:
            rows = rows[:args.top]
        if args.as_json:
            json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
            return 0 if rows else 1
        if not rows:
            print(f"[INFO] {td} 无数据")
            return 1
        print(f"=== 聚类视图 {td} · {len(rows)} 组（树形） ===\n")
        for i, g in enumerate(rows, 1):
            tag = "[未聚类]" if g["group_name"] is None else ""
            print(
                f"{i:3d}. {g['display_name']:14s} ×{g['cnt_in_data']:<2d} "
                f"{tag} median={_fmt_pct(g['median_pct_chg']):>7s}  "
                f"主力{_fmt_yi(g['median_main_net_yi']):>9s}  "
                f"代表={g['median_ts_code']}"
            )
            if g["cnt_in_data"] > 1:
                for m in g["members"]:
                    mark = "★" if m["is_median"] else " "
                    print(
                        f"       {mark} {m['ts_code']:14s} "
                        f"{m['sector_name'][:14]:14s} "
                        f"{_fmt_pct(m['pct_chg']):>7s} "
                        f"主力{_fmt_yi(m['main_net_yi']):>9s} "
                        f"({m['idx_type']}/{m['src']})"
                    )
        return 0

    # 平面输出
    rows = query_grouped_sectors_for_date(td)
    if args.top:
        rows = rows[:args.top]
    if args.as_json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if rows else 1
    if not rows:
        print(f"[INFO] {td} 无数据")
        return 1
    print(f"=== 聚类视图 {td} · {len(rows)} 组（平面） ===")
    if args.show_extra:
        print(
            f"{'组名':14s} ×N   涨幅      5日       "
            f"换手%   总市值(亿)  涨/跌    超大单(亿)  主力(亿)    代表"
        )
        print("-" * 110)
    else:
        print(f"{'组名':14s} ×N   涨幅      5日       主力(亿)    代表板块")
        print("-" * 80)
    for r in rows:
        tag = "[未聚类]" if r["group_name"] is None else ""
        if args.show_extra:
            to = (
                f"{r['turnover_rate']:.2f}%"
                if r.get("turnover_rate") is not None else "—"
            )
            mv = (
                f"{r['total_mv']:,.1f}"
                if r.get("total_mv") is not None else "—"
            )
            elg = (
                f"{r['main_elg_yi']:+.2f}亿"
                if r.get("main_elg_yi") is not None else "—"
            )
            up_n = r.get("up_num")
            dn_n = r.get("down_num")
            ud = (
                f"{up_n}/{dn_n}" if up_n is not None and dn_n is not None
                else "—"
            )
            print(
                f"{r['display_name'][:14]:14s} "
                f"×{r['cnt_in_data']:<2d}  "
                f"{_fmt_pct(r['pct_chg']):>7s}  "
                f"{_fmt_pct(r['pct_chg_5d']):>7s}  "
                f"{to:>6s}  {mv:>10s}  {ud:>6s}  {elg:>10s}  "
                f"{_fmt_yi(r['main_net_yi']):>10s}  "
                f"{r['ts_code']}{tag}"
            )
        else:
            print(
                f"{r['display_name'][:14]:14s} "
                f"×{r['cnt_in_data']:<2d}  "
                f"{_fmt_pct(r['pct_chg']):>7s}  "
                f"{_fmt_pct(r['pct_chg_5d']):>7s}  "
                f"{_fmt_yi(r['main_net_yi']):>10s}  "
                f"{r['ts_code']}{tag}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
