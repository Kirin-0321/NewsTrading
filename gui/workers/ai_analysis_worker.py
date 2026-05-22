"""
AI分析后台工作线程
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class AIAnalysisWorker(QThread):
    """AI分析工作线程"""

    # 信号定义
    finished = pyqtSignal(dict)  # result
    error = pyqtSignal(str)  # error_message
    progress = pyqtSignal(str)  # progress message
    streaming = pyqtSignal(str)  # streaming content

    def __init__(
        self,
        file_path,
        provider='openai',
        max_sectors=6,  # 可以是整数或字符串'auto'
        stocks_per_sector=5,  # 可以是整数或字符串'auto'
        max_news=None,
        template_id=None,
        market_summary=None
    ):
        super().__init__()
        self.file_path = file_path
        self.provider = provider
        self.max_sectors = max_sectors  # 支持 int 或 'auto'
        self.stocks_per_sector = stocks_per_sector  # 支持 int 或 'auto'
        self.max_news = max_news
        self.template_id = template_id
        self.market_summary = market_summary

    def run(self):
        """运行分析"""
        try:
            from core.ai_news_analyzer import AINewsAnalyzer

            # 创建分析器
            analyzer = AINewsAnalyzer()

            # 定义进度回调
            def progress_callback(message, is_streaming=False):
                if is_streaming:
                    self.streaming.emit(message)
                else:
                    self.progress.emit(message)

            # 执行分析
            result = analyzer.analyze(
                file_path=self.file_path,
                provider=self.provider,
                max_sectors=self.max_sectors,
                stocks_per_sector=self.stocks_per_sector,
                max_news=self.max_news,
                template_id=self.template_id,
                market_summary=self.market_summary,
                progress_callback=progress_callback
            )

            if result.get('success'):
                self.finished.emit(result)
            else:
                self.error.emit(result.get('error', '分析失败'))

        except Exception as e:
            error_msg = f"分析失败: {str(e)}"
            self.progress.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())

