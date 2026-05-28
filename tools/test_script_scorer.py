"""Phase 2 打分核心回归测试（16 用例，全部走伪数据，0 API 消耗）。

覆盖
----
1. 穿越保护：score_date <= report_date 必 raise ScoringError
2. 单题材 D+1 打分：标的涨幅平均值 / hit_rate / 写库行数全部精确匹配
3. direction_correct：strength_score=+80 板块跌 → 0；-50 板块跌 → 1
4. hit_rate 阈值：标的 [5, 1, -2] / 阈值 3 → hit_count=1 hit_rate=1/3
5. 跨节假日：report_date=99990430 score_date=99990506 → days_offset=1
   （99990501-05 全休市的伪日历）
6. theme_stocks 为空 → stock_avg_pct=None，不 crash
7. sector_ts_code=NULL → sector_pct=None
8. 幂等：同 (theme_id, score_date) 重跑 → 主表仍 1 行（UPSERT）
9. 跨库 ATTACH/DETACH 连续 50 次后再开常规连接不报错
10. CASCADE：DELETE theme_predictions 1 行 → theme_prediction_scores +
    theme_stock_scores 子表对应行自动清
11. ``_compute_theme_pct`` 三分支精确（2026-05-28 17:15 加权改造）
12. theme_pct 持久化与内存值一致（2026-05-28 17:15）
13. theme_pct 加权口径稳定（2026-05-28 22:30 α 体系金字塔重构后单日 alpha 已 DROP）
14. benchmark_zz1000_pct 写入正确（2026-05-28 18:40 α-1 指标）
15. 中证1000 当日缺数据时 benchmark_zz1000_pct=None 不 crash
16. backfill_benchmark_zz1000 干跑/真跑 行为正确

数据隔离
--------
* 所有 trade_date 用 99990501~99990511 伪日期；ts_code 用 TEST_*；
  theme_id 用 999800+ 大 ID。
* 用例结尾统一 cleanup，不污染真实数据。

运行
----
    python tools/test_script_scorer.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 测试数据：伪日历 + 伪行情 + 伪题材
# ---------------------------------------------------------------------------

_FAKE_TRADE_CALENDAR = [
    # (cal_date, is_open, pretrade_date)
    ("99990428", 1, "99990427"),
    ("99990429", 1, "99990428"),
    ("99990430", 1, "99990429"),
    ("99990501", 0, "99990430"),  # 劳动节
    ("99990502", 0, "99990430"),
    ("99990503", 0, "99990430"),
    ("99990504", 0, "99990430"),
    ("99990505", 0, "99990430"),
    ("99990506", 1, "99990430"),
    ("99990507", 1, "99990506"),
    ("99990508", 1, "99990507"),
    ("99990509", 0, "99990508"),
    ("99990510", 0, "99990508"),
    ("99990511", 1, "99990508"),
]

# 个股伪行情：3 只标的，99990506 当日涨幅 [5%, 1%, -2%]
_FAKE_STOCK_DAILY = [
    # (ts_code, trade_date, pct_chg, close, pre_close)
    ("TEST_A.SH", "99990506", 5.0, 10.5, 10.0),
    ("TEST_B.SH", "99990506", 1.0, 10.1, 10.0),
    ("TEST_C.SH", "99990506", -2.0, 9.8, 10.0),
    # D+2 数据
    ("TEST_A.SH", "99990507", 3.0, 10.815, 10.5),
    ("TEST_B.SH", "99990507", -0.5, 10.0495, 10.1),
    ("TEST_C.SH", "99990507", 2.0, 9.996, 9.8),
]

# 板块伪行情：99990506 涨 +1.85%
_FAKE_SECTOR_DAILY = [
    # (ts_code, trade_date, pct_chg)
    ("TEST_BK.DC", "99990506", 1.85),
    ("TEST_BK.DC", "99990507", -0.5),
]

# benchmark 指数伪行情
# 注：000852.SH 为中证1000 真实代码（α-1 指标基准），由 script_scorer
# 硬编码作为 zz1000 基准查询；本测试为它造伪行情。
# 99990506 故意造数据，99990507 故意不造 → 验证缺数据时 None 兜底
_FAKE_INDEX_DAILY = [
    # (ts_code, trade_date, pct_chg, close)
    ("TEST_BENCH.SH", "99990506", 0.5, 4145.0),
    ("TEST_BENCH.SH", "99990507", -0.3, 4132.6),
    ("000852.SH", "99990506", 0.8, 6500.0),
]


# ---------------------------------------------------------------------------
# Setup / Teardown
# ---------------------------------------------------------------------------


def _seed_all() -> None:
    """注入伪日历 + 伪行情。"""
    from services.market.market_db import get_market_db
    db = get_market_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO dim_trade_calendar "
            "(trade_date, is_open, pretrade_date, cal_date) "
            "VALUES (?, ?, ?, ?)",
            [(d, op, p, d) for d, op, p in _FAKE_TRADE_CALENDAR],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO fact_stock_daily "
            "(ts_code, trade_date, pct_chg, close, pre_close) "
            "VALUES (?, ?, ?, ?, ?)",
            _FAKE_STOCK_DAILY,
        )
        # fact_sector_daily 必须先有对应 dim_sector 行（FK）
        sector_codes = {r[0] for r in _FAKE_SECTOR_DAILY}
        conn.executemany(
            "INSERT OR IGNORE INTO dim_sector "
            "(ts_code, name, idx_type, src, last_seen_date) "
            "VALUES (?, ?, ?, ?, ?)",
            [(c, f"伪板块{c}", "概念板块", "test", "99990506")
             for c in sector_codes],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO fact_sector_daily "
            "(trade_date, ts_code, pct_chg) "
            "VALUES (?, ?, ?)",
            [(td, c, p) for c, td, p in _FAKE_SECTOR_DAILY],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO fact_index_daily "
            "(ts_code, trade_date, pct_chg, close) "
            "VALUES (?, ?, ?, ?)",
            _FAKE_INDEX_DAILY,
        )


def _cleanup_all() -> None:
    """清除所有 9999* / TEST_* 伪数据。"""
    from services.market.market_db import get_market_db
    from services.storage.ai_inference_db import get_ai_inference_db

    mdb = get_market_db()
    with mdb.connect() as conn:
        conn.execute(
            "DELETE FROM dim_trade_calendar WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_stock_daily WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_sector_daily WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM fact_index_daily WHERE trade_date LIKE '9999%'"
        )
        conn.execute(
            "DELETE FROM dim_sector WHERE ts_code LIKE 'TEST_%'"
        )
    adb = get_ai_inference_db()
    with adb.connect() as conn:
        conn.execute(
            "DELETE FROM theme_predictions WHERE id >= 999800"
        )
        # theme_stocks / theme_news / theme_*_scores 经 FK CASCADE 自动清


def _make_theme(
    theme_id: int,
    *,
    report_date: str,
    sector_ts_code: str = "TEST_BK.DC",
    strength_score: int = 80,
    stock_codes: List[str] = None,
    prompt_id: str = "TEST_PROMPT",
    prompt_version: str = "1.0",
) -> None:
    """插入伪题材 + 标的。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    adb = get_ai_inference_db()
    with adb.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO theme_predictions "
            "(id, report_id, report_date, report_path, theme_name, "
            " strength_score, strength_level, reason, "
            " prompt_id, prompt_version, sector_ts_code, "
            " is_backtest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                theme_id, f"test_{theme_id}.md", report_date,
                f"/test/{theme_id}.md", f"测试题材{theme_id}",
                strength_score, "5_中性偏多",
                "测试用 reason", prompt_id, prompt_version,
                sector_ts_code, 1,
            ),
        )
        # 先清空旧的标的（防止重复插）
        conn.execute(
            "DELETE FROM theme_stocks WHERE theme_id = ?", (theme_id,)
        )
        if stock_codes:
            conn.executemany(
                "INSERT INTO theme_stocks "
                "(theme_id, stock_name, stock_code, normalized_code) "
                "VALUES (?, ?, ?, ?)",
                [
                    (theme_id, f"个股{code}", code, code)
                    for code in stock_codes
                ],
            )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_anti_lookahead() -> None:
    """穿越保护：score_date 必须严格 > report_date。"""
    from services.scoring.script_scorer import (
        ScoringError, score_theme_on_date,
    )
    _make_theme(999801, report_date="99990506")
    try:
        score_theme_on_date(999801, "99990506", write=False)
    except ScoringError as exc:
        assert "穿越" in str(exc), f"应提示穿越: {exc}"
        return
    raise AssertionError("score_date == report_date 应 raise ScoringError")


