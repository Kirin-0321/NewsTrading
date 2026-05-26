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

from services.market.market_db import MarketDB, get_market_db
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
from services.market.tushare_client import TushareClient
from services.market.tushare_fetcher import (
    INDEX_CODES,
    FetchResult,
    TushareMarketFetcher,
)

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
    ) -> None:
        self.db = db or get_market_db()
        self.db.ensure_schema()
        # client 懒初始化：如果命中 cache，build() 整个流程都不需要它
        self._client = client
        self._fetcher = fetcher
        self.renderer = renderer or MarketSummaryRenderer()

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

    # ==================================================================
    # 主入口
    # ==================================================================

    def build(
        self,
        trade_date: Optional[str] = None,
        *,
        mode: str = "tushare-only",
        top_sector_n: int = 10,
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

        # ---- 1) mode 校验 / 降级 ----
        if mode not in VALID_MODES:
            result.warnings.append(
                f"未知 mode={mode!r}，降级为 tushare-only"
            )
            mode = "tushare-only"
            result.mode = mode
        if mode != "tushare-only":
            result.warnings.append(
                f"mode={mode} 暂未在 Phase M1b 实现，降级为 tushare-only"
                "（M2 阶段会接入 CLS / AI 兜底）"
            )
            mode = "tushare-only"
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

            # ---- 3) 缓存命中 ----
            if not force_refresh:
                cached = self.get(td)
                if cached.ok:
                    progress("命中缓存，直接复用…")
                    cached.from_cache = True
                    cached.warnings.extend(result.warnings)
                    cached.elapsed_ms = int((time.time() - t0) * 1000)
                    return cached

            # ---- 4) Tushare 拉数据 ----
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

            # ---- 6) 计算完整度 ----
            quality = self._calc_quality(summary, gaps, fetch_result)
            summary["quality"] = quality
            summary["gaps"] = gaps
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

        # dragon_tiger
        dragon_tiger = self._read_dragon_tiger(td)

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
            "limit_ladder": ladder,
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

            kpl_rows = conn.execute(
                "SELECT ts_code, "
                "       json_extract(raw_json, '$.name') AS name, "
                "       theme, status, cons_nums, limit_up_time, open_times "
                "FROM fact_limit_stock "
                "WHERE trade_date = ? AND limit_type = 'U' "
                "  AND cons_nums IS NOT NULL",
                (td,),
            ).fetchall()
        kpl_dicts = [dict(r) for r in kpl_rows]

        breadth = {
            "limit_up": u_cnt or None,
            "limit_down": d_cnt or None,
            "failed_limit": z_cnt or None,
            "advance": None,
            "decline": None,
        }
        if not u_cnt:
            gaps.append({
                "field": "breadth.limit_up",
                "reason": "fact_limit_stock 无 U 数据",
            })

        # 情绪指标
        ladder = build_limit_ladder(kpl_dicts)
        height, stock, theme = find_max_height(kpl_dicts)
        seal_rate = calc_seal_rate(u_cnt, z_cnt)
        fail_rate = calc_fail_rate(u_cnt, z_cnt)
        promo_rate = calc_promotion_rate(
            kpl_dicts, fetch_result.kpl_prev_rows
        )
        sentiment = {
            "seal_rate": seal_rate,
            "seal_rate_prev": None,
            "promotion_rate": promo_rate,
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
        with self.db.connect(readonly=True) as conn:
            row = conn.execute(
                "SELECT actual_date, is_delayed, north_net_yi, south_net_yi "
                "FROM fact_hsgt_daily WHERE trade_date = ?",
                (td,),
            ).fetchone()
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
                "main_net_yi": None,
            }
        return {
            "north_net_yi": _round(row["north_net_yi"], 2),
            "north_data_date": (
                str(row["actual_date"]) if row["actual_date"] else None
            ),
            "north_is_delayed": bool(row["is_delayed"]),
            "south_net_yi": _round(row["south_net_yi"], 2),
            "main_net_yi": None,
        }

    # --- sectors top N ---

    def _read_sectors_top(
        self,
        td: str,
        top_n: int,
        gaps: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        with self.db.connect(readonly=True) as conn:
            rows = conn.execute(
                "SELECT s.ts_code, s.pct_chg, s.main_net_yi, s.main_elg_yi, "
                "       s.main_lg_yi, s.limit_up_count, s.pct_chg_5d, "
                "       s.rank_today, d.name "
                "FROM fact_sector_daily s "
                "LEFT JOIN dim_sector d ON d.ts_code = s.ts_code "
                "WHERE s.trade_date = ? "
                "ORDER BY s.pct_chg DESC NULLS LAST LIMIT ?",
                (td, top_n),
            ).fetchall()
        if not rows:
            gaps.append({
                "field": "sectors_top",
                "reason": "fact_sector_daily 该日无数据",
            })
            return []

        out: List[Dict[str, Any]] = []
        for i, r in enumerate(rows, 1):
            out.append({
                "rank": i,
                "ts_code": str(r["ts_code"]),
                "name": str(r["name"] or ""),
                "pct_chg": safe_pct_chg(r["pct_chg"]),
                "pct_chg_5d": safe_pct_chg(r["pct_chg_5d"], bound=200.0),
                "limit_up_count": (
                    int(r["limit_up_count"])
                    if r["limit_up_count"] is not None else None
                ),
                "main_net_yi": _round(r["main_net_yi"], 2),
                "main_elg_yi": _round(r["main_elg_yi"], 2),
                "main_lg_yi": _round(r["main_lg_yi"], 2),
                "leaders": [],          # M2 阶段填充
                "catalysts": [],        # M2 阶段填充
                "high_risk": None,
                "field_sources": {
                    "pct_chg": "tushare",
                    "main_net_yi": "tushare",
                },
            })
        return out

    # --- dragon tiger ---

    def _read_dragon_tiger(self, td: str) -> Dict[str, Any]:
        with self.db.connect(readonly=True) as conn:
            agg_rows = conn.execute(
                "SELECT ts_code, "
                " SUM(net_amount_yi) AS net_yi, "
                " GROUP_CONCAT(rank_reason, ' | ') AS reasons "
                "FROM fact_top_list WHERE trade_date = ? "
                "GROUP BY ts_code "
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

        stocks = []
        for r in agg_rows:
            reasons_str = str(r["reasons"] or "")
            reasons = reasons_str.split(" | ") if reasons_str else []
            stocks.append({
                "ts_code": str(r["ts_code"]),
                "name": None,                  # M2 阶段 join dim_stock
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

        return {
            "stocks": stocks,
            "famous_traders": famous_traders,
        }

    # ==================================================================
    # 完整度 / 入库
    # ==================================================================

    def _calc_quality(
        self,
        summary: Dict[str, Any],
        gaps: List[Dict[str, Any]],
        fetch_result: FetchResult,
    ) -> Dict[str, Any]:
        l1_filled = sum(
            1 for path in _REQUIRED_L1_PATHS if _path_filled(summary, path)
        )
        l2_filled = sum(
            1 for path in _REQUIRED_L2_PATHS if _path_filled(summary, path)
        )
        l1_total = len(_REQUIRED_L1_PATHS)
        l2_total = len(_REQUIRED_L2_PATHS)
        l1 = l1_filled / l1_total if l1_total else 0.0
        l2 = l2_filled / l2_total if l2_total else 0.0
        # L3 M1b 阶段不算（catalysts / famous_traders 留 M2）
        l3 = 0.0
        completeness = round(0.6 * l1 + 0.3 * l2 + 0.1 * l3, 4)

        warnings = list(fetch_result.warnings)
        if fetch_result.hsgt_is_delayed:
            warnings.append("北向资金为 T-1 日数据（T 日未公布）")

        return {
            "completeness_score": completeness,
            "l1_complete": round(l1, 4),
            "l2_complete": round(l2, 4),
            "l3_complete": l3,
            "numeric_field_rate": None,  # M2 阶段统计
            "warnings": warnings,
        }

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
