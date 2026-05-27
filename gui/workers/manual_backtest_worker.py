"""手动回测后台工作线程（GUI 调 ``tools.backtest_prompt.backtest_one``）。

设计要点
--------
* 单一职责：一次跑一个 ``(template_id, trade_date, news_window)`` 回测
* 异步：QThread 避免阻塞 GUI 主线程（LLM 调用可能 30~60s）
* 进度信号：让页面显示 "重建快照 → 调 LLM → 抽题材 → 入库"
* 防穿越：news_end_dt 在未来时让 backtest_one 自然 ok=False 上抛
* 取消：当前一期不实现取消（LLM 已发出难以中断；二期再做）

时间窗主人语义（2026-05-27 重构）
--------------------------------
* ``news_start_dt`` / ``news_end_dt`` 传 ``None`` = snapshot 内部用主人默认窗
* ``news_status`` ``"curated"`` 默认精选，``""`` 全部

信号
----
* ``stage(str)``：状态描述，如 "重建快照"、"调用 LLM"
* ``finished_result(dict)``：完成时返回 ``BacktestResult`` 字段的字典
* ``error(str)``：异常时携带 traceback 摘要
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class ManualBacktestWorker(QThread):
    """单次手动回测线程。"""

    stage = pyqtSignal(str)
    finished_result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(
        self,
        template_id: str,
        trade_date: str,
        *,
        news_start_dt: Optional[datetime] = None,
        news_end_dt: Optional[datetime] = None,
        news_status: str = "curated",
        provider: Optional[str] = None,
        overwrite: bool = False,
        dry_run: bool = False,
    ):
        super().__init__()
        self.template_id = template_id
        self.trade_date = trade_date
        self.news_start_dt = news_start_dt
        self.news_end_dt = news_end_dt
        self.news_status = news_status
        self.provider = provider
        self.overwrite = overwrite
        self.dry_run = dry_run

    def run(self):
        try:
            win_str = (
                f"[{self.news_start_dt.strftime('%m-%d %H:%M') if self.news_start_dt else '默认'},"
                f" {self.news_end_dt.strftime('%m-%d %H:%M') if self.news_end_dt else '默认'})"
            )
            self.stage.emit(
                f"开始：{self.template_id} @ {self.trade_date} "
                f"win={win_str} status={self.news_status or 'all'} "
                f"(dry_run={self.dry_run})"
            )
            from tools.backtest_prompt import backtest_one
            self.stage.emit("重建历史快照...")
            res = backtest_one(
                self.template_id,
                self.trade_date,
                news_start_dt=self.news_start_dt,
                news_end_dt=self.news_end_dt,
                news_status=self.news_status,
                provider=self.provider,
                overwrite=self.overwrite,
                dry_run=self.dry_run,
            )
            if res.ok and not res.skipped and not self.dry_run:
                self.stage.emit("LLM 调用完成 + 题材抽取入库")
            self.finished_result.emit({
                "ok": res.ok,
                "skipped": res.skipped,
                "template_id": res.template_id,
                "trade_date": res.trade_date,
                "news_start_iso": res.news_start_iso,
                "news_end_iso": res.news_end_iso,
                "news_status": res.news_status,
                "snapshot_news_count": res.snapshot_news_count,
                "report_path": res.report_path,
                "themes_count": res.themes_count,
                "elapsed_ms": res.elapsed_ms,
                "error": res.error,
                "overwritten": res.overwritten,
                "deleted_reports": res.deleted_reports,
                "deleted_themes": res.deleted_themes,
                "deleted_md_files": res.deleted_md_files,
            })
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=5)
            self.error.emit(
                f"manual backtest 线程异常: "
                f"{type(exc).__name__}: {exc}\n{tb}"
            )
