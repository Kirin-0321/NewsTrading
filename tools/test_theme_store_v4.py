"""theme_store v4 端到端 CLI 测试。

覆盖:
    用例 1  normalize     - score 校正 / level 校正 / sentiment 残留剔除
    用例 2  save_basic    - 基础入库 + 字段验证
    用例 3  p1_path       - report_path 自动规范化为相对 posix
    用例 4  p2_idempotent - 同 report_id 重复 save，旧数据被清掉（幂等）
    用例 5  p6_filter     - get_by_date 按 prompt_id 过滤
    用例 6  distinct      - list_distinct_prompts
    用例 7  cascade       - delete_by_report 级联清子表

用法:
    python tools/test_theme_store_v4.py

退出码:
    0  全部用例通过
    1  至少一个失败

⚠️ 警告:
    本脚本会向 news.db 写入测试数据（report_id 前缀 '__test_'），
    跑完会自动 delete_by_report 清掉。如果中途 KeyboardInterrupt 退出，
    可能残留几条 __test_xxx 的测试数据，手动清理:
        DELETE FROM theme_predictions WHERE report_id LIKE '__test_%';
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from services.storage.theme_store import (  # noqa: E402
    ThemeStore,
    _normalize_theme,
)


_TEST_PREFIX = "__test_"


def _cleanup(store: ThemeStore) -> None:
    """删所有 __test_ 前缀的测试报告（v5: 走 ai_inference.db）。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    with get_ai_inference_db().connect() as conn:
        conn.execute(
            "DELETE FROM theme_predictions WHERE report_id LIKE ?",
            (f"{_TEST_PREFIX}%",),
        )


def _make_meta(report_id_suffix: str, raw_path: str) -> dict:
    return {
        "report_id": f"{_TEST_PREFIX}{report_id_suffix}",
        "report_date": "2026-05-27",
        "report_time": "10:30",
        "report_path": raw_path,
    }


def _make_themes(score: int = 75, with_sector: bool = True) -> list:
    t = {
        "theme_name": "测试题材_先进封装",
        "theme_category": "科技AI",
        "strength_score": score,
        "reason": "测试 reason",
        "stocks": [
            {"name": "晶方科技", "code": "603005", "normalized_code": "603005.SH"},
        ],
        "news": ["新闻101"],
    }
    if with_sector:
        t["sector_ts_code"] = "BK1101.DC"
        t["sector_match_conf"] = 1.0
    return [t]


def case1_normalize() -> int:
    print("\n=== 用例 1：_normalize_theme ===")
    fails = 0

    cases = [
        # (输入 score, 期望 score, 期望 level)
        ({"strength_score": 75}, 75, "较强利多"),
        ({"strength_score": -50}, -50, "弱利空"),
        ({"strength_score": 999}, 100, "重大利多"),
        ({"strength_score": -999}, -100, "重大利空"),
        ({"strength_score": None}, 0, "中性"),
        ({"strength_score": "55"}, 55, "弱利多"),
        # v1 残留 sentiment 应被剔除
        ({"strength_score": 30, "sentiment": "利好"}, 30, "中性偏多"),
    ]
    for raw, want_score, want_level in cases:
        raw_full = dict(raw)
        raw_full["theme_name"] = "x"
        raw_full["reason"] = "x"
        out = _normalize_theme(raw_full)
        ok = (
            out["strength_score"] == want_score
            and out["strength_level"] == want_level
            and "sentiment" not in out
        )
        marker = "[OK]" if ok else "[FAIL]"
        print(f"  {marker} in={raw} -> score={out['strength_score']} "
              f"level={out['strength_level']} sentiment_dropped="
              f"{'sentiment' not in out}")
        if not ok:
            fails += 1
    return fails


def case2_save_basic(store: ThemeStore) -> int:
    print("\n=== 用例 2：save_themes 基础入库 ===")
    fails = 0
    meta = _make_meta("basic_001", "data/AI_analysis/5月27日/test.md")
    themes = _make_themes(score=80)
    result = store.save_themes(meta, themes)
    print(f"  入库结果: {result}")

    rows = store.get_by_report(meta["report_id"])
    if len(rows) != 1:
        print(f"  [FAIL] 应有 1 条题材，实际 {len(rows)}")
        return 1
    theme = rows[0]
    checks = [
        ("strength_score", 80),
        ("strength_level", "重大利多"),
        ("sector_ts_code", "BK1101.DC"),
        ("sector_match_conf", 1.0),
    ]
    for key, want in checks:
        actual = theme.get(key)
        ok = actual == want
        marker = "[OK]" if ok else "[FAIL]"
        print(f"  {marker} {key:20s} expected={want}  actual={actual}")
        if not ok:
            fails += 1
    # 标的的 normalized_code
    stocks = theme.get("stocks") or []
    if stocks and stocks[0].get("normalized_code") == "603005.SH":
        print(f"  [OK] stocks[0].normalized_code = 603005.SH")
    else:
        print(f"  [FAIL] stocks[0].normalized_code 异常: {stocks}")
        fails += 1
    return fails


def case3_p1_path(store: ThemeStore) -> int:
    print("\n=== 用例 3：P1 路径自动规范化 ===")
    fails = 0
    weird_path = "F:\\NewsTrading\\data\\AI_analysis\\5月27日\\test_p1.md"
    meta = _make_meta("p1_001", weird_path)
    store.save_themes(meta, _make_themes())

    rows = store.get_by_report(meta["report_id"])
    if not rows:
        print("  [FAIL] 没查到数据")
        return 1
    rel = rows[0].get("report_path")
    print(f"  入库 report_path = {rel}")
    if "\\" in rel:
        print("  [FAIL] 仍含反斜杠，未规范化")
        fails += 1
    elif rel.startswith("F:") or rel.startswith("/"):
        print("  [FAIL] 仍是绝对路径")
        fails += 1
    elif rel.startswith("data/AI_analysis"):
        print("  [OK] 规范化为相对 posix")
    else:
        print(f"  [WARN] 未识别格式: {rel}")
    return fails


