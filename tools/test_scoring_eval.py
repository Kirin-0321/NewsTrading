"""模板/报告评估查询回归测试（24 用例，纯本地 SQLite，0 LLM 0 网络）。

覆盖
----
1. 默认调用 get_template_eval → 拿到全部样本
2. is_backtest_filter=1 → 只看回测
3. is_backtest_filter=0 → 只看真实
4. time_dim="report_date" 行为差异（早于 cutoff 的 score_date 仍能命中）
5. ignore_version=False → 按 (prompt_id, version) 分组
6. 默认调用 get_report_eval → 每份报告一行
7. get_report_eval(prompt_id=xxx) 过滤
8. get_report_eval(is_backtest_filter=1) 过滤
9. get_report_eval(time_dim) 切换
10. 非法 time_dim → ValueError
11. 未打分的报告也出现在 get_report_eval 结果中
12. rescore_one_report：报告无题材 → ok=True + errors 提示
13. rescore_one_report：report_id 不存在 → ValueError
14. delete_report(backtest)：级联删 theme_predictions / scores / md 文件
15. delete_report(real, allow_real=False) → PermissionError 拒绝
16. delete_report(dry_run=True) → 不动 db / md，但统计预估正确
17. get_theme_eval_for_report：报告下题材级聚合（含 D+1~D+5 透视）
18. get_theme_eval_for_report：未打分的报告返回 stocks_count 但 d* 全 None
19. get_stock_scores_for_theme：题材下标的级 D+N 涨跌 + 命中标记透视
20. get_stock_scores_for_theme：题材无标的 → 空列表（不抛错）
21. **v2** 题材级 D+1 来自 sector_pct（不是 stock_weighted_pct）
22. **v2** 报告级 d1_avg 按 |strength_score| 加权（仅看多）
23. **v2** 模板级 d1_avg 同样按 |strength_score| 加权
24. **v2** 全部 strength≤0 时 d1_avg = NULL（NULLIF 兜底）

数据隔离
--------
* 所有伪数据 prompt_id 以 ``TEST_EV_`` 开头，结尾全清
* file_path / report_path 走 ``data/AI_analysis/TEST_EV_xxx.md`` 命名
* score_date / report_date 用相对今天的偏移（避免硬编码过期）

运行
----
::

    python tools/test_scoring_eval.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

_PREFIX = "TEST_EV_"


def _yyyymmdd_back(days: int) -> str:
    """YYYYMMDD 8 字符（用于 score_date 字段）。"""
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _yyyymmdash_back(days: int) -> str:
    """[2026-05-27 18:30 协议归一化后已等价于 _yyyymmdd_back] 兼容旧名保留。

    schema 协议下 ``ar / tp / tps.report_date`` 全部统一 YYYYMMDD。
    """
    return _yyyymmdd_back(days)


def _cleanup() -> None:
    """删 ai_reports / theme_predictions / theme_prediction_scores 的全部伪数据。

    含 case_14 残留的 ``TEST_EV_DEL_BT_x.md`` 等物理伪 md 文件。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.database import get_project_root
    ai = get_ai_inference_db()
    with ai.connect() as conn:
        # 先清子表（无 prompt_id 的标的层走 theme_id 关联）
        conn.execute(
            "DELETE FROM theme_stock_scores "
            "WHERE theme_id IN ("
            "  SELECT id FROM theme_predictions "
            "  WHERE prompt_id LIKE 'TEST_EV_%'"
            ")"
        )
        conn.execute(
            "DELETE FROM theme_stocks "
            "WHERE theme_id IN ("
            "  SELECT id FROM theme_predictions "
            "  WHERE prompt_id LIKE 'TEST_EV_%'"
            ")"
        )
        conn.execute(
            "DELETE FROM theme_prediction_scores "
            "WHERE prompt_id LIKE 'TEST_EV_%'"
        )
        conn.execute(
            "DELETE FROM theme_predictions "
            "WHERE prompt_id LIKE 'TEST_EV_%'"
        )
        conn.execute(
            "DELETE FROM ai_reports WHERE prompt_id LIKE 'TEST_EV_%'"
        )
    # 物理清残留伪 md（case_14 等用过）
    ai_dir = Path(get_project_root()) / "data" / "AI_analysis"
    if ai_dir.exists():
        for p in ai_dir.glob("TEST_EV_*.md"):
            try:
                p.unlink()
            except OSError:
                pass


def _seed_dataset() -> None:
    """构造一组多模板 + 真实/回测交叉 + 多日 D+1~D+5 样本。

    生成结构（举例）：

        ai_reports:
            R1: prompt=TEST_EV_A v1, real, report_date=today-7
            R2: prompt=TEST_EV_A v2, real, report_date=today-5
            R3: prompt=TEST_EV_A v1, BT,   report_date=today-10
            R4: prompt=TEST_EV_B v1, real, report_date=today-3

        theme_predictions:
            R1 → 2 themes (T1, T2)
            R2 → 1 theme  (T3)
            R3 → 2 themes (T4, T5)
            R4 → 1 theme  (T6)

        theme_prediction_scores:
            每个 theme 各打 D+1, D+3, D+5 三个分（score_date = report_date+N）
            stock_weighted_pct / alpha / hit_rate / direction_correct 给固定值
            便于断言均值
    """
    from services.storage.ai_inference_db import get_ai_inference_db

    now_iso = datetime.now().isoformat(timespec="seconds")
    ai = get_ai_inference_db()
    ai.ensure_schema()

    # 4 份报告
    reports = [
        # (prompt_id, version, is_backtest, days_before_today)
        ("TEST_EV_A", "v1", 0, 7),
        ("TEST_EV_A", "v2", 0, 5),
        ("TEST_EV_A", "v1", 1, 10),
        ("TEST_EV_B", "v1", 0, 3),
    ]
    theme_counts = [2, 1, 2, 1]

    with ai.connect() as conn:
        for idx, ((pid, pv, bt, dbk), tc) in enumerate(
            zip(reports, theme_counts), start=1,
        ):
            base_dt = datetime.now() - timedelta(days=dbk)
            # 协议：report_date 统一 YYYYMMDD（2026-05-27 18:30 起强校验）
            rdate_dash = base_dt.strftime("%Y%m%d")
            file_path = f"data/AI_analysis/{_PREFIX}r{idx}.md"
            cur = conn.execute(
                "INSERT INTO ai_reports "
                "(report_date, file_path, provider, model, "
                " prompt_category, prompt_id, prompt_version, "
                " news_count, theme_extracted, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rdate_dash, file_path, "mock", "mock-model",
                    "analysis", pid, pv, 10, 1, bt, now_iso,
                ),
            )
            report_id = int(cur.lastrowid)

            for j in range(tc):
                theme_name = f"{_PREFIX}T{report_id}_{j}"
                tcur = conn.execute(
                    "INSERT INTO theme_predictions "
                    "(report_id, report_date, report_path, "
                    " theme_name, strength_score, strength_level, reason, "
                    " prompt_id, prompt_version, is_backtest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{_PREFIX}rep_{report_id}",
                        rdate_dash, file_path,
                        theme_name,
                        50, "中等利多", "伪原因",
                        pid, pv, bt, now_iso,
                    ),
                )
                tid = int(tcur.lastrowid)

                # 3 个打分（D+1 / D+3 / D+5）；score_date 用 YYYYMMDD 8 字符
                # 2026-05-28 17:15 加权改造：theme_pct 同步落库
                #   theme_pct = 0.6·sector + 0.4·stock_avg = 0.6·2 + 0.4·1.5 = 1.8
                for d_off in (1, 3, 5):
                    sd = (
                        base_dt + timedelta(days=d_off)
                    ).strftime("%Y%m%d")
                    # 2026-05-28 22:30 α 体系金字塔重构：alpha 列已 DROP，
                    # 改写 benchmark_zz1000_pct（中证1000 基准），
                    # α/α-1/α+N 全部由聚合层 SQL 现推。
                    conn.execute(
                        "INSERT INTO theme_prediction_scores "
                        "(theme_id, prompt_id, prompt_version, "
                        " report_date, score_date, days_offset, "
                        " sector_pct, stock_avg_pct, stock_weighted_pct, "
                        " theme_pct, "
                        " hit_count, total_count, hit_rate, "
                        " benchmark_pct, benchmark_zz1000_pct, "
                        " direction_correct, "
                        " created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "        ?, ?, ?, ?, ?)",
                        (
                            tid, pid, pv,
                            rdate_dash, sd, d_off,
                            2.0, 1.5, 2.0, 1.8,
                            3, 5, 0.6,
                            1.0, 0.8, 1,
                            now_iso,
                        ),
                    )

                # R1 的第一个题材（T1）补 3 只 theme_stocks + 标的级打分，
                # 给 case_17/19 用。其余 theme 不动（保持原 18 score 用例兼容）。
                if idx == 1 and j == 0:
                    stock_seed = [
                        # (name, code, normalized, role, [(d_off, pct, hit)])
                        ("TS_A", "600001", "600001.SH", "核心",
                         [(1, 5.0, 1), (3, 2.0, 0), (5, 8.0, 1)]),
                        ("TS_B", "600002", "600002.SH", "辐射",
                         [(1, -1.0, 0), (3, -2.0, 0)]),
                        ("TS_C", "600003", "600003.SH", "辐射",
                         []),  # 故意 0 打分，验证退化态
                    ]
                    for sname, scode, sncode, srole, sscores in stock_seed:
                        scur = conn.execute(
                            "INSERT INTO theme_stocks "
                            "(theme_id, stock_name, stock_code, "
                            " normalized_code, role, reason) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (tid, sname, scode, sncode, srole, "夹具理由"),
                        )
                        tsid = int(scur.lastrowid)
                        for d_off, pct, hit in sscores:
                            sd2 = (
                                base_dt + timedelta(days=d_off)
                            ).strftime("%Y%m%d")
                            conn.execute(
                                "INSERT INTO theme_stock_scores "
                                "(theme_stock_id, theme_id, "
                                " normalized_code, report_date, "
                                " score_date, days_offset, pct_chg, "
                                " is_hit, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    tsid, tid, sncode,
                                    rdate_dash, sd2, d_off,
                                    pct, hit, now_iso,
                                ),
                            )


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_template_default() -> None:
    """get_template_eval(): 默认 score_date / 全部 → 应有 TEST_EV_A 和 TEST_EV_B 两行。"""
    from services.scoring.scoring_service import get_template_eval
    rows = get_template_eval(days=30)
    test_rows = [r for r in rows if r["prompt_id"].startswith(_PREFIX)]
    pids = sorted({r["prompt_id"] for r in test_rows})
    assert pids == ["TEST_EV_A", "TEST_EV_B"], (
        f"应有 TEST_EV_A/B 两个模板，得到 {pids}"
    )
    # ignore_version=True 时，TEST_EV_A 合并 v1+v2 → 3 个 theme（1 BT + 2 real）
    row_a = next(r for r in test_rows if r["prompt_id"] == "TEST_EV_A")
    assert row_a["sample_count"] == 5, (
        f"TEST_EV_A 应合并 5 个 theme（R1=2 + R2=1 + R3=2），"
        f"得到 {row_a['sample_count']}"
    )
    # stock_weighted_pct 全部 = 2.0 → D+N 平均 = 2.0
    for col in ("d1_avg", "d3_avg", "d5_avg"):
        assert abs(row_a[col] - 2.0) < 0.01, (
            f"{col} 应 = 2.0，得到 {row_a[col]}"
        )
    assert row_a["last_report_date"] is not None


