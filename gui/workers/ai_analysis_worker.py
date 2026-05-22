"""
AI 分析后台工作线程（SQLite）
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class AIAnalysisWorker(QThread):
    """AI 分析工作线程"""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    streaming = pyqtSignal(str)

    def __init__(
        self,
        provider="openai",
        max_sectors=6,
        stocks_per_sector=5,
        max_news=None,
        template_id=None,
        market_summary=None,
        sqlite_source=None,
        sqlite_start=None,
        sqlite_end=None,
        extract_themes=None,
    ):
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

    def run(self):
        try:
            def progress_callback(message, is_streaming=False):
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
                progress_callback=progress_callback,
            )
            if result.ok:
                self.finished.emit({
                    "success": True,
                    "result": result.result_text,
                    "report_file": result.report_path,
                    "news_count": result.news_count,
                    "time_range": result.time_range,
                    "theme_count": result.theme_count,
                    "theme_error": result.theme_error,
                })
            else:
                self.error.emit(result.error or "分析失败")

        except Exception as e:
            error_msg = f"分析失败: {str(e)}"
            self.progress.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())
