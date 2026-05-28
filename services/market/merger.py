"""MarketSummary 合并器（Phase M2）。

把 ``service._build_summary_from_db()`` 出来的 tushare-only summary 与
``CLSEnricher`` / ``AIEnricher`` 的增量拼起来。

合并优先级（高 → 低）::

    1. Tushare 数值字段——**不可被覆盖**（任何尝试覆盖均落入 ``conflicts``）
    2. CLS catalysts（``cls_stock_shock.reason``）
    3. AI patch（仅允许补 CLS 没命中的板块）
    4. trader_aliases 映射（dragon_tiger.famous_traders）

merger 的输入是已经构造好的 summary（dict），merger 会原地 mutate 它的
``sectors_top`` / ``market_shock`` / ``conflicts`` / ``field_sources``
等字段，并返回该 dict。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.market.ai_enricher import AIEnrichPatch
from services.market.cls_enricher import CLSEnrichResult

_log = logging.getLogger(__name__)


@dataclass
class MergeStats:
    catalysts_from_cls: int = 0
    catalysts_from_ai: int = 0
    catalysts_unfilled: List[str] = field(default_factory=list)
    market_shock_events: int = 0
    conflicts: int = 0


class MarketSummaryMerger:
    """把 cls + ai patch 注入 tushare-only summary。"""

    def merge(
        self,
        summary: Dict[str, Any],
        cls_result: Optional[CLSEnrichResult] = None,
        ai_patch: Optional[AIEnrichPatch] = None,
    ) -> MergeStats:
        stats = MergeStats()

        summary.setdefault("field_sources", {})
        summary.setdefault("conflicts", [])

        self._inject_catalysts(summary, cls_result, ai_patch, stats)
        self._inject_market_shock(summary, cls_result, stats)

        return stats

    # ------------------------------------------------------------------
    # catalysts
    # ------------------------------------------------------------------

    def _inject_catalysts(
        self,
        summary: Dict[str, Any],
        cls_result: Optional[CLSEnrichResult],
        ai_patch: Optional[AIEnrichPatch],
        stats: MergeStats,
    ) -> None:
        # 决策 3（2026-05-28）：跌幅榜（sectors_bottom）也注入催化。
        # bottom 不参与 AI patch（prompt 只发 top），所以只用 cls 通道。
        sectors_top = summary.get("sectors_top") or []
        sectors_bottom = summary.get("sectors_bottom") or []
        if not isinstance(sectors_top, list):
            sectors_top = []
        if not isinstance(sectors_bottom, list):
            sectors_bottom = []
        if not sectors_top and not sectors_bottom:
            return

        cls_by_sector: Dict[str, List[str]] = {}
        cls_sources: Dict[str, str] = {}
        if cls_result is not None:
            cls_by_sector = dict(cls_result.catalysts_by_sector or {})
            cls_sources = dict(cls_result.match_sources or {})

        ai_sectors_catalysts: Dict[str, List[str]] = {}
        if ai_patch is not None and ai_patch.sectors.patch:
            raw = ai_patch.sectors.patch.get("sectors_catalysts") or {}
            if isinstance(raw, dict):
                ai_sectors_catalysts = {
                    str(k): list(v) for k, v in raw.items()
                    if isinstance(v, list)
                }

        field_sources: Dict[str, Any] = summary["field_sources"]
        sector_sources: Dict[str, str] = (
            field_sources.setdefault("sectors_top.catalysts", {})
        )
        # 跌幅榜独立记 source map（field_sources 字典支持任意子键）
        sector_bottom_sources: Dict[str, str] = (
            field_sources.setdefault("sectors_bottom.catalysts", {})
        )
        conflicts: List[Dict[str, Any]] = summary["conflicts"]

        # 先处理 top（保持原行为：AI 可参与 + 冲突计数）
        sectors = sectors_top
        for sector in sectors:
            if not isinstance(sector, dict):
                continue
            name = str(sector.get("name") or "").strip()
            if not name:
                continue

            cls_cats = cls_by_sector.get(name) or []
            ai_cats = ai_sectors_catalysts.get(name) or []

            chosen: List[str] = []
            source: str = "none"

            if cls_cats:
                chosen = list(cls_cats)
                source = "cls"
                stats.catalysts_from_cls += 1
                if ai_cats:
                    # AI 越权——cls 已经填好却尝试覆盖
                    conflicts.append({
                        "type": "catalyst_override",
                        "field": f"sectors_top[{name}].catalysts",
                        "winner": "cls",
                        "loser": "ai",
                        "loser_payload": ai_cats[:3],
                    })
                    stats.conflicts += 1
            elif ai_cats:
                chosen = list(ai_cats)
                source = "ai"
                stats.catalysts_from_ai += 1
            else:
                stats.catalysts_unfilled.append(name)

            sector["catalysts"] = chosen
            sector["catalysts_source"] = source
            if source == "cls":
                sector["catalysts_match"] = cls_sources.get(name, "exact")
            sector_sources[name] = source

        # 决策 3（2026-05-28）：bottom 走简化通道——只 cls，不 AI、不计冲突
        for sector in sectors_bottom:
            if not isinstance(sector, dict):
                continue
            name = str(sector.get("name") or "").strip()
            if not name:
                continue
            cls_cats = cls_by_sector.get(name) or []
            chosen: List[str] = list(cls_cats) if cls_cats else []
            source = "cls" if chosen else "none"
            sector["catalysts"] = chosen
            sector["catalysts_source"] = source
            if source == "cls":
                sector["catalysts_match"] = cls_sources.get(name, "exact")
            sector_bottom_sources[name] = source

    # ------------------------------------------------------------------
    # market_shock 时间线（cls_market_shock）
    # ------------------------------------------------------------------

    def _inject_market_shock(
        self,
        summary: Dict[str, Any],
        cls_result: Optional[CLSEnrichResult],
        stats: MergeStats,
    ) -> None:
        if cls_result is None or not cls_result.cls_market_shock:
            summary.setdefault("market_shock", [])
            return

        events: List[Dict[str, Any]] = []
        for sector_name, items in cls_result.cls_market_shock.items():
            for first_time, status, count in items:
                events.append({
                    "sector": sector_name,
                    "first_shock_time": first_time,
                    "status": status,
                    "shock_count": count,
                })
        events.sort(
            key=lambda e: (
                e.get("first_shock_time") or "",
                e.get("sector") or "",
            )
        )
        summary["market_shock"] = events
        stats.market_shock_events = len(events)


__all__ = [
    "MarketSummaryMerger",
    "MergeStats",
]
