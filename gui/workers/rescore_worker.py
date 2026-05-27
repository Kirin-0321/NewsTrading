"""重打分后台工作线程（GUI 调 scoring_service 的 rescore_* API）。

业务定位
--------
评估页两种触发：

1. 顶栏「🔁 重打分（区间）」→ ``RescoreWorker(report_date_start=...,
   report_date_end=...)`` → ``rescore_range``（按 report_date 区间扫所有模板）
2. 报告级表格「打分」按钮 → ``RescoreWorker(report_id=...)`` →
   ``rescore_one_report``（精准只动该 report 下题材，不波及其他报告）

为啥要异步：``rescore_range`` 内部对每个 (theme, score_date) 都要查市场库 +
算指标；样本量上百时阻塞 UI 几十秒。单报告通常 < 100 个 (theme, score_date)
对，也几秒钟，仍然走异步避免 UI 卡顿。

信号
----
* ``stage(str)``：状态描述
* ``finished_result(dict)``：``BatchScoringResult`` 字段字典
* ``error(str)``：异常 traceback 摘要
"""

from __future__ import annotations

import traceback
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class RescoreWorker(QThread):
    """重打分线程。两种模式互斥：传 report_id 走单报告，否则走区间。"""

    stage = pyqtSignal(str)
    finished_result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(
        self,
        report_date_start: Optional[str] = None,
        report_date_end: Optional[str] = None,
        *,
        report_id: Optional[int] = None,
        days_back: int = 5,
        hit_threshold_pct: float = 3.0,
    ):
        super().__init__()
        if report_id is None and not (
            report_date_start and report_date_end
        ):
            raise ValueError(
                "RescoreWorker 需要 report_id 或 (start, end) 之一"
            )
        self.report_id = report_id
        self.report_date_start = report_date_start
        self.report_date_end = report_date_end
        self.days_back = days_back
        self.hit_threshold_pct = hit_threshold_pct

    def run(self):
        try:
            if self.report_id is not None:
                self._run_one_report()
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
