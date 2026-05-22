"""题材抽取后台线程（单文件）。

调用链:
    ThemePredictionPage.start_extract()
        └→ ThemeExtractWorker
              ├→ ThemeExtractor.extract_from_file(report_path)
              ├→ parse_report_meta(report_path)
              └→ ThemeStore.save_themes(meta, themes)
"""

import traceback
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal


class ThemeExtractWorker(QThread):
    """单文件题材抽取线程。"""

    finished = pyqtSignal(dict)  # {'ok': bool, 'themes': N, 'stocks': M, 'news': K, 'report_id': str}
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    streaming = pyqtSignal(str)  # AI 流式 chunk，粘连写入 UI

    def __init__(
        self,
        report_path: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__()
        self.report_path = report_path
        self.provider = provider
        self.model = model

    def run(self):
        try:
            self.progress.emit(f"读取报告: {self.report_path}")

            from core.theme_extractor import ThemeExtractor, parse_report_meta
            from services.storage import get_theme_store

            extractor = ThemeExtractor(
                provider=self.provider,
                model=self.model,
            )
            self.progress.emit(
                f"调用 AI 抽取题材（provider={extractor.provider}, model={extractor.model}）..."
            )

            def on_progress(msg, is_streaming=False):
                """流式 chunk 走 streaming 信号，阶段提示走 progress 信号。"""
                if is_streaming:
                    self.streaming.emit(msg)
                else:
                    self.progress.emit(msg)

            themes, news_id_map, err = extractor.extract_from_file(
                self.report_path,
                progress_callback=on_progress,
            )
            if err and not themes:
                self.error.emit(err)
                return
            if err and themes:
                # 部分成功（多半是 AI 输出被截断救出的题材）
                self.progress.emit(f"⚠️ {err}")

            self.progress.emit(
                f"抽取到 {len(themes)} 个题材，正在入库..."
                f"（报告底部映射到 {len(news_id_map)} 个新闻 ID）"
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
            self.finished.emit(payload)

        except Exception as e:
            msg = f"题材抽取失败: {e}"
            self.progress.emit(msg)
            self.error.emit(msg)
            print(traceback.format_exc())
