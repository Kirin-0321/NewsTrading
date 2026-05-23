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

from services.storage import CLEAN_CURATED, get_raw_store


@dataclass
class AnalysisResult:
    ok: bool = False
    report_path: Optional[str] = None
    news_count: int = 0
    time_range: Dict = field(default_factory=dict)
    result_text: Optional[str] = None
    error: Optional[str] = None
    theme_count: int = 0
    theme_error: Optional[str] = None


class AnalysisService:
    """统一分析入口。"""

    def __init__(self):
        self.raw_store = get_raw_store()

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
            source: curated 仅取 clean_status='curated' / raw 取全部
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

        status = CLEAN_CURATED if source == "curated" else None
        return self.raw_store.get_news_in_range(start, end, status=status)

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
        extract_themes: Optional[bool] = None,
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
                self._maybe_extract_themes(
                    out, progress_callback, force=extract_themes
                )
            else:
                out.error = result.get("error", "分析失败")

        except Exception as e:
            out.error = str(e)

        return out

    @staticmethod
    def _maybe_extract_themes(
        result: AnalysisResult,
        progress_callback: Optional[Callable] = None,
        force: Optional[bool] = None,
    ) -> None:
        """分析完成后自动抽取题材入库。

        Args:
            force: GUI/Agent 调用者显式覆盖；
                   None → 走 config 的 enabled/auto_run；
                   True → 强制跑（仍需 enabled=True 且 API Key 存在）；
                   False → 强制跳过。

        失败不抛异常，仅记录 result.theme_error，避免影响主流程报告产出。
        """
        if not result.ok or not result.report_path:
            return

        try:
            from core.ai_config import AIConfig
            from core.theme_extractor import ThemeExtractor, parse_report_meta
            from services.storage import get_theme_store
        except ImportError as e:
            result.theme_error = f"题材抽取模块未就绪: {e}"
            return

        try:
            ext_cfg = AIConfig().get_theme_extraction_config()
        except Exception as e:
            result.theme_error = f"读取题材抽取配置失败: {e}"
            return

        if not ext_cfg.get("enabled"):
            return
        if force is False:
            return
        if force is None and not ext_cfg.get("auto_run"):
            return

        if progress_callback:
            progress_callback("正在抽取题材并入库...")

        try:
            extractor = ThemeExtractor()
            themes, news_id_map, err = extractor.extract_from_file(
                result.report_path
            )
            if err:
                result.theme_error = err
            if not themes:
                if progress_callback:
                    progress_callback(f"题材抽取无结果: {err or '空列表'}")
                return

            meta = parse_report_meta(result.report_path)
            saved = get_theme_store().save_themes(
                meta, themes, news_id_map=news_id_map
            )
            result.theme_count = saved.get("themes", 0)
            if progress_callback:
                matched = sum(1 for n in news_id_map) if news_id_map else 0
                progress_callback(
                    f"题材入库完成: {saved['themes']} 题材 / "
                    f"{saved['stocks']} 标的 / {saved['news']} 新闻引用 "
                    f"(底部映射: {matched} 条)"
                )
        except Exception as e:
            result.theme_error = f"题材抽取入库失败: {e}"
            if progress_callback:
                progress_callback(result.theme_error)

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
