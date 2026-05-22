"""
本地新闻索引辅助（基于 data/raw/*.json，非 SQLite）

说明：历史命名沿用 db_helper，实际通过 JSON 文件读写实现增量与去重。
"""

import json
import os
from glob import glob
from datetime import datetime
from typing import Optional, Dict, List


def get_latest_news_from_db() -> Optional[Dict]:
    """
    获取数据库中最新的一条新闻
    
    返回:
        Dict: 最新新闻的数据，包含 time, title, datetime等字段
        None: 数据库为空或读取失败
    """
    raw_dir = 'data/raw'
    
    if not os.path.exists(raw_dir):
        return None
    
    # 获取所有JSON文件
    files = glob(os.path.join(raw_dir, '*.json'))
    if not files:
        return None
    
    # 按修改时间排序，获取最新文件
    latest_file = max(files, key=os.path.getmtime)
    
    try:
        # 读取文件
        with open(latest_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            news_list = data.get('news', [])
            
            if news_list:
                # 第一条通常是最新的（按时间倒序）
                latest_news = news_list[0]
                print(f"[数据库] 最新新闻来自文件: {os.path.basename(latest_file)}")
                print(f"[数据库] 最新新闻时间: {latest_news.get('datetime') or latest_news.get('time', '')}")
                print(f"[数据库] 最新新闻标题: {latest_news.get('title', '')[:50]}...")
                return latest_news
    except Exception as e:
        print(f"[数据库] 读取最新新闻失败: {e}")
        return None
    
    return None


def load_all_db_news() -> List[Dict]:
    """
    加载数据库中所有新闻
    
    返回:
        List[Dict]: 所有新闻的列表
    """
    raw_dir = 'data/raw'
    all_news = []
    
    if not os.path.exists(raw_dir):
        return all_news
    
    # 获取所有JSON文件
    files = glob(os.path.join(raw_dir, '*.json'))
    
    for file_path in files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                news_list = data.get('news', [])
                all_news.extend(news_list)
        except Exception as e:
            print(f"[数据库] 读取文件 {os.path.basename(file_path)} 失败: {e}")
            continue
    
    print(f"[数据库] 总共加载 {len(all_news)} 条新闻")
    return all_news


def deduplicate_with_db(new_news_list: List[Dict]) -> tuple:
    """
    将新爬取的新闻与数据库中的新闻去重
    
    参数:
        new_news_list: 新爬取的新闻列表
    
    返回:
        tuple: (去重后的新闻列表, 统计信息dict)
    """
    from core.semantic_dedup import semantic_deduplicate
    
    print(f"[去重] 开始去重处理...")
    print(f"[去重] 新爬取新闻: {len(new_news_list)} 条")
    
    # 1. 先对新爬取的新闻进行内部去重
    internal_deduped = semantic_deduplicate(new_news_list)
    internal_removed = len(new_news_list) - len(internal_deduped)
    print(f"[去重] 内部去重: 去除 {internal_removed} 条")
    
    # 2. 加载数据库中的所有新闻
    db_news = load_all_db_news()
    
    if not db_news:
        print(f"[去重] 数据库为空，保留全部新闻")
        stats = {
            'crawled': len(new_news_list),
            'internal_duplicates': internal_removed,
            'db_duplicates': 0,
            'final_saved': len(internal_deduped)
        }
        return internal_deduped, stats
    
    # 3. 合并数据库新闻和新爬取的新闻
    all_news = db_news + internal_deduped
    
    # 4. 整体去重
    final_deduped = semantic_deduplicate(all_news)
    
    # 5. 提取只在新爬取中存在的新闻（通过id比对）
    db_ids = {str(n.get('id', '')) for n in db_news}
    new_only = [n for n in final_deduped if str(n.get('id', '')) not in db_ids]
    
    db_removed = len(internal_deduped) - len(new_only)
    print(f"[去重] 与数据库去重: 去除 {db_removed} 条")
    print(f"[去重] 最终保留: {len(new_only)} 条新增新闻")
    
    stats = {
        'crawled': len(new_news_list),
        'internal_duplicates': internal_removed,
        'db_duplicates': db_removed,
        'final_saved': len(new_only)
    }
    
    return new_only, stats


def parse_news_time(news: Dict) -> Optional[datetime]:
    """
    解析新闻时间
    
    参数:
        news: 新闻数据dict
    
    返回:
        datetime对象或None
    """
    time_str = news.get('datetime') or news.get('time', '')
    if not time_str:
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
                dt = datetime.strptime(time_str, fmt)
                # 如果没有年份，使用当前年份
                if '%Y' not in fmt:
                    dt = dt.replace(year=datetime.now().year)
                return dt
            except:
                continue
        
        return None
    except:
        return None

