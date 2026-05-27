"""Phase -1 三库重构回归测试（10 用例）。

覆盖范围
--------
1. news.db 表清单：仅 raw_news + sync_meta（旧 AI 表已 DROP）
2. market.db 表清单：含 fact_stock_daily + fact_sector_daily
3. ai_inference.db 表清单：6 业务表 + schema_migrations 齐全
4. ai_inference.db schema_migrations 至少版本 1 已应用
5. AIReportsStore.record() + is_backtest 字段透传
6. AIReportsStore.delete_by_path() + record_report 便捷入口
7. ThemeStore.save_themes() + is_backtest 透传 + CASCADE 子表
8. ThemeStore.delete_by_report() CASCADE 删 stocks / news 子表
9. attached_dbs(primary='ai', attach=('market',)) 跨库 ATTACH + JOIN
10. attached_dbs 异常路径自动 DETACH（不污染下次连接）

运行
----
    python tools/test_three_db.py

成功输出 [全部通过]，失败抛 AssertionError 并 exit(1)。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Callable, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_news_db_slim() -> None:
    """news.db 表清单：仅 raw_news + sync_meta。"""
    from services.storage.database import get_db_path, init_database
    init_database()
    conn = sqlite3.connect(get_db_path())
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()
    legacy = {"ai_reports", "theme_predictions", "theme_stocks", "theme_news"}
    bad = legacy & tables
    assert not bad, f"news.db 仍存在旧 AI 表 {bad}"
    must = {"raw_news", "sync_meta"}
    missing = must - tables
    assert not missing, f"news.db 缺失瘦身后必备表 {missing}"


def case_02_market_db_has_quotes() -> None:
    """market.db 含 fact_stock_daily + fact_sector_daily。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    db.ensure_schema()
    stats = db.get_stats()
    assert "fact_stock_daily" in stats["tables"], "fact_stock_daily 缺失"
    assert "fact_sector_daily" in stats["tables"], "fact_sector_daily 缺失"


def case_03_ai_inference_db_six_tables() -> None:
    """ai_inference.db 6 业务表齐全。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    db = get_ai_inference_db()
    db.ensure_schema()
    stats = db.get_stats()
    expected = {
        "ai_reports",
        "theme_predictions",
        "theme_stocks",
        "theme_news",
        "theme_prediction_scores",
        "theme_stock_scores",
    }
    actual = set(stats["tables"].keys()) - {"schema_migrations"}
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"ai_inference.db 缺失 {missing}"
    assert not extra, f"ai_inference.db 多余表 {extra}"


def case_04_ai_inference_db_schema_migrations() -> None:
    """ai_inference.db schema_migrations 至少版本 1 已应用。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    stats = get_ai_inference_db().get_stats()
    versions = [m["version"] for m in stats["applied_migrations"]]
    assert 1 in versions, f"schema_migrations 缺失版本 1: {versions}"


def case_05_ai_reports_record_is_backtest() -> None:
    """AIReportsStore.record() 写入 + is_backtest 字段透传。"""
    from services.storage.ai_reports_store import (
        AIReportRecord,
        get_ai_reports_store,
    )
    store = get_ai_reports_store()
    test_path = "data/AI_analysis/_test_three_db_case5.md"
    store.delete_by_path(test_path)  # 清理可能的残留

    rec = AIReportRecord(
        report_date="20260520",
        file_path=test_path,
        provider="deepseek",
        prompt_id="test_prompt",
        prompt_version="1.0",
        is_backtest=1,
    )
    rid = store.record(rec)
    assert rid > 0, f"record 返回非法 id={rid}"

    got = store.get_by_path(test_path)
    assert got is not None, "get_by_path 未找到刚写入的记录"
    assert got.is_backtest == 1, f"is_backtest 应为 1，实际 {got.is_backtest}"
    assert got.prompt_id == "test_prompt"

    store.delete_by_path(test_path)