def case_02_single_theme_d1() -> None:
    """单题材 D+1 打分：6 指标全部正确 + 两张表都写入。"""
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(
        999802, report_date="99990430",
        stock_codes=["TEST_A.SH", "TEST_B.SH", "TEST_C.SH"],
        strength_score=80,
    )
    res = score_theme_on_date(999802, "99990506",
                              hit_threshold_pct=3.0,
                              benchmark_ts_code="TEST_BENCH.SH")
    # D+1：99990430 → 99990506 跨小长假 = 第 1 个开市日
    assert res.days_offset == 1, (
        f"D+1 错: 期望 1，得到 {res.days_offset}"
    )
    # stock_avg_pct = (5 + 1 + (-2)) / 3 = 1.333...
    assert res.stock_avg_pct is not None
    assert abs(res.stock_avg_pct - 4.0 / 3) < 1e-6, (
        f"stock_avg_pct 错: {res.stock_avg_pct}"
    )
    assert res.total_count == 3 and res.hit_count == 1
    assert abs(res.hit_rate - 1.0 / 3) < 1e-6
    assert res.sector_pct == 1.85
    assert res.benchmark_pct == 0.5
    # 2026-05-28 17:15 加权改造：
    #   theme_pct = 0.6 * 1.85 + 0.4 * (4/3) = 1.11 + 0.53333... = 1.64333...
    # 2026-05-28 22:30 α 体系金字塔重构：单日 alpha 字段已 DROP，
    # α / α-1 / α+N 全部由聚合层 SQL 现推（theme_pct - zz1000）。
    expected_theme_pct = 0.6 * 1.85 + 0.4 * (4.0 / 3)
    assert res.theme_pct is not None
    assert abs(res.theme_pct - expected_theme_pct) < 1e-6, (
        f"theme_pct 错: 期望 {expected_theme_pct}, 得到 {res.theme_pct}"
    )
    assert res.direction_correct == 1  # +80 强度 + 板块涨 = 同向

    # 写库验证
    from services.storage.ai_inference_db import get_ai_inference_db
    with get_ai_inference_db().connect(readonly=True) as conn:
        n_main = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_prediction_scores "
            "WHERE theme_id = ?", (999802,)
        ).fetchone()["c"]
        n_detail = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stock_scores "
            "WHERE theme_id = ?", (999802,)
        ).fetchone()["c"]
    assert n_main == 1, f"主表期望 1 行，得到 {n_main}"
    assert n_detail == 3, f"明细表期望 3 行，得到 {n_detail}"