def case_02_template_backtest_only() -> None:
    """is_backtest_filter=1 → 只剩 R3 (TEST_EV_A v1, 2 themes)。"""
    from services.scoring.scoring_service import get_template_eval
    rows = get_template_eval(days=30, is_backtest_filter=1)
    test_rows = [r for r in rows if r["prompt_id"].startswith(_PREFIX)]
    assert len(test_rows) == 1, f"仅回测应只有 1 个模板，得到 {len(test_rows)}"
    assert test_rows[0]["prompt_id"] == "TEST_EV_A"
    assert test_rows[0]["sample_count"] == 2, (
        f"R3 应有 2 themes，得到 {test_rows[0]['sample_count']}"
    )


def case_03_template_real_only() -> None:
    """is_backtest_filter=0 → 真实 = R1+R2 (A=3 themes) + R4 (B=1 theme)。"""
    from services.scoring.scoring_service import get_template_eval
    rows = get_template_eval(days=30, is_backtest_filter=0)
    test_rows = [r for r in rows if r["prompt_id"].startswith(_PREFIX)]
    pids = {r["prompt_id"]: r["sample_count"] for r in test_rows}
    assert pids == {"TEST_EV_A": 3, "TEST_EV_B": 1}, (
        f"真实应 A=3 B=1，得到 {pids}"
    )


def case_04_template_time_dim() -> None:
    """time_dim 切换：report_date 比 score_date 早 N 天 → 用 report_date 不会少。

    具体：报告 R3 在 today-10，score 在 today-9~today-5；
    days=7 + time_dim=score_date 时 R3 的所有 score 都 < cutoff，全过滤掉
    （cutoff = today-7）。
    days=7 + time_dim=report_date 时 R3 的 report_date=today-10 也 < cutoff，
    同样过滤掉。所以两个维度都应该过滤掉 R3。
    """
    from services.scoring.scoring_service import get_template_eval
    rows_sd = get_template_eval(
        days=7, time_dim="score_date", is_backtest_filter=1,
    )
    rows_rd = get_template_eval(
        days=7, time_dim="report_date", is_backtest_filter=1,
    )
    sd_test = [r for r in rows_sd if r["prompt_id"].startswith(_PREFIX)]
    rd_test = [r for r in rows_rd if r["prompt_id"].startswith(_PREFIX)]
    # days=7 cutoff = today-7；R3 report_date=today-10，score 最早 today-9
    # report_date 维度：today-10 < today-7 → 过滤，应 0 行
    # score_date 维度：D+1=today-9 < today-7 → 过滤，但 D+3=today-7
    #   和 D+5=today-5 ≥ today-7 → 命中，应 1 行
    # 但 SQL 用 BETWEEN >= cutoff，所以包含 cutoff 那天。
    # 因为存储格式是 YYYYMMDD，时分秒为 0 → 等于"今天的字符串"减 N 天后字符串比较
    # 实际上 SQL '>=' 字符串字典序 vs cutoff 字符串，对 YYYYMMDD 是单调的
    # 所以两个维度结果有差异：report_date 严格 0 行，score_date 至少有 D+3/D+5
    assert len(rd_test) == 0, (
        f"report_date 维度应过滤掉 R3，得到 {len(rd_test)} 行"
    )
    assert len(sd_test) == 1, (
        f"score_date 维度应保留 R3 的 D+3/D+5 行，得到 {len(sd_test)} 行"
    )


def case_05_template_keep_version() -> None:
    """ignore_version=False → TEST_EV_A v1/v2 分两行。"""
    from services.scoring.scoring_service import get_template_eval
    rows = get_template_eval(days=30, ignore_version=False)
    test_rows = [r for r in rows if r["prompt_id"].startswith(_PREFIX)]
    a_rows = [r for r in test_rows if r["prompt_id"] == "TEST_EV_A"]
    versions = sorted(
        (r["prompt_version"] or "") for r in a_rows
    )
    assert versions == ["v1", "v2"], (
        f"应有 v1+v2 两行，得到 {versions}"
    )


def case_06_report_default() -> None:
    """get_report_eval(): 默认 → 4 份测试报告各 1 行。"""
    from services.scoring.scoring_service import get_report_eval
    rows = get_report_eval(days=30)
    test_rows = [
        r for r in rows
        if (r["prompt_id"] or "").startswith(_PREFIX)
    ]
    assert len(test_rows) == 4, f"应 4 份报告，得到 {len(test_rows)}"
    # 校验 themes_count 等于种子定义
    by_id = {
        Path(r["file_path"]).stem: r["themes_count"]
        for r in test_rows
    }
    assert by_id == {
        f"{_PREFIX}r1": 2,
        f"{_PREFIX}r2": 1,
        f"{_PREFIX}r3": 2,
        f"{_PREFIX}r4": 1,
    }, f"themes_count 不对：{by_id}"


