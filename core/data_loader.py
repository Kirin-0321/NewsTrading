"""
数据加载器
支持加载JSON、Markdown、TXT三种格式的数据
"""

import os
import json
import re
from typing import List, Dict, Optional


class DataLoader:
    """统一的数据加载器"""

    def __init__(self):
        self.supported_formats = ['.json', '.md', '.txt']

    def detect_format(self, file_path: str) -> str:
        """自动识别文件格式"""
        ext = os.path.splitext(file_path)[1].lower()

        if ext == '.json':
            return 'json'
        elif ext == '.md':
            return 'markdown'
        elif ext == '.txt':
            return 'txt'
        else:
            raise ValueError(f"不支持的文件格式: {ext}")

    def load(self, file_path: str) -> Dict:
        """
        根据格式自动加载数据
        返回: {
            'format': 'json/markdown/txt',
            'news_list': [...],
            'count': 100,
            'time_range': {'start': '...', 'end': '...'}
        }
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        format_type = self.detect_format(file_path)

        if format_type == 'json':
            return self.load_json(file_path)
        elif format_type == 'markdown':
            return self.load_markdown(file_path)
        elif format_type == 'txt':
            return self.load_txt(file_path)

    def load_json(self, file_path: str) -> Dict:
        """加载JSON格式"""
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # 统一格式化为新闻列表
        if isinstance(data, list):
            news_list = data
        elif isinstance(data, dict):
            news_list = data.get('news', [])
        else:
            raise ValueError("不支持的JSON结构")

        # 提取时间范围
        time_range = self._extract_time_range(news_list)

        return {
            'format': 'json',
            'news_list': news_list,
            'count': len(news_list),
            'time_range': time_range
        }

    def load_markdown(self, file_path: str) -> Dict:
        """加载Markdown格式"""
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        news_list = []

        # 解析Markdown格式
        # 匹配 ## 数字. 标题 格式
        pattern = r'##\s+\d+\.\s+(.+?)\n\n\*\*时间\*\*:\s*(.+?)\n\*\*来源\*\*:\s*(.+?)(?:\n\n\*\*内容\*\*:\n(.+?))?(?=\n---|\Z)'

        matches = re.finditer(pattern, content, re.DOTALL)

        for match in matches:
            title = match.group(1).strip()
            datetime_str = match.group(2).strip()
            source = match.group(3).strip()
            content_text = match.group(4).strip() if match.group(4) else ""

            news_list.append({
                'title': title,
                'datetime': datetime_str,
                'source': source,
                'content': content_text
            })

        # 提取时间范围
        time_range = self._extract_time_range(news_list)

        return {
            'format': 'markdown',
            'news_list': news_list,
            'count': len(news_list),
            'time_range': time_range
        }

    def load_txt(self, file_path: str) -> Dict:
        """加载TXT格式（极简标题列表）"""
        news_list = []

        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                # 解析格式: 时间 | 标题
                if ' | ' in line:
                    parts = line.split(' | ', 1)
                    if len(parts) == 2:
                        news_list.append({
                            'datetime': parts[0].strip(),
                            'title': parts[1].strip()
                        })

        # 提取时间范围
        time_range = self._extract_time_range(news_list)

        return {
            'format': 'txt',
            'news_list': news_list,
            'count': len(news_list),
            'time_range': time_range
        }

    def _extract_time_range(self, news_list: List[Dict]) -> Dict:
        """提取新闻的时间范围"""
        if not news_list:
            return {'start': None, 'end': None}

        times = []
        for news in news_list:
            time_str = news.get('datetime') or news.get('time', '')
            if time_str:
                times.append(time_str)

        if times:
            return {
                'start': min(times),
                'end': max(times)
            }
        else:
            return {'start': None, 'end': None}

    def format_for_ai(
        self,
        news_list: List[Dict],
        format_type: str,
        max_items: Optional[int] = None
    ) -> str:
        """
        格式化数据供AI分析使用
        """
        # 限制数量
        if max_items and len(news_list) > max_items:
            news_list = news_list[:max_items]

        if format_type == 'txt':
            # TXT格式：只发送时间和标题
            lines = []
            for news in news_list:
                time_str = news.get('datetime', '')
                title = news.get('title', '')
                lines.append(f"{time_str} | {title}")
            return '\n'.join(lines)

        elif format_type == 'json':
            # JSON格式：发送标题+内容
            items = []
            for news in news_list:
                time_str = news.get('datetime', '')
                title = news.get('title', '')
                content = news.get('content', '')

                item = f"【{time_str}】{title}"
                if content:
                    item += f"\n{content}"

                items.append(item)
            return '\n\n'.join(items)

        elif format_type == 'markdown':
            # Markdown格式：保持格式
            items = []
            for idx, news in enumerate(news_list, 1):
                time_str = news.get('datetime', '')
                title = news.get('title', '')
                content = news.get('content', '')

                item = f"{idx}. 【{time_str}】{title}"
                if content:
                    item += f"\n   {content}"

                items.append(item)
            return '\n\n'.join(items)

        else:
            return str(news_list)

    def estimate_tokens(self, text: str) -> int:
        """
        估算文本的token数量
        中文: 1字符 ≈ 2 tokens
        英文: 1单词 ≈ 1.3 tokens
        粗略估算: len(text) / 2
        """
        # 简单估算
        chinese_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english_count = len(text) - chinese_count

        estimated_tokens = chinese_count * 2 + english_count / 4
        return int(estimated_tokens)


if __name__ == '__main__':
    # 测试数据加载器
    loader = DataLoader()

    # 测试JSON
    json_file = 'data/raw/12-21.json'
    if os.path.exists(json_file):
        result = loader.load(json_file)
        print(f"JSON格式: {result['count']}条新闻")
        print(f"时间范围: {result['time_range']}")

        # 格式化为AI输入
        ai_input = loader.format_for_ai(
            result['news_list'][:10],
            'json'
        )
        print(f"AI输入预览:\n{ai_input[:500]}...")
        print(f"估算tokens: {loader.estimate_tokens(ai_input)}")