def case_03_direction_correct_negative_strength() -> None:
    """direction_correct：+80 强度板块跌 → 0；-50 强度板块跌 → 1。"""
    from services.scoring.script_scorer import score_theme_on_date
    # +80 vs 板块 99990507 跌 -0.5%
    _make_theme(999803, report_date="99990430",
                stock_codes=["TEST_A.SH"], strength_score=80)
    res1 = score_theme_on_date(999803, "99990507",
                               benchmark_ts_code="TEST_BENCH.SH")
    assert res1.direction_correct == 0, (
        f"+80 vs -0.5% 应 direction=0，得到 {res1.direction_correct}"
    )
    # -50 vs 板块跌 → 1
    _make_theme(999804, report_date="99990430",
                stock_codes=["TEST_A.SH"], strength_score=-50)
    res2 = score_theme_on_date(999804, "99990507",
                               benchmark_ts_code="TEST_BENCH.SH")
    assert res2.direction_correct == 1, (
        f"-50 vs -0.5% 应 direction=1，得到 {res2.direction_correct}"
    )


def case_04_hit_rate_threshold() -> None:
    """case_02 已经验过 hit_count=1/total=3；再单独跑阈值=10% 边界。"""
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(999805, report_date="99990430",
                stock_codes=["TEST_A.SH", "TEST_B.SH", "TEST_C.SH"])
    res = score_theme_on_date(999805, "99990506",
                              hit_threshold_pct=10.0,
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.hit_count == 0, (
        f"阈值 10%, 实际涨幅 [5, 1, -2] 都不达 → hit_count=0，"
        f"得到 {res.hit_count}"
    )
    assert res.hit_rate == 0.0


def case_05_cross_holiday_offset() -> None:
    """跨节假日：report_date=99990430 score_date=99990506 → days_offset=1。"""
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(999806, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    res = score_theme_on_date(999806, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.days_offset == 1, (
        f"跨小长假 D+1 应 days_offset=1，得到 {res.days_offset}"
    )
    # D+2
    res2 = score_theme_on_date(999806, "99990507",
                               benchmark_ts_code="TEST_BENCH.SH")
    assert res2.days_offset == 2, (
        f"D+2 应 days_offset=2，得到 {res2.days_offset}"
    )


def case_06_empty_stocks_no_crash() -> None:
    """theme_stocks 为空 → stock_avg_pct=None, hit_rate=0 但不 crash。

    2026-05-28 17:15 加权改造：标的全空时 theme_pct 退回 sector_pct（系数 1.0）。
    2026-05-28 22:30 α 体系金字塔重构：单日 alpha 字段已 DROP，dataclass 不再有 alpha。
    """
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(999807, report_date="99990430", stock_codes=[])
    res = score_theme_on_date(999807, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.stock_avg_pct is None
    assert res.stock_weighted_pct is None
    assert res.total_count == 0
    assert res.hit_rate == 0.0
    assert res.sector_pct == 1.85
    assert res.theme_pct is not None and abs(res.theme_pct - 1.85) < 1e-9, (
        f"无标的兜底应 theme_pct=sector_pct=1.85，得到 {res.theme_pct}"
    )


def case_07_null_sector() -> None:
    """sector_ts_code=NULL → sector_pct=None，direction_correct=None。"""
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999808, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    # 手动改 sector_ts_code 为 NULL
    with get_ai_inference_db().connect() as conn:
        conn.execute(
            "UPDATE theme_predictions SET sector_ts_code = NULL "
            "WHERE id = ?", (999808,)
        )
    res = score_theme_on_date(999808, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.sector_pct is None, (
        f"sector_ts_code=NULL 应得 sector_pct=None，得到 {res.sector_pct}"
    )
    assert res.direction_correct is None, (
        f"sector_pct=None 应得 direction=None，得到 {res.direction_correct}"
    )
    # 2026-05-28 17:15 加权改造：sector_pct=None 时 theme_pct=None
    # 2026-05-28 22:30 α 体系金字塔重构：单日 alpha 字段已删除
    assert res.theme_pct is None, (
        f"sector_pct=None 应得 theme_pct=None，得到 {res.theme_pct}"
    )


def case_08_idempotent_upsert() -> None:
    """同 (theme_id, score_date) 重跑：主表仍 1 行。"""
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999809, report_date="99990430",
                stock_codes=["TEST_A.SH", "TEST_B.SH"])
    score_theme_on_date(999809, "99990506",
                        benchmark_ts_code="TEST_BENCH.SH")
    score_theme_on_date(999809, "99990506",
                        benchmark_ts_code="TEST_BENCH.SH")
    score_theme_on_date(999809, "99990506",
                        benchmark_ts_code="TEST_BENCH.SH")
    with get_ai_inference_db().connect(readonly=True) as conn:
        n_main = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_prediction_scores "
            "WHERE theme_id = ?", (999809,)
        ).fetchone()["c"]
        n_detail = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stock_scores "
            "WHERE theme_id = ?", (999809,)
        ).fetchone()["c"]
    assert n_main == 1, f"3 次 upsert 后主表应 1 行，得到 {n_main}"
    assert n_detail == 2, f"3 次 upsert 后明细表应 2 行，得到 {n_detail}"


def case_09_attach_detach_no_pollution() -> None:
    """连续 50 次 score_theme_on_date 后开普通连接不应残留 ATTACH。"""
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999810, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    for _ in range(50):
        score_theme_on_date(999810, "99990506",
                            benchmark_ts_code="TEST_BENCH.SH")
    # 开新连接，列出 ATTACH database：应只有 main
    with get_ai_inference_db().connect(readonly=True) as conn:
        dbs = conn.execute("PRAGMA database_list").fetchall()
    # main 永远第一，可能还有 temp；不该有 'market'
    names = {r["name"] for r in dbs}
    assert "market" not in names, (
        f"残留 ATTACH 'market': {names}"
    )


def case_10_cascade_delete() -> None:
    """DELETE theme_predictions → 子表 theme_*_scores 自动清。"""
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999811, report_date="99990430",
                stock_codes=["TEST_A.SH", "TEST_B.SH"])
    score_theme_on_date(999811, "99990506",
                        benchmark_ts_code="TEST_BENCH.SH")
    with get_ai_inference_db().connect() as conn:
        n_before = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_prediction_scores "
            "WHERE theme_id = ?", (999811,)
        ).fetchone()["c"]
        assert n_before == 1
        conn.execute(
            "DELETE FROM theme_predictions WHERE id = ?", (999811,)
        )
        n_after = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_prediction_scores "
            "WHERE theme_id = ?", (999811,)
        ).fetchone()["c"]
        n_detail_after = conn.execute(
            "SELECT COUNT(*) AS c FROM theme_stock_scores "
            "WHERE theme_id = ?", (999811,)
        ).fetchone()["c"]
    assert n_after == 0, (
        f"CASCADE 应清主表，实际仍 {n_after} 行"
    )
    assert n_detail_after == 0, (
        f"CASCADE 应清明细表，实际仍 {n_detail_after} 行"
    )