def case_07_report_filter_prompt() -> None:
    """get_report_eval(prompt_id='TEST_EV_B') → 只剩 R4 一行。"""
    from services.scoring.scoring_service import get_report_eval
    rows = get_report_eval(days=30, prompt_id="TEST_EV_B")
    test_rows = [
        r for r in rows
        if (r["prompt_id"] or "").startswith(_PREFIX)
    ]
    assert len(test_rows) == 1, f"应只 1 份，得到 {len(test_rows)}"
    assert test_rows[0]["prompt_id"] == "TEST_EV_B"


def case_08_report_backtest_only() -> None:
    """get_report_eval(is_backtest_filter=1) → 只剩 R3。"""
    from services.scoring.scoring_service import get_report_eval
    rows = get_report_eval(days=30, is_backtest_filter=1)
    test_rows = [
        r for r in rows
        if (r["prompt_id"] or "").startswith(_PREFIX)
    ]
    assert len(test_rows) == 1
    assert test_rows[0]["is_backtest"] == 1
    assert test_rows[0]["themes_count"] == 2


def case_09_report_time_dim_switch() -> None:
    """time_dim=report_date：days=7 → R3(today-10) / R1(today-7) 边界。

    cutoff = today-7。R1 的 report_date = today-7 → 字符串 >= cutoff 命中。
    R3 = today-10 → 过滤。R2 = today-5 命中。R4 = today-3 命中。
    应得 R1 + R2 + R4 = 3 行。
    """
    from services.scoring.scoring_service import get_report_eval
    rows = get_report_eval(days=7, time_dim="report_date")
    test_rows = [
        r for r in rows
        if (r["prompt_id"] or "").startswith(_PREFIX)
    ]
    bt_set = {r["is_backtest"] for r in test_rows}
    assert 1 not in bt_set, "R3 (回测, today-10) 应被过滤"
    assert len(test_rows) == 3, (
        f"应 3 份（R1/R2/R4），得到 {len(test_rows)}"
    )


def case_11_unscored_reports_appear() -> None:
    """新版 get_report_eval：未打分的报告（scores 0 行）也应出现在结果中。

    场景：手动插一份 TEST_EV_NOSCORE_* 报告 + 2 个题材但 0 打分 →
    应能查到 score_status='none', themes_count=2, scored_pairs=0,
    expected_pairs=10。
    """
    from services.scoring.scoring_service import get_report_eval
    from services.storage.ai_inference_db import get_ai_inference_db

    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rdate_dash = _yyyymmdash_back(2)
    file_path = "data/AI_analysis/TEST_EV_NOSCORE_x.md"

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, 'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_NOSCORE', 'v1', 10, 1, 0, ?)",
            (rdate_dash, file_path, now_iso),
        )
        rid = int(cur.lastrowid)
        for j in range(2):
            conn.execute(
                "INSERT INTO theme_predictions "
                "(report_id, report_date, report_path, theme_name, "
                " strength_score, strength_level, reason, "
                " prompt_id, prompt_version, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, 50, '中等利多', '理由', "
                "        'TEST_EV_NOSCORE', 'v1', 0, ?)",
                (
                    f"TEST_EV_NOSCORE_rep_{rid}",
                    rdate_dash, file_path,
                    f"TEST_EV_NOSCORE_T{j}",
                    now_iso,
                ),
            )

    try:
        rows = get_report_eval(days=30, prompt_id="TEST_EV_NOSCORE")
        assert len(rows) == 1, (
            f"未打分报告也应出现，得到 {len(rows)} 行"
        )
        r = rows[0]
        assert r["themes_count"] == 2, (
            f"themes_count 应为 2，得到 {r['themes_count']}"
        )
        assert r["scored_pairs"] == 0, (
            f"scored_pairs 应为 0，得到 {r['scored_pairs']}"
        )
        assert r["expected_pairs"] == 10, (
            f"expected_pairs 应为 10 (2 themes × 5)，"
            f"得到 {r['expected_pairs']}"
        )
        assert r["score_status"] == "none", (
            f"未打分应 status=none，得到 {r['score_status']}"
        )
        # D+1~D+5 均 None
        for col in ("d1_avg", "d2_avg", "d3_avg", "d4_avg", "d5_avg"):
            assert r[col] is None, f"{col} 应 None，得到 {r[col]}"
    finally:
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_NOSCORE'"
            )
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_NOSCORE'"
            )


def case_12_rescore_one_report_no_themes() -> None:
    """rescore_one_report：report 存在但 0 题材 → ok=True + themes_total=0
    + errors 含说明。
    """
    from services.scoring.scoring_service import rescore_one_report
    from services.storage.ai_inference_db import get_ai_inference_db

    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rdate_dash = _yyyymmdash_back(1)

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, 'data/AI_analysis/TEST_EV_EMPTY_x.md', "
            "        'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_EMPTY', 'v1', 0, 0, 0, ?)",
            (rdate_dash, now_iso),
        )
        rid = int(cur.lastrowid)

    try:
        res = rescore_one_report(rid)
        assert res.ok, f"无题材也应 ok=True，得到 {res.ok}"
        assert res.themes_total == 0, (
            f"themes_total 应 0，得到 {res.themes_total}"
        )
        assert res.errors and "无题材" in res.errors[0], (
            f"errors 应含'无题材'，得到 {res.errors}"
        )
    finally:
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_EMPTY'"
            )


def case_13_rescore_one_report_not_found() -> None:
    """rescore_one_report：report_id 不存在 → ValueError。"""
    from services.scoring.scoring_service import rescore_one_report
    try:
        rescore_one_report(99999999)
    except ValueError as exc:
        assert "不存在" in str(exc)
        return
    raise AssertionError("不存在的 report_id 应 raise ValueError")


