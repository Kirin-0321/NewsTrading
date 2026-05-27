"""单报告打分 CLI（评估页「打分」按钮的 CLI 对位实现）。

业务定位
--------
按 ``ai_reports.id`` 精准打分该报告下属所有 ``theme_predictions``，
**不波及其他报告**——与 :func:`scoring_service.rescore_range` 区别：

* ``rescore_range``：按 ``report_date`` 区间扫所有模板的题材
* ``rescore_one_report``：按 ``ai_reports.id`` 反查 ``report_path`` 锁定题材集

幂等性：写入 ``theme_prediction_scores`` 有 ``UNIQUE(theme_id, score_date)``
约束 + INSERT OR REPLACE，重复跑安全。

用法
----
::

    # 给 ai_reports.id=96 那份报告下所有题材打 D+1~D+5
    python tools/score_one_report.py --report-id 96

    # 全量列出当前数据库的所有报告（找 ID 用）
    python tools/score_one_report.py --list

    # 调试：今天 cutoff 之外的 D+N 不打
    python tools/score_one_report.py --report-id 96 --cutoff 20260527

退出码
------
* 0 全部成功
* 1 至少一项 (theme, score_date) 失败
* 2 参数错误 / report_id 不存在
"""

from __future__ import annotations

import argparse
import json
import logging
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
    BatchScoringResult,
    rescore_one_report,
)
from services.storage.ai_inference_db import (  # noqa: E402
    get_ai_inference_db,
)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="score_one_report",
        description=(
            "按 ai_reports.id 给单份报告下所有题材打 D+1~D+days_back"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--report-id", type=int,
                     help="ai_reports.id（必填，二选一）")
    grp.add_argument("--list", action="store_true",
                     help="列出所有 ai_reports 找 ID（二选一）")

    p.add_argument("--days-back", type=int, default=5,
                   help="打几天，默认 5 (D+1~D+5)")
    p.add_argument("--cutoff", default=None,
                   help="score_date 上限 YYYYMMDD，默认 = 今天")
    p.add_argument("--hit-threshold-pct", type=float, default=None,
                   help="命中阈值，默认沿用 script_scorer 默认值")
    p.add_argument("--benchmark", default=None,
                   help="benchmark 指数 ts_code，默认 000001.SH")
    p.add_argument("--json", action="store_true",
                   help="JSON 输出（机器可读）")
    return p.parse_args(argv)


def _list_reports() -> int:
    """列出所有 ai_reports（按 report_date DESC, id DESC）。"""
    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT ar.id, ar.report_date, ar.is_backtest, "
            "       ar.prompt_id, ar.theme_extracted, ar.file_path, "
            "       (SELECT COUNT(*) FROM theme_predictions tp "
            "        WHERE tp.report_path = ar.file_path) AS tp_n, "
            "       (SELECT COUNT(*) FROM theme_prediction_scores tps "
            "        JOIN theme_predictions tp ON tp.id = tps.theme_id "
            "        WHERE tp.report_path = ar.file_path) "
            "        AS scored_n "
            "FROM ai_reports ar "
            "ORDER BY ar.report_date DESC, ar.id DESC"
        ).fetchall()

    print(f"{'id':>4}  {'date':<11} {'BT':<3} {'themes':<6} "
          f"{'scored':<6} {'status':<8} prompt")
    print("-" * 80)
    for r in rows:
        d = dict(r)
        tp_n = int(d["tp_n"])
        scored_n = int(d["scored_n"])
        expected = tp_n * 5
        if expected == 0 or scored_n == 0:
            status = "none"
        elif scored_n >= expected:
            status = "full"
        else:
            status = "partial"
        bt = "yes" if d["is_backtest"] else "no"
        print(
            f"{d['id']:>4}  {d['report_date']:<11} {bt:<3} "
            f"{tp_n:<6} {scored_n}/{expected:<4} {status:<8} "
            f"{d['prompt_id']}"
        )
    return 0


def _print_result(report_id: int, res: BatchScoringResult) -> None:
    flag = "OK" if res.ok else "PARTIAL FAIL"
    print(
        f"\n[{flag}] report_id={report_id} "
        f"themes_total={res.themes_total} "
        f"themes_scored={res.themes_scored} "
        f"pairs={res.pairs_succeeded}/{res.pairs_attempted} "
        f"days_covered={res.days_covered} "
        f"elapsed={res.elapsed_ms}ms"
    )
    if res.errors:
        print(f"\n[errors] {len(res.errors)} 项（前 10 条）:")
        for err in res.errors[:10]:
            print(f"  - {err}")


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    if args.list:
        return _list_reports()

    kwargs = {"days_back": args.days_back}
    if args.cutoff:
        if not (len(args.cutoff) == 8 and args.cutoff.isdigit()):
            print(
                f"[FAIL] --cutoff 必须是 YYYYMMDD: {args.cutoff}",
                file=sys.stderr,
            )
            return 2
        kwargs["score_date_override"] = args.cutoff
    if args.hit_threshold_pct is not None:
        kwargs["hit_threshold_pct"] = args.hit_threshold_pct
    if args.benchmark:
        kwargs["benchmark_ts_code"] = args.benchmark

    try:
        res = rescore_one_report(args.report_id, **kwargs)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "report_id": args.report_id,
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
        _print_result(args.report_id, res)

    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