# ---------------------------------------------------------------------------
# 2026-05-28 17:15 加权改造专项用例
# ---------------------------------------------------------------------------


def case_11_compute_theme_pct_pure() -> None:
    """`_compute_theme_pct` 纯函数三分支精确验证。

    覆盖：
        ① sector=None             → None
        ② stock_avg=None          → sector （兜底系数 1.0）
        ③ both 有值               → 0.6·sector + 0.4·stock_avg
    """
    from services.scoring.script_scorer import _compute_theme_pct
    # ① sector_pct = None
    assert _compute_theme_pct(None, 5.0) is None
    assert _compute_theme_pct(None, None) is None
    # ② stock_avg_pct = None → 退回 sector_pct（系数 1.0）
    assert _compute_theme_pct(2.0, None) == 2.0
    assert _compute_theme_pct(-1.5, None) == -1.5
    # ③ both 有值：0.6·sector + 0.4·stock_avg
    val = _compute_theme_pct(10.0, 5.0)
    assert val is not None and abs(val - (0.6 * 10 + 0.4 * 5)) < 1e-9, val
    val = _compute_theme_pct(-2.0, -3.0)
    expected = 0.6 * (-2) + 0.4 * (-3)
    assert val is not None and abs(val - expected) < 1e-9, val
    # 边界：0 / 0
    val = _compute_theme_pct(0.0, 0.0)
    assert val == 0.0, val


