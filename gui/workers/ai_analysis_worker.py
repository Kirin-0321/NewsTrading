"""AI 分析后台工作线程（SQLite）。

2026-05-28 多任务队列改造
--------------------------
* 把业务 ``finished`` 信号 **改名** 为 ``finished_result``（仿
  ``ManualBacktestWorker.finished_result``），避免覆盖 ``QThread.finished()``
  —— 否则 ``AnalysisTaskManager._on_worker_qthread_done`` 永远收不到调度信号
* ``finished_result`` 在所有终态都会 emit（成功 / 失败 / 取消），dict 内
  ``ok`` / ``cancelled`` 字段标识状态，业务字段按需读取
* 新增 ``task_id`` 字段，供 ``AnalysisTaskManager`` 在跨线程信号槽里反查
  ``AnalysisTask`` 对象用（不参与业务）

信号一览
--------
* ``progress(str)``：阶段日志（同语义于手动回测 worker.progress）
* ``streaming(str)``：LLM / 题材抽取流式 chunk
* ``finished_result(dict)``：终态结果（含 ok / cancelled / 业务字段）
* ``error(str)``：异常时携带 traceback 摘要（QThread 线程级异常专用）
* ``cancelled()``：用户取消时触发（额外信号；finished_result 里也会含
  ``cancelled=True``，二者择一监听皆可）
"""

from __future__ import annotations

import traceback
from datetime import datetime
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

from core.ai_news_analyzer import AnalysisCancelledError


class AIAnalysisWorker(QThread):
    """AI 分析工作线程（单次跑一个模板）。"""

    progress = pyqtSignal(str)
    streaming = pyqtSignal(str)
    finished_result = pyqtSignal(dict)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        provider: str = "openai",
        max_sectors=6,
        stocks_per_sector=5,
        max_news: Optional[int] = None,
        template_id: Optional[str] = None,
        market_summary: Optional[str] = None,
        sqlite_source: Optional[str] = None,
        sqlite_start: Optional[datetime] = None,
        sqlite_end: Optional[datetime] = None,
        extract_themes: Optional[bool] = None,
        enable_deep_thinking: bool = True,
        task_id: Optional[int] = None,
    ):
        """单次 AI 分析线程构造。

        Args:
            task_id: 队列管理器分配的任务 id（2026-05-28 多任务改造）。
                manager 模式下由 ``AnalysisTaskManager.enqueue`` 分配；单线程旧
                调用方式不传则为 None。本字段不参与业务，仅供 manager 在跨
                线程信号槽里反查 ``AnalysisTask`` 用。
        """
        super().__init__()
        self.provider = provider
        self.max_sectors = max_sectors
        self.stocks_per_sector = stocks_per_sector
        self.max_news = max_news
        self.template_id = template_id
        self.market_summary = market_summary
        self.sqlite_source = sqlite_source
        self.sqlite_start = sqlite_start
        self.sqlite_end = sqlite_end
        self.extract_themes = extract_themes
        self.enable_deep_thinking = enable_deep_thinking
        self.task_id = task_id
        self._cancel_requested = False

    def request_cancel(self):
        """请求终止当前分析任务。"""
        self._cancel_requested = True

    def is_cancel_requested(self) -> bool:
        return self._cancel_requested

    def run(self):
        try:
            def progress_callback(message, is_streaming=False):
                if self._cancel_requested:
                    raise AnalysisCancelledError("用户已终止分析")
                if is_streaming:
                    self.streaming.emit(message)
                else:
                    self.progress.emit(message)

            if not self.sqlite_source:
                self.error.emit("未指定 SQLite 数据源")
                return

            from services.analysis_service import AnalysisService

            service = AnalysisService()
            result = service.analyze(
                source=self.sqlite_source,
                start=self.sqlite_start,
                end=self.sqlite_end,
                template_id=self.template_id,
                provider=self.provider,
                max_sectors=self.max_sectors,
                stocks_per_sector=self.stocks_per_sector,
                max_news=self.max_news,
                market_summary=self.market_summary,
                extract_themes=self.extract_themes,
                enable_deep_thinking=self.enable_deep_thinking,
                cancel_check=self.is_cancel_requested,
                progress_callback=progress_callback,
            )
            if result.cancelled:
                self.cancelled.emit()
                self.finished_result.emit({
                    "ok": False,
                    "cancelled": True,
                    "error": "已取消",
                })
            elif result.ok:
                self.finished_result.emit({
                    "ok": True,
                    "cancelled": False,
                    "result": result.result_text,
                    "report_file": result.report_path,
                    "news_count": result.news_count,
                    "time_range": result.time_range,
                    "theme_count": result.theme_count,
                    "theme_error": result.theme_error,
                })
            else:
                err = result.error or "分析失败"
                self.error.emit(err)
                self.finished_result.emit({
                    "ok": False,
                    "cancelled": False,
                    "error": err,
                })

        except AnalysisCancelledError:
            self.cancelled.emit()
            self.finished_result.emit({
                "ok": False,
                "cancelled": True,
                "error": "已取消",
            })
        except Exception as e:  # noqa: BLE001
            error_msg = f"分析失败: {str(e)}"
            self.progress.emit(error_msg)
            self.error.emit(error_msg)
            self.finished_result.emit({
                "ok": False,
                "cancelled": False,
                "error": error_msg,
            })
            print(traceback.format_exc())
