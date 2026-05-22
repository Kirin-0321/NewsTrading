"""
数据合并去重器
用于合并多个清洗后的文件
"""

import json
import os
from datetime import datetime
from typing import List, Dict
from collections import defaultdict


class DataMerger:
    """数据合并去重器"""
    
    @staticmethod
    def merge_cleaned_files(
        file_paths: List[str],
        output_dir: str = 'data/cleaned'
    ) -> Dict:
        """
        合并多个清洗后的文件（只合并_clear.json）
        
        参数:
            file_paths: 文件路径列表（只接受_clear.json文件）
            output_dir: 输出目录
            
        返回:
            包含合并结果的字典
        """
        all_news = []
        source_files = []
        
        # 加载所有文件
        for file_path in file_paths:
            # 只处理clear文件
            if '_clear.json' not in os.path.basename(file_path):
                print(f"跳过非清洗文件: {file_path}")
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # 提取新闻数据
                    if isinstance(data, dict) and 'news' in data:
                        all_news.extend(data['news'])
                    elif isinstance(data, list):
                        all_news.extend(data)
                    
                    source_files.append(os.path.basename(file_path))
                    
            except Exception as e:
                print(f"加载文件失败 {file_path}: {e}")
                continue
        
        if not all_news:
            return {
                'success': False,
                'message': '没有可合并的新闻数据'
            }
        
        # 去重（基于标题+时间）
        merged_count = len(all_news)
        unique_news = DataMerger._deduplicate_news(all_news)
        final_count = len(unique_news)
        duplicates_removed = merged_count - final_count
        
        # 按时间排序
        unique_news.sort(key=lambda x: x.get('time', ''))
        
        # 计算时间范围
        all_times = [news.get('time', '') for news in unique_news if news.get('time')]
        start_time = min(all_times) if all_times else None
        end_time = max(all_times) if all_times else None
        
        # 生成文件名：月-日_月-日_clear.json
        try:
            if start_time and end_time:
                start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                end_dt = datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
                
                # 只使用月-日
                filename = f"{start_dt.strftime('%m-%d')}_{end_dt.strftime('%m-%d')}_clear.json"
            else:
                # 如果没有时间信息，使用当前时间
                filename = f"{datetime.now().strftime('%m-%d')}_merged_clear.json"
        except Exception as e:
            print(f"时间解析失败: {e}")
            filename = f"{datetime.now().strftime('%m-%d-%H-%M')}_merged_clear.json"
        
        # 保存合并结果
        os.makedirs(output_dir, exist_ok=True)
        output_file = os.path.join(output_dir, filename)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump({
                'metadata': {
                    'type': 'cleaned_merged',
                    'source_files': source_files,
                    'merged_count': merged_count,
                    'duplicates_removed': duplicates_removed,
                    'final_count': final_count,
                    'merge_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'time_range': {
                        'start': start_time,
                        'end': end_time
                    }
                },
                'news': unique_news
            }, f, ensure_ascii=False, indent=2)
        
        return {
            'success': True,
            'output_file': output_file,
            'merged_count': len(source_files),
            'total_count': merged_count,
            'unique_count': final_count,
            'removed_duplicates': duplicates_removed
        }
    
    @staticmethod
    def _deduplicate_news(news_list: List[Dict]) -> List[Dict]:
        """
        去重新闻（使用 semantic_dedup 默认：30 分钟窗口 + 50% 相似度）
        """
        from core.semantic_dedup import semantic_deduplicate
        return semantic_deduplicate(news_list)
    
    @staticmethod
    def get_mergeable_files(directory: str = 'data/cleaned') -> List[str]:
        """
        获取可合并的文件列表（只返回_clear.json文件）
        
        返回:
            文件路径列表
        """
        if not os.path.exists(directory):
            return []
        
        mergeable_files = []
        
        for filename in os.listdir(directory):
            if filename.endswith('_clear.json'):
                file_path = os.path.join(directory, filename)
                mergeable_files.append(file_path)
        
        # 按文件名排序
        mergeable_files.sort()
        
        return mergeable_files
    
    @staticmethod
    def get_file_info(file_path: str) -> Dict:
        """
        获取文件信息
        
        返回:
            {
                'filename': '...',
                'count': 100,
                'size': '1.2MB',
                'time_range': '2025-12-20 ~ 2025-12-23'
            }
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 提取新闻数据
            if isinstance(data, dict) and 'news' in data:
                news = data['news']
                metadata = data.get('metadata', {})
            elif isinstance(data, list):
                news = data
                metadata = {}
            else:
                news = []
                metadata = {}
            
            # 计算文件大小
            file_size = os.path.getsize(file_path)
            size_mb = file_size / (1024 * 1024)
            size_str = f"{size_mb:.1f}MB" if size_mb >= 1 else f"{file_size / 1024:.0f}KB"
            
            # 时间范围
            time_range_info = metadata.get('time_range', {})
            start = time_range_info.get('start', '')
            end = time_range_info.get('end', '')
            
            if start and end:
                # 提取月-日
                try:
                    start_dt = datetime.strptime(start, '%Y-%m-%d %H:%M:%S')
                    end_dt = datetime.strptime(end, '%Y-%m-%d %H:%M:%S')
                    time_range = f"{start_dt.strftime('%m-%d')} ~ {end_dt.strftime('%m-%d')}"
                except:
                    time_range = f"{start} ~ {end}"
            else:
                time_range = "未知"
            
            return {
                'filename': os.path.basename(file_path),
                'count': len(news),
                'size': size_str,
                'time_range': time_range,
                'metadata': metadata
            }
            
        except Exception as e:
            return {
                'filename': os.path.basename(file_path),
                'count': 0,
                'size': '0KB',
                'time_range': '未知',
                'error': str(e)
            }
    
    @staticmethod
    def merge_and_split_by_date(
        file_paths: List[str],
        output_dir: str = 'data/cleaned'
    ) -> Dict:
        """
        合并所有清洗文件，全局去重后按天分割保存
        
        参数:
            file_paths: 文件路径列表（只接受_clear.json文件）
            output_dir: 输出目录
            
        返回:
            {
                'success': True/False,
                'message': '...',
                'total_files': 处理的文件数,
                'total_news_before': 合并前总数,
                'total_news_after': 去重后总数,
                'duplicates_removed': 去除的重复数,
                'days_count': 分割的天数,
                'daily_files': [
                    {'date': '12-23', 'filename': '12-23_clear.json', 'count': 485},
                    ...
                ],
                'deleted_files': [...],
                'overwritten_files': [...]
            }
        """
        print("\n" + "="*60)
        print("开始合并并按天分割清洗数据...")
        print("="*60)
        
        all_news = []
        source_files = []
        
        # 1. 加载所有文件
        print("\n[1/5] 加载文件...")
        for file_path in file_paths:
            # 只处理clear文件
            if '_clear.json' not in os.path.basename(file_path):
                print(f"  跳过: {os.path.basename(file_path)}")
                continue
            
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    
                    # 提取新闻数据
                    if isinstance(data, dict) and 'news' in data:
                        news_list = data['news']
                    elif isinstance(data, list):
                        news_list = data
                    else:
                        news_list = []
                    
                    all_news.extend(news_list)
                    source_files.append(os.path.basename(file_path))
                    print(f"  ✓ {os.path.basename(file_path)}: {len(news_list)} 条")
                    
            except Exception as e:
                print(f"  ✗ {os.path.basename(file_path)}: 加载失败 - {e}")
                continue
        
        if not all_news:
            return {
                'success': False,
                'message': '没有可处理的新闻数据'
            }
        
        total_before = len(all_news)
        print(f"\n  合并前总数: {total_before} 条")
        
        # 2. 全局去重
        print("\n[2/5] 全局去重（30分钟窗口 + 50%相似度）...")
        unique_news = DataMerger._deduplicate_news(all_news)
        total_after = len(unique_news)
        duplicates = total_before - total_after
        print(f"  去重前: {total_before} 条")
        print(f"  去重后: {total_after} 条")
        print(f"  去除重复: {duplicates} 条 ({duplicates/total_before*100:.1f}%)")
        
        # 3. 按日期分组
        print("\n[3/5] 按日期分组...")
        news_by_date = defaultdict(list)
        
        for news in unique_news:
            # 获取时间字符串
            time_str = news.get('datetime') or news.get('time', '')
            
            if time_str:
                try:
                    # 解析日期
                    formats = ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%m-%d %H:%M']
                    news_date = None
                    
                    for fmt in formats:
                        try:
                            dt = datetime.strptime(time_str, fmt)
                            if '%Y' not in fmt:
                                dt = dt.replace(year=datetime.now().year)
                            news_date = dt.strftime('%m-%d')
                            break
                        except:
                            continue
                    
                    if news_date:
                        news_by_date[news_date].append(news)
                    else:
                        # 无法解析日期，放入unknown
                        news_by_date['unknown'].append(news)
                        
                except Exception as e:
                    news_by_date['unknown'].append(news)
            else:
                news_by_date['unknown'].append(news)
        
        print(f"  分组结果: {len(news_by_date)} 个日期")
        for date, news_list in sorted(news_by_date.items()):
            print(f"    {date}: {len(news_list)} 条")
        
        # 4. 保存分割后的文件
        print("\n[4/5] 保存分割后的文件...")
        os.makedirs(output_dir, exist_ok=True)
        
        daily_files = []
        overwritten_files = []
        
        for date, news_list in sorted(news_by_date.items()):
            if date == 'unknown':
                filename = 'unknown_clear.json'
            else:
                filename = f"{date}_clear.json"
            
            filepath = os.path.join(output_dir, filename)
            
            # 检查是否覆盖
            if os.path.exists(filepath):
                overwritten_files.append(filename)
            
            # 按时间排序
            news_list.sort(key=lambda x: x.get('datetime') or x.get('time', ''))
            
            # 计算时间范围
            all_times = [n.get('datetime') or n.get('time', '') for n in news_list if n.get('datetime') or n.get('time')]
            start_time = min(all_times) if all_times else None
            end_time = max(all_times) if all_times else None
            
            # 保存文件
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump({
                    'metadata': {
                        'type': 'cleaned_split_by_date',
                        'date': date,
                        'source_files': source_files,
                        'total_before_dedup': total_before,
                        'count': len(news_list),
                        'split_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'time_range': {
                            'start': start_time,
                            'end': end_time
                        }
                    },
                    'news': news_list
                }, f, ensure_ascii=False, indent=2)
            
            daily_files.append({
                'date': date,
                'filename': filename,
                'count': len(news_list)
            })
            
            print(f"  ✓ {filename}: {len(news_list)} 条")
        
        # 5. 删除旧文件（不删除_removed.json，不删除新生成的文件）
        print("\n[5/5] 清理旧文件...")
        deleted_files = []
        
        # 生成新文件名列表，避免删除刚生成的文件
        new_filenames = set([item['filename'] for item in daily_files])
        
        for file_path in file_paths:
            filename = os.path.basename(file_path)
            # 只删除_clear.json文件，不删除_removed.json，不删除新生成的文件
            if '_clear.json' in filename and filename not in new_filenames and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted_files.append(filename)
                    print(f"  ✓ 已删除: {filename}")
                except Exception as e:
                    print(f"  ✗ 删除失败: {filename} - {e}")
        
        print("\n" + "="*60)
        print("✅ 按天分割完成！")
        print("="*60)
        
        return {
            'success': True,
            'message': '按天分割成功',
            'total_files': len(source_files),
            'total_news_before': total_before,
            'total_news_after': total_after,
            'duplicates_removed': duplicates,
            'days_count': len(news_by_date),
            'daily_files': daily_files,
            'deleted_files': deleted_files,
            'overwritten_files': overwritten_files
        }

