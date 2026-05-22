"""
爬虫后台工作线程
"""

from PyQt5.QtCore import QThread, pyqtSignal
import traceback


class CrawlerWorker(QThread):
    """爬虫工作线程"""
    
    # 信号定义
    progress_updated = pyqtSignal(int, int, int, str)  # current, total, news_count, message
    finished = pyqtSignal(list)  # files
    error = pyqtSignal(str)  # error_message
    log_message = pyqtSignal(str)  # log message
    
    def __init__(self, scroll_times=36, wait_seconds=6, headless=True, max_no_change=3, auto_stop=True):
        super().__init__()
        self.scroll_times = scroll_times
        self.wait_seconds = wait_seconds
        self.headless = headless
        self.max_no_change = max_no_change
        self.auto_stop = auto_stop
        self._is_running = False
        self._crawler_ref = None
        
    def run(self):
        """运行爬虫"""
        self._is_running = True
        
        try:
            self.log_message.emit("正在初始化爬虫...")
            
            # 如果启用自动停止，获取数据库最新新闻
            latest_news = None
            if self.auto_stop:
                from core.db_helper import get_latest_news_from_db
                latest_news = get_latest_news_from_db()
                if latest_news:
                    time_str = latest_news.get('datetime') or latest_news.get('time', '')
                    title_str = latest_news.get('title', '')
                    self.log_message.emit(f"[增量模式] 数据库最新新闻:")
                    self.log_message.emit(f"  时间: {time_str}")
                    self.log_message.emit(f"  标题: {title_str[:50]}...")
                else:
                    self.log_message.emit("[增量模式] 数据库为空，将进行完整爬取")
            
            # 导入爬虫模块
            from core.news_crawler_scroll import NewsCrawler
            from core.db_helper import parse_news_time
            
            # 创建爬虫实例
            crawler = NewsCrawler()
            self._crawler_ref = crawler
            
            # 设置进度回调
            crawler.set_progress_callback(self._on_progress)
            
            # 如果启用自动停止，设置参数
            if self.auto_stop and latest_news:
                latest_time = parse_news_time(latest_news)
                latest_title = latest_news.get('title', '')
                crawler.set_auto_stop(True, latest_time, latest_title)  # 修复：直接调用 crawler 的方法
                self.log_message.emit(f"[增量模式] 已启用实时自动停止")
            
            self.log_message.emit(f"开始爬取（滚动{self.scroll_times}次，等待{self.wait_seconds}秒）...")
            
            # 运行爬虫
            files = crawler.run(
                scroll_times=self.scroll_times,
                wait_seconds=self.wait_seconds,
                headless=self.headless,
                max_no_change=self.max_no_change
            )
            
            self.log_message.emit(f"爬取完成！生成了 {len(files)} 个文件")
            
            # 如果启用自动停止且有最新新闻，进行增量处理
            if self.auto_stop and latest_news and files:
                self.log_message.emit("[增量处理] 开始去重和保存...")
                try:
                    processed_files = self._process_incremental(files, latest_news)
                    self.finished.emit(processed_files)
                except Exception as e:
                    self.log_message.emit(f"[增量处理] 处理失败: {str(e)}")
                    self.finished.emit(files)  # 出错时返回原始文件
            else:
                self.finished.emit(files)
            
        except Exception as e:
            error_msg = f"爬取失败: {str(e)}"
            self.log_message.emit(error_msg)
            self.error.emit(error_msg)
            print(traceback.format_exc())
        
        finally:
            self._is_running = False
    
    def _on_progress(self, current, total, news_count, message):
        """进度回调"""
        if self._is_running:
            self.progress_updated.emit(current, total, news_count, message)
            self.log_message.emit(f"[{current}/{total}] {message}")
    
    def _process_incremental(self, files, latest_news):
        """
        处理增量爬取结果
        
        Args:
            files: 爬取生成的文件列表
            latest_news: 数据库中最新的新闻
            
        Returns:
            处理后的文件列表
        """
        import json
        import os
        from datetime import datetime
        from core.db_helper import deduplicate_with_db, parse_news_time
        
        # 读取爬取的所有新闻
        all_news = []
        for file_path in files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    news_list = data.get('news', [])
                    all_news.extend(news_list)
            except Exception as e:
                self.log_message.emit(f"[增量处理] 读取文件失败: {file_path}, {e}")
                continue
        
        self.log_message.emit(f"[增量处理] 爬取到 {len(all_news)} 条新闻")
        
        # 检测是否需要截断（遇到已有新闻）
        latest_time = parse_news_time(latest_news)
        latest_title = latest_news.get('title', '')
        
        truncated_news = []
        found_existing = False
        
        for news in all_news:
            current_time = parse_news_time(news)
            current_title = news.get('title', '')
            
            # 检查是否遇到已有新闻
            if current_title == latest_title:
                self.log_message.emit(f"[自动停止] 遇到已有新闻（标题匹配）")
                found_existing = True
                break
            
            if current_time and latest_time and current_time <= latest_time:
                self.log_message.emit(f"[自动停止] 时间到达边界（{current_time} <= {latest_time}）")
                found_existing = True
                break
            
            truncated_news.append(news)
        
        if found_existing:
            self.log_message.emit(f"[增量处理] 保留 {len(truncated_news)} 条新增新闻")
        else:
            truncated_news = all_news
            self.log_message.emit(f"[增量处理] 未遇到已有新闻，保留全部")
        
        # 与数据库去重
        final_news, stats = deduplicate_with_db(truncated_news)
        
        self.log_message.emit(f"[去重统计] 爬取: {stats['crawled']} 条")
        self.log_message.emit(f"[去重统计] 内部去重: -{stats['internal_duplicates']} 条")
        self.log_message.emit(f"[去重统计] 数据库去重: -{stats['db_duplicates']} 条")
        self.log_message.emit(f"[去重统计] 最终保存: {stats['final_saved']} 条")
        
        if len(final_news) == 0:
            self.log_message.emit("[增量处理] 没有新增新闻，不生成文件")
            return []
        
        # 使用新的命名规则保存文件
        output_file = self._save_incremental_file(final_news, stats, found_existing, latest_news)
        
        # 删除原始临时文件
        for file_path in files:
            try:
                os.remove(file_path)
                self.log_message.emit(f"[清理] 删除临时文件: {os.path.basename(file_path)}")
            except Exception as e:
                self.log_message.emit(f"[清理] 删除失败: {e}")
        
        return [output_file] if output_file else []
    
    def _save_incremental_file(self, news_list, stats, found_existing, latest_news):
        """
        保存增量爬取的文件
        
        Args:
            news_list: 新闻列表
            stats: 统计信息
            found_existing: 是否遇到已有新闻
            latest_news: 数据库中最新的新闻
            
        Returns:
            保存的文件路径
        """
        import json
        import os
        from datetime import datetime
        
        if not news_list:
            return None
        
        # 获取时间范围
        start_time = news_list[-1].get('datetime') or news_list[-1].get('time', '')
        end_time = news_list[0].get('datetime') or news_list[0].get('time', '')
        
        # 生成文件名: MM-DD-HH_MM-DD-HH_new.json
        try:
            start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
            end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
            
            filename = f"{start_dt.strftime('%m-%d-%H')}_{end_dt.strftime('%m-%d-%H')}_new.json"
        except:
            # 如果解析失败，使用当前时间
            now = datetime.now()
            filename = f"{now.strftime('%m-%d-%H')}_{now.strftime('%m-%d-%H')}_new.json"
        
        # 保存文件
        output_dir = 'data/raw'
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, filename)
        
        # 构建JSON数据
        data = {
            "crawl_type": "incremental",
            "crawl_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "auto_stopped": found_existing,
            "stopped_reason": "found_existing_news" if found_existing else "normal_completion",
            "latest_existing": {
                "time": latest_news.get('datetime') or latest_news.get('time', ''),
                "title": latest_news.get('title', '')
            } if found_existing else None,
            "statistics": stats,
            "total": len(news_list),
            "time_range": {
                "start": start_time,
                "end": end_time
            },
            "news": news_list
        }
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        self.log_message.emit(f"[保存] 文件已保存: {filename}")
        return output_file
    
    def stop(self):
        """协作式停止爬虫（关闭浏览器并结束滚动）"""
        self._is_running = False
        self.log_message.emit("正在停止爬虫...")
        if self._crawler_ref is not None:
            self._crawler_ref.request_stop()

