"""
每日新闻分析流程
爬取 → 清洗 → 导出 → AI分析
"""

import os
from datetime import datetime
from typing import Dict, Any
from .base import WorkflowBase


class DailyNewsFlow(WorkflowBase):
    """每日新闻分析流程"""
    
    workflow_id = "daily_news_flow"
    name = "每日新闻分析流程"
    description = "自动执行爬取、清洗、导出、AI分析的完整流程"
    
    def execute(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行任务流
        
        Args:
            params: 执行参数
            
        Returns:
            执行结果
        """
        results = {}
        
        # Step 1: 爬取新闻
        self.log("Step 1: 开始爬取新闻...")
        crawler_file = self._run_crawler(params.get('crawler', {}))
        if not crawler_file:
            raise Exception("爬取新闻失败")
        results['crawler_file'] = crawler_file
        self.log(f"Step 1: ✓ 完成，文件: {crawler_file}")
        
        # Step 2: 清洗数据
        self.log("Step 2: 开始清洗数据...")
        cleaned_file = self._run_cleaner(crawler_file, params.get('cleaner', {}))
        if not cleaned_file:
            raise Exception("清洗数据失败")
        results['cleaned_file'] = cleaned_file
        self.log(f"Step 2: ✓ 完成，文件: {cleaned_file}")
        
        # Step 3: 导出数据
        self.log("Step 3: 开始导出数据...")
        export_file = self._run_export(params.get('export', {}))
        if not export_file:
            raise Exception("导出数据失败")
        results['export_file'] = export_file
        self.log(f"Step 3: ✓ 完成，文件: {export_file}")
        
        # Step 4: AI分析
        self.log("Step 4: 开始AI分析...")
        summary = self._get_summary(params.get('analyzer', {}))
        if summary:
            self.log(f"Step 4: 读取盘后总结成功，长度: {len(summary)}字")
        else:
            self.log("Step 4: 未找到盘后总结，将不使用盘后总结进行分析", level="warning")
        
        analysis_file = self._run_analyzer(export_file, summary, params.get('analyzer', {}))
        if not analysis_file:
            raise Exception("AI分析失败")
        results['analysis_file'] = analysis_file
        self.log(f"Step 4: ✓ 完成，文件: {analysis_file}")
        
        return results
    
    def _run_crawler(self, params: Dict[str, Any]) -> str:
        """
        执行爬虫任务
        
        Args:
            params: 爬虫参数
            
        Returns:
            生成的文件路径
        """
        try:
            from core.news_crawler_scroll import NewsCrawler
            
            scroll_times = params.get('scroll_times', 36)
            wait_seconds = params.get('wait_seconds', 6)
            auto_stop = params.get('auto_stop', True)
            
            self.log(f"  参数: 滚动{scroll_times}次, 等待{wait_seconds}秒, 自动停止={auto_stop}")
            
            crawler = NewsCrawler()
            
            # 如果启用自动停止，设置参数
            if auto_stop:
                from core.db_helper import get_latest_news_from_db, parse_news_time
                latest_news = get_latest_news_from_db()
                
                if latest_news:
                    latest_time = parse_news_time(latest_news)
                    latest_title = latest_news.get('title', '')
                    crawler.set_auto_stop(True, latest_time, latest_title)
                    self.log(f"  已启用自动停止，数据库最新新闻: {latest_title[:30]}...")
                else:
                    self.log(f"  未找到数据库新闻，将爬取全部")
            
            # 运行爬虫（不传递auto_stop参数）
            files = crawler.run(
                scroll_times=scroll_times,
                wait_seconds=wait_seconds
            )
            
            if files and len(files) > 0:
                # 返回最新生成的文件（如果启用自动停止，则返回 _new.json 文件）
                latest_file = files[-1]
                return latest_file
            else:
                return None
                
        except Exception as e:
            self.log(f"  爬虫执行失败: {str(e)}", level="error")
            raise
    
    def _run_cleaner(self, input_file: str, params: Dict[str, Any]) -> str:
        """
        执行数据清洗
        
        Args:
            input_file: 输入文件路径
            params: 清洗参数
            
        Returns:
            生成的清洗文件路径
        """
        try:
            from core.news_cleaner import NewsCleaner
            import json
            
            ai_provider = params.get('ai_provider', 'deepseek')
            batch_size = params.get('batch_size', 100)
            
            self.log(f"  参数: AI={ai_provider}, 批量={batch_size}")
            self.log(f"  输入: {input_file}")
            
            # 加载清洗标准
            criteria_file = 'config/cleaning_criteria.json'
            if os.path.exists(criteria_file):
                with open(criteria_file, 'r', encoding='utf-8') as f:
                    criteria_config = json.load(f)
                    criteria = criteria_config['default']['criteria']
                    self.log(f"  已加载清洗标准")
            else:
                self.log(f"  未找到清洗标准文件，使用默认标准", level="warning")
                criteria = "保留有投资价值的新闻"
            
            # 创建清洗器
            cleaner = NewsCleaner(criteria=criteria, ai_provider=ai_provider)
            
            # 使用 clean_news_files 方法
            result = cleaner.clean_news_files(
                file_paths=[input_file],
                batch_size=batch_size,
                auto_merge=True
            )
            
            kept = result['kept']
            removed = result['removed']
            
            self.log(f"  清洗完成: 保留{len(kept)}条, 移除{len(removed)}条")
            
            # 生成输出文件名
            base_name = os.path.basename(input_file)
            if base_name.endswith('_new.json'):
                output_name = base_name.replace('_new.json', '_clear.json')
            else:
                output_name = base_name.replace('.json', '_clear.json')
            
            output_path = os.path.join('data/cleaned', output_name)
            
            # 保存清洗结果
            os.makedirs('data/cleaned', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(kept, f, ensure_ascii=False, indent=2)
            
            return output_path
            
        except Exception as e:
            self.log(f"  清洗执行失败: {str(e)}", level="error")
            raise
    
    def _run_export(self, params: Dict[str, Any]) -> str:
        """
        执行数据导出
        
        Args:
            params: 导出参数
            
        Returns:
            导出的文件路径
        """
        try:
            from datetime import timedelta
            
            source = params.get('source', 'cleaned')
            export_format = params.get('format', 'markdown')
            time_range_hours = params.get('time_range_hours', 24)
            
            self.log(f"  参数: 数据源={source}, 格式={export_format}, 时间范围={time_range_hours}小时")
            
            # 计算时间范围
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=time_range_hours)
            
            self.log(f"  时间范围: {start_time.strftime('%Y-%m-%d %H:%M')} ~ {end_time.strftime('%Y-%m-%d %H:%M')}")
            
            # 使用统一的导出工具
            from core.news_exporter import NewsExporter
            exporter = NewsExporter()
            
            result = exporter.export(
                source=source,
                format=export_format,
                start_datetime=start_time,
                end_datetime=end_time
            )
            
            if not result['success']:
                raise Exception(result.get('error', '导出失败'))
            
            self.log(f"  导出成功: {result['news_count']} 条新闻")
            if result['time_range'][0] and result['time_range'][1]:
                self.log(f"  实际时间范围: {result['time_range'][0].strftime('%Y-%m-%d %H:%M')} ~ {result['time_range'][1].strftime('%Y-%m-%d %H:%M')}")
            
            return result['file_path']
            
        except Exception as e:
            self.log(f"  导出执行失败: {str(e)}", level="error")
            raise
    
    def _get_summary(self, params: Dict[str, Any]) -> str:
        """
        获取盘后总结
        
        Args:
            params: 分析参数
            
        Returns:
            盘后总结内容
        """
        summary_mode = params.get('summary_mode', 'auto')
        summary_file = params.get('summary_file', '')
        
        if summary_mode == 'auto':
            # 自动读取前日盘后总结
            return self.read_previous_day_summary()
        elif summary_file:
            # 手动指定文件
            self.log(f"  读取指定盘后总结: {summary_file}")
            return self.read_file(summary_file)
        else:
            return ""
    
    def _run_analyzer(self, input_file: str, summary: str, params: Dict[str, Any]) -> str:
        """
        执行AI分析
        
        Args:
            input_file: 输入文件路径
            summary: 盘后总结
            params: 分析参数
            
        Returns:
            分析结果文件路径
        """
        try:
            from core.ai_news_analyzer import AINewsAnalyzer
            
            template = params.get('template', 'short_term')
            
            self.log(f"  参数: 模板={template}")
            self.log(f"  输入: {input_file}")
            
            analyzer = AINewsAnalyzer()
            
            # 执行分析（auto_sectors 和 auto_stocks 已弃用）
            result = analyzer.analyze(
                file_path=input_file,
                template_id=template,
                market_summary=summary
            )
            
            if not result.get('success'):
                error = result.get('error', '未知错误')
                raise Exception(f"AI分析失败: {error}")
            
            # 返回报告文件路径
            report_file = result.get('report_file')
            news_count = result.get('news_count', 0)
            
            self.log(f"  分析完成，分析了 {news_count} 条新闻")
            self.log(f"  报告文件: {report_file}")
            
            return report_file
            
        except Exception as e:
            self.log(f"  分析执行失败: {str(e)}", level="error")
            raise