def case_12_score_writes_theme_pct_column() -> None:
    """打分实跑后 ``theme_prediction_scores.theme_pct`` 列正确写入。

    case_02 已断言 result.theme_pct 内存值，本用例额外验证表里持久化的列也
    与算法结果一致（防止 _UPSERT_TPS 漏列）。
    """
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db

    _make_theme(
        999812, report_date="99990430",
        stock_codes=["TEST_A.SH", "TEST_B.SH", "TEST_C.SH"],
        strength_score=80,
    )
    res = score_theme_on_date(999812, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    # 2026-05-28 22:30 α 体系金字塔重构：alpha 列已 DROP，仅校验 theme_pct
    with get_ai_inference_db().connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT theme_pct "
            "FROM theme_prediction_scores WHERE theme_id = ?",
            (999812,),
        ).fetchone()
    assert row is not None, "落库后该行应存在"
    assert row["theme_pct"] is not None
    assert abs(row["theme_pct"] - res.theme_pct) < 1e-9, (
        f"DB 中 theme_pct={row['theme_pct']} 与内存 {res.theme_pct} 不一致"
    )


def case_13_theme_pct_weighted() -> None:
    """theme_pct 应等于 ``0.6·sector_pct + 0.4·stock_avg_pct``。

    （原 case_13 校验 alpha = theme_pct - benchmark；2026-05-28 22:30 α 字段
    DROP 后，单日 alpha 已不存在，本用例改为校验 theme_pct 加权口径仍正确。）
        sector_pct=1.85, stock_avg=4/3
        theme_pct = 0.6·1.85 + 0.4·(4/3) = 1.6433...
    """
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(999813, report_date="99990430",
                stock_codes=["TEST_A.SH", "TEST_B.SH", "TEST_C.SH"],
                strength_score=50)
    res = score_theme_on_date(999813, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    expected_theme_pct = 0.6 * 1.85 + 0.4 * (4.0 / 3)
    assert res.theme_pct is not None
    assert abs(res.theme_pct - expected_theme_pct) < 1e-6, (
        f"theme_pct 期望 {expected_theme_pct}，得到 {res.theme_pct}"
    )
    # 防误回归：theme_pct 不应再等于纯 stock_avg 或纯 sector
    assert abs(res.theme_pct - (4.0 / 3)) > 1e-3, "theme_pct 不应等于 stock_avg"
    assert abs(res.theme_pct - 1.85) > 1e-3, "theme_pct 不应等于 sector_pct"


# ---------------------------------------------------------------------------
# 2026-05-28 18:40 α-1 指标 / 中证1000 基准列专项用例
# ---------------------------------------------------------------------------


def case_14_benchmark_zz1000_written() -> None:
    """打分时 ``benchmark_zz1000_pct`` 列写入正确（来自 fact_index_daily 000852.SH）。

    伪 _FAKE_INDEX_DAILY 给 99990506 造了 000852.SH 的 pct_chg=0.8。
    打完分后内存对象 + DB 行的 benchmark_zz1000_pct 都应为 0.8。
    """
    from services.scoring.script_scorer import score_theme_on_date
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999814, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    res = score_theme_on_date(999814, "99990506",
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.benchmark_zz1000_pct is not None, (
        "中证1000 99990506 已造数据，应写入 benchmark_zz1000_pct"
    )
    assert abs(res.benchmark_zz1000_pct - 0.8) < 1e-9, (
        f"期望 0.8，得到 {res.benchmark_zz1000_pct}"
    )
    with get_ai_inference_db().connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT benchmark_zz1000_pct "
            "FROM theme_prediction_scores WHERE theme_id = ?",
            (999814,),
        ).fetchone()
    assert row is not None
    assert row["benchmark_zz1000_pct"] is not None
    assert abs(row["benchmark_zz1000_pct"] - 0.8) < 1e-9, (
        f"DB 中 benchmark_zz1000_pct={row['benchmark_zz1000_pct']} != 0.8"
    )


def case_15_benchmark_zz1000_missing_is_none() -> None:
    """中证1000 当日无数据时 ``benchmark_zz1000_pct`` 应为 None 不 crash。

    99990507 故意没造 000852.SH 行情，benchmark_zz1000_pct 应兜底 None。
    上证综指替身 TEST_BENCH.SH 99990507 是有数据的，benchmark_pct 不受影响。
    """
    from services.scoring.script_scorer import score_theme_on_date
    _make_theme(999815, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    res = score_theme_on_date(999815, "99990507",
                              benchmark_ts_code="TEST_BENCH.SH")
    assert res.benchmark_zz1000_pct is None, (
        f"中证1000 99990507 无数据应得 None，得到 {res.benchmark_zz1000_pct}"
    )
    # 上证综指替身有数据，benchmark_pct 不受 zz1000 缺数据影响
    assert res.benchmark_pct is not None, (
        "上证综指替身有数据，benchmark_pct 不应受 zz1000 缺数据影响"
    )


def case_16_backfill_benchmark_zz1000() -> None:
    """``backfill_benchmark_zz1000`` 能把 NULL 行重新填回。

    流程：
        1. 跑一次打分让某行有 benchmark_zz1000_pct = 0.8
        2. 手动 UPDATE 设回 NULL（模拟迁移 003 后的老行）
        3. 干跑：rows_to_fill=1, rows_updated=0, missing_dates=[]
        4. 真跑：rows_updated=1，列重新等于 0.8
    """
    from services.scoring.script_scorer import (
        backfill_benchmark_zz1000, score_theme_on_date,
    )
    from services.storage.ai_inference_db import get_ai_inference_db
    _make_theme(999816, report_date="99990430",
                stock_codes=["TEST_A.SH"])
    score_theme_on_date(999816, "99990506",
                        benchmark_ts_code="TEST_BENCH.SH")

    # 设 NULL 模拟迁移后老行
    with get_ai_inference_db().connect() as conn:
        conn.execute(
            "UPDATE theme_prediction_scores "
            "SET benchmark_zz1000_pct = NULL WHERE theme_id = ?",
            (999816,),
        )

    # 干跑
    dry = backfill_benchmark_zz1000(dry_run=True)
    assert dry.rows_to_fill >= 1, (
        f"干跑应识别至少 1 行 NULL，得到 rows_to_fill={dry.rows_to_fill}"
    )
    assert dry.rows_updated == 0, (
        f"干跑不应实际写库，得到 rows_updated={dry.rows_updated}"
    )

    # 真跑
    real = backfill_benchmark_zz1000(dry_run=False)
    assert real.rows_updated >= 1, (
        f"真跑应至少更新 1 行，得到 {real.rows_updated}"
    )

    # 验证列已填回 0.8
    with get_ai_inference_db().connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT benchmark_zz1000_pct "
            "FROM theme_prediction_scores WHERE theme_id = ?",
            (999816,),
        ).fetchone()
    assert row is not None and row["benchmark_zz1000_pct"] is not None, (
        "回填后该列应非 NULL"
    )
    assert abs(row["benchmark_zz1000_pct"] - 0.8) < 1e-9, (
        f"回填值应等于 fact_index_daily 中的 0.8，得到 "
        f"{row['benchmark_zz1000_pct']}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_anti_lookahead", case_01_anti_lookahead),
    ("02_single_theme_d1", case_02_single_theme_d1),
    ("03_direction_correct_negative_strength",
     case_03_direction_correct_negative_strength),
    ("04_hit_rate_threshold", case_04_hit_rate_threshold),
    ("05_cross_holiday_offset", case_05_cross_holiday_offset),
    ("06_empty_stocks_no_crash", case_06_empty_stocks_no_crash),
    ("07_null_sector", case_07_null_sector),
    ("08_idempotent_upsert", case_08_idempotent_upsert),
    ("09_attach_detach_no_pollution", case_09_attach_detach_no_pollution),
    ("10_cascade_delete", case_10_cascade_delete),
    # 2026-05-28 17:15 加权改造
    ("11_compute_theme_pct_pure", case_11_compute_theme_pct_pure),
    ("12_score_writes_theme_pct_column", case_12_score_writes_theme_pct_column),
    ("13_theme_pct_weighted", case_13_theme_pct_weighted),
    # 2026-05-28 18:40 α-1 / 中证1000 基准列
    ("14_benchmark_zz1000_written", case_14_benchmark_zz1000_written),
    ("15_benchmark_zz1000_missing_is_none",
     case_15_benchmark_zz1000_missing_is_none),
    ("16_backfill_benchmark_zz1000", case_16_backfill_benchmark_zz1000),
]


def main() -> int:
    print(f"=== Phase 2 打分核心回归 ({len(_CASES)} 用例) ===\n")
    _cleanup_all()
    _seed_all()
    try:
        for name, fn in _CASES:
            try:
                fn()
                print(f"[PASS] {name}")
            except AssertionError as exc:
                print(f"[FAIL] {name}: {exc}")
                return 1
            except Exception as exc:  # noqa: BLE001
                print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
                return 1
    finally:
        _cleanup_all()
        print("\n[cleanup] 已删除所有 9999* / TEST_* / 999800+ 伪数据")
    print(f"\n[全部通过] {len(_CASES)}/{len(_CASES)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
