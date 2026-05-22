"""
语义去重工具
使用时间窗口 + 标题相似度进行智能去重
"""

from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Dict, Optional


class SemanticDeduplicator:
    """语义去重器"""
    
    def __init__(self, time_window_minutes=10, similarity_threshold=0.8):
        """
        初始化语义去重器
        
        参数:
            time_window_minutes: 时间窗口（分钟）
            similarity_threshold: 相似度阈值（0-1）
        """
        self.time_window_minutes = time_window_minutes
        self.similarity_threshold = similarity_threshold
    
    @staticmethod
    def calculate_similarity(str1: str, str2: str) -> float:
        """
        计算两个字符串的相似度（0-1之间）
        使用SequenceMatcher算法
        """
        return SequenceMatcher(None, str1, str2).ratio()
    
    @staticmethod
    def parse_datetime(dt_str: str) -> Optional[datetime]:
        """解析日期时间字符串"""
        if not dt_str:
            return None
        
        try:
            # 尝试多种时间格式
            formats = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%m-%d %H:%M',
            ]
            
            for fmt in formats:
                try:
                    dt = datetime.strptime(dt_str, fmt)
                    # 如果没有年份，使用当前年份
                    if '%Y' not in fmt:
                        dt = dt.replace(year=datetime.now().year)
                    return dt
                except:
                    continue
            
            return None
        except:
            return None
    
    def deduplicate(self, news_list: List[Dict]) -> List[Dict]:
        """
        语义去重
        
        参数:
            news_list: 新闻列表
        
        返回:
            去重后的新闻列表
        """
        if not news_list:
            return []
        
        # 1. 提取时间并排序
        news_with_time = []
        news_without_time = []
        
        for news in news_list:
            # 获取时间字符串（优先datetime，其次time）
            time_str = news.get('datetime') or news.get('time', '')
            dt = self.parse_datetime(time_str)
            
            if dt:
                news_with_time.append((news, dt))
            else:
                # 没有时间的新闻单独处理
                news_without_time.append(news)
        
        # 按时间排序
        news_with_time.sort(key=lambda x: x[1])
        
        # 2. 时间窗口 + 相似度去重
        unique_news = []
        
        for i, (current_news, current_time) in enumerate(news_with_time):
            current_title = current_news.get('title', '')
            
            # 检查是否与前面的新闻重复
            is_duplicate = False
            
            # 向前查找时间窗口内的新闻
            for j in range(i - 1, -1, -1):
                prev_news, prev_time = news_with_time[j]
                
                # 超出时间窗口，停止查找
                time_diff_minutes = (current_time - prev_time).total_seconds() / 60
                if time_diff_minutes > self.time_window_minutes:
                    break
                
                # 计算标题相似度
                prev_title = prev_news.get('title', '')
                similarity = self.calculate_similarity(current_title, prev_title)
                
                # 如果相似度超过阈值，判定为重复
                if similarity >= self.similarity_threshold:
                    is_duplicate = True
                    break
            
            # 不重复则保留
            if not is_duplicate:
                unique_news.append(current_news)
        
        # 3. 添加没有时间的新闻（这些新闻无法语义去重，保持原样）
        unique_news.extend(news_without_time)
        
        return unique_news
    
    def deduplicate_with_stats(self, news_list: List[Dict]) -> tuple:
        """
        语义去重并返回统计信息
        
        参数:
            news_list: 新闻列表
        
        返回:
            (去重后的新闻列表, 原始数量, 去重后数量, 去除数量)
        """
        original_count = len(news_list)
        unique_news = self.deduplicate(news_list)
        final_count = len(unique_news)
        removed_count = original_count - final_count
        
        return unique_news, original_count, final_count, removed_count


# 全局默认去重器实例（30 分钟窗口 + 50% 标题相似度）
default_deduplicator = SemanticDeduplicator(
    time_window_minutes=30,
    similarity_threshold=0.5,
)


def semantic_deduplicate(news_list: List[Dict]) -> List[Dict]:
    """
    便捷函数：使用默认配置进行语义去重
    
    参数:
        news_list: 新闻列表
    
    返回:
        去重后的新闻列表
    """
    return default_deduplicator.deduplicate(news_list)


def semantic_deduplicate_with_stats(news_list: List[Dict]) -> tuple:
    """
    便捷函数：使用默认配置进行语义去重并返回统计
    
    参数:
        news_list: 新闻列表
    
    返回:
        (去重后的新闻列表, 原始数量, 去重后数量, 去除数量)
    """
    return default_deduplicator.deduplicate_with_stats(news_list)

