"""
新闻清洗后台工作线程
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class NewsCleaningWorker(QThread):
    """新闻清洗工作线程"""
    
    # 信号定义
    finished = pyqtSignal(dict)  # 清洗完成: {kept_file, removed_file, statistics}
    error = pyqtSignal(str)  # 错误信息
    progress = pyqtSignal(str)  # 文本进度信息
    batch_progress = pyqtSignal(str, int, int, int, int)  # 批次进度
    
    def __init__(
        self,
        file_paths,
        criteria,
        ai_provider='deepseek',
        batch_size=100,
        auto_merge=True
    ):
        super().__init__()
        self.file_paths = file_paths
        self.criteria = criteria
        self.ai_provider = ai_provider
        self.batch_size = batch_size
        self.auto_merge = auto_merge
    
    def run(self):
        """运行清洗任务"""
        try:
            from core.news_cleaner import NewsCleaner
            
            # 创建清洗器
            cleaner = NewsCleaner(
                criteria=self.criteria,
                ai_provider=self.ai_provider
            )
            
            # 定义进度回调
            def progress_callback(*args):
                if len(args) == 1:
                    # 文本消息
                    self.progress.emit(args[0])
                elif len(args) == 5:
                    # 批次进度：batch_info, current, total, kept_count, removed_count
                    self.batch_progress.emit(*args)
            
            # 执行清洗
            results = cleaner.clean_news_files(
                file_paths=self.file_paths,
                batch_size=self.batch_size,
                auto_merge=self.auto_merge,
                progress_callback=progress_callback
            )
            
            # 保存结果
            self.progress.emit("正在保存结果...")
            kept_file, removed_file = cleaner.save_results(results)
            
            # 生成统计信息
            statistics = {
                'source_count': results['metadata']['source_count'],
                'kept_count': results['metadata']['kept_count'],
                'removed_count': results['metadata']['removed_count'],
                'kept_percent': round(
                    results['metadata']['kept_count'] / results['metadata']['source_count'] * 100, 1
                ) if results['metadata']['source_count'] > 0 else 0,
                'removed_percent': round(
                    results['metadata']['removed_count'] / results['metadata']['source_count'] * 100, 1
                ) if results['metadata']['source_count'] > 0 else 0
            }
            
            # 发送完成信号
            self.finished.emit({
                'kept_file': kept_file,
                'removed_file': removed_file,
                'statistics': statistics,
                'metadata': results['metadata']
            })
            
        except Exception as e:
            error_msg = f"清洗失败: {str(e)}"
            self.progress.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())