def case_14_delete_backtest_cascade() -> None:
    """delete_report(backtest)：级联删 theme_predictions / scores / md。

    场景：插一份 TEST_EV_DEL_BT_* backtest 报告 + 2 theme + 4 score 行，
    再 delete_report → 断言三张表都清光 + md 文件 unlink。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import delete_report
    from services.storage.database import get_project_root

    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rdate = _yyyymmdash_back(1)
    file_path = "data/AI_analysis/TEST_EV_DEL_BT_x.md"

    # 物理建一份伪 md 文件，验证 unlink 真发生
    abs_path = Path(get_project_root()) / file_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text("# fake backtest md", encoding="utf-8")
    assert abs_path.exists(), "夹具 md 文件未建成"

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, 'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_DEL_BT', 'v1', 5, 1, 1, ?)",
            (rdate, file_path, now_iso),
        )
        rid = int(cur.lastrowid)
        theme_ids = []
        for j in range(2):
            tcur = conn.execute(
                "INSERT INTO theme_predictions "
                "(report_id, report_date, report_path, theme_name, "
                " strength_score, strength_level, reason, "
                " prompt_id, prompt_version, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, 50, '中等利多', '理由', "
                "        'TEST_EV_DEL_BT', 'v1', 1, ?)",
                (
                    f"TEST_EV_DEL_BT_rep_{rid}", rdate, file_path,
                    f"TEST_EV_DEL_BT_T{j}", now_iso,
                ),
            )
            theme_ids.append(int(tcur.lastrowid))
        # 给每个 theme 各打 2 个 score（D+1/D+3）
        for tid in theme_ids:
            for d_off in (1, 3):
                sd = (datetime.now() + timedelta(days=d_off)).strftime(
                    "%Y%m%d"
                )
                conn.execute(
                    "INSERT INTO theme_prediction_scores "
                    "(theme_id, prompt_id, prompt_version, "
                    " report_date, score_date, days_offset, "
                    " sector_pct, stock_avg_pct, stock_weighted_pct, "
                    " theme_pct, "
                    " hit_count, total_count, hit_rate, "
                    " benchmark_pct, benchmark_zz1000_pct, "
                    " direction_correct, "
                    " created_at) "
                    "VALUES (?, 'TEST_EV_DEL_BT', 'v1', "
                    "        ?, ?, ?, 1.0, 1.0, 1.0, 1.0, 1, 1, 1.0, "
                    "        0.5, 0.4, 1, ?)",
                    (tid, rdate, sd, d_off, now_iso),
                )

    try:
        res = delete_report(rid, allow_real=False, delete_md=True)
        assert res.ok, f"删除应 ok=True，得到 {res.ok}"
        assert res.ai_reports_deleted == 1, (
            f"ai_reports 应 -1，得到 {res.ai_reports_deleted}"
        )
        assert res.theme_predictions_deleted == 2, (
            f"theme_predictions 应 -2，得到 "
            f"{res.theme_predictions_deleted}"
        )
        assert res.md_file_deleted, "md 文件应被删"
        assert not abs_path.exists(), "md 文件实际仍在"

        # 校验子表 CASCADE 真清光
        with adb.connect(readonly=True) as conn:
            n_ar = conn.execute(
                "SELECT COUNT(*) FROM ai_reports WHERE id = ?", (rid,),
            ).fetchone()[0]
            n_tp = conn.execute(
                "SELECT COUNT(*) FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_DEL_BT'"
            ).fetchone()[0]
            n_tps = conn.execute(
                "SELECT COUNT(*) FROM theme_prediction_scores "
                "WHERE prompt_id = 'TEST_EV_DEL_BT'"
            ).fetchone()[0]
        assert n_ar == 0, f"ai_reports 残留 {n_ar}"
        assert n_tp == 0, f"theme_predictions 残留 {n_tp}"
        assert n_tps == 0, (
            f"theme_prediction_scores 应被 CASCADE 清空，残留 {n_tps}"
        )
    finally:
        # 兜底清理（即使断言失败也要扫尾）
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_DEL_BT'"
            )
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_DEL_BT'"
            )
        if abs_path.exists():
            abs_path.unlink()


def case_15_delete_real_protected() -> None:
    """delete_report(real, allow_real=False) → PermissionError 拒绝。

    确认真实日常报告（is_backtest=0）默认不能被删，
    且失败后 db 不受任何影响（事务回滚）。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import delete_report

    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rdate = _yyyymmdash_back(1)
    file_path = "data/AI_analysis/TEST_EV_DEL_REAL_x.md"

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, 'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_DEL_REAL', 'v1', 5, 0, 0, ?)",
            (rdate, file_path, now_iso),
        )
        rid = int(cur.lastrowid)

    try:
        try:
            delete_report(rid, allow_real=False)
        except PermissionError as exc:
            assert "is_backtest=0" in str(exc) or "真实" in str(exc), (
                f"错误信息应提示 is_backtest=0/真实，得到 {exc}"
            )
        else:
            raise AssertionError(
                "删真实日常未传 allow_real 应抛 PermissionError"
            )

        # 校验 db 行仍在（事务未损）
        with adb.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT id FROM ai_reports WHERE id = ?", (rid,),
            ).fetchone()
        assert row is not None, "PermissionError 后 ai_reports 行不该被删"

        # 再用 allow_real=True 验证能删
        res = delete_report(rid, allow_real=True)
        assert res.ok and res.ai_reports_deleted == 1, (
            f"allow_real=True 后应能删，得到 {res}"
        )
    finally:
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_DEL_REAL'"
            )


def case_16_delete_dry_run_no_change() -> None:
    """delete_report(dry_run=True) → 不动 db / 不动 md，但统计数正确。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import delete_report

    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rdate = _yyyymmdash_back(1)
    file_path = "data/AI_analysis/TEST_EV_DEL_DRY_x.md"

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, 'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_DEL_DRY', 'v1', 5, 0, 1, ?)",
            (rdate, file_path, now_iso),
        )
        rid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO theme_predictions "
            "(report_id, report_date, report_path, theme_name, "
            " strength_score, strength_level, reason, "
            " prompt_id, prompt_version, is_backtest, created_at) "
            "VALUES (?, ?, ?, 'T0', 50, '中等利多', '理由', "
            "        'TEST_EV_DEL_DRY', 'v1', 1, ?)",
            (
                f"TEST_EV_DEL_DRY_rep_{rid}", rdate, file_path, now_iso,
            ),
        )

    try:
        res = delete_report(rid, dry_run=True)
        assert res.ok and res.dry_run, f"dry_run 标记缺失 {res}"
        assert res.ai_reports_deleted == 1, "dry_run 应预估会删 1 行"
        assert res.theme_predictions_deleted == 1, (
            "dry_run 应预估会删 1 个 theme_predictions"
        )

        # 实查 db：行应仍在（dry_run 没动）
        with adb.connect(readonly=True) as conn:
            n_ar = conn.execute(
                "SELECT COUNT(*) FROM ai_reports WHERE id = ?", (rid,),
            ).fetchone()[0]
            n_tp = conn.execute(
                "SELECT COUNT(*) FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_DEL_DRY'"
            ).fetchone()[0]
        assert n_ar == 1, f"dry_run 后 ai_reports 不该掉，残留 {n_ar}"
        assert n_tp == 1, f"dry_run 后 theme_predictions 不该掉 {n_tp}"
    finally:
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_DEL_DRY'"
            )
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_DEL_DRY'"
            )


def case_10_invalid_time_dim() -> None:
    """非法 time_dim → ValueError。"""
    from services.scoring.scoring_service import get_template_eval
    try:
        get_template_eval(days=30, time_dim="bad_field")
    except ValueError as exc:
        assert "time_dim" in str(exc)
        return
    raise AssertionError("非法 time_dim 应抛 ValueError")


# ---------------------------------------------------------------------------
# 树形展开新增用例（2026-05-28）
# ---------------------------------------------------------------------------


def _find_test_report_id(prompt_id: str, version: str = "v1") -> int:
    """工具：取一份 TEST_EV_* 的 ai_reports.id（用于树形 API 测试）。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT id FROM ai_reports "
            "WHERE prompt_id = ? AND prompt_version = ? "
            "ORDER BY id ASC LIMIT 1",
            (prompt_id, version),
        ).fetchone()
    return int(row["id"]) if row else 0


def case_17_theme_in_report_basic() -> None:
    """get_theme_eval_for_report：R1（A v1, real）下 2 个题材 + D+N 透视。

    R1 在夹具里 prompt_id=TEST_EV_A v1, real，含 T1/T2 两个 theme，
    每个 theme 各 3 个打分（D+1/D+3/D+5）。
    断言：返回 2 行 + d1/d3/d5 = 2.0 + d2/d4 = None + scored_pairs=3。
    R1 的 T1 还补了 3 只 theme_stocks，stocks_count 应 = 3。
    """
    from services.scoring.scoring_service import get_theme_eval_for_report
    rid = _find_test_report_id("TEST_EV_A", "v1")
    assert rid > 0, "找不到 TEST_EV_A v1 的报告，夹具异常"
    rows = get_theme_eval_for_report(rid)
    assert len(rows) == 2, f"R1 应 2 个题材，得到 {len(rows)}"

    for r in rows:
        assert r["scored_pairs"] == 3, (
            f"每题材应 3 个打分，得到 {r['scored_pairs']}"
        )
        assert abs((r["d1"] or 0) - 2.0) < 0.01, (
            f"d1 应 2.0，得到 {r['d1']}"
        )
        assert r["d2"] is None, f"d2 应 None（未打），得到 {r['d2']}"
        assert abs((r["d3"] or 0) - 2.0) < 0.01
        assert r["d4"] is None
        assert abs((r["d5"] or 0) - 2.0) < 0.01

    # T1 应有 3 只 stocks（夹具特意补的）；T2 应有 0 只
    stocks_counts = sorted(r["stocks_count"] for r in rows)
    assert stocks_counts == [0, 3], (
        f"R1 题材的 stocks_count 应为 [0, 3]，得到 {stocks_counts}"
    )


