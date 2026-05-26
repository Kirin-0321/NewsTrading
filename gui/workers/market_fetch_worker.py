"""盘后数据后台工作线程（Phase M3）。

封装 ``services.market.service.MarketSummaryService.build()`` 到 ``QThread``，
让 GUI 不阻塞主线程。

信号契约::

    progress(str)         实时进度文字（service 内部 progress_callback）
    finished(dict)        成功 - 携带 summary_json / summary_md / KPI 等
    error(str)            失败 - 携带异常文本
    cancelled()           用户点了 ⏹ 终止
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, Optional

from PyQt5.QtCore import QThread, pyqtSignal


class MarketFetchWorker(QThread):
    """触发 ``MarketSummaryService.build()``，将进度/结果通过信号回传。"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(
        self,
        *,
        trade_date: Optional[str] = None,
        mode: str = "hybrid",
        top_sector_n: int = 20,
        force_refresh: bool = False,
    ) -> None:
        super().__init__()
        self.trade_date = trade_date
        self.mode = mode
        self.top_sector_n = top_sector_n
        self.force_refresh = force_refresh
        self._cancel_requested = False

    # ------------------------------------------------------------------
    # 外部 API
    # ------------------------------------------------------------------

    def request_cancel(self) -> None:
        """主线程调用此方法发送取消请求。线程下次 cancel_check 时返回 True。"""
        self._cancel_requested = True

    def is_cancel_requested(self) -> bool:
        return self._cancel_requested

    # ------------------------------------------------------------------
    # QThread 入口
    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            from services.market.service import MarketSummaryService

            service = MarketSummaryService()
            result = service.build(
                trade_date=self.trade_date,
                mode=self.mode,
                top_sector_n=self.top_sector_n,
                force_refresh=self.force_refresh,
                cancel_check=lambda: self._cancel_requested,
                progress_callback=self._on_progress,
            )

            if result.cancelled:
                self.cancelled.emit()
                return

            if not result.ok:
                self.error.emit(
                    result.error or "盘后数据生成失败（未知错误）"
                )
                return

            payload: Dict[str, Any] = {
                "trade_date": result.trade_date,
                "prev_trade_date": result.prev_trade_date,
                "mode": result.mode,
                "summary_json": result.summary_json,
                "summary_md": result.summary_md,
                "completeness": result.completeness,
                "gaps": list(result.gaps or []),
                "warnings": list(result.warnings or []),
                "elapsed_ms": result.elapsed_ms,
                "api_call_count": result.api_call_count,
                "from_cache": result.from_cache,
            }
            self.finished.emit(payload)

        except Exception as e:
            tb = traceback.format_exc(limit=4)
            self.error.emit(f"{type(e).__name__}: {e}\n{tb}")

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _on_progress(self, msg: str) -> None:
        """``MarketSummaryService.build`` 的 progress_callback。"""
        if not msg:
            return
        # service 内部回调是字符串，原样转发给 GUI
        self.progress.emit(str(msg))
