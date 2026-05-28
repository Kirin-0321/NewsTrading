"""备份 → 清空 → 全量重打分 CLI。

业务定位
--------
2026-05-28 题材打分逻辑改造决策点 4·B：全量重打分。
1. 把现有 theme_prediction_scores / theme_stock_scores 备份为 CSV
2. 清空两张表
3. 调 rescore_range 对 [start, end] 区间所有题材重新打分

用法
----
::

    # 备份 + 清空 + 重打过去 30 天（默认）
    python tools/rescore_full_window.py --apply

    # 自定义区间
    python tools/rescore_full_window.py --start 20260427 --end 20260527 --apply

    # 仅备份不动数据（--apply 不传则只跑备份）
    python tools/rescore_full_window.py

退出码
------
* 0  备份 + 重打都成功
* 1  打分失败 / 备份失败
* 2  参数错
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_BACKUP_DIR = _ROOT / ".huiye"


def _backup_table(table: str, ts: str) -> Path:
    """把整张表导出 CSV 到 .huiye/_backup_{table}_{ts}.csv"""
    from services.storage.ai_inference_db import get_ai_inference_db
    out = _BACKUP_DIR / f"_backup_{table}_{ts}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    ai = get_ai_inference_db()
    with ai.connect(readonly=True) as conn:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        # 创建一个空文件标记
        out.write_text("", encoding="utf-8")
        return out

    cols = list(rows[0].keys())
    with out.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r[c] for c in cols])
    return out


def _purge_tables() -> None:
    """清空 theme_prediction_scores + theme_stock_scores。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    ai = get_ai_inference_db()
    with ai.connect() as conn:
        conn.execute("DELETE FROM theme_stock_scores")
        conn.execute("DELETE FROM theme_prediction_scores")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="备份 → 清空 → 全量重打分",
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="report_date 起始 YYYYMMDD，默认 30 天前",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="report_date 结束 YYYYMMDD，默认今天",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际清空 + 重打分（缺省仅备份不动数据）",
    )
    parser.add_argument(
        "--days-back", type=int, default=5,
        help="每个题材回算几天（默认 5 = D+1~D+5）",
    )
    args = parser.parse_args()

    today = datetime.now()
    end = args.end or today.strftime("%Y%m%d")
    start = args.start or (today - timedelta(days=30)).strftime("%Y%m%d")

    if not (start.isdigit() and len(start) == 8 and
            end.isdigit() and len(end) == 8):
        print(f"ERROR: --start/--end 必须 YYYYMMDD: {start}/{end}",
              file=sys.stderr)
        return 2

    print(f"=== 全量重打分 [{start} ~ {end}] {'APPLY' if args.apply else 'BACKUP-ONLY'} ===")

    # 1. 备份
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"\n[1/3] 备份 scores 表 → .huiye/_backup_*_{ts}.csv")
    try:
        bak1 = _backup_table("theme_prediction_scores", ts)
        bak2 = _backup_table("theme_stock_scores", ts)
        print(f"  - {bak1.name} ({bak1.stat().st_size} bytes)")
        print(f"  - {bak2.name} ({bak2.stat().st_size} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"  备份失败: {exc}", file=sys.stderr)
        return 1

    if not args.apply:
        print("\n仅备份模式（缺 --apply）。要继续清空+重打分请加 --apply")
        return 0

    # 2. 清空
    print("\n[2/3] 清空 theme_prediction_scores / theme_stock_scores")
    _purge_tables()

    # 3. 重打分
    print(f"\n[3/3] 重打分 rescore_range({start}, {end}, days_back={args.days_back})")
    from services.scoring.scoring_service import rescore_range
    res = rescore_range(
        start, end, days_back=args.days_back,
    )
    print(f"\nresult.ok={res.ok}")
    print(f"  themes_total      = {res.themes_total}")
    print(f"  themes_scored     = {res.themes_scored}")
    print(f"  pairs_attempted   = {res.pairs_attempted}")
    print(f"  pairs_succeeded   = {res.pairs_succeeded}")
    print(f"  days_covered      = {res.days_covered}")
    print(f"  elapsed_ms        = {res.elapsed_ms}")
    if res.errors:
        print(f"  errors ({len(res.errors)} 条，前 5):")
        for e in res.errors[:5]:
            print(f"    - {e}")

    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
