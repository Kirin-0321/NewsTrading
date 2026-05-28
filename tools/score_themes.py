"""题材打分 CLI（Phase 2 Step 2.3）。

用法
----
::

    # 给今天对应的 D+N 题材打分（调度器同款入口）
    python tools/score_themes.py --today

    # 指定打分日（解析当日对应的 D+N 题材）
    python tools/score_themes.py --date 20260527

    # 重打 report_date 区间内全部 D+1~D+5
    python tools/score_themes.py --range 20260520 20260526

    # 单题材调试（不写库）
    python tools/score_themes.py --theme-id 123 --dry-run

参数详解
--------
* ``--today`` ：等价于 ``--date <今天>``，用 ``run_daily_scoring`` 入口
* ``--date YYYYMMDD`` ：指定打分日
* ``--range S E`` ：``report_date`` 区间重打分（用 ``rescore_range``）
* ``--theme-id N`` ：单题材调试（绕过区间扫描）
* ``--dry-run`` ：仅计算，不入库（调试用）
* ``--days-back`` ：追踪期长度，默认 5

退出码
------
* 0 全部成功
* 1 至少一项失败（errors 列表非空）
* 2 参数错误
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.market.trade_date import next_trade_date  # noqa: E402
from services.scoring.script_scorer import (  # noqa: E402
    DEFAULT_BENCHMARK_TS_CODE,
    DEFAULT_HIT_THRESHOLD_PCT,
    ScoringError,
    score_theme_on_date,
)
from services.scoring.scoring_service import (  # noqa: E402
    BatchScoringResult,
    rescore_range,
    run_daily_scoring,
)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="score_themes",
        description="批量给题材跑脚本打分（写 theme_prediction_scores）",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--today", action="store_true",
                     help="给今天对应的 D+N 题材打分")
    grp.add_argument("--date", help="指定打分日 YYYYMMDD")
    grp.add_argument("--range", nargs=2, metavar=("START", "END"),
                     help="report_date 区间重算（YYYYMMDD YYYYMMDD）")
    grp.add_argument("--theme-id", type=int,
                     help="单题材调试 ID（跑 D+1~D+days_back 全部）")

    p.add_argument("--days-back", type=int, default=5,
                   help="追踪期长度，默认 5（D+1~D+5）")
    p.add_argument("--hit-threshold-pct", type=float,
                   default=DEFAULT_HIT_THRESHOLD_PCT,
                   help=f"命中阈值，默认 {DEFAULT_HIT_THRESHOLD_PCT}")
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK_TS_CODE,
                   help=f"benchmark 指数，默认 {DEFAULT_BENCHMARK_TS_CODE}")
    p.add_argument("--dry-run", action="store_true",
                   help="仅计算，不入库")
    p.add_argument("--json", action="store_true",
                   help="JSON 输出（机器可读）")
    return p.parse_args(argv)


def _print_summary(label: str, res: BatchScoringResult) -> None:
    print(
        f"\n[{label}] ok={res.ok} "
        f"themes_total={res.themes_total} "
        f"themes_scored={res.themes_scored} "
        f"pairs={res.pairs_succeeded}/{res.pairs_attempted} "
        f"days_covered={res.days_covered} "
        f"elapsed={res.elapsed_ms}ms"
    )
    if res.errors:
        print(f"[{label}] 失败 {len(res.errors)} 项（前 5 条）:")
        for err in res.errors[:5]:
            print(f"  - {err}")


def _print_score(score) -> None:
    # 2026-05-28 22:30 α 体系金字塔重构：单日 alpha 字段已 DROP，
    # 此处现推 α+N = theme_pct - benchmark_zz1000_pct（与 GUI / 聚合层一致）
    if score.theme_pct is not None and score.benchmark_zz1000_pct is not None:
        a_n = score.theme_pct - score.benchmark_zz1000_pct
    else:
        a_n = None
    print(
        f"  theme_id={score.theme_id} score_date={score.score_date} "
        f"D+{score.days_offset}: "
        f"sector={score.sector_pct} "
        f"stock_avg={score.stock_avg_pct} "
        f"hit={score.hit_count}/{score.total_count} "
        f"α+{score.days_offset}={a_n} "
        f"dir={score.direction_correct}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.dry_run:
        print("[dry-run] 不会写入打分表")

    # 1. --today / --date：每日入口
    if args.today or args.date:
        sd = args.date or datetime.now().strftime("%Y%m%d")
        if not _is_yyyymmdd(sd):
            print(f"[FAIL] --date 必须是 YYYYMMDD: {sd}", file=sys.stderr)
            return 2
        if args.dry_run:
            print(
                "[WARN] run_daily_scoring 暂不支持 dry-run；"
                "如需调试请用 --theme-id"
            )
        res = run_daily_scoring(
            sd,
            days_back=args.days_back,
            hit_threshold_pct=args.hit_threshold_pct,
            benchmark_ts_code=args.benchmark,
        )
        if args.json:
            print(json.dumps({
                "ok": res.ok,
                "themes_total": res.themes_total,
                "themes_scored": res.themes_scored,
                "pairs_attempted": res.pairs_attempted,
                "pairs_succeeded": res.pairs_succeeded,
                "days_covered": res.days_covered,
                "elapsed_ms": res.elapsed_ms,
                "errors": res.errors[:20],
            }, ensure_ascii=False, indent=2))
        else:
            _print_summary(f"daily {sd}", res)
        return 0 if res.ok else 1

    # 2. --range：区间重算
    if args.range:
        start, end = args.range
        if not (_is_yyyymmdd(start) and _is_yyyymmdd(end)):
            print(
                f"[FAIL] --range 两个参数必须是 YYYYMMDD: {start}, {end}",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            print(
                "[WARN] rescore_range 暂不支持 dry-run；"
                "如需调试请用 --theme-id"
            )
        res = rescore_range(
            start, end,
            days_back=args.days_back,
            hit_threshold_pct=args.hit_threshold_pct,
            benchmark_ts_code=args.benchmark,
        )
        _print_summary(f"range {start}~{end}", res)
        return 0 if res.ok else 1

    # 3. --theme-id：单题材调试
    if args.theme_id is not None:
        # 读题材 report_date，然后跑 D+1 ~ D+N
        from services.storage.ai_inference_db import get_ai_inference_db
        with get_ai_inference_db().connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT report_date FROM theme_predictions WHERE id = ?",
                (args.theme_id,),
            ).fetchone()
        if not row:
            print(
                f"[FAIL] theme_id={args.theme_id} 不存在",
                file=sys.stderr,
            )
            return 2
        report_date = str(row["report_date"])
        print(f"[debug] theme_id={args.theme_id} report_date={report_date}")
        failures = 0
        for n in range(1, args.days_back + 1):
            try:
                sd = next_trade_date(report_date, n)
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] 解算 D+{n} 失败: {exc}")
                failures += 1
                continue
            try:
                score = score_theme_on_date(
                    args.theme_id, sd,
                    hit_threshold_pct=args.hit_threshold_pct,
                    benchmark_ts_code=args.benchmark,
                    write=not args.dry_run,
                )
                _print_score(score)
            except (ScoringError, ValueError) as exc:
                print(f"  [FAIL] D+{n} ({sd}): {exc}")
                failures += 1
        return 0 if failures == 0 else 1

    return 0


def _is_yyyymmdd(s: str) -> bool:
    return isinstance(s, str) and len(s) == 8 and s.isdigit()


if __name__ == "__main__":
    sys.exit(main())
