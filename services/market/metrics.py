"""派生指标纯函数库（Phase M1b）。

本模块**不做 IO**——所有函数都接受 Python 基本类型（dict/list/float/int），
输出基本类型。便于:

* 单元测试（无需 DB / 网络 mock）
* 任何阶段拼装 ``MarketSummary`` 时随处可调

涉及指标
--------
* 金额/单位归一化（``normalize_amount_to_yi``）
* 涨跌幅清洗（``safe_pct_chg``）
* 封板率（``calc_seal_rate``）
* 晋级率（``calc_promotion_rate``）
* 失败率 / 炸板率（``calc_fail_rate``）
* 连板梯队（``build_limit_ladder``）
* 市场最高板（``find_max_height``）
* 板块 5 日累计涨幅（``calc_sector_5d_pct``）
* 板块当日排名（``rank_sectors_by_pct``）

测试基线（基于 ``_tushare_field_audit.md`` 20260525 实测）
--------------------------------------------------------
* ``calc_seal_rate(90, 34) ≈ 0.726``
* ``find_max_height(kpl) → (4, "四环生物", "生物制品·治愈类")``
* ``normalize_amount_to_yi(332577.01, "万元") → 33.2577``
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 单位归一化
# ---------------------------------------------------------------------------

#: Tushare 各接口返回金额的单位 → 转换到「亿元」的乘数
_UNIT_TO_YI: Dict[str, float] = {
    "元": 1e-8,
    "千元": 1e-5,
    "万元": 1e-4,
    "百万元": 1e-2,
    "亿元": 1.0,
    "亿": 1.0,
}


def normalize_amount_to_yi(value: Any, source_unit: str) -> Optional[float]:
    """把任意金额字段归一化到「亿元」浮点数。

    Args:
        value: 原始数值（float / int / str / None）。
        source_unit: 来源单位，须是 ``_UNIT_TO_YI`` 的键。

    Returns:
        归一化后的「亿元」float；输入为 None / 空 / 非数值 时返回 ``None``。

    Raises:
        ValueError: ``source_unit`` 不识别。
    """
    if source_unit not in _UNIT_TO_YI:
        raise ValueError(
            f"未识别的单位 {source_unit!r}，可选: {list(_UNIT_TO_YI)}"
        )
    num = _to_float(value)
    if num is None:
        return None
    return round(num * _UNIT_TO_YI[source_unit], 4)


def safe_pct_chg(value: Any, *, bound: float = 25.0) -> Optional[float]:
    """涨跌幅清洗。

    * 输入 None / 空 / 非数值 → None
    * 越界（|val| > bound）→ None（保留为缺失，避免污染均值/排序）
    * 否则返回 round 到 2 位的 float
    """
    num = _to_float(value)
    if num is None:
        return None
    if abs(num) > bound:
        return None
    return round(num, 2)


# ---------------------------------------------------------------------------
# 情绪指标
# ---------------------------------------------------------------------------


def calc_seal_rate(
    limit_up_count: Optional[int], failed_limit_count: Optional[int]
) -> Optional[float]:
    """封板率 = 涨停 / (涨停 + 炸板)。

    分母为 0 或缺失 → None。
    """
    u = _to_int(limit_up_count)
    z = _to_int(failed_limit_count)
    if u is None or z is None:
        return None
    total = u + z
    if total <= 0:
        return None
    return round(u / total, 4)


def calc_fail_rate(
    limit_up_count: Optional[int], failed_limit_count: Optional[int]
) -> Optional[float]:
    """炸板率 = 炸板 / (涨停 + 炸板)。"""
    u = _to_int(limit_up_count)
    z = _to_int(failed_limit_count)
    if u is None or z is None:
        return None
    total = u + z
    if total <= 0:
        return None
    return round(z / total, 4)


def calc_promotion_rate(
    kpl_today: Sequence[Dict[str, Any]],
    kpl_prev: Sequence[Dict[str, Any]],
) -> Optional[float]:
    """晋级率 = T 日连板数 ÷ T-1 日总涨停数。

    定义:
        - T-1 日涨停总数 = ``len(kpl_prev)``（kpl_list 默认就是涨停榜）
        - T 日连板成功 = T 日 kpl_list 中 ``cons_nums >= 2`` 的条数

    Returns:
        ``[0, 1]`` 区间的 float；prev 为空时返回 None。
    """
    prev_total = len(kpl_prev) if kpl_prev else 0
    if prev_total <= 0:
        return None
    cons_today = sum(1 for r in kpl_today if _cons_nums(r) >= 2)
    return round(cons_today / prev_total, 4)


# ---------------------------------------------------------------------------
# 连板梯队 / 最高板
# ---------------------------------------------------------------------------


def build_limit_ladder(
    kpl_today: Sequence[Dict[str, Any]],
    *,
    high_bucket_threshold: int = 4,
) -> Dict[str, List[Dict[str, Any]]]:
    """根据 ``kpl_list`` 数据构建连板梯队。

    分桶规则：
        * ``cons_nums >= high_bucket_threshold``：归到 ``"{N}连板及以上"``
        * ``cons_nums == 3``：``"3连板"``
        * ``cons_nums == 2``：``"2连板"``
        * 其余（含 ``cons_nums == 1`` / None）：``"首板"``

    每个桶内按 ``cons_nums DESC`` → ``lu_time ASC`` 稳定排序。

    Returns:
        ``{"4连板及以上": [...], "3连板": [...], "2连板": [...], "首板": [...]}``
    """
    high_key = f"{high_bucket_threshold}连板及以上"
    buckets: Dict[str, List[Dict[str, Any]]] = {
        high_key: [],
        "3连板": [],
        "2连板": [],
        "首板": [],
    }
    for raw in kpl_today or []:
        cons = _cons_nums(raw)
        stock = {
            "name": raw.get("name") or "",
            "code": raw.get("ts_code") or "",
            "theme": raw.get("theme") or "",
            "cons_nums": cons,
            "lu_time": raw.get("lu_time") or raw.get("first_time") or "",
            "open_times": _to_int(raw.get("open_times")),
            # ths 三源融合新增字段（缺失时为空字符串/None，渲染端容错）
            "lu_desc": raw.get("lu_desc") or "",
            "limit_up_suc_rate": raw.get("limit_up_suc_rate"),
            "tag": raw.get("tag") or "",
        }
        if cons >= high_bucket_threshold:
            buckets[high_key].append(stock)
        elif cons == 3:
            buckets["3连板"].append(stock)
        elif cons == 2:
            buckets["2连板"].append(stock)
        else:
            buckets["首板"].append(stock)

    for items in buckets.values():
        items.sort(
            key=lambda s: (
                -int(s.get("cons_nums") or 0),
                s.get("lu_time") or "",
            )
        )
    return buckets


def find_max_height(
    kpl_today: Sequence[Dict[str, Any]],
) -> Tuple[int, Optional[str], Optional[str]]:
    """市场最高板。

    Returns:
        ``(高度, 股票名, 题材)``；空时返回 ``(0, None, None)``。
        平高时取第一个（已按 cons_nums DESC → lu_time ASC 排序后的稳定结果）。
    """
    if not kpl_today:
        return 0, None, None
    ranked = sorted(
        list(kpl_today),
        key=lambda r: (-_cons_nums(r), r.get("lu_time") or ""),
    )
    top = ranked[0]
    return _cons_nums(top), top.get("name") or None, top.get("theme") or None


# ---------------------------------------------------------------------------
# 板块指标
# ---------------------------------------------------------------------------


def rank_sectors_by_pct(
    sectors: Sequence[Dict[str, Any]],
    *,
    pct_field: str = "pct_chg",
) -> List[Dict[str, Any]]:
    """对板块按当日涨跌幅倒序，写入 ``rank_today`` 字段（1 起）。

    输入若已含 ``rank_today`` 会被覆盖。涨跌幅缺失视为最低。
    返回**新列表**，不改原对象。
    """
    enriched = [dict(s) for s in sectors]

    def _key(s: Dict[str, Any]) -> float:
        val = safe_pct_chg(s.get(pct_field))
        return val if val is not None else -999.0

    enriched.sort(key=_key, reverse=True)
    for idx, s in enumerate(enriched, 1):
        s["rank_today"] = idx
    return enriched


def calc_sector_5d_pct(
    sector_daily_history: Sequence[Dict[str, Any]],
    *,
    close_field: str = "close",
    trade_date_field: str = "trade_date",
) -> Optional[float]:
    """5 日累计涨幅。

    Args:
        sector_daily_history: ``dc_daily`` 该板块按 ``trade_date`` 升序的 5~6 行。
        close_field/trade_date_field: 可配置字段名。

    定义:
        最近一条 close / 最早一条 close - 1，单位 %

    缺失任一端返回 None。
    """
    rows = [r for r in sector_daily_history if r.get(close_field) is not None]
    if len(rows) < 2:
        return None
    rows = sorted(rows, key=lambda r: str(r.get(trade_date_field) or ""))
    c0 = _to_float(rows[0].get(close_field))
    cn = _to_float(rows[-1].get(close_field))
    if not c0 or not cn:
        return None
    return round((cn / c0 - 1) * 100, 2)


def count_limit_up_in_sector(
    sector_name: str,
    kpl_today: Sequence[Dict[str, Any]],
) -> int:
    """统计某板块名在 ``kpl_list.theme`` 中出现的涨停股数。

    ``theme`` 字段用 ``·`` 分隔多题材，做包含/精确双重匹配。
    """
    if not sector_name or not kpl_today:
        return 0
    sector_clean = sector_name.strip()
    if not sector_clean:
        return 0
    n = 0
    for r in kpl_today:
        themes = re.split(r"[·,，;； /]+", str(r.get("theme") or ""))
        themes = [t.strip() for t in themes if t and t.strip()]
        if sector_clean in themes or any(sector_clean in t for t in themes):
            n += 1
    return n


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # 排除 NaN / Inf
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_cons_nums(row: Dict[str, Any]) -> int:
    """从一条涨停记录抽取「连板数」整数。

    优先级（覆盖三源融合后的全部数据形态）:
        1. ``row["cons_nums"]`` 整数
        2. ``row["status"]`` 形如 ``"3连板"`` → 3（kpl_list）
        3. ``status`` 是 ``"首板"`` / ``"首日"`` → 1（kpl_list）
        4. ``row["tag"]`` 形如 ``"7天5板"`` → 5（同花顺独家，间断梯队）
        5. ``tag`` 是 ``"首板"`` → 1（同花顺）
        6. 兜底 1（当首板处理）

    Returns:
        连板数（>=1 的整数）。
    """
    n = _to_int(row.get("cons_nums"))
    if n is not None and n > 0:
        return n
    status = str(row.get("status") or "")
    m = re.search(r"(\d+)连板", status)
    if m:
        return int(m.group(1))
    if status in ("首板", "首日"):
        return 1
    # 同花顺 tag 兜底（"7天5板" → 5；"首板" → 1）
    tag = str(row.get("tag") or "")
    m = re.match(r"^(\d+)天(\d+)板$", tag)
    if m:
        return int(m.group(2))  # 取板数（不是天数）
    if tag == "首板":
        return 1
    return 1  # 兜底当首板


# 兼容内部老调用
_cons_nums = parse_cons_nums


__all__ = [
    "normalize_amount_to_yi",
    "safe_pct_chg",
    "calc_seal_rate",
    "calc_fail_rate",
    "calc_promotion_rate",
    "build_limit_ladder",
    "find_max_height",
    "rank_sectors_by_pct",
    "calc_sector_5d_pct",
    "count_limit_up_in_sector",
    "parse_cons_nums",
]