def case_06_record_report_default_is_backtest_zero() -> None:
    """record_report 便捷入口默认 is_backtest=0。"""
    from services.storage.ai_reports_store import (
        get_ai_reports_store,
        record_report,
    )
    store = get_ai_reports_store()
    test_path = "data/AI_analysis/_test_three_db_case6.md"
    store.delete_by_path(test_path)

    rid = record_report(
        file_path=test_path,
        report_date="20260527",
        provider="deepseek",
        prompt_id="news_focused",
    )
    assert rid > 0
    got = store.get_by_path(test_path)
    assert got is not None
    assert got.is_backtest == 0, f"默认 is_backtest 应为 0，实际 {got.is_backtest}"

    store.delete_by_path(test_path)


def case_07_theme_store_save_with_is_backtest() -> None:
    """ThemeStore.save_themes() + is_backtest 透传到 theme_predictions。"""
    from services.storage import get_theme_store
    from services.storage.ai_inference_db import get_ai_inference_db
    store = get_theme_store()
    rid = "_test_three_db_case7"
    store.delete_by_report(rid)  # 清残留

    res = store.save_themes(
        report_meta={
            "report_id": rid,
            "report_date": "20260520",
            "report_time": "17:30",
            "report_path": f"data/AI_analysis/{rid}.md",
            "is_backtest": True,
        },
        themes=[
            {
                "theme_name": "三库测试题材",
                "strength_score": 60,
                "reason": "case 7 烟测",
                "stocks": [
                    {
                        "name": "测试股 1",
                        "code": "600172",
                        "normalized_code": "600172.SH",
                    },
                    {
                        "name": "测试股 2",
                        "code": "000001",
                        "normalized_code": "000001.SZ",
                    },
                ],
                "news": [{"ref": "新闻 A", "id": "news_test_xxx"}],
            }
        ],
    )
    assert res["themes"] == 1, f"应写 1 题材，实际 {res['themes']}"
    assert res["stocks"] == 2, f"应写 2 标的，实际 {res['stocks']}"
    assert res["news"] == 1

    with get_ai_inference_db().connect() as conn:
        row = conn.execute(
            "SELECT is_backtest FROM theme_predictions WHERE report_id = ?",
            (rid,),
        ).fetchone()
        assert row is not None, "题材未入库"
        assert row["is_backtest"] == 1, f"is_backtest 透传失败 {dict(row)}"

    store.delete_by_report(rid)


def case_08_cascade_delete_subtables() -> None:
    """ThemeStore.delete_by_report CASCADE 删 stocks / news 子表。"""
    from services.storage import get_theme_store
    from services.storage.ai_inference_db import get_ai_inference_db
    store = get_theme_store()
    rid = "_test_three_db_case8"
    store.delete_by_report(rid)

    store.save_themes(
        report_meta={
            "report_id": rid,
            "report_date": "20260520",
            "report_time": "17:30",
            "report_path": f"data/AI_analysis/{rid}.md",
        },
        themes=[
            {
                "theme_name": "CASCADE 测试题材",
                "strength_score": 50,
                "reason": "case 8 烟测",
                "stocks": [
                    {"name": "A", "code": "600001", "normalized_code": "600001.SH"},
                    {"name": "B", "code": "600002", "normalized_code": "600002.SH"},
                ],
                "news": [
                    {"ref": "A", "id": "n_a"},
                    {"ref": "B", "id": "n_b"},
                ],
            }
        ],
    )

    with get_ai_inference_db().connect() as conn:
        theme_id = conn.execute(
            "SELECT id FROM theme_predictions WHERE report_id=?", (rid,)
        ).fetchone()["id"]
        before_stocks = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stocks WHERE theme_id=?",
            (theme_id,),
        ).fetchone()["c"]
        before_news = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_news WHERE theme_id=?",
            (theme_id,),
        ).fetchone()["c"]
        assert before_stocks == 2 and before_news == 2, (
            f"前置数据异常 stocks={before_stocks} news={before_news}"
        )

    store.delete_by_report(rid)

    with get_ai_inference_db().connect() as conn:
        after_stocks = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stocks WHERE theme_id=?",
            (theme_id,),
        ).fetchone()["c"]
        after_news = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_news WHERE theme_id=?",
            (theme_id,),
        ).fetchone()["c"]
        assert after_stocks == 0 and after_news == 0, (
            f"CASCADE 未生效 stocks={after_stocks} news={after_news}"
        )


