"""
③ 新闻分析服务：从 SQLite 读取 → AI 分析 → 报告
GUI 与 Agent 共用此入口。
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Literal, Optional

from services.storage import CLEAN_CURATED, get_raw_store
from core.ai_news_analyzer import AnalysisCancelledError

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    ok: bool = False
    report_path: Optional[str] = None
    news_count: int = 0
    time_range: Dict = field(default_factory=dict)
    result_text: Optional[str] = None
    error: Optional[str] = None
    cancelled: bool = False
    theme_count: int = 0
    theme_error: Optional[str] = None
    report_id: Optional[int] = None
    market_summary_used: bool = False


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
        auto_market: bool = False,
        market_trade_date: Optional[str] = None,
        market_mode: str = "hybrid",
        extract_themes: Optional[bool] = None,
        enable_deep_thinking: bool = True,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable] = None,
        simulated_trade_date: Optional[str] = None,
    ) -> AnalysisResult:
        """
        从数据库加载新闻并执行 AI 分析。

        Args:
            simulated_trade_date: 虚拟回测专用。YYYYMMDD 格式。
                - None（默认）：真实生成，文件名/入库 report_date 都用当前时间
                - 给定（如 ``"20260522"``）：
                    * 报告文件名用 ``{月}月{日}日_0时00分_..._backtest.md`` 命名
                    * ``ai_reports.is_backtest = 1``、``report_date = 该日期``
                    * 题材入库（theme_predictions）的 report_date 同步

        Returns:
            AnalysisResult
        """
        out = AnalysisResult()
        try:
            # 可选：自动拉取盘后总结（用户手填的 market_summary 优先）
            if (
                auto_market
                and not (market_summary and market_summary.strip())
            ):
                market_summary = self._auto_fetch_market_summary(
                    out,
                    trade_date=market_trade_date,
                    mode=market_mode,
                    cancel_check=cancel_check,
                    progress_callback=progress_callback,
                )

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

            # 回测命名时间：模拟交易日 00:00（前缀显示该日期，便于人辨识）
            # 回测产物附加 _backtest_{template_id}_{HHMMSS} 后缀：
            #   - {template_id} 避免同日多模板互相覆盖
            #   - {HHMMSS} 真实生成时间，避免同日同模板多次跑互相覆盖
            naming_dt: Optional[datetime] = None
            backtest_suffix = False
            extra_suffix = ""
            if simulated_trade_date:
                if not (
                    len(simulated_trade_date) == 8
                    and simulated_trade_date.isdigit()
                ):
                    out.error = (
                        f"simulated_trade_date 应为 YYYYMMDD，得到 "
                        f"{simulated_trade_date!r}"
                    )
                    return out
                naming_dt = datetime.strptime(
                    simulated_trade_date + "0000", "%Y%m%d%H%M"
                )
                backtest_suffix = True
                sanitized_tpl = self._sanitize_template_id(template_id)
                run_stamp = datetime.now().strftime("%H%M%S")
                # 拼装规则：tpl 为空时只保留 HHMMSS；都有时下划线连接
                extra_suffix = (
                    f"{sanitized_tpl}_{run_stamp}"
                    if sanitized_tpl else run_stamp
                )

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
                    enable_deep_thinking=enable_deep_thinking,
                    cancel_check=cancel_check,
                    naming_dt=naming_dt,
                    backtest_suffix=backtest_suffix,
                    extra_suffix=extra_suffix,
                )
            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            if result.get("cancelled"):
                out.cancelled = True
                out.error = result.get("error", "用户已终止分析")
                return out

            if result.get("success"):
                out.ok = True
                out.report_path = result.get("report_file")
                out.result_text = result.get("result")
                out.time_range = result.get("time_range") or out.time_range
                # M4.4 / M4.5 — 先入 ai_reports 索引，便于后续 Eval/回测
                self._record_ai_report(
                    out,
                    provider=provider,
                    template_id=template_id,
                    market_trade_date=market_trade_date,
                    auto_market_used=bool(
                        market_summary and market_summary.strip()
                    ),
                    simulated_trade_date=simulated_trade_date,
                )
                self._maybe_extract_themes(
                    out,
                    progress_callback,
                    force=extract_themes,
                    cancel_check=cancel_check,
                    simulated_trade_date=simulated_trade_date,
                )
                # 题材抽取若成功，回写 theme_extracted=1
                if out.theme_count and out.report_path:
                    try:
                        from services.storage import get_ai_reports_store

                        get_ai_reports_store().mark_theme_extracted(
                            out.report_path, True
                        )
                    except Exception as e:
                        logger.warning(
                            "ai_reports.mark_theme_extracted 失败: %s", e
                        )
            else:
                out.error = result.get("error", "分析失败")

        except AnalysisCancelledError as e:
            out.cancelled = True
            out.error = str(e)
        except Exception as e:
            out.error = str(e)

        return out

    @staticmethod
    def _sanitize_template_id(template_id: Optional[str]) -> str:
        """把 ``template_id`` 清洗成文件名安全片段。

        - None / 空 → 返回 ``""``（调用方按"无 template"处理）
        - 仅保留 ``[a-zA-Z0-9_-]``，其它字符全部丢弃
        - 截断到 64 字符，避免 Windows MAX_PATH 风险

        典型回测 template_id 形如 ``custom_1770292858`` / ``speculator_scalper``
        本身就是字母数字下划线，不会损失信息；防御主要是兜底未来 UI 自定义模板
        允许中文/空格名字时不会污染文件系统。
        """
        if not template_id:
            return ""
        import re
        cleaned = re.sub(r"[^A-Za-z0-9_-]", "", template_id)
        return cleaned[:64]

    @staticmethod
    def _record_ai_report(
        out: AnalysisResult,
        *,
        provider: Optional[str],
        template_id: Optional[str],
        market_trade_date: Optional[str],
        auto_market_used: bool,
        simulated_trade_date: Optional[str] = None,
    ) -> None:
        """把刚生成的 AI 报告索引化到 ``ai_reports`` 表（M4.4/M4.5）。

        Args:
            simulated_trade_date: YYYYMMDD；给定时表示这是虚拟回测产物，
                ``ai_reports.report_date = 该值`` + ``is_backtest = 1``。
                为 None 时按 ``datetime.now()`` 的 YYYYMMDD 入库 + ``is_backtest = 0``。

        失败仅写 logger.warning，不影响主分析流程。
        """
        if not out.report_path:
            return
        try:
            from core.ai_config import AIConfig
            from core.prompt_loader import PromptError, get_loader
            from services.storage import record_report
        except ImportError as e:
            logger.warning("ai_reports 索引模块缺失: %s", e)
            return

        # provider / model
        provider = provider or "openai"
        try:
            cfg = AIConfig()
            current_provider = provider or cfg.get_current_provider()
            provider_cfg = cfg.get_provider_config(current_provider) or {}
            model = str(provider_cfg.get("model") or "")
        except Exception:
            current_provider, model = provider, ""

        # prompt_id / version
        prompt_id = template_id or ""
        prompt_version = ""
        if prompt_id:
            try:
                tmpl = get_loader().get("analysis", prompt_id)
                prompt_version = str(tmpl.version or "")
            except PromptError:
                pass
            except Exception:
                pass

        # report_date：回测产物用模拟交易日；真实生成用当前日期
        # 协议：统一 YYYYMMDD（与 schema 一致）
        report_date = simulated_trade_date or datetime.now().strftime("%Y%m%d")
        is_backtest = bool(simulated_trade_date)

        # 时间范围
        tr = out.time_range or {}
        news_start = (str(tr.get("start") or "") or None)
        news_end = (str(tr.get("end") or "") or None)

        # 是否用了 market_summary
        used_market_date = (
            market_trade_date or "auto"
        ) if auto_market_used else None
        out.market_summary_used = auto_market_used

        try:
            rid = record_report(
                file_path=out.report_path,
                report_date=report_date,
                provider=current_provider,
                model=model or None,
                prompt_category="analysis",
                prompt_id=prompt_id or None,
                prompt_version=prompt_version or None,
                news_range_start=news_start,
                news_range_end=news_end,
                news_count=out.news_count or None,
                used_market_date=used_market_date,
                theme_extracted=False,
                is_backtest=is_backtest,
            )
            if rid:
                out.report_id = rid
                logger.info(
                    "ai_reports 已索引 id=%s path=%s",
                    rid, out.report_path,
                )
        except Exception as e:
            logger.warning("ai_reports 写入失败: %s", e)

    @staticmethod
    def _auto_fetch_market_summary(
        result: AnalysisResult,
        *,
        trade_date: Optional[str],
        mode: str,
        cancel_check: Optional[Callable[[], bool]],
        progress_callback: Optional[Callable],
    ) -> Optional[str]:
        """``analyze(auto_market=True)`` 时调 MarketSummaryService.build()。

        失败不阻塞主分析，只往 progress 写一行警告，
        返回 None 让后续 analyze 继续走「仅基于新闻」路径。
        """
        try:
            from services.market.service import MarketSummaryService

            if progress_callback:
                progress_callback(
                    "正在自动获取盘后数据（模式 {}）...".format(mode)
                )
            svc = MarketSummaryService()
            ms = svc.build(
                trade_date=trade_date,
                mode=mode,
                cancel_check=cancel_check,
                progress_callback=(
                    (lambda msg: progress_callback(
                        f"[盘后] {msg}"
                    )) if progress_callback else None
                ),
            )
            if ms.ok and ms.summary_md:
                if progress_callback:
                    progress_callback(
                        "已自动获取盘后数据（完整度 "
                        f"{ms.completeness * 100:.1f}%, "
                        f"用时 {ms.elapsed_ms / 1000:.1f}s）"
                    )
                return ms.summary_md
            if progress_callback:
                progress_callback(
                    "⚠️ 自动获取盘后数据失败: "
                    f"{ms.error or '未知错误'}（继续仅基于新闻分析）"
                )
        except Exception as e:
            if progress_callback:
                progress_callback(
                    f"⚠️ 盘后数据模块加载失败: {e}（继续仅基于新闻分析）"
                )
        return None

    @staticmethod
    def _maybe_extract_themes(
        result: AnalysisResult,
        progress_callback: Optional[Callable] = None,
        force: Optional[bool] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        simulated_trade_date: Optional[str] = None,
    ) -> None:
        """分析完成后自动抽取题材入库。

        Args:
            force: GUI/Agent 调用者显式覆盖；
                   None → 走 config 的 enabled/auto_run；
                   True → 强制跑（仍需 enabled=True 且 API Key 存在）；
                   False → 强制跳过。
            simulated_trade_date: 虚拟回测专用，YYYYMMDD。给定时覆盖
                ``parse_report_meta`` 推断的 ``report_date``，并把
                ``is_backtest`` 置为 True 入库到 ``theme_predictions``。

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

        from core.ai_news_analyzer import _raise_if_cancelled
        _raise_if_cancelled(cancel_check)

        if progress_callback:
            progress_callback("正在抽取题材并入库...")

        try:
            extractor = ThemeExtractor()
            themes, news_id_map, err = extractor.extract_from_file(
                result.report_path,
                progress_callback=progress_callback,
            )
            if err:
                result.theme_error = err
            if not themes:
                if progress_callback:
                    progress_callback(f"题材抽取无结果: {err or '空列表'}")
                return

            meta = parse_report_meta(result.report_path)
            if simulated_trade_date:
                # 回测产物：用模拟交易日覆盖（最权威），并打回测标
                meta["report_date"] = simulated_trade_date
                meta["is_backtest"] = True
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
