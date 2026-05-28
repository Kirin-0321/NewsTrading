"""题材抽取后台线程（2026-05-28 队列化改造：信号加 task_id 首位）。

调用链:
    ThemeExtractTaskManager._start_worker(task)
        └→ ThemeExtractWorker(task_id, report_path, provider, model)
              ├→ ThemeExtractor.extract_from_file(report_path)
              ├→ parse_report_meta(report_path)
              └→ ThemeStore.save_themes(meta, themes)

信号契约（2026-05-28 队列化改造）
---------------------------------
所有 4 个业务信号首位都是 ``task_id``（int），方便 manager 在多任务并发
场景下区分；旧的"无 task_id"签名已废，所有上层调用走 manager 入队。

* ``progress(int task_id, str msg)``         阶段日志
* ``streaming(int task_id, str chunk)``      LLM 流式 chunk
* ``finished_payload(int task_id, dict)``    抽取完成（ok=True 入库成功 /
                                              ok=False 表示业务失败但线程
                                              没崩，附 error/warning 字段）
* ``error(int task_id, str msg)``            线程级异常（traceback 已捕获）

QThread 自带的 ``finished()``（无参）由 manager 单独连作清理槽，与上面
4 个业务信号互不冲突。

历史接口（已废）
---------------
2026-05-28 之前 worker 直接被 page 创建（``ThemeExtractWorker(report_path)``
不带 task_id），4 信号无 task_id；现已统一走 manager.enqueue 入队。
"""

import traceback
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class ThemeExtractWorker(QThread):
    """单文件题材抽取线程（接受 task_id，所有业务信号首位回传 task_id）。"""

    finished_payload = pyqtSignal(int, dict)
    error = pyqtSignal(int, str)
    progress = pyqtSignal(int, str)
    streaming = pyqtSignal(int, str)

    def __init__(
        self,
        *,
        task_id: int,
        report_path: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__()
        self.task_id = task_id
        self.report_path = report_path
        self.provider = provider
        self.model = model

    def run(self):
        tid = self.task_id
        try:
            self.progress.emit(tid, f"读取报告: {self.report_path}")

            from core.theme_extractor import ThemeExtractor, parse_report_meta
            from services.storage import get_theme_store

            extractor = ThemeExtractor(
                provider=self.provider,
                model=self.model,
            )
            self.progress.emit(
                tid,
                f"调用 AI 抽取题材（provider={extractor.provider}, "
                f"model={extractor.model}）...",
            )

            def on_progress(msg, is_streaming=False):
                """流式 chunk 走 streaming 信号，阶段提示走 progress 信号。"""
                if is_streaming:
                    self.streaming.emit(tid, msg)
                else:
                    self.progress.emit(tid, msg)

            themes, news_id_map, err = extractor.extract_from_file(
                self.report_path,
                progress_callback=on_progress,
            )
            if err and not themes:
                self.error.emit(tid, err)
                return
            if err and themes:
                # 部分成功（多半是 AI 输出被截断救出的题材）
                self.progress.emit(tid, f"⚠️ {err}")

            self.progress.emit(
                tid,
                f"抽取到 {len(themes)} 个题材，正在入库..."
                f"（报告底部映射到 {len(news_id_map)} 个新闻 ID）",
            )

            meta = parse_report_meta(self.report_path)
            stats = get_theme_store().save_themes(
                meta, themes, news_id_map=news_id_map
            )

            payload = {
                "ok": True,
                "report_id": meta["report_id"],
                "report_date": meta["report_date"],
                "report_time": meta["report_time"],
                "themes": stats["themes"],
                "stocks": stats["stocks"],
                "news": stats["news"],
                "warning": err or None,
            }
            self.finished_payload.emit(tid, payload)

        except Exception as e:
            msg = f"题材抽取失败: {e}"
            self.progress.emit(tid, msg)
            self.error.emit(tid, msg)
            print(traceback.format_exc())
