"""
定时任务执行器：根据 task type 调用 services 层。
"""

import logging
from threading import Thread
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


def _stub_response(task_type: str, hint: str) -> Dict:
    """打分系统 plan M3 桩响应：可执行 / 不报错 / 给出未实施提示。"""
    logger.info("[stub] %s: %s", task_type, hint)
    return {
        "ok": False,
        "type": task_type,
        "result": {"stub": True, "hint": hint},
        "error": hint,
    }


def normalize_task(task: Dict) -> Dict:
    """兼容旧版 schedule_tasks.json（无 type 字段）。"""
    if task.get("type"):
        return task
    return {
        **task,
        "type": "crawl_sync",
        "params": {
            "scroll_times": task.get("scroll_times", 36),
            "wait_seconds": task.get("wait_seconds", 6),
            "max_no_change": task.get("max_no_change", 3),
            "incremental": True,
            "headless": True,
        },
    }


def run_task_sync(task: Dict) -> Dict:
    """
    同步执行单个定时任务。

    Returns:
        {ok, type, result|error}
    """
    task = normalize_task(task)
    task_type = task.get("type", "crawl_sync")
    params = task.get("params") or {}

    try:
        if task_type == "crawl_sync":
            from services.crawl_sync_service import crawl_sync

            result = crawl_sync(
                scroll_times=params.get("scroll_times", 36),
                wait_seconds=params.get("wait_seconds", 6),
                headless=params.get("headless", True),
                max_no_change=params.get("max_no_change", 3),
                incremental=params.get("incremental", True),
            )
            outcome = {
                "ok": result.ok,
                "type": task_type,
                "result": result.stats,
                "error": result.error,
            }

            if result.ok and params.get("auto_clean", True):
                from services.clean_sync_service import clean_sync

                logger.info("爬取完成，开始同步清洗...")
                clean_result = clean_sync(
                    batch_size=params.get("batch_size", 100),
                    provider=params.get("provider", "deepseek"),
                    limit=params.get("limit", 500),
                    max_workers=params.get("max_workers", 2),
                )
                outcome["clean_result"] = clean_result.stats
                outcome["ok"] = result.ok and clean_result.ok
                if clean_result.error:
                    outcome["error"] = clean_result.error
            return outcome

        if task_type == "clean_sync":
            from services.clean_sync_service import clean_sync

            result = clean_sync(
                batch_size=params.get("batch_size", 100),
                provider=params.get("provider", "deepseek"),
                limit=params.get("limit", 500),
                max_workers=params.get("max_workers", 2),
            )
            return {
                "ok": result.ok,
                "type": task_type,
                "result": result.stats,
                "error": result.error,
            }

        if task_type == "analyze":
            from services.analysis_service import analyze_news

            result = analyze_news(
                source=params.get("source", "curated"),
                hours=params.get("time_range_hours", 24),
                template_id=params.get("template"),
                provider=params.get("provider"),
            )
            return {
                "ok": result.ok,
                "type": task_type,
                "result": {
                    "report_path": result.report_path,
                    "news_count": result.news_count,
                },
                "error": result.error,
            }

        if task_type == "market_fetch":
            from services.market.service import MarketSummaryService

            ms = MarketSummaryService().build(
                trade_date=params.get("trade_date"),
                mode=params.get("mode", "hybrid"),
                top_sector_n=params.get("top_sector_n", 10),
                force_refresh=params.get("force_refresh", False),
            )
            return {
                "ok": ms.ok,
                "type": task_type,
                "result": {
                    "trade_date": ms.trade_date,
                    "completeness": ms.completeness,
                    "elapsed_ms": ms.elapsed_ms,
                    "api_call_count": ms.api_call_count,
                    "mode": ms.mode,
                    "from_cache": ms.from_cache,
                },
                "error": ms.error,
            }

        # ==================================================================
        # 打分系统任务类型（plan M3 桩）
        # ------------------------------------------------------------------
        # 一期：4 个新任务类型都返回 "not_implemented"，让定时任务可被
        # 主人在 GUI 创建 / 启用 / 查看上次执行结果，但实际打分逻辑等
        # plan M3 / M4 完成后再填充。
        # ==================================================================
        if task_type == "stock_daily_sync":
            # Phase 1 已实施：拉全 A 股个股当日涨跌幅 -> fact_stock_daily
            from services.market.stock_daily_sync import sync_stock_daily
            from services.market.trade_date import resolve_trade_date
            from services.market.tushare_client import TushareClient
            cli = TushareClient()
            td, _ = resolve_trade_date(cli, params.get("trade_date"))
            stock_result = sync_stock_daily(
                td,
                client=cli,
                force=params.get("force", False),
            )
            return {
                "ok": stock_result.ok,
                "type": task_type,
                "result": {
                    "trade_date": td,
                    "rows_written": stock_result.rows_written,
                    "api_calls": stock_result.api_calls,
                    "elapsed_ms": stock_result.elapsed_ms,
                    "skipped": stock_result.skipped,
                },
                "error": stock_result.error,
            }

        if task_type == "sector_daily_sync":
            # Phase 1 已实施：拉全板块当日涨跌幅 -> fact_sector_daily
            from services.market.sector_daily_sync import sync_sector_daily
            from services.market.trade_date import resolve_trade_date
            from services.market.tushare_client import TushareClient
            cli = TushareClient()
            td, _ = resolve_trade_date(cli, params.get("trade_date"))
            sector_result = sync_sector_daily(
                td,
                client=cli,
                force=params.get("force", False),
            )
            return {
                "ok": sector_result.ok,
                "type": task_type,
                "result": {
                    "trade_date": td,
                    "rows_written": sector_result.rows_written,
                    "api_calls": sector_result.api_calls,
                    "elapsed_ms": sector_result.elapsed_ms,
                    "skipped": sector_result.skipped,
                },
                "error": sector_result.error,
            }

        if task_type == "theme_score_daily":
            # Phase 2 已实施：扫追踪期内题材跑脚本打分
            from services.scoring.scoring_service import (
                run_daily_scoring,
            )
            score_result = run_daily_scoring(
                score_date=params.get("score_date"),
                days_back=int(params.get("days_back", 5)),
                hit_threshold_pct=float(
                    params.get("hit_threshold_pct", 3.0)
                ),
                benchmark_ts_code=params.get(
                    "benchmark_ts_code", "000001.SH"
                ),
            )
            return {
                "ok": score_result.ok,
                "type": task_type,
                "result": {
                    "themes_total": score_result.themes_total,
                    "themes_scored": score_result.themes_scored,
                    "pairs_succeeded": score_result.pairs_succeeded,
                    "pairs_attempted": score_result.pairs_attempted,
                    "days_covered": score_result.days_covered,
                    "elapsed_ms": score_result.elapsed_ms,
                },
                "error": (
                    None if score_result.ok
                    else "; ".join(score_result.errors[:5])
                ),
            }

        if task_type == "theme_ai_review":
            # plan M3.3：扫今日 D+5 题材交 AI 复审 -> ai_review 字段
            return _stub_response(
                task_type,
                "plan M3.3 AI 评分员未实施"
                "（待 services/scoring/ai_scorer.py + "
                "prompts/theme_review/theme_d5_review.md 上线）",
            )

        return {"ok": False, "type": task_type,
                "error": f"未知任务类型: {task_type}"}

    except Exception as e:
        logger.exception("定时任务执行失败")
        return {"ok": False, "type": task_type, "error": str(e)}


def run_task_async(
    task: Dict,
    on_complete: Optional[Callable[[Dict], None]] = None,
) -> Thread:
    """在后台线程执行任务。"""

    def _worker():
        outcome = run_task_sync(task)
        if on_complete:
            on_complete(outcome)
        elif outcome.get("ok"):
            logger.info("任务 %s 完成: %s", task.get("name"), outcome.get("result"))
        else:
            logger.error("任务 %s 失败: %s", task.get("name"), outcome.get("error"))

    thread = Thread(target=_worker, daemon=True)
    thread.start()
    return thread
