"""``MarketSummaryService`` —— 盘后数据生成主入口（Phase M1b: tushare-only）。

本阶段仅实现 ``mode="tushare-only"`` 路径，工作流::

    1. resolve_trade_date  → (trade_date, prev_trade_date)
    2. 命中 market_summaries 缓存 → 直接 read 返回
    3. fetcher.fetch       → fact_* 表全部入库
    4. _build_summary_from_db → 拼 canonical JSON
    5. renderer.render_compact → compact Markdown
    6. _persist_summary    → 落 market_summaries 表
    7. 返回 :class:`MarketSummaryResult`

CLS 板块匹配（``mode="hybrid"``）与 AI 兜底（``mode="ai-full"``）留给后续
Phase M2 的 ``CLSEnricher`` / ``AIEnricher`` / ``MarketSummaryMerger`` 接入。
但 ``build()`` 现在就已经接受 ``mode`` 参数，未实现的模式会自动降级到 ``tushare-only``
并写入 warnings，主人会立刻看到提示。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from services.market.ai_enricher import AIEnricher, AIEnrichPatch
from services.market.cls_enricher import CLSEnricher, CLSEnrichResult
from services.market.market_db import MarketDB, get_market_db
from services.market.merger import MarketSummaryMerger
from services.market.metrics import (
    build_limit_ladder,
    calc_fail_rate,
    calc_promotion_rate,
    calc_seal_rate,
    find_max_height,
    safe_pct_chg,
)
from services.market.renderer import MarketSummaryRenderer
from services.market.trade_date import resolve_trade_date
from services.market.trader_aliases import TraderAliasMatcher
from services.market.tushare_client import TushareClient
from services.market.tushare_fetcher import (
    INDEX_CODES,
    FetchResult,
    TushareMarketFetcher,
)
from services.market.validator import MarketSummaryValidator

_log = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"

VALID_MODES = {"tushare-only", "hybrid", "ai-full"}

#: ``MarketSummary.quality.completeness_score`` 字段权重表（M1b 简化版）
#: M2 阶段会替换为 ``config/market_summary_schema.json`` 驱动的版本
_REQUIRED_L1_PATHS = [
    "indices.sh.pct_chg",
    "indices.sz.pct_chg",
    "indices.cyb.pct_chg",
    "indices.total_turnover_yi",
    "breadth.limit_up",
    "breadth.limit_down",
    "breadth.failed_limit",
]
_REQUIRED_L2_PATHS = [
    "sentiment.seal_rate",
    "sentiment.max_height",
    "capital_flow.north_net_yi",
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class MarketSummaryResult:
    """``MarketSummaryService.build`` / ``get`` 的返回值。"""

    ok: bool = False
    trade_date: Optional[str] = None
    prev_trade_date: Optional[str] = None
    mode: str = "tushare-only"

    summary_json: Optional[Dict[str, Any]] = None
    summary_md: Optional[str] = None

    completeness: float = 0.0
    gaps: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    elapsed_ms: int = 0
    api_call_count: int = 0
    cancelled: bool = False
    error: Optional[str] = None

    from_cache: bool = False


# ---------------------------------------------------------------------------
# 板块 Top/Bottom 数据来源（2026-05-28 v3 决策点 cluster_method=llm_only）
# ---------------------------------------------------------------------------
#
# v3 起 _read_sectors_top/_bottom 直接消费
# :func:`services.market.sector_grouping.query_grouped_sectors_for_date`，
# 不再做 SQL 同名去重；语义聚类已在 dim_sector_group 表里固化。
#
# 历史：v2 曾用 _SECTOR_DEDUP_SQL_TPL 按 ``d.name + idx_group`` 同名去重保 dc，
# 仅能合并跨源同名（白酒/酿酒概念合不上），v3 升级为 group_id 语义聚类。
# v2 SQL 模板已删除（git 历史可查），改造日志见
# ``doc/updates/05-28-1410-板块聚类与GUI聚类视图上线.md``。


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class MarketSummaryService:
    """统一入口：把所有子模块（fetcher / metrics / renderer / db）串起来。"""

    def __init__(
        self,
        *,
        db: Optional[MarketDB] = None,
        client: Optional[TushareClient] = None,
        fetcher: Optional[TushareMarketFetcher] = None,
        renderer: Optional[MarketSummaryRenderer] = None,
        cls_enricher: Optional[CLSEnricher] = None,
        ai_enricher: Optional[AIEnricher] = None,
        merger: Optional[MarketSummaryMerger] = None,
        validator: Optional[MarketSummaryValidator] = None,
        trader_matcher: Optional[TraderAliasMatcher] = None,
    ) -> None:
        self.db = db or get_market_db()
        self.db.ensure_schema()
        # client 懒初始化：如果命中 cache，build() 整个流程都不需要它
        self._client = client
        self._fetcher = fetcher
        self.renderer = renderer or MarketSummaryRenderer()
        self._cls_enricher = cls_enricher
        self._ai_enricher = ai_enricher
        self.merger = merger or MarketSummaryMerger()
        self._validator = validator
        self._trader_matcher = trader_matcher

    # ------------------------------------------------------------------
    # 懒属性
    # ------------------------------------------------------------------

    @property
    def client(self) -> TushareClient:
        if self._client is None:
            self._client = TushareClient()
        return self._client

    @property
    def fetcher(self) -> TushareMarketFetcher:
        if self._fetcher is None:
            self._fetcher = TushareMarketFetcher(self.client, self.db)
        return self._fetcher

    @property
    def cls_enricher(self) -> CLSEnricher:
        if self._cls_enricher is None:
            self._cls_enricher = CLSEnricher(db=self.db)
        return self._cls_enricher

    @property
    def ai_enricher(self) -> AIEnricher:
        if self._ai_enricher is None:
            self._ai_enricher = AIEnricher()
        return self._ai_enricher

    @property
    def validator(self) -> MarketSummaryValidator:
        if self._validator is None:
            self._validator = MarketSummaryValidator()
        return self._validator

    @property
    def trader_matcher(self) -> TraderAliasMatcher:
        if self._trader_matcher is None:
            self._trader_matcher = TraderAliasMatcher(db=self.db)
        return self._trader_matcher

    # ==================================================================
    # 主入口
    # ==================================================================

    def build(
        self,
        trade_date: Optional[str] = None,
        *,
        mode: str = "tushare-only",
        top_sector_n: int = 30,
        force_refresh: bool = False,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> MarketSummaryResult:
        """生成（或读出缓存的）盘后总结。

        Args:
            trade_date: ``YYYYMMDD``；None 时取最近一个交易日。
            mode: ``tushare-only`` / ``hybrid`` / ``ai-full``；
                M1b 阶段后两者自动降级为 ``tushare-only`` 并加 warning。
            top_sector_n: 板块榜单返回前 N 名。
            force_refresh: True 时无视缓存重拉。
            cancel_check: GUI Worker 取消时返回 True，service 会尽早停。
            progress_callback: ``cb(msg)`` 用于实时进度文字。

        失败不抛异常，所有错误都进 ``result.error / warnings``。
        """
        progress = progress_callback or (lambda _m: None)
        result = MarketSummaryResult(mode=mode)
        t0 = time.time()

        # ---- 1) mode 校验 ----
        if mode not in VALID_MODES:
            result.warnings.append(
                f"未知 mode={mode!r}，降级为 tushare-only"
            )
            mode = "tushare-only"
            result.mode = mode
        # M2 起 hybrid / ai-full 均走 CLS + AI 兜底完整路径
        if mode == "ai-full":
            # 当前 M2 阶段 ai-full 与 hybrid 行为一致，
            # 后续 M3 可拓展为强制 AI 全量重写
            result.warnings.append(
                "mode=ai-full 当前等价于 hybrid（M3 阶段会扩展）"
            )
            mode = "hybrid"
            result.mode = mode

        try:
            # ---- 2) 解析交易日 ----
            progress("解析交易日…")
            try:
                td, ptd = resolve_trade_date(
                    self.client, trade_date, db=self.db
                )
            except Exception as exc:  # noqa: BLE001
                result.error = f"解析交易日失败: {exc}"
                result.elapsed_ms = int((time.time() - t0) * 1000)
                return result
            result.trade_date = td
            result.prev_trade_date = ptd

            if _maybe_cancelled(cancel_check, result):
                return _finalize(result, t0)

            # ---- 3) 缓存命中：fall-through 跑 fetcher 补缺失维度 ----
            #
            # 2026-05-27 修订：旧版命中即 return，导致按钮按完后只有 summary_md
            # 复用、底层 fact_* 表的缺失维度（如 fact_stock_daily）永远填不上。
            # 新版命中后**仍跑一次 fetcher.fetch(force=False)**，让 14+1 个 step
            # 各自自检：已有数据→skip（毫秒级 COUNT），缺失→补齐。整体性能
            # 仍接近秒级（绝大多数 step 命中 skip）。
            if not force_refresh:
                cached = self.get(td)
                if cached.ok:
                    progress("命中缓存，开始逐表自检补缺失维度…")
                    try:
                        fr = self.fetcher.fetch(
                            trade_date=td,
                            prev_trade_date=ptd,
                            top_sector_n=top_sector_n,
                            force_refresh=False,
                            progress_callback=progress,
                        )
                        cached.api_call_count = (
                            (cached.api_call_count or 0) + fr.api_call_count
                        )
                        cached.warnings.extend(fr.warnings)
                    except Exception as exc:  # noqa: BLE001
                        cached.warnings.append(
                            f"缓存命中后补缺失败: {exc}"
                        )
                    cached.from_cache = True
                    cached.warnings.extend(result.warnings)
                    cached.elapsed_ms = int((time.time() - t0) * 1000)
                    return cached

            # ---- 4) 启动检查：dim_stock 空则首次自动初始化 ----
            # 让首次用户开箱即用，所有 ts_code → name 查询都能命中
            self._ensure_dim_stock_initialized(result, progress)

            # ---- 5) Tushare 拉数据 ----
            progress("启动 Tushare 取数…")
            api0 = self.client.call_count
            fetch_result = self.fetcher.fetch(
                trade_date=td,
                prev_trade_date=ptd,
                top_sector_n=top_sector_n,
                force_refresh=force_refresh,
                progress_callback=progress,
            )
            result.api_call_count = self.client.call_count - api0
            result.warnings.extend(fetch_result.warnings)

            if _maybe_cancelled(cancel_check, result):
                return _finalize(result, t0)

            # ---- 5) 拼 canonical summary ----
            progress("组装 MarketSummary…")
            summary, gaps = self._build_summary_from_db(
                td, ptd, fetch_result, mode=mode, top_sector_n=top_sector_n
            )

            # ---- 5b) hybrid: CLS + AI 兜底 + merger ----
            ai_patch: Optional[AIEnrichPatch] = None
            if mode == "hybrid":
                progress("CLS 数据匹配…")
                # 决策 3（2026-05-28）：跌幅榜也跑 cls 催化匹配。
                # cls_enricher.enrich 按 sector.name 匹配，对来源无感知，
                # 把 top + bottom 拼起来喂一次即可；后续 merger 会按 list
                # 分别注入 catalysts 字段。
                _top = summary.get("sectors_top") or []
                _bot = summary.get("sectors_bottom") or []
                cls_result = self.cls_enricher.enrich(td, list(_top) + list(_bot))

                if _maybe_cancelled(cancel_check, result):
                    return _finalize(result, t0)

                progress("AI 兜底补全…")
                ai_patch = self._run_ai_enrichment(
                    td, summary, cls_result
                )

                # AI 输出的知名游资别名 → 入 dim_trader_alias
                self._upsert_ai_traders(ai_patch)
                # 重读 dragon_tiger，让 AI 新加的 alias 立即生效
                summary["dragon_tiger"] = self._read_dragon_tiger(td)

                progress("合并 & 校验…")
                merge_stats = self.merger.merge(
                    summary, cls_result, ai_patch
                )
                summary.setdefault("meta", {})["merge_stats"] = {
                    "catalysts_from_cls": merge_stats.catalysts_from_cls,
                    "catalysts_from_ai": merge_stats.catalysts_from_ai,
                    "catalysts_unfilled": list(
                        merge_stats.catalysts_unfilled
                    ),
                    "market_shock_events": merge_stats.market_shock_events,
                    "conflicts": merge_stats.conflicts,
                }
                # market_shock JOIN fact_sector_daily 补 sector_pct_chg 等
                self._enrich_market_shock(summary, td)
            else:
                # tushare-only: 仍把 market_shock / regulation / catalysts
                # 字段留空（merger 不接入）
                cls_result = None

            # ---- 6) 计算完整度 ----
            quality = self._calc_quality(
                summary, gaps, fetch_result, mode=mode
            )
            summary["quality"] = quality
            # validator gaps 与手收集 gaps 合并（按 field 去重，validator 优先）
            v_gaps = list(quality.get("gaps") or [])
            v_fields = {(g.get("field") or "") for g in v_gaps if isinstance(g, dict)}
            for g in gaps:
                if isinstance(g, dict) and (g.get("field") or "") not in v_fields:
                    v_gaps.append(g)
            summary["gaps"] = v_gaps
            summary["meta"]["api_call_count"] = result.api_call_count

            # ---- 7) 渲染 compact MD ----
            progress("渲染 compact Markdown…")
            md_text = self.renderer.render_compact(summary)

            # ---- 8) 入库 ----
            progress("写入 market_summaries…")
            self._persist_summary(
                td=td, ptd=ptd, mode=mode, summary=summary,
                summary_md=md_text, result=result, fetch_result=fetch_result,
            )

            # ---- 8b) AI patch 落 ai_enrich_patches ----
            if ai_patch is not None:
                self._persist_ai_patch(td, ai_patch)

            result.summary_json = summary
            result.summary_md = md_text
            result.completeness = float(
                quality.get("completeness_score") or 0.0
            )
            result.gaps = gaps
            result.ok = True

        except Exception as exc:  # noqa: BLE001
            _log.exception("build 失败")
            result.error = f"build 失败: {exc}"

        return _finalize(result, t0)

    # ==================================================================
    # 读取 / 删除
    # ==================================================================

    def get(self, trade_date: str) -> MarketSummaryResult:
        """从 ``market_summaries`` 表读取已生成的盘后总结。"""
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM market_summaries WHERE trade_date = ?",
                (trade_date,),
            ).fetchone()
        if not row:
            return MarketSummaryResult(
                trade_date=trade_date,
                error=f"market_summaries 表中未找到 {trade_date}",
            )

        summary_json = _safe_json_loads(row["summary_json"]) or {}
        warnings = _safe_json_loads(row["warnings_json"]) or []
        gaps = _safe_json_loads(row["gaps_json"]) or []

        return MarketSummaryResult(
            ok=True,
            trade_date=str(row["trade_date"]),
            prev_trade_date=(
                str(row["prev_trade_date"]) if row["prev_trade_date"] else None
            ),
            mode=str(row["mode"] or "tushare-only"),
            summary_json=summary_json,
            summary_md=row["summary_md"],
            completeness=float(row["completeness"] or 0.0),
            gaps=list(gaps) if isinstance(gaps, list) else [],
            warnings=list(warnings) if isinstance(warnings, list) else [],
            elapsed_ms=int(row["elapsed_ms"] or 0),
            api_call_count=int(row["api_call_count"] or 0),
            from_cache=True,
        )

    def list_dates(self) -> List[str]:
        """已生成的所有日期，DESC。"""
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT trade_date FROM market_summaries "
                "ORDER BY trade_date DESC"
            ).fetchall()
        return [str(r[0]) for r in rows]

    def delete(self, trade_date: str) -> bool:
        """删除某天的 market_summaries 行（不清 fact_* 表）。"""
        with self.db.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM market_summaries WHERE trade_date = ?",
                (trade_date,),
            )
        return cursor.rowcount > 0

    # ==================================================================
    # canonical summary 拼装
    # ==================================================================

    def _build_summary_from_db(
        self,
        td: str,
        ptd: str,
        fetch_result: FetchResult,
        *,
        mode: str,
        top_sector_n: int,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """从 fact_* 表读出原始数据 → 拼装 ``MarketSummary`` JSON。"""
        gaps: List[Dict[str, Any]] = []

        # indices
        indices = self._read_indices(td, gaps)

        # breadth + sentiment
        breadth, sentiment, ladder = self._read_breadth_and_sentiment(
            td, ptd, fetch_result, gaps
        )

        # capital_flow
        capital_flow = self._read_capital_flow(td, fetch_result, gaps)

        # sectors_top
        sectors_top = self._read_sectors_top(td, top_sector_n, gaps)

        # sectors_bottom（涨幅倒数 15，跌停股龙头；2026-05-28 v3 升级 10→15）
        sectors_bottom = self._read_sectors_bottom(td, 15, gaps)

        # dragon_tiger
        dragon_tiger = self._read_dragon_tiger(td)

        # limit_sprint（冲刺涨停，三源融合 ths 独家泳池）
        limit_sprint = self._read_limit_sprint(td)

        summary: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "meta": {
                "trade_date": td,
                "prev_trade_date": ptd,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "api_call_count": fetch_result.api_call_count,
                "elapsed_ms": fetch_result.elapsed_ms,
                "sources": ["tushare"],
            },
            "indices": indices,
            "breadth": breadth,
            "sentiment": sentiment,
            "capital_flow": capital_flow,
            "sectors_top": sectors_top,
            "sectors_bottom": sectors_bottom,
            "limit_ladder": ladder,
            "limit_sprint": limit_sprint,
            "dragon_tiger": dragon_tiger,
            "market_shock": [],
            "regulation": [],
            "conflicts": [],
        }
        return summary, gaps

    # --- indices ---

    def _read_indices(
        self, td: str, gaps: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT ts_code, close, pct_chg, amount_yi "
                "FROM fact_index_daily WHERE trade_date = ?",
                (td,),
            ).fetchall()
        by_code = {str(r["ts_code"]): dict(r) for r in rows}

        amount_sum = 0.0
        sh_sz_amount = 0.0
        for ts_code, key, _label in INDEX_CODES:
            row = by_code.get(ts_code)
            if row:
                out[key] = {
                    "close": _round(row["close"], 2),
                    "pct_chg": safe_pct_chg(row["pct_chg"]),
                    "amount_yi": _round(row["amount_yi"], 2),
                }
                amt = _to_float(row["amount_yi"])
                if amt is not None:
                    amount_sum += amt
                if key in ("sh", "sz") and amt is not None:
                    sh_sz_amount += amt
            else:
                out[key] = {
                    "close": None, "pct_chg": None, "amount_yi": None,
                }
                if key in ("sh", "sz", "cyb"):
                    gaps.append({
                        "field": f"indices.{key}",
                        "reason": f"fact_index_daily 缺 ts_code={ts_code}",
                    })

        # 两市成交额 = 上证 + 深成
        out["total_turnover_yi"] = (
            _round(sh_sz_amount, 2) if sh_sz_amount > 0 else None
        )
        out["prev_turnover_yi"] = self._read_prev_turnover(td)
        return out

    def _read_prev_turnover(self, td: str) -> Optional[float]:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT pretrade_date FROM dim_trade_calendar "
                "WHERE trade_date = ? AND is_open = 1",
                (td,),
            ).fetchone()
            if not row or not row["pretrade_date"]:
                return None
            ptd = str(row["pretrade_date"])
            rs = conn.execute(
                "SELECT SUM(amount_yi) FROM fact_index_daily "
                "WHERE trade_date = ? "
                "AND ts_code IN ('000001.SH','399001.SZ')",
                (ptd,),
            ).fetchone()
        return _round(rs[0], 2) if rs and rs[0] is not None else None

    # --- breadth + sentiment + ladder ---

    def _read_breadth_and_sentiment(
        self,
        td: str,
        ptd: str,
        fetch_result: FetchResult,
        gaps: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT "
                " SUM(CASE WHEN limit_type='U' THEN 1 ELSE 0 END) AS u_cnt, "
                " SUM(CASE WHEN limit_type='Z' THEN 1 ELSE 0 END) AS z_cnt, "
                " SUM(CASE WHEN limit_type='D' THEN 1 ELSE 0 END) AS d_cnt "
                "FROM fact_limit_stock WHERE trade_date = ?",
                (td,),
            ).fetchone()
            u_cnt = int(row["u_cnt"] or 0)
            z_cnt = int(row["z_cnt"] or 0)
            d_cnt = int(row["d_cnt"] or 0)

            # 取所有 U 股；不再用 cons_nums IS NOT NULL 过滤
            # SQL alias limit_up_time AS lu_time —— 对齐 build_limit_ladder 字段名
            # 新增 lu_desc / limit_up_suc_rate / market_type / tag —— 三源融合
            kpl_rows = conn.execute(
                "SELECT ts_code, "
                "       json_extract(raw_json, '$.name') AS name, "
                "       theme, status, cons_nums, "
                "       limit_up_time AS lu_time, open_times, "
                "       lu_desc, limit_up_suc_rate, market_type, tag "
                "FROM fact_limit_stock "
                "WHERE trade_date = ? AND limit_type = 'U'",
                (td,),
            ).fetchall()
            # T-1 涨停 / 炸板数 —— seal_rate_prev 用
            prev_row = conn.execute(
                "SELECT "
                " SUM(CASE WHEN limit_type='U' THEN 1 ELSE 0 END) AS u_cnt, "
                " SUM(CASE WHEN limit_type='Z' THEN 1 ELSE 0 END) AS z_cnt "
                "FROM fact_limit_stock WHERE trade_date = ?",
                (ptd,),
            ).fetchone()
            prev_u = int(prev_row["u_cnt"] or 0) if prev_row else 0
            prev_z = int(prev_row["z_cnt"] or 0) if prev_row else 0

            # 全市场涨跌家数（fact_market_breadth 由 daily 接口聚合入库）
            mb_row = conn.execute(
                "SELECT advance, decline, unchanged, "
                "       advance_5, decline_5, advance_pct, total "
                "FROM fact_market_breadth WHERE trade_date = ?",
                (td,),
            ).fetchone()
            # 全 A 股个股日线行数（fact_stock_daily 由 step 15 写入）
            # 2026-05-27 新增：纳入完整度评分 → schema.json:breadth.stock_daily_rows
            sd_row = conn.execute(
                "SELECT COUNT(*) AS c FROM fact_stock_daily WHERE trade_date = ?",
                (td,),
            ).fetchone()
            stock_daily_rows = int(sd_row["c"]) if sd_row else 0
            kpl_dicts = [dict(r) for r in kpl_rows]
            null_cons_codes = [
                r["ts_code"] for r in kpl_rows if r["cons_nums"] is None
            ]
            null_theme_codes = [
                r["ts_code"] for r in kpl_rows
                if not r["theme"]
            ]
            cons_nums_filled = u_cnt - len(null_cons_codes)

            # —— Fallback：当 cons_nums NULL 时，用历史涨停递归推算
            # （T-1 仍涨停 → 2 板，T-2 仍涨停 → 3 板……上限 7 板）
            derived_cons: Dict[str, int] = {}
            if null_cons_codes:
                derived_cons = self._derive_cons_nums_from_history(
                    conn, td, null_cons_codes, max_n=7
                )
            # —— Fallback：当 theme 为空时，用 T-1..T-5 日的 theme 兜底
            derived_themes: Dict[str, str] = {}
            if null_theme_codes:
                derived_themes = self._derive_themes_from_history(
                    conn, td, null_theme_codes, lookback_days=5
                )

        # 把派生结果写回 kpl_dicts（in-place）
        for d in kpl_dicts:
            code = d["ts_code"]
            if d.get("cons_nums") is None and code in derived_cons:
                d["cons_nums"] = derived_cons[code]
                d["cons_nums_source"] = (
                    "derived_from_history"
                    if derived_cons[code] > 1 else "fallback_first"
                )
            if not d.get("theme") and code in derived_themes:
                d["theme"] = derived_themes[code]
                d["theme_source"] = "derived_from_history"

        breadth = {
            "limit_up": u_cnt or None,
            "limit_down": d_cnt or None,
            "failed_limit": z_cnt or None,
            "advance": int(mb_row["advance"]) if mb_row and mb_row["advance"] is not None else None,
            "decline": int(mb_row["decline"]) if mb_row and mb_row["decline"] is not None else None,
            "unchanged": int(mb_row["unchanged"]) if mb_row and mb_row["unchanged"] is not None else None,
            "advance_5": int(mb_row["advance_5"]) if mb_row and mb_row["advance_5"] is not None else None,
            "decline_5": int(mb_row["decline_5"]) if mb_row and mb_row["decline_5"] is not None else None,
            "advance_pct": float(mb_row["advance_pct"]) if mb_row and mb_row["advance_pct"] is not None else None,
            "total": int(mb_row["total"]) if mb_row and mb_row["total"] is not None else None,
            "stock_daily_rows": stock_daily_rows or None,
        }
        if mb_row is None:
            gaps.append({
                "field": "breadth.advance",
                "reason": "fact_market_breadth 该日无数据（daily 接口未拉到 / 接口失败）",
            })
        if stock_daily_rows <= 0:
            gaps.append({
                "field": "breadth.stock_daily_rows",
                "reason": "fact_stock_daily 该日无数据（step 15 未跑 / 失败）",
            })
        if not u_cnt:
            gaps.append({
                "field": "breadth.limit_up",
                "reason": "fact_limit_stock 无 U 数据",
            })
        elif u_cnt > 0 and cons_nums_filled == 0:
            # kpl_list 接口当日延迟 → 用历史递归推算 cons_nums
            from collections import Counter
            dist = Counter(d.get("cons_nums") or 1 for d in kpl_dicts)
            dist_str = "/".join(
                f"{k}板:{dist[k]}" for k in sorted(dist.keys())
            )
            gaps.append({
                "field": "limit_ladder.tiers",
                "reason": (
                    f"kpl_list({td}) 无数据，{u_cnt} 只涨停股的连板数 / 题材"
                    f"已基于历史 fact_limit_stock 递归推算；分布: {dist_str}；"
                    "T+1 Tushare 出数据后会自动 force-refresh 覆盖"
                ),
            })

        # 情绪指标
        ladder = build_limit_ladder(kpl_dicts)
        height, stock, theme = find_max_height(kpl_dicts)
        seal_rate = calc_seal_rate(u_cnt, z_cnt)
        fail_rate = calc_fail_rate(u_cnt, z_cnt)
        promo_rate = calc_promotion_rate(
            kpl_dicts, fetch_result.kpl_prev_rows
        )
        # 昨日封板率
        seal_rate_prev = calc_seal_rate(prev_u, prev_z)
        # 晋级率明细：T-1 涨停股在 T 仍涨停 / 没涨停
        prev_codes = {
            str(r.get("ts_code"))
            for r in (fetch_result.kpl_prev_rows or [])
            if r.get("ts_code")
        }
        today_u_codes = {
            str(d.get("ts_code")) for d in kpl_dicts if d.get("ts_code")
        }
        still_u = len(prev_codes & today_u_codes)
        prev_total = len(prev_codes)
        promotion_detail = (
            {
                "prev_u_count": prev_total,
                "today_still_u_count": still_u,
                "today_failed_count": max(prev_total - still_u, 0),
            }
            if prev_total > 0
            else None
        )
        sentiment = {
            "seal_rate": seal_rate,
            "seal_rate_prev": seal_rate_prev,
            "promotion_rate": promo_rate,
            "promotion_detail": promotion_detail,
            "fail_rate": fail_rate,
            "max_height": height if height > 0 else None,
            "max_stock": stock,
            "max_sector": (
                theme.split("·")[0]
                if theme and isinstance(theme, str)
                else None
            ),
        }
        return breadth, sentiment, ladder

    # --- capital flow ---

    def _read_capital_flow(
        self,
        td: str,
        fetch_result: FetchResult,
        gaps: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """北向 + 主力资金。

        ``main_net_yi`` 派生口径：SUM(fact_sector_daily.main_net_yi)
        WHERE dim_sector.idx_type='概念板块' AND dim_sector.src='dc'，
        即「东方财富概念板块主力净流入合计」(~486 行)。

        2026-05-28 hotfix：v2 改造把 fact_sector_daily 扩到 5 源（dc 概念/行业/
        地域 + ths 行业/概念，~1489 行），原 SQL 不带过滤直接 SUM 会让同一只
        股票被算 4-5 次，导致 20260527 出现「-49730 亿」物理不可能的离谱值
        （单日全市场总成交额仅 3.24 万亿）。锁回 v1 单源后回到正常量级。

        同一只股仍可能跨多个概念板块被重复统计（dc 概念板块本身就有
        ~486 个，大量股票分属多概念），所以这个值仅作市场宏观风向参考，
        不等于个股层主力净流入合计。``main_net_source`` 字段标注口径以
        避免 AI 误解。
        """
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT actual_date, is_delayed, north_net_yi, south_net_yi "
                "FROM fact_hsgt_daily WHERE trade_date = ?",
                (td,),
            ).fetchone()
            sec_row = conn.execute(
                "SELECT SUM(s.main_net_yi) AS s, COUNT(*) AS n "
                "FROM fact_sector_daily s "
                "JOIN dim_sector d ON d.ts_code = s.ts_code "
                "WHERE s.trade_date = ? "
                "  AND d.idx_type = '概念板块' "
                "  AND d.src = 'dc'",
                (td,),
            ).fetchone()

        main_net_yi: Optional[float] = None
        main_net_source: Optional[str] = None
        if sec_row and sec_row["n"] and sec_row["s"] is not None:
            main_net_yi = _round(sec_row["s"], 2)
            main_net_source = (
                f"sum_dc_concept_sector(n={int(sec_row['n'])}; "
                "注：同股属多概念存在重复，仅作宏观风向)"
            )
        else:
            gaps.append({
                "field": "capital_flow.main_net_yi",
                "reason": "fact_sector_daily 该日无数据，无法派生板块主力合计",
            })

        if not row:
            gaps.append({
                "field": "capital_flow.north_net_yi",
                "reason": "fact_hsgt_daily 无该日数据",
            })
            return {
                "north_net_yi": None,
                "north_data_date": None,
                "north_is_delayed": False,
                "south_net_yi": None,
                "main_net_yi": main_net_yi,
                "main_net_source": main_net_source,
            }
        return {
            "north_net_yi": _round(row["north_net_yi"], 2),
            "north_data_date": (
                str(row["actual_date"]) if row["actual_date"] else None
            ),
            "north_is_delayed": bool(row["is_delayed"]),
            "south_net_yi": _round(row["south_net_yi"], 2),
            "main_net_yi": main_net_yi,
            "main_net_source": main_net_source,
        }

    # --- sectors top N ---

    def _read_sectors_top(
        self,
        td: str,
        top_n: int,
        gaps: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """读取当日聚类后 Top N 组板块，派生 limit_up_count / leaders 字段。

        2026-05-28 v3 聚类版（替代 v2 同名去重）::

            v2 是按 ``d.name + idx_group`` 同名去重保 dc，~21% 跨源重名直接砍
            掉副本；v3 升级到按 ``dim_sector.group_id`` **语义聚类** 去重——
            "白酒/酿酒概念/白酒Ⅱ/白酒Ⅲ" 4 个不同名板块也合并成 1 组，
            涨幅取下中位（lower_median, ASC 第 (n+1)/2 项），
            未聚类板块自成一组不丢数据。

        排序：组间按 median_pct_chg DESC，前 N 组返回。

        派生口径（不变）：
            * limit_up_count → 中位代表板块名 LIKE 匹配 fact_limit_stock.theme
            * leaders        → 同上 cons_nums/fd_amount 排序前 3
        """
        from services.market.sector_grouping import (
            query_grouped_sectors_for_date,
        )

        rows = query_grouped_sectors_for_date(td)
        if not rows:
            gaps.append({
                "field": "sectors_top",
                "reason": "fact_sector_daily 该日无数据",
            })
            return []

        rows = rows[:top_n]

        out: List[Dict[str, Any]] = []
        with self.db.connect(readonly=True) as conn:
            for i, r in enumerate(rows, 1):
                sector_name = r["sector_name"]
                # leaders 匹配优先用 group_name（更宽泛，命中率高）
                # 比如 group="白酒" 匹配 limit_stock.theme="白酒" >>
                # sector_name="酿酒概念" 几乎匹配不到
                match_key = r["group_name"] or sector_name
                limit_up_count, leaders = self._derive_sector_leaders(
                    conn, td, match_key
                )
                out.append({
                    "rank": i,
                    "ts_code": r["ts_code"],
                    "name": r["display_name"],
                    "sector_name": sector_name,        # v3 新增：原始名
                    "group_name": r["group_name"],     # v3 新增：组名
                    "members_count": r["cnt_in_data"],  # v3 新增：组员数
                    "pct_chg": safe_pct_chg(r["pct_chg"]),
                    "pct_chg_5d": safe_pct_chg(r["pct_chg_5d"], bound=200.0),
                    "limit_up_count": limit_up_count,
                    "main_net_yi": _round(r["main_net_yi"], 2),
                    "main_elg_yi": _round(r["main_elg_yi"], 2),
                    "main_lg_yi": _round(r["main_lg_yi"], 2),
                    # 2026-05-28 新增（关联 008/dc_index/dc fallback）
                    "total_mv": _round(r.get("total_mv"), 1),
                    "turnover_rate": _round(r.get("turnover_rate"), 2),
                    "up_num": r.get("up_num"),
                    "down_num": r.get("down_num"),
                    "leaders": leaders,
                    "catalysts": [],        # cls/ai enricher 之后注入
                    "high_risk": None,
                    "field_sources": {
                        "pct_chg": "tushare",
                        "main_net_yi": "tushare",
                        "limit_up_count": (
                            "derived_from_limit_stock"
                            if limit_up_count is not None else "none"
                        ),
                        "leaders": (
                            "derived_from_limit_stock"
                            if leaders else "none"
                        ),
                    },
                })
        return out

    def _read_sectors_bottom(
        self,
        td: str,
        bottom_n: int,
        gaps: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """读取当日聚类后 Bottom N 组板块，派生 limit_down_count / laggards。

        ``laggards`` = 中位代表板块名 LIKE 命中的 D 股按 pct_chg ASC 取前 3。
        和 :meth:`_read_sectors_top` 同走聚类视图（v3）——倒序后的 Bottom N。
        """
        from services.market.sector_grouping import (
            query_grouped_sectors_for_date,
        )

        rows = query_grouped_sectors_for_date(td)
        if not rows:
            gaps.append({
                "field": "sectors_bottom",
                "reason": "fact_sector_daily 该日无数据",
            })
            return []

        # query_grouped_sectors_for_date 已按 pct_chg DESC，
        # 取末尾 N 行后再翻转得到"涨幅倒数前 N"
        rows = list(reversed(rows[-bottom_n:]))

        out: List[Dict[str, Any]] = []
        with self.db.connect(readonly=True) as conn:
            for i, r in enumerate(rows, 1):
                sector_name = r["sector_name"]
                match_key = r["group_name"] or sector_name
                limit_down_count, laggards = self._derive_sector_laggards(
                    conn, td, match_key
                )
                out.append({
                    "rank": i,
                    "ts_code": r["ts_code"],
                    "name": r["display_name"],
                    "sector_name": sector_name,        # v3 新增
                    "group_name": r["group_name"],     # v3 新增
                    "members_count": r["cnt_in_data"],  # v3 新增
                    "pct_chg": safe_pct_chg(r["pct_chg"]),
                    "pct_chg_5d": safe_pct_chg(r["pct_chg_5d"], bound=200.0),
                    "limit_down_count": limit_down_count,
                    "main_net_yi": _round(r["main_net_yi"], 2),
                    "main_elg_yi": _round(r["main_elg_yi"], 2),
                    "main_lg_yi": _round(r["main_lg_yi"], 2),
                    # 2026-05-28 新增（关联 008/dc_index/dc fallback）
                    "total_mv": _round(r.get("total_mv"), 1),
                    "turnover_rate": _round(r.get("turnover_rate"), 2),
                    "up_num": r.get("up_num"),
                    "down_num": r.get("down_num"),
                    "laggards": laggards,
                    "catalysts": [],        # 决策 3：跌幅榜也加催化
                    "field_sources": {
                        "pct_chg": "tushare",
                        "main_net_yi": "tushare",
                        "limit_down_count": (
                            "derived_from_limit_stock"
                            if limit_down_count is not None else "none"
                        ),
                        "laggards": (
                            "derived_from_limit_stock"
                            if laggards else "none"
                        ),
                    },
                })
        return out

    @staticmethod
    def _derive_sector_laggards(
        conn: Any,
        trade_date: str,
        sector_name: str,
        *,
        limit: int = 3,
    ) -> Tuple[Optional[int], List[Dict[str, Any]]]:
        """从 fact_limit_stock 按 theme LIKE 派生 (跌停数, 笨蛋股列表)。

        和 _derive_sector_leaders 对称，只是 limit_type='D'，按 pct_chg ASC 排。
        """
        if not sector_name:
            return (None, [])
        try:
            pattern = f"%{sector_name}%"
            count_row = conn.execute(
                "SELECT COUNT(*) FROM fact_limit_stock "
                "WHERE trade_date = ? AND limit_type = 'D' "
                "AND theme LIKE ?",
                (trade_date, pattern),
            ).fetchone()
            limit_down_count = (
                int(count_row[0]) if count_row and count_row[0] else None
            )

            laggard_rows = conn.execute(
                "SELECT COALESCE(d.name, "
                "         json_extract(l.raw_json, '$.name')) AS name, "
                "       l.pct_chg, l.status "
                "FROM fact_limit_stock l "
                "LEFT JOIN dim_stock d ON d.ts_code = l.ts_code "
                "WHERE l.trade_date = ? AND l.limit_type = 'D' "
                "  AND l.theme LIKE ? "
                "ORDER BY COALESCE(l.pct_chg, 0) ASC, "
                "         COALESCE(l.fd_amount_yi, 0) DESC "
                "LIMIT ?",
                (trade_date, pattern, int(limit)),
            ).fetchall()
            laggards = [
                {
                    "name": str(r["name"] or ""),
                    "pct_chg": r["pct_chg"],
                    "status": r["status"],
                }
                for r in laggard_rows
            ]
            return (limit_down_count, laggards)
        except Exception:  # noqa: BLE001
            return (None, [])

    @staticmethod
    def _derive_sector_leaders(
        conn: Any,
        trade_date: str,
        sector_name: str,
        *,
        limit: int = 3,
    ) -> Tuple[Optional[int], List[Dict[str, Any]]]:
        """从 fact_limit_stock 按 theme LIKE 派生 (涨停数, 龙头股列表)。

        ``sector_name`` 为空时返回 (None, [])；查询失败时返回 (None, [])。
        """
        if not sector_name:
            return (None, [])
        try:
            pattern = f"%{sector_name}%"
            count_row = conn.execute(
                "SELECT COUNT(*) FROM fact_limit_stock "
                "WHERE trade_date = ? AND limit_type = 'U' "
                "AND theme LIKE ?",
                (trade_date, pattern),
            ).fetchone()
            limit_up_count = (
                int(count_row[0]) if count_row and count_row[0] else None
            )

            leader_rows = conn.execute(
                "SELECT COALESCE(d.name, "
                "         json_extract(l.raw_json, '$.name')) AS name, "
                "       l.pct_chg, l.status, l.cons_nums "
                "FROM fact_limit_stock l "
                "LEFT JOIN dim_stock d ON d.ts_code = l.ts_code "
                "WHERE l.trade_date = ? AND l.limit_type = 'U' "
                "  AND l.theme LIKE ? "
                "ORDER BY COALESCE(l.cons_nums, 1) DESC, "
                "         COALESCE(l.fd_amount_yi, 0) DESC "
                "LIMIT ?",
                (trade_date, pattern, int(limit)),
            ).fetchall()
            leaders = [
                {
                    "name": str(r["name"] or ""),
                    "pct_chg": r["pct_chg"],
                    "status": r["status"],
                    "cons_nums": r["cons_nums"],
                }
                for r in leader_rows
            ]
            return (limit_up_count, leaders)
        except Exception:  # noqa: BLE001
            return (None, [])

    def _ensure_dim_stock_initialized(
        self,
        result: "MarketSummaryResult",
        progress: Callable[[str], None],
    ) -> None:
        """首次启动时，如 dim_stock 为空则自动拉一次 stock_basic 全量。

        非阻塞：失败时只 warn，不打断后续 build 流程（GUI 仍能用 raw_json
        兜底显示股名）。后续主人可手动 ``python tools/dim_stock_sync.py``。
        """
        try:
            with self.db.connect(readonly=True) as conn:
                cnt = int(
                    conn.execute("SELECT COUNT(*) FROM dim_stock").fetchone()[0]
                )
        except Exception as exc:  # noqa: BLE001
            _log.warning("dim_stock 行数检查失败: %s", exc)
            return
        if cnt > 0:
            return

        progress("首次启动，初始化 dim_stock（一次性，~5400 行）…")
        try:
            api0 = self.client.call_count
            out = self.fetcher.fetch_dim_stock_full(include_delisted=False)
            result.api_call_count += self.client.call_count - api0
            msg = (
                f"dim_stock 初始化完成: {out['total']} 行"
            )
            _log.info(msg)
            result.warnings.append(msg)
        except Exception as exc:  # noqa: BLE001
            warn = f"dim_stock 自动初始化失败（不影响主流程，可后续手动同步）: {exc}"
            _log.warning(warn)
            result.warnings.append(warn)

    @staticmethod
    def _derive_cons_nums_from_history(
        conn: Any,
        trade_date: str,
        ts_codes: List[str],
        *,
        max_n: int = 7,
    ) -> Dict[str, int]:
        """递归推算每只股的连板数。

        当 kpl_list 接口延迟（``cons_nums`` 全 NULL）时，用 fact_limit_stock
        的历史涨停记录推算：T 日涨停 + T-1 日仍涨停 → 2 连板，T-2 日仍涨停
        → 3 连板……上限 ``max_n`` 板（防止历史长链拖累性能）。

        Args:
            conn: 已 open 的 sqlite3 connection（readonly OK）
            trade_date: T 日 YYYYMMDD
            ts_codes: T 日涨停股 ts_code 列表
            max_n: 最多推算到 N 板（默认 7）
        Returns:
            ``{ts_code: cons_n}`` —— 未推出额外连板的默认 1（首板）。
            空输入返回 ``{}``。
        """
        if not ts_codes:
            return {}
        result: Dict[str, int] = {c: 1 for c in ts_codes}
        current: set = set(ts_codes)
        curr_date = trade_date
        for step in range(2, max_n + 1):
            if not current:
                break
            try:
                prev_row = conn.execute(
                    "SELECT pretrade_date FROM dim_trade_calendar "
                    "WHERE trade_date = ? AND is_open = 1",
                    (curr_date,),
                ).fetchone()
            except Exception:  # noqa: BLE001
                break
            if not prev_row or not prev_row["pretrade_date"]:
                break
            prev_date = str(prev_row["pretrade_date"])

            qmarks = ",".join("?" * len(current))
            try:
                rs = conn.execute(
                    f"SELECT ts_code FROM fact_limit_stock "
                    f"WHERE trade_date = ? AND limit_type = 'U' "
                    f"  AND ts_code IN ({qmarks})",
                    (prev_date, *current),
                ).fetchall()
            except Exception:  # noqa: BLE001
                break
            still_u = {r["ts_code"] for r in rs}
            for c in still_u:
                result[c] = step
            current = still_u
            curr_date = prev_date
        return result

    @staticmethod
    def _derive_themes_from_history(
        conn: Any,
        trade_date: str,
        ts_codes: List[str],
        *,
        lookback_days: int = 5,
    ) -> Dict[str, str]:
        """对每只股，回溯最近 ``lookback_days`` 个交易日找最新的 theme。

        当 kpl_list 当日延迟时 theme 全空，但同一只股在 T-1 / T-2 日的
        fact_limit_stock 通常有 theme（资金主线短期内稳定，可作近似兜底）。

        Args:
            conn: sqlite3 connection
            trade_date: T 日（不含），从 T-1 起回溯
            ts_codes: 要查 theme 的股代码
            lookback_days: 回溯天数（默认 5 个自然日，含周末按 SQL 过滤）
        Returns:
            ``{ts_code: theme}``。未找到 theme 的股不在返回 dict 里。
        """
        if not ts_codes:
            return {}
        out: Dict[str, str] = {}
        from datetime import datetime, timedelta
        try:
            d_end = datetime.strptime(trade_date, "%Y%m%d")
        except Exception:  # noqa: BLE001
            return {}
        start_str = (d_end - timedelta(days=lookback_days * 2 + 3)).strftime("%Y%m%d")
        qmarks = ",".join("?" * len(ts_codes))
        try:
            rs = conn.execute(
                f"SELECT ts_code, theme, trade_date "
                f"FROM fact_limit_stock "
                f"WHERE trade_date >= ? AND trade_date < ? "
                f"  AND limit_type = 'U' AND theme IS NOT NULL "
                f"  AND ts_code IN ({qmarks}) "
                f"ORDER BY ts_code, trade_date DESC",
                (start_str, trade_date, *ts_codes),
            ).fetchall()
        except Exception:  # noqa: BLE001
            return {}
        for r in rs:
            code = r["ts_code"]
            if code not in out:
                out[code] = str(r["theme"] or "")
        return out

    # --- market_shock enrichment ---

    def _enrich_market_shock(
        self, summary: Dict[str, Any], td: str
    ) -> None:
        """给 market_shock 时间线追加 sector_pct_chg / sector_main_net_yi。

        匹配口径：``dim_sector.name`` 完全相等。CLS 板块名与同花顺概念名
        可能有差异，匹配不上的事件仅保留原字段，不影响渲染。
        """
        shocks = summary.get("market_shock") or []
        if not shocks:
            return
        sector_names = sorted({
            str(s.get("sector"))
            for s in shocks
            if s.get("sector")
        })
        if not sector_names:
            return
        placeholders = ",".join("?" * len(sector_names))
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                f"SELECT d.name, s.pct_chg, s.main_net_yi "
                f"FROM fact_sector_daily s "
                f"JOIN dim_sector d ON d.ts_code = s.ts_code "
                f"WHERE s.trade_date = ? AND d.name IN ({placeholders})",
                (td, *sector_names),
            ).fetchall()
        by_name = {str(r["name"]): r for r in rows}
        matched = 0
        for sh in shocks:
            name = str(sh.get("sector") or "")
            r = by_name.get(name)
            if r is not None:
                sh["sector_pct_chg"] = safe_pct_chg(r["pct_chg"])
                sh["sector_main_net_yi"] = _round(r["main_net_yi"], 2)
                matched += 1
        # 记入 meta，方便观察匹配率
        if shocks:
            summary.setdefault("meta", {})["market_shock_match"] = {
                "matched": matched,
                "total": len(shocks),
            }

    # --- limit_sprint (冲刺涨停，ths 独家泳池) ---

    def _read_limit_sprint(self, td: str) -> List[Dict[str, Any]]:
        """读 fact_limit_sprint，按 pct_chg DESC 返回。

        Returns:
            形如 ``[{"code": "688585.SH", "name": "上纬新材",
            "close": 16.5, "pct_chg": 18.18, "rise_rate": ...,
            "turnover_rate": ..., "turnover_yi": ..., "free_float_yi": ...,
            "lu_desc": ..., "market_type": "STAR"}, ...]``。
            金额字段统一转为「亿元」单位。
        """
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT ts_code, name, close, pct_chg, rise_rate, "
                "       turnover_rate, turnover, free_float, "
                "       lu_desc, market_type "
                "FROM fact_limit_sprint WHERE trade_date = ? "
                "ORDER BY pct_chg DESC",
                (td,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            out.append({
                "code": r["ts_code"],
                "name": r["name"] or "",
                "close": _round(r["close"], 2),
                "pct_chg": safe_pct_chg(r["pct_chg"]),
                "rise_rate": _round(r["rise_rate"], 2),
                "turnover_rate": _round(r["turnover_rate"], 2),
                "turnover_yi": _round(
                    (r["turnover"] / 1e8) if r["turnover"] else None, 2
                ),
                "free_float_yi": _round(
                    (r["free_float"] / 1e8) if r["free_float"] else None, 2
                ),
                "lu_desc": r["lu_desc"] or "",
                "market_type": r["market_type"] or "",
            })
        return out

    # --- dragon tiger ---

    def _read_dragon_tiger(self, td: str) -> Dict[str, Any]:
        with self.db.connect(readonly=True) as conn:
            # name 优先级：dim_stock（如果填了）→ raw_json.name（top_list 必有）
            # 取 ANY_VALUE 是因为同一只股按 rank_reason 可能多条记录
            agg_rows = conn.execute(
                "SELECT t.ts_code, "
                " SUM(t.net_amount_yi) AS net_yi, "
                " GROUP_CONCAT(t.rank_reason, ' | ') AS reasons, "
                " COALESCE(d.name, MIN(json_extract(t.raw_json, '$.name'))) "
                "   AS stock_name "
                "FROM fact_top_list t "
                "LEFT JOIN dim_stock d ON d.ts_code = t.ts_code "
                "WHERE t.trade_date = ? "
                "GROUP BY t.ts_code "
                "ORDER BY net_yi DESC NULLS LAST",
                (td,),
            ).fetchall()
            inst_rows = conn.execute(
                "SELECT t.ts_code, t.exalter, t.side, t.net_buy_yi, "
                "       a.alias, a.is_famous "
                "FROM fact_top_inst t "
                "LEFT JOIN dim_trader_alias a ON a.exalter = t.exalter "
                "WHERE t.trade_date = ? AND a.is_famous = 1 "
                "ORDER BY ABS(t.net_buy_yi) DESC",
                (td,),
            ).fetchall()
            # 其他席位：未识别为知名游资的所有营业部 / 机构席位
            #   口径 = (a.is_famous IS NULL OR a.is_famous = 0)；按 |净额| DESC
            other_rows = conn.execute(
                "SELECT t.ts_code, t.exalter, t.side, t.net_buy_yi, "
                "       a.alias, a.is_famous, "
                "       COALESCE(d.name, '') AS stock_name "
                "FROM fact_top_inst t "
                "LEFT JOIN dim_trader_alias a ON a.exalter = t.exalter "
                "LEFT JOIN dim_stock d ON d.ts_code = t.ts_code "
                "WHERE t.trade_date = ? "
                "  AND (a.is_famous IS NULL OR a.is_famous = 0) "
                "ORDER BY ABS(t.net_buy_yi) DESC "
                "LIMIT 10",
                (td,),
            ).fetchall()

        stocks = []
        for r in agg_rows:
            reasons_str = str(r["reasons"] or "")
            reasons = reasons_str.split(" | ") if reasons_str else []
            stocks.append({
                "ts_code": str(r["ts_code"]),
                "name": str(r["stock_name"] or ""),
                "net_amount_yi": _round(r["net_yi"], 2),
                "reasons": [x.strip() for x in reasons if x.strip()],
            })

        famous_traders = []
        for r in inst_rows:
            side_str = str(r["side"] or "")
            famous_traders.append({
                "alias": str(r["alias"] or ""),
                "exalter_raw": str(r["exalter"] or ""),
                "stock": str(r["ts_code"] or ""),
                "side": side_str,
                "net_buy_yi": _round(r["net_buy_yi"], 2),
                "source": "alias_table",
            })

        other_traders = []
        for r in other_rows:
            side_str = str(r["side"] or "")
            other_traders.append({
                "exalter": str(r["exalter"] or ""),
                "stock": str(r["ts_code"] or ""),
                "stock_name": str(r["stock_name"] or ""),
                "side": side_str,
                "net_buy_yi": _round(r["net_buy_yi"], 2),
            })

        return {
            "stocks": stocks,
            "famous_traders": famous_traders,
            "other_traders": other_traders,
        }

    # ==================================================================
    # 完整度 / 入库
    # ==================================================================

    def _calc_quality(
        self,
        summary: Dict[str, Any],
        gaps: List[Dict[str, Any]],
        fetch_result: FetchResult,
        *,
        mode: str = "tushare-only",
    ) -> Dict[str, Any]:
        """完整度评分。

        M2 起优先调用 ``MarketSummaryValidator``（schema-driven）；
        失败/无 schema 时回退到 M1b 简化公式以保兼容。
        """
        # 1) 用新 validator
        try:
            report = self.validator.validate(summary)
            data = report.to_dict()
        except Exception as exc:  # noqa: BLE001
            _log.warning("validator 失败，回退 M1b 简化公式: %s", exc)
            data = self._calc_quality_legacy(summary)

        warnings_extra = list(fetch_result.warnings or [])
        if fetch_result.hsgt_is_delayed:
            warnings_extra.append("北向资金为 T-1 日数据（T 日未公布）")
        # 合并 validator 自身的 warnings
        merged_warnings = list(data.get("warnings") or [])
        for w in warnings_extra:
            if w and w not in merged_warnings:
                merged_warnings.append(w)
        data["warnings"] = merged_warnings
        data.setdefault("mode", mode)
        return data

    def _calc_quality_legacy(
        self, summary: Dict[str, Any]
    ) -> Dict[str, Any]:
        l1_filled = sum(
            1 for p in _REQUIRED_L1_PATHS if _path_filled(summary, p)
        )
        l2_filled = sum(
            1 for p in _REQUIRED_L2_PATHS if _path_filled(summary, p)
        )
        l1 = l1_filled / max(len(_REQUIRED_L1_PATHS), 1)
        l2 = l2_filled / max(len(_REQUIRED_L2_PATHS), 1)
        completeness = round(0.6 * l1 + 0.3 * l2, 4)
        return {
            "completeness_score": completeness,
            "l1_complete": round(l1, 4),
            "l2_complete": round(l2, 4),
            "l3_complete": 0.0,
            "numeric_field_rate": None,
            "warnings": [],
        }

    # ==================================================================
    # AI 兜底辅助
    # ==================================================================

    def _run_ai_enrichment(
        self,
        td: str,
        summary: Dict[str, Any],
        cls_result: CLSEnrichResult,
    ) -> AIEnrichPatch:
        """根据 cls_result 调 AIEnricher。失败返回空 patch。"""
        unmatched_sectors = list(cls_result.unmatched_sectors)
        known_plates = list(cls_result.plate_aggregates.keys())[:80]
        unmatched_exalters = self._collect_unmatched_exalters(td, limit=30)
        known_aliases = [
            t.to_dict() for t in self.trader_matcher.list_famous()
        ][:20]
        kpi_text = self._format_market_kpi_for_ai(summary)
        try:
            return self.ai_enricher.enrich(
                trade_date=td,
                unmatched_sectors=unmatched_sectors,
                known_cls_plates=known_plates,
                market_kpi_text=kpi_text,
                unmatched_exalters=unmatched_exalters,
                known_aliases=known_aliases,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("AIEnricher 异常: %s", exc)
            patch = AIEnrichPatch(trade_date=td)
            patch.sectors.error = f"AIEnricher 异常: {exc}"
            return patch

    def _collect_unmatched_exalters(
        self, td: str, *, limit: int = 30
    ) -> List[str]:
        """从 ``fact_top_inst`` 找出 dim_trader_alias 没匹配上的 exalter。"""
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT t.exalter "
                "FROM fact_top_inst t "
                "LEFT JOIN dim_trader_alias a ON a.exalter = t.exalter "
                "WHERE t.trade_date = ? "
                "  AND t.exalter IS NOT NULL AND t.exalter <> '' "
                "  AND a.exalter IS NULL "
                "ORDER BY t.exalter "
                "LIMIT ?",
                (td, int(limit)),
            ).fetchall()
        return [str(r["exalter"]) for r in rows if r["exalter"]]

    def _format_market_kpi_for_ai(self, summary: Dict[str, Any]) -> str:
        ind = summary.get("indices") or {}
        sh = ind.get("sh") or {}
        sz = ind.get("sz") or {}
        cyb = ind.get("cyb") or {}
        breadth = summary.get("breadth") or {}
        sentiment = summary.get("sentiment") or {}
        cap = summary.get("capital_flow") or {}
        turnover = ind.get("total_turnover_yi")
        lines = [
            "- 上证: {} / 深成: {} / 创业: {}".format(
                _fmt_pct(sh.get("pct_chg")),
                _fmt_pct(sz.get("pct_chg")),
                _fmt_pct(cyb.get("pct_chg")),
            ),
            "- 两市成交: {} 亿".format(
                _fmt_num(turnover) if turnover is not None else "-"
            ),
            "- 涨停 {} / 跌停 {} / 炸板 {} / 封板率 {}".format(
                breadth.get("limit_up", "-"),
                breadth.get("limit_down", "-"),
                breadth.get("failed_limit", "-"),
                _fmt_rate(sentiment.get("seal_rate")),
            ),
            "- 最高板 {} 连 / 北向 {} 亿".format(
                sentiment.get("max_height", "-"),
                _fmt_num(cap.get("north_net_yi")),
            ),
        ]
        return "\n".join(lines)

    def _upsert_ai_traders(self, ai_patch: AIEnrichPatch) -> None:
        """把 AI 返回的 ``is_famous=true`` 的别名写入 ``dim_trader_alias``。"""
        if ai_patch is None or not ai_patch.traders.patch:
            return
        raw = ai_patch.traders.patch.get("traders_aliases") or {}
        if not isinstance(raw, dict):
            return
        count = 0
        for exalter, val in raw.items():
            if not isinstance(val, dict):
                continue
            if not val.get("is_famous"):
                continue
            alias = (val.get("alias") or "").strip()
            if not alias:
                continue
            try:
                self.trader_matcher.add(
                    exalter=exalter,
                    alias=alias,
                    is_famous=True,
                    notes=val.get("notes"),
                )
                count += 1
            except Exception as exc:  # noqa: BLE001
                _log.warning("upsert trader alias %r 失败: %s", exalter, exc)
        if count:
            _log.info("AI 新增/更新 %d 个知名游资别名", count)

    def _persist_ai_patch(
        self, td: str, ai_patch: AIEnrichPatch
    ) -> None:
        """把 ai_patch 写入 ``ai_enrich_patches``（按 part 分别落库）。"""
        rows: List[tuple] = []
        now = _now_iso()
        for part_name, part in (
            ("sectors", ai_patch.sectors),
            ("traders", ai_patch.traders),
        ):
            if not part.patch and not part.error:
                continue
            payload = {
                "part": part_name,
                "patch": part.patch,
                "error": part.error,
                "warnings": part.warnings,
            }
            rows.append((
                td,
                part.provider,
                part.model,
                part.prompt_id,
                part.prompt_version,
                json.dumps(payload, ensure_ascii=False),
                int(part.input_tokens or 0),
                int(part.output_tokens or 0),
                int(part.elapsed_ms or 0),
                now,
            ))
        if not rows:
            return
        with self.db.connect() as conn:
            conn.executemany(
                "INSERT INTO ai_enrich_patches ("
                " trade_date, provider, model, prompt_id, prompt_version,"
                " patch_json, input_tokens, output_tokens, elapsed_ms,"
                " created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def _persist_summary(
        self,
        *,
        td: str,
        ptd: str,
        mode: str,
        summary: Dict[str, Any],
        summary_md: str,
        result: MarketSummaryResult,
        fetch_result: FetchResult,
    ) -> None:
        meta = summary.get("meta") or {}
        breadth = summary.get("breadth") or {}
        sentiment = summary.get("sentiment") or {}
        capital = summary.get("capital_flow") or {}
        indices = summary.get("indices") or {}
        quality = summary.get("quality") or {}

        warnings = quality.get("warnings") or []

        payload = (
            td,
            ptd,
            summary.get("schema_version") or SCHEMA_VERSION,
            mode,
            meta.get("generated_at") or _now_iso(),
            int(meta.get("elapsed_ms") or 0),
            int(meta.get("api_call_count") or fetch_result.api_call_count),
            float(quality.get("completeness_score") or 0.0),
            _to_int(breadth.get("limit_up")),
            _to_int(breadth.get("limit_down")),
            _to_float(sentiment.get("seal_rate")),
            _to_float(sentiment.get("promotion_rate")),
            _to_int(sentiment.get("max_height")),
            sentiment.get("max_stock"),
            _to_float(capital.get("north_net_yi")),
            _to_float(indices.get("total_turnover_yi")),
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            summary_md,
            json.dumps(warnings, ensure_ascii=False),
            json.dumps(summary.get("gaps") or [], ensure_ascii=False),
            json.dumps(summary.get("conflicts") or [], ensure_ascii=False),
        )
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO market_summaries ("
                " trade_date, prev_trade_date, schema_version, mode,"
                " generated_at, elapsed_ms, api_call_count, completeness,"
                " limit_up_count, limit_down_count, seal_rate,"
                " promotion_rate, max_height, max_stock_name,"
                " north_net_yi, total_turnover_yi,"
                " summary_json, summary_md,"
                " warnings_json, gaps_json, conflicts_json"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )


# ---------------------------------------------------------------------------
# 顶层便捷函数
# ---------------------------------------------------------------------------


def build_market_summary(**kwargs) -> MarketSummaryResult:
    """直接调用 ``MarketSummaryService().build(**kwargs)``。"""
    return MarketSummaryService().build(**kwargs)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _finalize(result: MarketSummaryResult, t0: float) -> MarketSummaryResult:
    result.elapsed_ms = int((time.time() - t0) * 1000)
    return result


def _maybe_cancelled(
    cb: Optional[Callable[[], bool]],
    result: MarketSummaryResult,
) -> bool:
    if cb is None:
        return False
    try:
        if cb():
            result.cancelled = True
            result.warnings.append("用户取消")
            return True
    except Exception:
        pass
    return False


def _safe_json_loads(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return None


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return f


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int) -> Optional[float]:
    f = _to_float(value)
    if f is None:
        return None
    return round(f, digits)


def _fmt_pct(v: Any) -> str:
    """格式化 ``+1.23%``；None 返回 ``-``。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f:+.2f}%"


def _fmt_num(v: Any) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f:.2f}"


def _fmt_rate(v: Any) -> str:
    """0~1 区间转 ``XX.X%``。"""
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-"
    return f"{f * 100:.1f}%"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _path_filled(d: Dict[str, Any], path: str) -> bool:
    """检查嵌套 dict 中 ``a.b.c`` 路径是否非空。"""
    cur: Any = d
    for key in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return False
        if cur is None:
            return False
    return cur not in ("", [], {}, None)


__all__ = [
    "MarketSummaryResult",
    "MarketSummaryService",
    "build_market_summary",
    "SCHEMA_VERSION",
]
