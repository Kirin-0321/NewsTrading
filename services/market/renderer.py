"""MarketSummary → compact Markdown 渲染（Phase M1b）。

设计目标
--------
* **稳定输出**：相同 ``summary`` dict 输入 → 完全一致的输出（含字段顺序），
  保证后续注入 AI 分析 prompt 时能命中 OpenAI/DeepSeek 的 prompt-cache，省 token。
* **缺失容忍**：任何字段缺失/None 渲染成 ``—``，不抛异常。
* **行数控制**：compact 版控制在 150~300 行，适合塞进对话窗口。

不写文件——返回字符串，由上层 ``MarketSummaryService`` 落到 ``market_summaries.summary_md``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EMPTY = "—"
LADDER_KEYS_DEFAULT = ("4连板及以上", "3连板", "2连板", "首板")


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------


class MarketSummaryRenderer:
    """compact Markdown 渲染器（无状态，线程安全）。"""

    def render_compact(self, summary: Dict[str, Any]) -> str:
        """渲染 ``MarketSummary`` 为 compact Markdown 字符串。"""
        meta = summary.get("meta") or {}
        trade_date = meta.get("trade_date") or "未知日期"

        parts: List[str] = []
        parts.append(f"# {trade_date} 盘后数据")
        parts.append("")
        parts.append(self._render_meta(meta))
        parts.append("")
        parts.append(self._render_indices(summary.get("indices") or {}))
        parts.append("")
        parts.append(self._render_breadth_sentiment(
            summary.get("breadth") or {},
            summary.get("sentiment") or {},
        ))
        parts.append("")
        parts.append(self._render_capital_flow(
            summary.get("capital_flow") or {}
        ))
        parts.append("")
        parts.append(self._render_sectors(
            summary.get("sectors_top") or [],
            summary.get("sectors_bottom") or [],
        ))
        parts.append("")
        parts.append(self._render_limit_ladder(
            summary.get("limit_ladder") or {}
        ))
        parts.append("")
        parts.append(self._render_limit_sprint(
            summary.get("limit_sprint") or []
        ))
        parts.append("")
        parts.append(self._render_dragon_tiger(
            summary.get("dragon_tiger") or {}
        ))
        parts.append("")
        parts.append(self._render_market_shock(
            summary.get("market_shock") or []
        ))
        parts.append("")
        parts.append(self._render_regulation(
            summary.get("regulation") or []
        ))
        parts.append("")
        parts.append(self._render_quality(
            summary.get("quality") or {},
            summary.get("gaps") or [],
        ))

        text = "\n".join(parts).rstrip() + "\n"
        return text

    # ------------------------------------------------------------------
    # 分节
    # ------------------------------------------------------------------

    def _render_meta(self, meta: Dict[str, Any]) -> str:
        bits = [
            f"- 交易日: **{_v(meta.get('trade_date'))}**"
            f"（上一交易日 {_v(meta.get('prev_trade_date'))}）",
            f"- 生成时间: {_v(meta.get('generated_at'))}"
            f" / 模式: {_v(meta.get('mode'))}"
            f" / Tushare 调用 {_v(meta.get('api_call_count'))} 次"
            f" / 耗时 {_fmt_elapsed(meta.get('elapsed_ms'))}",
        ]
        sources = meta.get("sources")
        if isinstance(sources, (list, tuple)) and sources:
            bits.append(f"- 数据来源: {' / '.join(str(s) for s in sources)}")
        return "\n".join(bits)

    def _render_indices(self, indices: Dict[str, Any]) -> str:
        order = [
            ("sh", "上证"),
            ("sz", "深成指"),
            ("cyb", "创业板"),
            ("hs300", "沪深300"),
            ("kc50", "科创50"),
            ("zz500", "中证500"),
            ("zz1000", "中证1000"),
        ]
        lines: List[str] = ["## 一、市场总览", ""]
        lines.append("| 指数 | 收盘 | 涨跌幅 | 成交额（亿） |")
        lines.append("|------|------|-------|------------|")
        for key, label in order:
            row = indices.get(key) or {}
            if not isinstance(row, dict) or not row:
                continue
            lines.append(
                f"| {label} | {_fmt_num(row.get('close'))} "
                f"| {_fmt_pct(row.get('pct_chg'))} "
                f"| {_fmt_num(row.get('amount_yi'))} |"
            )
        total = indices.get("total_turnover_yi")
        prev = indices.get("prev_turnover_yi")
        if total is not None or prev is not None:
            lines.append("")
            lines.append(
                f"- 两市成交额: **{_fmt_num(total)} 亿**"
                f"（昨日 {_fmt_num(prev)} 亿）"
            )
        return "\n".join(lines)

    def _render_breadth_sentiment(
        self,
        breadth: Dict[str, Any],
        sentiment: Dict[str, Any],
    ) -> str:
        lines: List[str] = ["## 二、情绪指标", ""]
        u = breadth.get("limit_up")
        d = breadth.get("limit_down")
        f = breadth.get("failed_limit")
        lines.append(
            f"- 涨停 **{_v(u)}** / 跌停 **{_v(d)}** / 炸板 **{_v(f)}**"
        )
        adv = breadth.get("advance")
        dec = breadth.get("decline")
        unc = breadth.get("unchanged")
        adv5 = breadth.get("advance_5")
        dec5 = breadth.get("decline_5")
        adv_pct = breadth.get("advance_pct")
        total = breadth.get("total")
        if adv is not None or dec is not None:
            mid_bits: List[str] = [
                f"上涨 **{_v(adv)}**",
                f"下跌 **{_v(dec)}**",
            ]
            if unc is not None:
                mid_bits.append(f"平 {_v(unc)}")
            if total is not None:
                mid_bits.append(f"样本 {_v(total)}")
            line = " / ".join(mid_bits)
            if adv_pct is not None:
                line += f"（涨家数占比 {_fmt_pct_ratio(adv_pct)}）"
            lines.append(f"- {line}")
            if adv5 is not None or dec5 is not None:
                lines.append(
                    f"- 大涨(≥5%) **{_v(adv5)}** / "
                    f"大跌(≤-5%) **{_v(dec5)}**"
                )

        seal_rate = sentiment.get("seal_rate")
        seal_rate_prev = sentiment.get("seal_rate_prev")
        promo = sentiment.get("promotion_rate")
        fail = sentiment.get("fail_rate")
        prev_suffix = ""
        if seal_rate_prev is not None:
            prev_suffix = (
                f"（昨日 {_fmt_pct_ratio(seal_rate_prev)}）"
            )
        lines.append(
            f"- 封板率 **{_fmt_pct_ratio(seal_rate)}**{prev_suffix} / "
            f"晋级率 {_fmt_pct_ratio(promo)} / "
            f"炸板率 {_fmt_pct_ratio(fail)}"
        )
        promo_detail = sentiment.get("promotion_detail") or {}
        if promo_detail:
            prev_u = promo_detail.get("prev_u_count")
            still_u = promo_detail.get("today_still_u_count")
            failed = promo_detail.get("today_failed_count")
            lines.append(
                f"- 晋级明细: 昨涨停 {_v(prev_u)} 只 → 今继续涨停 "
                f"**{_v(still_u)}** 只 / 失败 {_v(failed)} 只"
            )

        height = sentiment.get("max_height")
        stock = sentiment.get("max_stock")
        sector = sentiment.get("max_sector")
        if height or stock:
            lines.append(
                f"- 市场最高板: **{_v(height)}板** "
                f"《{_v(stock)}》 / 题材 {_v(sector)}"
            )
        return "\n".join(lines)

    def _render_capital_flow(self, cf: Dict[str, Any]) -> str:
        lines: List[str] = ["## 三、北向 / 主力", ""]
        north = cf.get("north_net_yi")
        north_date = cf.get("north_data_date")
        delayed = cf.get("north_is_delayed")
        suffix = ""
        if delayed:
            suffix = f"（⚠️ T-1 数据，实际日期 {_v(north_date)}）"
        lines.append(f"- 北向资金净流入: **{_fmt_num(north)} 亿**{suffix}")
        south = cf.get("south_net_yi")
        if south is not None:
            lines.append(f"- 南向资金净流入: {_fmt_num(south)} 亿")
        main = cf.get("main_net_yi")
        if main is not None:
            src = cf.get("main_net_source")
            note = f"（口径：{src}）" if src else ""
            lines.append(
                f"- 全市场主力净流入: **{_fmt_num(main)} 亿**{note}"
            )
        return "\n".join(lines)

    def _render_sectors(
        self,
        top: Sequence[Dict[str, Any]],
        bottom: Sequence[Dict[str, Any]],
    ) -> str:
        """板块强弱榜：Top N 涨幅 + Bottom N 跌幅 一节内分两表。"""
        lines: List[str] = ["## 四、板块强弱榜", ""]

        # ---- Top ----
        # v3 聚类版：service 已按 top_sector_n（默认 30）切好，
        # renderer 不再做 [:N] 二次切片，全量渲染
        cap_top = len(top)
        lines.append(f"### 涨幅 Top {cap_top}（聚类后）")
        if not top:
            lines.append("（无数据）")
        else:
            lines.append(
                "| # | 板块 | 成员 | 涨幅 | 5日累计 | "
                "主力净流入(亿) | 涨停数 | 龙头 | 催化 |"
            )
            lines.append(
                "|---|------|------|------|--------|"
                "--------------|------|------|------|"
            )
            for s in top:
                leaders = s.get("leaders") or []
                leader_str = "／".join(
                    _format_leader(le) for le in leaders[:3]
                ) or EMPTY
                catalysts = s.get("catalysts") or []
                catalyst_str = "；".join(
                    str(c.get("text") if isinstance(c, dict) else c)
                    for c in catalysts[:2]
                ) or EMPTY
                # 聚类组员数：×N（N>1 才显示）
                mc = s.get("members_count") or 1
                mc_str = f"×{mc}" if mc > 1 else "—"
                lines.append(
                    f"| {_v(s.get('rank'))} "
                    f"| {_v(s.get('name'))} "
                    f"| {mc_str} "
                    f"| {_fmt_pct(s.get('pct_chg'))} "
                    f"| {_fmt_pct(s.get('pct_chg_5d'))} "
                    f"| {_fmt_num(s.get('main_net_yi'))} "
                    f"| {_v(s.get('limit_up_count'))} "
                    f"| {leader_str} "
                    f"| {catalyst_str} |"
                )

        # ---- Bottom ----
        # v3 同 Top，service 切好（默认 15）
        cap_bot = len(bottom)
        lines.append("")
        lines.append(f"### 跌幅 Bottom {cap_bot}（聚类后）")
        if not bottom:
            lines.append("（无数据）")
        else:
            lines.append(
                "| # | 板块 | 成员 | 跌幅 | 5日累计 | "
                "主力净流出(亿) | 跌停数 | 领跌 |"
            )
            lines.append(
                "|---|------|------|------|--------|"
                "--------------|------|------|"
            )
            for s in bottom:
                laggards = s.get("laggards") or []
                laggard_str = "／".join(
                    _format_leader(le) for le in laggards[:3]
                ) or EMPTY
                mc = s.get("members_count") or 1
                mc_str = f"×{mc}" if mc > 1 else "—"
                lines.append(
                    f"| {_v(s.get('rank'))} "
                    f"| {_v(s.get('name'))} "
                    f"| {mc_str} "
                    f"| {_fmt_pct(s.get('pct_chg'))} "
                    f"| {_fmt_pct(s.get('pct_chg_5d'))} "
                    f"| {_fmt_num(s.get('main_net_yi'))} "
                    f"| {_v(s.get('limit_down_count'))} "
                    f"| {laggard_str} |"
                )

        return "\n".join(lines)

    def _render_limit_ladder(
        self,
        ladder: Dict[str, List[Dict[str, Any]]],
    ) -> str:
        lines: List[str] = ["## 五、连板梯队", ""]
        if not ladder:
            lines.append("（无数据）")
            return "\n".join(lines)
        # 用固定顺序，且把可能多出来的高连板自定义键也带上
        keys = list(LADDER_KEYS_DEFAULT)
        for k in ladder.keys():
            if k not in keys:
                keys.insert(0, k)  # 自定义高连板键放最前
        for k in keys:
            items = ladder.get(k) or []
            if not items:
                continue
            preview = "、".join(
                _format_ladder_item(it) for it in items[:6]
            )
            extra = "" if len(items) <= 6 else f" 等 {len(items)} 只"
            lines.append(f"- **{k}**（{len(items)}）: {preview}{extra}")
        return "\n".join(lines)

    def _render_limit_sprint(
        self,
        sprint: Sequence[Dict[str, Any]],
    ) -> str:
        """冲刺涨停池（同花顺独家，盘中接近涨停未封住的股）。

        输出形如::

            ## 五·B、冲刺涨停（盘中接近涨停未封）

            - 上纬新材[688585.SH] STAR +18.18%·换手3.22%·成交27.29亿
            - 线上线下[300959.SZ] GEM +17.37%·换手23.17%
            - ...（前 10 只）
        """
        lines: List[str] = ["## 五·B、冲刺涨停（盘中接近涨停未封）", ""]
        if not sprint:
            lines.append("（无数据）")
            return "\n".join(lines)
        for it in sprint[:10]:
            name = it.get("name") or ""
            code = it.get("code") or ""
            mt = it.get("market_type") or ""
            pct = it.get("pct_chg")
            tor = it.get("turnover_rate")
            tov_yi = it.get("turnover_yi")
            lu_desc = it.get("lu_desc") or ""

            head = f"{name}[{code}]" if code else name
            extras: List[str] = []
            if mt:
                extras.append(str(mt))
            if isinstance(pct, (int, float)):
                extras.append(f"+{pct:.2f}%")
            if isinstance(tor, (int, float)):
                extras.append(f"换手{tor:.2f}%")
            if isinstance(tov_yi, (int, float)):
                extras.append(f"成交{tov_yi:.2f}亿")
            if lu_desc:
                extras.append(str(lu_desc))
            tail = "·".join(extras) if extras else ""
            lines.append(f"- {head} {tail}".rstrip())
        if len(sprint) > 10:
            lines.append(f"- … 等共 {len(sprint)} 只冲刺股")
        return "\n".join(lines)

    def _render_dragon_tiger(self, dt: Dict[str, Any]) -> str:
        lines: List[str] = ["## 六、龙虎榜", ""]
        stocks = dt.get("stocks") or []
        if stocks:
            lines.append(
                f"- 上榜个股 {len(stocks)} 只，TOP5 净买入:"
            )
            top5 = sorted(
                stocks,
                key=lambda x: _to_float(x.get("net_amount_yi")) or -1e9,
                reverse=True,
            )[:5]
            for x in top5:
                reasons = x.get("reasons") or []
                reason_str = ""
                if reasons:
                    head = "；".join(str(r) for r in reasons[:2])
                    extra = "" if len(reasons) <= 2 else f"… 等 {len(reasons)} 条"
                    reason_str = f"，上榜原因：{head}{extra}"
                lines.append(
                    f"  - {_v(x.get('name'))}（{_v(x.get('ts_code'))}）"
                    f"净买入 **{_fmt_num(x.get('net_amount_yi'))} 亿**"
                    f"{reason_str}"
                )
        famous = dt.get("famous_traders") or []
        if famous:
            lines.append("- 已识别游资席位:")
            for f in famous[:8]:
                side_raw = str(f.get("side") or "").lower()
                side = "买" if side_raw == "buy" else "卖"
                lines.append(
                    f"  - {_v(f.get('alias'))} "
                    f"[{side}] {_v(f.get('stock'))} "
                    f"净额 {_fmt_num(f.get('net_buy_yi'))} 亿"
                )
        others = dt.get("other_traders") or []
        if others:
            lines.append(
                f"- 其他席位 Top {min(10, len(others))}（按 |净额| 排序）:"
            )
            for o in others[:10]:
                side_raw = str(o.get("side") or "").lower()
                side = "买" if side_raw == "buy" else "卖"
                stock_label = (
                    f"{o.get('stock_name')}({o.get('stock')})"
                    if o.get("stock_name") else _v(o.get("stock"))
                )
                exalter = str(o.get("exalter") or "")
                # 营业部名经常很长，截断显示
                if len(exalter) > 22:
                    exalter = exalter[:22] + "…"
                lines.append(
                    f"  - {exalter} "
                    f"[{side}] {stock_label} "
                    f"净额 {_fmt_num(o.get('net_buy_yi'))} 亿"
                )
        if not stocks and not famous and not others:
            lines.append("（今日龙虎榜暂无数据）")
        return "\n".join(lines)

    def _render_market_shock(self, shocks: Sequence[Dict[str, Any]]) -> str:
        lines: List[str] = ["## 七、板块异动时间线", ""]
        if not shocks:
            lines.append("（今日无显著异动数据）")
            return "\n".join(lines)
        sorted_shocks = sorted(
            list(shocks),
            key=lambda x: (
                str(x.get("first_shock_time") or ""),
                str(x.get("sector") or ""),
            ),
        )
        for s in sorted_shocks[:15]:
            arrow = "↑" if str(s.get("status") or "").lower() == "up" else "↓"
            # sector_pct_chg + sector_main_net_yi 由 service._enrich_market_shock
            # 注入，命中不上的板块字段为 None，正常显示 —
            pct = s.get("sector_pct_chg")
            main = s.get("sector_main_net_yi")
            tail_bits: List[str] = [f"共 {_v(s.get('shock_count'))} 只票"]
            if pct is not None:
                tail_bits.append(f"板块当日 {_fmt_pct(pct)}")
            if main is not None:
                tail_bits.append(f"主力净 {_fmt_num(main)} 亿")
            tail = "（" + "／".join(tail_bits) + "）"
            lines.append(
                f"- {_v(s.get('first_shock_time'))} "
                f"{arrow} {_v(s.get('sector'))} "
                f"{tail}"
            )
        return "\n".join(lines)

    def _render_regulation(self, regs: Sequence[Any]) -> str:
        lines: List[str] = ["## 八、监管警示", ""]
        if not regs:
            lines.append("（今日无监管警示）")
            return "\n".join(lines)
        for r in regs[:10]:
            if isinstance(r, dict):
                lines.append(f"- {_v(r.get('title') or r.get('text'))}")
            else:
                lines.append(f"- {_v(r)}")
        return "\n".join(lines)

    def _render_quality(
        self,
        quality: Dict[str, Any],
        gaps: Sequence[Dict[str, Any]],
    ) -> str:
        lines: List[str] = ["## 九、数据完整性", ""]
        score = quality.get("completeness_score")
        l1 = quality.get("l1_complete")
        l2 = quality.get("l2_complete")
        l3 = quality.get("l3_complete")
        numeric = quality.get("numeric_field_rate")
        lines.append(
            f"- 完整度评分: **{_fmt_pct_ratio(score)}**"
            f" / L1 {_fmt_pct_ratio(l1)}"
            f" / L2 {_fmt_pct_ratio(l2)}"
            f" / L3 {_fmt_pct_ratio(l3)}"
            f" / 数值字段率 {_fmt_pct_ratio(numeric)}"
        )
        warnings = quality.get("warnings") or []
        for w in warnings:
            lines.append(f"- ⚠️ {w}")
        if gaps:
            lines.append("")
            lines.append("**缺失字段（前 10 条）**:")
            for g in gaps[:10]:
                if isinstance(g, dict):
                    field = g.get("field") or g.get("path") or ""
                    reason = g.get("reason")
                    if not reason:
                        layer = g.get("layer")
                        filled = g.get("filled_count")
                        total = g.get("total_count")
                        ratio = g.get("filled_ratio")
                        bits = []
                        if layer:
                            bits.append(str(layer))
                        if filled is not None and total is not None:
                            bits.append(f"{filled}/{total}")
                        if isinstance(ratio, (int, float)):
                            bits.append(_fmt_pct_ratio(ratio))
                        reason = " ".join(bits) or "（无原因）"
                    lines.append(f"- `{field}`: {reason}")
                else:
                    lines.append(f"- {g}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _v(value: Any) -> str:
    """通用值渲染：None/空 → EMPTY，否则 str。"""
    if value is None or value == "":
        return EMPTY
    return str(value)


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_num(value: Any, *, digits: int = 2) -> str:
    f = _to_float(value)
    if f is None:
        return EMPTY
    return f"{f:.{digits}f}"


def _fmt_pct(value: Any) -> str:
    """涨跌幅：value 单位已经是 %，渲染成 ``+1.23%`` 形式。"""
    f = _to_float(value)
    if f is None:
        return EMPTY
    return f"{f:+.2f}%"


def _fmt_pct_ratio(value: Any) -> str:
    """0~1 区间的比例，渲染成 ``72.6%``。"""
    f = _to_float(value)
    if f is None:
        return EMPTY
    return f"{f * 100:.1f}%"


def _fmt_elapsed(ms: Any) -> str:
    f = _to_float(ms)
    if f is None:
        return EMPTY
    if f < 1000:
        return f"{int(f)}ms"
    return f"{f / 1000:.1f}s"


def _format_leader(le: Any) -> str:
    if not isinstance(le, dict):
        return str(le)
    name = le.get("name") or ""
    pct = _fmt_pct(le.get("pct_chg")) if le.get("pct_chg") is not None else ""
    status = le.get("status") or ""
    bits = [name]
    if pct and pct != EMPTY:
        bits.append(pct)
    if status:
        bits.append(status)
    return " ".join(bits)


def _format_ladder_item(item: Any) -> str:
    """连板梯队单只股票渲染（三源融合后版本）。

    输出形如::

        泰坦股份[003036.SZ] 2天2板·量子科技·拟收购+SaaS转型·封板率82%·炸2次

    字段优先级：
      * tag（"2天2板"，含间断梯队信息）> 旧 cons_nums（"2板"）
      * lu_desc（涨停原因，催化剂）单独成段，不与 theme 重复
      * limit_up_suc_rate（一年封板率）格式化为百分比
      * 缺失字段静默跳过（兼容老旧数据）
    """
    if not isinstance(item, dict):
        return str(item)
    name = item.get("name") or ""
    code = item.get("code") or ""
    theme = item.get("theme") or ""
    cons = item.get("cons_nums")
    open_times = item.get("open_times")
    tag = item.get("tag") or ""
    lu_desc = item.get("lu_desc") or ""
    suc_rate = item.get("limit_up_suc_rate")

    extras: List[str] = []
    # tag 优先（"2天2板" 比 "2板" 信息更全）
    if tag:
        extras.append(str(tag))
    elif isinstance(cons, int) and cons >= 1:
        extras.append(f"{cons}板")
    if theme:
        extras.append(str(theme))
    if lu_desc and lu_desc != theme:
        extras.append(str(lu_desc))
    if isinstance(suc_rate, (int, float)) and 0 <= suc_rate <= 1:
        extras.append(f"封板率{suc_rate * 100:.0f}%")
    if isinstance(open_times, int) and open_times > 0:
        extras.append(f"炸{open_times}次")
    suffix = (" " + "·".join(extras)) if extras else ""

    head = f"{name}[{code}]" if code else name
    return f"{head}{suffix}"


__all__ = ["MarketSummaryRenderer"]
