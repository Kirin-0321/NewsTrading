"""模板/报告评估查询回归测试（16 用例，纯本地 SQLite，0 LLM 0 网络）。

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
from typing import Callable, List, Tuple

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
    """YYYY-MM-DD 10 字符（用于 ar/tp/tps 的 report_date 字段，
    对齐 :mod:`services.scoring.script_scorer` 实际写入格式）。"""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _cleanup() -> None:
    """删 ai_reports / theme_predictions / theme_prediction_scores 的全部伪数据。

    含 case_14 残留的 ``TEST_EV_DEL_BT_x.md`` 等物理伪 md 文件。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.database import get_project_root
    ai = get_ai_inference_db()
    with ai.connect() as conn:
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
            rdate_dash = base_dt.strftime("%Y-%m-%d")
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
                for d_off in (1, 3, 5):
                    sd = (
                        base_dt + timedelta(days=d_off)
                    ).strftime("%Y%m%d")
                    conn.execute(
                        "INSERT INTO theme_prediction_scores "
                        "(theme_id, prompt_id, prompt_version, "
                        " report_date, score_date, days_offset, "
                        " sector_pct, stock_avg_pct, stock_weighted_pct, "
                        " hit_count, total_count, hit_rate, "
                        " benchmark_pct, alpha, direction_correct, "
                        " created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "        ?, ?, ?, ?, ?)",
                        (
                            tid, pid, pv,
                            rdate_dash, sd, d_off,
                            2.0, 1.5, 2.0,
                            3, 5, 0.6,
                            1.0, 1.0, 1,
                            now_iso,
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
                    " hit_count, total_count, hit_rate, "
                    " benchmark_pct, alpha, direction_correct, "
                    " created_at) "
                    "VALUES (?, 'TEST_EV_DEL_BT', 'v1', "
                    "        ?, ?, ?, 1.0, 1.0, 1.0, 1, 1, 1.0, "
                    "        0.5, 0.5, 1, ?)",
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
# Runner
# ---------------------------------------------------------------------------


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
]


def main() -> int:
    print(f"=== scoring_service 评估接口回归 ({len(_CASES)} 用例) ===\n")
    print("[prep] 清理历史 TEST_EV_* 数据 + 重新植入夹具 ...")
    _cleanup()
    _seed_dataset()
    print("[prep] 夹具就绪：4 份报告 / 6 个题材 / 18 个打分行\n")

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
