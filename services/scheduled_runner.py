"""
定时任务执行器：根据 task type 调用 services 层。
"""

import logging
from threading import Thread
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


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

        return {"ok": False, "type": task_type, "error": f"未知任务类型: {task_type}"}

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
