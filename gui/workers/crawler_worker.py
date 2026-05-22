"""
爬虫后台工作线程（SQLite 原始库）
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class CrawlerWorker(QThread):
    """爬虫工作线程"""

    progress_updated = pyqtSignal(int, int, int, str)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    log_message = pyqtSignal(str)

    def __init__(
        self,
        scroll_times=36,
        wait_seconds=6,
        headless=True,
        max_no_change=3,
        auto_stop=True,
    ):
        super().__init__()
        self.scroll_times = scroll_times
        self.wait_seconds = wait_seconds
        self.headless = headless
        self.max_no_change = max_no_change
        self.auto_stop = auto_stop
        self._is_running = False
        self._service = None

    def run(self):
        self._is_running = True
        try:
            from services.crawl_sync_service import CrawlSyncService

            self.log_message.emit("正在初始化爬虫（SQLite 原始库）...")
            service = CrawlSyncService()
            self._service = service

            def progress(current, total, news_count, message):
                if self._is_running:
                    self.progress_updated.emit(current, total, news_count, message)
                    self.log_message.emit(f"[{current}/{total}] {message}")

            result = service.run(
                scroll_times=self.scroll_times,
                wait_seconds=self.wait_seconds,
                headless=self.headless,
                max_no_change=self.max_no_change,
                incremental=self.auto_stop,
                progress_callback=progress,
            )

            if result.ok:
                self.log_message.emit(
                    f"完成：新增 {result.inserted} 条，跳过 {result.skipped} 条"
                )
                self.finished.emit(result.stats)
            else:
                self.error.emit(result.error or "爬取失败")

        except Exception as e:
            error_msg = f"爬取失败: {str(e)}"
            self.log_message.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())
        finally:
            self._is_running = False
            self._service = None

    def stop(self):
        self._is_running = False
        self.log_message.emit("正在停止爬虫...")
        if self._service is not None:
            self._service.request_stop()