def case_18_theme_in_report_no_score() -> None:
    """get_theme_eval_for_report：未打分报告下题材 d1~d5 全 None。

    复用 case_11 的思路：插一份 TEST_EV_TIR_NS 报告 + 1 个 theme + 0 打分 →
    d1~d5 / alpha_avg / hit_rate_avg 全 None，scored_pairs=0。
    """
    from datetime import datetime as _dt
    from services.scoring.scoring_service import get_theme_eval_for_report
    from services.storage.ai_inference_db import get_ai_inference_db

    adb = get_ai_inference_db()
    now_iso = _dt.now().isoformat(timespec="seconds")
    rdate = _yyyymmdash_back(2)
    fp = "data/AI_analysis/TEST_EV_TIR_NS_x.md"

    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, 'mock', 'mock-m', 'analysis', "
            "        'TEST_EV_TIR_NS', 'v1', 5, 1, 0, ?)",
            (rdate, fp, now_iso),
        )
        rid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO theme_predictions "
            "(report_id, report_date, report_path, theme_name, "
            " strength_score, strength_level, reason, "
            " prompt_id, prompt_version, is_backtest, created_at) "
            "VALUES (?, ?, ?, 'NS_T1', 30, '中性偏多', '理由', "
            "        'TEST_EV_TIR_NS', 'v1', 0, ?)",
            (
                f"TEST_EV_TIR_NS_rep_{rid}",
                rdate, fp, now_iso,
            ),
        )

    try:
        rows = get_theme_eval_for_report(rid)
        assert len(rows) == 1, f"应 1 个题材，得到 {len(rows)}"
        r = rows[0]
        assert r["scored_pairs"] == 0
        assert r["stocks_count"] == 0
        for k in ("d1", "d2", "d3", "d4", "d5",
                  "alpha_avg", "hit_rate_avg",
                  "direction_correct_rate", "sector_pct_avg"):
            assert r[k] is None, f"{k} 应 None，得到 {r[k]}"
    finally:
        with adb.connect() as conn:
            conn.execute(
                "DELETE FROM theme_predictions "
                "WHERE prompt_id = 'TEST_EV_TIR_NS'"
            )
            conn.execute(
                "DELETE FROM ai_reports "
                "WHERE prompt_id = 'TEST_EV_TIR_NS'"
            )


def case_19_stocks_in_theme_basic() -> None:
    """get_stock_scores_for_theme：R1.T1 下 3 只标的，D+N 涨跌 + 命中标记透视。

    夹具：TS_A 有 D+1/D+3/D+5 三天打分 → scored_days=3
          TS_B 有 D+1/D+3 两天 → scored_days=2
          TS_C 0 天 → scored_days=0
    """
    from services.scoring.scoring_service import (
        get_stock_scores_for_theme, get_theme_eval_for_report,
    )
    rid = _find_test_report_id("TEST_EV_A", "v1")
    themes = get_theme_eval_for_report(rid)
    target = next(
        (t for t in themes if (t["stocks_count"] or 0) > 0), None,
    )
    assert target is not None, "夹具应至少有 1 个含标的的题材"
    tid = target["theme_id"]

    rows = get_stock_scores_for_theme(tid)
    assert len(rows) == 3, f"应 3 只标的，得到 {len(rows)}"
    by_name = {r["stock_name"]: r for r in rows}

    # TS_A：3 天打分
    a = by_name["TS_A"]
    assert a["scored_days"] == 3
    assert abs(a["d1_pct"] - 5.0) < 0.01
    assert a["d1_hit"] == 1
    assert a["d2_pct"] is None and a["d2_hit"] is None
    assert abs(a["d3_pct"] - 2.0) < 0.01
    assert a["d3_hit"] == 0
    assert abs(a["d5_pct"] - 8.0) < 0.01
    assert a["d5_hit"] == 1

    # TS_B：2 天打分
    b = by_name["TS_B"]
    assert b["scored_days"] == 2
    assert abs(b["d1_pct"] - (-1.0)) < 0.01
    assert b["d1_hit"] == 0

    # TS_C：0 天打分（退化态）
    c = by_name["TS_C"]
    assert c["scored_days"] == 0
    for k in ("d1_pct", "d2_pct", "d3_pct", "d4_pct", "d5_pct",
              "d1_hit", "d2_hit", "d3_hit", "d4_hit", "d5_hit"):
        assert c[k] is None, (
            f"TS_C 全未打 {k} 应 None，得到 {c[k]}"
        )


def case_20_stocks_in_theme_empty() -> None:
    """get_stock_scores_for_theme：题材无标的 → 空列表。

    R1 的 T2 题材没有植入 theme_stocks → 应返回 []，且不抛错。
    """
    from services.scoring.scoring_service import (
        get_stock_scores_for_theme, get_theme_eval_for_report,
    )
    rid = _find_test_report_id("TEST_EV_A", "v1")
    themes = get_theme_eval_for_report(rid)
    target = next(
        (t for t in themes if (t["stocks_count"] or 0) == 0), None,
    )
    assert target is not None, "夹具应至少有 1 个无标的的题材"
    rows = get_stock_scores_for_theme(target["theme_id"])
    assert rows == [], f"无标的题材应返回空 [], 得到 {rows}"

    # 不存在的 theme_id 也应不抛错
    rows2 = get_stock_scores_for_theme(99999999)
    assert rows2 == [], f"不存在的 theme_id 应返回空，得到 {rows2}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-05-28 v2 加权聚合 + sector_pct 字段切换专项用例
# ---------------------------------------------------------------------------


_PREFIX_W = "TEST_W_"


def _seed_weighted_dataset() -> None:
    """构造 v2 加权聚合验证夹具（独立 prefix TEST_W_，与主夹具隔离）。

    场景：1 份报告下 4 个题材：
        T1: strength=+80, d1=10    （强看多）
        T2: strength=+40, d1=2     （弱看多）
        T3: strength=-50, d1=-3    （看空，应被报告级忽略）
        T4: strength= 0,  d1=5     （中性，应被报告级忽略）

    期望（仅看多题材按 |strength| 加权）：
        report.d1_avg = (10*80 + 2*40) / (80+40) = 880/120 = 7.333...
        老的等权 AVG 是 (10+2-3+5)/4 = 3.5，区分度足够大不易误判。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    now_iso = datetime.now().isoformat(timespec="seconds")
    today = datetime.now()
    rdate = (today - timedelta(days=2)).strftime("%Y%m%d")
    sd = (today - timedelta(days=1)).strftime("%Y%m%d")
    file_path = f"data/AI_analysis/{_PREFIX_W}r1.md"

    ai = get_ai_inference_db()
    with ai.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rdate, file_path, "mock", "mock-model",
                "analysis", _PREFIX_W + "P", "v1", 5, 1, 0, now_iso,
            ),
        )
        report_id = int(cur.lastrowid)

        themes = [
            # (theme_name, strength_score, d1_value)
            (_PREFIX_W + "T_strong", 80, 10.0),
            (_PREFIX_W + "T_weak", 40, 2.0),
            (_PREFIX_W + "T_short", -50, -3.0),
            (_PREFIX_W + "T_neutral", 0, 5.0),
        ]
        for tname, ss, d1v in themes:
            tcur = conn.execute(
                "INSERT INTO theme_predictions "
                "(report_id, report_date, report_path, "
                " theme_name, strength_score, strength_level, reason, "
                " prompt_id, prompt_version, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{_PREFIX_W}rep_{report_id}",
                    rdate, file_path, tname, ss,
                    "测试", "伪原因",
                    _PREFIX_W + "P", "v1", 0, now_iso,
                ),
            )
            tid = int(tcur.lastrowid)

            conn.execute(
                "INSERT INTO theme_prediction_scores "
                "(theme_id, prompt_id, prompt_version, "
                " report_date, score_date, days_offset, "
                " sector_pct, stock_avg_pct, stock_weighted_pct, "
                " theme_pct, "
                " hit_count, total_count, hit_rate, "
                " benchmark_pct, benchmark_zz1000_pct, "
                " direction_correct, "
                " created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid, _PREFIX_W + "P", "v1",
                    rdate, sd, 1,
                    d1v,
                    77.0,
                    99.0,
                    d1v,
                    1, 1, 1.0,
                    0.0, 0.0, 1,
                    now_iso,
                ),
            )


def _cleanup_weighted() -> None:
    """清掉 TEST_W_* 夹具。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    ai = get_ai_inference_db()
    with ai.connect() as conn:
        conn.execute(
            "DELETE FROM theme_prediction_scores "
            "WHERE prompt_id LIKE 'TEST_W_%'"
        )
        conn.execute(
            "DELETE FROM theme_predictions "
            "WHERE prompt_id LIKE 'TEST_W_%'"
        )
        conn.execute(
            "DELETE FROM ai_reports WHERE prompt_id LIKE 'TEST_W_%'"
        )


