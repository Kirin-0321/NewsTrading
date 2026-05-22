"""
语义去重工具
时间窗口 + 标题/标题+正文 双路相似度（阈值可分别设置）
"""

import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple


class SemanticDeduplicator:
    """语义去重器"""

    def __init__(
        self,
        time_window_minutes: int = 30,
        title_threshold: float = 0.60,
        merged_threshold: float = 0.55,
        similarity_threshold: Optional[float] = None,
    ):
        """
        Args:
            time_window_minutes: 时间窗口（分钟）
            title_threshold: 标题相似度阈值 0~1
            merged_threshold: 标题+正文合并相似度阈值 0~1
            similarity_threshold: 兼容旧参数，若传入则两路共用同一阈值
        """
        self.time_window_minutes = time_window_minutes
        if similarity_threshold is not None:
            self.title_threshold = similarity_threshold
            self.merged_threshold = similarity_threshold
        else:
            self.title_threshold = title_threshold
            self.merged_threshold = merged_threshold

    @property
    def similarity_threshold(self) -> float:
        """兼容旧代码读取单一阈值。"""
        return self.title_threshold

    @staticmethod
    def calculate_similarity(str1: str, str2: str) -> float:
        """计算两个字符串的相似度（SequenceMatcher，0~1）。"""
        if not str1 or not str2:
            return 0.0
        return SequenceMatcher(None, str1, str2).ratio()

    @staticmethod
    def _normalize_text(text: str) -> str:
        """压缩空白，便于比较。"""
        if not text:
            return ""
        return re.sub(r"\s+", " ", str(text).strip())

    def _has_content(self, news: Dict) -> bool:
        """是否有正文（非空）。"""
        return bool(self._normalize_text(news.get("content", "")))

    def _merged_text(self, news: Dict) -> str:
        """标题 + 正文前缀合并文本。"""
        title = self._normalize_text(news.get("title", ""))
        content = self._normalize_text(news.get("content", ""))
        if content:
            content = content[:500]
        if title and content:
            return f"{title} {content}"
        return title or content

    def _news_time(self, news: Dict) -> Optional[datetime]:
        time_str = news.get("datetime") or news.get("time", "")
        if not time_str:
            ts = news.get("timestamp")
            if ts:
                try:
                    return datetime.fromtimestamp(int(ts))
                except (TypeError, ValueError, OSError):
                    return None
            return None

        formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M"]
        for fmt in formats:
            try:
                dt = datetime.strptime(time_str, fmt)
                if "%Y" not in fmt:
                    dt = dt.replace(year=datetime.now().year)
                return dt
            except ValueError:
                continue
        return None

    def _in_time_window(self, time_a: datetime, time_b: datetime) -> bool:
        diff_minutes = abs((time_a - time_b).total_seconds()) / 60
        return diff_minutes <= self.time_window_minutes

    def is_similar(self, news_a: Dict, news_b: Dict) -> bool:
        """
        判断两条新闻是否相似。
        - 标题相似度 >= title_threshold → 重复
        - 双方均有正文且合并相似度 >= merged_threshold → 重复
        """
        title_a = self._normalize_text(news_a.get("title", ""))
        title_b = self._normalize_text(news_b.get("title", ""))
        if title_a and title_b:
            if self.calculate_similarity(title_a, title_b) >= self.title_threshold:
                return True

        if not (self._has_content(news_a) and self._has_content(news_b)):
            return False

        merge_a = self._merged_text(news_a)
        merge_b = self._merged_text(news_b)
        if merge_a and merge_b:
            if self.calculate_similarity(merge_a, merge_b) >= self.merged_threshold:
                return True
        return False

    def similarity_detail(self, news_a: Dict, news_b: Dict) -> Dict:
        """返回两条新闻的相似度明细。"""
        title_a = self._normalize_text(news_a.get("title", ""))
        title_b = self._normalize_text(news_b.get("title", ""))
        merge_a = self._merged_text(news_a)
        merge_b = self._merged_text(news_b)

        title_sim = self.calculate_similarity(title_a, title_b) if title_a and title_b else 0.0
        both_have_content = self._has_content(news_a) and self._has_content(news_b)
        merge_sim = (
            self.calculate_similarity(merge_a, merge_b)
            if both_have_content and merge_a and merge_b
            else 0.0
        )

        reasons = []
        if title_sim >= self.title_threshold:
            reasons.append("title")
        if both_have_content and merge_sim >= self.merged_threshold:
            reasons.append("merged")

        return {
            "title_similarity": round(title_sim, 4),
            "merged_similarity": round(merge_sim, 4),
            "is_duplicate": bool(reasons),
            "match_reason": "+".join(reasons) if reasons else "",
        }

    def is_duplicate_of_any(
        self,
        news: Dict,
        news_time: datetime,
        others: List[Tuple[Dict, datetime]],
    ) -> bool:
        """是否与给定列表中（时间窗口内）任一条重复。"""
        for other, other_time in others:
            if not self._in_time_window(news_time, other_time):
                continue
            if self.is_similar(news, other):
                return True
        return False

    def deduplicate(
        self,
        news_list: List[Dict],
        reference_pool: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """
        语义去重。

        Args:
            news_list: 待去重列表
            reference_pool: 参照池（如库内已有新闻），与之相似者会被剔除
        """
        if not news_list:
            return []

        ref_with_time: List[Tuple[Dict, datetime]] = []
        for item in reference_pool or []:
            dt = self._news_time(item)
            if dt:
                ref_with_time.append((item, dt))

        news_with_time: List[Tuple[Dict, datetime]] = []
        news_without_time: List[Dict] = []

        for news in news_list:
            dt = self._news_time(news)
            if dt:
                news_with_time.append((news, dt))
            else:
                news_without_time.append(news)

        news_with_time.sort(key=lambda x: x[1])

        unique_news: List[Dict] = []
        kept_with_time: List[Tuple[Dict, datetime]] = list(ref_with_time)

        for current_news, current_time in news_with_time:
            if self.is_duplicate_of_any(current_news, current_time, kept_with_time):
                continue
            unique_news.append(current_news)
            kept_with_time.append((current_news, current_time))

        unique_news.extend(news_without_time)
        return unique_news

    def deduplicate_with_stats(
        self,
        news_list: List[Dict],
        reference_pool: Optional[List[Dict]] = None,
    ) -> tuple:
        original_count = len(news_list)
        unique_news = self.deduplicate(news_list, reference_pool=reference_pool)
        final_count = len(unique_news)
        removed_count = original_count - final_count
        return unique_news, original_count, final_count, removed_count


default_deduplicator = SemanticDeduplicator(
    time_window_minutes=30,
    title_threshold=0.60,
    merged_threshold=0.55,
)


def semantic_deduplicate(
    news_list: List[Dict],
    reference_pool: Optional[List[Dict]] = None,
) -> List[Dict]:
    """使用默认配置进行语义去重。"""
    return default_deduplicator.deduplicate(news_list, reference_pool=reference_pool)


def semantic_deduplicate_with_stats(
    news_list: List[Dict],
    reference_pool: Optional[List[Dict]] = None,
) -> tuple:
    """语义去重并返回统计。"""
    return default_deduplicator.deduplicate_with_stats(
        news_list, reference_pool=reference_pool
    )
