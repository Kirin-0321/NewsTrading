"""单报告删除后台工作线程。

业务定位
--------
评估页（``PromptEvalPage``）报告级表格「删除」按钮触发：
异步调 :func:`services.storage.ai_reports_store.delete_report`，
避免删 md 文件 + db 操作阻塞 UI。

调用链
------
::

    PromptEvalPage._on_delete_clicked(rid)
        ↓
    DeleteReportWorker(report_id=rid, allow_real=bool)
        ↓
    delete_report(rid, allow_real=...) → DeleteResult
        ↓
    finished_result.emit(dict)
        ↓
    PromptEvalPage._on_delete_done(...) → refresh()

信号
----
* ``stage(str)``：状态描述
* ``finished_result(dict)``：:class:`DeleteResult` 字段字典 + ``ok``
* ``error(str)``：异常摘要（含 PermissionError / ValueError）
"""

from __future__ import annotations

import traceback
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class DeleteReportWorker(QThread):
    """删除单份报告线程（DB + md 文件）。"""

    stage = pyqtSignal(str)
    finished_result = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(
        self,
        report_id: int,
        *,
        allow_real: bool = False,
        delete_md: bool = True,
        dry_run: bool = False,
    ):
        super().__init__()
        if report_id is None or int(report_id) <= 0:
            raise ValueError(
                f"DeleteReportWorker.report_id 必须为正整数，"
                f"得到 {report_id!r}"
            )
        self.report_id = int(report_id)
        self.allow_real = bool(allow_real)
        self.delete_md = bool(delete_md)
        self.dry_run = bool(dry_run)
        self._last_error: Optional[str] = None

    def run(self):
        try:
            self.stage.emit(
                f"删除中：report_id={self.report_id} "
                f"allow_real={self.allow_real}"
            )
            from services.storage.ai_reports_store import delete_report
            res = delete_report(
                self.report_id,
                allow_real=self.allow_real,
                delete_md=self.delete_md,
                dry_run=self.dry_run,
            )
            self.finished_result.emit({
                "ok": res.ok,
                "report_id": res.report_id,
                "file_path": res.file_path,
                "is_backtest": res.is_backtest,
                "prompt_id": res.prompt_id,
                "report_date": res.report_date,
                "ai_reports_deleted": res.ai_reports_deleted,
                "theme_predictions_deleted":
                    res.theme_predictions_deleted,
                "md_file_deleted": res.md_file_deleted,
                "dry_run": res.dry_run,
                "error": res.error,
            })
        except (ValueError, PermissionError) as exc:
            # 拒绝/不存在：业务错，结构化抛 finished_result + ok=False
            self.finished_result.emit({
                "ok": False,
                "report_id": self.report_id,
                "file_path": "",
                "is_backtest": -1,
                "prompt_id": None,
                "report_date": None,
                "ai_reports_deleted": 0,
                "theme_predictions_deleted": 0,
                "md_file_deleted": False,
                "dry_run": self.dry_run,
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=5)
            self.error.emit(
                f"删除线程异常: {type(exc).__name__}: {exc}\n{tb}"
            )