def case_21_theme_eval_uses_sector_pct() -> None:
    """题材级 D+1 应来自 sector_pct（v2 改造），不再读 stock_weighted_pct。

    夹具刻意把 sector_pct=10 / stock_weighted_pct=99，得到 d1=10 即说明 SQL
    切到 sector_pct 字段了。

    **2026-05-29 重设**：strength_score <= 0 的题材（T_short / T_neutral）
    被 SQL 过滤，应只剩 2 个看多题材；且按 strength DESC 排序
    （T_strong=80 在前，T_weak=40 在后）。
    """
    _cleanup_weighted()
    _seed_weighted_dataset()
    try:
        from services.storage.ai_inference_db import get_ai_inference_db
        from services.scoring.scoring_service import (
            get_theme_eval_for_report,
        )
        ai = get_ai_inference_db()
        with ai.connect(readonly=True) as conn:
            ar = conn.execute(
                "SELECT id FROM ai_reports WHERE prompt_id = ? LIMIT 1",
                (_PREFIX_W + "P",),
            ).fetchone()
        assert ar is not None
        themes = get_theme_eval_for_report(int(ar["id"]))
        # 2026-05-29 重设：strength<=0 过滤后只剩 2 个看多题材
        assert len(themes) == 2, (
            f"strength>0 过滤后应只剩 2 个看多题材，得到 {len(themes)}"
        )
        # 排序：strength DESC → T_strong (80) 在前，T_weak (40) 在后
        assert themes[0]["strength_score"] == 80, (
            f"第 1 个应是 strength=80 的 T_strong，得到 {themes[0]}"
        )
        assert themes[1]["strength_score"] == 40, (
            f"第 2 个应是 strength=40 的 T_weak，得到 {themes[1]}"
        )
        # 验证没有 strength<=0 的题材混入
        for t in themes:
            assert t["strength_score"] > 0, (
                f"strength<=0 题材不应出现：{t}"
            )
        for t in themes:
            d1 = t.get("d1")
            sw = t.get("sector_pct_avg")  # 也是 sector_pct 算出来
            assert d1 is not None, f"题材 {t['theme_name']} d1 不应为 None"
            assert d1 != 99.0, (
                f"题材 d1={d1}，应来自 sector_pct（不是 stock_weighted_pct=99）"
            )
            # sector_pct_avg 也应等于 d1（单日单 score）
            assert abs((sw or 0) - d1) < 1e-6, (
                f"sector_pct_avg={sw} 应与 d1={d1} 一致"
            )
    finally:
        _cleanup_weighted()


def case_22_report_eval_weighted_aggregation() -> None:
    """报告级 d1_avg 应按 |strength_score| 加权（仅看多 strength>0 参与）。

    2026-05-28 17:15 加权改造后：
        d1 来自 theme_pct（夹具特意把 theme_pct=d1v 与 sector_pct=d1v 一致），
        故期望仍为 (10*80 + 2*40) / (80+40) = 7.333...

    防回归点：夹具把 stock_avg_pct=77 / stock_weighted_pct=99 故意伪造大数；
    若 SQL 误读这两个字段中的任何一个，结果都不会等于 7.333。
    """
    _cleanup_weighted()
    _seed_weighted_dataset()
    try:
        from services.scoring.scoring_service import get_report_eval
        rows = get_report_eval(
            days=10,
            prompt_id=_PREFIX_W + "P",
            time_dim="report_date",
        )
        assert len(rows) == 1, f"应有 1 份报告，得到 {len(rows)}"
        d1 = rows[0]["d1_avg"]
        expected = (10 * 80 + 2 * 40) / (80 + 40)  # 7.333
        assert d1 is not None, "d1_avg 不应为 None（有看多题材）"
        assert abs(d1 - expected) < 0.001, (
            f"加权 d1={d1} 期望 {expected}，看空/中性应被排除"
        )
        # themes_count 应包括全部 4 个（含看空和中性，这是题材计数）
        assert rows[0]["themes_count"] == 4, (
            f"themes_count={rows[0]['themes_count']} 期望 4"
        )
    finally:
        _cleanup_weighted()


def case_23_template_eval_weighted_aggregation() -> None:
    """模板级 d1_avg 也应按 |strength_score| 加权。"""
    _cleanup_weighted()
    _seed_weighted_dataset()
    try:
        from services.scoring.scoring_service import get_template_eval
        rows = get_template_eval(days=10, time_dim="report_date")
        target = [
            r for r in rows
            if r.get("prompt_id") == _PREFIX_W + "P"
        ]
        assert len(target) == 1, f"应有 1 个模板行，得到 {len(target)}"
        d1 = target[0]["d1_avg"]
        expected = (10 * 80 + 2 * 40) / (80 + 40)
        assert d1 is not None
        assert abs(d1 - expected) < 0.001, (
            f"模板级加权 d1={d1} 期望 {expected}"
        )
    finally:
        _cleanup_weighted()


def case_24_report_eval_all_short_returns_null() -> None:
    """全部题材 strength≤0 时报告级 d_n 应为 NULL（NULLIF 兜底）。"""
    _cleanup_weighted()
    from services.storage.ai_inference_db import get_ai_inference_db
    now_iso = datetime.now().isoformat(timespec="seconds")
    today = datetime.now()
    rdate = (today - timedelta(days=2)).strftime("%Y%m%d")
    sd = (today - timedelta(days=1)).strftime("%Y%m%d")
    file_path = f"data/AI_analysis/{_PREFIX_W}r_short.md"

    ai = get_ai_inference_db()
    try:
        with ai.connect() as conn:
            cur = conn.execute(
                "INSERT INTO ai_reports "
                "(report_date, file_path, provider, model, "
                " prompt_category, prompt_id, prompt_version, "
                " news_count, theme_extracted, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (rdate, file_path, "mock", "mock-model", "analysis",
                 _PREFIX_W + "S", "v1", 5, 1, 0, now_iso),
            )
            report_id = int(cur.lastrowid)
            for ss in (-30, -50, 0):
                tcur = conn.execute(
                    "INSERT INTO theme_predictions "
                    "(report_id, report_date, report_path, "
                    " theme_name, strength_score, strength_level, reason, "
                    " prompt_id, prompt_version, is_backtest, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{_PREFIX_W}rep_short_{report_id}",
                        rdate, file_path, _PREFIX_W + f"T_{ss}", ss,
                        "测试看空", "伪原因",
                        _PREFIX_W + "S", "v1", 0, now_iso,
                    ),
                )
                tid = int(tcur.lastrowid)
                conn.execute(
                    "INSERT INTO theme_prediction_scores "
                    "(theme_id, prompt_id, prompt_version, "
                    " report_date, score_date, days_offset, "
                    " sector_pct, stock_avg_pct, stock_weighted_pct, "
                    " theme_pct, "
                    " hit_count, total_count, hit_rate, "
                    " benchmark_pct, benchmark_zz1000_pct, "
                    " direction_correct, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tid, _PREFIX_W + "S", "v1",
                        rdate, sd, 1,
                        -2.0, -2.0, -2.0, -2.0,
                        0, 1, 0.0,
                        0.0, -1.0, 1,
                        now_iso,
                    ),
                )

        from services.scoring.scoring_service import get_report_eval
        rows = get_report_eval(
            days=10,
            prompt_id=_PREFIX_W + "S",
            time_dim="report_date",
        )
        assert len(rows) == 1
        assert rows[0]["d1_avg"] is None, (
            f"全看空报告 d1_avg 应为 NULL，得到 {rows[0]['d1_avg']}"
        )
        # themes_count 仍然应为 3（题材数计数和加权聚合分别独立）
        assert rows[0]["themes_count"] == 3
    finally:
        _cleanup_weighted()


# ---------------------------------------------------------------------------
# 2026-05-29 命中率与方向算法重设·聚合层专项用例
# ---------------------------------------------------------------------------