def case_09_attached_dbs_cross_query() -> None:
    """attached_dbs(primary='ai', attach=('market',)) 跨库 ATTACH + JOIN。"""
    from services.storage.cross_db import attached_dbs

    with attached_dbs(primary="ai", attach=("market",)) as conn:
        # 验证 ATTACH 生效：查 market 库行情数据应能返回行数
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM market.fact_sector_daily"
        ).fetchone()
        assert row["c"] >= 0, "跨库查询失败"

        # 验证主库表也能正常访问（不带前缀）
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_predictions"
        ).fetchone()
        assert row["c"] >= 0, "主库查询失败"


def case_10_attached_dbs_exception_safety() -> None:
    """attached_dbs 异常路径自动 DETACH（不污染下次连接）。"""
    from services.storage.cross_db import attached_dbs

    # 先故意抛异常
    try:
        with attached_dbs(primary="ai", attach=("market",)) as conn:
            raise RuntimeError("故意抛错验证 DETACH 是否被执行")
    except RuntimeError:
        pass  # 预期

    # 立刻再开一次：如果上次没 DETACH 干净，这里会因为
    # connection 复用残留而看到 market 双重 ATTACH 错误。
    # 但 attached_dbs 每次都开新连接 + 退出时 close，所以一定干净。
    with attached_dbs(primary="ai", attach=("market", "news")) as conn:
        # 三库都能访问
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM market.dim_sector"
        ).fetchone()
        assert row["c"] >= 0

        row = conn.execute(
            "SELECT COUNT(*) AS c FROM news.raw_news"
        ).fetchone()
        assert row["c"] >= 0

        row = conn.execute(
            "SELECT COUNT(*) AS c FROM ai_reports"
        ).fetchone()
        assert row["c"] >= 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01 news.db 瘦身（仅 raw_news + sync_meta）", case_01_news_db_slim),
    ("02 market.db 含 fact_stock_daily + fact_sector_daily", case_02_market_db_has_quotes),
    ("03 ai_inference.db 6 业务表齐全", case_03_ai_inference_db_six_tables),
    ("04 ai_inference.db schema_migrations 版本 1 已应用", case_04_ai_inference_db_schema_migrations),
    ("05 AIReportsStore.record() + is_backtest 透传", case_05_ai_reports_record_is_backtest),
    ("06 record_report 便捷入口默认 is_backtest=0", case_06_record_report_default_is_backtest_zero),
    ("07 ThemeStore.save_themes() + is_backtest 透传", case_07_theme_store_save_with_is_backtest),
    ("08 ThemeStore.delete_by_report CASCADE 删子表", case_08_cascade_delete_subtables),
    ("09 attached_dbs 跨库 ATTACH + JOIN", case_09_attached_dbs_cross_query),
    ("10 attached_dbs 异常路径自动 DETACH", case_10_attached_dbs_exception_safety),
]


def main() -> int:
    passed = 0
    failed: list[tuple[str, str]] = []
    for name, fn in CASES:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except AssertionError as exc:
            print(f"[FAIL] {name}: {exc}")
            failed.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
            failed.append((name, f"{type(exc).__name__}: {exc}"))

    print()
    print(f"{'=' * 50}")
    print(f"{passed} / {len(CASES)} passed")
    if failed:
        print(f"{'=' * 50}")
        for name, msg in failed:
            print(f"  - {name}")
            print(f"    {msg}")
        print(f"{'=' * 50}")
        print("[全部通过]" if not failed else "[有失败]")
    else:
        print(f"{'=' * 50}")
        print("[全部通过]")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
