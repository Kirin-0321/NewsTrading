"""
② 数据清理服务：从未清洗原始数据 → AI 清洗 → 更新 raw_news.clean_status

未清洗：`clean_status='pending'`（见 RawStore.get_uncleaned_news）。
"""

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from services.storage import get_raw_store


@dataclass
class CleanSyncResult:
    ok: bool = False
    processed: int = 0
    kept: int = 0
    removed: int = 0
    error: Optional[str] = None
    stats: Dict = field(default_factory=dict)


def _load_cleaning_criteria() -> str:
    path = os.path.join("config", "cleaning_criteria.json")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("default", {}).get("criteria", "")


class CleanSyncService:
    """定时/手动清洗同步。"""

    def __init__(self):
        self.raw_store = get_raw_store()

    def run(
        self,
        batch_size: int = 100,
        provider: str = "deepseek",
        limit: int = 500,
        max_workers: int = 2,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> CleanSyncResult:
        """
        清洗未处理的原始新闻并写入精选库。

        Args:
            batch_size: AI 批大小
            provider: AI 服务商
            limit: 单次最多处理条数
            max_workers: 并行批次数（1–4，默认 2）
            progress_callback: 文本进度回调，签名 ``Callable[[str], None]``。
                NewsCleaner 内部既会 1 参（普通日志）也会 5 参（批次进度）地调
                progress_callback，本方法会将 5 参合并为单字符串再上抛，
                保证对外契约始终是单参文本。

        Returns:
            CleanSyncResult
        """
        result = CleanSyncResult()
        log = progress_callback or (lambda _m: None)

        # 适配 NewsCleaner 的双签名回调，统一为单参字符串
        def _adapter(*args):
            if not progress_callback:
                return
            if len(args) == 1:
                progress_callback(args[0])
                return
            if len(args) == 5:
                batch_info, current, total, kept, removed = args
                progress_callback(
                    f"{batch_info}: {current}/{total} "
                    f"(保留 {kept}, 剔除 {removed})"
                )
                return
            # 其他签名兜底，避免再次出现 TypeError
            progress_callback(" | ".join(str(a) for a in args))

        try:
            from core.news_cleaner import NewsCleaner

            uncleaned = self.raw_store.get_uncleaned_news(limit=limit)
            if not uncleaned:
                log("没有待清洗的新闻")
                result.ok = True
                return result

            log(f"待清洗 {len(uncleaned)} 条新闻")
            criteria = _load_cleaning_criteria()
            cleaner = NewsCleaner(criteria=criteria, ai_provider=provider)

            for item in uncleaned:
                item["raw_id"] = item.get("id")

            clean_result = cleaner.clean_news_list(
                uncleaned,
                batch_size=batch_size,
                auto_merge=False,
                progress_callback=_adapter,
                max_workers=max_workers,
            )

            kept = clean_result.get("kept", [])
            removed = clean_result.get("removed", [])

            kept_count, removed_count = self.raw_store.apply_clean_results(
                kept, removed, provider=provider
            )

            result.processed = len(uncleaned)
            result.kept = kept_count
            result.removed = removed_count
            result.ok = True
            result.stats = {
                "processed": result.processed,
                "kept": len(kept),
                "removed": len(removed),
                "updated_curated": kept_count,
                "updated_rejected": removed_count,
                "error_skipped": clean_result.get("metadata", {}).get("error_skipped", 0),
            }
            skipped = result.stats["error_skipped"]
            if skipped:
                log(
                    f"清洗完成: 保留 {kept_count}，剔除 {removed_count}，"
                    f"审核跳过 {skipped} 条"
                )
            else:
                log(f"清洗完成: 保留 {kept_count}，剔除 {removed_count}")

        except Exception as e:
            result.error = str(e)
            result.ok = False

        return result


def clean_sync(**kwargs) -> CleanSyncResult:
    return CleanSyncService().run(**kwargs)