def _seed_hit_dir_dataset() -> None:
    """专门为 case_25~28 构造的小夹具（前缀 TEST_HD_）。

    构造：
        - 1 份回测报告 prompt_id=TEST_HD_P / version=v1
        - 1 个看多题材 strength=+60，配 2 只标的 + 3 天打分行 + 6 条标的明细
          * D+1: stock1 hit=1, stock2 hit=0；theme_pct=+1.0%
          * D+2: stock1 hit=1, stock2 hit=1；theme_pct=+0.5%
          * D+3: stock1 hit=NULL（pct_chg 缺失）, stock2 hit=0；theme_pct=-2.0%
          * direction_correct（取 D+3）= 累计 1.01 * 1.005 * 0.98 ≈ 0.9949 < 1 → 0
        - 1 个看空题材 strength=-30（用于验证利空过滤）
        - 1 个中性题材 strength=0（用于验证利空过滤）

    期望聚合：
        - 标的 stock1：命中率 = 2/2 = 1.0（有效天=2，命中=2，D+3 NULL 不计）
        - 标的 stock2：命中率 = 1/3 ≈ 0.333
        - 题材命中率 = (1.0 + 0.333) / 2 = 0.667
        - 题材方向（取 D+3）= 0（累计 < 1）
        - 报告命中率 = 0.667（只看 strength>0 题材）
        - 报告方向 = 0/1 = 0
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    now_iso = datetime.now().isoformat(timespec="seconds")
    today = datetime.now()
    rdate = (today - timedelta(days=5)).strftime("%Y%m%d")
    file_path = "data/AI_analysis/TEST_HD_r1.md"

    ai = get_ai_inference_db()
    with ai.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rdate, file_path, "mock", "mock-model",
                "analysis", "TEST_HD_P", "v1", 3, 1, 1, now_iso,
            ),
        )
        report_id = int(cur.lastrowid)

        # 3 个题材
        themes_meta = [
            ("TEST_HD_T_long", 60),
            ("TEST_HD_T_short", -30),
            ("TEST_HD_T_neutral", 0),
        ]
        theme_ids: Dict[str, int] = {}
        for tname, ss in themes_meta:
            tcur = conn.execute(
                "INSERT INTO theme_predictions "
                "(report_id, report_date, report_path, theme_name, "
                " strength_score, strength_level, reason, prompt_id, "
                " prompt_version, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"TEST_HD_rep_{report_id}",
                    rdate, file_path, tname, ss,
                    "测试", "测试 reason", "TEST_HD_P", "v1", 1, now_iso,
                ),
            )
            theme_ids[tname] = int(tcur.lastrowid)

        long_tid = theme_ids["TEST_HD_T_long"]

        # 看多题材绑 2 只标的
        stock_codes = [("S1_LONG.SH",), ("S2_LONG.SH",)]
        stock_ids: List[int] = []
        for (code,) in stock_codes:
            scur = conn.execute(
                "INSERT INTO theme_stocks "
                "(theme_id, stock_name, stock_code, normalized_code) "
                "VALUES (?, ?, ?, ?)",
                (long_tid, f"个股{code}", code, code),
            )
            stock_ids.append(int(scur.lastrowid))

        # 看多题材 3 天打分行
        sd_d1 = (today - timedelta(days=4)).strftime("%Y%m%d")
        sd_d2 = (today - timedelta(days=3)).strftime("%Y%m%d")
        sd_d3 = (today - timedelta(days=2)).strftime("%Y%m%d")
        score_pacts = [
            # (score_date, days_offset, theme_pct, direction_correct)
            (sd_d1, 1, 1.0, 1),    # 累计 1.01 > 1 → 1
            (sd_d2, 2, 0.5, 1),    # 累计 1.01*1.005 ≈ 1.015 > 1 → 1
            (sd_d3, 3, -2.0, 0),   # 累计 1.015 * 0.98 ≈ 0.9947 < 1 → 0
        ]
        for sd, off, tpct, dc in score_pacts:
            conn.execute(
                "INSERT INTO theme_prediction_scores "
                "(theme_id, prompt_id, prompt_version, report_date, "
                " score_date, days_offset, sector_pct, stock_avg_pct, "
                " stock_weighted_pct, theme_pct, hit_count, total_count, "
                " hit_rate, benchmark_pct, benchmark_zz1000_pct, "
                " direction_correct, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    long_tid, "TEST_HD_P", "v1", rdate, sd, off,
                    tpct, tpct, tpct, tpct,
                    1, 2, 0.5, 0.0, 0.5,
                    dc, now_iso,
                ),
            )

        # 标的明细 6 行：D+1 / D+2 / D+3 × 2 标的
        # 主人设定：
        #   stock1（S1_LONG）：D+1 hit=1, D+2 hit=1, D+3 hit=NULL（缺数据）
        #   stock2（S2_LONG）：D+1 hit=0, D+2 hit=1, D+3 hit=0
        # → stock1 累计命中率 = 2/2 = 1.0；stock2 累计命中率 = 1/3
        # → 题材命中率 = (1.0 + 0.333) / 2 ≈ 0.667
        details = [
            # (theme_stock_id, normalized_code, score_date, off, pct, is_hit)
            (stock_ids[0], "S1_LONG.SH", sd_d1, 1, 5.0, 1),
            (stock_ids[0], "S1_LONG.SH", sd_d2, 2, 4.0, 1),
            (stock_ids[0], "S1_LONG.SH", sd_d3, 3, None, None),
            (stock_ids[1], "S2_LONG.SH", sd_d1, 1, 0.5, 0),
            (stock_ids[1], "S2_LONG.SH", sd_d2, 2, 6.0, 1),
            (stock_ids[1], "S2_LONG.SH", sd_d3, 3, -1.0, 0),
        ]
        for ts_id, code, sd, off, pct, hit in details:
            conn.execute(
                "INSERT INTO theme_stock_scores "
                "(theme_stock_id, theme_id, normalized_code, "
                " report_date, score_date, days_offset, pct_chg, is_hit, "
                " created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_id, long_tid, code, rdate, sd, off, pct, hit, now_iso),
            )


def _cleanup_hit_dir() -> None:
    from services.storage.ai_inference_db import get_ai_inference_db
    ai = get_ai_inference_db()
    with ai.connect() as conn:
        conn.execute(
            "DELETE FROM theme_stock_scores "
            "WHERE theme_id IN ("
            "  SELECT id FROM theme_predictions "
            "  WHERE prompt_id LIKE 'TEST_HD_%')"
        )
        conn.execute(
            "DELETE FROM theme_prediction_scores "
            "WHERE prompt_id LIKE 'TEST_HD_%'"
        )
        conn.execute(
            "DELETE FROM theme_stocks "
            "WHERE theme_id IN ("
            "  SELECT id FROM theme_predictions "
            "  WHERE prompt_id LIKE 'TEST_HD_%')"
        )
        conn.execute(
            "DELETE FROM theme_predictions "
            "WHERE prompt_id LIKE 'TEST_HD_%'"
        )
        conn.execute(
            "DELETE FROM ai_reports WHERE prompt_id LIKE 'TEST_HD_%'"
        )


def case_25_theme_filter_skips_short_neutral() -> None:
    """**2026-05-29 重设** get_theme_eval_for_report 过滤 strength<=0 题材。

    夹具有 3 题材（+60 / -30 / 0），新 SQL 应只返回 1 个看多题材。
    """
    _cleanup_hit_dir()
    _seed_hit_dir_dataset()
    try:
        from services.storage.ai_inference_db import get_ai_inference_db
        from services.scoring.scoring_service import (
            get_theme_eval_for_report,
        )
        ai = get_ai_inference_db()
        with ai.connect(readonly=True) as conn:
            ar = conn.execute(
                "SELECT id FROM ai_reports WHERE prompt_id = ?",
                ("TEST_HD_P",),
            ).fetchone()
        assert ar is not None
        themes = get_theme_eval_for_report(int(ar["id"]))
        assert len(themes) == 1, (
            f"strength>0 过滤后应剩 1 题材，得到 {len(themes)}"
        )
        assert themes[0]["strength_score"] == 60
        assert themes[0]["theme_name"] == "TEST_HD_T_long"
    finally:
        _cleanup_hit_dir()


def case_26_theme_hit_and_direction_cumulative() -> None:
    """**2026-05-29 重设** 题材级 hit_rate_avg + direction_correct_rate 新口径。

    期望（基于夹具）：
        - hit_rate_avg = (1.0 + 1/3) / 2 ≈ 0.6667
          （stock1=2/2=1.0、stock2=1/3 ≈ 0.333）
        - direction_correct_rate = 0（D+3 行 direction_correct=0）
    """
    _cleanup_hit_dir()
    _seed_hit_dir_dataset()
    try:
        from services.storage.ai_inference_db import get_ai_inference_db
        from services.scoring.scoring_service import (
            get_theme_eval_for_report,
        )
        ai = get_ai_inference_db()
        with ai.connect(readonly=True) as conn:
            ar = conn.execute(
                "SELECT id FROM ai_reports WHERE prompt_id = ?",
                ("TEST_HD_P",),
            ).fetchone()
        assert ar is not None
        themes = get_theme_eval_for_report(int(ar["id"]))
        assert len(themes) == 1
        t = themes[0]
        hr = t.get("hit_rate_avg")
        dr = t.get("direction_correct_rate")
        expected_hr = (1.0 + 1.0 / 3) / 2  # ≈ 0.6667
        assert hr is not None and abs(hr - expected_hr) < 1e-6, (
            f"题材命中率期望 {expected_hr}，得到 {hr}"
        )
        assert dr == 0, (
            f"题材方向（取 D+3 行 direction_correct=0）期望 0，得到 {dr}"
        )
    finally:
        _cleanup_hit_dir()


def case_27_report_eval_hit_dir_new_semantics() -> None:
    """**2026-05-29 重设** get_report_eval.hit_rate_avg / direction_correct_rate 新口径。

    只有 1 个看多题材打分 → 报告命中率 = 题材命中率 ≈ 0.667；
    报告方向 = 题材方向 = 0。
    """
    _cleanup_hit_dir()
    _seed_hit_dir_dataset()
    try:
        from services.scoring.scoring_service import get_report_eval
        rows = get_report_eval(days=30, prompt_id="TEST_HD_P")
        assert len(rows) == 1, f"应找到 1 份报告，得到 {len(rows)}"
        r = rows[0]
        hr = r.get("hit_rate_avg")
        dr = r.get("direction_correct_rate")
        expected_hr = (1.0 + 1.0 / 3) / 2
        assert hr is not None and abs(hr - expected_hr) < 1e-6, (
            f"报告命中率期望 {expected_hr}，得到 {hr}"
        )
        assert dr is not None and abs(float(dr) - 0.0) < 1e-9, (
            f"报告方向期望 0（看多题材方向=0），得到 {dr}"
        )
    finally:
        _cleanup_hit_dir()


def case_28_stock_eval_returns_stock_hit_rate() -> None:
    """**2026-05-29 重设** get_stock_scores_for_theme 新增 stock_hit_rate / valid_days。

    期望：
        - S1_LONG: valid_days=2, stock_hit_rate=1.0
        - S2_LONG: valid_days=3, stock_hit_rate=1/3 ≈ 0.333
    """
    _cleanup_hit_dir()
    _seed_hit_dir_dataset()
    try:
        from services.storage.ai_inference_db import get_ai_inference_db
        from services.scoring.scoring_service import (
            get_stock_scores_for_theme,
        )
        ai = get_ai_inference_db()
        with ai.connect(readonly=True) as conn:
            tp = conn.execute(
                "SELECT id FROM theme_predictions "
                "WHERE theme_name = ?",
                ("TEST_HD_T_long",),
            ).fetchone()
        assert tp is not None
        stocks = get_stock_scores_for_theme(int(tp["id"]))
        assert len(stocks) == 2, f"应有 2 只标的，得到 {len(stocks)}"
        by_code = {s["normalized_code"]: s for s in stocks}
        s1 = by_code["S1_LONG.SH"]
        s2 = by_code["S2_LONG.SH"]
        assert s1.get("valid_days") == 2, (
            f"S1 有效天数应 2，得到 {s1.get('valid_days')}"
        )
        assert (
            s1.get("stock_hit_rate") is not None
            and abs(s1["stock_hit_rate"] - 1.0) < 1e-9
        ), f"S1 命中率应 1.0，得到 {s1.get('stock_hit_rate')}"
        assert s2.get("valid_days") == 3, (
            f"S2 有效天数应 3，得到 {s2.get('valid_days')}"
        )
        assert (
            s2.get("stock_hit_rate") is not None
            and abs(s2["stock_hit_rate"] - 1.0 / 3) < 1e-6
        ), f"S2 命中率应 1/3，得到 {s2.get('stock_hit_rate')}"
    finally:
        _cleanup_hit_dir()


_CASES: List[Tuple[str, Callable[[], None]]] = [
    ("01_template_default", case_01_template_default),
    ("02_template_backtest_only", case_02_template_backtest_only),
    ("03_template_real_only", case_03_template_real_only),
    ("04_template_time_dim", case_04_template_time_dim),
    ("05_template_keep_version", case_05_template_keep_version),
    ("06_report_default", case_06_report_default),
    ("07_report_filter_prompt", case_07_report_filter_prompt),
    ("08_report_backtest_only", case_08_report_backtest_only),
    ("09_report_time_dim_switch", case_09_report_time_dim_switch),
    ("11_unscored_reports_appear", case_11_unscored_reports_appear),
    ("12_rescore_one_report_no_themes",
     case_12_rescore_one_report_no_themes),
    ("13_rescore_one_report_not_found",
     case_13_rescore_one_report_not_found),
    ("14_delete_backtest_cascade", case_14_delete_backtest_cascade),
    ("15_delete_real_protected", case_15_delete_real_protected),
    ("16_delete_dry_run_no_change", case_16_delete_dry_run_no_change),
    ("10_invalid_time_dim", case_10_invalid_time_dim),
    # 树形展开新增（2026-05-28）
    ("17_theme_in_report_basic", case_17_theme_in_report_basic),
    ("18_theme_in_report_no_score", case_18_theme_in_report_no_score),
    ("19_stocks_in_theme_basic", case_19_stocks_in_theme_basic),
    ("20_stocks_in_theme_empty", case_20_stocks_in_theme_empty),
    # 2026-05-28 v2 加权聚合 + sector_pct 切换
    ("21_theme_eval_uses_sector_pct", case_21_theme_eval_uses_sector_pct),
    ("22_report_eval_weighted_aggregation",
     case_22_report_eval_weighted_aggregation),
    ("23_template_eval_weighted_aggregation",
     case_23_template_eval_weighted_aggregation),
    ("24_report_eval_all_short_returns_null",
     case_24_report_eval_all_short_returns_null),
    # 2026-05-29 命中率与方向算法重设·聚合层
    ("25_theme_filter_skips_short_neutral",
     case_25_theme_filter_skips_short_neutral),
    ("26_theme_hit_and_direction_cumulative",
     case_26_theme_hit_and_direction_cumulative),
    ("27_report_eval_hit_dir_new_semantics",
     case_27_report_eval_hit_dir_new_semantics),
    ("28_stock_eval_returns_stock_hit_rate",
     case_28_stock_eval_returns_stock_hit_rate),
]


def main() -> int:
    print(f"=== scoring_service 评估接口回归 ({len(_CASES)} 用例) ===\n")
    print("[prep] 清理历史 TEST_EV_* 数据 + 重新植入夹具 ...")
    _cleanup()
    _seed_dataset()
    print(
        "[prep] 夹具就绪：4 份报告 / 6 个题材 / 18 个打分行 + "
        "T1 配 3 只 theme_stocks（5 条标的级打分）\n"
    )

    pass_n = fail_n = 0
    try:
        for name, fn in _CASES:
            try:
                fn()
                print(f"  [PASS] {name}")
                pass_n += 1
            except AssertionError as exc:
                print(f"  [FAIL] {name}: {exc}")
                fail_n += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  [ERROR] {name}: {type(exc).__name__}: {exc}")
                import traceback
                traceback.print_exc()
                fail_n += 1
    finally:
        _cleanup()
        print("\n[cleanup] 已删除所有 TEST_EV_* 伪数据")

    print(f"\n[汇总] {pass_n} PASS / {fail_n} FAIL / {len(_CASES)} 用例")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
