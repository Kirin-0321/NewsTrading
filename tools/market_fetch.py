"""盘后数据 CLI 入口（Phase M1b）。

用法::

    # 最近一个交易日（自动解析），tushare-only 模式
    python tools/market_fetch.py

    # 指定日期
    python tools/market_fetch.py 20260525

    # 强制重拉（先 DELETE 当天所有 fact_* 数据再 fetch）
    python tools/market_fetch.py 20260525 --force-refresh

    # 板块榜单取前 50（默认 30，对应 v3 聚类版）
    python tools/market_fetch.py 20260525 --top-n 50

    # 只打印不入库（暂未实现，build 总会入库；可通过 --dry-run 改）
    python tools/market_fetch.py 20260525 --print-md

退出码::

    0  success（``MarketSummaryResult.ok == True``）
    1  失败 / 用户取消 / build 抛异常
    2  参数错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from services.market.service import MarketSummaryService  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="market_fetch",
        description="抓取并生成指定交易日的盘后总结（Phase M1b: tushare-only）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "trade_date",
        nargs="?",
        default=None,
        help="交易日 YYYYMMDD；省略时取最近一个开市日",
    )
    parser.add_argument(
        "--mode",
        choices=["tushare-only", "hybrid", "ai-full"],
        default="tushare-only",
        help="构建模式（M1b 阶段后两个会自动降级为 tushare-only）",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=30,
        help="板块榜单 Top N（默认 30，对应 v3 聚类版）",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="忽略缓存，先清空当日 fact_* 数据再重拉",
    )
    parser.add_argument(
        "--print-md",
        action="store_true",
        help="把渲染的 compact Markdown 也打印到 stdout",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="把 canonical summary JSON 打印到 stdout",
    )
    parser.add_argument(
        "--save-md",
        type=str,
        default=None,
        help="把渲染的 Markdown 同时另存到该路径（便于人工预览）",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="DEBUG 级别日志",
    )
    return parser


def _print_summary_header(result, args) -> None:
    print("=" * 72)
    print(f"  Phase M1b CLI —— {result.trade_date} 盘后总结")
    print("=" * 72)
    print(f"  mode             : {result.mode}")
    print(f"  prev_trade_date  : {result.prev_trade_date}")
    print(f"  ok               : {result.ok}")
    print(f"  from_cache       : {result.from_cache}")
    print(f"  completeness     : {result.completeness:.2%}")
    print(f"  elapsed          : {result.elapsed_ms} ms")
    print(f"  api_call_count   : {result.api_call_count}")
    print(f"  cancelled        : {result.cancelled}")
    print(f"  error            : {result.error or '—'}")
    if result.warnings:
        print(f"  warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"    - {w}")
    if result.gaps:
        print(f"  gaps ({len(result.gaps)}):")
        for g in result.gaps[:10]:
            field = g.get("field", "?") if isinstance(g, dict) else str(g)
            reason = g.get("reason", "") if isinstance(g, dict) else ""
            print(f"    - {field}: {reason}")
        if len(result.gaps) > 10:
            print(f"    - ...（共 {len(result.gaps)} 条）")
    print("=" * 72)


def _print_kpi_table(summary: dict) -> None:
    indices = summary.get("indices") or {}
    breadth = summary.get("breadth") or {}
    sentiment = summary.get("sentiment") or {}
    cf = summary.get("capital_flow") or {}

    print("\n[KPI 快照]")
    print(
        "  上证 {0:>7}  / 深成 {1:>7}  / 创业 {2:>7}".format(
            _fmt_pct((indices.get("sh") or {}).get("pct_chg")),
            _fmt_pct((indices.get("sz") or {}).get("pct_chg")),
            _fmt_pct((indices.get("cyb") or {}).get("pct_chg")),
        )
    )
    print(
        "  两市成交额 {0} 亿（昨日 {1} 亿）".format(
            _fmt_num(indices.get("total_turnover_yi")),
            _fmt_num(indices.get("prev_turnover_yi")),
        )
    )
    print(
        "  涨停 {u}  / 跌停 {d}  / 炸板 {z}  / 封板率 {sr}  / 晋级率 {pr}".format(
            u=breadth.get("limit_up") or "—",
            d=breadth.get("limit_down") or "—",
            z=breadth.get("failed_limit") or "—",
            sr=_fmt_ratio(sentiment.get("seal_rate")),
            pr=_fmt_ratio(sentiment.get("promotion_rate")),
        )
    )
    print(
        "  市场最高板 {h}板 《{s}》 / 题材 {t}".format(
            h=sentiment.get("max_height") or "—",
            s=sentiment.get("max_stock") or "—",
            t=sentiment.get("max_sector") or "—",
        )
    )
    north_suffix = ""
    if cf.get("north_is_delayed"):
        north_suffix = f"（⚠️ T-1，实际 {cf.get('north_data_date')}）"
    print(
        "  北向 {n} 亿{suffix} / 南向 {s} 亿".format(
            n=_fmt_num(cf.get("north_net_yi")),
            s=_fmt_num(cf.get("south_net_yi")),
            suffix=north_suffix,
        )
    )

    # Top 5 板块速览
    sectors = summary.get("sectors_top") or []
    if sectors:
        print("\n[板块涨幅 Top 5]")
        for s in sectors[:5]:
            print(
                "  #{r}  {n:<14}  {p:>7}  5日 {p5:>7}  主力净 {m} 亿".format(
                    r=s.get("rank") or "?",
                    n=str(s.get("name") or "")[:14],
                    p=_fmt_pct(s.get("pct_chg")),
                    p5=_fmt_pct(s.get("pct_chg_5d")),
                    m=_fmt_num(s.get("main_net_yi")),
                )
            )


def _print_ingest_stats(summary: dict) -> None:
    """打印每张 fact 表的入库行数（从 quality / 自己反查 DB 都可）。"""
    # quality 里没存 ingest 细节，简单从 summary 衍生几个数字
    sectors = summary.get("sectors_top") or []
    print("\n[数据规模]")
    print(f"  sectors_top      : {len(sectors)}")
    ladder = summary.get("limit_ladder") or {}
    for k, v in ladder.items():
        if v:
            print(f"  limit_ladder.{k}: {len(v)} 只")
    dt = summary.get("dragon_tiger") or {}
    print(
        f"  dragon_tiger     : 个股 {len(dt.get('stocks') or [])} / "
        f"知名席位 {len(dt.get('famous_traders') or [])}"
    )


def _fmt_pct(val) -> str:
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_ratio(val) -> str:
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(val) -> str:
    if val is None or val == "":
        return "—"
    try:
        return f"{float(val):.2f}"
    except (TypeError, ValueError):
        return "—"


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.trade_date is not None:
        td = args.trade_date.strip()
        if not (len(td) == 8 and td.isdigit()):
            print(f"[错误] trade_date 格式应为 YYYYMMDD，实际: {td!r}",
                  file=sys.stderr)
            return 2

    service = MarketSummaryService()

    def _progress(msg: str) -> None:
        print(f"  · {msg}")

    result = service.build(
        trade_date=args.trade_date,
        mode=args.mode,
        top_sector_n=args.top_n,
        force_refresh=args.force_refresh,
        progress_callback=_progress,
    )

    _print_summary_header(result, args)

    if not result.ok:
        return 1

    summary = result.summary_json or {}
    _print_kpi_table(summary)
    _print_ingest_stats(summary)

    if args.print_md and result.summary_md:
        print("\n" + "=" * 72)
        print("  Compact Markdown")
        print("=" * 72)
        print(result.summary_md)

    if args.print_json:
        print("\n" + "=" * 72)
        print("  Canonical Summary JSON")
        print("=" * 72)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if args.save_md and result.summary_md:
        out = Path(args.save_md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.summary_md, encoding="utf-8")
        print(f"\n[保存] Markdown 已写入 {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
