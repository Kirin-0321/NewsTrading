"""一键打分未完成报告 CLI（评估页同款逻辑的命令行入口）。

业务定位
--------
扫当前筛选范围内 ``score_status ∈ {none, partial}`` 的报告，逐个调
``rescore_one_report`` 增量补齐；已 ``full`` 的报告不动，节省市场 API
配额。这是评估页「⚡ 一键打分未完成」按钮的 CLI 同款，方便定时任务在
凌晨补齐当天没打完的 D+N。

用法
----
::

    # 默认：最近 30 天 / time_dim=report_date / 真+回测都打
    python tools/rescore_unfinished.py

    # 仅真实日常报告（is_backtest=0）/ 最近 7 天
    python tools/rescore_unfinished.py --days 7 --is-backtest 0

    # 按 score_date 维度（最近 N 天发生过打分的范围）
    python tools/rescore_unfinished.py --time-dim score_date

    # 机器可读 JSON 输出
    python tools/rescore_unfinished.py --json

参数详解
--------
* ``--days N``：回看 N 个自然日，默认 30
* ``--time-dim {report_date|score_date}``：筛选维度，默认 report_date
* ``--is-backtest {0|1}``：回测/真实过滤，默认两个都包
* ``--days-back N``：每个报告回算 D+1~D+N，默认 5
* ``--hit-threshold-pct F``：命中阈值（默认沿用 service 默认值）
* ``--benchmark TS_CODE``：benchmark 指数（默认沿用 service 默认值）
* ``--score-date-override YYYYMMDD``：覆盖 cutoff（防穿越测试用）
* ``--json``：JSON 输出，与 ``BatchUnfinishedResult`` 字段一一对应

退出码
------
* 0 全部成功
* 1 至少一个报告 / pair 失败（errors 非空）
* 2 参数错误
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.scoring.script_scorer import (  # noqa: E402
    DEFAULT_BENCHMARK_TS_CODE,
    DEFAULT_HIT_THRESHOLD_PCT,
)
from services.scoring.scoring_service import (  # noqa: E402
    rescore_unfinished_reports,
)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rescore_unfinished",
        description="批量给 score_status ∈ {none, partial} 的报告补齐打分",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--days", type=int, default=30,
                   help="回看 N 个自然日，默认 30")
    p.add_argument("--time-dim",
                   choices=("report_date", "score_date"),
                   default="report_date",
                   help="筛选维度，默认 report_date")
    p.add_argument("--is-backtest", type=int, choices=(0, 1),
                   default=None,
                   help="回测/真实过滤：0 仅真实 / 1 仅回测 / 不传两个都包")
    p.add_argument("--days-back", type=int, default=5,
                   help="每个报告回算 D+1~D+N，默认 5")
    p.add_argument("--hit-threshold-pct", type=float,
                   default=DEFAULT_HIT_THRESHOLD_PCT,
                   help=f"命中阈值，默认 {DEFAULT_HIT_THRESHOLD_PCT}")
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK_TS_CODE,
                   help=f"benchmark 指数，默认 {DEFAULT_BENCHMARK_TS_CODE}")
    p.add_argument("--score-date-override",
                   help="覆盖 cutoff（YYYYMMDD），防穿越测试用")
    p.add_argument("--json", action="store_true",
                   help="JSON 输出（机器可读）")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    res = rescore_unfinished_reports(
        days=args.days,
        time_dim=args.time_dim,
        is_backtest_filter=args.is_backtest,
        days_back=args.days_back,
        hit_threshold_pct=args.hit_threshold_pct,
        benchmark_ts_code=args.benchmark,
        score_date_override=args.score_date_override,
    )

    if args.json:
        print(json.dumps(asdict(res), ensure_ascii=False, indent=2))
    else:
        print(
            f"\n[rescore_unfinished] ok={res.ok} "
            f"reports {res.reports_done}/{res.reports_targeted} "
            f"(skip={res.reports_skipped}) "
            f"themes_scored={res.themes_scored} "
            f"pairs={res.pairs_succeeded}/{res.pairs_attempted} "
            f"days_covered={res.days_covered} "
            f"elapsed={res.elapsed_ms}ms"
        )
        if res.errors:
            print(f"[rescore_unfinished] 失败 {len(res.errors)} 项（前 5 条）:")
            for err in res.errors[:5]:
                print(f"  - {err}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
