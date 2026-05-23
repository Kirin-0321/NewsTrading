"""
新闻清洗后台工作线程（SQLite 原始库）
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class NewsCleaningWorker(QThread):
    """新闻清洗工作线程"""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)
    batch_progress = pyqtSignal(str, int, int, int, int)

    def __init__(
        self,
        criteria,
        ai_provider="deepseek",
        batch_size=100,
        auto_merge=True,
        sqlite_limit=500,
        max_workers=1,
    ):
        super().__init__()
        self.criteria = criteria
        self.ai_provider = ai_provider
        self.batch_size = batch_size
        self.auto_merge = auto_merge
        self.sqlite_limit = sqlite_limit
        self.max_workers = max_workers

    def run(self):
        try:
            from core.news_cleaner import NewsCleaner
            from services.storage import get_raw_store

            cleaner = NewsCleaner(
                criteria=self.criteria,
                ai_provider=self.ai_provider,
            )

            def progress_callback(*args):
                if len(args) == 1:
                    self.progress.emit(args[0])
                elif len(args) == 5:
                    self.batch_progress.emit(*args)

            raw_store = get_raw_store()
            uncleaned = raw_store.get_uncleaned_news(limit=self.sqlite_limit)
            if not uncleaned:
                self.error.emit("没有待清洗的原始新闻")
                return

            for item in uncleaned:
                item["raw_id"] = item.get("id")

            self.progress.emit(f"从 SQLite 原始库加载 {len(uncleaned)} 条待清洗新闻")
            results = cleaner.clean_news_list(
                uncleaned,
                batch_size=self.batch_size,
                auto_merge=self.auto_merge,
                progress_callback=progress_callback,
                max_workers=self.max_workers,
            )
            kept = results.get("kept", [])
            removed = results.get("removed", [])

            self.progress.emit("正在更新 raw_news.clean_status ...")
            updated_kept, updated_rejected = raw_store.apply_clean_results(
                kept, removed, provider=self.ai_provider
            )

            source_count = len(uncleaned)
            kept_count = len(kept)
            removed_count = len(removed)
            statistics = {
                "source_count": source_count,
                "kept_count": kept_count,
                "removed_count": removed_count,
                "updated_curated": updated_kept,
                "updated_rejected": updated_rejected,
                "kept_percent": round(
                    kept_count / source_count * 100, 1
                ) if source_count else 0,
                "removed_percent": round(
                    removed_count / source_count * 100, 1
                ) if source_count else 0,
            }
            self.finished.emit({
                "statistics": statistics,
                "metadata": results.get("metadata", {}),
            })

        except Exception as e:
            error_msg = f"清洗失败: {str(e)}"
            self.progress.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())