def case4_p2_idempotent(store: ThemeStore) -> int:
    print("\n=== 用例 4：P2 重复 save_themes 幂等性 ===")
    fails = 0
    meta = _make_meta("p2_001", "data/AI_analysis/test.md")

    # 第一次入库
    store.save_themes(meta, _make_themes(score=70))
    first = store.get_by_report(meta["report_id"])
    print(f"  第一次入库后: {len(first)} 条")

    # 第二次入库（不同 score）
    store.save_themes(meta, _make_themes(score=85))
    second = store.get_by_report(meta["report_id"])
    print(f"  第二次入库后: {len(second)} 条")

    if len(second) == 1 and second[0]["strength_score"] == 85:
        print("  [OK] 旧数据被清掉，仅保留新版（score=85）")
    else:
        print(f"  [FAIL] 重复入库未幂等，second={second}")
        fails += 1
    return fails


def case5_p6_filter(store: ThemeStore) -> int:
    print("\n=== 用例 5：P6 get_by_date 按 prompt_id 过滤 ===")
    fails = 0
    # 注：本测试 prompt_id 反查必失败（无对应 ai_reports 记录），
    # 因此过滤行为只能验证：传 prompt_id 参数不报错，传 None 时取全部。
    meta = _make_meta("p6_001", "data/AI_analysis/test.md")
    store.save_themes(meta, _make_themes())

    all_today = store.get_by_date("2026-05-27")
    test_rows = [r for r in all_today
                 if r["report_id"].startswith(_TEST_PREFIX)]
    print(f"  get_by_date(today) -> 含 {len(test_rows)} 条测试数据")

    filtered = store.get_by_date("2026-05-27", prompt_id="nonexistent_xxx")
    test_filtered = [r for r in filtered
                     if r["report_id"].startswith(_TEST_PREFIX)]
    if len(test_filtered) == 0:
        print("  [OK] 按不存在的 prompt_id 过滤 -> 0 条测试数据")
    else:
        print(f"  [FAIL] 应 0 条，实际 {len(test_filtered)}")
        fails += 1
    return fails


def case6_distinct(store: ThemeStore) -> int:
    print("\n=== 用例 6：list_distinct_prompts ===")
    prompts = store.list_distinct_prompts()
    print(f"  当前去重 prompt_id 列表: {prompts}")
    print("  [OK] 接口可调用，不报错（具体值取决于 news.db 历史数据）")
    return 0


def case7_cascade(store: ThemeStore) -> int:
    print("\n=== 用例 7：delete_by_report 级联 ===")
    fails = 0
    meta = _make_meta("cascade_001", "data/AI_analysis/test.md")
    store.save_themes(meta, _make_themes())

    rows_before = store.get_by_report(meta["report_id"])
    if not rows_before:
        print("  [FAIL] 入库失败")
        return 1
    stock_count_before = len(rows_before[0].get("stocks") or [])
    news_count_before = len(rows_before[0].get("news") or [])
    print(f"  入库后: 1 题材 / {stock_count_before} 标的 / "
          f"{news_count_before} 新闻")

    n = store.delete_by_report(meta["report_id"])
    print(f"  delete_by_report -> 删除 {n} 行")

    rows_after = store.get_by_report(meta["report_id"])
    if rows_after:
        print(f"  [FAIL] 仍有 {len(rows_after)} 题材残留")
        fails += 1
    else:
        print("  [OK] 题材已清空")

    # CASCADE 检查（直接查子表，v5: ai_inference.db）
    from services.storage.ai_inference_db import get_ai_inference_db
    with get_ai_inference_db().connect() as conn:
        orphan_stocks = conn.execute(
            "SELECT COUNT(*) FROM theme_stocks "
            "WHERE theme_id NOT IN (SELECT id FROM theme_predictions)"
        ).fetchone()[0]
        orphan_news = conn.execute(
            "SELECT COUNT(*) FROM theme_news "
            "WHERE theme_id NOT IN (SELECT id FROM theme_predictions)"
        ).fetchone()[0]
    print(f"  孤儿 stocks: {orphan_stocks}, 孤儿 news: {orphan_news}")
    if orphan_stocks > 0 or orphan_news > 0:
        print("  [FAIL] CASCADE 未生效")
        fails += 1
    else:
        print("  [OK] CASCADE 生效，无孤儿子表数据")
    return fails


def main() -> int:
    print(">>> theme_store v4 端到端测试")
    store = ThemeStore()
    print(f"  当前 news.db 题材总数: {store.count()}")

    # 起点清理（防上次中断残留）
    _cleanup(store)

    total_fails = 0
    try:
        total_fails += case1_normalize()
        total_fails += case2_save_basic(store)
        total_fails += case3_p1_path(store)
        total_fails += case4_p2_idempotent(store)
        total_fails += case5_p6_filter(store)
        total_fails += case6_distinct(store)
        total_fails += case7_cascade(store)
    finally:
        # 终点清理
        _cleanup(store)
        print(f"\n[清理] __test_ 前缀的测试数据已清空")
        print(f"  最终 news.db 题材总数: {store.count()}")

    print(f"\n========== 总结：{total_fails} 个失败 ==========")
    return 1 if total_fails else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[用户中断]")
        sys.exit(2)
