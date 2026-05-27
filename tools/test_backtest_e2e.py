"""Phase 6 Step 6.3 · 虚拟回测端到端回归测试（mock LLM 完全离线）。

.. warning::

    本测试文件是基于 2026-05-27 18:30 **旧链路**（事后 rename + UPDATE
    report_date）写的。新链路下：

    * 回测产物文件名从一开始就用模拟交易日 ``5月20日_0时00分_..._backtest.md``
    * ``ai_reports.report_date`` / ``theme_predictions.report_date`` 统一 YYYYMMDD
    * 不再有 ``_rename_to_backtest`` 这条物理重命名链路
    * ``_run_analyze_and_mark_backtest`` 退化为薄壳，无事后 UPDATE

    本文件的多数断言（``_backtest_{trade_date}`` 后缀、``target_report_date``
    用 dash 等）已与新链路不一致，**需要主人择期重写**。

    本次提交只做最小化适配：把 ``_seed_fake_report`` 入参从 YYYY-MM-DD 改为
    YYYYMMDD，避免被 ``_ensure_yyyymmdd`` 入口防御抛出 ValueError。

运行
----
::

    python tools/test_backtest_e2e.py
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Tuple
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 测试常量 & 数据隔离辅助
# ---------------------------------------------------------------------------

# 用过去日期避开穿越保护（5/20 已过）
_PAST_DATE = "20260520"
_PAST_DATE_2 = "20260519"
_FUTURE_DATE = "99991231"  # 故意非法用于穿越保护

# 伪数据前缀（cleanup 用）
_PREFIX = "TEST_BT_"

_AI_OUT_DIR = _ROOT / "data" / "AI_analysis" / "2026" / "5月20日"


def _ensure_out_dir() -> Path:
    _AI_OUT_DIR.mkdir(parents=True, exist_ok=True)
    return _AI_OUT_DIR


def _fake_md_path(template_id: str, date: str) -> str:
    """生成伪 md 路径（**不带** _backtest 后缀，模拟刚 analyze 完）。"""
    out_dir = _ensure_out_dir()
    p = out_dir / f"{_PREFIX}{template_id}_{date}_总结分析报告.md"
    p.write_text(
        f"# 伪报告\n模板={template_id} 日期={date}\n", encoding="utf-8"
    )
    return str(p)


def _seed_fake_report(
    *,
    file_path: str,
    template_id: str,
    report_date: str,
    is_backtest: int = 0,
    theme_count: int = 0,
) -> int:
    """往 ai_reports 插一条伪记录，返回 id。

    同时按 theme_count 往 theme_predictions 插对应条数。
    入库的 file_path / report_path 统一转相对 posix 与真实 store 对齐，
    否则 backtest_one 的 UPDATE WHERE 匹配不上（路径格式不一致）。
    """
    from services.storage.ai_inference_db import get_ai_inference_db
    from services.storage.ai_reports_store import _to_relative_posix
    adb = get_ai_inference_db()
    now_iso = datetime.now().isoformat(timespec="seconds")
    rel_path = _to_relative_posix(file_path)
    with adb.connect() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports "
            "(report_date, file_path, provider, model, "
            " prompt_category, prompt_id, prompt_version, "
            " news_count, theme_extracted, is_backtest, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_date, rel_path, "mock", "mock-model",
                "analysis", template_id, "v1",
                10, int(theme_count > 0), is_backtest, now_iso,
            ),
        )
        report_id = int(cur.lastrowid)
        for i in range(theme_count):
            conn.execute(
                "INSERT INTO theme_predictions "
                "(report_id, report_date, report_path, "
                " theme_name, strength_score, strength_level, reason, "
                " prompt_id, prompt_version, is_backtest, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{_PREFIX}fake_{report_id}",
                    report_date, rel_path,
                    f"{_PREFIX}主题{i}",
                    50, "中等利多", f"伪原因{i}",
                    template_id, "v1", is_backtest, now_iso,
                ),
            )
    return report_id


def _cleanup_all() -> None:
    """删 ai_reports / theme_predictions / 伪 md。"""
    from services.storage.ai_inference_db import get_ai_inference_db
    adb = get_ai_inference_db()
    with adb.connect() as conn:
        # 1. 先删 theme_predictions（外键 CASCADE 会拖走 theme_stocks/news/scores）
        conn.execute(
            "DELETE FROM theme_predictions "
            "WHERE prompt_id LIKE 'TEST_BT_%' "
            "   OR report_id LIKE 'TEST_BT_%' "
            "   OR theme_name LIKE 'TEST_BT_%'"
        )
        # 2. 删 ai_reports（按 file_path 含 TEST_BT_）
        conn.execute(
            "DELETE FROM ai_reports WHERE file_path LIKE '%TEST_BT_%'"
        )
    # 3. 删物理 md
    if _AI_OUT_DIR.exists():
        for f in _AI_OUT_DIR.glob(f"{_PREFIX}*"):
            try:
                f.unlink()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# mock 工厂
# ---------------------------------------------------------------------------


def _build_fake_snapshot(
    trade_date: str = _PAST_DATE,
    news_count: int = 50,
    market_md: str = "盘后伪 md",
):
    """构造 HistoricalSnapshot 实例（绕开真实数据库 / Tushare）。

    新接口字段：trade_date / news_start_dt / news_end_dt /
    news_start_ts / news_end_ts / news_status / ...
    """
    from services.scoring.snapshot import HistoricalSnapshot
    start_dt = datetime.strptime(trade_date, "%Y%m%d").replace(hour=14)
    end_dt = datetime.strptime(trade_date, "%Y%m%d").replace(
        day=int(trade_date[6:8]) + 1, hour=9,
    )
    return HistoricalSnapshot(
        trade_date=trade_date,
        news_start_dt=start_dt,
        news_end_dt=end_dt,
        news_start_ts=int(start_dt.timestamp()),
        news_end_ts=int(end_dt.timestamp()),
        news_status="curated",
        max_news_end_dt=end_dt,
        news=[],
        news_count=news_count,
        market_summary_date=trade_date,
        market_summary_md=market_md,
    )


@contextmanager
def _patched_backtest(
    *,
    template_id: str,
    trade_date: str,
    theme_count: int = 2,
    fail_analyze: bool = False,
):
    """patch ``build_snapshot`` + ``AnalysisService.analyze`` 双 mock。

    yield 的 dict 含 ``analyze_call_count`` 让用例断言 LLM 是否被调过。
    """
    from services.analysis_service import AnalysisResult
    counter = {"snapshot_calls": 0, "analyze_calls": 0}

    def _fake_build_snapshot(td, **_kw):
        counter["snapshot_calls"] += 1
        return _build_fake_snapshot(trade_date=td)

    def _fake_analyze(self, **kw):
        counter["analyze_calls"] += 1
        if fail_analyze:
            return AnalysisResult(ok=False, error="mock 故意失败")
        fake_md = _fake_md_path(template_id, trade_date)
        # 关键：先把"假 analyze 写出的 md"对应行塞进 ai_reports，
        # 让 _run_analyze_and_mark_backtest 的 UPDATE 有目标。
        # 真实 analyze 调用方拿得到 report_id（int 自增主键），
        # mock 也要返回这个 id 让 UPDATE 走主路径（report_id WHERE）。
        report_id = _seed_fake_report(
            file_path=fake_md,
            template_id=template_id,
            # 2026-05-27 18:30：协议归一化为 YYYYMMDD
            report_date=datetime.now().strftime("%Y%m%d"),
            is_backtest=0,
            theme_count=theme_count,
        )
        return AnalysisResult(
            ok=True,
            report_path=fake_md,
            news_count=50,
            theme_count=theme_count,
            market_summary_used=True,
            report_id=report_id,
        )

    # compute_default_news_window 也 mock 掉，避免真实查 dim_trade_calendar
    fake_start = datetime.strptime(trade_date, "%Y%m%d").replace(hour=14)
    fake_end = datetime.strptime(trade_date, "%Y%m%d").replace(
        day=int(trade_date[6:8]) + 1, hour=9,
    )

    def _fake_default_win(_td, **_kw):
        return (fake_start, fake_end)

    with patch(
        "tools.backtest_prompt.build_snapshot",
        side_effect=_fake_build_snapshot,
    ), patch(
        "tools.backtest_prompt.compute_default_news_window",
        side_effect=_fake_default_win,
    ), patch(
        "tools.backtest_prompt.TushareClient", autospec=True,
    ), patch.object(
        __import__(
            "services.analysis_service", fromlist=["AnalysisService"]
        ).AnalysisService,
        "analyze", _fake_analyze,
    ):
        yield counter


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


def case_01_single_backtest() -> None:
    """单日单模板：md 改名 + ai_reports.is_backtest=1 + report_date 透传。"""
    from tools.backtest_prompt import backtest_one
    from services.storage.ai_inference_db import get_ai_inference_db
    template = "TEST_BT_tmpl_01"
    target_report_date = (
        f"{_PAST_DATE[:4]}-{_PAST_DATE[4:6]}-{_PAST_DATE[6:8]}"
    )
    with _patched_backtest(template_id=template, trade_date=_PAST_DATE):
        res = backtest_one(template, _PAST_DATE, dry_run=False)
    assert res.ok, f"应成功，得到 error={res.error}"
    assert res.report_path is not None
    assert f"_backtest_{_PAST_DATE}" in res.report_path, (
        f"文件名应含 _backtest_{_PAST_DATE}，得到 {res.report_path}"
    )
    # 注意：修复后 res.report_path 是相对 posix
    # 验证物理文件确实在新路径（拼绝对路径）
    from services.storage.database import get_project_root
    abs_path = Path(get_project_root()) / res.report_path
    assert abs_path.exists(), f"重命名后文件应存在: {abs_path}"

    # ai_reports 应有 is_backtest=1 + report_date 等于 trade_date YYYY-MM-DD
    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT is_backtest, file_path, report_date FROM ai_reports "
            "WHERE prompt_id = ? AND report_date = ?",
            (template, target_report_date),
        ).fetchone()
    assert row is not None, (
        f"ai_reports 应有 report_date={target_report_date} 的记录"
    )
    assert row["is_backtest"] == 1, (
        f"is_backtest 应为 1，得到 {row['is_backtest']}"
    )
    assert row["file_path"] == res.report_path, (
        f"file_path 应已 UPDATE 为 {res.report_path}，"
        f"得到 {row['file_path']}"
    )


def case_02_theme_propagate() -> None:
    """题材抽取自动透传 is_backtest=1 + report_date + report_path 全改。"""
    from tools.backtest_prompt import backtest_one
    from services.storage.ai_inference_db import get_ai_inference_db
    template = "TEST_BT_tmpl_02"
    target_report_date = (
        f"{_PAST_DATE[:4]}-{_PAST_DATE[4:6]}-{_PAST_DATE[6:8]}"
    )
    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE, theme_count=3,
    ):
        res = backtest_one(template, _PAST_DATE, dry_run=False)
    assert res.ok, f"应成功，得到 error={res.error}"

    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT is_backtest, report_date, report_path "
            "FROM theme_predictions "
            "WHERE prompt_id = ? AND report_date = ?",
            (template, target_report_date),
        ).fetchall()
    assert len(rows) == 3, f"应有 3 条 theme，得到 {len(rows)}"
    for r in rows:
        assert r["is_backtest"] == 1, "theme.is_backtest 应为 1"
        assert r["report_date"] == target_report_date, (
            f"theme.report_date 应为 {target_report_date}，"
            f"得到 {r['report_date']}"
        )
        assert f"_backtest_{_PAST_DATE}" in r["report_path"], (
            "theme.report_path 应跟着改名"
        )


def case_03_two_dates() -> None:
    """两个日期 × 1 模板：两份独立样本入库 + report_date 各自正确。"""
    from tools.backtest_prompt import backtest_one
    from services.storage.ai_inference_db import get_ai_inference_db
    template = "TEST_BT_tmpl_03"

    expected_dates = [
        f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        for d in (_PAST_DATE_2, _PAST_DATE)
    ]

    # 第一个日期
    with _patched_backtest(template_id=template, trade_date=_PAST_DATE):
        r1 = backtest_one(template, _PAST_DATE, dry_run=False)
    # 第二个日期
    with _patched_backtest(template_id=template, trade_date=_PAST_DATE_2):
        r2 = backtest_one(template, _PAST_DATE_2, dry_run=False)

    assert r1.ok and r2.ok, "两次都应成功"

    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        dates = [
            r["report_date"]
            for r in conn.execute(
                "SELECT report_date FROM ai_reports "
                "WHERE prompt_id = ? AND is_backtest = 1 "
                "ORDER BY report_date",
                (template,),
            ).fetchall()
        ]
    assert dates == expected_dates, (
        f"应有两条且日期正确（YYYY-MM-DD 升序）={expected_dates}，"
        f"得到 {dates}"
    )


def case_04_future_date_blocked() -> None:
    """穿越保护：news_end_dt > now 必须 ok=False。"""
    from tools.backtest_prompt import backtest_one
    from datetime import timedelta
    # 传一个 1 小时后的右边界，左边界随便（小于右即可）
    fut_end = datetime.now() + timedelta(hours=1)
    fut_start = datetime.now() - timedelta(hours=1)
    # 用 _PAST_DATE 作为 trade_date 避免 compute_default_news_window
    # 查 99991231 失败（即便后面会被穿越保护拦下）
    with _patched_backtest(
        template_id="TEST_BT_tmpl_04", trade_date=_PAST_DATE,
    ):
        res = backtest_one(
            "TEST_BT_tmpl_04", _PAST_DATE,
            news_start_dt=fut_start, news_end_dt=fut_end,
            dry_run=False,
        )
    assert not res.ok, "未来 news_end_dt 应 ok=False"
    assert "穿越" in (res.error or ""), (
        f"error 应含'穿越'，得到 {res.error}"
    )


def case_05_skip_existing() -> None:
    """已存在 (template, date) 的 backtest → 第二次 skipped=True。"""
    from tools.backtest_prompt import backtest_one
    template = "TEST_BT_tmpl_05"
    # 第一次跑出来
    with _patched_backtest(template_id=template, trade_date=_PAST_DATE):
        r1 = backtest_one(template, _PAST_DATE, dry_run=False)
    assert r1.ok and not r1.skipped, "第一次应正常成功"
    # 第二次：patch 仍然在但 analyze 不应被调
    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE,
    ) as counter:
        r2 = backtest_one(template, _PAST_DATE, dry_run=False)
    assert r2.ok and r2.skipped, (
        f"第二次应 skipped=True，得到 ok={r2.ok} skipped={r2.skipped}"
    )
    assert counter["analyze_calls"] == 0, (
        f"已跳过则 analyze 不应被调，得到 {counter['analyze_calls']}"
    )


def case_07_overwrite_existing() -> None:
    """已存在 (template, date) 且 overwrite=True → 删旧重跑、成功。

    断言：
      * 第二次 ok=True + skipped=False（不是跳过）
      * res.overwritten=True
      * deleted_reports >= 1
      * analyze 真的被第二次调用
      * 最终 ai_reports 只有 1 条（不会重复）
    """
    from tools.backtest_prompt import backtest_one
    from services.storage.ai_inference_db import get_ai_inference_db
    template = "TEST_BT_tmpl_07"
    target_report_date = (
        f"{_PAST_DATE[:4]}-{_PAST_DATE[4:6]}-{_PAST_DATE[6:8]}"
    )

    with _patched_backtest(template_id=template, trade_date=_PAST_DATE):
        r1 = backtest_one(template, _PAST_DATE, dry_run=False)
    assert r1.ok and not r1.skipped, "第一次应成功"

    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE,
    ) as counter:
        r2 = backtest_one(
            template, _PAST_DATE, dry_run=False, overwrite=True,
        )

    assert r2.ok, f"第二次（overwrite）应 ok=True，得到 error={r2.error}"
    assert not r2.skipped, "overwrite=True 不应 skipped"
    assert r2.overwritten, "overwritten 应为 True"
    assert r2.deleted_reports >= 1, (
        f"应删 ai_reports >= 1，得到 {r2.deleted_reports}"
    )
    assert counter["analyze_calls"] == 1, (
        f"overwrite 后 analyze 应被调 1 次，"
        f"得到 {counter['analyze_calls']}"
    )

    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM ai_reports "
            "WHERE prompt_id = ? AND report_date = ? "
            "  AND is_backtest = 1",
            (template, target_report_date),
        ).fetchone()[0]
    assert n == 1, f"最终 ai_reports 应只剩 1 条，得到 {n}"


def case_08_overwrite_cascade_theme_predictions() -> None:
    """overwrite 时 CASCADE 应该把旧 theme_predictions 也清掉。

    断言：第一次 3 条题材 → overwrite 后 db 依然只剩 3 条
    （新写的，不会变成 6 条）。
    """
    from tools.backtest_prompt import backtest_one
    from services.storage.ai_inference_db import get_ai_inference_db
    template = "TEST_BT_tmpl_08"
    target_report_date = (
        f"{_PAST_DATE[:4]}-{_PAST_DATE[4:6]}-{_PAST_DATE[6:8]}"
    )

    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE, theme_count=3,
    ):
        r1 = backtest_one(template, _PAST_DATE, dry_run=False)
    assert r1.ok, f"第一次应成功，得到 error={r1.error}"

    adb = get_ai_inference_db()
    with adb.connect(readonly=True) as conn:
        n1 = conn.execute(
            "SELECT COUNT(*) FROM theme_predictions "
            "WHERE prompt_id = ? AND report_date = ?",
            (template, target_report_date),
        ).fetchone()[0]
    assert n1 == 3, f"第一次后 theme_predictions 应 3 条，得到 {n1}"

    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE, theme_count=3,
    ):
        r2 = backtest_one(
            template, _PAST_DATE, dry_run=False, overwrite=True,
        )
    assert r2.ok and r2.overwritten, (
        f"第二次 overwrite 应成功，得到 ok={r2.ok} "
        f"overwritten={r2.overwritten}"
    )
    assert r2.deleted_themes == 3, (
        f"应删 theme_predictions=3，得到 {r2.deleted_themes}"
    )

    with adb.connect(readonly=True) as conn:
        n2 = conn.execute(
            "SELECT COUNT(*) FROM theme_predictions "
            "WHERE prompt_id = ? AND report_date = ?",
            (template, target_report_date),
        ).fetchone()[0]
    assert n2 == 3, (
        f"overwrite 后 theme_predictions 仍应 3 条（不重复），"
        f"得到 {n2}"
    )


def case_06_dry_run() -> None:
    """dry-run：返回 ok=True + report_path=None + LLM 不调。"""
    from tools.backtest_prompt import backtest_one
    template = "TEST_BT_tmpl_06"
    with _patched_backtest(
        template_id=template, trade_date=_PAST_DATE,
    ) as counter:
        res = backtest_one(template, _PAST_DATE, dry_run=True)
    assert res.ok, f"dry-run 应成功，得到 error={res.error}"
    assert res.report_path is None, "dry-run 不应有 report_path"
    assert res.snapshot_news_count == 50, "应有 snapshot 计数"
    assert counter["analyze_calls"] == 0, "dry-run 不应调 analyze"
    assert counter["snapshot_calls"] == 1, (
        f"应调一次 snapshot，得到 {counter['snapshot_calls']}"
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run(
    cases: List[Tuple[str, Callable[[], None]]],
) -> Tuple[int, int]:
    pass_n = fail_n = 0
    for name, fn in cases:
        t0 = time.perf_counter()
        try:
            fn()
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"  [PASS] {name} ({ms} ms)")
            pass_n += 1
        except AssertionError as exc:
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"  [FAIL] {name} ({ms} ms): {exc}")
            fail_n += 1
        except Exception as exc:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            print(
                f"  [ERROR] {name} ({ms} ms): "
                f"{type(exc).__name__}: {exc}"
            )
            fail_n += 1
    return pass_n, fail_n


def main() -> int:
    print("=" * 64)
    print("Phase 6 Step 6.3 · backtest_prompt e2e 回归测试")
    print("=" * 64)

    print("\n[prep] 清理历史伪数据 ...")
    _cleanup_all()

    cases: List[Tuple[str, Callable[[], None]]] = [
        ("case_01_single_backtest", case_01_single_backtest),
        ("case_02_theme_propagate", case_02_theme_propagate),
        ("case_03_two_dates", case_03_two_dates),
        ("case_04_future_date_blocked", case_04_future_date_blocked),
        ("case_05_skip_existing", case_05_skip_existing),
        ("case_06_dry_run", case_06_dry_run),
        ("case_07_overwrite_existing", case_07_overwrite_existing),
        (
            "case_08_overwrite_cascade_theme_predictions",
            case_08_overwrite_cascade_theme_predictions,
        ),
    ]

    print(f"\n[run] {len(cases)} 个用例")
    pass_n, fail_n = _run(cases)

    print("\n[cleanup] 清理伪数据 ...")
    _cleanup_all()

    print("\n" + "=" * 64)
    print(f"汇总: {pass_n} PASS / {fail_n} FAIL / {len(cases)} 用例")
    print("=" * 64)
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
