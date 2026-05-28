"""重打分后台工作线程（GUI 调 scoring_service 的 rescore_* API）。

业务定位
--------
评估页三种触发：

1. 顶栏「🔁 重打分（区间）」→ ``RescoreWorker(report_date_start=...,
   report_date_end=...)`` → ``rescore_range``（按 report_date 区间扫所有模板，
   含已 full 的报告也会被无差别覆盖）
2. 顶栏「⚡ 一键打分未完成」→ ``RescoreWorker(unfinished_filter={...})`` →
   ``rescore_unfinished_reports``（只挑 score_status ∈ {none, partial} 的
   报告逐个补齐，跳过已 full 的，节省市场 API 配额）
3. 报告级表格「打分」按钮 → ``RescoreWorker(report_id=...)`` →
   ``rescore_one_report``（精准只动该 report 下题材，不波及其他报告）

为啥要异步：``rescore_range`` / ``rescore_unfinished_reports`` 内部对每个
(theme, score_date) 都要查市场库 + 算指标；样本量上百时阻塞 UI 几十秒。
单报告通常 < 100 个 (theme, score_date) 对，也几秒钟，仍然走异步避免
UI 卡顿。

信号
----
* ``stage(str)``：状态描述
* ``finished_result(dict)``：``BatchScoringResult`` / ``BatchUnfinishedResult``
  字段字典（``mode`` 字段区分调用入口）
* ``error(str)``：异常 traceback 摘要
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal


class RescoreWorker(QThread):
    """重打分线程。三模式互斥：``report_id`` / ``unfinished_filter`` / 区间。"""

    stage = pyqtSignal(str)
    finished_result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(
        self,
        report_date_start: Optional[str] = None,
        report_date_end: Optional[str] = None,
        *,
        report_id: Optional[int] = None,
        unfinished_filter: Optional[Dict[str, Any]] = None,
        days_back: int = 5,
        hit_threshold_pct: float = 3.0,
    ):
        super().__init__()
        modes_set = sum(
            1 for x in (
                report_id is not None,
                unfinished_filter is not None,
                bool(report_date_start and report_date_end),
            ) if x
        )
        if modes_set != 1:
            raise ValueError(
                "RescoreWorker 仅支持三选一：report_id / "
                "unfinished_filter / (start, end)"
            )
        self.report_id = report_id
        self.unfinished_filter = unfinished_filter
        self.report_date_start = report_date_start
        self.report_date_end = report_date_end
        self.days_back = days_back
        self.hit_threshold_pct = hit_threshold_pct

    def run(self):
        try:
            if self.report_id is not None:
                self._run_one_report()
            elif self.unfinished_filter is not None:
                self._run_unfinished()
            else:
                self._run_range()
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=5)
            self.error.emit(
                f"rescore 线程异常: {type(exc).__name__}: {exc}\n{tb}"
            )

    def _run_one_report(self):
        self.stage.emit(
            f"打分中：report_id={self.report_id} "
            f"D+1~D+{self.days_back} ..."
        )
        from services.scoring.scoring_service import rescore_one_report
        res = rescore_one_report(
            self.report_id,
            days_back=self.days_back,
            hit_threshold_pct=self.hit_threshold_pct,
        )
        self.finished_result.emit({
            "mode": "one_report",
            "report_id": self.report_id,
            "ok": res.ok,
            "themes_total": res.themes_total,
            "themes_scored": res.themes_scored,
            "pairs_attempted": res.pairs_attempted,
            "pairs_succeeded": res.pairs_succeeded,
            "days_covered": res.days_covered,
            "elapsed_ms": res.elapsed_ms,
            "errors": res.errors[:5],
        })

    def _run_range(self):
        self.stage.emit(
            f"重打分中：[{self.report_date_start}, "
            f"{self.report_date_end}] D+1~D+{self.days_back} ..."
        )
        from services.scoring.scoring_service import rescore_range
        res = rescore_range(
            self.report_date_start, self.report_date_end,
            days_back=self.days_back,
            hit_threshold_pct=self.hit_threshold_pct,
        )
        self.finished_result.emit({
            "mode": "range",
            "ok": res.ok,
            "themes_total": res.themes_total,
            "themes_scored": res.themes_scored,
            "pairs_attempted": res.pairs_attempted,
            "pairs_succeeded": res.pairs_succeeded,
            "days_covered": res.days_covered,
            "elapsed_ms": res.elapsed_ms,
            "errors": res.errors[:5],
        })

    def _run_unfinished(self):
        f = self.unfinished_filter or {}
        self.stage.emit(
            f"扫描未完成报告（最近 {f.get('days', 30)} 天 / "
            f"{f.get('time_dim', 'report_date')}）..."
        )
        from services.scoring.scoring_service import (
            rescore_unfinished_reports,
        )
        res = rescore_unfinished_reports(
            days=int(f.get("days", 30)),
            time_dim=str(f.get("time_dim", "report_date")),
            is_backtest_filter=f.get("is_backtest_filter"),
            days_back=self.days_back,
            hit_threshold_pct=self.hit_threshold_pct,
        )
        self.finished_result.emit({
            "mode": "unfinished",
            "ok": res.ok,
            "themes_total": res.themes_total,
            "themes_scored": res.themes_scored,
            "pairs_attempted": res.pairs_attempted,
            "pairs_succeeded": res.pairs_succeeded,
            "days_covered": res.days_covered,
            "elapsed_ms": res.elapsed_ms,
            "errors": res.errors[:5],
            "reports_targeted": res.reports_targeted,
            "reports_done": res.reports_done,
            "reports_skipped": res.reports_skipped,
        })
