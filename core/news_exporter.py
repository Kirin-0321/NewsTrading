"""
新闻导出工具
提供独立的导出功能，供GUI和工作流使用
"""

import os
import json
from datetime import datetime
from collections import OrderedDict
from typing import List, Dict, Tuple


class NewsExporter:
    """新闻导出工具类"""
    
    def __init__(self):
        pass
    
    def export(self, 
               source: str = 'cleaned',
               format: str = 'markdown',
               start_datetime: datetime = None,
               end_datetime: datetime = None,
               output_path: str = None) -> Dict:
        """
        导出新闻数据
        
        Args:
            source: 'cleaned' 或 'raw'
            format: 'markdown' 或 'json' 或 'txt'
            start_datetime: 开始时间
            end_datetime: 结束时间
            output_path: 输出文件路径（可选）
            
        Returns:
            {
                'success': bool,
                'file_path': str,
                'news_count': int,
                'time_range': (datetime, datetime),
                'error': str (if failed)
            }
        """
        try:
            # 加载并合并数据
            news_list = self.load_and_merge_json(
                source=source,
                start_datetime=start_datetime,
                end_datetime=end_datetime
            )
            
            if not news_list:
                return {
                    'success': False,
                    'error': '没有找到符合条件的新闻数据'
                }
            
            # 获取实际时间范围
            time_range = self._get_time_range(news_list)
            
            # 生成输出文件路径
            if not output_path:
                timestamp = datetime.now().strftime('%m-%d-%H')
                suffix = '_cleaned' if source == 'cleaned' else ''
                os.makedirs('data/exports', exist_ok=True)
                
                if format == 'markdown':
                    output_path = f"data/exports/{timestamp}{suffix}.md"
                elif format == 'json':
                    output_path = f"data/exports/{timestamp}{suffix}.json"
                else:  # txt
                    output_path = f"data/exports/{timestamp}{suffix}.txt"
            
            # 保存文件
            if format == 'markdown':
                self.save_markdown(news_list, output_path, source, time_range)
            elif format == 'json':
                self.save_json(news_list, output_path)
            else:  # txt
                self.save_txt(news_list, output_path)
            
            return {
                'success': True,
                'file_path': output_path,
                'news_count': len(news_list),
                'time_range': time_range
            }
            
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def load_and_merge_json(self, 
                           source: str,
                           start_datetime: datetime,
                           end_datetime: datetime) -> List[Dict]:
        """读取并合并时间范围内的所有JSON数据"""
        # 确定数据源目录和文件模式
        if source == 'cleaned':
            source_dir = 'data/cleaned'
            file_pattern = '_clear.json'
        else:
            source_dir = 'data/raw'
            file_pattern = '.json'
        
        if not os.path.exists(source_dir):
            return []
        
        news_dict = OrderedDict()
        all_news_dict = OrderedDict()
        
        # 遍历所有JSON文件
        for filename in os.listdir(source_dir):
            if not filename.endswith(file_pattern):
                continue
            
            filepath = os.path.join(source_dir, filename)
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 处理不同的JSON结构
                if isinstance(data, dict) and 'metadata' in data and 'news' in data:
                    news_list = data['news']
                elif isinstance(data, list):
                    news_list = data
                else:
                    news_list = data.get('news', [])
                
                for news in news_list:
                    time_str = self._get_news_time_str(news)
                    news_time = self._parse_news_time(time_str)
                    
                    if not news_time:
                        unique_key = f"unknown_{news.get('title', '')}"
                        if unique_key not in all_news_dict:
                            all_news_dict[unique_key] = news
                        continue
                    
                    unique_key = f"{time_str}_{news.get('title', '')}"
                    
                    if unique_key not in all_news_dict:
                        all_news_dict[unique_key] = news
                    
                    if start_datetime <= news_time <= end_datetime:
                        if unique_key not in news_dict:
                            news_dict[unique_key] = news
                
            except Exception as e:
                print(f"读取文件 {filename} 失败: {e}")
                continue
        
        # 如果有严格匹配结果
        if news_dict:
            merged_news = list(news_dict.values())
            # 语义去重
            from core.semantic_dedup import semantic_deduplicate
            merged_news = semantic_deduplicate(merged_news)
            # 按时间倒序排序
            merged_news.sort(
                key=lambda x: self._parse_news_time(self._get_news_time_str(x)) or datetime.min,
                reverse=True
            )
            return merged_news
        
        return []
    
    def save_markdown(self, news_list: List[Dict], filepath: str, 
                     source: str, time_range: Tuple[datetime, datetime]):
        """保存为Markdown格式"""
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write("# 新闻导出报告\n\n")
            f.write(f"**导出时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"**数据源**: {'清洗后数据' if source == 'cleaned' else '原始数据'}\n\n")
            
            if time_range[0] and time_range[1]:
                f.write(f"**时间范围**: {time_range[0].strftime('%Y-%m-%d %H:%M')} ~ {time_range[1].strftime('%Y-%m-%d %H:%M')}\n\n")
            
            f.write(f"**新闻数量**: {len(news_list)} 条\n\n")
            f.write("---\n\n")
            
            for i, news in enumerate(news_list, 1):
                f.write(f"## {i}. {news.get('title', '无标题')}\n\n")
                f.write(f"**时间**: {news.get('datetime') or news.get('time', '未知')}\n")
                f.write(f"**来源**: {news.get('source', '未知来源')}\n\n")
                if news.get('content'):
                    f.write(f"**内容**:\n{news['content']}\n\n")
                f.write("---\n\n")
    
    def save_json(self, news_list: List[Dict], filepath: str):
        """保存为JSON格式"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=2)
    
    def save_txt(self, news_list: List[Dict], filepath: str):
        """保存为TXT格式（极简）"""
        with open(filepath, 'w', encoding='utf-8') as f:
            for news in news_list:
                title = news.get('title', '无标题')
                f.write(f"{title}\n")
    
    def _get_news_time_str(self, news: Dict) -> str:
        """获取新闻时间字符串"""
        return news.get('datetime') or news.get('time', '')
    
    def _parse_news_time(self, time_str: str) -> datetime:
        """解析新闻时间"""
        if not time_str:
            return None
        
        # 尝试多种时间格式
        formats = [
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
            '%Y年%m月%d日 %H:%M:%S',
            '%Y年%m月%d日 %H:%M',
            '%Y/%m/%d %H:%M:%S',
            '%Y/%m/%d %H:%M'
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(time_str, fmt)
            except:
                continue
        
        return None
    
    def _get_time_range(self, news_list: List[Dict]) -> Tuple[datetime, datetime]:
        """获取新闻列表的实际时间范围"""
        times = []
        for news in news_list:
            time_str = self._get_news_time_str(news)
            news_time = self._parse_news_time(time_str)
            if news_time:
                times.append(news_time)
        
        if times:
            return (min(times), max(times))
        return (None, None)

