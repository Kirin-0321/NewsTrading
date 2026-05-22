"""
③ 新闻分析服务：从 SQLite 读取 → AI 分析 → 报告
GUI 与 Agent 共用此入口。
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Literal, Optional

from services.storage import get_raw_store, get_curated_store


@dataclass
class AnalysisResult:
    ok: bool = False
    report_path: Optional[str] = None
    news_count: int = 0
    time_range: Dict = field(default_factory=dict)
    result_text: Optional[str] = None
    error: Optional[str] = None


class AnalysisService:
    """统一分析入口。"""

    def __init__(self):
        self.raw_store = get_raw_store()
        self.curated_store = get_curated_store()

    def load_news(
        self,
        source: Literal["curated", "raw"] = "curated",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        hours: Optional[int] = None,
    ) -> List[Dict]:
        """
        按数据源与时间范围加载新闻。

        Args:
            source: curated 精选库 / raw 原始库
            start: 起始时间
            end: 结束时间
            hours: 若未指定 start/end，则取最近 N 小时
        """
        if end is None:
            end = datetime.now()
        if start is None:
            if hours is not None:
                start = end - timedelta(hours=hours)
            else:
                start = end - timedelta(hours=24)

        store = self.curated_store if source == "curated" else self.raw_store
        return store.get_news_in_range(start, end)

    def analyze(
        self,
        source: Literal["curated", "raw"] = "curated",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        hours: Optional[int] = None,
        template_id: Optional[str] = None,
        provider: Optional[str] = None,
        max_sectors=6,
        stocks_per_sector=5,
        max_news: Optional[int] = None,
        market_summary: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
    ) -> AnalysisResult:
        """
        从数据库加载新闻并执行 AI 分析。

        Returns:
            AnalysisResult
        """
        out = AnalysisResult()
        try:
            news_list = self.load_news(source, start, end, hours)
            if not news_list:
                out.error = "选定时间范围内没有新闻数据"
                return out

            if max_news and len(news_list) > max_news:
                news_list = news_list[:max_news]

            out.news_count = len(news_list)
            times = [
                n.get("datetime") or n.get("time", "")
                for n in news_list
                if n.get("datetime") or n.get("time")
            ]
            out.time_range = {
                "start": min(times) if times else "",
                "end": max(times) if times else "",
            }

            temp_path = self._write_temp_json(news_list, source)
            try:
                from core.ai_news_analyzer import AINewsAnalyzer

                analyzer = AINewsAnalyzer()
                result = analyzer.analyze(
                    file_path=temp_path,
                    provider=provider,
                    max_sectors=max_sectors,
                    stocks_per_sector=stocks_per_sector,
                    template_id=template_id,
                    market_summary=market_summary,
                    progress_callback=progress_callback,
                )
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            if result.get("success"):
                out.ok = True
                out.report_path = result.get("report_file")
                out.result_text = result.get("result")
                out.time_range = result.get("time_range") or out.time_range
            else:
                out.error = result.get("error", "分析失败")

        except Exception as e:
            out.error = str(e)

        return out

    @staticmethod
    def _write_temp_json(news_list: List[Dict], source: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".json", prefix=f"analyze_{source}_")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "source": source,
                    "export_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "total": len(news_list),
                    "news": news_list,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return path


def analyze_news(**kwargs) -> AnalysisResult:
    return AnalysisService().analyze(**kwargs)
