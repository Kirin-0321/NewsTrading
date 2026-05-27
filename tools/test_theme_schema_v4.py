"""验证 theme_predictions / theme_stocks / theme_news v5 schema CLI。

v5 (Phase -1 三库重构, 2026-05-27)：
    - 题材三张表已从 news.db 迁到 ai_inference.db
    - theme_predictions 新增 is_backtest 字段

用法：
    python tools/test_theme_schema_v4.py             # 自动 ensure_schema + 检查
    python tools/test_theme_schema_v4.py --check     # 只读检查（不触发 ensure）

退出码：
    0  schema 与期望完全一致
    1  schema 不一致

何时重跑：
    - 升级 services/storage/ai_inference_db.py 或 ai_migrations/*.sql 后
    - 怀疑 ai_inference.db schema 漂移
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from services.storage.ai_inference_db import (  # noqa: E402
    get_ai_inference_db,
)

# v5 期望字段（脱钩 database.py，本测试自包含）
_THEME_V5_COLUMNS = {
    "theme_predictions": {
        "id", "report_id", "report_date", "report_time", "report_path",
        "theme_name", "theme_category", "strength_score", "strength_level",
        "priority_rank", "duration", "expectation_gap",
        "is_cold", "reason", "risk_note",
        "prompt_id", "prompt_version",
        "sector_ts_code", "sector_match_conf",
        "is_backtest",  # v5 新增
        "created_at",
    },
    "theme_stocks": {
        "id", "theme_id", "stock_name", "stock_code", "normalized_code",
        "role", "reason",
    },
    "theme_news": {
        "id", "theme_id", "news_ref", "news_id", "relation_type",
    },
}


def _dump_cols(conn: sqlite3.Connection, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="只读检查（跳过 ensure_schema）")
    args = parser.parse_args()

    db = get_ai_inference_db()
    path = str(db.path)
    print(f"ai_inference.db 路径：{path}")

    if not args.check:
        print("→ 调用 ensure_schema()（运行所有 ai_migrations/*.sql）")
        db.ensure_schema()
    else:
        print("→ 跳过 ensure_schema（--check 模式）")

    fails = 0
    with sqlite3.connect(path) as conn:
        for table, want_cols in _THEME_V5_COLUMNS.items():
            actual = _dump_cols(conn, table)
            extra = actual - want_cols
            missing = want_cols - actual

            print(f"\n=== {table} ===")
            print(f"  字段数  actual={len(actual)} expect={len(want_cols)}")
            if actual == want_cols:
                print("  [OK] 完全一致")
                continue

            print("  [FAIL] schema 不一致")
            if missing:
                print(f"    missing 字段（应有但没有）: {sorted(missing)}")
            if extra:
                print(f"    extra   字段（多余的旧字段）: {sorted(extra)}")
            fails += 1

        # 顺手验证一下 created_at 在 theme_predictions
        print("\n=== 索引检查 ===")
        for table in ("theme_predictions", "theme_stocks", "theme_news"):
            rows = conn.execute(
                f"SELECT name FROM sqlite_master "
                f"WHERE type='index' AND tbl_name='{table}'"
            ).fetchall()
            names = [r[0] for r in rows if not r[0].startswith("sqlite_")]
            print(f"  {table}: {names}")

    print(f"\n========== 总结：{fails} 张表失败 ==========")
    return 1 if fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[用户中断]")
        sys.exit(2)
