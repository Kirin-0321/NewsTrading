"""
分析阶段的数据加载器。

仅支持 JSON 格式：services.analysis_service 会从 SQLite 取数后写入临时 JSON，
再交由 AINewsAnalyzer 走本加载器读取并格式化。
"""

import json
import os
from typing import Dict, List, Optional


class DataLoader:
    """JSON 新闻数据加载与 AI 输入格式化。"""

    def load(self, file_path: str) -> Dict:
        """
        加载 JSON 文件，返回:
            {
                'format': 'json',
                'news_list': [...],
                'count': int,
                'time_range': {'start': str | None, 'end': str | None}
            }
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = os.path.splitext(file_path)[1].lower()
        if ext != '.json':
            raise ValueError(f"仅支持 JSON 格式，收到: {ext}")

        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if isinstance(data, list):
            news_list = data
        elif isinstance(data, dict):
            news_list = data.get('news', [])
        else:
            raise ValueError("不支持的 JSON 结构")

        return {
            'format': 'json',
            'news_list': news_list,
            'count': len(news_list),
            'time_range': self._extract_time_range(news_list),
        }

    @staticmethod
    def _extract_time_range(news_list: List[Dict]) -> Dict:
        """从新闻列表提取最早/最晚时间。"""
        times = [
            n.get('datetime') or n.get('time', '')
            for n in news_list
            if n.get('datetime') or n.get('time')
        ]
        if not times:
            return {'start': None, 'end': None}
        return {'start': min(times), 'end': max(times)}

    def format_for_ai(
        self,
        news_list: List[Dict],
        format_type: str = 'json',
        max_items: Optional[int] = None,
    ) -> str:
        """
        将新闻列表格式化为 AI prompt 文本：「N. 【时间】标题\\n   内容」。

        重要：必须给每条新闻打 1-based 序号前缀。
        AI 在报告中以「[新闻X](#新闻X)」格式引用，下游
        AINewsAnalyzer._extract_referenced_news 通过 ``news_list[X - 1]``
        反查原文，序号必须与 news_list 索引严格对齐，否则报告
        引用与底部原文将错乱。

        Args:
            news_list: 新闻列表
            format_type: 已退化为占位参数（兼容旧签名），统一输出带编号格式
            max_items: 截断条数（可选）
        """
        if max_items and len(news_list) > max_items:
            news_list = news_list[:max_items]

        items = []
        for idx, news in enumerate(news_list, 1):
            time_str = news.get('datetime') or news.get('time', '')
            title = news.get('title', '')
            content = news.get('content', '')
            item = f"{idx}. 【{time_str}】{title}"
            if content:
                item += f"\n   {content}"
            items.append(item)
        return '\n\n'.join(items)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """
        粗略估算 token 数：中文 ≈ 2 token/字，英文 ≈ 0.25 token/字符。
        """
        chinese_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        english_count = len(text) - chinese_count
        return int(chinese_count * 2 + english_count / 4)


if __name__ == '__main__':
    from services.storage import get_raw_store

    news_list = get_raw_store().get_all_news()[:10]
    if news_list:
        loader = DataLoader()
        ai_input = loader.format_for_ai(news_list)
        print(f"SQLite 原始库抽样: {len(news_list)} 条")
        print(f"AI输入预览:\n{ai_input[:500]}...")
        print(f"估算tokens: {loader.estimate_tokens(ai_input)}")
    else:
        print("原始库为空，跳过测试")
