"""回填 theme_prediction_scores.benchmark_zz1000_pct（一次性 / 可重跑）。

业务定位
--------
迁移 ``003_add_benchmark_zz1000.sql`` 给 ``theme_prediction_scores`` 加了
``benchmark_zz1000_pct REAL`` 列，老行该列为 NULL。本 CLI 扫所有 NULL 行的
``score_date``，去 ``market.fact_index_daily`` 查中证1000 (``000852.SH``)
当日 ``pct_chg`` 写回；幂等可重跑（已填充的行不会被覆盖）。

α-1 指标依赖该列：
    α-1 = AVG over D+2~D+5 of (theme_pct - benchmark_zz1000_pct)

用法
----
    # 干跑：只统计将影响的行数 / 缺数据的日期，不写库
    python tools/backfill_benchmark_zz1000.py --dry-run

    # 真跑：把所有 NULL 行回填
    python tools/backfill_benchmark_zz1000.py

    # 输出 JSON（程序化集成用）
    python tools/backfill_benchmark_zz1000.py --json

退出码
------
* 0：成功（含 0 行待回填的 no-op 情况）
* 1：业务错误（如 attached_dbs 跨库失败）
* 2：参数错误

注意
----
若 ``missing_dates`` 非空，说明 fact_index_daily 没同步对应日期的中证1000
行情，需要先跑 ``python tools/market_fetch.py <YYYYMMDD>`` 抓回来再重跑本脚本。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from services.scoring.script_scorer import backfill_benchmark_zz1000  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="回填 theme_prediction_scores.benchmark_zz1000_pct（中证1000 当日 pct_chg）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="干跑：只统计影响范围，不实际更新数据库",
    )
    parser.add_argument(
        "--json", dest="as_json", action="store_true",
        help="以 JSON 格式输出 BackfillResult（适合脚本集成）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    try:
        result = backfill_benchmark_zz1000(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"回填失败: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        mode = "干跑" if args.dry_run else "实际写入"
        print(f"=== 中证1000 基准列回填（{mode}） ===")
        print(f"theme_prediction_scores 总行数 : {result.total_rows}")
        print(f"待回填行数（NULL）           : {result.rows_to_fill}")
        print(f"涉及不同 score_date          : {result.distinct_dates}")
        print(f"实际更新行数                 : {result.rows_updated}")
        if result.missing_dates:
            print(
                f"⚠ 中证1000 行情缺失日期（{len(result.missing_dates)} 个）："
            )
            for d in result.missing_dates[:20]:
                print(f"  - {d}")
            if len(result.missing_dates) > 20:
                print(f"  ... 共 {len(result.missing_dates)} 个，已截断")
            print(
                "  → 跑 `python tools/market_fetch.py <YYYYMMDD>` 补行情后再重跑本脚本"
            )
        else:
            print("✓ 所有 score_date 在 fact_index_daily 中均有中证1000 行情")

    return 0


if __name__ == "__main__":
    sys.exit(main())
