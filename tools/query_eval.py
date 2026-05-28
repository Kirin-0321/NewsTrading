"""模板评估 / 题材打分明细 查询 CLI（2026-05-27 评估页改造 + 2026-05-28 树形展开扩展）。

业务定位
--------
让主人在 **不开 GUI** 的情况下也能看 `scoring_service.*` 的全部结果，方便
CI 比对 / 远程 SSH / 跑批后立刻校验，也是 GUI 评估页改造的"对照样板"。

对应服务层：
* :func:`services.scoring.scoring_service.get_template_eval`（模板汇总）
* :func:`services.scoring.scoring_service.get_report_eval`（**报告级**明细）
* :func:`services.scoring.scoring_service.get_theme_eval_for_report`（**树第 2 层** 报告→题材）
* :func:`services.scoring.scoring_service.get_stock_scores_for_theme`（**树第 3 层** 题材→标的）
* :func:`services.scoring.scoring_service.get_theme_score_detail`（单题材逐日）

五种模式
--------
::

    # 模式 1：模板汇总（默认）
    python tools/query_eval.py
    python tools/query_eval.py --by template

    # 模式 2：报告级明细（按 ai_reports.id 一行一份报告）
    python tools/query_eval.py --by report
    python tools/query_eval.py --by report --prompt-id custom_6

    # 模式 3：报告下题材级明细（评估页树第 2 层对照）
    python tools/query_eval.py --by theme_in_report --report-id 1234

    # 模式 4：题材下标的级明细（评估页树第 3 层对照）
    python tools/query_eval.py --by stocks_in_theme --theme-id 5678

    # 模式 5：单题材打分逐日明细
    python tools/query_eval.py --theme-id 999971

通用参数
--------
::

    --days N             最近 N 自然日（默认 30）
    --time-dim DIM       score_date（CLI 默认）/ report_date（GUI 默认）
    --backtest-only      仅回测（is_backtest=1）
    --real-only          仅真实（is_backtest=0）
    --keep-version       按 (prompt_id, version) 聚合（仅 template 模式）
    --report-id ID       仅 theme_in_report 模式必填
    --json               JSON 输出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.scoring.scoring_service import (  # noqa: E402
    get_report_eval,
    get_stock_scores_for_theme,
    get_template_eval,
    get_theme_eval_for_report,
    get_theme_score_detail,
)


# ---------------------------------------------------------------------------
# 打印辅助
# ---------------------------------------------------------------------------


def _fmt_pct(v) -> str:
    if v is None:
        return "    -"
    return f"{float(v):+6.2f}%"


def _fmt_rate(v) -> str:
    if v is None:
        return "    -"
    return f"{float(v) * 100:5.1f}%"


_STATUS_TAG = {
    "none": "未打分",
    "partial": "部分",
    "full": "完整",
}


def _print_template_table(rows: List[dict]) -> None:
    if not rows:
        print("  (空) 没有任何模板评估样本")
        return

    hdr = (
        f"{'prompt_id':22s} {'ver':6s} {'题材':>5s} {'已打':>5s} "
        f"{'D+1':>7s} {'D+2':>7s} {'D+3':>7s} {'D+4':>7s} {'D+5':>7s} "
        f"{'alpha':>7s} {'命中率':>6s} {'方向准':>6s} {'最近报告日':>10s}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        pid = (r.get("prompt_id") or "")[:22]
        ver = (r.get("prompt_version") or "-")[:6]
        n_total = r.get("themes_total") or r.get("sample_count") or 0
        n_scored = r.get("scored_themes") or 0
        last = (r.get("last_report_date") or "-")[:10]
        print(
            f"{pid:22s} {ver:6s} {n_total:5d} {n_scored:5d} "
            f"{_fmt_pct(r.get('d1_avg'))} "
            f"{_fmt_pct(r.get('d2_avg'))} "
            f"{_fmt_pct(r.get('d3_avg'))} "
            f"{_fmt_pct(r.get('d4_avg'))} "
            f"{_fmt_pct(r.get('d5_avg'))} "
            f"{_fmt_pct(r.get('alpha_avg'))} "
            f"{_fmt_rate(r.get('hit_rate_avg'))} "
            f"{_fmt_rate(r.get('direction_correct_rate'))} "
            f"{last:>10s}"
        )


def _print_report_table(rows: List[dict]) -> None:
    if not rows:
        print("  (空) 没有任何报告级评估样本")
        return

    hdr = (
        f"{'rid':>5s} {'report_date':10s} {'B/R':>3s} "
        f"{'prompt_id':18s} {'ver':5s} {'题材':>4s} "
        f"{'打分':>9s} {'状态':>6s} "
        f"{'D+1':>7s} {'D+2':>7s} {'D+3':>7s} {'D+4':>7s} {'D+5':>7s} "
        f"{'alpha':>7s} {'命中率':>6s}  文件名"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        rid = r.get("report_id") or 0
        bt = "回测" if int(r.get("is_backtest") or 0) == 1 else "真实"
        pid = (r.get("prompt_id") or "")[:18]
        ver = (r.get("prompt_version") or "-")[:5]
        n = int(r.get("themes_count") or 0)
        scored = int(r.get("scored_pairs") or 0)
        expected = int(r.get("expected_pairs") or 0)
        status = _STATUS_TAG.get(r.get("score_status") or "none", "?")
        fp = r.get("file_path") or ""
        fname = Path(fp).name if fp else "-"
        score_cell = f"{scored:>3d}/{expected:<4d}"
        print(
            f"{rid:5d} {r['report_date']:10s} {bt:>3s} "
            f"{pid:18s} {ver:5s} {n:4d} "
            f"{score_cell:>9s} {status:>6s} "
            f"{_fmt_pct(r.get('d1_avg'))} "
            f"{_fmt_pct(r.get('d2_avg'))} "
            f"{_fmt_pct(r.get('d3_avg'))} "
            f"{_fmt_pct(r.get('d4_avg'))} "
            f"{_fmt_pct(r.get('d5_avg'))} "
            f"{_fmt_pct(r.get('alpha_avg'))} "
            f"{_fmt_rate(r.get('hit_rate_avg'))}  {fname}"
        )


def _print_themes_in_report(report_id: int, rows: List[dict]) -> None:
    """评估页树第 2 层（报告→题材）的 CLI 对照打印。"""
    print(f"\n=== report_id={report_id} 下题材级明细 ===")
    if not rows:
        print("  (空) 该报告下没有题材")
        return
    hdr = (
        f"  {'tid':>5s} {'题材名':16s} {'等级':6s} {'分':>4s} "
        f"{'板块代码':12s} {'股':>3s} {'打':>3s} "
        f"{'D+1':>7s} {'D+2':>7s} {'D+3':>7s} {'D+4':>7s} {'D+5':>7s} "
        f"{'alpha':>7s} {'命中率':>6s}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        tid = r.get("theme_id") or 0
        name = (r.get("theme_name") or "")[:16]
        level = (r.get("strength_level") or "—")[:6]
        score = r.get("strength_score") or 0
        ts_code = (r.get("sector_ts_code") or "—")[:12]
        n_stocks = r.get("stocks_count") or 0
        scored = r.get("scored_pairs") or 0
        print(
            f"  {tid:5d} {name:16s} {level:6s} {score:>4d} "
            f"{ts_code:12s} {n_stocks:3d} {scored:3d} "
            f"{_fmt_pct(r.get('d1'))} "
            f"{_fmt_pct(r.get('d2'))} "
            f"{_fmt_pct(r.get('d3'))} "
            f"{_fmt_pct(r.get('d4'))} "
            f"{_fmt_pct(r.get('d5'))} "
            f"{_fmt_pct(r.get('alpha_avg'))} "
            f"{_fmt_rate(r.get('hit_rate_avg'))}"
        )


def _print_stocks_in_theme(theme_id: int, rows: List[dict]) -> None:
    """评估页树第 3 层（题材→标的）的 CLI 对照打印。"""
    print(f"\n=== theme_id={theme_id} 下标的逐日明细 ===")
    if not rows:
        print("  (空) 该题材下没有标的")
        return

    def _hit_mark(v) -> str:
        if v is None:
            return "  -"
        return "  ✓" if int(v) == 1 else "  ✗"

    hdr = (
        f"  {'tsid':>5s} {'标的名':12s} {'代码(标准化)':14s} {'角色':4s} {'打':>3s} "
        f"{'D+1%':>7s} {'h1':>2s} {'D+2%':>7s} {'h2':>2s} "
        f"{'D+3%':>7s} {'h3':>2s} {'D+4%':>7s} {'h4':>2s} "
        f"{'D+5%':>7s} {'h5':>2s}"
    )
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        tsid = r.get("theme_stock_id") or 0
        name = (r.get("stock_name") or "")[:12]
        ncode = (r.get("normalized_code") or "—")[:14]
        role = (r.get("role") or "—")[:4]
        scored = r.get("scored_days") or 0
        print(
            f"  {tsid:5d} {name:12s} {ncode:14s} {role:4s} {scored:3d} "
            f"{_fmt_pct(r.get('d1_pct'))}{_hit_mark(r.get('d1_hit'))} "
            f"{_fmt_pct(r.get('d2_pct'))}{_hit_mark(r.get('d2_hit'))} "
            f"{_fmt_pct(r.get('d3_pct'))}{_hit_mark(r.get('d3_hit'))} "
            f"{_fmt_pct(r.get('d4_pct'))}{_hit_mark(r.get('d4_hit'))} "
            f"{_fmt_pct(r.get('d5_pct'))}{_hit_mark(r.get('d5_hit'))}"
        )


def _print_theme_detail(theme_id: int, rows: List[dict]) -> None:
    print(f"\n=== theme_id={theme_id} 打分明细 ===")
    if not rows:
        print("  (空) 该 theme_id 暂无打分记录")
        return
    print(
        f"  {'score_date':10s} {'D+N':>3s} "
        f"{'sector':>7s} {'std_avg':>7s} {'std_w':>7s} "
        f"{'hit':>4s} {'bench':>7s} {'α+N':>7s} {'dir':>4s}"
    )
    print("  " + "-" * 74)
    for r in rows:
        dir_c = r.get("direction_correct")
        dir_s = "-" if dir_c is None else ("✓" if dir_c else "✗")
        # 2026-05-28 22:30 α 体系金字塔重构：单日 alpha 字段已 DROP，
        # 这里改为现推 α+N = theme_pct - benchmark_zz1000_pct（与 GUI 一致）
        tp = r.get("theme_pct")
        zz = r.get("benchmark_zz1000_pct")
        a_n = (tp - zz) if (tp is not None and zz is not None) else None
        print(
            f"  {r['score_date']} {r['days_offset']:>3d} "
            f"{_fmt_pct(r.get('sector_pct'))} "
            f"{_fmt_pct(r.get('stock_avg_pct'))} "
            f"{_fmt_pct(r.get('stock_weighted_pct'))} "
            f"{(r.get('hit_count') or 0):2d}/{(r.get('total_count') or 0):2d} "
            f"{_fmt_pct(r.get('benchmark_pct'))} "
            f"{_fmt_pct(a_n)} "
            f"{dir_s:>4s}"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="query_eval",
        description=(
            "模板评估 / 报告级 / 单题材 打分查询 CLI"
            "（不开 GUI 也能看，等同评估页接口）"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "--by",
        choices=[
            "template", "report", "theme_in_report", "stocks_in_theme",
        ],
        default="template",
        help=(
            "查询粒度：\n"
            "  template         模板汇总（默认）\n"
            "  report           报告级明细\n"
            "  theme_in_report  报告下题材级（需 --report-id）\n"
            "  stocks_in_theme  题材下标的级（需 --theme-id）"
        ),
    )
    p.add_argument(
        "--report-id", type=int, default=None,
        help="（仅 theme_in_report 模式）必填，指定 ai_reports.id",
    )
    p.add_argument(
        "--days", type=int, default=30,
        help="取最近 N 个自然日内的样本（默认 30）",
    )
    p.add_argument(
        "--time-dim", choices=["score_date", "report_date"],
        default="report_date",
        help=(
            "时间维度。默认 report_date（与 GUI 评估页一致，含未打分报告）；"
            "score_date 仅看最近 N 天发生过打分的"
        ),
    )
    p.add_argument(
        "--keep-version", action="store_true",
        help="（仅 template 模式）按 (prompt_id, version) 聚合",
    )
    p.add_argument(
        "--prompt-id", default=None,
        help="（仅 report 模式）只看某模板下的报告",
    )
    p.add_argument(
        "--prompt-version", default=None,
        help="（仅 report 模式）搭配 --prompt-id 进一步过滤版本",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument(
        "--backtest-only", action="store_true",
        help="只看回测样本（is_backtest=1）",
    )
    grp.add_argument(
        "--real-only", action="store_true",
        help="只看真实样本（is_backtest=0）",
    )
    p.add_argument(
        "--theme-id", type=int, default=None,
        help="若指定则忽略 --by，直接查该单题材的 D+N 明细",
    )
    p.add_argument(
        "--json", action="store_true",
        help="输出 JSON",
    )
    return p.parse_args(argv)


def _resolve_bt_filter(args: argparse.Namespace) -> Optional[int]:
    if args.backtest_only:
        return 1
    if args.real_only:
        return 0
    return None


def _filter_human(bt: Optional[int]) -> str:
    if bt is None:
        return "全部"
    return "仅回测" if bt == 1 else "仅真实"


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    bt_filter = _resolve_bt_filter(args)

    # 树第 3 层：报告下题材级明细
    if args.by == "theme_in_report":
        if args.report_id is None:
            print(
                "错误：--by theme_in_report 需要 --report-id ID",
                file=sys.stderr,
            )
            return 2
        rows = get_theme_eval_for_report(args.report_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        _print_themes_in_report(args.report_id, rows)
        print(f"\n[共 {len(rows)} 个题材]")
        return 0

    # 树第 4 层：题材下标的级明细
    if args.by == "stocks_in_theme":
        if args.theme_id is None:
            print(
                "错误：--by stocks_in_theme 需要 --theme-id ID",
                file=sys.stderr,
            )
            return 2
        rows = get_stock_scores_for_theme(args.theme_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        _print_stocks_in_theme(args.theme_id, rows)
        print(f"\n[共 {len(rows)} 只标的]")
        return 0

    # 单题材模式（--theme-id 不带 --by 时，等价 score_detail）
    if args.theme_id is not None:
        rows = get_theme_score_detail(args.theme_id)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            _print_theme_detail(args.theme_id, rows)
        return 0

    # 报告级明细
    if args.by == "report":
        rows = get_report_eval(
            days=args.days,
            prompt_id=args.prompt_id,
            prompt_version=args.prompt_version,
            is_backtest_filter=bt_filter,
            time_dim=args.time_dim,
        )
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        extra = ""
        if args.prompt_id:
            extra = f" / prompt_id={args.prompt_id}"
            if args.prompt_version:
                extra += f" v{args.prompt_version}"
        print(
            f"\n=== 报告级评估（最近 {args.days} 天[{args.time_dim}] / "
            f"{_filter_human(bt_filter)}{extra}）==="
        )
        _print_report_table(rows)
        print(f"\n[共 {len(rows)} 份报告]")
        return 0

    # 模板汇总（默认）
    rows = get_template_eval(
        days=args.days,
        ignore_version=(not args.keep_version),
        is_backtest_filter=bt_filter,
        time_dim=args.time_dim,
    )

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    print(
        f"\n=== 模板评估（最近 {args.days} 天[{args.time_dim}] / "
        f"{_filter_human(bt_filter)} / "
        f"keep_version={args.keep_version}）==="
    )
    _print_template_table(rows)
    print(f"\n[共 {len(rows)} 个模板]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
